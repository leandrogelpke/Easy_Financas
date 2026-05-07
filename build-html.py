#!/usr/bin/env python3
"""
build-html.py - Le bling_data_<data>.json gerado por fetch-bling.py e
produz bling-live.html (dashboard 'live' com dados do dia).

Uso:
    python3 ~/Documents/Easy_Financas/build-html.py
    python3 ~/Documents/Easy_Financas/build-html.py --in <pasta> --out <arquivo>

Comportamento default:
    - le o bling_data_*.json mais recente em ~/Documents/GRAP/.../relatorios atuais/bling-api/
      (ou em --in)
    - escreve bling-live.html em ~/Documents/Easy_Financas/bling-live.html
      (ou em --out)
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple


HERE = Path(__file__).resolve().parent
DEFAULT_IN_CANDIDATES = [
    Path.home() / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api",
    HERE / "relatorios",
]
DEFAULT_OUT = HERE / "bling-live.html"


def find_latest_snapshot(in_dir: Path) -> Path:
    candidates = sorted(in_dir.glob("bling_data_*.json"))
    if not candidates:
        raise FileNotFoundError(f"nenhum bling_data_*.json em {in_dir}")
    return candidates[-1]


def parse_money(s: str) -> float:
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fmt_brl(v: float) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}R$ {v/1_000_000:.1f} M".replace(".", ",")
    if v >= 1_000:
        return f"{sign}R$ {v/1_000:.0f} K"
    return f"{sign}R$ {v:.0f}"


def fmt_brl_full(v: float) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}R$ {s}"


def fmt_date_br(d: date | None) -> str:
    if not d:
        return ""
    months = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    return f"{d.day:02d}/{months[d.month-1]}/{str(d.year)[-2:]}"


def aggregate(data: Dict[str, Any], today: date) -> Dict[str, Any]:
    pagar_aberto = data.get("contas_pagar_em_aberto", [])
    pagar_pagas = data.get("contas_pagar_pagas", [])
    receber_aberto = data.get("contas_receber_em_aberto", [])
    receber_recebidas = data.get("contas_receber_recebidas", [])

    # 1) Proximos a pagar — agrupados por janela
    upcoming = []
    overdue = []
    for c in pagar_aberto:
        venc = parse_date(c.get("vencimento", ""))
        if not venc:
            continue
        valor = parse_money(c.get("valor"))
        item = {
            "venc": venc,
            "venc_str": fmt_date_br(venc),
            "valor": valor,
            "valor_str": fmt_brl_full(valor),
            "fornecedor": c.get("contato_nome") or "(sem fornecedor)",
            "categoria": c.get("categoria_descricao") or "(sem categoria)",
            "historico": c.get("historico", "")[:80],
        }
        if venc < today:
            overdue.append(item)
        else:
            upcoming.append(item)
    overdue.sort(key=lambda r: r["venc"])
    upcoming.sort(key=lambda r: r["venc"])

    venc_30 = today + timedelta(days=30)
    venc_60 = today + timedelta(days=60)
    venc_90 = today + timedelta(days=90)
    bucket_overdue = sum(r["valor"] for r in overdue)
    bucket_30 = sum(r["valor"] for r in upcoming if r["venc"] <= venc_30)
    bucket_60 = sum(r["valor"] for r in upcoming if venc_30 < r["venc"] <= venc_60)
    bucket_90 = sum(r["valor"] for r in upcoming if venc_60 < r["venc"] <= venc_90)
    bucket_total = sum(parse_money(r.get("valor")) for r in pagar_aberto if isinstance(r, dict))

    # 2) Receber em aberto — sort por venc
    receber_rows = []
    receber_overdue = []
    receber_total = 0.0
    for c in receber_aberto:
        venc = parse_date(c.get("vencimento", ""))
        valor = parse_money(c.get("valor"))
        receber_total += valor
        item = {
            "venc": venc,
            "venc_str": fmt_date_br(venc),
            "valor": valor,
            "valor_str": fmt_brl_full(valor),
            "cliente": c.get("contato_nome") or "(sem cliente)",
            "historico": c.get("historico", "")[:80],
        }
        if venc and venc < today:
            receber_overdue.append(item)
        else:
            receber_rows.append(item)
    receber_overdue.sort(key=lambda r: r["venc"] or date.min)
    receber_rows.sort(key=lambda r: r["venc"] or date.max)

    # 3) Gastos por categoria (pagas)
    cat_total = defaultdict(lambda: {"valor": 0.0, "count": 0})
    pagas_total = 0.0
    for p in pagar_pagas:
        cat = p.get("categoria_descricao") or "(sem categoria)"
        v = parse_money(p.get("valor"))
        cat_total[cat]["valor"] += v
        cat_total[cat]["count"] += 1
        pagas_total += v
    cat_sorted = sorted(cat_total.items(), key=lambda x: -x[1]["valor"])

    # 4) Top clientes — receber_recebidas agregado por contato_nome
    cli_total = defaultdict(lambda: {"valor": 0.0, "count": 0})
    receb_total = 0.0
    for r in receber_recebidas:
        nome = r.get("contato_nome") or "(sem cliente)"
        v = parse_money(r.get("valor"))
        cli_total[nome]["valor"] += v
        cli_total[nome]["count"] += 1
        receb_total += v
    cli_sorted = sorted(cli_total.items(), key=lambda x: -x[1]["valor"])

    return {
        "pagar": {
            "overdue": overdue,
            "upcoming": upcoming[:30],   # primeiros 30
            "bucket_overdue": bucket_overdue,
            "bucket_30": bucket_30,
            "bucket_60": bucket_60,
            "bucket_90": bucket_90,
            "total": bucket_total,
            "count": len(pagar_aberto),
        },
        "receber": {
            "overdue": receber_overdue,
            "rows": receber_rows[:30],
            "total": receber_total,
            "count": len(receber_aberto),
        },
        "gastos": {
            "by_cat": cat_sorted,
            "total": pagas_total,
            "count": len(pagar_pagas),
        },
        "clientes": {
            "by_cli": cli_sorted[:15],
            "total": receb_total,
            "count": len(receber_recebidas),
        },
    }


CSS = """
:root{
  --bg:#f4f6f9;--s1:#fff;--s2:#f0f3f7;--s3:#e4e9f0;
  --bd:rgba(60,90,130,0.1);--bd2:rgba(60,90,130,0.2);
  --t1:#1a2436;--t2:#4a5f7a;--t3:#8a9db8;
  --red:#c0392b;--red-bg:rgba(192,57,43,0.08);--red-br:rgba(192,57,43,0.2);
  --amber:#b7650a;--amber-bg:rgba(183,101,10,0.08);--amber-br:rgba(183,101,10,0.2);
  --green:#1e7e4f;--green-bg:rgba(30,126,79,0.08);--green-br:rgba(30,126,79,0.2);
  --blue:#1a5fa8;--blue-bg:rgba(26,95,168,0.08);--blue-br:rgba(26,95,168,0.2);
  --slate:#3d5a80;--slate-bg:rgba(61,90,128,0.08);
  --sans:'IBM Plex Sans',sans-serif;--mono:'IBM Plex Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t1);font-family:var(--sans);font-size:14px;line-height:1.6;min-height:100vh;padding:24px}
.wrap{max-width:1320px;margin:0 auto}
header{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px;padding-bottom:18px;border-bottom:1px solid var(--bd);margin-bottom:24px}
h1{font-size:22px;font-weight:600;letter-spacing:-.02em}
.sub{font-size:11px;color:var(--t3);font-family:var(--mono);margin-top:4px}
.tag{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:10px;font-weight:500;font-family:var(--mono);background:var(--blue-bg);color:var(--blue);border:1px solid var(--bd2)}
.tag::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--blue)}
.sl{font-family:var(--mono);font-size:9.5px;font-weight:500;color:var(--t3);text-transform:uppercase;letter-spacing:.12em;margin:24px 0 10px;display:flex;align-items:center;gap:8px}
.sl::after{content:'';flex:1;height:1px;background:var(--bd)}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:880px){.g4{grid-template-columns:repeat(2,1fr)}.g2{grid-template-columns:1fr}}
.met{background:var(--s1);border:1px solid var(--bd);border-radius:11px;padding:14px 16px;box-shadow:0 1px 3px rgba(60,90,130,0.06);position:relative;overflow:hidden}
.met::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px}
.met.red::after{background:var(--red)}.met.amber::after{background:var(--amber)}
.met.green::after{background:var(--green)}.met.blue::after{background:var(--blue)}
.ml{font-size:10px;font-weight:500;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px;font-family:var(--mono)}
.mv{font-family:var(--mono);font-size:20px;font-weight:500;letter-spacing:-.01em;line-height:1}
.mv.red{color:var(--red)}.mv.amber{color:var(--amber)}.mv.green{color:var(--green)}.mv.blue{color:var(--blue)}
.ms{font-size:10.5px;color:var(--t3);margin-top:4px}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:13px;padding:16px;box-shadow:0 1px 3px rgba(60,90,130,0.06)}
.ct{font-family:var(--mono);font-size:9.5px;font-weight:500;letter-spacing:.1em;color:var(--t3);text-transform:uppercase;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;gap:6px}
.ct b{color:var(--t1);font-weight:500}
.dt{width:100%;border-collapse:collapse;font-size:12px;table-layout:auto}
.dt th{font-family:var(--mono);font-size:9px;font-weight:500;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;padding:6px 8px;border-bottom:1px solid var(--bd);text-align:left}
.dt th.r{text-align:right}
.dt td{padding:7px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
.dt td.r{text-align:right;font-family:var(--mono)}
.dt td.mono{font-family:var(--mono);font-size:11px}
.dt tbody tr:hover{background:var(--s2)}
.tbl-wrap{overflow-x:auto}
.bar{height:6px;background:var(--s2);border-radius:3px;overflow:hidden;margin-top:3px}
.bar>span{display:block;height:100%;background:var(--blue);border-radius:3px}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10px;font-family:var(--mono);font-weight:500}
.pill.over{background:var(--red-bg);color:var(--red);border:1px solid var(--red-br)}
.pill.amber{background:var(--amber-bg);color:var(--amber);border:1px solid var(--amber-br)}
.pill.green{background:var(--green-bg);color:var(--green);border:1px solid var(--green-br)}
.tot{margin-top:10px;padding-top:10px;border-top:1px solid var(--bd2);font-family:var(--mono);font-size:11px;color:var(--t2);display:flex;justify-content:space-between}
.tot b{color:var(--t1);font-weight:500}
"""


def render(agg: Dict[str, Any], snapshot_path: Path, today: date) -> str:
    pagar = agg["pagar"]
    receber = agg["receber"]
    gastos = agg["gastos"]
    clientes = agg["clientes"]

    def esc(s: str) -> str:
        return html.escape(str(s or ""))

    # cards top
    metric_cards = []
    for label, valor, sub, color in [
        ("A pagar — vencidos", fmt_brl(pagar["bucket_overdue"]),
         f'{sum(1 for r in pagar["overdue"])} lançamentos', "red"),
        ("A pagar — 30 dias", fmt_brl(pagar["bucket_30"]),
         "vencimento próximo", "amber"),
        ("A receber — em aberto", fmt_brl(receber["total"]),
         f'{receber["count"]} parcelas', "blue"),
        ("Recebido (12m)", fmt_brl(clientes["total"]),
         f'{clientes["count"]} parcelas', "green"),
    ]:
        metric_cards.append(
            f'<div class="met {color}"><div class="ml">{esc(label)}</div>'
            f'<div class="mv {color}">{esc(valor)}</div>'
            f'<div class="ms">{esc(sub)}</div></div>'
        )

    # buckets pagar
    bucket_cards = "".join([
        f'<div class="met red"><div class="ml">Vencidos</div>'
        f'<div class="mv red">{fmt_brl(pagar["bucket_overdue"])}</div></div>',
        f'<div class="met amber"><div class="ml">≤ 30 dias</div>'
        f'<div class="mv amber">{fmt_brl(pagar["bucket_30"])}</div></div>',
        f'<div class="met blue"><div class="ml">31–60 dias</div>'
        f'<div class="mv blue">{fmt_brl(pagar["bucket_60"])}</div></div>',
        f'<div class="met blue"><div class="ml">61–90 dias</div>'
        f'<div class="mv blue">{fmt_brl(pagar["bucket_90"])}</div></div>',
    ])

    # tabelas
    def render_pagar_table(rows: List[Dict[str, Any]], hide_overdue: bool = False) -> str:
        body = []
        for r in rows:
            pill = ""
            if not hide_overdue and r["venc"] and r["venc"] < today:
                pill = '<span class="pill over">vencido</span>'
            body.append(
                f'<tr><td class="mono">{esc(r["venc_str"])} {pill}</td>'
                f'<td>{esc(r["fornecedor"])}<div style="font-size:10px;color:var(--t3)">{esc(r["categoria"])} · {esc(r["historico"])}</div></td>'
                f'<td class="r">{esc(r["valor_str"])}</td></tr>'
            )
        return "<tr><td colspan=3 style='color:var(--t3);text-align:center;padding:14px'>nenhum item</td></tr>" if not body else "".join(body)

    def render_receber_table(rows: List[Dict[str, Any]]) -> str:
        body = []
        for r in rows:
            pill = '<span class="pill over">vencido</span>' if r["venc"] and r["venc"] < today else ""
            body.append(
                f'<tr><td class="mono">{esc(r["venc_str"])} {pill}</td>'
                f'<td>{esc(r["cliente"])}</td>'
                f'<td class="r">{esc(r["valor_str"])}</td></tr>'
            )
        return "<tr><td colspan=3 style='color:var(--t3);text-align:center;padding:14px'>nenhum item</td></tr>" if not body else "".join(body)

    pagar_overdue_table = render_pagar_table(pagar["overdue"], hide_overdue=True)
    pagar_upcoming_table = render_pagar_table(pagar["upcoming"], hide_overdue=False)
    receber_overdue_table = render_receber_table(receber["overdue"])
    receber_upcoming_table = render_receber_table(receber["rows"])

    # gastos por categoria
    max_gasto = max((c[1]["valor"] for c in gastos["by_cat"]), default=1) or 1
    gasto_rows = []
    for cat, data in gastos["by_cat"]:
        pct = data["valor"] / max_gasto * 100 if max_gasto else 0
        gasto_rows.append(
            f'<tr><td>{esc(cat)}<div class="bar"><span style="width:{pct:.1f}%"></span></div></td>'
            f'<td class="r mono">{data["count"]}x</td>'
            f'<td class="r">{fmt_brl_full(data["valor"])}</td></tr>'
        )

    # top clientes
    max_cli = max((c[1]["valor"] for c in clientes["by_cli"]), default=1) or 1
    cli_rows = []
    for nome, data in clientes["by_cli"]:
        pct = data["valor"] / max_cli * 100 if max_cli else 0
        cli_rows.append(
            f'<tr><td>{esc(nome)}<div class="bar"><span style="width:{pct:.1f}%;background:var(--green)"></span></div></td>'
            f'<td class="r mono">{data["count"]}x</td>'
            f'<td class="r">{fmt_brl_full(data["valor"])}</td></tr>'
        )

    snapshot_label = snapshot_path.stem.replace("bling_data_", "")
    fetched_at = ""
    try:
        meta = json.loads(snapshot_path.read_text())
        fetched_at = meta.get("_metadata", {}).get("fetched_at", "")
    except Exception:
        pass

    html_out = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Easy Analytics — Live Bling</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Easy Analytics — Live Bling</h1>
      <div class="sub">Snapshot {esc(snapshot_label)} · gerado em {esc(fetched_at)}</div>
    </div>
    <span class="tag">Dados Bling API v3</span>
  </header>

  <div class="g4">{''.join(metric_cards)}</div>

  <div class="sl">Contas a pagar — janela de vencimento</div>
  <div class="g4">{bucket_cards}</div>

  <div class="g2">
    <div class="card">
      <div class="ct"><b>Vencidos a pagar</b><span>{len(pagar["overdue"])} itens · {fmt_brl(pagar["bucket_overdue"])}</span></div>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Vencimento</th><th>Fornecedor</th><th class="r">Valor</th></tr></thead>
        <tbody>{pagar_overdue_table}</tbody></table></div>
    </div>
    <div class="card">
      <div class="ct"><b>Próximos a pagar (30 itens)</b><span>{len(pagar["upcoming"])} itens · {fmt_brl(pagar["bucket_30"]+pagar["bucket_60"]+pagar["bucket_90"])} em 90d</span></div>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Vencimento</th><th>Fornecedor</th><th class="r">Valor</th></tr></thead>
        <tbody>{pagar_upcoming_table}</tbody></table></div>
    </div>
  </div>

  <div class="sl">Contas a receber</div>
  <div class="g2">
    <div class="card">
      <div class="ct"><b>Vencidos a receber</b><span>{len(receber["overdue"])} itens</span></div>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Vencimento</th><th>Cliente</th><th class="r">Valor</th></tr></thead>
        <tbody>{receber_overdue_table}</tbody></table></div>
    </div>
    <div class="card">
      <div class="ct"><b>A receber — em aberto</b><span>{len(receber["rows"])} itens</span></div>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Vencimento</th><th>Cliente</th><th class="r">Valor</th></tr></thead>
        <tbody>{receber_upcoming_table}</tbody></table></div>
    </div>
  </div>

  <div class="sl">Gastos por categoria · realizados</div>
  <div class="card">
    <div class="ct"><b>Categorias por valor</b><span>{gastos["count"]} pagamentos · {fmt_brl_full(gastos["total"])}</span></div>
    <div class="tbl-wrap"><table class="dt">
      <thead><tr><th>Categoria</th><th class="r"># pgts</th><th class="r">Total R$</th></tr></thead>
      <tbody>{''.join(gasto_rows) or '<tr><td colspan=3>sem dados</td></tr>'}</tbody></table></div>
  </div>

  <div class="sl">Top clientes pagantes</div>
  <div class="card">
    <div class="ct"><b>Clientes por receita recebida</b><span>{clientes["count"]} parcelas · {fmt_brl_full(clientes["total"])}</span></div>
    <div class="tbl-wrap"><table class="dt">
      <thead><tr><th>Cliente</th><th class="r"># parc.</th><th class="r">Total R$</th></tr></thead>
      <tbody>{''.join(cli_rows) or '<tr><td colspan=3>sem dados</td></tr>'}</tbody></table></div>
  </div>

  <div class="sl">Status</div>
  <div class="card" style="font-family:var(--mono);font-size:11px;color:var(--t2)">
    Snapshot: {esc(snapshot_path.name)}<br>
    Gerado em: {esc(fetched_at)}<br>
    Hoje: {today.isoformat()}<br>
    Total contas a pagar em aberto: {pagar["count"]} · {fmt_brl_full(pagar["total"])}<br>
    Pipeline pago (período do filtro): {gastos["count"]} pagamentos · {fmt_brl_full(gastos["total"])}<br>
    Para regerar: <code style="background:var(--s2);padding:2px 6px;border-radius:4px">python3 ~/Documents/Easy_Financas/build-html.py</code>
  </div>
</div>
</body>
</html>
"""
    return html_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", type=Path, default=None,
                    help="pasta com bling_data_*.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"arquivo de saida (default {DEFAULT_OUT})")
    args = ap.parse_args()

    in_dir = args.in_dir
    if in_dir is None:
        for c in DEFAULT_IN_CANDIDATES:
            if c.exists() and any(c.glob("bling_data_*.json")):
                in_dir = c
                break
    if in_dir is None:
        print("ERRO: nenhuma pasta com snapshot encontrada. Rode fetch-bling.py primeiro.", file=sys.stderr)
        return 1

    snapshot = find_latest_snapshot(in_dir)
    print(f"[ok] lendo {snapshot.name}")
    data = json.loads(snapshot.read_text())
    today = date.today()
    agg = aggregate(data, today)
    out_html = render(agg, snapshot, today)
    args.out.write_text(out_html, encoding="utf-8")
    print(f"[ok] -> {args.out} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
