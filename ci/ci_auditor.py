#!/usr/bin/env python3
"""
ci_auditor.py — Agente AUDITOR do pipeline Easy Analytics.

Roda logo após cada execução do agente principal (update.yml). Não toca em
nenhum HTML publicado — apenas analisa, reporta e sugere. Faz:

  1. Localiza o log mais recente do agente principal (logs/update_*.json).
  2. Avalia a qualidade da execução:
       - fetch do Bling concluiu e trouxe registros completos
       - build do index.html sem erros + gates de teste verdes
       - push bem-sucedido (commit gerado)
       - padrões de falha recorrentes nos últimos 7 dias
       - sanidade dos dados (nulos, datas inconsistentes, campos vazios,
         queda anômala de volume vs. execução anterior)
  3. Grava logs/audit_YYYY-MM-DD_HH.json com achados e recomendações.
  4. Abre Issue no GitHub se houver problema (dedupe por janela).
  5. Atualiza IMPROVEMENTS.md com recomendações recorrentes (>=2x em 7d)
     para revisão humana.

Stdlib only + `gh` CLI (via subprocess). Horários em BRT (UTC-3).
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

BRT = timezone(timedelta(hours=-3))
HERE = Path(__file__).resolve().parent.parent
LOGS_DIR = HERE / "logs"
SNAP_DIR = HERE / "data" / "bling-api"
IMPROVEMENTS = HERE / "IMPROVEMENTS.md"

REPO = os.environ.get("GITHUB_REPOSITORY", "leandrogelpke/Easy_Financas")
ISSUE_LABEL = "auditoria-automatica"

DATASETS = [
    "contas_pagar_em_aberto",
    "contas_pagar_pagas",
    "contas_receber_em_aberto",
    "contas_receber_recebidas",
]

SEV_ORDER = {"error": 0, "warn": 1, "info": 2, "ok": 3}


# ──────────────────────────────────────────────────────────────────
# Util
# ──────────────────────────────────────────────────────────────────
def load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def update_logs_sorted() -> list[Path]:
    return sorted(LOGS_DIR.glob("update_*.json"))


def audit_logs_sorted() -> list[Path]:
    return sorted(LOGS_DIR.glob("audit_*.json"))


def parse_stamp_from_name(name: str) -> datetime | None:
    # update_YYYY-MM-DD_HH.json
    m = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{2})\.json$", name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H").replace(tzinfo=BRT)
    except ValueError:
        return None


def latest_snapshot() -> Path | None:
    snaps = sorted(SNAP_DIR.glob("bling_data_*.json"))
    return snaps[-1] if snaps else None


def gh(*args: str, input_text: str | None = None) -> tuple[int, str]:
    """Chama o gh CLI. Retorna (rc, output). Tolerante a ausência do gh/token."""
    try:
        r = subprocess.run(
            ["gh", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"gh indisponível: {e}"


# ──────────────────────────────────────────────────────────────────
# Checks
# ──────────────────────────────────────────────────────────────────
def finding(check: str, severity: str, detail: str, recommendation: str = "") -> dict:
    return {"check": check, "severity": severity, "detail": detail,
            "recommendation": recommendation}


def check_execution(update_log: dict) -> list[dict]:
    out: list[dict] = []
    steps = update_log.get("steps", {})
    rec = update_log.get("records", {})

    # fetch
    if steps.get("fetch") == "success":
        out.append(finding("fetch_bling", "ok", "Fetch do Bling concluído com sucesso."))
    else:
        out.append(finding(
            "fetch_bling", "error",
            f"Fetch do Bling não concluiu (status={steps.get('fetch')}).",
            "Verificar validade do token Bling (refresh/rotação) e disponibilidade da API v3."))

    # registros completos
    total = rec.get("total", 0)
    if steps.get("fetch") == "success":
        if total <= 0:
            out.append(finding("registros_volume", "error",
                               "Fetch OK porém 0 registros consolidados.",
                               "Conferir filtro --data-inicial e situações; possível janela vazia."))
        else:
            faltando = [ds for ds in DATASETS if rec.get(ds, 0) == 0]
            if faltando:
                out.append(finding("registros_completos", "warn",
                                   f"Datasets vazios: {', '.join(faltando)}.",
                                   "Confirmar se é esperado (ex.: sem contas a receber em aberto) ou falha de paginação."))
            else:
                out.append(finding("registros_completos", "ok",
                                   f"{total} registros em 4 datasets ("
                                   + ", ".join(f"{ds}={rec.get(ds,0)}" for ds in DATASETS) + ")."))

    # build
    if steps.get("build") == "success":
        out.append(finding("build_html", "ok", "index.html gerado sem erros."))
    else:
        out.append(finding("build_html", "error",
                           f"Build do index.html falhou (status={steps.get('build')}).",
                           "Inspecionar log do step 'Gerar index.html'; conferir template e módulos de render."))

    # gates
    for gate, key in (("test_audit", "gate_test_audit"), ("test_build", "gate_test_build")):
        st = steps.get(key)
        if st == "success":
            out.append(finding(f"gate_{gate}", "ok", f"Gate {gate} verde."))
        elif st == "failure":
            out.append(finding(f"gate_{gate}", "error", f"Gate {gate} FALHOU.",
                               "Bloqueia confiança nos números. Corrigir antes de confiar no dashboard."))
        else:
            out.append(finding(f"gate_{gate}", "warn", f"Gate {gate} não executou (status={st})."))

    # auditor pré-commit (gate duro)
    pc = steps.get("precommit")
    if pc == "success":
        out.append(finding("precommit_gate", "ok", "Auditor pré-commit liberou a publicação."))
    elif pc == "failure":
        out.append(finding("precommit_gate", "error",
                           "Auditor pré-commit BLOQUEOU a publicação (dado/artefato inválido).",
                           "Ver log do step 'Auditor pré-commit' no run; dashboard manteve a última versão boa."))

    # push
    push = steps.get("push")
    if push == "success":
        out.append(finding("push_pages", "ok",
                           f"Publicado. Commit {update_log.get('commit_hash')}."))
    elif push == "nothing":
        out.append(finding("push_pages", "info",
                           "Nenhuma mudança publicada (dados idênticos à execução anterior)."))
    else:
        out.append(finding("push_pages", "error",
                           f"Push não confirmado (status={push}).",
                           "Conferir permissão contents:write e conflito de push."))

    # token
    tok = steps.get("token_refresh")
    if tok == "rotated_not_saved":
        out.append(finding("token_rotacao", "error",
                           "refresh_token do Bling rotacionou mas não foi persistido (GH_PAT ausente).",
                           "Configurar Secret GH_PAT com escopo de secrets, senão a próxima execução falha."))
    elif tok == "rotated_saved":
        out.append(finding("token_rotacao", "info", "Token rotacionado e Secret BLING_TOKEN atualizado."))

    return out


def check_data_sanity(snap_path: Path | None) -> list[dict]:
    out: list[dict] = []
    if not snap_path:
        out.append(finding("dados_sanidade", "warn", "Snapshot Bling não encontrado para checagem de dados."))
        return out
    data = load_json(snap_path)
    if not data:
        out.append(finding("dados_sanidade", "error", f"Snapshot {snap_path.name} ilegível/corrompido.",
                           "Reexecutar fetch; conferir escrita atômica."))
        return out

    today = date.today()
    problemas: list[str] = []
    nome_vazio_total = 0
    nome_total = 0
    valor_vazio_total = 0
    data_ruim_total = 0

    for ds in DATASETS:
        for r in data.get(ds, []) or []:
            nome_total += 1
            if not (r.get("contato_nome") or "").strip():
                nome_vazio_total += 1
            if not (r.get("valor") or "").strip():
                valor_vazio_total += 1
            venc = (r.get("vencimento") or "").strip()
            if venc:
                m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", venc)
                if not m:
                    data_ruim_total += 1
                else:
                    y = int(m.group(1))
                    if y < 2015 or y > today.year + 5:
                        data_ruim_total += 1

    if nome_total == 0:
        out.append(finding("dados_sanidade", "warn", "Nenhum lançamento nos datasets para auditar."))
        return out

    # contato_nome vazio
    ratio_nome = nome_vazio_total / nome_total
    if ratio_nome > 0.10:
        problemas.append(f"{nome_vazio_total}/{nome_total} ({ratio_nome:.0%}) lançamentos sem contato_nome")
        out.append(finding("dados_nome_vazio", "warn",
                           f"{nome_vazio_total}/{nome_total} lançamentos sem nome de contato ({ratio_nome:.0%}).",
                           "Possível falha no fetch de detalhes de contatos (/contatos/{id}). Revisar cache contatos.json."))
    else:
        out.append(finding("dados_nome_vazio", "ok",
                           f"Nomes de contato preenchidos ({nome_total-nome_vazio_total}/{nome_total})."))

    # valor vazio
    if valor_vazio_total > 0:
        problemas.append(f"{valor_vazio_total} lançamentos com valor vazio")
        out.append(finding("dados_valor_vazio", "warn",
                           f"{valor_vazio_total} lançamentos com campo 'valor' vazio.",
                           "Conferir parsing de valor no fetch-bling (fmt_money)."))
    else:
        out.append(finding("dados_valor_vazio", "ok", "Todos os lançamentos têm valor."))

    # datas
    if data_ruim_total > 0:
        problemas.append(f"{data_ruim_total} datas de vencimento implausíveis")
        out.append(finding("dados_datas", "warn",
                           f"{data_ruim_total} vencimentos com data ausente/implausível.",
                           "Conferir campo 'vencimento' na origem Bling."))
    else:
        out.append(finding("dados_datas", "ok", "Datas de vencimento consistentes."))

    return out


def check_volume_drop(update_log: dict) -> list[dict]:
    """Compara volume desta execução com a anterior — queda >50% é anômala."""
    logs = update_logs_sorted()
    cur_total = (update_log.get("records") or {}).get("total", 0)
    prev = None
    cur_name = None
    # acha o log atual e o imediatamente anterior
    for p in logs:
        d = load_json(p)
        if not d:
            continue
        if d is update_log or (d.get("run", {}).get("run_id") == update_log.get("run", {}).get("run_id")
                               and update_log.get("run", {}).get("run_id")):
            cur_name = p.name
    # anterior = penúltimo log com total>0 diferente do atual
    totals = [(p.name, (load_json(p) or {}).get("records", {}).get("total", 0)) for p in logs]
    totals = [t for t in totals if t[1] and t[0] != cur_name]
    if totals:
        prev = totals[-1][1]
    if prev and cur_total and cur_total < prev * 0.5:
        return [finding("volume_anomalo", "warn",
                        f"Volume caiu de {prev} para {cur_total} (>50%) vs. execução anterior.",
                        "Investigar possível truncamento de fetch ou mudança de janela/filtro.")]
    if prev and cur_total:
        return [finding("volume_anomalo", "ok",
                        f"Volume estável ({cur_total} vs {prev} anterior).")]
    return []


def check_recurring(window_days: int = 7) -> list[dict]:
    """Padrões de falha recorrentes nos logs de update dos últimos N dias."""
    cutoff = datetime.now(BRT) - timedelta(days=window_days)
    fail_counts: dict[str, int] = {}
    n = 0
    for p in update_logs_sorted():
        stamp = parse_stamp_from_name(p.name)
        if stamp and stamp < cutoff:
            continue
        d = load_json(p)
        if not d:
            continue
        n += 1
        steps = d.get("steps", {})
        for key in ("fetch", "build", "gate_test_audit", "gate_test_build", "precommit"):
            if steps.get(key) == "failure":
                fail_counts[key] = fail_counts.get(key, 0) + 1
        if steps.get("push") not in ("success", "nothing"):
            fail_counts["push"] = fail_counts.get("push", 0) + 1
    out: list[dict] = []
    for step, c in sorted(fail_counts.items(), key=lambda kv: -kv[1]):
        if c >= 2:
            out.append(finding("falha_recorrente", "error",
                               f"Etapa '{step}' falhou {c}x nos últimos {window_days} dias ({n} execuções).",
                               f"Falha recorrente em '{step}' — priorizar correção de causa-raiz."))
    if not out and n:
        out.append(finding("falha_recorrente", "ok",
                           f"Sem padrões de falha recorrente em {n} execuções nos últimos {window_days} dias."))
    return out


# ──────────────────────────────────────────────────────────────────
# Saídas: relatório, issue, IMPROVEMENTS
# ──────────────────────────────────────────────────────────────────
def severity_summary(findings: list[dict]) -> dict:
    s = {"error": 0, "warn": 0, "info": 0, "ok": 0}
    for f in findings:
        s[f["severity"]] = s.get(f["severity"], 0) + 1
    return s


def write_audit_report(now_brt: datetime, target_log_name: str,
                       findings: list[dict], extra: dict) -> Path:
    findings_sorted = sorted(findings, key=lambda f: SEV_ORDER.get(f["severity"], 9))
    summary = severity_summary(findings)
    report = {
        "generated_at_brt": now_brt.strftime("%Y-%m-%d %H:%M:%S"),
        "auditor": "ci_auditor.py",
        "target_update_log": target_log_name,
        "upstream_conclusion": extra.get("upstream_conclusion"),
        "summary": summary,
        "ok": summary["error"] == 0,
        "findings": findings_sorted,
        "recommendations": [f["recommendation"] for f in findings_sorted
                            if f["severity"] in ("error", "warn") and f.get("recommendation")],
        "links": {
            "dashboard": "https://leandrogelpke.github.io/Easy_Financas/",
            "audit_run": extra.get("run_url"),
            "update_run": extra.get("upstream_run_url"),
        },
    }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"audit_{now_brt.strftime('%Y-%m-%d_%H')}.json"
    path = LOGS_DIR / fname
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[audit] relatório: logs/{fname}  "
          f"(erros={summary['error']}, avisos={summary['warn']}, info={summary['info']}, ok={summary['ok']})")
    return path


def maybe_open_issue(now_brt: datetime, findings: list[dict], extra: dict) -> None:
    problemas = [f for f in findings if f["severity"] in ("error", "warn")]
    if not problemas:
        print("[audit] sem problemas — nenhuma Issue aberta.")
        return

    erros = [f for f in problemas if f["severity"] == "error"]
    top = (erros or problemas)[0]
    stamp = now_brt.strftime("%d/%m %H:%M")
    window_key = now_brt.strftime("%Y-%m-%d_%H")
    title = f"⚠️ Auditoria {stamp}: {top['detail'][:90]}"

    # garante o label (idempotente)
    gh("label", "create", ISSUE_LABEL, "--repo", REPO,
       "--color", "B60205", "--description", "Aberta automaticamente pelo agente auditor",
       "--force")

    # dedupe: já existe issue aberta para esta janela?
    rc, out = gh("issue", "list", "--repo", REPO, "--label", ISSUE_LABEL,
                 "--state", "open", "--search", window_key, "--json", "number,title")
    if rc == 0 and out.strip():
        try:
            existing = json.loads(out)
            if existing:
                print(f"[audit] Issue já aberta para a janela {window_key} (#{existing[0].get('number')}). Pulando.")
                return
        except Exception:  # noqa: BLE001
            pass

    n_err = len(erros)
    n_warn = len(problemas) - n_err
    linhas = [
        f"**Janela:** `{window_key}` BRT",
        f"**Conclusão do agente principal:** `{extra.get('upstream_conclusion')}`",
        f"**Resumo:** {n_err} erro(s), {n_warn} aviso(s).",
        "",
        "## Achados",
    ]
    for f in sorted(problemas, key=lambda x: SEV_ORDER.get(x["severity"], 9)):
        ic = "🔴" if f["severity"] == "error" else "🟡"
        linhas.append(f"- {ic} **{f['check']}** — {f['detail']}")
        if f.get("recommendation"):
            linhas.append(f"  - 💡 _Recomendação:_ {f['recommendation']}")
    linhas += [
        "",
        "## Links",
        f"- Execução da auditoria: {extra.get('run_url')}",
        f"- Execução auditada: {extra.get('upstream_run_url')}",
        "- Dashboard: https://leandrogelpke.github.io/Easy_Financas/",
        "",
        "_Aberta automaticamente pelo agente auditor (`ci/ci_auditor.py`). "
        "As recomendações recorrentes são acumuladas em `IMPROVEMENTS.md`._",
    ]
    body = "\n".join(linhas)
    rc, out = gh("issue", "create", "--repo", REPO, "--title", title,
                 "--body", body, "--label", ISSUE_LABEL)
    if rc == 0:
        print(f"[audit] Issue aberta: {out.strip().splitlines()[-1] if out.strip() else 'ok'}")
    else:
        print(f"[audit] não foi possível abrir Issue (rc={rc}): {out[:200]}")


def update_improvements(window_days: int = 7) -> None:
    """Regenera IMPROVEMENTS.md a partir das recomendações recorrentes
    (>=2 ocorrências) nos relatórios de auditoria dos últimos N dias."""
    cutoff = datetime.now(BRT) - timedelta(days=window_days)
    counter: dict[str, dict] = {}
    n_reports = 0
    for p in audit_logs_sorted():
        stamp = parse_stamp_from_name(p.name)
        if stamp and stamp < cutoff:
            continue
        d = load_json(p)
        if not d:
            continue
        n_reports += 1
        for f in d.get("findings", []):
            rec = (f.get("recommendation") or "").strip()
            if not rec or f.get("severity") not in ("error", "warn"):
                continue
            slot = counter.setdefault(rec, {"count": 0, "checks": set(), "sev": f.get("severity")})
            slot["count"] += 1
            slot["checks"].add(f.get("check"))
            if f.get("severity") == "error":
                slot["sev"] = "error"

    recorrentes = sorted(
        [(rec, v) for rec, v in counter.items() if v["count"] >= 2],
        key=lambda kv: -kv[1]["count"],
    )

    now_brt = datetime.now(BRT)
    lines = [
        "# IMPROVEMENTS.md — Recomendações acumuladas (revisão humana)",
        "",
        "> Arquivo mantido **automaticamente** pelo agente auditor "
        "(`ci/ci_auditor.py`). Lista recomendações que apareceram em "
        f"**2 ou mais** auditorias nos últimos **{window_days} dias**, para "
        "realimentar o agente principal. Não editar à mão — será sobrescrito.",
        "",
        f"_Última atualização: {now_brt.strftime('%Y-%m-%d %H:%M')} BRT · "
        f"base: {n_reports} relatório(s) de auditoria na janela._",
        "",
    ]
    if not recorrentes:
        lines += [
            "## Status",
            "",
            "✅ Nenhuma recomendação recorrente na janela. Pipeline saudável "
            "ou histórico ainda insuficiente (acumula a cada execução).",
            "",
        ]
    else:
        lines += ["## Recomendações recorrentes", ""]
        for rec, v in recorrentes:
            ic = "🔴" if v["sev"] == "error" else "🟡"
            checks = ", ".join(sorted(c for c in v["checks"] if c))
            lines.append(f"- {ic} **({v['count']}x)** {rec}")
            if checks:
                lines.append(f"  - _checks:_ `{checks}`")
        lines.append("")
    IMPROVEMENTS.write_text("\n".join(lines), encoding="utf-8")
    print(f"[audit] IMPROVEMENTS.md atualizado ({len(recorrentes)} recorrente(s), {n_reports} relatórios).")


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────
def main() -> int:
    now_brt = datetime.now(BRT)
    extra = {
        "upstream_conclusion": os.environ.get("UPSTREAM_CONCLUSION"),
        "upstream_run_url": os.environ.get("UPSTREAM_RUN_URL"),
        "run_url": os.environ.get("RUN_URL"),
    }

    logs = update_logs_sorted()
    if not logs:
        # Nenhum log do agente principal — registra e encerra com aviso.
        findings = [finding("log_principal", "warn",
                            "Nenhum log update_*.json encontrado para auditar.",
                            "Conferir se o agente principal gravou e commitou o log.")]
        write_audit_report(now_brt, "(nenhum)", findings, extra)
        update_improvements()
        maybe_open_issue(now_brt, findings, extra)
        return 0

    target_log_path = logs[-1]
    update_log = load_json(target_log_path) or {}
    print(f"[audit] auditando: {target_log_path.name}")

    findings: list[dict] = []
    findings += check_execution(update_log)
    findings += check_data_sanity(latest_snapshot())
    findings += check_volume_drop(update_log)
    findings += check_recurring()

    write_audit_report(now_brt, target_log_path.name, findings, extra)
    update_improvements()
    maybe_open_issue(now_brt, findings, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
