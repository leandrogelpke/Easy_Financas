#!/usr/bin/env python3
"""
merge_historico.py — arquivo histórico acumulado dos lançamentos do Bling.

────────────────────────────────────────────────────────────────────────────
PROBLEMA QUE ESTE SCRIPT RESOLVE
────────────────────────────────────────────────────────────────────────────
O `fetch-bling.py` filtra por `dataEmissaoInicial` com janela deslizante
(7 meses no `update.yml`). Parcelamentos longos são emitidos UMA única vez e
vencem por meses — às vezes anos. Quando a data de EMISSÃO sai da janela,
TODAS as parcelas somem do snapshot, inclusive as que vencem dentro do
período analisado.

Caso real (detectado em 21/ago/2026):
    Buy-out de quotas — ROMULO FERREIRA LIMA
    data_emissao 2025-12-08 · parcelas 13/18 a 18/18 · R$ 59.814,26 cada
    vencimentos jan/2026 → jun/2026 · total R$ 358.885,56

Em 31/jul/2026 a janela começava em 2025-12-01 e pegava tudo. Em 1º/ago/2026
ela passou a começar em 2026-01-01 e os 6 lançamentos evaporaram. A linha
"Buy-out + Acordo trabalhista" da DRE caiu de ~R$ 479K para R$ 120K (só o
acordo do Geremias) sem que nada no CI acusasse erro — o gate valida
consistência, não completude histórica.

Junto foram embora, pelo mesmo motivo: Efata (R$ 210K em aberto), Milajanu,
IRPJ/CSLL do 4T2025 e R$ 100K a receber da TOTVS.

────────────────────────────────────────────────────────────────────────────
SOLUÇÃO
────────────────────────────────────────────────────────────────────────────
`data/bling-api/historico.json` acumula tudo que já foi visto, indexado por
`id`. A cada rodada o merge aplica três regras:

  1. Registro presente no snapshot novo  → snapshot vence (dado fresco).
  2. Registro só no histórico, com emissão FORA da janela buscada
     → preserva. Não foi buscado; ausência não é prova de exclusão.
  3. Registro só no histórico, com emissão DENTRO da janela buscada
     → remove. Ali o snapshot é autoritativo, então sumiu = foi apagado
       no Bling.

Sem `--window-start` o merge é puramente aditivo (nunca remove) — modo
seguro pra rodar à mão.

Transição em aberto → pago: se um id aparece no bucket "pagas/recebidas"
mesclado e o snapshot novo já não o traz mais em "em aberto", ele sai do
em aberto. Isso evita ressuscitar do arquivo uma parcela que já foi baixada.
Não confunda com os "fantasmas" do §6.10 do CLAUDE.md (o mesmo lançamento
aparecendo nos dois buckets no MESMO snapshot) — aquilo continua sendo
tratado por `audit.py::reconcile_em_aberto`, que roda depois.

Depois de mesclar, o script reescreve o snapshot do dia e os 4 CSVs com o
conjunto completo, de modo que `build-html.py` (lê o JSON) e
`dre_render.py` (lê os CSVs) continuam funcionando sem nenhuma alteração.

Idempotente: rodar N vezes na mesma pasta produz o mesmo resultado.

Uso:
    python3 ci/merge_historico.py --dir data/bling-api --window-start 2026-01-01
    python3 ci/merge_historico.py --dir data/bling-api --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# Buckets acumulados. A ordem importa: pagas antes de em_aberto, porque a
# resolução de "já foi baixado" consulta o bucket pago já mesclado.
BUCKETS = [
    "contas_pagar_pagas",
    "contas_receber_recebidas",
    "contas_pagar_em_aberto",
    "contas_receber_em_aberto",
]

# Pares (bucket_liquidado, bucket_em_aberto) pra resolver a transição.
PARES_LIQUIDACAO = [
    ("contas_pagar_pagas", "contas_pagar_em_aberto"),
    ("contas_receber_recebidas", "contas_receber_em_aberto"),
]

# Mesma lista de colunas do fetch-bling.py::CONTAS_COLS. Duplicada de
# propósito: este script precisa rodar mesmo se o fetch falhar.
CONTAS_COLS = [
    "id", "situacao_codigo", "situacao",
    "vencimento", "vencimento_original", "data_emissao", "competencia",
    "valor", "saldo",
    "numero_documento", "historico",
    "contato_id", "contato_nome", "contato_documento",
    "categoria_id", "categoria_descricao", "categoria_tipo",
    "portador_id",
]

HISTORICO_NOME = "historico.json"


def log(msg: str) -> None:
    print(msg, flush=True)


def _atomic_write(path: Path, texto: str) -> None:
    """Escrita atômica — evita snapshot meio-escrito se o runner morrer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texto)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rid(rec: dict) -> str | None:
    """Chave de identidade do lançamento. Sem id não dá pra deduplicar."""
    v = rec.get("id")
    if v in (None, "", 0, "0"):
        return None
    return str(v)


def _dentro_da_janela(rec: dict, window_start: str | None) -> bool:
    """
    O registro estaria no alcance do fetch desta rodada?

    A janela do Bling filtra por data de EMISSÃO — é exatamente por isso que
    parcelamentos antigos somem. Sem data de emissão, cai no vencimento; sem
    os dois, assume FORA da janela (preserva, que é o lado seguro).
    """
    if not window_start:
        return False
    ref = (rec.get("data_emissao") or "").strip() or (rec.get("vencimento") or "").strip()
    if not ref:
        return False
    return ref >= window_start


def carregar_historico(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"_meta": {}, **{b: [] for b in BUCKETS}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # arquivo corrompido não pode derrubar o CI
        log(f"[warn] histórico ilegível ({e}) — recomeçando do zero")
        return {"_meta": {}, **{b: [] for b in BUCKETS}}
    for b in BUCKETS:
        d.setdefault(b, [])
    d.setdefault("_meta", {})
    return d


def achar_snapshot(dir_: Path, explicito: Path | None) -> Path:
    if explicito:
        return explicito
    cands = sorted(dir_.glob("bling_data_*.json"))
    if not cands:
        raise FileNotFoundError(f"nenhum bling_data_*.json em {dir_}")
    return cands[-1]


def merge_bucket(
    hist_rows: list[dict],
    snap_rows: list[dict],
    window_start: str | None,
) -> tuple[list[dict], dict[str, int]]:
    """
    Mescla um bucket. Devolve (linhas, estatísticas).

    Estatísticas: novos, atualizados, preservados (fora da janela),
    removidos (apagados no Bling), sem_id (mantidos como estão).
    """
    stats = {"novos": 0, "atualizados": 0, "preservados": 0, "removidos": 0, "sem_id": 0}

    snap_idx: dict[str, dict] = {}
    sem_id_snap: list[dict] = []
    for r in snap_rows:
        rid = _rid(r)
        if rid is None:
            sem_id_snap.append(r)
        else:
            snap_idx[rid] = r

    out: dict[str, dict] = {}

    # 1) Passa o histórico, decidindo preservar x remover.
    for r in hist_rows:
        rid = _rid(r)
        if rid is None:
            stats["sem_id"] += 1
            continue
        if rid in snap_idx:
            continue  # tratado no laço seguinte (snapshot vence)
        if _dentro_da_janela(r, window_start):
            stats["removidos"] += 1  # estava no alcance do fetch e sumiu
            continue
        out[rid] = r
        stats["preservados"] += 1

    # 2) Snapshot sobrescreve / adiciona.
    hist_ids = {_rid(r) for r in hist_rows}
    for rid, r in snap_idx.items():
        if rid in hist_ids:
            stats["atualizados"] += 1
        else:
            stats["novos"] += 1
        out[rid] = r

    linhas = list(out.values()) + sem_id_snap
    linhas.sort(key=lambda r: ((r.get("vencimento") or ""), str(r.get("id") or "")))
    return linhas, stats


def resolver_liquidados(
    merged: dict[str, list[dict]],
    snap: dict[str, Any],
) -> dict[str, int]:
    """
    Tira do "em aberto" o que o arquivo histórico ressuscitaria mas que já
    consta como pago/recebido — e que o snapshot fresco já não lista mais
    como em aberto.

    Só age em registros que NÃO vieram do snapshot desta rodada. Se o Bling
    ainda devolve o lançamento em aberto, ele fica: o duplo-registro
    legítimo é problema do `reconcile_em_aberto`, não deste script.
    """
    baixas: dict[str, int] = {}
    for bucket_pago, bucket_aberto in PARES_LIQUIDACAO:
        pagos_ids = {_rid(r) for r in merged.get(bucket_pago, [])}
        pagos_ids.discard(None)
        snap_abertos = {_rid(r) for r in snap.get(bucket_aberto, []) or []}
        antes = len(merged.get(bucket_aberto, []))
        merged[bucket_aberto] = [
            r for r in merged.get(bucket_aberto, [])
            if _rid(r) in snap_abertos or _rid(r) not in pagos_ids
        ]
        n = antes - len(merged[bucket_aberto])
        if n:
            baixas[bucket_aberto] = n
    return baixas


def escrever_csvs(dir_: Path, merged: dict[str, list[dict]], stem: str) -> None:
    """Reescreve os CSVs que o dre_render.py consome (_load_bling_csvs)."""
    for bucket, rows in merged.items():
        path = dir_ / f"{bucket}_{stem}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CONTAS_COLS, delimiter=";")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in CONTAS_COLS})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path("data/bling-api"),
                    help="pasta com bling_data_*.json e os CSVs (default: data/bling-api)")
    ap.add_argument("--snapshot", type=Path, default=None,
                    help="snapshot específico (default: o mais recente da pasta)")
    ap.add_argument("--historico", type=Path, default=None,
                    help=f"arquivo do histórico (default: <dir>/{HISTORICO_NOME})")
    ap.add_argument("--window-start", default=None,
                    help="início da janela de emissão usada no fetch (YYYY-MM-DD). "
                         "Sem isso o merge é só aditivo — nunca remove nada.")
    ap.add_argument("--dry-run", action="store_true", help="não escreve nada, só relata")
    args = ap.parse_args()

    dir_ = args.dir
    if not dir_.exists():
        log(f"[erro] pasta inexistente: {dir_}")
        return 1

    hist_path = args.historico or (dir_ / HISTORICO_NOME)

    try:
        snap_path = achar_snapshot(dir_, args.snapshot)
    except FileNotFoundError as e:
        log(f"[erro] {e}")
        return 1

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    hist = carregar_historico(hist_path)

    log(f"[merge] snapshot .. {snap_path.name}")
    log(f"[merge] histórico . {hist_path.name} ({'novo' if not hist_path.exists() else 'existente'})")
    log(f"[merge] janela .... {args.window_start or 'não informada (modo aditivo)'}")

    merged: dict[str, list[dict]] = {}
    total_preservado = 0
    total_removido = 0
    for b in BUCKETS:
        linhas, st = merge_bucket(hist.get(b, []), snap.get(b, []) or [], args.window_start)
        merged[b] = linhas
        total_preservado += st["preservados"]
        total_removido += st["removidos"]
        log(
            f"  {b:28s} {len(linhas):4d} linhas "
            f"(+{st['novos']} novos · {st['atualizados']} atualizados · "
            f"{st['preservados']} preservados · -{st['removidos']} removidos)"
        )

    baixas = resolver_liquidados(merged, snap)
    for bucket, n in baixas.items():
        log(f"  [baixa] {bucket}: -{n} já liquidado(s), removido(s) do em aberto")

    log(f"[merge] preservados do arquivo: {total_preservado} · removidos (apagados no Bling): {total_removido}")

    if args.dry_run:
        log("[merge] dry-run — nada foi escrito")
        return 0

    # ── grava o histórico ──
    novo_hist: dict[str, Any] = {
        "_meta": {
            "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_origem": snap_path.name,
            "window_start": args.window_start,
            "totais": {b: len(merged[b]) for b in BUCKETS},
        },
        **{b: merged[b] for b in BUCKETS},
    }
    _atomic_write(hist_path, json.dumps(novo_hist, ensure_ascii=False, indent=1))
    log(f"[ok] histórico -> {hist_path.name}")

    # ── reescreve o snapshot do dia com o conjunto completo ──
    # build-html.py lê daqui; sem isso o merge não chega ao dashboard.
    snap_out = dict(snap)
    for b in BUCKETS:
        snap_out[b] = merged[b]
    meta = dict(snap_out.get("_metadata") or {})
    meta["merged_historico"] = True
    meta["window_start"] = args.window_start
    meta["preservados_do_historico"] = total_preservado
    snap_out["_metadata"] = meta
    _atomic_write(snap_path, json.dumps(snap_out, ensure_ascii=False, indent=2))
    log(f"[ok] snapshot -> {snap_path.name} (com {total_preservado} linha(s) do arquivo)")

    # ── reescreve os CSVs que o dre_render consome ──
    stem = snap_path.stem.replace("bling_data_", "")
    escrever_csvs(dir_, merged, stem)
    log(f"[ok] CSVs reescritos (sufixo _{stem}.csv)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
