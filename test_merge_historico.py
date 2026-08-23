#!/usr/bin/env python3
"""
test_merge_historico.py — trava de regressão do arquivo histórico do Bling.

O bug que motivou tudo isto (21/ago/2026): a janela do fetch é por data de
EMISSÃO, e o buy-out do Rômulo foi emitido uma única vez (2025-12-08) com 18
parcelas. Quando a janela avançou para 2026-01-01, as 6 parcelas que vencem em
2026 sumiram do snapshot de uma vez — R$ 358.885,56 evaporaram da DRE sem que
nenhum gate reclamasse.

Cada teste aqui corresponde a uma regra que, se quebrar, traz o bug de volta.
Rodar: python3 test_merge_historico.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "ci"))

from ci.merge_historico import (  # noqa: E402
    BUCKETS,
    merge_bucket,
    resolver_liquidados,
    _dentro_da_janela,
)

_falhas: list[str] = []


def check(nome: str, cond: bool, detalhe: str = "") -> None:
    if cond:
        print(f"  ok  {nome}")
    else:
        print(f"  XX  {nome} {detalhe}")
        _falhas.append(nome)


def rec(id_: int, emissao: str, venc: str, valor: str = "100,00", nome: str = "FULANO") -> dict:
    return {
        "id": id_, "data_emissao": emissao, "vencimento": venc,
        "valor": valor, "contato_nome": nome, "historico": "",
    }


# ── 1. O caso Rômulo: emissão fora da janela precisa sobreviver ──────────────
def test_preserva_parcelamento_fora_da_janela() -> None:
    buyout = [rec(i, "2025-12-08", f"2026-0{m}-25", "59814,26", "ROMULO FERREIRA LIMA")
              for i, m in zip(range(1, 7), range(1, 7))]
    linhas, st = merge_bucket(hist_rows=buyout, snap_rows=[], window_start="2026-01-01")
    check("parcelamento emitido antes da janela é preservado",
          len(linhas) == 6, f"(sobraram {len(linhas)})")
    check("nada é contado como removido", st["removidos"] == 0)
    total = sum(float(r["valor"].replace(".", "").replace(",", ".")) for r in linhas)
    check("valor do buy-out intacto (R$ 358.885,56)", abs(total - 358885.56) < 0.01,
          f"(deu {total:.2f})")


# ── 2. Dentro da janela, ausência = exclusão de verdade ─────────────────────
def test_remove_apagado_dentro_da_janela() -> None:
    hist = [rec(1, "2026-03-10", "2026-04-10"), rec(2, "2026-03-11", "2026-04-11")]
    snap = [rec(1, "2026-03-10", "2026-04-10")]
    linhas, st = merge_bucket(hist, snap, window_start="2026-01-01")
    ids = {r["id"] for r in linhas}
    check("registro apagado no Bling (dentro da janela) some", ids == {1}, f"(ficou {ids})")
    check("contabiliza a remoção", st["removidos"] == 1)


# ── 3. Sem janela informada, o merge não pode remover nada ──────────────────
def test_modo_aditivo_nunca_remove() -> None:
    hist = [rec(1, "2026-03-10", "2026-04-10"), rec(2, "2026-03-11", "2026-04-11")]
    linhas, st = merge_bucket(hist, [], window_start=None)
    check("sem window_start o merge é puramente aditivo",
          len(linhas) == 2 and st["removidos"] == 0)


# ── 4. Dado fresco vence o arquivado ────────────────────────────────────────
def test_snapshot_sobrescreve_historico() -> None:
    hist = [rec(1, "2026-03-10", "2026-04-10", "100,00")]
    snap = [rec(1, "2026-03-10", "2026-04-15", "250,00")]
    linhas, st = merge_bucket(hist, snap, window_start="2026-01-01")
    check("snapshot sobrescreve o histórico",
          len(linhas) == 1 and linhas[0]["valor"] == "250,00" and linhas[0]["vencimento"] == "2026-04-15")
    check("conta como atualizado, não como novo", st["atualizados"] == 1 and st["novos"] == 0)


# ── 5. Em aberto que virou pago não pode ressuscitar ────────────────────────
def test_liquidado_sai_do_em_aberto() -> None:
    merged = {b: [] for b in BUCKETS}
    merged["contas_pagar_pagas"] = [rec(7, "2025-11-01", "2026-02-10")]
    merged["contas_pagar_em_aberto"] = [rec(7, "2025-11-01", "2026-02-10"),
                                        rec(8, "2025-11-01", "2026-09-10")]
    snap = {"contas_pagar_em_aberto": [rec(8, "2025-11-01", "2026-09-10")],
            "contas_receber_em_aberto": []}
    baixas = resolver_liquidados(merged, snap)
    ids = {r["id"] for r in merged["contas_pagar_em_aberto"]}
    check("lançamento já pago sai do em aberto", ids == {8}, f"(ficou {ids})")
    check("baixa é reportada", baixas.get("contas_pagar_em_aberto") == 1)


# ── 6. Se o Bling ainda devolve como em aberto, o merge não interfere ───────
#     (o duplo-registro legítimo é problema do audit.reconcile_em_aberto)
def test_nao_interfere_no_fantasma_do_bling() -> None:
    merged = {b: [] for b in BUCKETS}
    merged["contas_pagar_pagas"] = [rec(9, "2025-11-01", "2026-02-10")]
    merged["contas_pagar_em_aberto"] = [rec(9, "2025-11-01", "2026-02-10")]
    snap = {"contas_pagar_em_aberto": [rec(9, "2025-11-01", "2026-02-10")],
            "contas_receber_em_aberto": []}
    resolver_liquidados(merged, snap)
    check("duplicata vinda do snapshot atual é deixada pro reconcile_em_aberto",
          len(merged["contas_pagar_em_aberto"]) == 1)


# ── 7. Semântica da janela ──────────────────────────────────────────────────
def test_semantica_da_janela() -> None:
    check("emissão anterior à janela → fora",
          _dentro_da_janela(rec(1, "2025-12-08", "2026-05-25"), "2026-01-01") is False)
    check("emissão dentro da janela → dentro",
          _dentro_da_janela(rec(1, "2026-02-08", "2026-05-25"), "2026-01-01") is True)
    check("sem emissão, cai no vencimento",
          _dentro_da_janela({"vencimento": "2026-05-25"}, "2026-01-01") is True)
    check("sem data nenhuma → fora (preserva, lado seguro)",
          _dentro_da_janela({}, "2026-01-01") is False)


# ── 8. Idempotência: rodar de novo não muda nada ────────────────────────────
def test_idempotente() -> None:
    hist = [rec(1, "2025-12-08", "2026-01-25"), rec(2, "2026-02-01", "2026-03-01")]
    snap = [rec(2, "2026-02-01", "2026-03-01")]
    l1, _ = merge_bucket(hist, snap, "2026-01-01")
    l2, _ = merge_bucket(l1, snap, "2026-01-01")
    check("merge é idempotente", json.dumps(l1, sort_keys=True) == json.dumps(l2, sort_keys=True))


# ── 10. Fatiamento da janela de emissão (limite de 366 dias do Bling) ───────
#     Bug de 21-22/ago/2026: o bootstrap de 24 meses mandava dataEmissaoInicial
#     2 anos atrás numa chamada só → HTTP 400 "Período do filtro é maior que o
#     permitido (366 dias)" → todos os runs do update.yml falhavam no fetch.
def _load_fetch_bling():
    import importlib.util
    p = Path(__file__).resolve().parent / "fetch-bling.py"
    spec = importlib.util.spec_from_file_location("fetch_bling", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fatiar_janela_emissao() -> None:
    from datetime import date as _d, timedelta as _td
    fb = _load_fetch_bling()

    # rodada normal (7 meses, com data_final): janela única, params intactos
    j = fb._fatiar_janela_emissao("2026-02-01", "2026-08-22")
    check("janela ≤360d não é fatiada", j == [("2026-02-01", "2026-08-22")])

    # o caso do bug: bootstrap de 24 meses precisa virar várias janelas
    j = fb._fatiar_janela_emissao("2024-08-01", "2026-08-22")
    check("24 meses são fatiados em 3 janelas", len(j) == 3, f"(deu {len(j)})")
    ok_cont = all(
        _d.fromisoformat(j[i + 1][0]) == _d.fromisoformat(j[i][1]) + _td(days=1)
        for i in range(len(j) - 1))
    check("janelas contíguas (sem buraco nem sobreposição)", ok_cont, f"({j})")
    ok_tam = all(
        (_d.fromisoformat(f) - _d.fromisoformat(i0)).days < 366
        for i0, f in j if f)
    check("cada janela cabe no limite de 366 dias do Bling", ok_tam, f"({j})")
    check("última janela termina no data_final pedido", j[-1][1] == "2026-08-22")

    # sem data_final: a ÚLTIMA janela fica aberta (fim=None), preservando o
    # comportamento "até hoje" da chamada original
    ini_curta = (_d.today() - _td(days=30)).isoformat()
    check("janela curta sem data_final fica aberta",
          fb._fatiar_janela_emissao(ini_curta) == [(ini_curta, None)])
    ini_longa = (_d.today() - _td(days=730)).isoformat()
    j = fb._fatiar_janela_emissao(ini_longa)
    check("janela longa sem data_final: última fatia aberta",
          len(j) >= 2 and j[-1][1] is None, f"({j})")
    check("fatia aberta começa a ≤360 dias de hoje",
          (_d.today() - _d.fromisoformat(j[-1][0])).days < 366)

    # degradação segura: entrada estranha devolve a janela original
    check("data invertida não fatia (API decide, como antes)",
          fb._fatiar_janela_emissao("2026-08-01", "2026-01-01")
          == [("2026-08-01", "2026-01-01")])


# ── 11. enrich_and_dump defensivo com dados antigos do Bling ────────────────
#     Bug de 23/ago/2026 (bootstrap 24m, runs 07h e 12h): lançamentos antigos
#     trazem `borderos` como lista de IDs (int), não de dicts → AttributeError
#     em (borderos[0]).get("data") derrubava o fetch inteiro no enrich.
def test_enrich_defensivo_dados_antigos() -> None:
    fb = _load_fetch_bling()
    base = {"id": 1, "situacao": 2, "valor": "10"}

    r = fb.enrich_and_dump([dict(base, borderos=[12345])], "pagar", {}, {})
    check("borderos como lista de ids não explode (data vazia)",
          r[0]["data_pagamento"] == "", f"(deu {r[0]['data_pagamento']!r})")

    r = fb.enrich_and_dump([dict(base, borderos=[{"data": "2026-01-05"}])],
                           "pagar", {}, {})
    check("bordero dict devolve a data", r[0]["data_pagamento"] == "2026-01-05")

    r = fb.enrich_and_dump([dict(base, dataPagamento="2026-02-01",
                                 borderos=[999])], "pagar", {}, {})
    check("dataPagamento explícita vence o borderô",
          r[0]["data_pagamento"] == "2026-02-01")

    r = fb.enrich_and_dump([dict(base, borderos=[111, {"data": "2026-03-09"}])],
                           "pagar", {}, {})
    check("lista mista de borderôs acha o primeiro dict",
          r[0]["data_pagamento"] == "2026-03-09")

    r = fb.enrich_and_dump([dict(base, contato=7, categoria=None, portador=[])],
                           "pagar", {}, {})
    check("contato/categoria/portador não-dict não explodem",
          r[0]["contato_id"] == "" and r[0]["categoria_id"] == ""
          and r[0]["portador_id"] == "")


# ── 9. Registro sem id não pode virar duplicata infinita ────────────────────
def test_sem_id_nao_acumula() -> None:
    sem_id = {"vencimento": "2026-04-01", "valor": "10,00", "contato_nome": "X"}
    l1, _ = merge_bucket([sem_id], [sem_id], "2026-01-01")
    l2, _ = merge_bucket(l1, [sem_id], "2026-01-01")
    check("linha sem id não se multiplica a cada rodada",
          len(l1) == 1 and len(l2) == 1, f"(l1={len(l1)} l2={len(l2)})")


def main() -> int:
    print("── test_merge_historico ──")
    for fn in (
        test_preserva_parcelamento_fora_da_janela,
        test_remove_apagado_dentro_da_janela,
        test_modo_aditivo_nunca_remove,
        test_snapshot_sobrescreve_historico,
        test_liquidado_sai_do_em_aberto,
        test_nao_interfere_no_fantasma_do_bling,
        test_semantica_da_janela,
        test_idempotente,
        test_sem_id_nao_acumula,
        test_fatiar_janela_emissao,
        test_enrich_defensivo_dados_antigos,
    ):
        fn()
    print()
    if _falhas:
        print(f"FALHOU — {len(_falhas)} teste(s): {', '.join(_falhas)}")
        return 1
    print("OK — todos os testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
