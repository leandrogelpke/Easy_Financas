#!/usr/bin/env python3
"""
write_update_log.py — grava o log detalhado de uma execução do agente
principal (update.yml) em logs/update_YYYY-MM-DD_HH.json.

Lê o estado de cada etapa dos GITHUB_OUTPUTs (via env), o snapshot Bling
gerado (para contagem de registros) e monta o JSON consumido depois pelo
agente auditor (audit.yml → ci_auditor.py).

Stdlib only. Horários em BRT (America/Sao_Paulo = UTC-3).
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BRT = timezone(timedelta(hours=-3))
HERE = Path(__file__).resolve().parent.parent
LOGS_DIR = HERE / "logs"
SNAP_DIR = HERE / "data" / "bling-api"

DATASETS = [
    "contas_pagar_em_aberto",
    "contas_pagar_pagas",
    "contas_receber_em_aberto",
    "contas_receber_recebidas",
]


def _norm(outcome: str | None) -> str:
    """Normaliza outcome do Actions ('success'/'failure'/'skipped'/'')."""
    o = (outcome or "").strip().lower()
    if o in ("success", "failure", "skipped", "cancelled"):
        return o
    return "skipped"


def latest_snapshot() -> Path | None:
    snaps = sorted(SNAP_DIR.glob("bling_data_*.json"))
    return snaps[-1] if snaps else None


def count_records() -> dict:
    snap = latest_snapshot()
    rec: dict = {"snapshot_file": None, "fetched_at": None, "total": 0}
    if not snap:
        return rec
    rec["snapshot_file"] = snap.name
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rec["parse_error"] = str(e)
        return rec
    rec["fetched_at"] = (data.get("_metadata") or {}).get("fetched_at")
    total = 0
    for ds in DATASETS:
        n = len(data.get(ds, []) or [])
        rec[ds] = n
        total += n
    rec["categorias"] = len(data.get("categorias", []) or [])
    rec["contatos"] = len(data.get("contatos", []) or [])
    rec["total"] = total
    return rec


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_brt = now_utc.astimezone(BRT)

    start_ts = os.environ.get("START_TS", "").strip()
    try:
        started_utc = datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
        duration_s = int((now_utc - started_utc).total_seconds())
    except (ValueError, TypeError):
        started_utc = None
        duration_s = None

    steps = {
        "fetch": _norm(os.environ.get("OUT_FETCH")),
        "merge_historico": _norm(os.environ.get("OUT_MERGE")),
        "token_refresh": (os.environ.get("TOKEN_STATUS") or "skipped").strip() or "skipped",
        "build": _norm(os.environ.get("OUT_BUILD")),
        "gate_test_audit": _norm(os.environ.get("OUT_GATE_A")),
        "gate_test_build": _norm(os.environ.get("OUT_GATE_B")),
        "gate_test_merge": _norm(os.environ.get("OUT_GATE_M")),
        "precommit": _norm(os.environ.get("OUT_PRECOMMIT")),
        "push": (os.environ.get("OUT_PUSH") or _norm(os.environ.get("PUSH_OUTCOME"))).strip(),
    }

    records = count_records()

    warnings: list[str] = []
    errors: list[str] = []

    if steps["fetch"] != "success":
        errors.append("Fetch do Bling não concluiu com sucesso.")
    if steps["merge_historico"] not in ("success", "skipped", ""):
        errors.append("Merge com o histórico acumulado falhou — lançamentos "
                      "fora da janela do fetch (parcelamentos longos) podem "
                      "estar faltando na DRE.")
    if steps["build"] != "success":
        errors.append("Geração do index.html (build) falhou.")
    if steps["gate_test_audit"] == "failure":
        errors.append("Gate test_audit FALHOU (regressão tributária).")
    if steps["gate_test_build"] == "failure":
        errors.append("Gate test_build FALHOU (estrutura do HTML).")
    if steps["gate_test_merge"] == "failure":
        errors.append("Gate test_merge_historico FALHOU (acúmulo do histórico do Bling).")
    if steps["precommit"] == "failure":
        errors.append("Auditor pré-commit BLOQUEOU a publicação (artefato/dado inválido).")
    # publicação bloqueada pelo gate → push 'skipped'
    blocked = any(steps[k] != "success" for k in
                  ("fetch", "build", "gate_test_audit", "gate_test_build",
                   "gate_test_merge", "precommit"))
    if blocked and steps["push"] in ("skipped", ""):
        warnings.append("Publicação bloqueada pelo gate — última versão boa mantida no ar.")
    elif steps["push"] not in ("success", "nothing", "skipped"):
        warnings.append("Push do dashboard não confirmado.")
    if steps["push"] == "nothing":
        warnings.append("Nenhuma mudança publicada (dados idênticos à última execução).")
    if steps["token_refresh"] == "rotated_not_saved":
        errors.append("Token do Bling rotacionou mas não foi persistido (GH_PAT ausente).")
    if records.get("total", 0) == 0 and steps["fetch"] == "success":
        warnings.append("Fetch reportou sucesso mas 0 registros — possível janela vazia ou filtro.")
    for ds in ("contas_pagar_pagas", "contas_receber_recebidas"):
        if steps["fetch"] == "success" and records.get(ds, 0) == 0:
            warnings.append(f"Dataset '{ds}' veio vazio.")

    log = {
        "run": {
            "workflow": "update",
            "agente": "principal",
            "trigger": os.environ.get("TRIGGER"),
            "run_id": os.environ.get("RUN_ID"),
            "run_number": os.environ.get("RUN_NUMBER"),
            "run_url": os.environ.get("RUN_URL"),
            "started_at_utc": started_utc.strftime("%Y-%m-%d %H:%M:%S") if started_utc else None,
            "ended_at_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at_brt": now_brt.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": duration_s,
        },
        "steps": steps,
        "records": records,
        "commit_hash": (os.environ.get("COMMIT_SHA") or "").strip() or None,
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
    }

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"update_{now_brt.strftime('%Y-%m-%d_%H')}.json"
    path = LOGS_DIR / fname
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[log] gravado: logs/{fname}  (ok={log['ok']}, "
          f"registros={records.get('total')}, erros={len(errors)}, avisos={len(warnings)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
