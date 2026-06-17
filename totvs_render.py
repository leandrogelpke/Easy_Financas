#!/usr/bin/env python3
"""
totvs_render.py — gera os fragments HTML da aba "Comissões Totvs" para
integração no build-html.py / template.html do Easy Analytics.

Funções públicas:
    render_totvs(snap_path, bling_dir) -> dict:
        {
            "ntab":       <button class="ntab"...>,
            "mobtab":     <button class="mobtab"...>,
            "pg":         <div class="pg" id="pg-totvs">...</div>,
            "n_docs":     int,
            "total_real": float,
        }

Se o snapshot ainda não existir ou estiver vazio, retorna fragments
discretos (placeholder) que não quebram o dashboard.
"""
from __future__ import annotations
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

HOME = Path.home()
DEFAULT_SNAP = HOME / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api" / "totvs_snapshot.json"
DEFAULT_BLING_DIR = HOME / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api"


# ============ HELPERS DE FORMATAÇÃO ============
def fmt_brl(v, compact=False):
    if v is None:
        return "—"
    if compact:
        if abs(v) >= 1_000_000:
            return f"R$ {v/1_000_000:.2f}M".replace(".", ",")
        if abs(v) >= 1_000:
            return f"R$ {v/1_000:.0f}K"
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.1f}%".replace(",", "X").replace(".", ",").replace("X", ".")


_MESES = ["jan", "fev", "mar", "abr", "mai", "jun",
          "jul", "ago", "set", "out", "nov", "dez"]


def fmt_comp(c):
    if not c:
        return "—"
    try:
        y, m, _ = c.split("-")
        return f"{_MESES[int(m)-1]}/{y[2:]}"
    except Exception:
        return c


def fmt_short_filial(razao):
    r = (razao or "").strip().upper()
    if "MATRIZ" in r:
        return "MATRIZ"
    if "BELO HORIZONTE" in r or "(BH)" in r:
        return "BH"
    if "SANTA CATARINA" in r or "(SC)" in r:
        return "SC"
    if "LARGE ENTERPRISE" in r:
        return "LARGE"
    return (razao or "")[:18]


def br_to_float(s):
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def _next_month(c):
    try:
        y, m, _ = c.split("-"); y = int(y); m = int(m) + 1
        if m > 12:
            m = 1; y += 1
        return f"{y:04d}-{m:02d}"
    except Exception:
        return None


# ============ SVG MINI-CHARTS ============
def _line_chart_svg(data, w=620, h=180, color="#1e7e4f"):
    if not data:
        return ""
    vals = [v for _, v in data]
    mx, mn = max(vals), min(vals)
    rng = (mx - mn) or 1
    n = len(data)
    pad_l = 56; pad_b = 24; pad_t = 16
    inner_h = h - pad_t - pad_b
    step = (w - pad_l - 15) / max(n - 1, 1)
    pts = []
    for i, (_, v) in enumerate(data):
        x = pad_l + i * step
        y = pad_t + (1 - (v - mn) / rng) * inner_h
        pts.append((x, y))
    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="display:block;width:100%;height:auto;max-height:{h}px">']
    for i in range(3):
        y = pad_t + inner_h * (1 - i / 2)
        parts.append(f'<line x1="{pad_l}" y1="{y:.0f}" x2="{w-5}" y2="{y:.0f}" stroke="#e4e9f0" stroke-width="0.5"/>')
        ref = mn + (mx - mn) * i / 2
        parts.append(f'<text x="{pad_l-4}" y="{y+3:.0f}" font-family="IBM Plex Mono,monospace" font-size="9" fill="#8a9db8" text-anchor="end">{ref/1000:.0f}K</text>')
    area = f"M{pts[0][0]:.1f},{pad_t+inner_h:.1f} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{pad_t+inner_h:.1f} Z"
    parts.append(f'<path d="{area}" fill="{color}" opacity="0.13"/>')
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    parts.append(f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
    for i, ((lbl, v), (x, y)) in enumerate(zip(data, pts)):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#fff" stroke="{color}" stroke-width="1.8"/>')
        parts.append(f'<text x="{x:.1f}" y="{y-9:.0f}" font-family="IBM Plex Mono,monospace" font-size="9.5" fill="#1a2436" text-anchor="middle" font-weight="500">{v/1000:.1f}K</text>')
        parts.append(f'<text x="{x:.1f}" y="{h-7:.0f}" font-family="IBM Plex Mono,monospace" font-size="9.5" fill="#4a5f7a" text-anchor="middle">{lbl}</text>')
    parts.append('</svg>')
    return "".join(parts)


# ============ CARREGA DADOS ============
def _load_snapshot(snap_path: Path) -> dict:
    if not snap_path.exists():
        return {"documentos": [], "_meta": {}}
    try:
        d = json.loads(snap_path.read_text(encoding="utf-8"))
        if "documentos" not in d:
            d["documentos"] = []
        return d
    except Exception:
        return {"documentos": [], "_meta": {}}


def _load_bling_receber(bling_dir: Path) -> defaultdict:
    """Indexa contas a receber por (cnpj, year-month da emissão/vencimento)."""
    idx = defaultdict(list)
    if not bling_dir.exists():
        return idx
    for p in bling_dir.glob("contas_receber_*.csv"):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh, delimiter=";"):
                    cnpj = (r.get("contato_documento") or "").strip()
                    emi = r.get("data_emissao") or r.get("vencimento") or ""
                    ym = emi[:7] if emi else ""
                    if cnpj and ym:
                        idx[(cnpj, ym)].append(r)
        except Exception:
            continue
    return idx


# ============ AGREGAÇÕES ============
def _aggregate(docs):
    docs = sorted(docs, key=lambda d: (d.get("competencia") or "",
                                       {"BASE": 0, "COMPLEMENTAR": 1, "REAL": 2, "PROVISAO": 3}.get(d.get("tipo"), 9)))
    real_por_comp = defaultdict(float)
    real_docs_por_comp = defaultdict(list)
    # Realizado = BASE + COMPLEMENTAR (relatórios mensais) ou REAL (faturamento histórico).
    # O dedup do fetch-totvs.py garante que não há overlap (REAL é dropped onde BASE/COMP existe).
    for d in docs:
        if d.get("tipo") in ("BASE", "COMPLEMENTAR", "REAL") and d.get("competencia") and d.get("total_geral"):
            real_por_comp[d["competencia"]] += d["total_geral"]
            real_docs_por_comp[d["competencia"]].append(d)

    provisao_docs = [d for d in docs if d.get("tipo") == "PROVISAO"]
    provisao_total = sum((d.get("total_geral") or 0) for d in provisao_docs)
    provisao_comps = sorted({d.get("competencia") for d in provisao_docs if d.get("competencia")})

    real_acumulado = sum(real_por_comp.values())
    n_comps_real = len(real_por_comp)
    media_mensal = real_acumulado / n_comps_real if n_comps_real else 0
    comps_sorted = sorted(real_por_comp.keys())
    ultima_comp = comps_sorted[-1] if comps_sorted else None
    penult_comp = comps_sorted[-2] if len(comps_sorted) >= 2 else None
    delta_pct = None
    if ultima_comp and penult_comp and real_por_comp[penult_comp]:
        delta_pct = (real_por_comp[ultima_comp] - real_por_comp[penult_comp]) / real_por_comp[penult_comp] * 100

    cli_acc = defaultdict(float)
    cli_meta = {}
    for d in docs:
        if d.get("tipo") == "PROVISAO":
            continue
        for c in d.get("clientes", []):
            nm = (c.get("cliente") or "").strip() or "(sem nome)"
            cli_acc[nm] += (c.get("valor_comissao") or 0)
            if nm not in cli_meta:
                cli_meta[nm] = {"cod": c.get("cod_cliente"), "produtos": set()}
            if c.get("produto_desc"):
                cli_meta[nm]["produtos"].add(c["produto_desc"])
    top_clientes = sorted(cli_acc.items(), key=lambda x: -x[1])[:8]

    prod_acc = defaultdict(float)
    prod_n_clientes = defaultdict(set)
    for d in docs:
        if d.get("tipo") == "PROVISAO":
            continue
        for c in d.get("clientes", []):
            p = (c.get("produto_desc") or "").strip() or "(s/ desc)"
            prod_acc[p] += (c.get("valor_comissao") or 0)
            prod_n_clientes[p].add((c.get("cod_cliente") or c.get("cliente")))
    top_produtos = sorted(prod_acc.items(), key=lambda x: -x[1])[:6]

    filial_acc = defaultdict(float)
    filial_meta = {}
    filial_mensal = defaultdict(lambda: defaultdict(float))
    filial_mensal_tipo = defaultdict(lambda: defaultdict(list))
    filial_provisao = defaultdict(float)
    for d in docs:
        comp = d.get("competencia")
        for f in d.get("cnpjs_filial", []):
            cnpj = f["cnpj"]
            if cnpj not in filial_meta:
                filial_meta[cnpj] = f["razao_social"]
            if d.get("tipo") == "PROVISAO":
                filial_provisao[cnpj] += f["total"]
                continue
            filial_acc[cnpj] += f["total"]
            if comp:
                filial_mensal[cnpj][comp] += f["total"]
                filial_mensal_tipo[cnpj][comp].append(d.get("tipo"))
    filial_sorted = sorted(filial_acc.items(), key=lambda x: -x[1])

    return {
        "docs": docs,
        "real_por_comp": dict(real_por_comp),
        "real_docs_por_comp": dict(real_docs_por_comp),
        "provisao_docs": provisao_docs,
        "provisao_total": provisao_total,
        "provisao_comps": provisao_comps,
        "real_acumulado": real_acumulado,
        "n_comps_real": n_comps_real,
        "media_mensal": media_mensal,
        "comps_sorted": comps_sorted,
        "ultima_comp": ultima_comp,
        "penult_comp": penult_comp,
        "delta_pct": delta_pct,
        "top_clientes": top_clientes,
        "cli_meta": cli_meta,
        "top_produtos": top_produtos,
        "prod_n_clientes": prod_n_clientes,
        "filial_acc": filial_acc,
        "filial_meta": filial_meta,
        "filial_sorted": filial_sorted,
        "filial_mensal": dict(filial_mensal),
        "filial_mensal_tipo": dict(filial_mensal_tipo),
        "filial_provisao": filial_provisao,
    }


def _reconcile(agg, idx_bling):
    RETENCAO_LIMITE = 0.07
    rows = []
    for d in agg["docs"]:
        # Não reconcilia PROVISAO (futuro) nem REAL (faturamento histórico —
        # NFs Bling cobrem só janela recente, geraria muitos falsos negativos)
        if d.get("tipo") in ("PROVISAO", "REAL"):
            continue
        comp = d.get("competencia")
        if not comp:
            continue
        ym_nf = _next_month(comp)
        for f in d.get("cnpjs_filial", []):
            cnpj = f["cnpj"]
            total_totvs = f["total"]
            matches = idx_bling.get((cnpj, ym_nf), [])
            sum_bling = round(sum(br_to_float(r.get("valor", "0")) for r in matches), 2)
            diff = round(total_totvs - sum_bling, 2)
            if matches and abs(diff) / total_totvs < 0.01:
                status = "OK"; tone = "green"
            elif matches and 0 <= diff / total_totvs <= RETENCAO_LIMITE:
                status = "OK (retenção)"; tone = "green"
            elif sum_bling == 0:
                status = "Sem NF Bling"; tone = "red"
            elif sum_bling > total_totvs * 1.5:
                status = "NF inclui outros"; tone = "amber"
            else:
                status = "Divergente"; tone = "amber"
            rows.append({
                "comp": comp, "tipo": d["tipo"], "filial": f["razao_social"], "cnpj": cnpj,
                "ym_nf": ym_nf, "totvs": total_totvs, "bling": sum_bling, "diff": diff,
                "n_nfs": len(matches), "status": status, "tone": tone,
            })
    return rows, Counter([r["tone"] for r in rows])


# ============ RENDER FRAGMENTS ============
def _render_diretoria(agg, reconc_rows, reconc_summary):
    p = []
    real_acumulado = agg["real_acumulado"]; n_comps_real = agg["n_comps_real"]
    ultima_comp = agg["ultima_comp"]; penult_comp = agg["penult_comp"]; delta_pct = agg["delta_pct"]
    provisao_total = agg["provisao_total"]; provisao_comps = agg["provisao_comps"]
    real_por_comp = agg["real_por_comp"]; comps_sorted = agg["comps_sorted"]
    media_mensal = agg["media_mensal"]
    filial_meta = agg["filial_meta"]; filial_sorted = agg["filial_sorted"]
    filial_mensal = agg["filial_mensal"]; filial_mensal_tipo = agg["filial_mensal_tipo"]
    filial_provisao = agg["filial_provisao"]
    top_clientes = agg["top_clientes"]; cli_meta = agg["cli_meta"]
    top_produtos = agg["top_produtos"]; prod_n_clientes = agg["prod_n_clientes"]

    ok = reconc_summary.get("green", 0); warn = reconc_summary.get("amber", 0); err = reconc_summary.get("red", 0)
    tone_recon = "green" if err == 0 and warn <= 2 else ("amber" if err <= 1 else "red")

    p.append('<div class="sl">KPIs executivos · Comissões Totvs (ASE806)</div>')
    p.append('<div class="g4">')
    p.append(f'<div class="met green"><div class="ml">Receita real acumulada</div><div class="mv green">{fmt_brl(real_acumulado, True)}</div><div class="ms">{n_comps_real} competências · Base + Complementar</div></div>')
    delta_str = ""
    if delta_pct is not None:
        arrow = "▲" if delta_pct >= 0 else "▼"
        delta_str = f"{arrow} {fmt_pct(delta_pct)} vs {fmt_comp(penult_comp)}"
    p.append(f'<div class="met blue"><div class="ml">Última competência · {fmt_comp(ultima_comp)}</div><div class="mv blue">{fmt_brl(real_por_comp.get(ultima_comp, 0), True) if ultima_comp else "—"}</div><div class="ms">{delta_str}</div></div>')
    p.append(f'<div class="met amber"><div class="ml">Provisão (forecast)</div><div class="mv amber">{fmt_brl(provisao_total, True)}</div><div class="ms">{", ".join(fmt_comp(c) for c in provisao_comps) or "—"} · não somar à receita</div></div>')
    p.append(f'<div class="met {tone_recon}"><div class="ml">Reconciliação Bling</div><div class="mv {tone_recon}">{ok} OK</div><div class="ms">{warn} divergentes · {err} sem NF</div></div>')
    p.append('</div>')

    # ── De-para: faturado bruto × líquido recebido ──────────────────
    # Esclarece por que o caixa (Bling) recebe MENOS que a comissão bruta:
    # a Totvs retém 6,15% na fonte (PIS+COFINS+IRRF+CSLL). Alíquota vem do
    # audit.CONFIG (fonte única). O líquido ainda pode diferir do caixa por
    # notas emitidas mas não recebidas no mês.
    try:
        from audit import CONFIG as _CFG  # type: ignore
        _ret_fonte = _CFG["ret_pis_cofins_csll_aliq"] + _CFG["ret_irrf_pj_aliq"]
    except Exception:
        _ret_fonte = 0.0615
    if ultima_comp:
        _bruto = real_por_comp.get(ultima_comp, 0) or 0
        _ret = _bruto * _ret_fonte
        _liq = _bruto - _ret
        p.append(f'<div class="sl">Faturado × recebido · {fmt_comp(ultima_comp)} — por que o caixa recebe menos</div>')
        p.append('<div class="g3">')
        p.append(f'<div class="met blue"><div class="ml">Faturado bruto (comissão)</div><div class="mv blue">{fmt_brl(_bruto, True)}</div><div class="ms">NFS-e emitidas · competência</div></div>')
        p.append(f'<div class="met amber"><div class="ml">Retenção na fonte ({_ret_fonte*100:.2f}%)</div><div class="mv amber">− {fmt_brl(_ret, True)}</div><div class="ms">PIS+COFINS+IRRF+CSLL · crédito que abate tributos</div></div>')
        p.append(f'<div class="met green"><div class="ml">Líquido a receber</div><div class="mv green">{fmt_brl(_liq, True)}</div><div class="ms">o que cai no caixa (Bling) · menos notas ainda não recebidas no mês</div></div>')
        p.append('</div>')

    p.append('<div class="sl">Tendência mensal · Mix por filial</div>')
    p.append('<div class="g2">')
    p.append('<div class="card"><div class="ct"><b>Comissão por competência</b><span>Real (Base + Complementar)</span></div>')
    p.append(_line_chart_svg([(fmt_comp(c), real_por_comp[c]) for c in comps_sorted], w=620, h=180))
    p.append(f'<div class="tot"><span>Média mensal</span><b>{fmt_brl(media_mensal)}</b></div>')
    p.append('</div>')

    p.append(f'<div class="card"><div class="ct"><b>Mix por filial Totvs</b><span>Acumulado · {len(filial_sorted)} filiais · clique para abrir mensal</span></div>')
    p.append('<table class="dt mix-tbl"><thead><tr><th></th><th>Filial</th><th class="r">Acumulado</th><th class="r">Mix</th></tr></thead><tbody>')
    total_filial = sum(v for _, v in filial_sorted) or 1
    comps_all = sorted(set().union(*[set(filial_mensal.get(c, {}).keys()) for c, _ in filial_sorted]) or [])
    for cnpj, v in filial_sorted:
        pct = v / total_filial * 100
        p.append(f'<tr class="mix-row" data-cnpj="{cnpj}" onclick="toggleFilial(\'{cnpj}\')" style="cursor:pointer">')
        p.append(f'<td style="width:22px;text-align:center;color:var(--t3);font-family:var(--mono);font-size:11px;user-select:none" id="ic-{cnpj}">▸</td>')
        p.append(f'<td>{escape(filial_meta[cnpj])}<div style="font-size:10px;color:var(--t3);font-family:var(--mono)">{cnpj}</div></td>')
        p.append(f'<td class="r">{fmt_brl(v)}</td>')
        p.append(f'<td class="r" style="min-width:130px"><div style="display:flex;align-items:center;gap:6px;justify-content:flex-end"><span class="pill pg2">{pct:.1f}%</span><div class="bar" style="width:60px"><span style="width:{pct:.0f}%"></span></div></div></td>')
        p.append('</tr>')
        mensal = filial_mensal.get(cnpj, {})
        max_m = max(mensal.values()) if mensal else 1
        media_filial = (sum(mensal.values()) / len(mensal)) if mensal else 0
        prov = filial_provisao.get(cnpj, 0)
        p.append(f'<tr class="mix-detail" id="d-{cnpj}" style="display:none;background:var(--s2)"><td colspan="4" style="padding:14px 16px">')
        p.append(f'<div style="font-family:var(--mono);font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Histórico mensal · {len(mensal)} competências · média {fmt_brl(media_filial)}</div>')
        for comp in comps_all:
            val = mensal.get(comp, 0)
            tipos = filial_mensal_tipo.get(cnpj, {}).get(comp, [])
            tipo_pills = ""
            for t in tipos:
                tone = {"BASE": "pg2", "COMPLEMENTAR": "pa", "REAL": "pb"}.get(t, "pb2")
                lbl = {"BASE": "B", "COMPLEMENTAR": "C", "REAL": "R"}.get(t, t[:1])
                tipo_pills += f' <span class="pill {tone}" style="font-size:9px;padding:0 5px">{lbl}</span>'
            bar_pct = (val / max_m * 100) if max_m else 0
            vs_media = val / media_filial if media_filial else 1
            arrow = '▲' if vs_media > 1.05 else ('▼' if vs_media < 0.95 else '')
            arrow_color = "var(--green)" if arrow == '▲' else ("var(--red)" if arrow == '▼' else "var(--t3)")
            p.append('<div style="display:grid;grid-template-columns:55px 1fr 110px 60px;gap:10px;align-items:center;padding:5px 0;font-size:11.5px">')
            p.append(f'<div style="font-family:var(--mono);color:var(--t2);font-size:11px">{fmt_comp(comp)}{tipo_pills}</div>')
            p.append(f'<div class="bar" style="height:8px;background:var(--s3)"><span style="width:{bar_pct:.0f}%;background:var(--blue)"></span></div>')
            p.append(f'<div style="font-family:var(--mono);text-align:right;color:var(--t1);font-weight:500">{fmt_brl(val)}</div>')
            if arrow:
                p.append(f'<div style="font-family:var(--mono);text-align:right;color:{arrow_color};font-size:10px">{arrow} {(vs_media*100-100):+.0f}%</div>')
            else:
                p.append(f'<div style="font-family:var(--mono);text-align:right;color:var(--t3);font-size:10px">{(vs_media*100-100):+.0f}%</div>')
            p.append('</div>')
        if prov > 0:
            p.append(f'<div style="margin-top:10px;padding:8px 10px;background:var(--amber-bg);border:1px solid var(--amber-br);border-radius:6px;font-size:11px;color:var(--amber);font-family:var(--mono);display:flex;justify-content:space-between"><span>+ Provisão (forecast, não somada)</span><b>{fmt_brl(prov)}</b></div>')
        p.append('<div style="margin-top:10px;font-size:10px;color:var(--t3);font-family:var(--mono)">Legenda: <span class="pill pg2" style="font-size:9px;padding:0 5px">B</span> Base · <span class="pill pa" style="font-size:9px;padding:0 5px">C</span> Complementar · Δ vs média da filial</div>')
        p.append('</td></tr>')
    p.append('</tbody></table>')
    p.append('<script>function toggleFilial(c){var d=document.getElementById("d-"+c),i=document.getElementById("ic-"+c);if(d.style.display==="none"){d.style.display="table-row";i.textContent="▾";}else{d.style.display="none";i.textContent="▸";}}</script>')
    p.append('</div></div>')

    p.append('<div class="sl">Top clientes finais · Top produtos</div>')
    p.append('<div class="g2">')
    p.append('<div class="card"><div class="ct"><b>Top clientes finais</b><span>Comissão acumulada</span></div>')
    p.append('<table class="dt"><thead><tr><th>#</th><th>Cliente final</th><th class="r">Acumulado</th></tr></thead><tbody>')
    max_cli = top_clientes[0][1] if top_clientes else 1
    for i, (nm, v) in enumerate(top_clientes, 1):
        prods = list(cli_meta.get(nm, {}).get("produtos", []))
        prod_str = (prods[0] if prods else "")
        if len(prods) > 1:
            prod_str += f" +{len(prods)-1}"
        bar_pct = v / max_cli * 100 if max_cli else 0
        p.append(f'<tr><td class="mono" style="color:var(--t3)">{i:02d}</td><td>{escape(nm[:46])}<div style="font-size:10px;color:var(--t3)">{escape(prod_str[:54])}</div></td><td class="r" style="min-width:160px"><div style="display:flex;align-items:center;gap:8px;justify-content:flex-end">{fmt_brl(v)}<div class="bar" style="width:60px"><span style="width:{bar_pct:.0f}%"></span></div></div></td></tr>')
    p.append('</tbody></table></div>')

    p.append('<div class="card"><div class="ct"><b>Top produtos</b><span>Comissão acumulada</span></div>')
    p.append('<table class="dt"><thead><tr><th>Produto</th><th class="r">Clientes</th><th class="r">Acumulado</th></tr></thead><tbody>')
    for prod, v in top_produtos:
        nc = len(prod_n_clientes[prod])
        p.append(f'<tr><td>{escape(prod[:48])}</td><td class="r mono">{nc}</td><td class="r">{fmt_brl(v)}</td></tr>')
    p.append('</tbody></table></div></div>')

    p.append('<div class="sl">Semáforo de reconciliação · NF Easy × Comissão Totvs</div>')
    p.append(f'<div class="card"><div class="ct"><b>Status por competência × filial</b><span>{len(reconc_rows)} pares · regra: ≤7% retenção tributária = OK</span></div>')
    p.append('<div style="display:grid;grid-template-columns:auto repeat(auto-fit,minmax(95px,1fr));gap:6px;font-family:var(--mono);font-size:11px;overflow-x:auto">')
    p.append('<div style="color:var(--t3);font-size:10px">FILIAL ↓ / COMP →</div>')
    comps_for_grid = sorted({r["comp"] for r in reconc_rows})
    for c in comps_for_grid:
        p.append(f'<div style="color:var(--t3);font-size:10px;text-align:center">{fmt_comp(c)}</div>')
    by_pair = {(r["cnpj"], r["comp"]): r for r in reconc_rows}
    for cnpj, _ in filial_sorted:
        p.append(f'<div style="font-weight:500;font-size:11px">{fmt_short_filial(filial_meta[cnpj])}</div>')
        for c in comps_for_grid:
            r = by_pair.get((cnpj, c))
            if not r:
                p.append('<div style="background:var(--s2);border-radius:6px;padding:8px;text-align:center;color:var(--t3);font-size:10px">—</div>')
            else:
                p.append(f'<div style="background:var(--{r["tone"]}-bg);border:1px solid var(--{r["tone"]}-br);border-radius:6px;padding:6px 8px;text-align:center"><div style="font-size:10px;color:var(--{r["tone"]});font-weight:500">{escape(r["status"][:14])}</div><div style="font-size:10px;color:var(--t2);margin-top:2px">{fmt_brl(r["totvs"], True)}</div></div>')
    p.append('</div>')
    p.append('<div style="margin-top:14px;padding:10px 12px;background:var(--s2);border-radius:8px;font-size:11px;color:var(--t2);line-height:1.5"><b>Como ler:</b> verde = NF Easy bate (com até 7% de retenção tributária esperada). Amarelo = NF maior que comissão (provavelmente cobre outros serviços, caso típico da Matriz Totvs). Vermelho = não há NF correspondente no mês esperado.</div>')
    p.append('</div>')

    return "".join(p)


def _render_operacional(agg, reconc_rows, reconc_summary):
    p = []
    docs = agg["docs"]; n_comps_real = agg["n_comps_real"]
    real_acumulado = agg["real_acumulado"]; provisao_total = agg["provisao_total"]
    real_docs_por_comp = agg["real_docs_por_comp"]
    ultima_comp = agg["ultima_comp"]; filial_meta = agg["filial_meta"]

    p.append('<div class="sl">Documentos processados</div>')
    p.append(f'<div class="card" style="margin-bottom:14px"><div class="ct"><b>Recebidos da Totvs (ASE806)</b><span>{len(docs)} documentos · {n_comps_real} competências reais · provisão à parte</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Competência</th><th>Tipo</th><th class="r">Filiais</th><th class="r">Total geral</th><th class="r">Linhas</th><th class="r">Clientes</th><th>Recebido em</th><th>Arquivo</th></tr></thead><tbody>')
    for d in docs:
        tipo_pill = {"BASE": "<span class='pill pg2'>BASE</span>",
                     "COMPLEMENTAR": "<span class='pill pa'>COMPLEMENTAR</span>",
                     "REAL": "<span class='pill pb'>REAL</span>",
                     "PROVISAO": "<span class='pill pr'>PROVISÃO</span>"}.get(d.get("tipo"), "")
        n_cli = len({c.get("cliente") for c in d.get("clientes", []) if c.get("cliente")})
        p.append(f'<tr><td class="mono">{fmt_comp(d.get("competencia"))}</td><td>{tipo_pill}</td><td class="r mono" style="font-size:10px">{len(d.get("cnpjs_filial",[]))}</td><td class="r"><b>{fmt_brl(d.get("total_geral"))}</b></td><td class="r mono">{d.get("n_linhas")}</td><td class="r mono">{n_cli}</td><td class="mono" style="font-size:10px">{d.get("source_email_date") or "—"}</td><td style="font-size:10px;color:var(--t3)">{escape((d.get("source_file") or "")[:50])}…</td></tr>')
    p.append('</tbody></table></div>')
    p.append(f'<div class="tot"><span>Real (Base + Complementar)</span><b>{fmt_brl(real_acumulado)}</b></div>')
    p.append(f'<div class="tot" style="border:0;padding-top:4px"><span>Provisão (forecast)</span><b>{fmt_brl(provisao_total)}</b></div>')
    p.append('</div>')

    p.append('<div class="sl">Reconciliação detalhada · Totvs × Bling</div>')
    p.append('<div class="card" style="margin-bottom:14px"><div class="ct"><b>Total Totvs por filial × NFs Bling no mês seguinte</b><span>Mês NF Bling = competência + 1</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Comp.</th><th>Tipo</th><th>Filial Totvs</th><th class="r">Totvs</th><th class="r">Bling</th><th class="r">Δ R$</th><th class="r">Δ %</th><th class="r">Nº NF</th><th>Status</th></tr></thead><tbody>')
    for r in reconc_rows:
        dpct = (r["diff"] / r["totvs"] * 100) if r["totvs"] else 0
        pill_class = {"green": "pg2", "amber": "pa", "red": "pr"}[r["tone"]]
        diff_color = "var(--green)" if r["diff"] <= 0 else "var(--amber)"
        p.append(f'<tr><td class="mono">{fmt_comp(r["comp"])}</td><td style="font-size:10px;color:var(--t3);font-family:var(--mono)">{r["tipo"]}</td><td>{escape(r["filial"])}<div style="font-size:10px;color:var(--t3);font-family:var(--mono)">{r["cnpj"]}</div></td><td class="r">{fmt_brl(r["totvs"])}</td><td class="r">{fmt_brl(r["bling"])}</td><td class="r" style="color:{diff_color}">{fmt_brl(r["diff"])}</td><td class="r mono" style="color:var(--t3);font-size:11px">{dpct:+.1f}%</td><td class="r mono">{r["n_nfs"]}</td><td><span class="pill {pill_class}">{escape(r["status"])}</span></td></tr>')
    p.append('</tbody></table></div>')
    ok = reconc_summary.get("green", 0); warn = reconc_summary.get("amber", 0); err = reconc_summary.get("red", 0)
    p.append(f'<div class="tot"><span>Resumo</span><b>{ok} OK · {warn} divergentes · {err} sem NF</b></div>')
    p.append('<div style="margin-top:10px;padding:10px 12px;background:var(--s2);border-radius:8px;font-size:11px;color:var(--t2);line-height:1.5"><b>Critérios:</b><br>• <span class="pill pg2">OK</span> Bling = Totvs (até 7% de diferença = retenção tributária esperada na NF)<br>• <span class="pill pa">NF inclui outros</span> Bling &gt; 1,5× Totvs (NF cobre receitas além da comissão — caso típico da Matriz)<br>• <span class="pill pa">Divergente</span> Bling &lt; Totvs (faltou faturar? investigar)<br>• <span class="pill pr">Sem NF Bling</span> Nenhuma NF no mês esperado (Bling antigo? NF atrasada?)</div>')
    p.append('</div>')

    if ultima_comp:
        ud_list = real_docs_por_comp.get(ultima_comp, [])
        for ud in ud_list:
            p.append(f'<div class="sl">Detalhe — {fmt_comp(ultima_comp)} · {ud.get("tipo")}</div>')
            p.append(f'<div class="card" style="margin-bottom:14px"><div class="ct"><b>Abertura por cliente final</b><span>{len(ud.get("clientes",[]))} linhas · {fmt_brl(ud.get("total_geral"))} total</span></div>')
            p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Filial</th><th>Cliente final</th><th>Produto</th><th class="r">Bruto NF Totvs</th><th class="r">% Com.</th><th class="r">Comissão</th><th>NF Totvs</th><th>Emissão</th><th>Contrato</th></tr></thead><tbody>')
            for c in sorted(ud.get("clientes", []), key=lambda x: -(x.get("valor_comissao") or 0)):
                p.append(f'<tr><td class="mono" style="font-size:10px">{fmt_short_filial(filial_meta.get(c.get("cnpj_filial"),""))}</td><td>{escape((c.get("cliente") or "")[:42])}<div style="font-size:10px;color:var(--t3)">cod {escape(c.get("cod_cliente") or "")}</div></td><td style="font-size:11px">{escape((c.get("produto_desc") or "")[:30])}</td><td class="r">{fmt_brl(c.get("valor_bruto_item"))}</td><td class="r mono">{(c.get("perc_total") or 0):.0f}%</td><td class="r"><b>{fmt_brl(c.get("valor_comissao"))}</b></td><td class="mono" style="font-size:10px">{escape(c.get("nota_fiscal") or "")}</td><td class="mono" style="font-size:10px;color:var(--t3)">{(c.get("emissao_nf") or "—")[:10]}</td><td class="mono" style="font-size:10px;color:var(--t3)">{escape((c.get("contrato") or "")[:12])}</td></tr>')
            p.append('</tbody></table></div>')
            p.append('</div>')
            break
    return "".join(p)


# ============ COMPARATIVO ANUAL ============
def _render_yoy(docs):
    """Renderiza comparativo entre anos: totais anuais, mensais por ano, top clientes/filiais por ano."""
    from collections import defaultdict
    by_year_month: Dict[str, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
    by_year_total: Dict[str, float] = defaultdict(float)
    by_year_filial: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_year_cliente: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_year_n_clientes: Dict[str, set] = defaultdict(set)
    filial_meta = {}
    for d in docs:
        if d.get("tipo") == "PROVISAO":
            continue
        comp = d.get("competencia") or ""
        if len(comp) < 7:
            continue
        year = comp[:4]
        month = int(comp[5:7]) if comp[5:7].isdigit() else None
        total = d.get("total_geral") or 0
        by_year_total[year] += total
        if month:
            by_year_month[year][month] += total
        for f in d.get("cnpjs_filial", []):
            by_year_filial[year][f["cnpj"]] += f["total"]
            if f["cnpj"] not in filial_meta:
                filial_meta[f["cnpj"]] = f["razao_social"]
        for c in d.get("clientes", []):
            nm = (c.get("cliente") or "").strip() or "(sem nome)"
            by_year_cliente[year][nm] += (c.get("valor_comissao") or 0)
            by_year_n_clientes[year].add(nm)

    years = sorted(by_year_total.keys())
    if not years:
        return '<div class="card" style="padding:24px;text-align:center;color:var(--t3)">Sem dados anuais para comparar.</div>'

    _MES_ABRV = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]

    p = []
    # --- KPIs por ano (cards lado-a-lado) — Δ% normalizado YTD vs YTD ---
    p.append('<div class="sl">Receita realizada por ano</div>')
    n_yrs = len(years)
    cls = "g4" if n_yrs <= 4 else "g4"
    p.append(f'<div class="{cls}">')
    prev_year = None
    for y in years:
        total = by_year_total[y]
        n_clientes = len(by_year_n_clientes[y])
        meses_presentes = sorted([m for m, v in by_year_month[y].items() if v > 0])
        n_meses = len(meses_presentes)
        # Detectar gap (meses ausentes no range mínimo–máximo do ano)
        gap_str = ""
        if meses_presentes and n_meses > 1:
            faltando = [m for m in range(meses_presentes[0], meses_presentes[-1]+1)
                        if m not in meses_presentes]
            if faltando:
                gap_str = " · gap " + ", ".join(_MES_ABRV[m-1] for m in faltando)

        delta_str = ""
        delta_tone = "blue"
        if prev_year is not None:
            # Δ% YTD vs YTD — restringir ano anterior aos meses presentes em y
            prev_total_ytd = sum(by_year_month[prev_year].get(m, 0) for m in meses_presentes)
            cur_total_ytd = sum(by_year_month[y].get(m, 0) for m in meses_presentes)
            # se o ano anterior é completo (12m) e o atual é parcial, mostrar nominal + YTD
            prev_meses = sorted([m for m, v in by_year_month[prev_year].items() if v > 0])
            same_window = (set(prev_meses) == set(meses_presentes))
            if prev_total_ytd > 0:
                d_pct = (cur_total_ytd - prev_total_ytd) / prev_total_ytd * 100
                arrow = "▲" if d_pct >= 0 else "▼"
                if same_window:
                    delta_str = f"{arrow} {fmt_pct(d_pct)} vs {prev_year}"
                else:
                    delta_str = f"{arrow} {fmt_pct(d_pct)} vs {prev_year} (YTD {n_meses}m)"
                delta_tone = "green" if d_pct >= 0 else "amber"
        ms_text = f"{n_meses}m · {n_clientes} clientes{gap_str}{(' · ' + delta_str) if delta_str else ''}"
        p.append(f'<div class="met {delta_tone}"><div class="ml">{y}</div><div class="mv {delta_tone}">{fmt_brl(total, True)}</div><div class="ms">{ms_text}</div></div>')
        prev_year = y
    p.append('</div>')

    # --- Tabela mensal por ano (heatmap-like) ---
    p.append('<div class="sl">Mensal por ano · sazonalidade</div>')
    p.append('<div class="card"><div class="ct"><b>Comissão por mês × ano</b><span>Total mensal · cor mais escura = mais alto</span></div>')
    # global max para colorir
    global_max = max((max(by_year_month[y].values()) for y in years if by_year_month[y]), default=1) or 1
    p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Mês</th>')
    for y in years:
        p.append(f'<th class="r">{y}</th>')
    p.append('<th class="r" style="color:var(--t3)">Média</th></tr></thead><tbody>')
    for m in range(1, 13):
        p.append(f'<tr><td class="mono">{_MESES[m-1]}</td>')
        vals = [by_year_month[y].get(m, 0) for y in years]
        for i, v in enumerate(vals):
            if v == 0:
                p.append('<td class="r mono" style="color:var(--t3);font-size:10px">—</td>')
            else:
                intensity = (v / global_max) * 0.7 + 0.1
                p.append(f'<td class="r mono" style="background:rgba(64,140,90,{intensity:.2f});font-weight:500;font-size:11px">{fmt_brl(v, True)}</td>')
        avg = sum(v for v in vals if v > 0) / max(1, len([v for v in vals if v > 0]))
        p.append(f'<td class="r mono" style="color:var(--t3);font-size:10px">{fmt_brl(avg, True) if avg else "—"}</td>')
        p.append('</tr>')
    # linha total
    p.append('<tr style="border-top:2px solid var(--bd);font-weight:600"><td class="mono">TOTAL</td>')
    for y in years:
        p.append(f'<td class="r"><b>{fmt_brl(by_year_total[y], True)}</b></td>')
    p.append('<td class="r mono" style="color:var(--t3)">—</td></tr>')
    p.append('</tbody></table></div></div>')

    # Ano mais recente = base de ranking para tops
    latest_year = years[-1]

    # --- Top filiais por ano (ordenado pelo ano mais recente) ---
    p.append('<div class="sl">Top filiais Totvs por ano</div>')
    p.append(f'<div class="card"><div class="ct"><b>Top filiais</b><span>Ranking baseado em {latest_year} · 8 maiores</span></div><div class="tbl-wrap"><table class="dt"><thead><tr><th>Filial</th>')
    for y in years:
        marker = " ★" if y == latest_year else ""
        p.append(f'<th class="r">{y}{marker}</th>')
    p.append('</tr></thead><tbody>')
    # Universo: todas filiais que tiveram receita em qualquer ano
    all_filiais = set()
    for y in years:
        all_filiais.update(by_year_filial[y].keys())
    # Ordenação primária: receita no ano mais recente (desc).
    # Tie-breaker para filiais sem receita no ano mais recente: total geral
    total_filial_geral = defaultdict(float)
    for y in years:
        for cnpj, v in by_year_filial[y].items():
            total_filial_geral[cnpj] += v
    top_filiais = sorted(
        all_filiais,
        key=lambda c: (-by_year_filial[latest_year].get(c, 0), -total_filial_geral[c])
    )[:8]
    for cnpj in top_filiais:
        p.append(f'<tr><td>{escape(filial_meta.get(cnpj, "?"))}<div style="font-size:10px;color:var(--t3);font-family:var(--mono)">{cnpj}</div></td>')
        for y in years:
            v = by_year_filial[y].get(cnpj, 0)
            highlight = ';font-weight:600;color:var(--t1)' if y == latest_year and v else ''
            p.append(f'<td class="r mono" style="font-size:11px{highlight}">{fmt_brl(v, True) if v else "—"}</td>')
        p.append('</tr>')
    p.append('</tbody></table></div></div>')

    # --- Top clientes por ano (ordenado pelo ano mais recente) ---
    p.append('<div class="sl">Top clientes finais por ano</div>')
    p.append(f'<div class="card"><div class="ct"><b>Top clientes finais</b><span>Ranking baseado em {latest_year} · 12 maiores</span></div><div class="tbl-wrap"><table class="dt"><thead><tr><th>Cliente final</th>')
    for y in years:
        marker = " ★" if y == latest_year else ""
        p.append(f'<th class="r">{y}{marker}</th>')
    p.append('</tr></thead><tbody>')
    all_clientes = set()
    for y in years:
        all_clientes.update(by_year_cliente[y].keys())
    total_cli_geral = defaultdict(float)
    for y in years:
        for nm, v in by_year_cliente[y].items():
            total_cli_geral[nm] += v
    top_clientes_all = sorted(
        all_clientes,
        key=lambda nm: (-by_year_cliente[latest_year].get(nm, 0), -total_cli_geral[nm])
    )[:12]
    for nm in top_clientes_all:
        p.append(f'<tr><td>{escape(nm[:46])}</td>')
        for y in years:
            v = by_year_cliente[y].get(nm, 0)
            highlight = ';font-weight:600;color:var(--t1)' if y == latest_year and v else ''
            p.append(f'<td class="r mono" style="font-size:11px{highlight}">{fmt_brl(v, True) if v else "—"}</td>')
        p.append('</tr>')
    p.append('</tbody></table></div></div>')

    return "".join(p)


# ============ API PÚBLICA ============
def render_totvs(snap_path: Path = DEFAULT_SNAP, bling_dir: Path = DEFAULT_BLING_DIR) -> dict:
    """Retorna os fragments HTML para inserir no template do dashboard."""
    snap = _load_snapshot(Path(snap_path))
    docs = snap.get("documentos", [])

    # placeholder se ainda não há dados
    if not docs:
        pg_empty = (
            '<div class="pg" id="pg-totvs">'
            '<div class="hero"><div><div class="htitle">Comissões Totvs (ASE806)</div>'
            '<div class="hsub">Aguardando primeiros relatórios da Totvs</div></div></div>'
            '<div class="card" style="padding:32px;text-align:center;color:var(--t3)">'
            '<div style="font-family:var(--mono);font-size:11px;margin-bottom:8px">SEM DADOS</div>'
            '<div style="font-size:14px;color:var(--t2)">Suba os .eml ou .xlsx em <b>Drive › Easy_Financas › Comissoes_Totvs</b>'
            ' e o próximo runbook semanal vai popular esta aba.</div></div></div>'
        )
        return {
            "ntab": '<button class="ntab" onclick="sp(\'totvs\',this)">Comissões Totvs</button>',
            "mobtab": '<button class="mobtab" onclick="sp(\'totvs\',this,1)">Comissões Totvs</button>',
            "pg": pg_empty,
            "n_docs": 0,
            "total_real": 0.0,
        }

    # Agregação geral (todos os anos) — usada como default
    agg_all = _aggregate(docs)
    idx_bling = _load_bling_receber(Path(bling_dir))
    reconc_rows_all, reconc_summary_all = _reconcile(agg_all, idx_bling)

    real_acumulado = agg_all["real_acumulado"]
    provisao_total = agg_all["provisao_total"]
    comps_sorted = agg_all["comps_sorted"]
    filial_sorted = agg_all["filial_sorted"]

    # Agregações por ano (pre-renderizadas, JS troca visibilidade)
    years = sorted({(d.get("competencia") or "")[:4] for d in docs if d.get("competencia")})
    years = [y for y in years if y and len(y) == 4]  # remove vazios
    diretoria_by_year: Dict[str, str] = {"all": _render_diretoria(agg_all, reconc_rows_all, reconc_summary_all)}
    for year in years:
        docs_y = [d for d in docs if (d.get("competencia") or "").startswith(year)]
        agg_y = _aggregate(docs_y)
        reconc_y, summary_y = _reconcile(agg_y, idx_bling)
        diretoria_by_year[year] = _render_diretoria(agg_y, reconc_y, summary_y)

    yoy_html = _render_yoy(docs)

    hero_sub = f'{len(docs)} relatórios processados · {fmt_comp(min(comps_sorted)) if comps_sorted else "—"}–{fmt_comp(max(comps_sorted)) if comps_sorted else "—"}'

    # Renderiza pills de ano (Todos + cada ano detectado, em ordem decrescente)
    year_pills_html = '<button class="yr-pill active" data-yr="all" onclick="totvsYear(\'all\')" style="padding:5px 14px;font-size:11px;font-weight:500;cursor:pointer;border-radius:14px;border:1px solid var(--bd);background:var(--blue);color:#fff;font-family:var(--mono)">Todos</button>'
    for y in sorted(years, reverse=True):
        year_pills_html += f'<button class="yr-pill" data-yr="{y}" onclick="totvsYear(\'{y}\')" style="padding:5px 14px;font-size:11px;font-weight:500;cursor:pointer;border-radius:14px;border:1px solid var(--bd);background:var(--s1);color:var(--t1);font-family:var(--mono)">{y}</button>'

    # Renderiza divs de Diretoria — um por ano, só "all" visível
    diretoria_divs = []
    for key, html in diretoria_by_year.items():
        style = "" if key == "all" else "display:none"
        diretoria_divs.append(f'<div class="dir-year" data-yr="{key}" style="{style}">{html}</div>')
    diretoria_section = "".join(diretoria_divs)

    pg = f"""<!-- ═══ COMISSÕES TOTVS ═══ -->
<div class="pg" id="pg-totvs">
<div class="hero">
  <div>
    <div class="htitle">Comissões Totvs (ASE806)<br><span style="font-size:14px;color:var(--t2);font-weight:400">Abertura por cliente final · reconciliação com Bling · histórico desde {min(years) if years else "—"}</span></div>
    <div class="hsub">{hero_sub}</div>
  </div>
  <div class="pills">
    <span class="pill pg2"><span class="pdot"></span>Receita real {fmt_brl(real_acumulado, True)}</span>
    <span class="pill pa"><span class="pdot"></span>Provisão {fmt_brl(provisao_total, True)}</span>
    <span class="pill pb2"><span class="pdot"></span>{len(filial_sorted)} filiais Totvs</span>
  </div>
</div>

<div style="display:inline-flex;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:3px;margin-bottom:14px;gap:2px" id="totvs-modes">
  <button class="totvs-mode active" data-mode="diretoria" onclick="totvsMode('diretoria')" style="padding:6px 18px;font-size:12px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:var(--s1);color:var(--t1);font-family:var(--sans);box-shadow:0 1px 2px rgba(60,90,130,0.08)">Diretoria</button>
  <button class="totvs-mode" data-mode="anual" onclick="totvsMode('anual')" style="padding:6px 18px;font-size:12px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans)">Comparativo anual</button>
  <button class="totvs-mode" data-mode="operacional" onclick="totvsMode('operacional')" style="padding:6px 18px;font-size:12px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans)">Operacional</button>
</div>

<div id="totvs-year-bar" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;align-items:center">
  <span style="font-family:var(--mono);font-size:10.5px;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-right:6px">Filtrar por ano:</span>
  {year_pills_html}
</div>

<div id="totvs-pg-diretoria">{diretoria_section}</div>
<div id="totvs-pg-anual" style="display:none">{yoy_html}</div>
<div id="totvs-pg-operacional" style="display:none">{_render_operacional(agg_all, reconc_rows_all, reconc_summary_all)}</div>

<script>
function totvsMode(m){{
  document.querySelectorAll('.totvs-mode').forEach(function(b){{
    var on = b.dataset.mode === m;
    b.style.background = on ? 'var(--s1)' : 'none';
    b.style.color = on ? 'var(--t1)' : 'var(--t2)';
    b.style.boxShadow = on ? '0 1px 2px rgba(60,90,130,0.08)' : 'none';
    b.classList.toggle('active', on);
  }});
  document.getElementById('totvs-pg-diretoria').style.display = (m==='diretoria') ? 'block' : 'none';
  document.getElementById('totvs-pg-anual').style.display = (m==='anual') ? 'block' : 'none';
  document.getElementById('totvs-pg-operacional').style.display = (m==='operacional') ? 'block' : 'none';
  // Esconde year-bar fora da Diretoria (filtro anual só faz sentido lá)
  document.getElementById('totvs-year-bar').style.display = (m==='diretoria') ? 'flex' : 'none';
}}
function totvsYear(y){{
  document.querySelectorAll('.yr-pill').forEach(function(b){{
    var on = b.dataset.yr === y;
    b.style.background = on ? 'var(--blue)' : 'var(--s1)';
    b.style.color = on ? '#fff' : 'var(--t1)';
    b.classList.toggle('active', on);
  }});
  document.querySelectorAll('.dir-year').forEach(function(d){{
    d.style.display = (d.dataset.yr === y) ? 'block' : 'none';
  }});
}}
</script>
</div>"""

    return {
        "ntab": '<button class="ntab" onclick="sp(\'totvs\',this)">Comissões Totvs</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'totvs\',this,1)">Comissões Totvs</button>',
        "pg": pg,
        "n_docs": len(docs),
        "total_real": real_acumulado,
    }


if __name__ == "__main__":
    import sys
    r = render_totvs()
    print(f"[ok] n_docs={r['n_docs']} total_real={r['total_real']:.2f}", file=sys.stderr)
    print(r["pg"])
