#!/usr/bin/env python3
"""
precommit_check.py — GATE DURO pré-commit do agente principal.

Roda em update.yml DEPOIS do build e ANTES do commit/push. Valida os
artefatos recém-gerados (snapshot do Bling + index.html + saídas da
auditoria/contas). Se achar problema CRÍTICO, sai com código != 0 → o
commit/push é pulado e o dashboard publicado PERMANECE na última versão
boa (fail-safe). Diferente do auditor pós-commit (ci_auditor.py), que é
analítico e abre Issues; aqui o objetivo é BLOQUEAR dado ruim antes de ir
ao ar.

Checagens críticas (bloqueiam):
  - snapshot bling_data ausente / 0 registros
  - datasets-núcleo vazios (pagas, recebidas)
  - index.html ausente, < 500 KB, com marcadores @@...@@ ou < 12 abas
  - auditoria com 0 findings  → sinaliza abas Auditoria/DRE/Contas vazias
    (regressão clássica quando TOTVS_SNAP.parent não tem os CSVs)

Avisos (não bloqueiam, só reportam):
  - audit_findings.json / contas_snapshot.json ausentes
  - queda de volume > 60% vs. snapshot anterior

Stdlib only. Saída: rc 0 (ok) | rc 1 (bloqueado).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SNAP_DIR = HERE / "data" / "bling-api"
INDEX = HERE / "index.html"

DATASETS = [
    "contas_pagar_em_aberto",
    "contas_pagar_pagas",
    "contas_receber_em_aberto",
    "contas_receber_recebidas",
]
MIN_INDEX_BYTES = 500_000
MIN_PGS = 12


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None


def main() -> int:
    critical: list[str] = []
    warns: list[str] = []

    # ── 1. snapshot do Bling ──────────────────────────────────────
    snaps = sorted(SNAP_DIR.glob("bling_data_*.json"))
    data = None
    if not snaps:
        critical.append("nenhum bling_data_*.json em data/bling-api (fetch falhou?)")
    else:
        data = _load(snaps[-1])
        if data is None:
            critical.append(f"snapshot {snaps[-1].name} ilegível/corrompido")
        else:
            total = sum(len(data.get(ds, []) or []) for ds in DATASETS)
            if total == 0:
                critical.append("snapshot com 0 registros nos 4 datasets")
            for ds in ("contas_pagar_pagas", "contas_receber_recebidas"):
                if len(data.get(ds, []) or []) == 0:
                    critical.append(f"dataset-núcleo vazio: {ds}")

    # ── 2. index.html ─────────────────────────────────────────────
    if not INDEX.exists():
        critical.append("index.html não foi gerado")
    else:
        html = INDEX.read_text(encoding="utf-8")
        markers = sorted(set(re.findall(r"@@[A-Z_]+@@", html)))
        if markers:
            critical.append(f"marcadores não substituídos: {', '.join(markers[:6])}")
        size = INDEX.stat().st_size
        if size < MIN_INDEX_BYTES:
            critical.append(f"index.html muito pequeno ({size} bytes < {MIN_INDEX_BYTES})")
        npg = len(re.findall(r'<div class="pg(?:\s+active)?"\s+id="pg-', html))
        if npg < MIN_PGS:
            critical.append(f"apenas {npg} abas .pg (esperado >= {MIN_PGS}) — possível div desbalanceada")

    # ── 3. auditoria não-vazia (pega regressão de _bling_dir) ─────
    af = SNAP_DIR / "audit_findings.json"
    if af.exists():
        d = _load(af) or {}
        n = d.get("n_findings", 0)
        if n == 0:
            critical.append("auditoria com 0 findings — abas Auditoria/P&L/DRE/Contas "
                            "provavelmente vazias (TOTVS_SNAP.parent sem os CSVs?)")
    else:
        warns.append("audit_findings.json ausente — auditoria não validada")

    # ── 4. contas snapshot ────────────────────────────────────────
    cs = SNAP_DIR / "contas_snapshot.json"
    if cs.exists():
        t = (_load(cs) or {}).get("totais")
        if not t:
            warns.append("contas_snapshot.json sem 'totais'")
    else:
        warns.append("contas_snapshot.json ausente")

    # ── 5. queda anômala de volume (warn) ─────────────────────────
    if data is not None and len(snaps) >= 2:
        prev = _load(snaps[-2])
        if prev:
            cur_total = sum(len(data.get(ds, []) or []) for ds in DATASETS)
            prev_total = sum(len(prev.get(ds, []) or []) for ds in DATASETS)
            if prev_total and cur_total < prev_total * 0.4:
                warns.append(f"volume caiu de {prev_total} para {cur_total} (>60%) vs. snapshot anterior")

    # ── Relatório ─────────────────────────────────────────────────
    print("── precommit_check ──")
    for w in warns:
        print(f"  [warn] {w}")
    if critical:
        for c in critical:
            print(f"  [CRÍTICO] {c}")
        print(f"BLOQUEADO: {len(critical)} problema(s) crítico(s) — commit/push NÃO será feito.")
        return 1
    print(f"OK: artefatos válidos ({len(warns)} aviso(s) não-bloqueante(s)). Liberado para commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
