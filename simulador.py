#!/usr/bin/env python3
"""
simulador.py — Aba "Simulador de Cenários" do dashboard Easy Analytics.

Pedido do Leandro (26/09/2026): aba para reuniões com os sócios com
choques de ±% no faturamento e ±% nos gastos, recalculando em tempo real
o DRE, o fluxo de caixa projetado e a tabela mês a mês de receitas e
gastos (com abertura por fornecedor/cliente).

REGRA DA BASE (não recalcula projeção própria — ver CLAUDE.md §10, 11/09):
  A base do simulador é EXATAMENTE a matriz do DRE/P&L
  (dre_render._build_matriz_2y): meses passados = realizado Bling (+ Totvs
  onde o Bling é mudo); mês corrente e futuros = em aberto + complementos
  das fontes únicas `receita_sintetica_por_mes` (média recorrente, janela
  `media_meses` do receitas_classificacao.json) e
  `despesa_projetada_por_mes` (média 3m fechados operacionais).

  Duas bases alternativas são pré-calculadas com as MESMAS funções, só
  trocando a janela da média (3m/3m e 6m/6m) — o seletor "Base da média"
  existe para a discussão com os sócios; a base "Oficial" bate com o DRE.

CHOQUES (aplicados só nos meses FUTUROS — > mês corrente; realizado nunca muda):
  - Receita: ±% sobre a base (nível) ou ±% a.m. composto.
  - Gastos operacionais (Pessoal/PJ + Serviços/Adm): ±% global e ajuste fino
    por grupo.
  - Tributos (ISS/PIS/COFINS/IRPJ/CSLL): acompanham a receita na mesma
    proporção (Lucro Presumido → proporcional ao faturamento).
  - Estruturais (buy-out/acordo): contratuais, não sofrem choque.
  - Ajustes em R$/mês (receita nova, corte/aumento de gasto) a partir de um mês.

Tudo numérico vem dos dados (princípio §6.1). O template só recebe os
marcadores @@SIM_NTAB@@ / @@SIM_MOBTAB@@ / @@SIM_PG@@.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import dre_render as _dr

MN = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
      "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

# grupo do DRE → grupo do simulador
_GMAP = {
    "receita_servicos": "rec", "outras_receitas": "rec", "financ_receita": "rec",
    "deducoes_iss": "tv", "deducoes_pis": "tv", "deducoes_cofins": "tv",
    "deducoes_pis_esperado": "tv", "deducoes_cofins_esperado": "tv",
    "desp_pessoal": "pes",
    "desp_admin": "adm", "outras_despesas": "adm", "financ_despesa": "adm",
    "impostos_lucro": "tl",
    "nao_recorrente": "est",
    "aporte_socio": "apo",
}
_GROUPS = [
    {"k": "rec", "label": "Receita de serviços", "sign": 1},
    {"k": "tv",  "label": "Tributos sobre vendas (ISS · PIS · COFINS)", "sign": -1},
    {"k": "pes", "label": "Pessoal · PJ · encargos", "sign": -1},
    {"k": "adm", "label": "Serviços estratégicos + Administrativas", "sign": -1},
    {"k": "tl",  "label": "IRPJ + CSLL", "sign": -1},
    {"k": "est", "label": "Estruturais (buy-out · acordo)", "sign": -1},
    {"k": "apo", "label": "Aportes / sócios (informativo — fora do resultado)", "sign": 0},
]
_KIND = {"real": "r", "em_aberto": "a", "projetado": "p", "totvs": "t", "sem_dado": "s"}
_KPRI = {"p": 4, "a": 3, "t": 2, "r": 1, "s": 0, "-": -1}
_RESID_LABEL = {
    "deducoes_pis_esperado": "PIS esperado (0,65% da receita)",
    "deducoes_cofins_esperado": "COFINS esperado (3% da receita)",
}

_SUFIXOS = re.compile(
    r"\b(LTDA|S/?A|S\.A\.?|EIRELI|ME|EPP|INDUSTRIA E COMERCIO|COMERCIO)\b\.?", re.I)


def _nice_cliente(nome: str) -> str:
    n = (nome or "(sem nome)").strip()
    if n.upper() != n and n.lower() != n:
        base = n  # já tem caixa mista (rótulo sintético ou nome digitado)
    else:
        base = _SUFIXOS.sub("", n).strip(" .-,")
        preps = {"DE", "DA", "DO", "DAS", "DOS", "E", "EM", "PARA"}
        ws = []
        for w in base.split():
            if w.upper() in preps:
                ws.append(w.lower())
            elif len(w) <= 3 and w.isalpha():
                ws.append(w.upper())
            else:
                ws.append(w.capitalize())
        base = " ".join(ws)
    return (base[:46] + "…") if len(base) > 47 else base


def _lbl(ym: str) -> str:
    return f"{MN[int(ym[5:7]) - 1]}/{ym[2:4]}"


def _rows_da_matriz(mz: dict, months: list[str], cutoff: str,
                    desp_n: int) -> list[dict]:
    """Achata a matriz em linhas {g, s, l, v[], k} — soma por grupo bate
    100% com mz['grupos'] (resíduo sem item vira linha própria)."""
    idx = {m: i for i, m in enumerate(months)}
    n = len(months)
    agg: dict[tuple[str, str, str], dict] = {}

    def _slot(g: str, s: str, l: str) -> dict:
        key = (g, s, l)
        if key not in agg:
            agg[key] = {"g": g, "s": s, "l": l, "v": [0.0] * n, "k": ["-"] * n}
        return agg[key]

    item_sum: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for dg, subs in (mz.get("items") or {}).items():
        sg = _GMAP.get(dg, "adm")
        for sub, lst in subs.items():
            for it in lst:
                ym = it.get("ym")
                if ym not in idx:
                    continue
                v = float(it.get("valor") or 0)
                item_sum[dg][ym] += v
                kind = _KIND.get(it.get("kind"), "r")
                if sg == "rec":
                    lab = _nice_cliente(it.get("contato", ""))
                else:
                    lab = _dr._classify(it.get("contato", "") or "",
                                        it.get("historico", "") or "")[2]
                    if kind == "p" and not it.get("contato"):
                        lab = "Projeção"
                row = _slot(sg, sub, lab)
                i = idx[ym]
                row["v"][i] += v
                if _KPRI[kind] > _KPRI[row["k"][i]]:
                    row["k"][i] = kind

    ck = mz.get("cell_kinds") or {}
    for dg, mvals in (mz.get("grupos") or {}).items():
        sg = _GMAP.get(dg, "adm")
        for ym, total in mvals.items():
            if ym not in idx:
                continue
            resid = round(float(total) - item_sum[dg].get(ym, 0.0), 2)
            if abs(resid) < 0.5:
                continue
            if dg in _RESID_LABEL:
                lab, sub = _RESID_LABEL[dg], "Impostos sobre vendas"
            elif ym >= cutoff:
                lab = f"Projeção despesa recorrente (média {desp_n}m)"
                sub = "Projeção"
            else:
                lab, sub = "Outros (sem lançamento detalhado)", "Outros"
            kind = _KIND.get((ck.get(dg) or {}).get(ym), "p" if ym >= cutoff else "r")
            row = _slot(sg, sub, lab)
            i = idx[ym]
            row["v"][i] += resid
            if _KPRI[kind] > _KPRI[row["k"][i]]:
                row["k"][i] = kind

    out = []
    for row in agg.values():
        row["v"] = [round(x, 2) for x in row["v"]]
        if not any(abs(x) >= 0.5 for x in row["v"]):
            continue
        row["k"] = "".join(row["k"])
        out.append(row)
    out.sort(key=lambda r: (r["g"], -sum(abs(x) for x in r["v"])))
    return out


def compute_simulador_data(bling_dir: Path, totvs_snap: Path, today: date,
                           saldo_caixa: float | None, saldo_fonte: str) -> dict:
    pagas, em_aberto, recebidas, receber_em_aberto = _dr._load_bling_csvs(bling_dir)
    totvs = _dr._load_totvs_por_mes(Path(totvs_snap))
    cutoff = today.strftime("%Y-%m")
    cfg = _dr._load_receitas_cfg()
    rec_n_oficial = int(cfg.get("media_meses") or 2)

    variantes = [
        ("oficial", rec_n_oficial, 3,
         f"Oficial do DRE — receita {rec_n_oficial}m · despesa 3m"),
        ("m3", 3, 3, "3 meses — receita 3m · despesa 3m"),
        ("m6", 6, 6, "6 meses — receita 6m · despesa 6m"),
    ]
    bases: dict[str, Any] = {}
    months: list[str] = []
    mtypes: list[str] = []
    for key, rn, dn, label in variantes:
        mz = _dr._build_matriz_2y(pagas, recebidas, em_aberto, receber_em_aberto,
                                  today, start_year=today.year,
                                  end_year=today.year + 1, totvs_por_mes=totvs,
                                  media_rec_meses=rn, media_desp_meses=dn)
        if not months:
            months = mz["months"]
            mtypes = [_KIND.get(mz["month_types"].get(m), "p") for m in months]
        c2 = dict(cfg)
        c2["media_meses"] = rn
        media_rec, meses_rec = _dr._media_recorrente(recebidas, c2, cutoff)
        dp = _dr.despesa_projetada_por_mes(pagas, em_aberto, today,
                                           _dr._next_month(cutoff), n_meses=dn)
        media_desp = next(iter(dp.values()))["media_op"] if dp else 0.0
        y, m = today.year, today.month
        meses_desp = []
        for _ in range(dn):
            m -= 1
            if m == 0:
                y, m = y - 1, 12
            meses_desp.append(f"{y:04d}-{m:02d}")
        bases[key] = {
            "label": label,
            "rec_n": rn, "desp_n": dn,
            "media_rec": round(media_rec, 2),
            "meses_rec": [_lbl(x) for x in meses_rec],
            "media_desp": round(media_desp, 2),
            "meses_desp": [_lbl(x) for x in sorted(meses_desp)],
            "rows": _rows_da_matriz(mz, months, cutoff, dn),
        }

    return {
        "gerado": today.isoformat(),
        "cutoff": cutoff,
        "cutoff_label": _lbl(cutoff),
        "months": months,
        "labels": [_lbl(m) for m in months],
        "mtypes": "".join(mtypes),
        "groups": _GROUPS,
        "bases": bases,
        "base_default": "oficial",
        "saldo": round(saldo_caixa, 2) if isinstance(saldo_caixa, (int, float)) else None,
        "saldo_fonte": saldo_fonte or "",
    }


def _json_script(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("</", "<\\/")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_simulador(bling_dir: Path, totvs_snap: Path, today: date,
                     saldo_caixa: float | None, saldo_fonte: str) -> dict[str, str]:
    data = compute_simulador_data(bling_dir, totvs_snap, today, saldo_caixa, saldo_fonte)
    pg = (_PG_HTML
          .replace("@@SIMX_DATA@@", _json_script(data))
          .replace("@@SIMX_CSS@@", _CSS)
          .replace("@@SIMX_JS@@", _JS))
    n_rows = sum(len(b["rows"]) for b in data["bases"].values())
    return {
        "ntab": '<button class="ntab" onclick="sp(\'simulador\',this)">Simulador</button>',
        "mobtab": '<button class="mobtab" onclick="sp(\'simulador\',this,1)">Simulador</button>',
        "pg": pg,
        "n_rows": n_rows,
        "n_months": len(data["months"]),
    }


# ───────────────────────────── HTML / CSS / JS ─────────────────────────────
_CSS = r"""
.simx-panel{position:sticky;top:50px;z-index:20;background:var(--s1);border:1px solid var(--bd);border-radius:13px;padding:14px 16px;box-shadow:0 4px 14px rgba(20,40,70,0.08);margin-bottom:14px}
.simx-grid{display:grid;grid-template-columns:1.25fr 1.25fr 1fr;gap:18px}
.simx-g21{grid-template-columns:minmax(0,2fr) minmax(0,1fr)}
.simx-lbl{font-family:var(--mono);font-size:9.5px;font-weight:500;letter-spacing:.09em;color:var(--t3);text-transform:uppercase;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.simx-val{font-family:var(--mono);font-size:22px;font-weight:500;letter-spacing:-.01em;line-height:1}
.simx-val.pos{color:var(--green)}.simx-val.neg{color:var(--red)}.simx-val.zero{color:var(--t2)}
.simx-row{display:flex;align-items:center;gap:10px}
.simx-row input[type=range]{flex:1;accent-color:var(--blue);height:22px;cursor:pointer;min-width:0}
.simx-num{width:62px;padding:4px 6px;border-radius:7px;border:1px solid var(--bd2);background:var(--s2);color:var(--t1);font-family:var(--mono);font-size:12px;text-align:right;outline:none}
.simx-num.wide{width:110px}
.simx-num:focus{border-color:var(--blue)}
.simx-seg{display:inline-flex;background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:2px;gap:2px}
.simx-seg button{padding:4px 10px;font-size:10.5px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans);white-space:nowrap}
.simx-seg button.on{background:var(--s1);color:var(--t1);box-shadow:0 1px 2px rgba(60,90,130,0.12)}
.simx-chips{display:flex;flex-wrap:wrap;gap:6px}
.simx-chip{padding:4px 11px;border-radius:999px;font-size:11px;font-family:var(--sans);font-weight:500;cursor:pointer;border:1px solid var(--bd2);background:var(--s2);color:var(--t2);transition:all .15s}
.simx-chip:hover{color:var(--t1);border-color:var(--blue-br)}
.simx-chip.on{background:var(--blue);border-color:var(--blue);color:#fff}
.simx-btn{padding:5px 12px;border-radius:7px;font-size:11px;font-family:var(--sans);font-weight:500;cursor:pointer;border:1px solid var(--bd2);background:var(--s2);color:var(--t1);transition:all .15s;white-space:nowrap}
.simx-btn:hover{border-color:var(--blue);color:var(--blue)}
.simx-btn.pri{background:var(--blue);border-color:var(--blue);color:#fff}
.simx-btn.pri:hover{opacity:.88;color:#fff}
.simx-adv{margin-top:12px;padding-top:12px;border-top:1px dashed var(--bd2);display:none;grid-template-columns:repeat(4,1fr);gap:16px}
.simx-adv.open{display:grid}
.simx-foot{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-top:12px;padding-top:10px;border-top:1px solid var(--bd)}
.simx-note{font-size:10.5px;color:var(--t3);font-family:var(--mono);line-height:1.5}
.simx-kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:12px}
.simx-kpi{background:var(--s1);border:1px solid var(--bd);border-radius:11px;padding:13px 14px;box-shadow:0 1px 3px rgba(60,90,130,0.06);position:relative;overflow:hidden}
.simx-kpi::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--kc,var(--blue))}
.simx-kpi .kl{font-size:9.5px;font-weight:500;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;font-family:var(--mono);margin-bottom:6px}
.simx-kpi .kv{font-family:var(--mono);font-size:19px;font-weight:500;line-height:1.05;letter-spacing:-.01em}
.simx-kpi .ks{font-size:10.5px;color:var(--t3);margin-top:5px;line-height:1.35}
.simx-d{display:inline-block;font-family:var(--mono);font-size:10px;font-weight:500;padding:1px 6px;border-radius:999px;margin-top:6px}
.simx-d.up{background:var(--green-bg);color:var(--green)}.simx-d.dn{background:var(--red-bg);color:var(--red)}.simx-d.eq{background:var(--slate-bg);color:var(--slate)}
.simx-tbl{width:100%;border-collapse:separate;border-spacing:0;font-size:11.5px}
.simx-tbl th{position:sticky;top:0;background:var(--s1);z-index:2;text-align:right;padding:6px 8px;color:var(--t3);font-size:9.5px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--bd2);font-family:var(--mono);white-space:nowrap}
.simx-tbl th:first-child,.simx-tbl td:first-child{text-align:left;position:sticky;left:0;background:var(--s1);z-index:3;min-width:230px;max-width:300px}
.simx-tbl th:first-child{z-index:4}
.simx-tbl td{padding:5px 8px;border-bottom:1px solid var(--bd);text-align:right;font-family:var(--mono);font-size:11px;white-space:nowrap;color:var(--t1)}
.simx-tbl td:first-child{font-family:var(--sans);font-size:11.5px;overflow:hidden;text-overflow:ellipsis}
.simx-tbl tr.sub td{font-weight:600;background:var(--s2)}
.simx-tbl tr.sub td:first-child{background:var(--s2)}
.simx-tbl tr.tot td{font-weight:600;background:var(--s3);border-top:1px solid var(--bd2)}
.simx-tbl tr.tot td:first-child{background:var(--s3)}
.simx-tbl tr.mg td{color:var(--t3);font-size:10.5px;font-style:italic}
.simx-tbl tr.grp{cursor:pointer}
.simx-tbl tr.grp:hover td{background:var(--blue-bg)}
.simx-tbl tr.grp td:first-child::before{content:'▸';display:inline-block;width:14px;color:var(--t3);transition:transform .15s}
.simx-tbl tr.grp.open td:first-child::before{transform:rotate(90deg)}
.simx-tbl tr.it td{font-size:10.5px;color:var(--t2)}
.simx-tbl tr.it td:first-child{padding-left:26px;color:var(--t2)}
.simx-tbl td.fut{background:var(--amber-bg)}
.simx-tbl th.fut{color:var(--amber)}
.simx-tbl td.chg{color:var(--blue);font-weight:500}
.simx-tbl td.cmp{background:var(--s2)}
.simx-tbl td.neg{color:var(--red)}
.simx-tbl .kchip{display:block;font-size:8.5px;letter-spacing:.04em;margin-top:1px;font-weight:400}
.simx-wrap{overflow:auto;max-height:640px;border:1px solid var(--bd);border-radius:9px}
.simx-legend{display:flex;flex-wrap:wrap;gap:14px;font-size:10.5px;color:var(--t2);margin-top:10px}
.simx-legend span{display:inline-flex;align-items:center;gap:5px}
.simx-sw{width:10px;height:10px;border-radius:2px;display:inline-block}
.simx-cmp td.nm{font-family:var(--sans);font-weight:500}
.simx-alert{padding:9px 12px;border-radius:9px;border-left:2px solid var(--amber);background:var(--amber-bg);color:var(--amber);font-size:11.5px;margin-bottom:10px}
@media(max-width:1100px){.simx-kpis{grid-template-columns:repeat(3,1fr)}.simx-grid{grid-template-columns:1fr 1fr}.simx-adv{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.simx-g21{grid-template-columns:minmax(0,1fr)!important}.simx-panel{position:static}.simx-grid{grid-template-columns:1fr}.simx-kpis{grid-template-columns:1fr 1fr}.simx-adv{grid-template-columns:1fr}}
@media print{
  body.simx-printing nav,body.simx-printing .mobmenu,body.simx-printing footer,body.simx-printing .simx-noprint,body.simx-printing #eaChatBtn{display:none!important}
  body.simx-printing .pg{display:none!important}
  body.simx-printing #pg-simulador{display:block!important;max-width:none;padding:0}
  body.simx-printing .simx-panel{position:static;box-shadow:none}
  body.simx-printing .simx-wrap{max-height:none;overflow:visible}
}
"""

_PG_HTML = r"""<!-- ═══ SIMULADOR DE CENÁRIOS ═══ -->
<div class="pg" id="pg-simulador">
<style>@@SIMX_CSS@@</style>
<div class="hero">
  <div>
    <div class="htitle">Simulador de Cenários<br><span style="font-size:14px;color:var(--t2);font-weight:400" id="simxSubtitle">DRE · fluxo de caixa · receitas e gastos — recalculado em tempo real</span></div>
    <div class="hsub" id="simxHsub">—</div>
  </div>
  <div class="pills">
    <span class="pill pg2"><span class="pdot"></span>Realizado (Bling)</span>
    <span class="pill pa"><span class="pdot"></span>Projetado (base DRE)</span>
    <span class="pill pb2"><span class="pdot"></span>Cenário simulado</span>
  </div>
</div>

<div class="simx-panel simx-noprint" id="simxPanel">
  <div class="simx-grid">
    <div>
      <div class="simx-lbl"><span>Faturamento</span>
        <span class="simx-seg"><button id="simxRecNivel" class="on" onclick="simxSet('recMode','nivel')">sobre a base</button><button id="simxRecAm" onclick="simxSet('recMode','am')">ao mês (composto)</button></span>
      </div>
      <div class="simx-row">
        <div class="simx-val zero" id="simxRecOut" style="width:92px">0%</div>
        <input type="range" id="simxRec" min="-50" max="50" step="1" value="0" oninput="simxSlide('rec',this.value)">
        <input class="simx-num" id="simxRecN" type="number" step="0.5" value="0" onchange="simxSlide('rec',this.value)">
      </div>
      <div class="simx-note" id="simxRecNote">—</div>
    </div>
    <div>
      <div class="simx-lbl"><span>Gastos operacionais</span>
        <button class="simx-btn" style="padding:2px 9px;font-size:10px" onclick="simxToggleAdv()" id="simxAdvBtn">ajuste fino ▾</button>
      </div>
      <div class="simx-row">
        <div class="simx-val zero" id="simxDespOut" style="width:92px">0%</div>
        <input type="range" id="simxDesp" min="-50" max="50" step="1" value="0" oninput="simxSlide('desp',this.value)">
        <input class="simx-num" id="simxDespN" type="number" step="0.5" value="0" onchange="simxSlide('desp',this.value)">
      </div>
      <div class="simx-note" id="simxDespNote">—</div>
    </div>
    <div>
      <div class="simx-lbl"><span>Período de análise</span></div>
      <select class="ctrl-select" id="simxPeriodo" onchange="simxSet('periodo',this.value)" style="width:100%"></select>
      <div class="simx-lbl" style="margin-top:10px"><span>Base da média (projeção)</span></div>
      <select class="ctrl-select" id="simxBase" onchange="simxSet('base',this.value)" style="width:100%"></select>
    </div>
  </div>
  <div class="simx-adv" id="simxAdv">
    <div>
      <div class="simx-lbl"><span>Pessoal · PJ (adicional)</span><span id="simxPesOut">0%</span></div>
      <input type="range" id="simxPes" min="-50" max="50" step="1" value="0" oninput="simxSlide('pes',this.value)" style="width:100%;accent-color:var(--blue)">
    </div>
    <div>
      <div class="simx-lbl"><span>Serviços + Adm. (adicional)</span><span id="simxAdmOut">0%</span></div>
      <input type="range" id="simxAdm" min="-50" max="50" step="1" value="0" oninput="simxSlide('adm',this.value)" style="width:100%;accent-color:var(--blue)">
    </div>
    <div>
      <div class="simx-lbl"><span>Receita nova (R$/mês)</span></div>
      <div class="simx-row"><input class="simx-num wide" id="simxAddRec" type="number" step="1000" value="0" onchange="simxSet('addRec',+this.value||0)"><span class="simx-note">a partir de</span><select class="ctrl-select" id="simxAddRecDe" onchange="simxSet('addRecDe',this.value)"></select></div>
    </div>
    <div>
      <div class="simx-lbl"><span>Gasto extra / corte (R$/mês)</span></div>
      <div class="simx-row"><input class="simx-num wide" id="simxAddDesp" type="number" step="1000" value="0" onchange="simxSet('addDesp',+this.value||0)"><span class="simx-note">a partir de</span><select class="ctrl-select" id="simxAddDespDe" onchange="simxSet('addDespDe',this.value)"></select></div>
      <div class="simx-note">negativo = corte de gasto</div>
    </div>
  </div>
  <div class="simx-foot">
    <div class="simx-chips" id="simxPresets"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="simx-btn" onclick="simxReset()">Zerar</button>
      <button class="simx-btn" onclick="simxPin()">Fixar cenário p/ comparar</button>
      <button class="simx-btn" onclick="simxPrint()">Imprimir / PDF</button>
    </div>
  </div>
</div>

<div id="simxAlerts"></div>
<div class="simx-kpis" id="simxKpis"></div>

<div class="sl">Resultado mês a mês — cenário vs base</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Receita × Despesas totais · resultado de caixa</b><span class="ct-tag" id="simxChartTag">—</span></div>
  <div class="cw" style="height:300px"><canvas id="simxBar"></canvas></div>
  <div class="simx-legend">
    <span><i class="simx-sw" style="background:var(--green)"></i>Receita (cenário)</span>
    <span><i class="simx-sw" style="background:var(--red)"></i>Despesas + tributos + estruturais (cenário)</span>
    <span><i class="simx-sw" style="background:var(--blue);height:3px"></i>Resultado de caixa (cenário)</span>
    <span><i class="simx-sw" style="border-top:2px dashed var(--t3);height:0;width:14px"></i>Resultado de caixa (base)</span>
    <span style="color:var(--t3)">barras translúcidas = meses projetados</span>
  </div>
</div>

<div class="sl">DRE simulado</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b id="simxDreTitle">DRE — cenário</b>
    <span style="display:flex;gap:8px;align-items:center" class="simx-noprint">
      <span class="simx-seg" id="simxDreMode"><button data-m="mensal" class="on" onclick="simxSet('dreMode','mensal')">Mensal</button><button data-m="tri" onclick="simxSet('dreMode','tri')">Trimestral</button><button data-m="anual" onclick="simxSet('dreMode','anual')">Anual</button></span>
      <button class="simx-btn" style="padding:3px 9px;font-size:10px" onclick="simxCsv('dre')">CSV</button>
    </span>
  </div>
  <div class="simx-wrap"><table class="simx-tbl" id="simxDre"></table></div>
  <div class="simx-note" style="margin-top:8px">Colunas âmbar = projeção. Valores em azul = alterados pelo cenário. As 4 últimas colunas comparam o total do período no cenário contra a base (mesma regra do DRE).</div>
</div>

<div class="sl">Fluxo de caixa projetado</div>
<div class="g21 simx-g21" style="margin-bottom:12px">
  <div class="card">
    <div class="ct"><b>Saldo de caixa — cenário vs base</b><span class="ct-tag" id="simxCxTag">—</span></div>
    <div class="cw" style="height:260px"><canvas id="simxCx"></canvas></div>
  </div>
  <div class="card">
    <div class="ct"><b>Premissas do caixa</b></div>
    <div class="simx-lbl"><span>Saldo inicial de caixa (R$)</span><span id="simxSaldoFonte">—</span></div>
    <div class="simx-row simx-noprint" style="margin-bottom:8px"><input class="simx-num wide" id="simxSaldo" type="number" step="1000" onchange="simxSet('saldo',+this.value||0)" style="width:100%;font-size:14px;padding:7px 9px"></div>
    <div class="simx-note" id="simxSaldoNote">—</div>
    <hr class="dv" style="margin:12px 0">
    <div id="simxCxResumo"></div>
  </div>
</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Fluxo de caixa mensal — cenário</b><button class="simx-btn simx-noprint" style="padding:3px 9px;font-size:10px" onclick="simxCsv('cx')">CSV</button></div>
  <div class="simx-wrap"><table class="simx-tbl" id="simxCxTbl"></table></div>
  <div class="simx-note" style="margin-top:8px">Regime de caixa ≈ vencimento (mesma convenção do DRE). Começa no mês seguinte ao atual; lançamentos em aberto do mês corrente e recebíveis vencidos não entram. Aportes de sócio não entram — o "menor saldo" mostra a necessidade de caixa.</div>
</div>

<div class="sl">Receitas e gastos mês a mês — com abertura</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Por grupo → cliente / fornecedor</b>
    <span style="display:flex;gap:8px" class="simx-noprint">
      <button class="simx-btn" style="padding:3px 9px;font-size:10px" onclick="simxExpand(true)">Abrir tudo</button>
      <button class="simx-btn" style="padding:3px 9px;font-size:10px" onclick="simxExpand(false)">Fechar tudo</button>
      <button class="simx-btn" style="padding:3px 9px;font-size:10px" onclick="simxCsv('det')">CSV</button>
    </span>
  </div>
  <div class="simx-wrap"><table class="simx-tbl" id="simxDet"></table></div>
  <div class="simx-note" style="margin-top:8px">Clique no grupo para abrir os lançamentos agregados por cliente/fornecedor. Em meses projetados, "Projeção recorrente/despesa recorrente" é o complemento da média — o restante são lançamentos já agendados no Bling. Passe o mouse para ver o valor base.</div>
</div>

<div class="sl">Comparação de cenários fixados</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Cenários lado a lado (período selecionado)</b><span class="ct-tag">salvos só neste navegador</span></div>
  <div class="tbl-wrap"><table class="simx-tbl simx-cmp" id="simxCmp"></table></div>
</div>

<script>window.SIMX_DATA=@@SIMX_DATA@@;
@@SIMX_JS@@</script>
</div>"""

_JS = r"""
(function(){
  var D=null;
  D=window.SIMX_DATA||null;
  if(!D||!D.months||!D.bases){window.simRender=function(){};return;}
  var M=D.months, N=M.length, L=D.labels, MT=D.mtypes, CUT=D.cutoff;
  var CI=M.indexOf(CUT);            // índice do mês corrente
  var FUT=function(i){return i>CI;};
  var LSKEY='ea_simx_state_v1', LSPIN='ea_simx_pins_v1';
  var S={base:D.base_default,rec:0,recMode:'nivel',desp:0,pes:0,adm:0,addRec:0,addRecDe:M[CI+1]||M[N-1],addDesp:0,addDespDe:M[CI+1]||M[N-1],periodo:'prox12',dreMode:'mensal',saldo:(D.saldo!=null?D.saldo:0)};
  var PRESETS=[
    {n:'Pessimista',rec:-20,recMode:'nivel',desp:5},
    {n:'Conservador',rec:-10,recMode:'nivel',desp:0},
    {n:'Base',rec:0,recMode:'nivel',desp:0},
    {n:'Otimista',rec:10,recMode:'nivel',desp:-5},
    {n:'Expansão',rec:3,recMode:'am',desp:8}
  ];
  var OPEN={};             // grupos abertos na tabela de detalhe
  var CH={bar:null,cx:null};
  var inited=false;

  function cssv(n,fb){try{var v=getComputedStyle(document.documentElement).getPropertyValue(n).trim();return v||fb;}catch(e){return fb;}}
  function alpha(c,a){ // aceita #hex ou rgb()
    c=(c||'').trim();
    if(c[0]==='#'){var h=c.slice(1);if(h.length===3)h=h.split('').map(function(x){return x+x;}).join('');
      var r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16);return 'rgba('+r+','+g+','+b+','+a+')';}
    var m=c.match(/rgba?\(([^)]+)\)/);if(m){var p=m[1].split(',');return 'rgba('+p[0]+','+p[1]+','+p[2]+','+a+')';}
    return c;
  }
  function fmt(v){if(!isFinite(v))return '—';var r=Math.round(v);if(r===0)return '—';return (r<0?'−':'')+Math.abs(r).toLocaleString('pt-BR');}
  function brl(v){if(!isFinite(v))return '—';var r=Math.round(v);return (r<0?'−':'')+'R$ '+Math.abs(r).toLocaleString('pt-BR');}
  function kbrl(v){var a=Math.abs(v),s=v<0?'−':'';if(a>=1e6)return s+'R$ '+(a/1e6).toFixed(2).replace('.',',')+'M';if(a>=1e3)return s+'R$ '+(a/1e3).toFixed(a>=1e5?0:1).replace('.',',')+'K';return s+'R$ '+Math.round(a);}
  function pct(v,d){if(!isFinite(v))return '—';return (v>0?'+':v<0?'−':'')+Math.abs(v*100).toFixed(d==null?1:d).replace('.',',')+'%';}
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function lsGet(k){try{var v=localStorage.getItem(k);return v?JSON.parse(v):null;}catch(e){return null;}}
  function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}

  // ── período ──
  function periodos(){
    var y0=M[0].slice(0,4), y1=M[N-1].slice(0,4), out=[];
    var f=CI+1, t=Math.min(N-1,CI+12);
    if(f<N)out.push({k:'prox12',n:'Próximos 12 meses ('+L[f]+' → '+L[t]+')',a:f,b:t});
    out.push({k:'ano',n:'Ano '+y0+' (realizado + projeção)',a:0,b:Math.min(N-1,11)});
    if(y1!==y0)out.push({k:'ano1',n:'Ano '+y1+' (projeção)',a:12,b:N-1});
    out.push({k:'tudo',n:'Jan/'+y0.slice(2)+' → Dez/'+y1.slice(2)+' (24 meses)',a:0,b:N-1});
    return out;
  }
  function per(){var ps=periodos();for(var i=0;i<ps.length;i++)if(ps[i].k===S.periodo)return ps[i];return ps[0];}

  // ── motor ──
  function compute(st){
    var B=D.bases[st.base]||D.bases[D.base_default];
    var rows=B.rows, n=N, i, g;
    var gb={}, gs={}; // grupo → [base],[sim]
    ['rec','tv','pes','adm','tl','est','apo'].forEach(function(k){gb[k]=new Array(n).fill(0);gs[k]=new Array(n).fill(0);});
    // receita base por mês (para fator de tributos)
    rows.forEach(function(r){if(r.g==='rec')for(i=0;i<n;i++)gb.rec[i]+=r.v[i];});
    var fr=new Array(n).fill(1), addR=new Array(n).fill(0), addD=new Array(n).fill(0), ft=new Array(n).fill(1);
    var fp=(1+st.desp/100)*(1+st.pes/100), fa=(1+st.desp/100)*(1+st.adm/100);
    var iR=M.indexOf(st.addRecDe), iD=M.indexOf(st.addDespDe);
    for(i=0;i<n;i++){
      if(!FUT(i))continue;
      var k=i-CI;
      fr[i]=st.recMode==='am'?Math.pow(1+st.rec/100,k):(1+st.rec/100);
      if(st.addRec&&iR>=0&&i>=iR)addR[i]=st.addRec;
      if(st.addDesp&&iD>=0&&i>=iD)addD[i]=st.addDesp;
      var rs=gb.rec[i]*fr[i]+addR[i];
      ft[i]=gb.rec[i]>0?rs/gb.rec[i]:1;
    }
    var fac=function(gk,i){
      if(!FUT(i))return 1;
      if(gk==='rec')return fr[i];
      if(gk==='tv'||gk==='tl')return ft[i];
      if(gk==='pes')return fp;
      if(gk==='adm')return fa;
      return 1;
    };
    var det=[];
    rows.forEach(function(r){
      var sv=new Array(n);
      for(i=0;i<n;i++){sv[i]=r.v[i]*fac(r.g,i);if(r.g!=='rec')gb[r.g][i]+=r.v[i];gs[r.g][i]+=sv[i];}
      det.push({g:r.g,s:r.s,l:r.l,k:r.k,v:r.v,sv:sv});
    });
    // tributos sobre receita nova quando a base do mês é zero (sem proporção possível)
    var taxRate=0,rb=0,tb=0;for(i=0;i<n;i++){if(gb.rec[i]>0){rb+=gb.rec[i];tb+=gb.tv[i]+gb.tl[i];}}
    taxRate=rb>0?tb/rb:0;
    if(addR.some(function(x){return x;})){
      var sv=new Array(n).fill(0),zv=new Array(n).fill(0),kk='';
      for(i=0;i<n;i++){sv[i]=addR[i];gs.rec[i]+=addR[i];kk+=addR[i]?'p':'-';
        if(addR[i]&&gb.rec[i]<=0){gs.tv[i]+=addR[i]*taxRate;}}
      det.push({g:'rec',s:'Cenário',l:'Receita nova (cenário)',k:kk,v:zv,sv:sv,sim:1});
    }
    if(addD.some(function(x){return x;})){
      var sv2=new Array(n).fill(0),zv2=new Array(n).fill(0),kk2='';
      for(i=0;i<n;i++){sv2[i]=addD[i];gs.adm[i]+=addD[i];kk2+=addD[i]?'p':'-';}
      det.push({g:'adm',s:'Cenário',l:addD.some(function(x){return x<0;})?'Corte de gasto (cenário)':'Gasto adicional (cenário)',k:kk2,v:zv2,sv:sv2,sim:1});
    }
    function lines(G){
      var o={};
      o.rec=G.rec; o.tv=G.tv; o.pes=G.pes; o.adm=G.adm; o.tl=G.tl; o.est=G.est; o.apo=G.apo;
      o.rl=[];o.ebitda=[];o.lop=[];o.lcx=[];o.dtot=[];
      for(var i=0;i<n;i++){
        o.rl[i]=G.rec[i]-G.tv[i];
        o.ebitda[i]=o.rl[i]-G.pes[i]-G.adm[i];
        o.lop[i]=o.ebitda[i]-G.tl[i];
        o.lcx[i]=o.lop[i]-G.est[i];
        o.dtot[i]=G.tv[i]+G.pes[i]+G.adm[i]+G.tl[i]+G.est[i];
      }
      return o;
    }
    return {B:B,base:lines(gb),sim:lines(gs),det:det,taxRate:taxRate,fr:fr};
  }
  function sum(a,x,y){var s=0;for(var i=x;i<=y;i++)s+=a[i]||0;return s;}
  function cash(R,st,p){
    var a=Math.max(CI+1,p.a), b=p.b; if(b<a){a=CI+1;b=Math.min(N-1,CI+12);}
    var s0=+st.saldo||0, sb=s0, ss=s0, out=[];
    for(var i=a;i<=b;i++){
      var ini=ss; ss+=R.sim.lcx[i]; sb+=R.base.lcx[i];
      out.push({i:i,ini:ini,fim:ss,fimB:sb});
    }
    var min=Infinity,minI=-1,minB=Infinity;
    out.forEach(function(o){if(o.fim<min){min=o.fim;minI=o.i;}if(o.fimB<minB)minB=o.fimB;});
    return {a:a,b:b,rows:out,min:min,minI:minI,minB:minB,fim:out.length?out[out.length-1].fim:s0,fimB:out.length?out[out.length-1].fimB:s0};
  }
  function resumo(st){
    var R=compute(st), p=per(), a=p.a, b=p.b, C=cash(R,st,p);
    var o={R:R,p:p,C:C};
    ['rec','dtot','lop','lcx','ebitda','pes','adm','tv','tl','est'].forEach(function(k){o[k]=sum(R.sim[k],a,b);o[k+'B']=sum(R.base[k],a,b);});
    // equilíbrio: receita mensal que zera o resultado operacional nos meses futuros do período
    var fa=Math.max(a,CI+1), nf=b-fa+1;
    if(nf>0){
      var opx=sum(R.sim.pes,fa,b)+sum(R.sim.adm,fa,b), rb=sum(R.base.rec,fa,b);
      var eq=(opx/nf)/Math.max(0.01,1-R.taxRate);
      o.eq=eq; o.recFutMed=sum(R.sim.rec,fa,b)/nf; o.recFutMedB=rb/nf;
      var eqx=(opx/nf+sum(R.sim.est,fa,b)/nf)/Math.max(0.01,1-R.taxRate); o.eqCx=eqx;
    }
    return o;
  }

  // ── UI helpers ──
  function syncControls(){
    var q=function(id){return document.getElementById(id);};
    q('simxRec').value=S.rec;q('simxRecN').value=S.rec;
    q('simxDesp').value=S.desp;q('simxDespN').value=S.desp;
    q('simxPes').value=S.pes;q('simxAdm').value=S.adm;
    q('simxPesOut').textContent=(S.pes>0?'+':'')+S.pes+'%';q('simxAdmOut').textContent=(S.adm>0?'+':'')+S.adm+'%';
    q('simxAddRec').value=S.addRec;q('simxAddDesp').value=S.addDesp;
    q('simxAddRecDe').value=S.addRecDe;q('simxAddDespDe').value=S.addDespDe;
    q('simxPeriodo').value=S.periodo;q('simxBase').value=S.base;
    q('simxSaldo').value=Math.round(S.saldo);
    q('simxRecNivel').classList.toggle('on',S.recMode==='nivel');q('simxRecAm').classList.toggle('on',S.recMode==='am');
    var ro=q('simxRecOut'),dO=q('simxDespOut');
    ro.textContent=(S.rec>0?'+':'')+String(S.rec).replace('.',',')+'%'+(S.recMode==='am'?' a.m.':'');
    ro.className='simx-val '+(S.rec>0?'pos':S.rec<0?'neg':'zero');
    dO.textContent=(S.desp>0?'+':'')+String(S.desp).replace('.',',')+'%';
    dO.className='simx-val '+(S.desp<0?'pos':S.desp>0?'neg':'zero');
    document.querySelectorAll('#simxDreMode button').forEach(function(b){b.classList.toggle('on',b.dataset.m===S.dreMode);});
    var pr=document.querySelectorAll('#simxPresets .simx-chip');
    pr.forEach(function(c,ix){var P=PRESETS[ix];c.classList.toggle('on',P.rec===S.rec&&P.desp===S.desp&&P.recMode===S.recMode&&!S.pes&&!S.adm&&!S.addRec&&!S.addDesp);});
  }
  function initControls(){
    var ps=periodos(), sel=document.getElementById('simxPeriodo');
    sel.innerHTML=ps.map(function(p){return '<option value="'+p.k+'">'+esc(p.n)+'</option>';}).join('');
    var bs=document.getElementById('simxBase');
    bs.innerHTML=Object.keys(D.bases).map(function(k){return '<option value="'+k+'">'+esc(D.bases[k].label)+'</option>';}).join('');
    var fut=M.map(function(m,i){return i>CI?'<option value="'+m+'">'+L[i]+'</option>':'';}).join('');
    document.getElementById('simxAddRecDe').innerHTML=fut;document.getElementById('simxAddDespDe').innerHTML=fut;
    document.getElementById('simxPresets').innerHTML=PRESETS.map(function(p,ix){
      var t=(p.rec>0?'+':'')+p.rec+'%'+(p.recMode==='am'?' a.m.':'')+' receita · '+(p.desp>0?'+':'')+p.desp+'% gastos';
      return '<button class="simx-chip" title="'+esc(t)+'" onclick="simxPreset('+ix+')">'+esc(p.n)+'</button>';}).join('');
    var saved=lsGet(LSKEY);
    if(saved&&typeof saved==='object'){Object.keys(S).forEach(function(k){if(saved[k]!=null&&k!=='saldo')S[k]=saved[k];});
      if(D.saldo==null&&saved.saldo!=null)S.saldo=+saved.saldo||0;}
    if(!D.bases[S.base])S.base=D.base_default;
    if(M.indexOf(S.addRecDe)<=CI)S.addRecDe=M[CI+1]||M[N-1];
    if(M.indexOf(S.addDespDe)<=CI)S.addDespDe=M[CI+1]||M[N-1];
    if(!periodos().some(function(p){return p.k===S.periodo;}))S.periodo=periodos()[0].k;
    var sf=document.getElementById('simxSaldoFonte'), sn=document.getElementById('simxSaldoNote');
    if(D.saldo!=null){sf.textContent=D.saldo_fonte==='bling'?'Bling':'manual';sn.textContent='Saldo '+(D.saldo_fonte==='bling'?'do Bling':'manual (caixa_config.json)')+' na data do snapshot. Pode sobrescrever para testar.';}
    else{sf.textContent='informar';sn.innerHTML='<span style="color:var(--amber)">O Bling não devolveu saldo de caixa neste snapshot (e o caixa_config.json está vazio).</span> Digite o saldo atual para o fluxo ficar correto — fica salvo neste navegador.';}
    var B0=D.bases[D.base_default];
    document.getElementById('simxHsub').textContent='Base = regra do DRE: realizado até '+L[Math.max(0,CI-1)]+' · '+L[CI]+' em curso · projeção = em aberto no Bling + média (receita recorrente '+B0.rec_n+'m: '+kbrl(B0.media_rec)+'/mês · despesa op. '+B0.desp_n+'m: '+kbrl(B0.media_desp)+'/mês) · choques só de '+(L[CI+1]||'—')+' em diante';
    inited=true;
  }
  function kpi(label,val,sub,delta,color,good){
    var dh='';
    if(delta!=null){var cls=Math.abs(delta)<0.5?'eq':((delta>0)===(good!==false)?'up':'dn');dh='<div class="simx-d '+cls+'">'+(Math.abs(delta)<0.5?'= base':(delta>0?'+':'−')+kbrl(Math.abs(delta)).replace('−','')+' vs base')+'</div>';}
    return '<div class="simx-kpi" style="--kc:var('+color+')"><div class="kl">'+label+'</div><div class="kv" style="color:var('+(color==='--t1'?'--t1':color)+')">'+val+'</div>'+dh+(sub?'<div class="ks">'+sub+'</div>':'')+'</div>';
  }

  function renderKpis(Z){
    var p=Z.p, mg=Z.rec?Z.lop/Z.rec:0, mgB=Z.recB?Z.lopB/Z.recB:0, C=Z.C;
    var h='';
    h+=kpi('Receita · período',kbrl(Z.rec),L[p.a]+' → '+L[p.b],Z.rec-Z.recB,'--green',true);
    h+=kpi('Despesas totais',kbrl(Z.dtot),'op. + tributos + estruturais',Z.dtot-Z.dtotB,'--red',false);
    h+=kpi('Resultado operacional',kbrl(Z.lop),'margem '+pct(mg)+' (base '+pct(mgB)+')',Z.lop-Z.lopB,Z.lop>=0?'--green':'--red',true);
    h+=kpi('Resultado de caixa',kbrl(Z.lcx),'após buy-out/acordo',Z.lcx-Z.lcxB,Z.lcx>=0?'--green':'--red',true);
    h+=kpi('Caixa no fim',kbrl(C.fim),'em '+L[C.b]+' · menor: '+kbrl(C.min)+(C.minI>=0?' ('+L[C.minI]+')':''),C.fim-C.fimB,C.min<0?'--red':'--blue',true);
    var eqs='';
    if(Z.eq){var gap=Z.recFutMed>0?(Z.eq/Z.recFutMed-1):NaN;eqs='vs receita projetada '+kbrl(Z.recFutMed)+'/mês: '+(gap>0?'precisa +'+Math.round(gap*100)+'%':'folga de '+Math.round(-gap*100)+'%')+' · incl. buy-out/acordo: '+kbrl(Z.eqCx)+'/mês';}
    h+=kpi('Receita de equilíbrio',Z.eq?kbrl(Z.eq)+'/mês':'—',eqs,null,'--purple');
    document.getElementById('simxKpis').innerHTML=h;
    var al='';
    if(C.min<0)al+='<div class="simx-alert"><b>Necessidade de caixa:</b> neste cenário o saldo fica negativo a partir de '+L[firstNeg(C)]+' e chega a '+brl(C.min)+' em '+L[C.minI]+'. É o aporte mínimo para atravessar o período sem novas receitas.</div>';
    if(D.saldo==null&&!S.saldo)al+='<div class="simx-alert">Saldo inicial de caixa não informado (Bling não devolveu saldo). O fluxo abaixo parte de R$ 0 — informe o saldo real no card "Premissas do caixa".</div>';
    document.getElementById('simxAlerts').innerHTML=al;
  }
  function firstNeg(C){for(var j=0;j<C.rows.length;j++)if(C.rows[j].fim<0)return C.rows[j].i;return C.minI;}

  function renderNotes(Z){
    var B=Z.R.B, p=Z.p, fa=Math.max(p.a,CI+1);
    var rb=sum(Z.R.base.rec,fa,p.b), rs=sum(Z.R.sim.rec,fa,p.b);
    document.getElementById('simxRecNote').textContent='Base '+B.rec_n+'m ('+B.meses_rec.join('+')+'): '+kbrl(B.media_rec)+'/mês recorrente · projeção no período: '+kbrl(rb)+' → '+kbrl(rs);
    var db=sum(Z.R.base.pes,fa,p.b)+sum(Z.R.base.adm,fa,p.b), ds=sum(Z.R.sim.pes,fa,p.b)+sum(Z.R.sim.adm,fa,p.b);
    document.getElementById('simxDespNote').textContent='Base '+B.desp_n+'m ('+B.meses_desp[0]+'–'+B.meses_desp[B.meses_desp.length-1]+'): '+kbrl(B.media_desp)+'/mês op. · projeção no período: '+kbrl(db)+' → '+kbrl(ds)+' · tributos acompanham a receita';
    document.getElementById('simxChartTag').textContent=L[p.a]+' → '+L[p.b];
  }

  // colunas do DRE conforme modo
  function cols(p){
    var c=[],i;
    if(S.dreMode==='mensal'){for(i=p.a;i<=p.b;i++)c.push({n:L[i],a:i,b:i,f:FUT(i),k:MT[i]});return c;}
    if(S.dreMode==='anual'){var y=null,st=p.a;for(i=p.a;i<=p.b+1;i++){var yy=i<=p.b?M[i].slice(0,4):null;if(yy!==y){if(y!==null)c.push({n:y,a:st,b:i-1,f:FUT(i-1),mix:!FUT(st)&&FUT(i-1)});y=yy;st=i;}}return c;}
    var q=null,s0=p.a;for(i=p.a;i<=p.b+1;i++){var qq=i<=p.b?M[i].slice(0,4)+'T'+(Math.floor((+M[i].slice(5,7)-1)/3)+1):null;if(qq!==q){if(q!==null)c.push({n:q.slice(5)+'/'+q.slice(2,4),a:s0,b:i-1,f:FUT(i-1),mix:!FUT(s0)&&FUT(i-1)});q=qq;s0=i;}}return c;
  }
  var DRE_ROWS=[
    ['rec','Receita bruta de serviços','sub',1],
    ['tv','(−) Tributos sobre vendas (ISS/PIS/COFINS)','',-1],
    ['rl','= Receita líquida','tot',1],
    ['pes','(−) Pessoal · PJ · encargos','',-1],
    ['adm','(−) Serviços estratégicos + Administrativas','',-1],
    ['ebitda','= EBITDA','tot',1],
    ['m_ebitda','→ Margem EBITDA','mg',0],
    ['tl','(−) IRPJ + CSLL','',-1],
    ['lop','= Resultado operacional (lucro líquido)','tot',1],
    ['m_lop','→ Margem líquida','mg',0],
    ['est','(−) Estruturais (buy-out · acordo)','',-1],
    ['lcx','= Resultado de caixa','tot',1],
    ['apo','Aportes / sócios (informativo)','mg',0]
  ];
  var LAST_CSV={};
  function renderDre(Z){
    var p=Z.p, C=cols(p), R=Z.R;
    var tag=S.dreMode==='mensal'?'':(S.dreMode==='tri'?' · trimestral':' · anual');
    document.getElementById('simxDreTitle').textContent='DRE — cenário · '+L[p.a]+' → '+L[p.b]+tag;
    var h='<thead><tr><th>Linha</th>'+C.map(function(c){var t=c.f?(c.mix?'real+proj':'proj'):(c.k==='a'?'aberto':'real');if(c.a<=CI&&c.b>=CI&&S.dreMode==='mensal')t='em curso';return '<th class="'+(c.f?'fut':'')+'">'+c.n+'<span class="kchip">'+t+'</span></th>';}).join('')+'<th>Total cenário</th><th>Total base</th><th>Δ R$</th><th>Δ %</th></tr></thead><tbody>';
    var csv=[['Linha'].concat(C.map(function(c){return c.n;})).concat(['Total cenário','Total base','Delta'])];
    DRE_ROWS.forEach(function(d){
      var key=d[0],cls=d[2],vals=[],base=[],tS=0,tB=0,rowC=[d[1]];
      var isM=key.indexOf('m_')===0;
      C.forEach(function(c){
        var v,b;
        if(isM){var num=key==='m_ebitda'?'ebitda':'lop';var rs=sum(R.sim.rec,c.a,c.b),rbb=sum(R.base.rec,c.a,c.b);v=rs?sum(R.sim[num],c.a,c.b)/rs:NaN;b=rbb?sum(R.base[num],c.a,c.b)/rbb:NaN;}
        else{v=sum(R.sim[key],c.a,c.b);b=sum(R.base[key],c.a,c.b);}
        vals.push(v);base.push(b);
      });
      if(isM){var rsT=sum(R.sim.rec,p.a,p.b),rbT=sum(R.base.rec,p.a,p.b),num2=key==='m_ebitda'?'ebitda':'lop';tS=rsT?sum(R.sim[num2],p.a,p.b)/rsT:NaN;tB=rbT?sum(R.base[num2],p.a,p.b)/rbT:NaN;}
      else{tS=sum(R.sim[key],p.a,p.b);tB=sum(R.base[key],p.a,p.b);}
      h+='<tr class="'+cls+'"><td title="'+esc(d[1])+'">'+esc(d[1])+'</td>';
      vals.forEach(function(v,ix){
        var c=C[ix],chg=!isM&&Math.abs(v-base[ix])>=0.5,neg=!isM&&d[3]===1&&v<0;
        var txt=isM?pct(v):fmt(v);
        h+='<td class="'+(c.f?'fut ':'')+(chg?'chg ':'')+(neg?'neg':'')+'" title="base: '+(isM?pct(base[ix]):brl(base[ix]))+'">'+txt+'</td>';
        rowC.push(isM?(isFinite(v)?(v*100).toFixed(1):''):Math.round(v));
      });
      var dl=tS-tB;
      h+='<td class="cmp'+(!isM&&Math.abs(dl)>=0.5?' chg':'')+'">'+(isM?pct(tS):fmt(tS))+'</td><td class="cmp">'+(isM?pct(tB):fmt(tB))+'</td>';
      if(isM)h+='<td class="cmp">'+(isFinite(tS-tB)&&Math.abs(tS-tB)>=0.0005?((tS-tB)>=0?'+':'−')+Math.abs((tS-tB)*100).toFixed(1).replace('.',',')+' p.p.':'—')+'</td><td class="cmp"></td>';
      else{var good=d[3]===1?dl>=0:dl<=0;h+='<td class="cmp" style="color:var('+(Math.abs(dl)<0.5?'--t3':good?'--green':'--red')+')">'+(Math.abs(dl)<0.5?'—':(dl>0?'+':'−')+Math.abs(Math.round(dl)).toLocaleString('pt-BR'))+'</td><td class="cmp" style="color:var(--t3)">'+(tB&&Math.abs(dl)>=0.5?pct(dl/Math.abs(tB)):'—')+'</td>';}
      h+='</tr>';
      rowC.push(isM?(isFinite(tS)?(tS*100).toFixed(1):''):Math.round(tS));rowC.push(isM?(isFinite(tB)?(tB*100).toFixed(1):''):Math.round(tB));rowC.push(isM?'':Math.round(dl));
      csv.push(rowC);
    });
    document.getElementById('simxDre').innerHTML=h+'</tbody>';
    LAST_CSV.dre=csv;
  }

  function renderCash(Z){
    var C=Z.C, R=Z.R, rows=C.rows;
    document.getElementById('simxCxTag').textContent=(rows.length?L[C.a]+' → '+L[C.b]:'—')+' · saldo inicial '+kbrl(+S.saldo||0);
    var hdr='<thead><tr><th>Fluxo</th>'+rows.map(function(o){return '<th class="fut">'+L[o.i]+'</th>';}).join('')+'<th>Total</th></tr></thead><tbody>';
    var defs=[
      ['Saldo inicial','ini','sub'],
      ['(+) Recebimentos (receita)','rec',''],
      ['(−) Tributos sobre vendas','tv',''],
      ['(−) Pessoal · PJ','pes',''],
      ['(−) Serviços + Adm.','adm',''],
      ['(−) IRPJ + CSLL','tl',''],
      ['= Geração operacional','lop','tot'],
      ['(−) Estruturais (buy-out · acordo)','est',''],
      ['= Fluxo líquido do mês','lcx','tot'],
      ['Saldo final — cenário','fim','sub'],
      ['Saldo final — base','fimB','mg']
    ];
    var csv=[['Fluxo'].concat(rows.map(function(o){return L[o.i];})).concat(['Total'])];
    var h=hdr;
    defs.forEach(function(d){
      var k=d[1],t=0,rc=[d[0]];
      h+='<tr class="'+d[2]+'"><td>'+d[0]+'</td>';
      rows.forEach(function(o,ix){
        var v=(k==='ini'||k==='fim'||k==='fimB')?o[k]:R.sim[k][o.i];
        var sign=(k==='tv'||k==='pes'||k==='adm'||k==='tl'||k==='est')?-1:1;
        var shown=v*sign;
        if(!(k==='ini'||k==='fim'||k==='fimB'))t+=shown;
        var b=(k==='ini'||k==='fim'||k==='fimB')?null:R.base[k][o.i]*sign;
        h+='<td class="'+(shown<0&&(k==='fim'||k==='fimB'||k==='lop'||k==='lcx'||k==='ini')?'neg ':'')+(b!=null&&Math.abs(b-shown)>=0.5?'chg':'')+'"'+(b!=null?' title="base: '+brl(b)+'"':'')+'>'+fmt(shown)+'</td>';
        rc.push(Math.round(shown));
      });
      var tt=(k==='ini')?(rows.length?rows[0].ini:0):(k==='fim'?C.fim:(k==='fimB'?C.fimB:t));
      h+='<td class="cmp'+(tt<0&&k!=='rec'?' neg':'')+'">'+fmt(tt)+'</td></tr>';
      rc.push(Math.round(tt));csv.push(rc);
    });
    document.getElementById('simxCxTbl').innerHTML=h+'</tbody>';
    LAST_CSV.cx=csv;
    var gOp=sum(R.sim.lop,C.a,C.b), gB=sum(R.base.lop,C.a,C.b);
    document.getElementById('simxCxResumo').innerHTML=
      row2('Geração operacional',brl(gOp),gOp-gB)+
      row2('Estruturais a pagar',brl(-sum(R.sim.est,C.a,C.b)),null)+
      row2('Saldo final — cenário',brl(C.fim),C.fim-C.fimB)+
      row2('Menor saldo — cenário',brl(C.min)+(C.minI>=0?' · '+L[C.minI]:''),C.min-C.minB)+
      row2('Menor saldo — base',brl(C.minB),null);
    // gráfico
    var green=cssv('--green','#1e7e4f'),red=cssv('--red','#c0392b'),blue=cssv('--blue','#1a5fa8'),t3=cssv('--t3','#8a9db8'),bd=cssv('--bd','rgba(60,90,130,.1)'),t2=cssv('--t2','#4a5f7a');
    var labels=rows.map(function(o){return L[o.i];});
    var ds=[
      {type:'bar',label:'Fluxo do mês (cenário)',data:rows.map(function(o){return Math.round(R.sim.lcx[o.i]);}),backgroundColor:rows.map(function(o){return alpha(R.sim.lcx[o.i]>=0?green:red,.35);}),borderRadius:3,order:3,yAxisID:'y'},
      {type:'line',label:'Saldo — cenário',data:rows.map(function(o){return Math.round(o.fim);}),borderColor:blue,backgroundColor:alpha(blue,.12),fill:false,tension:.25,pointRadius:3,borderWidth:2.2,order:1},
      {type:'line',label:'Saldo — base',data:rows.map(function(o){return Math.round(o.fimB);}),borderColor:t3,borderDash:[5,4],pointRadius:0,borderWidth:1.6,fill:false,tension:.25,order:2}
    ];
    if(typeof Chart==='undefined')return;
    if(CH.cx)CH.cx.destroy();
    CH.cx=new Chart(document.getElementById('simxCx'),{data:{labels:labels,datasets:ds},options:chartOpts(t2,bd,true)});
  }
  function row2(l,v,d){return '<div class="row"><div class="rl"><strong>'+l+'</strong></div><div style="text-align:right;font-family:var(--mono);font-size:12px">'+v+(d!=null&&Math.abs(d)>=0.5?'<div class="simx-d '+(d>0?'up':'dn')+'" style="margin-top:2px">'+(d>0?'+':'−')+kbrl(Math.abs(d))+'</div>':'')+'</div></div>';}
  function chartOpts(t2,bd,legend){
    return {responsive:true,maintainAspectRatio:false,animation:{duration:250},interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:!!legend,labels:{color:t2,boxWidth:10,font:{size:10}}},tooltip:{callbacks:{label:function(c){return ' '+c.dataset.label+': '+brl(c.parsed.y);}}}},
      scales:{x:{grid:{display:false},ticks:{color:t2}},y:{grid:{color:bd},ticks:{color:t2,callback:function(v){return kbrl(v);}}}}};
  }
  function renderBar(Z){
    if(typeof Chart==='undefined')return;
    var p=Z.p,R=Z.R,idx=[];for(var i=p.a;i<=p.b;i++)idx.push(i);
    var green=cssv('--green','#1e7e4f'),red=cssv('--red','#c0392b'),blue=cssv('--blue','#1a5fa8'),t3=cssv('--t3','#8a9db8'),bd=cssv('--bd','rgba(60,90,130,.1)'),t2=cssv('--t2','#4a5f7a');
    var ds=[
      {type:'bar',label:'Receita (cenário)',data:idx.map(function(i){return Math.round(R.sim.rec[i]);}),backgroundColor:idx.map(function(i){return alpha(green,FUT(i)?.45:.85);}),borderRadius:3,order:3},
      {type:'bar',label:'Despesas totais (cenário)',data:idx.map(function(i){return Math.round(R.sim.dtot[i]);}),backgroundColor:idx.map(function(i){return alpha(red,FUT(i)?.4:.8);}),borderRadius:3,order:3},
      {type:'line',label:'Resultado de caixa (cenário)',data:idx.map(function(i){return Math.round(R.sim.lcx[i]);}),borderColor:blue,backgroundColor:blue,pointRadius:2.5,borderWidth:2.2,tension:.25,order:1},
      {type:'line',label:'Resultado de caixa (base)',data:idx.map(function(i){return Math.round(R.base.lcx[i]);}),borderColor:t3,borderDash:[5,4],pointRadius:0,borderWidth:1.6,tension:.25,order:2}
    ];
    if(CH.bar)CH.bar.destroy();
    CH.bar=new Chart(document.getElementById('simxBar'),{data:{labels:idx.map(function(i){return L[i];}),datasets:ds},options:chartOpts(t2,bd,false)});
  }

  function renderDet(Z){
    var p=Z.p,R=Z.R,idx=[];for(var i=p.a;i<=p.b;i++)idx.push(i);
    var h='<thead><tr><th>Grupo / cliente / fornecedor</th>'+idx.map(function(i){var t=FUT(i)?'proj':(i===CI?'em curso':(MT[i]==='a'?'aberto':'real'));return '<th class="'+(FUT(i)?'fut':'')+'">'+L[i]+'<span class="kchip">'+t+'</span></th>';}).join('')+'<th>Total</th><th>Base</th></tr></thead><tbody>';
    var csv=[['Grupo','Item'].concat(idx.map(function(i){return L[i];})).concat(['Total','Base'])];
    D.groups.forEach(function(G){
      var gk=G.k, items=R.det.filter(function(r){return r.g===gk;});
      var gv=idx.map(function(i){return R.sim[gk][i];}), gbv=idx.map(function(i){return R.base[gk][i];});
      var tS=gv.reduce(function(a,b){return a+b;},0), tB=gbv.reduce(function(a,b){return a+b;},0);
      if(Math.abs(tS)<0.5&&Math.abs(tB)<0.5)return;
      var op=!!OPEN[gk];
      h+='<tr class="sub grp'+(op?' open':'')+'" onclick="simxToggleG(\''+gk+'\')"><td title="'+esc(G.label)+'">'+esc(G.label)+' <span style="color:var(--t3);font-weight:400;font-size:10px">('+items.length+')</span></td>'+
        gv.map(function(v,ix){var i=idx[ix];return '<td class="'+(FUT(i)?'fut ':'')+(Math.abs(v-gbv[ix])>=0.5?'chg':'')+'" title="base: '+brl(gbv[ix])+'">'+fmt(v)+'</td>';}).join('')+
        '<td class="cmp'+(Math.abs(tS-tB)>=0.5?' chg':'')+'">'+fmt(tS)+'</td><td class="cmp">'+fmt(tB)+'</td></tr>';
      csv.push([G.label,'(total)'].concat(gv.map(Math.round)).concat([Math.round(tS),Math.round(tB)]));
      var its=items.map(function(r){var s=0,b=0;idx.forEach(function(i){s+=r.sv[i];b+=r.v[i];});return {r:r,s:s,b:b};}).filter(function(o){return Math.abs(o.s)>=0.5||Math.abs(o.b)>=0.5;});
      its.sort(function(x,y){return Math.abs(y.s)-Math.abs(x.s);});
      its.forEach(function(o){
        var r=o.r;
        csv.push([G.label,r.l].concat(idx.map(function(i){return Math.round(r.sv[i]);})).concat([Math.round(o.s),Math.round(o.b)]));
        if(!op)return;
        h+='<tr class="it"><td title="'+esc(r.l+(r.s?' · '+r.s:''))+'">'+esc(r.l)+(r.sim?' <span class="bge bgb" style="font-size:9px">cenário</span>':'')+'</td>'+
          idx.map(function(i){var v=r.sv[i],k=r.k[i]||'-';var tt=k==='p'?'projeção':k==='a'?'em aberto':k==='t'?'relatório Totvs':k==='r'?'realizado':'';return '<td class="'+(FUT(i)?'fut ':'')+(Math.abs(v-r.v[i])>=0.5?'chg':'')+'" title="'+tt+(Math.abs(v-r.v[i])>=0.5?' · base: '+brl(r.v[i]):'')+'"'+(k==='p'?' style="font-style:italic"':'')+'>'+fmt(v)+'</td>';}).join('')+
          '<td class="cmp">'+fmt(o.s)+'</td><td class="cmp">'+fmt(o.b)+'</td></tr>';
      });
    });
    var res=idx.map(function(i){return R.sim.lcx[i];}),resB=idx.map(function(i){return R.base.lcx[i];});
    var tr=res.reduce(function(a,b){return a+b;},0),trB=resB.reduce(function(a,b){return a+b;},0);
    h+='<tr class="tot"><td>= Resultado de caixa</td>'+res.map(function(v,ix){return '<td class="'+(FUT(idx[ix])?'fut ':'')+(v<0?'neg':'')+'" title="base: '+brl(resB[ix])+'">'+fmt(v)+'</td>';}).join('')+'<td class="cmp'+(tr<0?' neg':'')+'">'+fmt(tr)+'</td><td class="cmp'+(trB<0?' neg':'')+'">'+fmt(trB)+'</td></tr>';
    csv.push(['Resultado de caixa',''].concat(res.map(Math.round)).concat([Math.round(tr),Math.round(trB)]));
    document.getElementById('simxDet').innerHTML=h+'</tbody>';
    LAST_CSV.det=csv;
  }

  function stateLabel(st){
    var s=(st.rec>0?'+':'')+st.rec+'%'+(st.recMode==='am'?' a.m.':'')+' receita · '+(st.desp>0?'+':'')+st.desp+'% gastos';
    if(st.pes)s+=' · pessoal '+(st.pes>0?'+':'')+st.pes+'%';
    if(st.adm)s+=' · adm '+(st.adm>0?'+':'')+st.adm+'%';
    if(st.addRec)s+=' · receita nova '+kbrl(st.addRec)+'/mês';
    if(st.addDesp)s+=' · '+(st.addDesp<0?'corte ':'gasto extra ')+kbrl(Math.abs(st.addDesp))+'/mês';
    if(st.base!==D.base_default)s+=' · base '+(D.bases[st.base]?D.bases[st.base].rec_n+'m':'?');
    return s;
  }
  function renderCmp(){
    var pins=lsGet(LSPIN)||[], all=[{nome:'Base (DRE)',st:Object.assign({},S,{rec:0,desp:0,pes:0,adm:0,addRec:0,addDesp:0,recMode:'nivel',base:D.base_default}),fixed:1},{nome:'Cenário atual',st:S,cur:1}].concat(pins.map(function(p,ix){p.ix=ix;return p;}));
    var h='<thead><tr><th>Cenário</th><th>Receita</th><th>Despesas</th><th>Resultado op.</th><th>Margem</th><th>Resultado caixa</th><th>Caixa final</th><th>Menor saldo</th><th></th></tr></thead><tbody>';
    all.forEach(function(p){
      var st=Object.assign({},p.st,{periodo:S.periodo,saldo:S.saldo,dreMode:S.dreMode});
      var Z=resumo(st);
      h+='<tr'+(p.cur?' class="sub"':'')+'><td class="nm" title="'+esc(stateLabel(st))+'">'+esc(p.nome)+'<div style="font-size:9.5px;color:var(--t3);font-weight:400;font-family:var(--mono)">'+esc(stateLabel(st))+'</div></td>'+
        '<td>'+kbrl(Z.rec)+'</td><td>'+kbrl(Z.dtot)+'</td><td class="'+(Z.lop<0?'neg':'')+'">'+kbrl(Z.lop)+'</td><td>'+pct(Z.rec?Z.lop/Z.rec:NaN)+'</td><td class="'+(Z.lcx<0?'neg':'')+'">'+kbrl(Z.lcx)+'</td><td class="'+(Z.C.fim<0?'neg':'')+'">'+kbrl(Z.C.fim)+'</td><td class="'+(Z.C.min<0?'neg':'')+'">'+kbrl(Z.C.min)+'</td>'+
        '<td>'+(p.ix!=null?'<button class="simx-btn simx-noprint" style="padding:2px 8px;font-size:10px" onclick="simxLoadPin('+p.ix+')">carregar</button> <button class="simx-btn simx-noprint" style="padding:2px 8px;font-size:10px" onclick="simxDelPin('+p.ix+')">×</button>':'')+'</td></tr>';
    });
    document.getElementById('simxCmp').innerHTML=h+'</tbody>';
  }

  function render(){
    if(!inited)initControls();
    syncControls();
    var Z=resumo(S);
    renderNotes(Z);renderKpis(Z);renderBar(Z);renderDre(Z);renderCash(Z);renderDet(Z);renderCmp();
    lsSet(LSKEY,S);
  }
  var tmr=null;
  function schedule(){if(tmr)cancelAnimationFrame(tmr);tmr=requestAnimationFrame(function(){tmr=null;render();});}

  window.simRender=function(){render();};
  window.simxSlide=function(k,v){v=parseFloat(String(v).replace(',','.'));if(!isFinite(v))v=0;v=Math.max(-90,Math.min(200,v));S[k]=v;schedule();};
  window.simxSet=function(k,v){S[k]=v;schedule();};
  window.simxPreset=function(ix){var P=PRESETS[ix];S.rec=P.rec;S.recMode=P.recMode;S.desp=P.desp;S.pes=0;S.adm=0;S.addRec=0;S.addDesp=0;schedule();};
  window.simxReset=function(){S.rec=0;S.recMode='nivel';S.desp=0;S.pes=0;S.adm=0;S.addRec=0;S.addDesp=0;S.base=D.base_default;schedule();};
  window.simxToggleAdv=function(){var a=document.getElementById('simxAdv');a.classList.toggle('open');document.getElementById('simxAdvBtn').textContent=a.classList.contains('open')?'ajuste fino ▴':'ajuste fino ▾';};
  window.simxToggleG=function(g){OPEN[g]=!OPEN[g];renderDet(resumo(S));};
  window.simxExpand=function(on){D.groups.forEach(function(G){OPEN[G.k]=!!on;});renderDet(resumo(S));};
  window.simxPin=function(){
    var pins=lsGet(LSPIN)||[];
    var nome=null;try{nome=window.prompt('Nome do cenário:','Cenário '+(pins.length+1));}catch(e){nome=null;}
    if(nome===null)nome='Cenário '+(pins.length+1);
    pins.push({nome:String(nome).slice(0,40),st:{base:S.base,rec:S.rec,recMode:S.recMode,desp:S.desp,pes:S.pes,adm:S.adm,addRec:S.addRec,addRecDe:S.addRecDe,addDesp:S.addDesp,addDespDe:S.addDespDe}});
    if(pins.length>8)pins.shift();
    lsSet(LSPIN,pins);renderCmp();
  };
  window.simxDelPin=function(ix){var pins=lsGet(LSPIN)||[];pins.splice(ix,1);lsSet(LSPIN,pins);renderCmp();};
  window.simxLoadPin=function(ix){var pins=lsGet(LSPIN)||[];var p=pins[ix];if(!p)return;Object.keys(p.st).forEach(function(k){S[k]=p.st[k];});schedule();};
  window.simxPrint=function(){document.body.classList.add('simx-printing');setTimeout(function(){try{window.print();}finally{setTimeout(function(){document.body.classList.remove('simx-printing');},300);}},50);};
  window.simxCsv=function(which){
    var rows=LAST_CSV[which];if(!rows)return;
    var txt=rows.map(function(r){return r.map(function(c){c=String(c==null?'':c);return /[;"\n]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c;}).join(';');}).join('\n');
    try{var blob=new Blob(['﻿'+txt],{type:'text/csv;charset=utf-8'});var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='simulador_'+which+'_'+D.gerado+'.csv';document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},500);}catch(e){}
  };
  // re-render ao trocar tema (cores dos gráficos vêm das CSS vars)
  try{new MutationObserver(function(){var pg=document.getElementById('pg-simulador');if(pg&&pg.classList.contains('active'))schedule();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});}catch(e){}
  // expõe o motor p/ testes
  window.__simx={compute:compute,resumo:resumo,S:S,D:D};
})();
"""
