#!/usr/bin/env python3
"""
audit.py — Agente de auditoria tributária / contábil da Easy Analytics.

Roda a cada weekly.sh, checa divergências e produz:
  1. lista de AuditFinding (status, título, detalhe, ação)
  2. JSON snapshot em <bling_dir>/audit_findings.json (histórico)
  3. fragments HTML (aba "Auditoria" no dashboard)

Filosofia:
  - cada check é puro: recebe dados, retorna findings
  - severidade: ok (verde) | warn (amarelo) | error (vermelho) | info (azul)
  - todo warn/error tem ação sugerida
  - CONFIG é a única fonte de verdade pra alíquotas — dre_render.py importa daqui

Perfil tributário modelado:
  - Regime: Lucro Presumido
  - Sede: São Bernardo do Campo - SP
  - Atividade: Serviços de software / TI (base presumida 32%)
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════
# CONFIG TRIBUTÁRIA — Confirmada com a contabilidade (mai/2026)
# Lucro Presumido · sede São Bernardo do Campo - SP
# Impostos: ISS, PIS, COFINS, IRRF, CSRF (PIS+COFINS+CSLL retido),
# IRPJ trimestral, CSLL trimestral.
# ═══════════════════════════════════════════════════════════════
CONFIG: dict[str, Any] = {
    "regime": "Lucro Presumido",
    "municipio_sede": "São Bernardo do Campo - SP",
    "atividade": "Software / TI (base presumida 32%)",

    # ── Federais sobre receita (mensal, cumulativo no Presumido)
    "pis_aliq":        0.0065,   # 0,65%
    "cofins_aliq":     0.03,     # 3%

    # ── Federais sobre lucro (trimestral)
    "irpj_base_presumida":            0.32,
    "irpj_aliq":                      0.15,
    "irpj_adicional_aliq":            0.10,
    "irpj_adicional_limite_trim":     60_000.0,   # R$/trim (R$ 20K/mês × 3)
    "csll_base_presumida":            0.32,
    "csll_aliq":                      0.09,

    # ── Municipal ISS — São Bernardo do Campo
    # Lei municipal nº 5232/03, subitens 1.01 a 1.08 (TODOS os serviços
    # de TI: análise, desenvolvimento, programação, processamento, licenciamento,
    # consultoria, suporte, páginas eletrônicas) = 2,00%.
    "iss_sbc_aliq":     0.02,
    "iss_sbc_tolerancia": 0.10,  # 10% — alíquota única confirmada

    # ── INSS pró-labore
    "inss_patronal_aliq":  0.20,
    "inss_socio_aliq":     0.11,

    # ── CBS/IBS 2026 (Reforma Tributária — LC 214/2025)
    # 2026: alíquota teste totalmente compensável.
    "cbs_teste_aliq":    0.009,    # 0,9%
    "ibs_teste_aliq":    0.001,    # 0,1%
    "cbs_ibs_inicio":    "2026-01",

    # ── Retenções obrigatórias quando Easy contrata PJ
    "ret_pis_cofins_csll_aliq":  0.0465,  # 4,65% (PIS 0,65 + COF 3 + CSLL 1)
    "ret_pis_cofins_csll_min":   215.05,  # piso mensal pra reter (Lei 10.833)
    "ret_irrf_pj_aliq":          0.015,   # 1,5% serviços profissionais

    # ── Retenções recebidas (cliente retém na NF da Easy)
    # Faixa esperada de retenção total = 4,65% (PIS+COFINS+CSLL) + 1,5% (IRRF) = 6,15%
    "ret_recebida_aliq_max":  0.065,
    "ret_recebida_aliq_min":  0.045,

    # ── Tolerância padrão pra OK
    "tolerancia_padrao":    0.05,
}


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════
@dataclass
class AuditFinding:
    check_id: str
    status: str               # ok | warn | error | info
    title: str
    detail: str
    competencia: str | None = None
    value: float | None = None
    expected: float | None = None
    diff: float | None = None
    action: str | None = None
    category: str = "tributario"   # tributario | reconciliacao | operacional | reforma

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _norm(s: str) -> str:
    return _strip_accents((s or "").upper().strip())


def _trimestre_de(ym: str) -> str:
    """'2026-04' -> '2026-T2'."""
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y:04d}-T{(m - 1) // 3 + 1}"


# ── Helpers de competência por histórico ──────────────────────────────
# Os históricos do Bling vêm com a competência embutida em vários formatos:
#   "ISS S/ FATURAMENTO - 01/2026"     -> 2026-01
#   "ISS PRESTADOR - 02/2026"          -> 2026-02
#   "ISS - 04/2026"                    -> 2026-04
#   "PIS REFERENTE 03/2026"            -> 2026-03
#   "IRPJ - 1º TRIMESTRE/2026"         -> 2026-T1
#   "CSLL 4° TRIMESTRE 2025 - 3º Quota"-> 2025-T4
# Quando o histórico não traz nada, caímos no fallback (mês do pagamento ou
# regra N+1 de quem chamou).
_RE_COMPETENCIA_MM_AAAA = re.compile(r"\b(0[1-9]|1[0-2])[/\-](20\d{2})\b")
_RE_COMPETENCIA_AAAA_MM = re.compile(r"\b(20\d{2})[/\-](0[1-9]|1[0-2])\b")
_RE_TRIMESTRE = re.compile(
    r"\b([1-4])\s*[ºo°]?\s*TRIMET?S?T?R?E?\s*/?\s*(20\d{2})\b",
    re.IGNORECASE,
)


def _competencia_do_historico(historico: str | None,
                              fallback_ym: str | None = None) -> str | None:
    """Extrai YYYY-MM do histórico do Bling. Retorna fallback se não achar."""
    if not historico:
        return fallback_ym
    h = historico.upper()
    m = _RE_COMPETENCIA_MM_AAAA.search(h)
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    m = _RE_COMPETENCIA_AAAA_MM.search(h)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return fallback_ym


def _trimestre_do_historico(historico: str | None) -> str | None:
    """Extrai 'YYYY-TQ' do histórico (ex: '1º TRIMETRE/2026' -> '2026-T1').

    Aceita variações 'TRIMESTRE', 'TRIMETRE' (typo comum), com ou sem '/'.
    """
    if not historico:
        return None
    m = _RE_TRIMESTRE.search(historico)
    if not m:
        return None
    q, y = m.group(1), m.group(2)
    return f"{y}-T{q}"


def _is_iss_tomador(item: dict) -> bool:
    """ISS-TOMADOR é o ISS retido pela Easy quando contrata serviço de PJ
    de fora de SBC — NÃO é imposto sobre a receita. Não deve entrar na
    apuração de ISS sobre vendas.
    """
    h = _norm(item.get("historico") or "")
    return "TOMADOR" in h


def _ym_pagamento_irpj_csll(trim: str) -> str:
    """Trimestre 2026-T1 (jan-mar) → pagamento típico em abr (2026-04)."""
    y, q = trim.split("-T")
    y, q = int(y), int(q)
    pay_month = q * 3 + 1
    pay_year = y
    if pay_month > 12:
        pay_month -= 12
        pay_year += 1
    return f"{pay_year:04d}-{pay_month:02d}"


def _classify_diff(value: float | None, expected: float | None,
                   tol: float = 0.05) -> str:
    """Retorna ok|warn|error com base na diferença relativa."""
    v = value or 0
    e = expected or 0
    if e == 0:
        return "warn" if v != 0 else "ok"
    rel = abs(v - e) / e
    if rel <= tol:
        return "ok"
    return "warn" if rel <= 0.30 else "error"


def _fmt_brl(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}" if v >= 0 else f"(R$ {s})"


def _fmt_pct(v: float) -> str:
    return f"{v*100:.2f}%".replace(".", ",")


_MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez"]


def _fmt_comp(ym: str | None) -> str:
    if not ym:
        return "—"
    if "T" in ym:  # trimestre
        return ym
    try:
        y, m = int(ym[:4]), int(ym[5:7])
        return f"{_MESES_PT[m-1]}/{str(y)[-2:]}"
    except Exception:
        return ym


# ═══════════════════════════════════════════════════════════════
# CHECKS — cada um retorna list[AuditFinding]
# ═══════════════════════════════════════════════════════════════
def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + 1
    if m > 12:
        m = 1; y += 1
    return f"{y:04d}-{m:02d}"


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) - 1
    if m < 1:
        m = 12; y -= 1
    return f"{y:04d}-{m:02d}"


def _agregar_pago_por_competencia(matriz: dict, grupo_pago: str,
                                  excluir_tomador: bool = False
                                  ) -> dict[str, float]:
    """Soma pagamentos do grupo agrupando pela COMPETÊNCIA real do tributo
    (extraída do histórico), não pelo mês de pagamento. Aplica filtro de
    ISS TOMADOR quando solicitado.

    Itens sem competência no histórico caem em fallback "ym pago - 1 mês"
    (regra N+1) para preservar o comportamento anterior em casos legados.
    """
    out: dict[str, float] = defaultdict(float)
    items_por_label = matriz.get("items", {}).get(grupo_pago, {})
    for label, lista in items_por_label.items():
        # Cada item tem seu próprio "ym" (mês de pagamento).
        for it in lista:
            if excluir_tomador and _is_iss_tomador(it):
                continue
            hist = it.get("historico") or ""
            comp = _competencia_do_historico(hist, None)
            if comp is None:
                # Fallback: assume "pago em N+1 => competência N"
                ym_pago = it.get("ym") or ""
                try:
                    comp = _prev_month(ym_pago) if ym_pago else ym_pago
                except Exception:
                    comp = ym_pago
            out[comp] += float(it.get("valor", 0) or 0)
    return dict(out)


def _check_tributo_mensal(matriz: dict, grupo_pago: str, aliq: float,
                          tributo_label: str, action_extra: str,
                          tol: float = 0.05,
                          excluir_tomador: bool = False) -> list[AuditFinding]:
    """Compara receita[N] × alíquota vs pago[N] (matching por COMPETÊNCIA
    extraída do histórico do Bling, com fallback para regra N+1).

    Tributos mensais sobre receita (PIS, COFINS, ISS) têm fato gerador no
    mês N e recolhimento no mês N+1, mas o que importa pra auditoria é a
    competência declarada — não a data do pagamento. Isso evita:
      - falso positivo quando pagamento atrasou ou foi antecipado;
      - falso positivo quando o pagador empacotou meses (caso jan/26: o
        Bling registra o ISS de jan/26 pago em dez/25 por antecipação);
      - somar ISS TOMADOR (retenção da Easy ao contratar PJ de fora de SBC)
        junto com ISS PRESTADOR (sobre a receita).
    """
    findings: list[AuditFinding] = []
    rec = matriz.get("receita_por_mes", {})
    today_ym = datetime.now().strftime("%Y-%m")

    pago_por_comp = _agregar_pago_por_competencia(
        matriz, grupo_pago, excluir_tomador=excluir_tomador)

    months_real = sorted(ym for ym in rec.keys()
                         if matriz["month_types"].get(ym) == "real"
                         and rec[ym] >= 100)
    for ym in months_real:
        ym_pay = _next_month(ym)
        esp = round(rec[ym] * aliq, 2)

        # Se ainda não chegou no mês de pagamento, só info
        if ym_pay > today_ym:
            findings.append(AuditFinding(
                check_id=f"{grupo_pago}_{ym}", status="info",
                title=f"{tributo_label} {_fmt_comp(ym)} — a vencer",
                detail=(f"Receita {_fmt_brl(rec[ym])} × {_fmt_pct(aliq)} = "
                        f"{_fmt_brl(esp)}. Vence em {_fmt_comp(ym_pay)}."),
                competencia=ym, expected=esp,
                action="Provisionar pra próximo recolhimento.",
            ))
            continue

        p = pago_por_comp.get(ym, 0.0)
        if p == 0:
            findings.append(AuditFinding(
                check_id=f"{grupo_pago}_{ym}", status="warn",
                title=f"{tributo_label} {_fmt_comp(ym)} — sem DARF localizado",
                detail=(f"Receita {_fmt_brl(rec[ym])} × {_fmt_pct(aliq)} = "
                        f"{_fmt_brl(esp)} esperado. Nenhum lançamento com "
                        f"competência {_fmt_comp(ym)} localizado no Bling."),
                competencia=ym, value=0, expected=esp, diff=-esp,
                action=action_extra,
            ))
        else:
            st = _classify_diff(p, esp, tol=tol)
            if st == "ok":
                continue
            findings.append(AuditFinding(
                check_id=f"{grupo_pago}_{ym}", status=st,
                title=f"{tributo_label} {_fmt_comp(ym)} — divergente",
                detail=(f"Receita {_fmt_brl(rec[ym])} → esperado "
                        f"{_fmt_brl(esp)} ({_fmt_pct(aliq)}) · pago "
                        f"(competência {_fmt_comp(ym)}): {_fmt_brl(p)}."),
                competencia=ym, value=p, expected=esp, diff=p-esp,
                action=action_extra,
            ))
    return findings


def check_pis(matriz: dict, cfg: dict = CONFIG) -> list[AuditFinding]:
    return _check_tributo_mensal(
        matriz, "deducoes_pis", cfg["pis_aliq"], "PIS",
        ("Pode estar sendo retido na fonte pelo cliente (caso típico Totvs). "
         "Confirmar com Serrano."),
        tol=cfg["tolerancia_padrao"],
    )


def check_cofins(matriz: dict, cfg: dict = CONFIG) -> list[AuditFinding]:
    return _check_tributo_mensal(
        matriz, "deducoes_cofins", cfg["cofins_aliq"], "COFINS",
        "Verificar se cliente retém na fonte. Confirmar com Serrano.",
        tol=cfg["tolerancia_padrao"],
    )


def check_iss_sbc(matriz: dict, cfg: dict = CONFIG) -> list[AuditFinding]:
    return _check_tributo_mensal(
        matriz, "deducoes_iss", cfg["iss_sbc_aliq"], "ISS-SBC",
        ("Alíquota 2,00% (Lei municipal nº 5232/03, subitens 1.01–1.08). "
         "Diff pode indicar parte retida na fonte em outro município (ISS-fonte)."),
        tol=cfg["iss_sbc_tolerancia"],
        excluir_tomador=True,  # ISS-TOMADOR é retenção da Easy, não receita
    )


def check_irpj_trimestral(matriz: dict, cfg: dict = CONFIG,
                          today: date | None = None) -> list[AuditFinding]:
    """IRPJ trimestral: base + adicional 10% sobre lucro presumido > R$60K/trim."""
    today = today or date.today()
    findings: list[AuditFinding] = []
    rec = matriz.get("receita_por_mes", {})

    trim_rec: dict[str, float] = {}
    for ym in sorted(rec.keys()):
        if matriz["month_types"].get(ym) != "real":
            continue
        trim_rec.setdefault(_trimestre_de(ym), 0.0)
        trim_rec[_trimestre_de(ym)] += rec[ym]

    pago_por_trim, quotas_por_trim = _agregar_pago_lucro_por_trimestre(
        matriz, tributo="IRPJ")

    for trim, r in sorted(trim_rec.items()):
        lp = r * cfg["irpj_base_presumida"]
        base = lp * cfg["irpj_aliq"]
        adicional = max(0.0, lp - cfg["irpj_adicional_limite_trim"]) * \
            cfg["irpj_adicional_aliq"]
        esp_total = round(base + adicional, 2)

        ym_pay_1 = _ym_pagamento_irpj_csll(trim)
        p = pago_por_trim.get(trim, 0.0)
        n_quotas = quotas_por_trim.get(trim, 0)

        # Se nenhuma quota foi paga E o mês de vencimento da 1ª ainda não
        # chegou, é só provisão informativa.
        if p == 0 and ym_pay_1 > today.strftime("%Y-%m"):
            findings.append(AuditFinding(
                check_id=f"irpj_{trim}", status="info",
                title=f"IRPJ {trim} — provisão",
                detail=(f"Receita trim {_fmt_brl(r)} · lucro pres. (32%) "
                        f"{_fmt_brl(lp)} · IRPJ base {_fmt_brl(base)}"
                        + (f" + adicional 10% {_fmt_brl(adicional)}"
                           if adicional > 0 else "")
                        + f" = {_fmt_brl(esp_total)}. Vence em "
                        f"{_fmt_comp(ym_pay_1)} (até 3 quotas)."),
                competencia=trim, expected=esp_total,
                action="Provisionar caixa pra próximo recolhimento.",
                category="tributario",
            ))
            continue

        # Janela de cobrança: 1ª quota no mês ym_pay_1, 2ª em +1, 3ª em +2.
        # Se ainda estamos antes do vencimento da 3ª quota, recolhimento
        # parcial NÃO é divergência — pode estar parcelado.
        y, q = trim.split("-T")
        ym_pay_3 = q_to_month(int(q), 3, int(y))
        parcelamento_em_curso = today.strftime("%Y-%m") <= ym_pay_3

        st = _classify_diff(p, esp_total)
        if st == "ok" and p > 0:
            continue

        if p == 0:
            findings.append(AuditFinding(
                check_id=f"irpj_{trim}", status="error",
                title=f"IRPJ {trim} não recolhido",
                detail=(f"Receita trim {_fmt_brl(r)} → lucro pres. {_fmt_brl(lp)} → "
                        f"esperado {_fmt_brl(esp_total)} "
                        f"({_fmt_brl(base)} base"
                        + (f" + {_fmt_brl(adicional)} adicional 10%"
                           if adicional > 0 else "")
                        + f"). Sem DARF localizado em {_fmt_comp(ym_pay_1)}."),
                competencia=trim, value=0, expected=esp_total, diff=-esp_total,
                action="Conferir DARF do trimestre. Pode estar com pagamento atrasado.",
                category="tributario",
            ))
        elif parcelamento_em_curso and p < esp_total and n_quotas < 3:
            # Provavelmente parcelado em até 3 quotas, ainda dentro do prazo
            findings.append(AuditFinding(
                check_id=f"irpj_{trim}", status="info",
                title=f"IRPJ {trim} — parcelado em andamento",
                detail=(f"Pago {_fmt_brl(p)} de {_fmt_brl(esp_total)} esperado "
                        f"({n_quotas} de até 3 quotas)"
                        + (f" — adicional 10% R$ {_fmt_brl(adicional)}"
                           if adicional > 0 else "") + "."),
                competencia=trim, value=p, expected=esp_total,
                diff=p-esp_total,
                action=("Aguardar próximas quotas até "
                        f"{_fmt_comp(ym_pay_3)}."),
                category="tributario",
            ))
        else:
            findings.append(AuditFinding(
                check_id=f"irpj_{trim}", status=st,
                title=f"IRPJ {trim} divergente",
                detail=(f"Pago {_fmt_brl(p)} ({n_quotas} quota(s)) vs esperado "
                        f"{_fmt_brl(esp_total)}"
                        + (f" (inclui adicional 10% de {_fmt_brl(adicional)})"
                           if adicional > 0 else "")),
                competencia=trim, value=p, expected=esp_total, diff=p-esp_total,
                action="Conferir cálculo com Serrano. Adicional 10% costuma ser esquecido.",
                category="tributario",
            ))
    return findings


def _agregar_pago_lucro_por_trimestre(
        matriz: dict, tributo: str
) -> tuple[dict[str, float], dict[str, int]]:
    """Agrupa pagamentos de IRPJ ou CSLL por TRIMESTRE de competência.

    Estratégia:
      1. Filtra items cujo contato OU histórico mencionem o tributo
         (IRPJ ou CSLL).
      2. Para cada item, tenta extrair trimestre do histórico
         (_trimestre_do_historico). Ex: "IRPJ - 1º TRIMETRE/2026" → 2026-T1.
      3. Sem trimestre no histórico, infere pelo mês de pagamento (regra
         padrão: pagamento em mês M+1 a M+3 após fim do trim). Usa a 1ª
         quota como referência: trim cujo ym_pay_1 == ym_pago, ou se
         estiver atrasado, o trim anterior também é considerado dentro da
         janela ym_pay_1..ym_pay_3.

    Retorna (total_pago_por_trim, n_quotas_identificadas_por_trim).
    """
    items = matriz.get("items", {}).get("impostos_lucro", {}).get(
        "Impostos sobre lucro", [])

    pattern = re.compile(rf"\b{tributo}\b", re.IGNORECASE)

    pago: dict[str, float] = defaultdict(float)
    quotas: dict[str, int] = defaultdict(int)

    for it in items:
        contato = it.get("contato") or ""
        historico = it.get("historico") or ""
        if not (pattern.search(contato) or pattern.search(historico)):
            continue

        valor = float(it.get("valor", 0) or 0)
        if valor <= 0:
            continue

        trim = _trimestre_do_historico(historico)
        if not trim:
            # Fallback: olhar mês de pagamento e mapear para o trim cujo
            # intervalo de quotas (ym_pay_1..ym_pay_3) contém esse mês.
            ym_pago = it.get("ym") or ""
            if not ym_pago or "-" not in ym_pago:
                continue
            y_pg, m_pg = int(ym_pago[:4]), int(ym_pago[5:7])
            # quota_1 do trim T = mês 3*Q+1 do ano (no ano corrente);
            # busca o trim cuja janela [3Q+1..3Q+3] contém m_pg.
            # Considera tanto trim do ano corrente quanto T4 do ano anterior.
            for delta_year, year in ((0, y_pg), (-1, y_pg - 1)):
                for q in range(1, 5):
                    first = q * 3 + 1
                    months = [(year if first + i <= 12 else year + 1,
                               (first + i - 1) % 12 + 1)
                              for i in range(3)]
                    if (y_pg, m_pg) in months:
                        trim = f"{year:04d}-T{q}"
                        break
                if trim:
                    break
            if not trim:
                continue

        pago[trim] += valor
        quotas[trim] += 1

    return dict(pago), dict(quotas)


def q_to_month(q: int, m_off: int, year: int) -> str:
    """Trimestre Q (1-4), offset 1-3 (1=mês de pagamento, 2/3=meses seguintes)."""
    month = q * 3 + m_off
    y = year
    while month > 12:
        month -= 12
        y += 1
    return f"{y:04d}-{month:02d}"


def check_csll_trimestral(matriz: dict, cfg: dict = CONFIG,
                          today: date | None = None) -> list[AuditFinding]:
    today = today or date.today()
    findings: list[AuditFinding] = []
    rec = matriz.get("receita_por_mes", {})

    trim_rec: dict[str, float] = {}
    for ym in sorted(rec.keys()):
        if matriz["month_types"].get(ym) != "real":
            continue
        trim_rec.setdefault(_trimestre_de(ym), 0.0)
        trim_rec[_trimestre_de(ym)] += rec[ym]

    pago_por_trim, quotas_por_trim = _agregar_pago_lucro_por_trimestre(
        matriz, tributo="CSLL")

    for trim, r in sorted(trim_rec.items()):
        esp = round(r * cfg["csll_base_presumida"] * cfg["csll_aliq"], 2)
        ym_pay_1 = _ym_pagamento_irpj_csll(trim)
        p = pago_por_trim.get(trim, 0.0)
        n_quotas = quotas_por_trim.get(trim, 0)

        if p == 0 and ym_pay_1 > today.strftime("%Y-%m"):
            findings.append(AuditFinding(
                check_id=f"csll_{trim}", status="info",
                title=f"CSLL {trim} — provisão",
                detail=(f"Receita trim {_fmt_brl(r)} × 32% × 9% = "
                        f"{_fmt_brl(esp)}. Vence em {_fmt_comp(ym_pay_1)} "
                        "(até 3 quotas)."),
                competencia=trim, expected=esp,
                action="Provisionar caixa.",
            ))
            continue

        y, q = trim.split("-T")
        ym_pay_3 = q_to_month(int(q), 3, int(y))
        parcelamento_em_curso = today.strftime("%Y-%m") <= ym_pay_3

        st = _classify_diff(p, esp)
        if st == "ok" and p > 0:
            continue
        if p == 0:
            findings.append(AuditFinding(
                check_id=f"csll_{trim}", status="error",
                title=f"CSLL {trim} não recolhido",
                detail=(f"Receita trim {_fmt_brl(r)} → CSLL esperado "
                        f"{_fmt_brl(esp)} (2,88% efetivo). Sem DARF em "
                        f"{_fmt_comp(ym_pay_1)}."),
                competencia=trim, value=0, expected=esp, diff=-esp,
                action="Conferir DARF do trimestre.",
            ))
        elif parcelamento_em_curso and p < esp and n_quotas < 3:
            findings.append(AuditFinding(
                check_id=f"csll_{trim}", status="info",
                title=f"CSLL {trim} — parcelado em andamento",
                detail=(f"Pago {_fmt_brl(p)} de {_fmt_brl(esp)} esperado "
                        f"({n_quotas} de até 3 quotas)."),
                competencia=trim, value=p, expected=esp, diff=p-esp,
                action=f"Aguardar próximas quotas até {_fmt_comp(ym_pay_3)}.",
            ))
        else:
            findings.append(AuditFinding(
                check_id=f"csll_{trim}", status=st,
                title=f"CSLL {trim} divergente",
                detail=(f"Pago {_fmt_brl(p)} ({n_quotas} quota(s)) vs "
                        f"esperado {_fmt_brl(esp)}."),
                competencia=trim, value=p, expected=esp, diff=p-esp,
                action="Conferir cálculo com Serrano.",
            ))
    return findings


def check_cbs_ibs_2026(matriz: dict, today: date,
                       cfg: dict = CONFIG) -> list[AuditFinding]:
    """Em 2026 a reforma tributária (LC 214/2025) impõe alíquota teste de
    CBS 0,9% + IBS 0,1% = 1% sobre receita, totalmente compensável.
    Mesmo compensada, precisa apurar e declarar."""
    findings: list[AuditFinding] = []
    if today.strftime("%Y-%m") < cfg["cbs_ibs_inicio"]:
        return findings
    rec = matriz.get("receita_por_mes", {})
    for ym in sorted(rec.keys()):
        if ym < cfg["cbs_ibs_inicio"]:
            continue
        if matriz["month_types"].get(ym) != "real":
            continue
        r = rec[ym]
        if r < 100:
            continue
        esp_cbs = round(r * cfg["cbs_teste_aliq"], 2)
        esp_ibs = round(r * cfg["ibs_teste_aliq"], 2)
        findings.append(AuditFinding(
            check_id=f"cbs_ibs_{ym}", status="warn",
            title=f"CBS/IBS 2026 — apuração teste pendente ({_fmt_comp(ym)})",
            detail=(f"Receita {_fmt_brl(r)} · CBS teste 0,9% = {_fmt_brl(esp_cbs)} · "
                    f"IBS teste 0,1% = {_fmt_brl(esp_ibs)}. Compensável "
                    f"100% com PIS/COFINS/ISS, mas precisa apurar e declarar."),
            competencia=ym, expected=esp_cbs + esp_ibs,
            action=("Confirmar com Serrano se a DGD/EFD Reinf de CBS/IBS está "
                    "sendo gerada desde jan/26."),
            category="reforma",
        ))
    return findings


def check_retencoes_recebidas(matriz: dict, totvs_por_mes: dict,
                              cfg: dict = CONFIG) -> list[AuditFinding]:
    """Compara receita Bling vs comissão Totvs do mês anterior. Se Bling
    é menor que Totvs em ~4,65–6,15%, é retenção esperada (OK). Fora dessa
    faixa, flagga."""
    findings: list[AuditFinding] = []
    rec = matriz.get("receita_por_mes", {})
    for ym in sorted(rec.keys()):
        if matriz["month_types"].get(ym) != "real":
            continue
        # Competência Totvs = mês anterior ao Bling
        y, m = int(ym[:4]), int(ym[5:7])
        prev_m = m - 1 or 12
        prev_y = y if m > 1 else y - 1
        ym_prev = f"{prev_y:04d}-{prev_m:02d}"
        t = totvs_por_mes.get(ym_prev, {})
        totvs_total = (t.get("BASE", 0) + t.get("COMPLEMENTAR", 0))
        if totvs_total < 100:
            continue
        bling = rec[ym]
        diff_pct = (totvs_total - bling) / totvs_total
        # diff_pct positivo: Bling menor → cliente reteve algo
        if 0.04 <= diff_pct <= cfg["ret_recebida_aliq_max"]:
            # Está na faixa esperada
            findings.append(AuditFinding(
                check_id=f"retencao_recebida_{ym}", status="ok",
                title=f"Retenção recebida na faixa esperada — {_fmt_comp(ym)}",
                detail=(f"Bling {_fmt_brl(bling)} vs Totvs {_fmt_brl(totvs_total)} → "
                        f"{diff_pct*100:.1f}% retido (4,65–6,15% = PIS+COFINS+CSLL "
                        f"+ possível IRRF)."),
                competencia=ym, value=bling, expected=totvs_total,
                diff=bling-totvs_total,
                action="Esses valores reduzem PIS/COFINS/CSLL/IRPJ a recolher.",
                category="reconciliacao",
            ))
        elif diff_pct > cfg["ret_recebida_aliq_max"]:
            findings.append(AuditFinding(
                check_id=f"retencao_recebida_{ym}", status="warn",
                title=f"Retenção atípica — {_fmt_comp(ym)}",
                detail=(f"Bling {_fmt_brl(bling)} vs Totvs {_fmt_brl(totvs_total)} → "
                        f"{diff_pct*100:.1f}% diferença. Acima do esperado (6,15%)."),
                competencia=ym, value=bling, expected=totvs_total,
                diff=bling-totvs_total,
                action="Investigar — NF não emitida, ou retenção atípica.",
                category="reconciliacao",
            ))
    return findings


def check_pj_sem_retencao(matriz: dict, cfg: dict = CONFIG) -> list[AuditFinding]:
    """Para cada fornecedor PJ com pagamento > R$215,05 no mês, deveria
    haver retenção 4,65% + IRRF 1,5%. Como o Bling não rastreia retenção
    em separado, isso é informativo — só lista pra Serrano verificar."""
    findings: list[AuditFinding] = []
    # Soma por (fornecedor, mês) os itens de desp_admin que não são impostos
    by_forn_mes: dict[tuple[str, str], float] = {}
    items_admin = matriz.get("items", {}).get("desp_admin", {})
    for sub, lst in items_admin.items():
        for it in lst:
            forn = (it.get("contato") or "").strip()
            if not forn:
                continue
            n = _norm(forn)
            # Pula impostos e taxas (não são PJ contratada)
            if any(x in n for x in ("RECEITA FEDERAL", "PREFEITURA",
                                    "GOVERNO DO ESTADO", "MUNICIPIO")):
                continue
            # Pula cartão de crédito (não é PJ contratada pra reter)
            if "CARTAO" in n or "CARTÃO" in n:
                continue
            key = (forn, it["ym"])
            by_forn_mes[key] = by_forn_mes.get(key, 0) + float(it["valor"])

    # Agora consolida por fornecedor — quantos meses passaram do piso?
    forn_meses_acima: dict[str, list[tuple[str, float]]] = {}
    for (forn, ym), v in by_forn_mes.items():
        if v >= cfg["ret_pis_cofins_csll_min"]:
            forn_meses_acima.setdefault(forn, []).append((ym, v))

    if not forn_meses_acima:
        return findings

    # Top 5 fornecedores em valor total
    top = sorted(forn_meses_acima.items(),
                 key=lambda kv: -sum(v for _, v in kv[1]))[:5]

    for forn, lst in top:
        total = sum(v for _, v in lst)
        ret_pis_cof_csll = total * cfg["ret_pis_cofins_csll_aliq"]
        ret_irrf = total * cfg["ret_irrf_pj_aliq"]
        findings.append(AuditFinding(
            check_id=f"pj_retencao_{_norm(forn)[:30]}",
            status="info",
            title=f"PJ acima do piso de retenção — {forn[:50]}",
            detail=(f"{len(lst)} meses com pagamento > "
                    f"{_fmt_brl(cfg['ret_pis_cofins_csll_min'])} · total "
                    f"{_fmt_brl(total)}. Retenção esperada: "
                    f"{_fmt_brl(ret_pis_cof_csll)} (PIS+COFINS+CSLL 4,65%) "
                    f"+ {_fmt_brl(ret_irrf)} (IRRF 1,5%)."),
            value=total, expected=ret_pis_cof_csll + ret_irrf,
            action=("Confirmar com Serrano se retenções 4,65% e 1,5% IRRF "
                    "estão sendo feitas e recolhidas via DARF (códigos 5952 "
                    "e 1708 respectivamente)."),
            category="tributario",
        ))
    return findings


# ═══════════════════════════════════════════════════════════════
# RECONCILIAÇÃO DE CONTAS A PAGAR EM ABERTO
# ───────────────────────────────────────────────────────────────
# O Bling mantém lançamentos "fantasma" que inflam o saldo a pagar e,
# pior, a métrica de "vencidos" da Visão Geral. Dois padrões:
#
#   (A) DUPLICATA_PAGA — uma parcela/NF foi paga (existe em
#       contas_pagar_pagas) mas a cópia original em aberto nunca foi
#       baixada no Bling. Mesmo (contato · vencimento · valor) aparece
#       nos dois buckets. Ex.: Rômulo PARC 17 (R$ 59.814, venc 25/05).
#
#   (B) PROVISAO_COBERTA — provisão genérica (histórico com PREVISÃO /
#       PROVISÃO / placeholder "XX/AAAA") que já foi substituída pelas
#       NFs reais pagas no mesmo mês de vencimento. Ex.: Efata
#       "PRESTAÇÃO SERVIÇOS XX/2026" R$ 35.000 venc 20/05, coberta por
#       2× NF de R$ 17.500 pagas em maio.
#
# `reconcile_em_aberto` é a fonte única dessa limpeza — build-html.py,
# contas_view.py e dre_render.py importam daqui pra que TODAS as abas
# (Visão Geral, Caixa, A Pagar/Receber, DRE, Auditoria) usem o mesmo
# saldo. Pura, idempotente, não muta as listas de entrada.
# ═══════════════════════════════════════════════════════════════

_RE_PROVISAO = re.compile(
    r"(PREVISAO|PROVISAO|\bXX\s*/\s*20\d{2}\b)", re.IGNORECASE)


def _recon_money(v: Any) -> float:
    """Parse robusto de valor BR vindo de JSON ('59,814' / '1.234,56') ou número."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def _recon_contato(r: dict) -> str:
    cid = r.get("contato_id")
    if cid not in (None, ""):
        return str(cid).strip()
    return _norm(r.get("contato_nome") or r.get("nomeContato") or "")


def _recon_venc(r: dict) -> str:
    return (r.get("vencimento") or r.get("dataVencimento") or "").strip()


def reconcile_em_aberto(
    pagas: list[dict],
    em_aberto: list[dict],
    today: date | None = None,
) -> tuple[list[dict], list[dict]]:
    """Remove lançamentos fantasma de `em_aberto` (a pagar).

    Retorna (em_aberto_limpo, ajustes), onde cada ajuste é um dict:
      {tipo, contato, contato_id, venc, valor, historico[, realizado_mes]}
    `tipo` ∈ {"DUPLICATA_PAGA", "PROVISAO_COBERTA"}.

    Não altera as listas recebidas. Determinística → idempotente: rodar
    duas vezes sobre o resultado já limpo não remove nada a mais.
    """
    today = today or date.today()

    # índice de gêmeos pagos (consumíveis) + realizado por contato×mês-venc
    paid_twins: dict[tuple, int] = defaultdict(int)
    paid_by_cm: dict[tuple, float] = defaultdict(float)
    for p in pagas:
        v = round(_recon_money(p.get("valor") or p.get("saldo")), 2)
        ck = _recon_contato(p)
        vc = _recon_venc(p)
        paid_twins[(ck, vc, v)] += 1
        paid_by_cm[(ck, vc[:7])] += v

    limpo: list[dict] = []
    ajustes: list[dict] = []
    used: dict[tuple, int] = defaultdict(int)

    for r in em_aberto:
        v = round(_recon_money(r.get("saldo") or r.get("valor")), 2)
        ck = _recon_contato(r)
        vc = _recon_venc(r)
        hist = (r.get("historico") or r.get("historico_descricao") or "")
        key = (ck, vc, v)

        # (A) duplicata de algo já pago — consome um gêmeo
        if v > 0 and paid_twins.get(key, 0) - used[key] > 0:
            used[key] += 1
            ajustes.append({
                "tipo": "DUPLICATA_PAGA", "contato": r.get("contato_nome", ""),
                "contato_id": r.get("contato_id"), "venc": vc, "valor": v,
                "historico": hist[:80],
            })
            continue

        # (B) provisão genérica já coberta por realizado no mês de vencimento
        if v > 0 and _RE_PROVISAO.search(_norm(hist)):
            realizado = paid_by_cm.get((ck, vc[:7]), 0.0)
            if realizado >= v * 0.9:
                ajustes.append({
                    "tipo": "PROVISAO_COBERTA", "contato": r.get("contato_nome", ""),
                    "contato_id": r.get("contato_id"), "venc": vc, "valor": v,
                    "historico": hist[:80], "realizado_mes": round(realizado, 2),
                })
                continue

        limpo.append(r)

    return limpo, ajustes


def check_reconciliacao_pagar(bling_dir: Path, today: date) -> list[AuditFinding]:
    """Audita a limpeza de fantasmas em contas a pagar (camada de reconciliação).

    Emite um finding por rodada: lista quantos lançamentos foram suprimidos
    e o impacto em R$ sobre o saldo a pagar. Status `ok` quando nada foi
    removido (Bling limpo), `info` quando ajustou (trilha de auditoria).
    """
    def _load_csv(prefix: str) -> list[dict]:
        files = sorted(bling_dir.glob(f"{prefix}_*.csv"))
        if not files:
            return []
        with open(files[-1], encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    pagas = _load_csv("contas_pagar_pagas")
    em_aberto = _load_csv("contas_pagar_em_aberto")
    if not em_aberto:
        return []
    _limpo, ajustes = reconcile_em_aberto(pagas, em_aberto, today)
    if not ajustes:
        return [AuditFinding(
            check_id="reconc_pagar", status="ok",
            title="Carteira a pagar sem duplicatas",
            detail=("Nenhum lançamento em aberto coincide com pagamento já "
                    "confirmado — saldo a pagar do Bling está limpo."),
            category="reconciliacao",
        )]

    n_dup = sum(1 for a in ajustes if a["tipo"] == "DUPLICATA_PAGA")
    n_prov = sum(1 for a in ajustes if a["tipo"] == "PROVISAO_COBERTA")
    tot_dup = sum(a["valor"] for a in ajustes if a["tipo"] == "DUPLICATA_PAGA")
    tot_prov = sum(a["valor"] for a in ajustes if a["tipo"] == "PROVISAO_COBERTA")
    nomes = ", ".join(dict.fromkeys(
        (a["contato"] or "")[:24] for a in sorted(ajustes, key=lambda x: -x["valor"])
    ))[:140]
    partes = []
    if n_dup:
        partes.append(f"{n_dup} duplicata(s) já paga(s) ({_fmt_brl(tot_dup)})")
    if n_prov:
        partes.append(f"{n_prov} provisão(ões) coberta(s) por NF paga ({_fmt_brl(tot_prov)})")
    return [AuditFinding(
        check_id="reconc_pagar", status="info",
        title=f"{len(ajustes)} lançamento(s) fantasma removidos do a pagar",
        detail=(f"Removidos do saldo a pagar: {' + '.join(partes)}. "
                f"Total {_fmt_brl(tot_dup + tot_prov)}. Fornecedores: {nomes}. "
                f"São lançamentos em aberto que já foram pagos no Bling mas "
                f"não baixados — corrigido automaticamente a cada rodada."),
        value=tot_dup + tot_prov,
        action=("Opcional: baixar/excluir essas duplicatas no Bling pra a "
                "origem ficar limpa. O dashboard já as ignora."),
        category="reconciliacao",
    )]


def check_vencidos(bling_dir: Path, today: date) -> list[AuditFinding]:
    """Vencidos a pagar e a receber (após reconciliação de fantasmas)."""
    findings: list[AuditFinding] = []

    def _load_csv(prefix: str) -> list[dict]:
        files = sorted(bling_dir.glob(f"{prefix}_*.csv"))
        if not files:
            return []
        with open(files[-1], encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def _v(s: str) -> float:
        if not s:
            return 0.0
        try:
            return float(str(s).replace(".", "").replace(",", "."))
        except Exception:
            return 0.0

    today_iso = today.isoformat()

    # Reconcilia o a pagar antes de contar vencidos (remove duplicatas já
    # pagas e provisões cobertas) — alinhado com Visão Geral / Caixa.
    _pagas = _load_csv("contas_pagar_pagas")
    _pagar_limpo, _ = reconcile_em_aberto(_pagas, _load_csv("contas_pagar_em_aberto"), today)

    for kind, prefix, label in [
        ("pagar", "contas_pagar_em_aberto", "a pagar"),
        ("receber", "contas_receber_em_aberto", "a receber"),
    ]:
        items = _pagar_limpo if kind == "pagar" else _load_csv(prefix)
        vencidos = [r for r in items
                    if r.get("vencimento") and r["vencimento"] < today_iso]
        if not vencidos:
            continue
        total = sum(_v(r.get("valor", "0")) for r in vencidos)
        status = "error" if (kind == "pagar" and len(vencidos) > 5) else "warn"
        nomes = ", ".join(sorted({r.get("contato_nome", "")[:25]
                                  for r in vencidos})[:5])
        findings.append(AuditFinding(
            check_id=f"vencidos_{kind}", status=status,
            title=f"{len(vencidos)} contas {label} vencidas",
            detail=f"Total {_fmt_brl(total)}. Fornecedores/clientes: {nomes}.",
            value=total,
            action=("Acionar cobrança dos clientes em atraso." if kind == "receber"
                    else "Conferir e regularizar os pagamentos atrasados."),
            category="operacional",
        ))
    return findings


def check_sem_categoria(bling_dir: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    def _load(prefix: str) -> list[dict]:
        files = sorted(bling_dir.glob(f"{prefix}_*.csv"))
        if not files:
            return []
        with open(files[-1], encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def _v(s: str) -> float:
        if not s:
            return 0.0
        try:
            return float(str(s).replace(".", "").replace(",", "."))
        except Exception:
            return 0.0

    pagas = _load("contas_pagar_pagas")
    sem_cat = [r for r in pagas if not (r.get("categoria_descricao") or "").strip()]
    if sem_cat:
        total = sum(_v(r.get("valor", "0")) for r in sem_cat)
        findings.append(AuditFinding(
            check_id="sem_categoria_pagas", status="warn",
            title=f"{len(sem_cat)} pagamentos sem categoria no Bling",
            detail=(f"Total {_fmt_brl(total)} de pagamentos confirmados sem "
                    f"categoria gerencial — distorce DRE."),
            value=total,
            action="Classificar no Bling. Considerar criar regras automáticas por contato.",
            category="operacional",
        ))
    return findings


def check_reconc_bling_totvs(matriz: dict, totvs_por_mes: dict,
                             cfg: dict = CONFIG) -> list[AuditFinding]:
    """Versão simplificada de reconciliação receita Bling vs Totvs.
    Detalhe completo está em totvs_render — aqui só flag divergência grave."""
    findings: list[AuditFinding] = []
    rec = matriz.get("receita_por_mes", {})
    for ym in sorted(rec.keys()):
        if matriz["month_types"].get(ym) != "real":
            continue
        # mês anterior do Totvs
        y, m = int(ym[:4]), int(ym[5:7])
        prev = f"{y-1 if m == 1 else y:04d}-{12 if m == 1 else m-1:02d}"
        t = totvs_por_mes.get(prev, {})
        t_total = t.get("BASE", 0) + t.get("COMPLEMENTAR", 0)
        if t_total < 100:
            continue
        bling = rec[ym]
        if bling == 0:
            findings.append(AuditFinding(
                check_id=f"sem_nf_bling_{ym}", status="error",
                title=f"Sem NF Bling — comp {_fmt_comp(prev)}",
                detail=(f"Totvs comissionou {_fmt_brl(t_total)} em {_fmt_comp(prev)} "
                        f"mas Bling não tem NF emitida em {_fmt_comp(ym)}."),
                competencia=ym, value=0, expected=t_total, diff=-t_total,
                action="Conferir se NF foi emitida — atrasou faturamento?",
                category="reconciliacao",
            ))
    return findings


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════
def run_audit(matriz: dict, totvs_por_mes: dict, bling_dir: Path,
              today: date | None = None, cfg: dict = CONFIG) -> list[AuditFinding]:
    today = today or date.today()
    all_findings: list[AuditFinding] = []
    all_findings += check_pis(matriz, cfg)
    all_findings += check_cofins(matriz, cfg)
    all_findings += check_iss_sbc(matriz, cfg)
    all_findings += check_irpj_trimestral(matriz, cfg, today)
    all_findings += check_csll_trimestral(matriz, cfg, today)
    all_findings += check_cbs_ibs_2026(matriz, today, cfg)
    all_findings += check_retencoes_recebidas(matriz, totvs_por_mes, cfg)
    all_findings += check_pj_sem_retencao(matriz, cfg)
    all_findings += check_reconciliacao_pagar(bling_dir, today)
    all_findings += check_vencidos(bling_dir, today)
    all_findings += check_sem_categoria(bling_dir)
    all_findings += check_reconc_bling_totvs(matriz, totvs_por_mes, cfg)
    return all_findings


def write_audit_snapshot(findings: list[AuditFinding], path: Path) -> None:
    """Grava JSON com histórico — pra acompanhar evolução semana a semana."""
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_findings": len(findings),
        "by_status": {
            s: sum(1 for f in findings if f.status == s)
            for s in ("ok", "info", "warn", "error")
        },
        "config": CONFIG,
        "findings": [f.to_dict() for f in findings],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    import os as _os
    _os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════
# HTML RENDER
# ═══════════════════════════════════════════════════════════════
_STATUS_META = {
    "error": {"tone": "red", "pill": "pr", "icon": "⛔", "lbl": "ERRO"},
    "warn":  {"tone": "amber", "pill": "pa", "icon": "⚠", "lbl": "ATENÇÃO"},
    "info":  {"tone": "blue", "pill": "pb2", "icon": "ℹ", "lbl": "INFO"},
    "ok":    {"tone": "green", "pill": "pg2", "icon": "✓", "lbl": "OK"},
}


def _render_finding_card(f: AuditFinding) -> str:
    meta = _STATUS_META.get(f.status, _STATUS_META["info"])
    tone = meta["tone"]
    parts = []
    parts.append(f'<div class="card" style="margin-bottom:10px;border-left:3px solid var(--{tone})">')
    parts.append(f'<div style="display:flex;align-items:flex-start;gap:12px;padding:4px 0">')
    parts.append(f'<div style="font-size:18px;color:var(--{tone});flex-shrink:0">{meta["icon"]}</div>')
    parts.append(f'<div style="flex:1;min-width:0">')
    parts.append(f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">')
    parts.append(f'<span class="pill {meta["pill"]}">{meta["lbl"]}</span>')
    if f.competencia:
        parts.append(f'<span style="font-family:var(--mono);font-size:10px;color:var(--t3)">{escape(_fmt_comp(f.competencia))}</span>')
    parts.append(f'<span style="font-family:var(--mono);font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:.06em">{escape(f.category)}</span>')
    parts.append('</div>')
    parts.append(f'<div style="font-weight:500;font-size:13px;color:var(--t1);margin-bottom:4px">{escape(f.title)}</div>')
    parts.append(f'<div style="font-size:11.5px;color:var(--t2);line-height:1.45">{escape(f.detail)}</div>')
    # Valores
    if f.value is not None or f.expected is not None:
        parts.append('<div style="display:flex;gap:14px;margin-top:6px;font-family:var(--mono);font-size:10.5px">')
        if f.value is not None:
            parts.append(f'<div><span style="color:var(--t3)">Pago/Real:</span> <b>{_fmt_brl(f.value)}</b></div>')
        if f.expected is not None:
            parts.append(f'<div><span style="color:var(--t3)">Esperado:</span> <b>{_fmt_brl(f.expected)}</b></div>')
        if f.diff is not None:
            color = "var(--green)" if f.diff >= 0 else "var(--red)"
            parts.append(f'<div><span style="color:var(--t3)">Δ:</span> <b style="color:{color}">{_fmt_brl(f.diff)}</b></div>')
        parts.append('</div>')
    if f.action:
        parts.append(f'<div style="margin-top:8px;padding:6px 10px;background:var(--s2);border-radius:6px;font-size:11px;color:var(--t2);line-height:1.45"><b style="color:var(--t1)">Ação:</b> {escape(f.action)}</div>')
    parts.append('</div></div></div>')
    return "".join(parts)


def render_audit_pg(findings: list[AuditFinding], today: date,
                    cfg: dict = CONFIG) -> dict[str, str]:
    """Retorna fragments HTML pra inserir no dashboard."""
    by_st = {s: [f for f in findings if f.status == s]
             for s in ("error", "warn", "info", "ok")}
    n = {k: len(v) for k, v in by_st.items()}

    valor_risco = sum(abs(f.diff or 0)
                      for f in findings if f.status in ("warn", "error"))

    # Quando o agente foi rodado pela última vez
    updated = f"{today.day:02d}/{_MESES_PT[today.month-1]}/{str(today.year)[-2:]}"

    p = []
    p.append('<!-- ═══ AUDITORIA ═══ -->')
    p.append('<div class="pg" id="pg-audit">')
    p.append('<div class="hero">')
    p.append('  <div>')
    p.append(f'    <div class="htitle">Auditoria tributária<br><span style="font-size:14px;color:var(--t2);font-weight:400">Lucro Presumido · {escape(cfg["municipio_sede"])} · {escape(cfg["atividade"])}</span></div>')
    p.append(f'    <div class="hsub">{len(findings)} checks · gerado em {updated} · valor em risco {_fmt_brl(valor_risco)}</div>')
    p.append('  </div>')
    p.append('  <div class="pills">')
    if n["error"]:
        p.append(f'    <span class="pill pr"><span class="pdot"></span>{n["error"]} erros</span>')
    if n["warn"]:
        p.append(f'    <span class="pill pa"><span class="pdot"></span>{n["warn"]} atenções</span>')
    if n["info"]:
        p.append(f'    <span class="pill pb2"><span class="pdot"></span>{n["info"]} info</span>')
    if n["ok"]:
        p.append(f'    <span class="pill pg2"><span class="pdot"></span>{n["ok"]} OK</span>')
    p.append('  </div>')
    p.append('</div>')

    # KPIs
    p.append('<div class="sl">Resumo executivo</div>')
    p.append('<div class="g4" style="margin-bottom:14px">')
    p.append(f'<div class="met red"><div class="ml">Erros (ação imediata)</div><div class="mv red">{n["error"]}</div><div class="ms">checks com diff > 30%</div></div>')
    p.append(f'<div class="met amber"><div class="ml">Atenções</div><div class="mv amber">{n["warn"]}</div><div class="ms">divergências 5–30%</div></div>')
    p.append(f'<div class="met blue"><div class="ml">Provisões / info</div><div class="mv blue">{n["info"]}</div><div class="ms">tributos a vencer</div></div>')
    p.append(f'<div class="met green"><div class="ml">Itens em ordem</div><div class="mv green">{n["ok"]}</div><div class="ms">divergência ≤ 5%</div></div>')
    p.append('</div>')

    # Findings por severidade
    for st, lbl, color in [
        ("error", "⛔ Erros — ação imediata", "red"),
        ("warn",  "⚠ Atenções — investigar",   "amber"),
        ("info",  "ℹ Provisões e informativos", "blue"),
        ("ok",    "✓ Em ordem (dentro da tolerância)", "green"),
    ]:
        items = by_st[st]
        if not items:
            continue
        # Ordena por valor de diff descrescente (maiores diff primeiro)
        items.sort(key=lambda f: -abs(f.diff or f.expected or 0))
        p.append(f'<div class="sl">{escape(lbl)} <span style="color:var(--{color});margin-left:6px;font-weight:500">({len(items)})</span></div>')
        for f in items:
            p.append(_render_finding_card(f))

    # Bloco de premissas / config
    p.append('<div class="sl">Premissas tributárias (Easy)</div>')
    p.append('<div class="card" style="font-size:11.5px;color:var(--t2);line-height:1.7">')
    p.append('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">')
    rows = [
        ("Regime",           cfg["regime"]),
        ("Município sede",   cfg["municipio_sede"]),
        ("Atividade",        cfg["atividade"]),
        ("PIS",              f"{_fmt_pct(cfg['pis_aliq'])} sobre receita bruta"),
        ("COFINS",           f"{_fmt_pct(cfg['cofins_aliq'])} sobre receita bruta"),
        ("ISS (SBC)",        f"{_fmt_pct(cfg['iss_sbc_aliq'])} · Lei 5232/03 subitens 1.01–1.08 (TI)"),
        ("IRPJ base",        f"{_fmt_pct(cfg['irpj_aliq'])} sobre lucro presumido ({_fmt_pct(cfg['irpj_base_presumida'])}) — efetivo {_fmt_pct(cfg['irpj_aliq']*cfg['irpj_base_presumida'])}"),
        ("IRPJ adicional",   f"+{_fmt_pct(cfg['irpj_adicional_aliq'])} sobre lucro presumido > {_fmt_brl(cfg['irpj_adicional_limite_trim'])}/trim"),
        ("CSLL",             f"{_fmt_pct(cfg['csll_aliq'])} × {_fmt_pct(cfg['csll_base_presumida'])} = {_fmt_pct(cfg['csll_aliq']*cfg['csll_base_presumida'])} efetivo"),
        ("CBS/IBS 2026",     f"teste {_fmt_pct(cfg['cbs_teste_aliq']+cfg['ibs_teste_aliq'])} sobre receita · 100% compensável"),
        ("Retenção PJ",      f"4,65% (PIS+COFINS+CSLL) + 1,5% IRRF · piso {_fmt_brl(cfg['ret_pis_cofins_csll_min'])}/mês"),
    ]
    for k, v in rows:
        p.append(f'<div><div style="color:var(--t3);font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px">{escape(k)}</div><div>{escape(str(v))}</div></div>')
    p.append('</div>')
    p.append('<div style="margin-top:14px;padding:10px 12px;background:var(--blue-bg);border:1px solid var(--blue-br);border-radius:6px;color:var(--blue);font-size:11px;line-height:1.5"><b>✓ Config confirmada com a contabilidade (mai/2026):</b> Lucro Presumido · sede SBC · ISS 2,00% (Lei 5232/03, subitens 1.01–1.08) · PIS 0,65% · COFINS 3% · IRPJ/CSLL trimestrais · IRRF 1,5% e CSRF 4,65% nas retenções recebidas. As checagens automatizadas são <b>aproximações</b> — não substituem revisão contábil profissional.</div>')
    p.append('</div>')

    p.append('</div>')

    return {
        "ntab":   '<button class="ntab" onclick="sp(\'audit\',this)">Auditoria</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'audit\',this,1)">Auditoria</button>',
        "pg":     "\n".join(p),
        "n_findings": len(findings),
        "n_error": n["error"],
        "n_warn":  n["warn"],
        "n_info":  n["info"],
        "n_ok":    n["ok"],
        "valor_risco": valor_risco,
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from datetime import date as _date
    ap = argparse.ArgumentParser(description="Audit agent for Easy_Financas")
    ap.add_argument("--bling-dir", type=Path, required=True)
    ap.add_argument("--totvs-snap", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="JSON output (default: <bling-dir>/audit_findings.json)")
    args = ap.parse_args(argv)

    # Importa dre_render aqui — ele faz o build da matriz
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dre_render import _load_bling_csvs, _build_matriz, _load_totvs_por_mes  # type: ignore

    pagas, em_aberto, recebidas, receber = _load_bling_csvs(args.bling_dir)
    matriz = _build_matriz(pagas, recebidas, em_aberto, receber, _date.today())
    totvs_snap = args.totvs_snap or (args.bling_dir / "totvs_snapshot.json")
    totvs_por_mes = _load_totvs_por_mes(totvs_snap)

    findings = run_audit(matriz, totvs_por_mes, args.bling_dir, _date.today())
    by_st: dict[str, int] = {}
    for f in findings:
        by_st[f.status] = by_st.get(f.status, 0) + 1

    out = args.out or (args.bling_dir / "audit_findings.json")
    write_audit_snapshot(findings, out)

    print(f"[audit] {len(findings)} findings: "
          f"{by_st.get('error', 0)} erros · "
          f"{by_st.get('warn', 0)} atenções · "
          f"{by_st.get('info', 0)} info · "
          f"{by_st.get('ok', 0)} OK",
          file=sys.stderr)
    print(f"[audit] snapshot -> {out}", file=sys.stderr)
    return 0 if by_st.get("error", 0) == 0 else 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
