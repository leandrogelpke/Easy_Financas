#!/usr/bin/env python3
"""cartao_render.py — renderiza a aba "Cartão" do dashboard Easy Analytics.

Lê `cartao_snapshot.json` (gerado por fetch-cartao.py) e devolve os
fragments HTML (ntab, mobtab, pg) que o build-html.py injeta no template,
exatamente como totvs_render.py / contas_view.py.

Tudo é data-driven: nenhum número é hardcoded aqui (vem do snapshot).
Gráficos são barras CSS (resilientes — funcionam mesmo sem Chart.js).
"""
from __future__ import annotations
import json
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SNAP = HERE / "cartao_snapshot.json"

_MES = ["jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez"]


def _fmt_brl(v: float, compact: bool = False) -> str:
    if compact:
        if abs(v) >= 1_000_000:
            return f"R$ {v/1_000_000:.1f}M".replace(".", ",")
        if abs(v) >= 1_000:
            return f"R$ {v/1_000:.1f}K".replace(".", ",")
        return f"R$ {v:.0f}"
    return "R$ " + f"{round(v):,}".replace(",", ".")


def _fmt_brl_preciso(v: float) -> str:
    """Valor exato em R$ com 2 casas, locale pt-BR (ex.: 1.234,56). Usado só no title=."""
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_usd(v: float, compact: bool = False) -> str:
    if compact and abs(v) >= 1_000:
        return f"US$ {v/1_000:.1f}K".replace(".", ",")
    return "US$ " + f"{round(v):,}".replace(",", ".")


def _comp_label(comp: str) -> str:
    """'2026-01' → 'Jan/26'."""
    try:
        y, m = comp.split("-")
        return _MES[int(m) - 1].capitalize() + "/" + y[2:]
    except Exception:
        return comp


def _card_short(name: str) -> str:
    n = (name or "").upper()
    if "EASY" in n:
        return "Easy Analytics"
    if "PAULO" in n:
        return "Paulo V. Andrade"
    return (name or "")[:20]


def _load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _empty_pg() -> dict:
    pg = (
        '<div class="pg" id="pg-cartao">'
        '<div class="hero"><div><div class="htitle">Cartão de Crédito</div>'
        '<div class="hsub">Aguardando faturas</div></div></div>'
        '<div class="card" style="padding:32px;text-align:center;color:var(--t3)">'
        '<div style="font-family:var(--mono);font-size:11px;margin-bottom:8px">SEM DADOS</div>'
        '<div style="font-size:14px;color:var(--t2)">Coloque as faturas PDF em '
        '<b>Controles Easy › Cartão de Crédito 2026 › 2026</b> e rode o runbook.</div>'
        '</div></div>'
    )
    return {
        "ntab": '<button class="ntab" onclick="sp(\'cartao\',this)">Cartão</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'cartao\',this,1)">Cartão</button>',
        "pg": pg, "n_transacoes": 0, "total_brl": 0.0,
    }


def render_cartao(snap_path: Path = DEFAULT_SNAP) -> dict:
    snap = _load(Path(snap_path))
    txs = snap.get("transacoes", [])
    if not txs:
        return _empty_pg()

    comps = snap["comps"]
    cards = snap["cards"]
    total = snap["total_brl"]
    total_usd = snap["total_usd"]
    by_month = snap["by_month"]
    by_month_card = snap["by_month_card"]
    by_vendor = snap["by_vendor"]
    vendor_colors = snap.get("vendor_colors", {})

    n_meses = len(comps)
    media_mes = total / n_meses if n_meses else 0
    ultimo = comps[-1]
    ultimo_val = by_month.get(ultimo, 0)
    primeiro_val = by_month.get(comps[0], 0)
    iof_total = by_vendor.get("IOF / impostos", 0)
    aws_total = by_vendor.get("AWS", 0)
    aws_pct = round(100 * aws_total / total) if total else 0
    # variação último vs primeiro mês
    var_pct = round(100 * (ultimo_val - primeiro_val) / primeiro_val) if primeiro_val else 0

    # ── KPIs (g4) ──
    kpis = (
        '<div class="g4">'
        f'<div class="met blue"><div class="ml">Total no período</div>'
        f'<div class="mv blue">{_fmt_brl(total)}</div>'
        f'<div class="ms">{len(txs)} lançamentos · {_fmt_usd(total_usd)} em moeda estrangeira</div></div>'
        f'<div class="met teal"><div class="ml">Média mensal</div>'
        f'<div class="mv teal">{_fmt_brl(media_mes)}</div>'
        f'<div class="ms">{n_meses} meses ({_comp_label(comps[0])}–{_comp_label(ultimo)})</div></div>'
        f'<div class="met amber"><div class="ml">Último mês ({_comp_label(ultimo)})</div>'
        f'<div class="mv amber">{_fmt_brl(ultimo_val)}</div>'
        f'<div class="ms">{("+" if var_pct>=0 else "")}{var_pct}% vs {_comp_label(comps[0])}</div></div>'
        f'<div class="met red"><div class="ml">IOF / impostos</div>'
        f'<div class="mv red">{_fmt_brl(iof_total)}</div>'
        f'<div class="ms">{round(100*iof_total/total) if total else 0}% do total · custo de compras no exterior</div></div>'
        '</div>'
    )

    # ── Evolução mensal (barras CSS, empilhadas por cartão) ──
    max_mes = max(by_month.values()) if by_month else 1
    card_colors = {cards[0]: "var(--amber)"}
    if len(cards) > 1:
        card_colors[cards[1]] = "var(--blue)"
    rows_mes = []
    for c in comps:
        seg = []
        for cd in cards:
            v = by_month_card.get(c, {}).get(cd, 0)
            if v <= 0:
                continue
            w = 100 * v / max_mes
            seg.append(
                f'<div title="{escape(_card_short(cd))}: {_fmt_brl_preciso(v)}" '
                f'style="width:{w:.2f}%;background:{card_colors.get(cd,"var(--blue)")};'
                f'height:100%"></div>'
            )
        bar = "".join(seg)
        rows_mes.append(
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">'
            f'<div style="width:54px;font-family:var(--mono);font-size:11px;color:var(--t2)">{_comp_label(c)}</div>'
            f'<div style="flex:1;height:18px;background:var(--s2);border-radius:4px;overflow:hidden;display:flex">{bar}</div>'
            f'<div style="width:90px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--t1)">{_fmt_brl(by_month[c])}</div>'
            '</div>'
        )
    legenda_cartoes = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:14px">'
        f'<span style="width:9px;height:9px;border-radius:2px;background:{card_colors.get(cd,"var(--blue)")}"></span>'
        f'{escape(_card_short(cd))}</span>'
        for cd in cards
    )
    evol = (
        '<div class="card" style="margin-bottom:12px">'
        '<div class="ct">Evolução mensal <span class="ct-tag">por cartão</span></div>'
        f'<div style="margin-top:10px">{"".join(rows_mes)}</div>'
        f'<div style="margin-top:8px;font-size:10.5px;color:var(--t2)">{legenda_cartoes}</div>'
        '</div>'
    )

    # ── Por fornecedor (barras CSS) ──
    vend_sorted = sorted(by_vendor.items(), key=lambda kv: -kv[1])
    max_v = vend_sorted[0][1] if vend_sorted else 1
    rows_vend = []
    for name, v in vend_sorted:
        w = 100 * v / max_v
        pct = round(100 * v / total) if total else 0
        col = vendor_colors.get(name, "#3d5a80")
        rows_vend.append(
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">'
            f'<div style="width:110px;font-size:11.5px;color:var(--t1)">{escape(name)}</div>'
            f'<div style="flex:1;height:18px;background:var(--s2);border-radius:4px;overflow:hidden">'
            f'<div title="{escape(name)}: {_fmt_brl_preciso(v)}" style="width:{w:.2f}%;height:100%;background:{col}"></div></div>'
            f'<div style="width:120px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--t1)">{_fmt_brl(v)} <span style="color:var(--t3)">{pct}%</span></div>'
            '</div>'
        )
    vendores = (
        '<div class="card" style="margin-bottom:12px">'
        '<div class="ct">Gasto por fornecedor <span class="ct-tag">período todo</span></div>'
        f'<div style="margin-top:10px">{"".join(rows_vend)}</div>'
        '</div>'
    )

    # ── Detalhamento por mês (tabela colapsável) ──
    blocks = []
    for c in reversed(comps):  # mais recente primeiro
        itens = [t for t in txs if t["comp"] == c]
        itens.sort(key=lambda t: (t["data"][3:], t["data"][:2]))
        tot_c = sum(t["brl"] for t in itens)
        body = []
        for t in itens:
            usd = f'{t["usd"]:,.2f}'.replace(",", "X").replace(".", ",").replace("X", ".") if t["usd"] else "—"
            body.append(
                f'<tr><td style="font-family:var(--mono);font-size:11px;color:var(--t2)">{escape(t["data"])}</td>'
                f'<td>{escape(t["historico"])}</td>'
                f'<td style="color:var(--t3)">{escape(t.get("card_last",""))}</td>'
                f'<td>{escape(t["vendor"])}</td>'
                f'<td class="r">{usd}</td>'
                f'<td class="r" title="{_fmt_brl_preciso(t["brl"])}">{_fmt_brl(t["brl"])}</td></tr>'
            )
        cid = c.replace("-", "")
        blocks.append(
            f'<div class="card" style="margin-bottom:10px">'
            f'<div class="ct" style="cursor:pointer" onclick="cartToggle(\'{cid}\')">'
            f'<span id="cic-{cid}">▸</span> {_comp_label(c)} '
            f'<span class="ct-tag">{len(itens)} lançamentos</span> '
            f'<span style="float:right;font-family:var(--mono);color:var(--t1)" title="{_fmt_brl_preciso(tot_c)}">{_fmt_brl(tot_c)}</span></div>'
            f'<div id="cd-{cid}" style="display:none;margin-top:8px">'
            '<table class="dt"><thead><tr><th>Data</th><th>Histórico</th><th>Cartão</th>'
            '<th>Fornecedor</th><th class="r">US$</th><th class="r">R$</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></div>'
        )
    detalhe = (
        '<div class="sl">Detalhamento por mês <span style="color:var(--t3);font-size:11px;text-transform:none;letter-spacing:0">— toque no mês para expandir</span></div>'
        + "".join(blocks)
    )

    hero_sub = (
        f'{len(snap.get("faturas", []))} faturas · {_comp_label(comps[0])}–{_comp_label(ultimo)} · '
        f'{len(cards)} cartões (Bradesco VISA)'
    )

    pg = f"""<!-- ═══ CARTÃO DE CRÉDITO ═══ -->
<div class="pg" id="pg-cartao">
<div class="hero">
  <div>
    <div class="htitle">Cartão de Crédito<br><span style="font-size:14px;color:var(--t2);font-weight:400">Bradesco · análise de gastos por fornecedor e cartão</span></div>
    <div class="hsub">{hero_sub}</div>
  </div>
  <div class="pills">
    <span class="pill pb2"><span class="pdot"></span>Total {_fmt_brl(total, True)}</span>
    <span class="pill pg2"><span class="pdot"></span>{_fmt_usd(total_usd, True)} no exterior</span>
    <span class="pill pa"><span class="pdot"></span>AWS {aws_pct}% do gasto</span>
  </div>
</div>
{kpis}
<div class="sl">Visão geral</div>
{evol}
{vendores}
{detalhe}
<script>
function cartToggle(c){{
  var d=document.getElementById('cd-'+c), i=document.getElementById('cic-'+c);
  if(d.style.display==='none'){{d.style.display='block';i.textContent='▾';}}
  else{{d.style.display='none';i.textContent='▸';}}
}}
</script>
</div>"""

    return {
        "ntab": '<button class="ntab" onclick="sp(\'cartao\',this)">Cartão</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'cartao\',this,1)">Cartão</button>',
        "pg": pg,
        "n_transacoes": len(txs),
        "total_brl": total,
    }


if __name__ == "__main__":
    import sys
    r = render_cartao()
    print(f"[ok] n_transacoes={r['n_transacoes']} total={r['total_brl']:.2f}",
          file=sys.stderr)
    print(r["pg"][:600])
