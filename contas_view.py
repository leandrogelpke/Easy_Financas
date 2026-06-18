#!/usr/bin/env python3
"""
contas_view.py — Aba "A Pagar / A Receber" do dashboard Easy Analytics.

Combina dois universos:
  1. Lançado no Bling (em_aberto a pagar + em_aberto a receber)
  2. Previsto pelo histórico (fornecedor/cliente recorrente sem lançamento)

A projeção de recorrência cobre os próximos 3 meses e marca cada célula como
"Lançado" (existe NF/título no Bling), "Previsto" (esperado pelo padrão
histórico, ainda sem lançamento) ou "Vazio".

Função pública:
    render_contas(bling_dir, today, horizon=3) -> dict com fragments HTML.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
HISTORY_WINDOW_MONTHS = 6        # janela de histórico pra detectar recorrência
MIN_MONTHS_FOR_RECURRENCE = 3    # mínimo de meses com pagamento pra considerar recorrente
DEFAULT_HORIZON = 3              # quantos meses no futuro projetar
PISO_VALOR = 100.0               # valor mínimo pra considerar (ignora ruído pequeno)

# Categorias que NÃO devem aparecer como "fornecedor previsto"
# (são tributos, sócio, cartão — não são compromissos contratuais recorrentes)
SKIP_PATTERNS = [
    "IRPJ", "CSLL", "RECEITA FEDERAL", "PREFEITURA",
    "GOVERNO DO ESTADO", "MUNICIPIO DE",
    "CARTAO DE CREDITO", "CARTÃO DE CREDITO",
    "POLAR TECNICA", "IVY GROUP",  # aporte sócio
    "ROMULO FERREIRA", "GEREMIAS FERREIRA",  # buy-out/acordo (one-time, encerrarão)
]


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
_MESES = ["jan", "fev", "mar", "abr", "mai", "jun",
          "jul", "ago", "set", "out", "nov", "dez"]


def _fmt_brl(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) < 0.01:
        return "—"
    s = f"{abs(v):,.0f}".replace(",", ".")
    return f"R$ {s}" if v >= 0 else f"(R$ {s})"


def _fmt_brl_compact(v: float | None) -> str:
    if v is None or abs(v) < 0.01:
        return "—"
    if abs(v) >= 1_000_000:
        return f"R$ {v/1_000_000:.2f}M".replace(".", ",")
    if abs(v) >= 1_000:
        return f"R$ {v/1_000:.1f}K".replace(".", ",")
    return f"R$ {v:,.0f}".replace(",", ".")


def _fmt_comp(ym: str) -> str:
    if not ym or len(ym) < 7:
        return ym
    try:
        return f"{_MESES[int(ym[5:7])-1]}/{ym[2:4]}"
    except Exception:
        return ym


def _parse_money(s: Any) -> float:
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _next_months(start: date, n: int) -> list[str]:
    """Retorna lista YYYY-MM dos próximos n meses (incluindo o atual)."""
    out = []
    y, m = start.year, start.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _prev_months(end: date, n: int) -> list[str]:
    """Retorna lista YYYY-MM dos últimos n meses (até o anterior ao atual, exclusivo)."""
    out = []
    y, m = end.year, end.month
    for _ in range(n):
        m -= 1
        if m < 1:
            m = 12
            y -= 1
        out.append(f"{y:04d}-{m:02d}")
    return list(reversed(out))


def _norm(s: str) -> str:
    return (s or "").upper().strip()


def _should_skip(nome: str) -> bool:
    """True se o fornecedor/cliente não deve aparecer como previsão recorrente."""
    n = _norm(nome)
    return any(p in n for p in SKIP_PATTERNS)


# ═══════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════
def _load_csv(bling_dir: Path, prefix: str) -> list[dict]:
    files = sorted(bling_dir.glob(f"{prefix}_*.csv"))
    if not files:
        return []
    try:
        with open(files[-1], encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))
    except Exception:
        return []


@dataclass
class Counterparty:
    """Fornecedor (a pagar) ou cliente (a receber)."""
    nome: str
    cnpj: str
    history: dict[str, float] = field(default_factory=dict)  # ym -> sum value
    lancado: dict[str, float] = field(default_factory=dict)  # ym -> sum em_aberto
    lancado_vencido: dict[str, float] = field(default_factory=dict)
    is_recurrent: bool = False
    avg_value: float = 0.0
    median_value: float = 0.0
    n_meses_historico: int = 0
    confidence: str = "baixa"  # alta | media | baixa

    @property
    def total_historico(self) -> float:
        return sum(self.history.values())


# ═══════════════════════════════════════════════════════════════
# RECURRENCE DETECTION
# ═══════════════════════════════════════════════════════════════
def _build_counterparties(
    realizados: list[dict],
    em_aberto: list[dict],
    today: date,
    history_window: int = HISTORY_WINDOW_MONTHS,
) -> dict[str, Counterparty]:
    """Cruza histórico realizado + lançado em aberto pra montar perfil por
    contraparte."""
    history_months = _prev_months(today.replace(day=1), history_window)
    today_iso = today.isoformat()

    by_key: dict[str, Counterparty] = {}

    def get_or_create(nome: str, cnpj: str) -> Counterparty:
        key = _norm(nome) + "|" + (cnpj or "")
        if key not in by_key:
            by_key[key] = Counterparty(nome=nome.strip(), cnpj=cnpj or "")
        return by_key[key]

    # Histórico realizado
    for r in realizados:
        nome = (r.get("contato_nome") or "").strip()
        if not nome or _should_skip(nome):
            continue
        ym = (r.get("vencimento") or "")[:7]
        if not ym or ym not in history_months:
            continue
        v = _parse_money(r.get("valor", 0))
        if v < PISO_VALOR:
            continue
        cp = get_or_create(nome, r.get("contato_documento", ""))
        cp.history[ym] = cp.history.get(ym, 0) + v

    # Lançado em aberto — aplica o MESMO filtro de skip (tributos, sócio,
    # cartão, buy-out, acordo) pra manter consistência com o histórico
    for r in em_aberto:
        nome = (r.get("contato_nome") or "").strip()
        if not nome or _should_skip(nome):
            continue
        ym = (r.get("vencimento") or "")[:7]
        if not ym:
            continue
        v = _parse_money(r.get("valor", 0))
        if v < 1:  # aceita até pequenos lançamentos em aberto
            continue
        cp = get_or_create(nome, r.get("contato_documento", ""))
        cp.lancado[ym] = cp.lancado.get(ym, 0) + v
        if (r.get("vencimento") or "") < today_iso:
            cp.lancado_vencido[ym] = cp.lancado_vencido.get(ym, 0) + v

    # Detecta recorrência
    for cp in by_key.values():
        meses_com_pag = [v for v in cp.history.values() if v > 0]
        cp.n_meses_historico = len(meses_com_pag)
        if meses_com_pag:
            cp.avg_value = statistics.mean(meses_com_pag)
            cp.median_value = statistics.median(meses_com_pag)
        cp.is_recurrent = cp.n_meses_historico >= MIN_MONTHS_FOR_RECURRENCE
        # Confidence:
        # alta: 5-6 meses; média: 3-4 meses; baixa: <3 meses
        if cp.n_meses_historico >= 5:
            cp.confidence = "alta"
        elif cp.n_meses_historico >= 3:
            cp.confidence = "media"
        else:
            cp.confidence = "baixa"

    return by_key


# ═══════════════════════════════════════════════════════════════
# BUILD MATRIX (lançado + previsto)
# ═══════════════════════════════════════════════════════════════
@dataclass
class CellInfo:
    value: float = 0.0
    tipo: str = "vazio"  # lancado | previsto | vencido | vazio | misto
    n_titulos: int = 0


def _build_matrix(
    counterparties: dict[str, Counterparty],
    today: date,
    horizon: int = DEFAULT_HORIZON,
) -> tuple[list[str], list[Counterparty], dict[tuple[str, str], CellInfo]]:
    """
    Retorna:
      - lista dos próximos N meses (YYYY-MM)
      - lista ordenada de contrapartes (por valor total esperado decrescente)
      - matriz {(key, month): CellInfo}
    """
    future_months = _next_months(today.replace(day=1), horizon)

    matrix: dict[tuple[str, str], CellInfo] = {}
    today_ym = today.strftime("%Y-%m")

    # Ordenação: pelo total esperado (lançado + previsto) nos próximos N meses
    def expected_total(cp: Counterparty) -> float:
        total = 0.0
        for ym in future_months:
            lan = cp.lancado.get(ym, 0)
            if lan > 0:
                total += lan
            elif cp.is_recurrent and ym >= today_ym:
                total += cp.median_value
        # Boost pra quem tem vencido (importância)
        total += sum(cp.lancado_vencido.values()) * 2
        return total

    sorted_cps = sorted(
        counterparties.values(),
        key=lambda cp: -expected_total(cp),
    )
    sorted_cps = [cp for cp in sorted_cps
                  if expected_total(cp) >= PISO_VALOR
                  or cp.lancado_vencido]

    for cp in sorted_cps:
        key = _norm(cp.nome) + "|" + (cp.cnpj or "")
        for ym in future_months:
            lan = cp.lancado.get(ym, 0)
            venc = cp.lancado_vencido.get(ym, 0)
            if lan > 0:
                tipo = "vencido" if venc > 0 else "lancado"
                matrix[(key, ym)] = CellInfo(value=lan, tipo=tipo)
            elif cp.is_recurrent and ym >= today_ym:
                # Previsão sem lançamento
                matrix[(key, ym)] = CellInfo(value=cp.median_value, tipo="previsto")
            else:
                matrix[(key, ym)] = CellInfo(value=0, tipo="vazio")

    return future_months, sorted_cps, matrix


# ═══════════════════════════════════════════════════════════════
# RENDER HTML
# ═══════════════════════════════════════════════════════════════
_CONF_PILL = {
    "alta":  ('<span class="pill pg2" style="font-size:9px;padding:0 5px">alta</span>'),
    "media": ('<span class="pill pa" style="font-size:9px;padding:0 5px">média</span>'),
    "baixa": ('<span class="pill pb2" style="font-size:9px;padding:0 5px">baixa</span>'),
}


def _cell_html(cell: CellInfo) -> str:
    """Renderiza uma célula da matriz (valor + tag tipo)."""
    if cell.tipo == "vazio" or cell.value < 0.01:
        return '<td class="r" style="font-family:var(--mono);font-size:11px;color:var(--t3)">—</td>'

    v_fmt = _fmt_brl_compact(cell.value)
    v_full = escape(_fmt_brl(cell.value))
    if cell.tipo == "vencido":
        return (f'<td class="r" title="{v_full}" style="font-family:var(--mono);font-size:11px;'
                f'color:var(--red);font-weight:500;padding:6px 8px">'
                f'⚠ {v_fmt}<div style="font-size:9px;color:var(--red);'
                f'font-weight:400">vencido</div></td>')
    if cell.tipo == "lancado":
        return (f'<td class="r" title="{v_full}" style="font-family:var(--mono);font-size:11px;'
                f'color:var(--t1);font-weight:500;padding:6px 8px">'
                f'{v_fmt}<div style="font-size:9px;color:var(--green);'
                f'font-weight:400">lançado</div></td>')
    if cell.tipo == "previsto":
        return (f'<td class="r" title="{v_full}" style="font-family:var(--mono);font-size:11px;'
                f'color:var(--t2);padding:6px 8px;font-style:italic">'
                f'~{v_fmt}<div style="font-size:9px;color:var(--amber);'
                f'font-weight:400;font-style:normal">previsto</div></td>')
    return f'<td class="r" title="{v_full}">{v_fmt}</td>'


def _render_matrix_table(
    title: str,
    subtitle: str,
    future_months: list[str],
    counterparties: list[Counterparty],
    matrix: dict[tuple[str, str], CellInfo],
    label_contraparte: str = "Fornecedor",
) -> str:
    p = []
    p.append('<div class="card" style="margin-bottom:14px">')
    p.append(f'<div class="ct"><b>{escape(title)}</b>'
             f'<span>{escape(subtitle)}</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt">')
    p.append('<thead><tr>')
    p.append(f'<th style="position:sticky;left:0;background:var(--s1);z-index:2">'
             f'{escape(label_contraparte)}</th>')
    p.append('<th style="text-align:center">Histórico</th>')
    for ym in future_months:
        p.append(f'<th class="r">{_fmt_comp(ym)}</th>')
    p.append(f'<th class="r" style="border-left:1px solid var(--bd)">Total {len(future_months)}m</th>')
    p.append('</tr></thead><tbody>')

    # Totais por coluna
    col_total = defaultdict(float)
    col_lancado = defaultdict(float)
    col_previsto = defaultdict(float)
    col_vencido = defaultdict(float)

    for cp in counterparties:
        key = _norm(cp.nome) + "|" + (cp.cnpj or "")
        # Coluna 0: fornecedor
        cnpj_fmt = ""
        if cp.cnpj:
            d = "".join(c for c in cp.cnpj if c.isdigit())
            if len(d) == 14:
                cnpj_fmt = f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
            elif len(d) == 11:
                cnpj_fmt = f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
            else:
                cnpj_fmt = cp.cnpj
        p.append('<tr>')
        p.append(f'<td style="position:sticky;left:0;background:var(--s1);z-index:1;'
                 f'padding:6px 10px;font-size:11.5px">'
                 f'{escape(cp.nome[:42])}'
                 f'<div style="font-size:9px;color:var(--t3);font-family:var(--mono)">'
                 f'{escape(cnpj_fmt)}</div></td>')

        # Coluna 1: histórico (badge confidence + n meses)
        conf_pill = _CONF_PILL.get(cp.confidence, "")
        p.append(f'<td style="text-align:center;padding:6px 8px">'
                 f'{conf_pill}<div style="font-size:9px;color:var(--t3);'
                 f'font-family:var(--mono);margin-top:2px">'
                 f'{cp.n_meses_historico}/6m · mediana {_fmt_brl_compact(cp.median_value)}'
                 f'</div></td>')

        # Colunas dos meses futuros
        row_total = 0.0
        for ym in future_months:
            cell = matrix.get((key, ym), CellInfo())
            p.append(_cell_html(cell))
            row_total += cell.value
            col_total[ym] += cell.value
            if cell.tipo == "lancado":
                col_lancado[ym] += cell.value
            elif cell.tipo == "previsto":
                col_previsto[ym] += cell.value
            elif cell.tipo == "vencido":
                col_vencido[ym] += cell.value

        # Coluna total da linha
        p.append(f'<td class="r" style="font-family:var(--mono);font-size:11.5px;'
                 f'font-weight:500;border-left:1px solid var(--bd);padding:6px 10px">'
                 f'{_fmt_brl_compact(row_total)}</td>')
        p.append('</tr>')

    # Linha de totais
    p.append('<tr style="background:var(--s2);border-top:2px solid var(--bd2)">')
    p.append('<td style="position:sticky;left:0;background:var(--s2);z-index:1;'
             'padding:8px 10px;font-weight:600;font-size:12px">Total</td>')
    p.append('<td></td>')
    grand_total = 0.0
    for ym in future_months:
        tot = col_total[ym]
        grand_total += tot
        # Mini-breakdown na própria célula
        breakdown = []
        if col_lancado[ym] > 0:
            breakdown.append(f'<span style="color:var(--t1)">{_fmt_brl_compact(col_lancado[ym])} L</span>')
        if col_previsto[ym] > 0:
            breakdown.append(f'<span style="color:var(--amber)">{_fmt_brl_compact(col_previsto[ym])} P</span>')
        if col_vencido[ym] > 0:
            breakdown.append(f'<span style="color:var(--red)">{_fmt_brl_compact(col_vencido[ym])} V</span>')
        p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;'
                 f'padding:8px;font-weight:600">'
                 f'<div>{_fmt_brl_compact(tot)}</div>'
                 f'<div style="font-size:9px;font-weight:400;margin-top:2px">'
                 f'{" · ".join(breakdown) if breakdown else ""}</div></td>')
    p.append(f'<td class="r" style="font-family:var(--mono);font-weight:600;'
             f'font-size:13px;border-left:1px solid var(--bd);padding:8px 10px">'
             f'{_fmt_brl_compact(grand_total)}</td>')
    p.append('</tr>')

    p.append('</tbody></table></div>')

    # Legenda
    p.append('<div style="margin-top:10px;padding:10px 12px;background:var(--s2);'
             'border-radius:8px;font-size:11px;color:var(--t2);line-height:1.5">'
             '<b>Como ler:</b> '
             '<span style="color:var(--t1)">Lançado (L)</span> = título existe no Bling · '
             '<span style="color:var(--amber);font-style:italic">Previsto (P)</span> = '
             'estimativa baseada no padrão histórico (mediana dos últimos 6 meses) — '
             'ainda sem lançamento · '
             '<span style="color:var(--red)">Vencido (V)</span> = lançado e em atraso · '
             'Histórico mostra confidence: '
             '<span class="pill pg2" style="font-size:9px;padding:0 5px">alta</span> '
             '5–6 meses · '
             '<span class="pill pa" style="font-size:9px;padding:0 5px">média</span> '
             '3–4 meses · '
             '<span class="pill pb2" style="font-size:9px;padding:0 5px">baixa</span> '
             '&lt; 3 meses (não projeta).</div>')

    p.append('</div>')
    return "".join(p)


# ═══════════════════════════════════════════════════════════════
# SNAPSHOT
# ═══════════════════════════════════════════════════════════════
def write_contas_snapshot(
    snapshot: dict[str, Any], path: Path
) -> None:
    """Grava resumo JSON pra leitura externa (weekly.sh, histórico)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════
# API PÚBLICA
# ═══════════════════════════════════════════════════════════════
def render_contas(bling_dir: Path, today: date | None = None,
                  horizon: int = DEFAULT_HORIZON) -> dict[str, Any]:
    """
    Retorna fragments HTML pra inserir no dashboard:
      ntab, mobtab, pg, n_pagar_lancado, n_pagar_previsto, valor_pagar_total,
      n_receber_lancado, n_receber_previsto, valor_receber_total
    """
    today = today or date.today()
    pagas = _load_csv(bling_dir, "contas_pagar_pagas")
    em_aberto_pagar = _load_csv(bling_dir, "contas_pagar_em_aberto")
    recebidas = _load_csv(bling_dir, "contas_receber_recebidas")
    em_aberto_receber = _load_csv(bling_dir, "contas_receber_em_aberto")

    # Reconciliação: remove fantasmas do a pagar (duplicatas já pagas +
    # provisões cobertas). Fonte única em audit.py — mantém esta aba alinhada
    # com Visão Geral / Caixa / DRE / Auditoria.
    try:
        from audit import reconcile_em_aberto  # type: ignore
        em_aberto_pagar, _ = reconcile_em_aberto(pagas, em_aberto_pagar, today)
    except Exception:
        pass

    fornecedores = _build_counterparties(pagas, em_aberto_pagar, today)
    clientes = _build_counterparties(recebidas, em_aberto_receber, today)

    f_months, f_sorted, f_matrix = _build_matrix(fornecedores, today, horizon)
    c_months, c_sorted, c_matrix = _build_matrix(clientes, today, horizon)

    # KPIs A pagar
    pagar_lancado = sum(
        cell.value for (_, _), cell in f_matrix.items()
        if cell.tipo in ("lancado", "vencido")
    )
    pagar_previsto = sum(
        cell.value for (_, _), cell in f_matrix.items()
        if cell.tipo == "previsto"
    )
    pagar_vencido = sum(
        cell.value for (_, _), cell in f_matrix.items()
        if cell.tipo == "vencido"
    )
    n_forn_lancados = sum(1 for cp in f_sorted if cp.lancado)
    n_forn_previstos = sum(1 for cp in f_sorted
                           if cp.is_recurrent and any(
                               f_matrix.get((_norm(cp.nome)+"|"+(cp.cnpj or ""), ym),
                                            CellInfo()).tipo == "previsto"
                               for ym in f_months))

    # KPIs A receber
    receber_lancado = sum(
        cell.value for (_, _), cell in c_matrix.items()
        if cell.tipo in ("lancado", "vencido")
    )
    receber_previsto = sum(
        cell.value for (_, _), cell in c_matrix.items()
        if cell.tipo == "previsto"
    )
    receber_vencido = sum(
        cell.value for (_, _), cell in c_matrix.items()
        if cell.tipo == "vencido"
    )
    n_cli_lancados = sum(1 for cp in c_sorted if cp.lancado)
    n_cli_previstos = sum(1 for cp in c_sorted
                          if cp.is_recurrent and any(
                              c_matrix.get((_norm(cp.nome)+"|"+(cp.cnpj or ""), ym),
                                           CellInfo()).tipo == "previsto"
                              for ym in c_months))

    # Header dinâmico
    horizon_lbl = f"{_fmt_comp(f_months[0])} → {_fmt_comp(f_months[-1])}"
    gap_30d = receber_lancado - max(pagar_vencido + pagar_lancado, 0)

    p = []
    p.append('<!-- ═══ A PAGAR / A RECEBER ═══ -->')
    p.append('<div class="pg" id="pg-contas">')
    p.append('<div class="hero">')
    p.append('  <div>')
    p.append('    <div class="htitle">A Pagar &amp; A Receber<br>'
             '<span style="font-size:14px;color:var(--t2);font-weight:400">'
             f'{horizon} meses · lançado no Bling + previsto pelo histórico</span></div>')
    p.append(f'    <div class="hsub">{horizon_lbl} · contrapartes recorrentes detectadas '
             f'pelo padrão dos últimos {HISTORY_WINDOW_MONTHS} meses</div>')
    p.append('  </div>')
    p.append('  <div class="pills">')
    if pagar_vencido > 0:
        p.append(f'    <span class="pill pr"><span class="pdot"></span>'
                 f'{_fmt_brl_compact(pagar_vencido)} vencidos a pagar</span>')
    if receber_vencido > 0:
        p.append(f'    <span class="pill pr"><span class="pdot"></span>'
                 f'{_fmt_brl_compact(receber_vencido)} vencidos a receber</span>')
    p.append(f'    <span class="pill pg2"><span class="pdot"></span>'
             f'{_fmt_brl_compact(receber_lancado + receber_previsto)} a receber {horizon}m</span>')
    p.append(f'    <span class="pill pa"><span class="pdot"></span>'
             f'{_fmt_brl_compact(pagar_lancado + pagar_previsto)} a pagar {horizon}m</span>')
    p.append('  </div>')
    p.append('</div>')

    # KPIs
    p.append('<div class="sl">Visão consolidada — próximos 3 meses</div>')
    p.append('<div class="g4" style="margin-bottom:14px">')
    gap_color = "green" if gap_30d >= 0 else "red"
    gap_sign = "+" if gap_30d >= 0 else ""
    p.append(f'<div class="met blue"><div class="ml">A receber total</div>'
             f'<div class="mv blue">{_fmt_brl_compact(receber_lancado + receber_previsto)}</div>'
             f'<div class="ms">{_fmt_brl_compact(receber_lancado)} lançado · '
             f'{_fmt_brl_compact(receber_previsto)} previsto</div></div>')
    p.append(f'<div class="met amber"><div class="ml">A pagar total</div>'
             f'<div class="mv amber">{_fmt_brl_compact(pagar_lancado + pagar_previsto)}</div>'
             f'<div class="ms">{_fmt_brl_compact(pagar_lancado)} lançado · '
             f'{_fmt_brl_compact(pagar_previsto)} previsto</div></div>')
    p.append(f'<div class="met {gap_color}"><div class="ml">Gap (receber − pagar)</div>'
             f'<div class="mv {gap_color}">{gap_sign}{_fmt_brl_compact(abs(gap_30d))}</div>'
             f'<div class="ms">posição líquida considerando recorrência</div></div>')
    total_vencido = pagar_vencido + receber_vencido
    venc_color = "red" if total_vencido > 0 else "green"
    p.append(f'<div class="met {venc_color}"><div class="ml">Vencidos (pagar + receber)</div>'
             f'<div class="mv {venc_color}">{_fmt_brl_compact(total_vencido)}</div>'
             f'<div class="ms">{_fmt_brl_compact(pagar_vencido)} pagar · '
             f'{_fmt_brl_compact(receber_vencido)} receber</div></div>')
    p.append('</div>')

    # A receber
    p.append('<div class="sl">Contas a receber</div>')
    p.append(_render_matrix_table(
        title="A receber por cliente — próximos meses",
        subtitle=(f"{len(c_sorted)} clientes · {n_cli_lancados} com lançamento · "
                  f"{n_cli_previstos} previstos pelo histórico"),
        future_months=c_months,
        counterparties=c_sorted,
        matrix=c_matrix,
        label_contraparte="Cliente",
    ))

    # A pagar
    p.append('<div class="sl">Contas a pagar</div>')
    p.append(_render_matrix_table(
        title="A pagar por fornecedor — próximos meses",
        subtitle=(f"{len(f_sorted)} fornecedores · {n_forn_lancados} com lançamento · "
                  f"{n_forn_previstos} previstos pelo histórico · "
                  "tributos, sócio e cartão excluídos"),
        future_months=f_months,
        counterparties=f_sorted,
        matrix=f_matrix,
        label_contraparte="Fornecedor",
    ))

    # Nota metodológica
    p.append('<div class="sl">Notas metodológicas</div>')
    p.append('<div class="card" style="font-size:11.5px;color:var(--t2);line-height:1.7">')
    p.append('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px">')
    notas = [
        ("Detecção de recorrência",
         f"Contraparte com pagamento em ≥{MIN_MONTHS_FOR_RECURRENCE} dos últimos "
         f"{HISTORY_WINDOW_MONTHS} meses é considerada recorrente e tem os próximos "
         f"{horizon} meses projetados."),
        ("Valor previsto",
         "Mediana dos pagamentos históricos (mais robusta a outliers do que média)."),
        ("Confidence",
         "Alta: 5–6 meses de histórico. Média: 3–4 meses. Baixa: &lt;3 meses (não projeta — mostra só lançado)."),
        ("O que é excluído",
         "Tributos federais/municipais, aporte do sócio (Polar/IVY), cartão de crédito, buy-out e acordo (encerramento previsto). Esses têm comportamento não-comercial e ficam em outras abas."),
        ("Lançado vs previsto",
         "<b>Lançado</b> = título já existe no Bling (NF emitida ou conta a pagar registrada). <b>Previsto</b> = expectativa pelo padrão histórico, ainda sem lançamento formal."),
        ("Atualização",
         "Esta aba reflete o último snapshot Bling. Roda a cada execução do weekly.sh — quando lançamentos novos entram no Bling, sai automaticamente da coluna <i>previsto</i> e vai pra <i>lançado</i>."),
    ]
    for k, v in notas:
        p.append(f'<div><div style="color:var(--t3);font-family:var(--mono);'
                 f'font-size:10px;text-transform:uppercase;letter-spacing:.06em;'
                 f'margin-bottom:4px">{escape(k)}</div><div>{v}</div></div>')
    p.append('</div></div>')

    p.append('</div>')

    # ── Resumo estruturado pra snapshot JSON ──
    def _cp_summary(cps: list[Counterparty], months: list[str],
                    mat: dict[tuple[str, str], CellInfo]) -> list[dict]:
        items = []
        for cp in cps:
            key = _norm(cp.nome) + "|" + (cp.cnpj or "")
            por_mes = {ym: {"valor": round(mat.get((key, ym), CellInfo()).value, 2),
                            "tipo": mat.get((key, ym), CellInfo()).tipo}
                       for ym in months}
            items.append({
                "nome": cp.nome,
                "cnpj": cp.cnpj,
                "n_meses_historico": cp.n_meses_historico,
                "confidence": cp.confidence,
                "mediana_historica": round(cp.median_value, 2),
                "media_historica": round(cp.avg_value, 2),
                "vencido_total": round(sum(cp.lancado_vencido.values()), 2),
                "por_mes": por_mes,
                "total_horizonte": round(
                    sum(mat.get((key, ym), CellInfo()).value for ym in months), 2),
            })
        return items

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "horizon_meses": horizon,
        "horizon_range": [f_months[0], f_months[-1]],
        "totais": {
            "pagar_lancado": round(pagar_lancado, 2),
            "pagar_previsto": round(pagar_previsto, 2),
            "pagar_vencido": round(pagar_vencido, 2),
            "receber_lancado": round(receber_lancado, 2),
            "receber_previsto": round(receber_previsto, 2),
            "receber_vencido": round(receber_vencido, 2),
            "gap_3m": round(receber_lancado + receber_previsto -
                            pagar_lancado - pagar_previsto, 2),
        },
        "fornecedores": _cp_summary(f_sorted, f_months, f_matrix),
        "clientes": _cp_summary(c_sorted, c_months, c_matrix),
    }

    return {
        "ntab": '<button class="ntab" onclick="sp(\'contas\',this)">A Pagar / Receber</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'contas\',this,1)">A Pagar / Receber</button>',
        "pg": "\n".join(p),
        "n_forn_lancados": n_forn_lancados,
        "n_forn_previstos": n_forn_previstos,
        "n_cli_lancados": n_cli_lancados,
        "n_cli_previstos": n_cli_previstos,
        "pagar_total": pagar_lancado + pagar_previsto,
        "receber_total": receber_lancado + receber_previsto,
        "pagar_vencido": pagar_vencido,
        "receber_vencido": receber_vencido,
        "summary": summary,
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Renderiza aba contas a pagar/receber")
    ap.add_argument("--bling-dir", type=Path, required=True)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--out", type=Path, default=None, help="HTML output")
    args = ap.parse_args()
    r = render_contas(args.bling_dir, horizon=args.horizon)
    print(f"[contas] fornecedores: {r['n_forn_lancados']} L + "
          f"{r['n_forn_previstos']} P · "
          f"clientes: {r['n_cli_lancados']} L + {r['n_cli_previstos']} P",
          file=sys.stderr)
    print(f"[contas] a pagar total {args.horizon}m: R$ {r['pagar_total']:,.0f} · "
          f"a receber R$ {r['receber_total']:,.0f}".replace(",", "."),
          file=sys.stderr)
    if args.out:
        args.out.write_text(r["pg"], encoding="utf-8")
        print(f"[contas] fragment salvo em {args.out}", file=sys.stderr)
    sys.exit(0)
