"""
chat_widget.py — Widget de busca local + chat com IA pro dashboard Easy_Financas.

Injetado antes de </body> pelo build-html.py.

Arquitetura (v2 — tool use):
- Snapshot SUMMARY enxuto (~2 KB) é enviado no system prompt — KPIs globais.
- Dataset FULL (~30 KB) fica embedado no JS do navegador (NÃO enviado por default).
- A IA Claude usa TOOLS pra consultar slices do dataset sob demanda:
   consultar_fornecedor, consultar_cliente, listar_vencidos, consultar_dre,
   consultar_auditoria, consultar_totvs, top_categoria, buscar.
- Loop multi-turn no JS: envia → resposta tem tool_use → resolve local → envia
  tool_result → repete até stop_reason=end_turn.
- Modelo default: claude-haiku-4-5 (rápido, barato, faz tool use bem).

Uso no build-html.py:

    from chat_widget import build_chat_widget, build_chat_context
    ctx = build_chat_context(rows=…, pagar_data=…, …)
    html = html.replace("</body>", build_chat_widget(ctx) + "\n</body>")
"""

from __future__ import annotations
import json


# ──────────────────────────────────────────────
# Contexto pra IA — produz {summary, full}
# ──────────────────────────────────────────────

def build_chat_context(
    *,
    rows: list[dict],
    months: list,
    pagar_data: list[dict],
    cx_data: list[dict],
    dre_data: list[dict],
    recebido_data: list[dict] | None = None,
    dre_detail: dict[str, dict] | None = None,
    audit_findings: list | None = None,
    totvs_summary: dict | None = None,
    totvs_por_mes: dict | None = None,
    pagas_raw: list[dict] | None = None,
    recebidas_raw: list[dict] | None = None,
    em_aberto_raw: list[dict] | None = None,
    receber_aberto_raw: list[dict] | None = None,
    updated_at: str = "",
) -> dict:
    """Monta {summary, full} pra alimentar o widget.

    `summary` (~2 KB) entra no system prompt da IA.
    `full` (~30 KB) fica no JS pras tools consultarem localmente.
    """
    # ── Totais agregados
    pagar_aberto_total = sum(float(p.get("val", 0)) for p in pagar_data)
    pagar_vencidos = [p for p in pagar_data if p.get("window") == "vencido"]
    pagar_vencidos_total = sum(float(p.get("val", 0)) for p in pagar_vencidos)

    receber_aberto_total = sum(float(c.get("val", 0)) for c in cx_data)
    receber_vencidos = [c for c in cx_data if c.get("sit") == "Atrasada"]
    receber_vencidos_total = sum(float(c.get("val", 0)) for c in receber_vencidos)

    # ── Meses do histórico (chaves tipo "mai26" no rows)
    _reserved = {"id", "n", "cat", "st", "sc", "total"}
    _mn = {"jan":1,"fev":2,"mar":3,"abr":4,"mai":5,"jun":6,
           "jul":7,"ago":8,"set":9,"out":10,"nov":11,"dez":12}
    month_keys: list[str] = []
    if rows and isinstance(rows[0], dict):
        month_keys = [
            k for k in rows[0].keys()
            if isinstance(k, str) and k not in _reserved
            and 4 <= len(k) <= 6
            and k[:-2].isalpha() and k[-2:].isdigit()
        ]
        month_keys.sort(key=lambda k: (int(k[-2:]), _mn.get(k[:-2].lower(), 0)))

    # ── Top 5 fornecedores em aberto (preview pro summary)
    forn_total: dict[str, float] = {}
    for p in pagar_data:
        n = (p.get("n") or "?").strip()
        forn_total[n] = forn_total.get(n, 0) + float(p.get("val", 0))
    top5_forn = sorted(forn_total.items(), key=lambda x: -x[1])[:5]

    cli_total: dict[str, float] = {}
    for c in cx_data:
        label = (c.get("c") or "?").strip()
        n = label.split(" — doc")[0].strip() if " — doc" in label else label
        cli_total[n] = cli_total.get(n, 0) + float(c.get("val", 0))
    top5_cli = sorted(cli_total.items(), key=lambda x: -x[1])[:5]

    # ── Audit resumo
    audit_resumo = {"ok": 0, "warn": 0, "error": 0, "info": 0}
    audit_full: list[dict] = []
    if audit_findings:
        for f in audit_findings:
            s = getattr(f, "status", "info")
            audit_resumo[s] = audit_resumo.get(s, 0) + 1
            try:
                d = f.to_dict() if hasattr(f, "to_dict") else dict(f)
            except Exception:
                d = {}
            audit_full.append({
                "status":      d.get("status", "info"),
                "titulo":      str(d.get("title", ""))[:140],
                "detalhe":     str(d.get("detail", ""))[:280],
                "competencia": d.get("competencia"),
                "valor":       d.get("value"),
                "esperado":    d.get("expected"),
                "diff":        d.get("diff"),
                "acao":        (str(d.get("action") or "")[:200]) or None,
                "categoria":   d.get("category", "tributario"),
            })

    # ── Totvs por mês compactado
    totvs_meses: list[dict] = []
    if totvs_por_mes:
        for mk in sorted(totvs_por_mes.keys()):
            v = totvs_por_mes[mk]
            if isinstance(v, dict):
                totvs_meses.append({"mes": mk, **{k: round(float(vv), 2) if isinstance(vv, (int, float)) else vv for k, vv in v.items()}})
            else:
                totvs_meses.append({"mes": mk, "valor": round(float(v), 2)})

    # ── DRE completo
    dre_full = []
    if dre_data:
        for d in dre_data:
            rec = float(d.get("rec", 0))
            desp = float(d.get("desp", 0))
            dre_full.append({
                "mes":     d.get("month", ""),
                "label":   d.get("label", ""),
                "tipo":    d.get("tipo", ""),
                "receita": round(rec, 2),
                "despesa": round(desp, 2),
                "lucro":   round(rec - desp, 2),
            })

    # ── Histórico pago 4m por fornecedor (rows completo)
    historico_pago: list[dict] = []
    if rows:
        for r in rows:
            total_r = float(r.get("total", 0))
            if total_r <= 0:
                continue
            por_mes = {mk: round(float(r.get(mk, 0)), 2) for mk in month_keys}
            historico_pago.append({
                "fornecedor": r.get("n", "?"),
                "categoria":  r.get("cat", ""),
                "status":     r.get("st", ""),
                "total_4m":   round(total_r, 2),
                "por_mes":    por_mes,
            })
        historico_pago.sort(key=lambda x: -x["total_4m"])

    # ── Mapa pra enriquecer pagar/receber com historico/observacoes do raw
    def _key(contato, doc, venc):
        return (contato.strip(), str(doc or "").strip(), str(venc or "")[:10])

    pagar_raw_idx = {}
    for r in (em_aberto_raw or []):
        contato = (r.get("contato_nome") or r.get("nomeContato") or "").strip()
        doc = r.get("numero_documento") or r.get("numeroDocumento") or ""
        venc = r.get("vencimento") or r.get("dataVencimento") or ""
        pagar_raw_idx[_key(contato, doc, venc)] = {
            "historico":   (r.get("historico") or "")[:120],
            "observacoes": (r.get("observacoes") or "")[:120],
            "categoria_id": r.get("categoria_id"),
            "competencia": (r.get("competencia") or "")[:10],
            "venc_iso":    str(venc)[:10],
        }

    receber_raw_idx = {}
    for r in (receber_aberto_raw or []):
        contato = (r.get("contato_nome") or r.get("nomeContato") or "").strip()
        doc = r.get("numero_documento") or r.get("numeroDocumento") or ""
        venc = r.get("vencimento") or r.get("dataVencimento") or ""
        receber_raw_idx[_key(contato, doc, venc)] = {
            "historico":   (r.get("historico") or "")[:120],
            "observacoes": (r.get("observacoes") or "")[:120],
            "venc_iso":    str(venc)[:10],
        }

    # Index alternativo por contato+documento (sem venc) pra fallback
    pagar_by_cdoc: dict[tuple, list] = {}
    for r in (em_aberto_raw or []):
        contato = (r.get("contato_nome") or "").strip()
        doc = str(r.get("numero_documento") or r.get("numeroDocumento") or "").strip()
        pagar_by_cdoc.setdefault((contato, doc), []).append(r)
    receber_by_cdoc: dict[tuple, list] = {}
    for r in (receber_aberto_raw or []):
        contato = (r.get("contato_nome") or "").strip()
        doc = str(r.get("numero_documento") or r.get("numeroDocumento") or "").strip()
        receber_by_cdoc.setdefault((contato, doc), []).append(r)

    # ── A pagar completo (com historico/observacoes do raw quando disponível)
    pagar_full = []
    for p in pagar_data:
        nome = (p.get("n") or "").strip()
        doc = str(p.get("doc") or "").strip()
        # Tenta achar match nos raw por contato + doc (fallback)
        cands = pagar_by_cdoc.get((nome, doc), [])
        extra = {}
        if cands:
            r = cands[0]  # melhor que nada
            extra = {
                "historico":   (r.get("historico") or "")[:120],
                "observacoes": (r.get("observacoes") or "")[:120],
            }
        pagar_full.append({
            "fornecedor": nome or "?",
            "categoria":  p.get("cat", ""),
            "documento":  doc,
            "vencimento": p.get("venc", ""),
            "janela":     p.get("window", ""),
            "valor":      round(float(p.get("val", 0)), 2),
            "historico":  extra.get("historico", ""),
            "observacoes": extra.get("observacoes", ""),
        })

    receber_full = []
    for c in cx_data:
        label = (c.get("c") or "?").strip()
        nome = label.split(" — doc")[0].strip() if " — doc" in label else label
        doc = label.split(" — doc")[1].strip() if " — doc" in label else ""
        cands = receber_by_cdoc.get((nome, doc), [])
        extra = {}
        if cands:
            r = cands[0]
            extra = {
                "historico":   (r.get("historico") or "")[:120],
                "observacoes": (r.get("observacoes") or "")[:120],
            }
        receber_full.append({
            "cliente":     nome,
            "documento":   doc,
            "vencimento":  c.get("venc", ""),
            "situacao":    c.get("sit", ""),
            "valor":       round(float(c.get("val", 0)), 2),
            "historico":   extra.get("historico", ""),
            "observacoes": extra.get("observacoes", ""),
        })

    # ── Recebidos por cliente (histórico)
    recebido_full = []
    if recebido_data:
        for r in recebido_data:
            recebido_full.append({
                "cliente":      r.get("n", "?"),
                "total_pago":   round(float(r.get("val", 0)), 2),
                "n_pagamentos": int(r.get("count", 0)),
            })

    # Categorias (do rows agregadas) + meses
    cat_agg: dict[str, float] = {}
    for r in rows or []:
        cat = (r.get("cat") or "Outros").strip()
        for mk in month_keys:
            cat_agg[cat] = cat_agg.get(cat, 0) + float(r.get(mk, 0))
    categorias = [
        {"categoria": c, "total_4m": round(v, 2)}
        for c, v in sorted(cat_agg.items(), key=lambda x: -x[1])
        if v > 0
    ]

    # Coberturas computadas mais abaixo (depois que pagamentos_hist/recebimentos_hist existem)
    # ────────────── SUMMARY (~2 KB, vai no system prompt) ──────────────
    summary = {
        "atualizado_em": updated_at,
        "kpis": {
            "a_pagar_aberto":     round(pagar_aberto_total, 2),
            "a_pagar_vencido":    round(pagar_vencidos_total, 2),
            "n_pagar_vencidos":   len(pagar_vencidos),
            "n_pagar_aberto":     len(pagar_data),
            "a_receber_aberto":   round(receber_aberto_total, 2),
            "a_receber_vencido":  round(receber_vencidos_total, 2),
            "n_receber_vencidos": len(receber_vencidos),
            "n_receber_aberto":   len(cx_data),
            "n_pagos_hist":       len(pagas_raw or []),
            "n_recebidos_hist":   len(recebidas_raw or []),
            "n_findings_audit":   sum(audit_resumo.values()),
        },
        "meses_historico_pago": month_keys,
        "coberturas": None,  # preenchido abaixo
        "preview": {
            "top5_a_pagar_fornecedor": [{"nome": n, "total": round(v, 2)} for n, v in top5_forn],
            "top5_a_receber_cliente":  [{"nome": n, "total": round(v, 2)} for n, v in top5_cli],
            "audit_resumo":            audit_resumo,
            "totvs_resumo":            totvs_summary or {},
        },
        "abas_dashboard": [
            "Overview", "Caixa", "Contas a Pagar / a Receber",
            "DRE / P&L", "Comissões Totvs", "Auditoria Tributária",
        ],
    }

    # ── Raw histórico compactado (pagamentos individuais)
    def _money(v):
        """Converte 'R$ 1.234,56' ou '1234.56' ou float em float."""
        if v is None or v == "":
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        # Remove R$, espaços, símbolos
        for c in ("R$", "r$", " ", "\xa0"):
            s = s.replace(c, "")
        # Se tem ',' e '.', vírgula é decimal (formato BR): "1.234,56" → "1234.56"
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except Exception:
            return 0.0

    pagamentos_hist = []
    for p in (pagas_raw or []):
        contato = (p.get("contato_nome") or p.get("nomeContato") or "").strip()
        valor = _money(p.get("valor"))
        if valor <= 0:
            continue
        pagamentos_hist.append({
            "data":      str(p.get("vencimento") or p.get("dataVencimento") or "")[:10],
            "fornecedor": contato,
            "valor":     round(valor, 2),
            "doc":       (p.get("numero_documento") or p.get("numeroDocumento") or "")[:25],
            "categoria": (p.get("categoria_descricao") or p.get("categoriaDescricao") or "").strip(),
            "historico": (p.get("historico") or "")[:80],
        })

    recebimentos_hist = []
    for r in (recebidas_raw or []):
        contato = (r.get("contato_nome") or r.get("nomeContato") or "").strip()
        valor = _money(r.get("valor"))
        if valor <= 0:
            continue
        recebimentos_hist.append({
            "data":     str(r.get("vencimento") or r.get("dataVencimento") or "")[:10],
            "cliente":  contato,
            "valor":    round(valor, 2),
            "doc":      (r.get("numero_documento") or r.get("numeroDocumento") or "")[:25],
        })

    # ── Coberturas temporais (pra IA saber o range sem chamar tool extra)
    def _rng(items, key, slice_to=7):
        ds = sorted({(str(it.get(key, "")) or "")[:slice_to] for it in items if it.get(key)})
        ds = [d for d in ds if d]
        if not ds: return None
        return {"primeiro": ds[0], "ultimo": ds[-1], "n_meses": len(ds)}

    summary["coberturas"] = {
        "pagamentos_realizados":   _rng(pagamentos_hist, "data"),
        "recebimentos_realizados": _rng(recebimentos_hist, "data"),
        "dre":                     ({"primeiro": dre_full[0]["mes"], "ultimo": dre_full[-1]["mes"], "n_meses": len(dre_full)} if dre_full else None),
        "totvs":                   ({"primeiro": totvs_meses[0]["mes"], "ultimo": totvs_meses[-1]["mes"], "n_meses": len(totvs_meses)} if totvs_meses else None),
        "n_pagar_aberto":          len(pagar_data),
        "n_receber_aberto":        len(cx_data),
    }

    # ────────────── FULL (vai no JS, alimenta tools) ──────────────
    full = {
        # Em aberto (a pagar / a receber — futuro)
        "pagar_aberto":     pagar_full,
        "receber_aberto":   receber_full,
        # Histórico raw (TODOS os pagamentos/recebimentos já realizados — filtrável por data)
        "pagamentos_historico":   pagamentos_hist,
        "recebimentos_historico": recebimentos_hist,
        # Agregações pré-computadas pelo dashboard (janela definida pelo build-html)
        "historico_pago_recente":  historico_pago,
        "recebido_por_cliente":    recebido_full,
        "categorias_recente":      categorias,
        "meses_recentes":          month_keys,  # ex: ["fev26","mar26","abr26","mai26"]
        # DRE / Audit / Totvs
        "dre":              dre_full,
        "auditoria":        audit_full,
        "totvs_por_mes":    totvs_meses,
        "totvs_resumo":     totvs_summary or {},
    }

    return {"summary": summary, "full": full}


# ──────────────────────────────────────────────
# HTML/CSS/JS do widget
# ──────────────────────────────────────────────

def build_chat_widget(ctx: dict) -> str:
    """Retorna HTML+CSS+JS pro widget (tool use loop)."""
    summary = ctx.get("summary", {})
    full = ctx.get("full", {})

    summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    full_json    = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
    summary_safe = summary_json.replace("</", "<\\/")
    full_safe    = full_json.replace("</", "<\\/")

    return f"""
<!-- ── EF CHAT WIDGET v2 (tool use) ─────────────────────────────── -->
<style>
#ef-chat-fab{{
  position:fixed;bottom:20px;right:20px;z-index:8500;
  width:54px;height:54px;border-radius:50%;border:none;
  background:var(--blue);color:#fff;cursor:pointer;
  box-shadow:0 4px 14px rgba(60,90,130,0.25);
  font-size:22px;display:flex;align-items:center;justify-content:center;
  transition:transform .15s, box-shadow .15s;
}}
#ef-chat-fab:hover{{transform:scale(1.06);box-shadow:0 6px 18px rgba(60,90,130,0.35)}}
#ef-chat-fab.open{{background:var(--t1)}}
#ef-chat-panel{{
  position:fixed;bottom:84px;right:20px;z-index:8499;
  width:min(440px,calc(100vw - 40px));max-height:min(680px,calc(100vh - 120px));
  background:var(--s1);border:1px solid var(--bd2);border-radius:14px;
  box-shadow:0 8px 32px rgba(60,90,130,0.2);
  display:none;flex-direction:column;overflow:hidden;font-family:var(--sans);
}}
#ef-chat-panel.open{{display:flex}}
.ef-chat-head{{
  display:flex;align-items:center;gap:8px;padding:12px 14px;
  border-bottom:1px solid var(--bd);background:var(--s2);
}}
.ef-chat-head .ttl{{font-size:13px;font-weight:600;color:var(--t1);flex:1}}
.ef-chat-head .sub{{font-size:10px;color:var(--t3);font-family:var(--mono)}}
.ef-chat-head button{{
  background:none;border:none;color:var(--t2);cursor:pointer;padding:4px 6px;
  border-radius:6px;font-size:14px;
}}
.ef-chat-head button:hover{{background:var(--s3);color:var(--t1)}}
.ef-chat-input-wrap{{padding:10px 14px;border-bottom:1px solid var(--bd)}}
.ef-chat-input{{
  width:100%;padding:9px 12px;border:1px solid var(--bd2);border-radius:10px;
  background:var(--s2);color:var(--t1);font-size:13px;font-family:var(--sans);
  outline:none;transition:border-color .15s;
}}
.ef-chat-input:focus{{border-color:var(--blue);background:var(--s1)}}
.ef-chat-body{{flex:1;overflow-y:auto;padding:12px 14px;background:var(--s1)}}
.ef-chat-body::-webkit-scrollbar{{width:6px}}
.ef-chat-body::-webkit-scrollbar-thumb{{background:var(--bd2);border-radius:3px}}
.ef-search-section{{margin-bottom:12px}}
.ef-search-hdr{{
  font-size:10px;color:var(--t3);text-transform:uppercase;
  letter-spacing:.08em;font-weight:500;margin-bottom:6px;font-family:var(--mono);
}}
.ef-search-results{{font-size:12px;color:var(--t2);line-height:1.5}}
.ef-search-hit{{
  padding:6px 8px;background:var(--s2);border-radius:6px;margin-bottom:4px;
  cursor:pointer;border:1px solid transparent;transition:border-color .12s;
}}
.ef-search-hit:hover{{border-color:var(--bd2);background:var(--s3)}}
.ef-search-hit .ctx{{font-size:10px;color:var(--t3);margin-top:2px;font-family:var(--mono)}}
.ef-search-empty{{color:var(--t3);font-size:11px;font-style:italic}}
.ef-chat-msg{{margin-bottom:10px;font-size:12.5px;line-height:1.55}}
.ef-chat-msg.user{{
  background:var(--blue);color:#fff;padding:8px 12px;border-radius:12px 12px 4px 12px;
  margin-left:36px;
}}
.ef-chat-msg.ai{{
  background:var(--s2);color:var(--t1);padding:8px 12px;border-radius:12px 12px 12px 4px;
  margin-right:36px;
}}
.ef-chat-msg.ai p{{margin-bottom:6px}}
.ef-chat-msg.ai p:last-child{{margin-bottom:0}}
.ef-chat-msg.ai code{{background:rgba(0,0,0,.06);padding:1px 5px;border-radius:4px;font-family:var(--mono);font-size:11px}}
.ef-chat-msg.ai strong{{font-weight:600}}
.ef-chat-msg.ai table{{border-collapse:collapse;margin:6px 0;font-size:11.5px}}
.ef-chat-msg.ai th, .ef-chat-msg.ai td{{border:1px solid var(--bd2);padding:4px 8px;text-align:left}}
.ef-chat-msg.ai th{{background:var(--s3);font-weight:600}}
.ef-chat-msg.err{{color:var(--red);font-size:11px;font-style:italic;margin-right:36px}}
.ef-chat-msg.thinking{{color:var(--t3);font-size:11px;font-style:italic;margin-right:36px;display:flex;align-items:center;gap:6px}}
.ef-chat-msg.thinking::before{{content:'●';animation:efpulse 1.2s infinite}}
@keyframes efpulse{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
.ef-chat-msg.toolcall{{
  font-size:10.5px;color:var(--t3);font-family:var(--mono);font-style:italic;
  margin-right:36px;padding:3px 8px;background:rgba(60,90,130,0.04);border-radius:4px;
  border-left:2px solid var(--blue);
}}
.ef-chat-actions{{
  padding:8px 14px;border-top:1px solid var(--bd);
  display:flex;gap:8px;align-items:center;background:var(--s2);
}}
.ef-chat-actions button{{
  padding:7px 12px;border-radius:8px;border:1px solid var(--bd2);
  background:var(--s1);color:var(--t1);font-size:11px;font-weight:500;
  cursor:pointer;font-family:var(--sans);transition:all .12s;
}}
.ef-chat-actions button:hover{{background:var(--blue);color:#fff;border-color:var(--blue)}}
.ef-chat-actions button.primary{{background:var(--blue);color:#fff;border-color:var(--blue);flex:1}}
.ef-chat-actions button.primary:hover{{opacity:.88}}
.ef-chat-actions button:disabled{{opacity:.5;cursor:not-allowed}}
.ef-config-modal{{
  position:fixed;inset:0;z-index:9500;display:none;align-items:center;justify-content:center;
  background:rgba(20,30,50,.5);
}}
.ef-config-modal.open{{display:flex}}
.ef-config-box{{
  background:var(--s1);border:1px solid var(--bd2);border-radius:14px;
  padding:24px 28px;width:min(420px,90vw);
  box-shadow:0 8px 32px rgba(60,90,130,0.2);
}}
.ef-config-box h3{{font-size:15px;font-weight:600;color:var(--t1);margin-bottom:4px}}
.ef-config-box p{{font-size:11px;color:var(--t3);margin-bottom:14px;line-height:1.5}}
.ef-config-box label{{display:block;font-size:11px;color:var(--t2);margin-bottom:4px;font-weight:500}}
.ef-config-box input, .ef-config-box select{{
  width:100%;padding:9px 12px;border:1px solid var(--bd2);border-radius:8px;
  background:var(--s2);color:var(--t1);font-size:13px;outline:none;
  font-family:var(--mono);margin-bottom:12px;
}}
.ef-config-box input:focus{{border-color:var(--blue)}}
.ef-config-box .row{{display:flex;gap:8px;margin-top:6px}}
.ef-config-box button{{
  padding:9px 14px;border-radius:8px;border:none;cursor:pointer;
  font-family:var(--sans);font-weight:500;font-size:12px;transition:opacity .12s;
}}
.ef-config-box button.save{{background:var(--blue);color:#fff;flex:1}}
.ef-config-box button.cancel{{background:var(--s3);color:var(--t1)}}
.ef-config-box button:hover{{opacity:.85}}
.ef-config-box .note{{font-size:10px;color:var(--t3);font-style:italic;margin-top:4px}}
.ef-mark{{background:rgba(255,210,80,0.4);padding:0 2px;border-radius:2px}}

/* Modal de Memória */
.ef-mem-modal{{position:fixed;inset:0;z-index:9500;display:none;align-items:center;justify-content:center;background:rgba(20,30,50,.5)}}
.ef-mem-modal.open{{display:flex}}
.ef-mem-box{{
  background:var(--s1);border:1px solid var(--bd2);border-radius:14px;
  padding:18px 22px;width:min(560px,92vw);max-height:85vh;
  display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(60,90,130,0.2);
}}
.ef-mem-box h3{{font-size:15px;font-weight:600;color:var(--t1);margin-bottom:4px}}
.ef-mem-box .sub{{font-size:11px;color:var(--t3);margin-bottom:14px;line-height:1.5}}
.ef-mem-tabs{{display:flex;border-bottom:1px solid var(--bd);margin-bottom:12px}}
.ef-mem-tab{{
  padding:8px 14px;font-size:12px;font-weight:500;color:var(--t2);
  cursor:pointer;border-bottom:2px solid transparent;font-family:var(--sans);background:none;border-top:none;border-left:none;border-right:none;
}}
.ef-mem-tab.active{{color:var(--blue);border-bottom-color:var(--blue)}}
.ef-mem-content{{flex:1;overflow-y:auto;min-height:200px}}
.ef-mem-pane{{display:none}}
.ef-mem-pane.active{{display:block}}
.ef-mem-pane textarea{{
  width:100%;min-height:160px;padding:10px 12px;
  border:1px solid var(--bd2);border-radius:8px;background:var(--s2);color:var(--t1);
  font-size:12px;font-family:var(--mono);outline:none;resize:vertical;line-height:1.5;
}}
.ef-mem-pane textarea:focus{{border-color:var(--blue)}}
.ef-mem-list{{display:flex;flex-direction:column;gap:6px;max-height:300px;overflow-y:auto}}
.ef-mem-item{{
  padding:8px 12px;background:var(--s2);border-radius:8px;
  font-size:12px;color:var(--t1);display:flex;gap:8px;align-items:flex-start;
}}
.ef-mem-item .txt{{flex:1;line-height:1.4}}
.ef-mem-item .meta{{font-size:10px;color:var(--t3);font-family:var(--mono);margin-top:2px}}
.ef-mem-item .del{{background:none;border:none;color:var(--t3);cursor:pointer;padding:2px 6px;font-size:13px}}
.ef-mem-item .del:hover{{color:var(--red)}}
.ef-mem-empty{{color:var(--t3);font-size:11px;font-style:italic;text-align:center;padding:24px 0}}
.ef-mem-add{{display:flex;gap:6px;margin-top:10px}}
.ef-mem-add input{{
  flex:1;padding:8px 12px;border:1px solid var(--bd2);border-radius:8px;
  background:var(--s2);color:var(--t1);font-size:12px;font-family:var(--sans);outline:none;
}}
.ef-mem-add input:focus{{border-color:var(--blue)}}
.ef-mem-add button{{padding:8px 14px;border-radius:8px;border:none;background:var(--blue);color:#fff;font-size:11px;cursor:pointer;font-family:var(--sans);font-weight:500}}
.ef-mem-add select{{padding:8px 10px;border:1px solid var(--bd2);border-radius:8px;background:var(--s2);font-size:11px;color:var(--t1)}}
.ef-mem-foot{{display:flex;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--bd)}}
.ef-mem-foot button{{padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-family:var(--sans);font-weight:500;font-size:12px}}
.ef-mem-foot button.save{{background:var(--blue);color:#fff;flex:1}}
.ef-mem-foot button.close{{background:var(--s3);color:var(--t1)}}
.ef-mem-foot button:hover{{opacity:.85}}
</style>

<button id="ef-chat-fab" title="Buscar ou perguntar à IA">💬</button>

<div id="ef-chat-panel">
  <div class="ef-chat-head">
    <span class="ttl">Buscar / Perguntar à IA</span>
    <span class="sub" id="ef-chat-modelbadge"></span>
    <button id="ef-chat-mem" title="Memória persistente">🧠</button>
    <button id="ef-chat-cfg" title="Configurar chave Anthropic">⚙️</button>
    <button id="ef-chat-close" title="Fechar">✕</button>
  </div>
  <div class="ef-chat-input-wrap">
    <input id="ef-chat-input" class="ef-chat-input" type="text"
      placeholder="Buscar fornecedor, valor, conta… ou faça uma pergunta">
  </div>
  <div class="ef-chat-body" id="ef-chat-body">
    <div class="ef-search-section" id="ef-search-section">
      <div class="ef-search-hdr">Como usar</div>
      <div class="ef-search-results" style="color:var(--t2);font-size:11.5px;line-height:1.6">
        Digite no campo acima pra <b>buscar</b> em todas as tabelas do dashboard.
        Para perguntar em linguagem natural, clique em <b>Perguntar à IA</b>.
        A IA tem acesso aos dados completos via ferramentas — pode consultar
        fornecedor, cliente, DRE, auditoria, comissões Totvs.
      </div>
    </div>
  </div>
  <div class="ef-chat-actions">
    <button id="ef-chat-ask" class="primary">Perguntar à IA</button>
    <button id="ef-chat-clear" title="Limpar conversa">Limpar</button>
  </div>
</div>

<div id="ef-mem-modal" class="ef-mem-modal">
  <div class="ef-mem-box">
    <h3>🧠 Memória persistente da IA</h3>
    <p class="sub">Tudo salvo só no seu browser (<code>localStorage</code>). A IA usa essas informações em toda pergunta — como se "lembrasse" de você entre sessões.</p>
    <div class="ef-mem-tabs">
      <button class="ef-mem-tab active" data-pane="perfil">Perfil do negócio</button>
      <button class="ef-mem-tab" data-pane="notas">Anotações</button>
      <button class="ef-mem-tab" data-pane="classif">Classificações</button>
    </div>
    <div class="ef-mem-content">
      <div class="ef-mem-pane active" data-pane="perfil">
        <p class="sub">Contexto fixo sobre o negócio, suas preferências, formato de resposta esperado. Vai junto em todo system prompt.</p>
        <textarea id="ef-mem-perfil" placeholder="Ex: Sou Leandro, sócio da Easy Analytics (TI/SaaS, ~R$3M receita anual). Foco em fluxo de caixa Q3-Q4. Prefiro respostas curtas com bullets + 1 ação concreta. Sazonalidade alta em dez/jan (Totvs paga comissões). Sócio sai via buy-out parcelado (Romulo)."></textarea>
      </div>
      <div class="ef-mem-pane" data-pane="notas">
        <p class="sub">Anotações livres sobre fornecedores, clientes, contratos. A IA consulta essas notas em toda pergunta.</p>
        <div class="ef-mem-list" id="ef-mem-notas-list"></div>
        <div class="ef-mem-add">
          <input id="ef-mem-nota-input" type="text" placeholder="Ex: Geremias Ferreira Lima = acordo trabalhista 20x R$10K (jan/26→ago/27)">
          <button id="ef-mem-nota-add">+ Adicionar</button>
        </div>
      </div>
      <div class="ef-mem-pane" data-pane="classif">
        <p class="sub">Sobrescreve a categoria do Bling. Útil quando o Bling tá com "(sem categoria)" mas você sabe o que é.</p>
        <div class="ef-mem-list" id="ef-mem-classif-list"></div>
        <div class="ef-mem-add">
          <select id="ef-mem-classif-tipo">
            <option value="fornecedor">Fornecedor</option>
            <option value="cliente">Cliente</option>
          </select>
          <input id="ef-mem-classif-nome" type="text" placeholder="Nome (parcial)">
          <input id="ef-mem-classif-cat" type="text" placeholder="Categoria">
          <button id="ef-mem-classif-add">+</button>
        </div>
      </div>
    </div>
    <div class="ef-mem-foot">
      <button class="save" id="ef-mem-save">Salvar</button>
      <button class="close" id="ef-mem-close">Fechar</button>
    </div>
  </div>
</div>

<div id="ef-config-modal" class="ef-config-modal">
  <div class="ef-config-box">
    <h3>Configurar IA</h3>
    <p>Cole sua chave da Anthropic API. Ela fica salva só no seu browser
    (<code>localStorage</code>) e nunca sai pro servidor — exceto pra api.anthropic.com.</p>
    <label>Chave Anthropic (começa com <code>sk-ant-</code>)</label>
    <input id="ef-cfg-key" type="password" placeholder="sk-ant-…" autocomplete="off">
    <label>Modelo</label>
    <select id="ef-cfg-model">
      <option value="claude-haiku-4-5-20251001">Haiku 4.5 — rápido e barato (default, recomendado)</option>
      <option value="claude-sonnet-4-6">Sonnet 4.6 — mais inteligente (10x mais caro)</option>
      <option value="claude-opus-4-6">Opus 4.6 — máximo de qualidade (30x mais caro)</option>
    </select>
    <div class="note">Pegue uma chave em
      <code>console.anthropic.com</code> → API Keys.</div>
    <div class="row">
      <button class="save" id="ef-cfg-save">Salvar</button>
      <button class="cancel" id="ef-cfg-cancel">Cancelar</button>
    </div>
  </div>
</div>

<script>
(function(){{
  // ── Dados ──
  const EF_SUMMARY = {summary_safe};
  const EF_FULL    = {full_safe};

  // ── DOM ──
  const fab    = document.getElementById('ef-chat-fab');
  const panel  = document.getElementById('ef-chat-panel');
  const input  = document.getElementById('ef-chat-input');
  const body   = document.getElementById('ef-chat-body');
  const askBtn = document.getElementById('ef-chat-ask');
  const clrBtn = document.getElementById('ef-chat-clear');
  const cfgBtn = document.getElementById('ef-chat-cfg');
  const closeBtn = document.getElementById('ef-chat-close');
  const cfgModal = document.getElementById('ef-config-modal');
  const cfgKey   = document.getElementById('ef-cfg-key');
  const cfgModel = document.getElementById('ef-cfg-model');
  const cfgSave  = document.getElementById('ef-cfg-save');
  const cfgCancel= document.getElementById('ef-cfg-cancel');
  const modelBadge = document.getElementById('ef-chat-modelbadge');
  const searchSection = document.getElementById('ef-search-section');

  // ── Storage ──
  const LS_KEY = 'ef_chat_apikey';
  const LS_MOD = 'ef_chat_model';
  const LS_HIST = 'ef_chat_history';
  const LS_PERFIL = 'ef_chat_perfil';
  const LS_NOTAS  = 'ef_chat_notas';
  const LS_CLASSIF= 'ef_chat_classif';
  const DEFAULT_MODEL = 'claude-haiku-4-5-20251001';

  // ── Memória persistente ──
  function getPerfil(){{ return localStorage.getItem(LS_PERFIL) || ''; }}
  function setPerfil(s){{ if(s) localStorage.setItem(LS_PERFIL, s); else localStorage.removeItem(LS_PERFIL); }}
  function getNotas(){{
    try{{ return JSON.parse(localStorage.getItem(LS_NOTAS) || '[]'); }}
    catch(e){{ return []; }}
  }}
  function setNotas(arr){{ localStorage.setItem(LS_NOTAS, JSON.stringify(arr)); }}
  function addNota(texto){{
    const arr = getNotas();
    arr.push({{id: Date.now(), texto: texto, criado_em: new Date().toISOString().slice(0,10)}});
    setNotas(arr);
    return arr[arr.length-1];
  }}
  function delNota(id){{
    const arr = getNotas().filter(n => n.id != id);
    setNotas(arr);
  }}
  function getClassif(){{
    try{{ return JSON.parse(localStorage.getItem(LS_CLASSIF) || '[]'); }}
    catch(e){{ return []; }}
  }}
  function setClassif(arr){{ localStorage.setItem(LS_CLASSIF, JSON.stringify(arr)); }}
  function addClassif(tipo, nome, categoria){{
    const arr = getClassif();
    // remove duplicata
    const filt = arr.filter(c => !(c.tipo === tipo && c.nome.toLowerCase() === nome.toLowerCase()));
    filt.push({{id: Date.now(), tipo, nome, categoria, criado_em: new Date().toISOString().slice(0,10)}});
    setClassif(filt);
    return filt[filt.length-1];
  }}
  function delClassif(id){{
    const arr = getClassif().filter(c => c.id != id);
    setClassif(arr);
  }}

  // Monta bloco de "MEMÓRIA PERSISTENTE" pra anexar ao system prompt
  function getMemoryBlock(){{
    const perfil = getPerfil().trim();
    const notas = getNotas();
    const classif = getClassif();
    if(!perfil && !notas.length && !classif.length) return '';
    let txt = '\\n\\n=== MEMÓRIA PERSISTENTE DO USUÁRIO (use em toda resposta) ===';
    if(perfil) txt += '\\n\\n[Perfil do negócio]\\n' + perfil;
    if(notas.length){{
      txt += '\\n\\n[Anotações registradas]';
      notas.forEach(n => {{ txt += '\\n- ' + n.texto; }});
    }}
    if(classif.length){{
      txt += '\\n\\n[Classificações sobre fornecedores/clientes (sobrescrevem o Bling)]';
      classif.forEach(c => {{ txt += '\\n- ' + c.tipo + ' "' + c.nome + '" = categoria "' + c.categoria + '"'; }});
    }}
    txt += '\\n=== FIM DA MEMÓRIA ===';
    return txt;
  }}

  function getKey(){{ return localStorage.getItem(LS_KEY) || ''; }}
  function getModel(){{ return localStorage.getItem(LS_MOD) || DEFAULT_MODEL; }}
  function setKey(k){{
    if(k) localStorage.setItem(LS_KEY, k);
    else localStorage.removeItem(LS_KEY);
  }}
  function setModel(m){{ localStorage.setItem(LS_MOD, m); }}

  function modelShortLabel(m){{
    if(m.includes('haiku')) return 'Haiku 4.5';
    if(m.includes('sonnet')) return 'Sonnet 4.6';
    if(m.includes('opus')) return 'Opus 4.6';
    return m;
  }}
  function updateModelBadge(){{
    modelBadge.textContent = getKey() ? modelShortLabel(getModel()) : 'sem chave';
  }}

  // ── Toggle painel ──
  fab.addEventListener('click', () => {{
    const isOpen = panel.classList.toggle('open');
    fab.classList.toggle('open', isOpen);
    fab.textContent = isOpen ? '✕' : '💬';
    if(isOpen){{ input.focus(); updateModelBadge(); }}
  }});
  closeBtn.addEventListener('click', () => {{
    panel.classList.remove('open'); fab.classList.remove('open'); fab.textContent = '💬';
  }});

  // ── Config modal ──
  cfgBtn.addEventListener('click', () => {{
    cfgKey.value = getKey(); cfgModel.value = getModel();
    cfgModal.classList.add('open');
  }});
  cfgCancel.addEventListener('click', () => cfgModal.classList.remove('open'));
  cfgSave.addEventListener('click', () => {{
    setKey(cfgKey.value.trim()); setModel(cfgModel.value);
    cfgModal.classList.remove('open'); updateModelBadge();
  }});

  // ── Memória modal ──
  const memBtn   = document.getElementById('ef-chat-mem');
  const memModal = document.getElementById('ef-mem-modal');
  const memClose = document.getElementById('ef-mem-close');
  const memSave  = document.getElementById('ef-mem-save');
  const memPerfil= document.getElementById('ef-mem-perfil');
  const memNotaIn= document.getElementById('ef-mem-nota-input');
  const memNotaAdd = document.getElementById('ef-mem-nota-add');
  const memNotasList = document.getElementById('ef-mem-notas-list');
  const memClassifTipo = document.getElementById('ef-mem-classif-tipo');
  const memClassifNome = document.getElementById('ef-mem-classif-nome');
  const memClassifCat  = document.getElementById('ef-mem-classif-cat');
  const memClassifAdd  = document.getElementById('ef-mem-classif-add');
  const memClassifList = document.getElementById('ef-mem-classif-list');

  function renderMemNotas(){{
    const notas = getNotas();
    if(!notas.length){{ memNotasList.innerHTML = '<div class="ef-mem-empty">Nenhuma anotação ainda. Adicione abaixo.</div>'; return; }}
    memNotasList.innerHTML = notas.slice().reverse().map(n =>
      `<div class="ef-mem-item"><div class="txt">${{(n.texto||'').replace(/</g,'&lt;')}}<div class="meta">${{n.criado_em||''}}</div></div><button class="del" data-id="${{n.id}}" title="Apagar">✕</button></div>`
    ).join('');
    memNotasList.querySelectorAll('.del').forEach(b => b.addEventListener('click', () => {{ delNota(b.dataset.id); renderMemNotas(); }}));
  }}
  function renderMemClassif(){{
    const arr = getClassif();
    if(!arr.length){{ memClassifList.innerHTML = '<div class="ef-mem-empty">Nenhuma classificação ainda.</div>'; return; }}
    memClassifList.innerHTML = arr.slice().reverse().map(c =>
      `<div class="ef-mem-item"><div class="txt"><b>${{c.tipo}}</b> · ${{(c.nome||'').replace(/</g,'&lt;')}} → <code>${{(c.categoria||'').replace(/</g,'&lt;')}}</code><div class="meta">${{c.criado_em||''}}</div></div><button class="del" data-id="${{c.id}}" title="Apagar">✕</button></div>`
    ).join('');
    memClassifList.querySelectorAll('.del').forEach(b => b.addEventListener('click', () => {{ delClassif(b.dataset.id); renderMemClassif(); }}));
  }}

  memBtn.addEventListener('click', () => {{
    memPerfil.value = getPerfil();
    renderMemNotas();
    renderMemClassif();
    memModal.classList.add('open');
  }});
  memClose.addEventListener('click', () => memModal.classList.remove('open'));
  memSave.addEventListener('click', () => {{
    setPerfil(memPerfil.value.trim());
    memModal.classList.remove('open');
  }});
  // Tabs
  document.querySelectorAll('.ef-mem-tab').forEach(t => {{
    t.addEventListener('click', () => {{
      document.querySelectorAll('.ef-mem-tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.ef-mem-pane').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      document.querySelector('.ef-mem-pane[data-pane="' + t.dataset.pane + '"]').classList.add('active');
    }});
  }});
  memNotaAdd.addEventListener('click', () => {{
    const t = memNotaIn.value.trim();
    if(!t) return;
    addNota(t); memNotaIn.value = ''; renderMemNotas();
  }});
  memNotaIn.addEventListener('keydown', e => {{ if(e.key === 'Enter') memNotaAdd.click(); }});
  memClassifAdd.addEventListener('click', () => {{
    const nome = memClassifNome.value.trim();
    const cat  = memClassifCat.value.trim();
    if(!nome || !cat) return;
    addClassif(memClassifTipo.value, nome, cat);
    memClassifNome.value = ''; memClassifCat.value = '';
    renderMemClassif();
  }});

  // ── Busca local (mantém igual ao v1) ──
  let searchIndex = null;
  function buildIndex(){{
    if(searchIndex) return searchIndex;
    const items = [];
    document.querySelectorAll('table tr').forEach(tr => {{
      const txt = tr.innerText.replace(/\\s+/g, ' ').trim();
      if(!txt || txt.length < 4) return;
      let parent = tr.closest('[data-aba],.pg,section,article');
      let ctx = '';
      if(parent){{
        const hdr = parent.querySelector('h1,h2,h3,.tab-title,.section-title');
        if(hdr) ctx = hdr.innerText.trim().slice(0, 40);
      }}
      items.push({{txt, el: tr, ctx}});
    }});
    document.querySelectorAll('.card,.kpi,.metric,[class*="card"]').forEach(c => {{
      const txt = c.innerText.replace(/\\s+/g, ' ').trim();
      if(!txt || txt.length < 4 || txt.length > 400) return;
      items.push({{txt, el: c, ctx: 'KPI'}});
    }});
    searchIndex = items;
    return items;
  }}
  function doSearch(term){{
    if(!term){{
      searchSection.innerHTML = `
        <div class="ef-search-hdr">Como usar</div>
        <div class="ef-search-results" style="color:var(--t2);font-size:11.5px;line-height:1.6">
          Digite no campo acima pra <b>buscar</b> em todas as tabelas do dashboard.
          Para perguntar em linguagem natural, clique em <b>Perguntar à IA</b>.
        </div>`;
      return [];
    }}
    const idx = buildIndex();
    const t = term.toLowerCase();
    const hits = idx.filter(it => it.txt.toLowerCase().includes(t)).slice(0, 30);
    const html = hits.length
      ? hits.map(h => {{
          const safe = h.txt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
          const rg = new RegExp('('+term.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&')+')', 'gi');
          const hl = safe.replace(rg, '<span class="ef-mark">$1</span>');
          return `<div class="ef-search-hit" data-key="${{h.ctx}}">${{hl.slice(0,160)}}${{hl.length>160?'…':''}}<div class="ctx">${{h.ctx||'tabela'}}</div></div>`;
        }}).join('')
      : '<div class="ef-search-empty">Nada encontrado.</div>';
    searchSection.innerHTML = `<div class="ef-search-hdr">Resultados (${{hits.length}})</div><div class="ef-search-results">${{html}}</div>`;
    searchSection.querySelectorAll('.ef-search-hit').forEach((el, i) => {{
      el.addEventListener('click', () => {{
        const target = hits[i].el;
        target.scrollIntoView({{behavior:'smooth', block:'center'}});
        target.style.outline = '2px solid var(--amber)';
        setTimeout(() => target.style.outline = '', 1600);
      }});
    }});
    return hits;
  }}
  input.addEventListener('input', e => doSearch(e.target.value.trim()));

  // ── TOOLS (rodam local sobre EF_FULL) ──
  function normName(s){{ return (s||'').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,''); }}

  const TOOLS = {{
    consultar_fornecedor: function(args){{
      const termo = normName(args.nome || '');
      if(!termo) return {{erro: 'parâmetro "nome" obrigatório'}};
      // Histórico TOTAL (todos os pagamentos já feitos, sem janela)
      const histRaw = (EF_FULL.pagamentos_historico || []).filter(r => normName(r.fornecedor).includes(termo));
      const histTotal = histRaw.reduce((s, r) => s + r.valor, 0);
      // Agrega por mês (YYYY-MM)
      const porMes = {{}};
      histRaw.forEach(r => {{
        const m = (r.data || '').slice(0,7);
        if(m) porMes[m] = (porMes[m] || 0) + r.valor;
      }});
      const mesesOrd = Object.keys(porMes).sort();
      // Agregação recente (janela do dashboard)
      const recente = (EF_FULL.historico_pago_recente || []).filter(r => normName(r.fornecedor).includes(termo));
      // Em aberto
      const aberto = (EF_FULL.pagar_aberto || []).filter(r => normName(r.fornecedor).includes(termo));
      const tot_aberto = aberto.reduce((s, r) => s + r.valor, 0);
      const venc = aberto.filter(r => r.janela === 'vencido');
      const datas = aberto.map(r => r.vencimento).filter(Boolean).sort();
      if(histRaw.length === 0 && aberto.length === 0) return {{achados: 0, termo: args.nome}};
      return {{
        termo: args.nome,
        historico_total: {{
          total_pago: Math.round(histTotal * 100) / 100,
          n_pagamentos: histRaw.length,
          primeira_data: mesesOrd[0] || null,
          ultima_data:   mesesOrd[mesesOrd.length-1] || null,
          por_mes: Object.fromEntries(mesesOrd.map(m => [m, Math.round(porMes[m]*100)/100])),
        }},
        historico_recente: recente.map(h => ({{
          fornecedor: h.fornecedor, categoria: h.categoria, status: h.status,
          total: h.total_4m, por_mes: h.por_mes, janela_meses: EF_FULL.meses_recentes,
        }})),
        em_aberto: {{
          total: Math.round(tot_aberto * 100) / 100,
          n_parcelas: aberto.length,
          vencido: Math.round(venc.reduce((s,r)=>s+r.valor,0) * 100) / 100,
          n_vencidas: venc.length,
          prox_venc: datas[0] || null,
          ultimo_venc: datas[datas.length-1] || null,
          parcelas: aberto.slice(0, 30),
        }},
      }};
    }},
    consultar_cliente: function(args){{
      const termo = normName(args.nome || '');
      if(!termo) return {{erro: 'parâmetro "nome" obrigatório'}};
      // Histórico TOTAL
      const histRaw = (EF_FULL.recebimentos_historico || []).filter(r => normName(r.cliente).includes(termo));
      const histTotal = histRaw.reduce((s, r) => s + r.valor, 0);
      const porMes = {{}};
      histRaw.forEach(r => {{
        const m = (r.data || '').slice(0,7);
        if(m) porMes[m] = (porMes[m] || 0) + r.valor;
      }});
      const mesesOrd = Object.keys(porMes).sort();
      // Agregação por cliente (snapshot atual)
      const agreg = (EF_FULL.recebido_por_cliente || []).filter(r => normName(r.cliente).includes(termo));
      // Em aberto
      const aberto = (EF_FULL.receber_aberto || []).filter(r => normName(r.cliente).includes(termo));
      const atrasadas = aberto.filter(r => r.situacao === 'Atrasada');
      if(histRaw.length === 0 && aberto.length === 0) return {{achados: 0, termo: args.nome}};
      return {{
        termo: args.nome,
        historico_total: {{
          total_recebido: Math.round(histTotal * 100) / 100,
          n_recebimentos: histRaw.length,
          primeira_data: mesesOrd[0] || null,
          ultima_data:   mesesOrd[mesesOrd.length-1] || null,
          por_mes: Object.fromEntries(mesesOrd.map(m => [m, Math.round(porMes[m]*100)/100])),
        }},
        em_aberto: {{
          total: Math.round(aberto.reduce((s,r)=>s+r.valor,0) * 100) / 100,
          n_parcelas: aberto.length,
          n_atrasadas: atrasadas.length,
          atrasado: Math.round(atrasadas.reduce((s,r)=>s+r.valor,0) * 100) / 100,
          parcelas: aberto.slice(0, 30),
        }},
      }};
    }},
    consultar_pagamentos: function(args){{
      // Filtra pagamentos históricos por data (YYYY-MM-DD), mes (YYYY-MM), fornecedor (parcial), categoria (parcial)
      let items = (EF_FULL.pagamentos_historico || []).slice();
      if(args.fornecedor){{ const t = normName(args.fornecedor); items = items.filter(r => normName(r.fornecedor).includes(t)); }}
      if(args.categoria){{ const t = normName(args.categoria); items = items.filter(r => normName(r.categoria).includes(t)); }}
      if(args.mes){{ items = items.filter(r => (r.data||'').slice(0,7) === args.mes); }}
      if(args.data_inicio){{ items = items.filter(r => r.data >= args.data_inicio); }}
      if(args.data_fim){{ items = items.filter(r => r.data <= args.data_fim); }}
      const total = items.reduce((s,r)=>s+r.valor,0);
      // Resumo por mês
      const porMes = {{}};
      items.forEach(r => {{ const m = (r.data||'').slice(0,7); if(m) porMes[m] = (porMes[m]||0) + r.valor; }});
      return {{
        filtros: {{fornecedor: args.fornecedor, categoria: args.categoria, mes: args.mes,
                  data_inicio: args.data_inicio, data_fim: args.data_fim}},
        n: items.length,
        total: Math.round(total * 100) / 100,
        por_mes: Object.fromEntries(Object.keys(porMes).sort().map(m => [m, Math.round(porMes[m]*100)/100])),
        items: items.sort((a,b) => (b.data||'').localeCompare(a.data||'')).slice(0, 30),
      }};
    }},
    consultar_recebimentos: function(args){{
      let items = (EF_FULL.recebimentos_historico || []).slice();
      if(args.cliente){{ const t = normName(args.cliente); items = items.filter(r => normName(r.cliente).includes(t)); }}
      if(args.mes){{ items = items.filter(r => (r.data||'').slice(0,7) === args.mes); }}
      if(args.data_inicio){{ items = items.filter(r => r.data >= args.data_inicio); }}
      if(args.data_fim){{ items = items.filter(r => r.data <= args.data_fim); }}
      const total = items.reduce((s,r)=>s+r.valor,0);
      const porMes = {{}};
      items.forEach(r => {{ const m = (r.data||'').slice(0,7); if(m) porMes[m] = (porMes[m]||0) + r.valor; }});
      return {{
        filtros: {{cliente: args.cliente, mes: args.mes,
                  data_inicio: args.data_inicio, data_fim: args.data_fim}},
        n: items.length,
        total: Math.round(total * 100) / 100,
        por_mes: Object.fromEntries(Object.keys(porMes).sort().map(m => [m, Math.round(porMes[m]*100)/100])),
        items: items.sort((a,b) => (b.data||'').localeCompare(a.data||'')).slice(0, 30),
      }};
    }},
    listar_vencidos: function(args){{
      const tipo = args.tipo || 'pagar';
      if(tipo === 'pagar'){{
        const items = (EF_FULL.pagar_aberto || []).filter(r => r.janela === 'vencido')
          .sort((a,b) => b.valor - a.valor).slice(0, 30);
        return {{tipo, n: items.length, total: items.reduce((s,r)=>s+r.valor,0), items}};
      }} else {{
        const items = (EF_FULL.receber_aberto || []).filter(r => r.situacao === 'Atrasada')
          .sort((a,b) => b.valor - a.valor).slice(0, 30);
        return {{tipo, n: items.length, total: items.reduce((s,r)=>s+r.valor,0), items}};
      }}
    }},
    consultar_dre: function(args){{
      let dre = EF_FULL.dre || [];
      // Filtros opcionais: mes exato (YYYY-MM), data_inicio/data_fim (YYYY-MM)
      if(args.mes) dre = dre.filter(d => d.mes === args.mes);
      if(args.data_inicio) dre = dre.filter(d => d.mes >= args.data_inicio);
      if(args.data_fim) dre = dre.filter(d => d.mes <= args.data_fim);
      if(args.tipo) dre = dre.filter(d => d.tipo === args.tipo);
      // Se sem filtros, default = últimos N meses
      if(!args.mes && !args.data_inicio && !args.data_fim && !args.tipo){{
        const n = args.meses_recentes || 6;
        dre = dre.slice(-n);
      }}
      return {{
        filtros: {{mes: args.mes, data_inicio: args.data_inicio, data_fim: args.data_fim, tipo: args.tipo}},
        n_meses: dre.length,
        meses: dre,
        agregado: dre.length > 0 ? {{
          receita_total: Math.round(dre.reduce((s,d)=>s+d.receita,0) * 100) / 100,
          despesa_total: Math.round(dre.reduce((s,d)=>s+d.despesa,0) * 100) / 100,
          lucro_total:   Math.round(dre.reduce((s,d)=>s+d.lucro,0) * 100) / 100,
        }} : null,
      }};
    }},
    cobertura_temporal: function(){{
      // Retorna o range que cada fonte cobre — pra IA saber o que pode perguntar
      function rng(items, key){{
        const ds = items.map(it => (it[key] || '').slice(0,7)).filter(Boolean).sort();
        return ds.length ? {{primeiro: ds[0], ultimo: ds[ds.length-1], n_meses: new Set(ds).size}} : null;
      }}
      return {{
        pagamentos_realizados:    rng(EF_FULL.pagamentos_historico || [], 'data'),
        recebimentos_realizados:  rng(EF_FULL.recebimentos_historico || [], 'data'),
        a_pagar_projetado:        {{n_parcelas: (EF_FULL.pagar_aberto||[]).length, datas_br: 'dd/mes/aa - use consultar_fornecedor pra ver vencimentos exatos'}},
        a_receber_projetado:      {{n_parcelas: (EF_FULL.receber_aberto||[]).length, datas_br: 'dd/mes/aa - use consultar_cliente pra ver vencimentos exatos'}},
        dre:                      (EF_FULL.dre||[]).length ? {{primeiro: EF_FULL.dre[0].mes, ultimo: EF_FULL.dre[EF_FULL.dre.length-1].mes, n_meses: EF_FULL.dre.length}} : null,
        totvs:                    (EF_FULL.totvs_por_mes||[]).length ? {{primeiro: EF_FULL.totvs_por_mes[0].mes, ultimo: EF_FULL.totvs_por_mes[EF_FULL.totvs_por_mes.length-1].mes, n_meses: EF_FULL.totvs_por_mes.length}} : null,
        nota: 'Se a pergunta envolver mês fora desses ranges, informe ao usuário que o dado não está no snapshot (limitação do Bling/extração).',
      }};
    }},
    consultar_auditoria: function(args){{
      const sev = args.severidade;
      let items = EF_FULL.auditoria || [];
      if(sev) items = items.filter(f => f.status === sev);
      items = items.slice(0, 25);
      return {{filtro: sev || 'todos', n: items.length, findings: items}};
    }},
    consultar_totvs: function(args){{
      const mes = args.mes;
      if(mes){{
        const m = (EF_FULL.totvs_por_mes || []).find(t => t.mes === mes);
        return m ? {{mes, dados: m}} : {{mes, erro: 'mes não encontrado'}};
      }}
      return {{
        resumo: EF_FULL.totvs_resumo || {{}},
        por_mes: EF_FULL.totvs_por_mes || [],
      }};
    }},
    top_categoria: function(args){{
      const cat = args.categoria;
      const limit = args.limit || 10;
      if(cat){{
        const cn = normName(cat);
        const items = (EF_FULL.historico_pago_recente || [])
          .filter(r => normName(r.categoria).includes(cn))
          .sort((a,b) => b.total_4m - a.total_4m).slice(0, limit);
        return {{
          categoria: cat,
          janela: 'meses recentes do dashboard (' + (EF_FULL.meses_recentes || []).join(',') + ')',
          n: items.length, fornecedores: items
        }};
      }}
      return {{
        janela: 'meses recentes do dashboard (' + (EF_FULL.meses_recentes || []).join(',') + ')',
        categorias: (EF_FULL.categorias_recente || []).slice(0, limit)
      }};
    }},
    buscar: function(args){{
      const t = (args.termo || '').toLowerCase();
      if(!t) return {{erro: 'termo obrigatório'}};
      const idx = buildIndex();
      const hits = idx.filter(it => it.txt.toLowerCase().includes(t)).slice(0, 20);
      return {{n: hits.length, hits: hits.map(h => ({{contexto: h.ctx, trecho: h.txt.slice(0,180)}}))}};
    }},
    // ── TOOLS DE MEMÓRIA (a IA pode gerenciar sozinha) ──
    salvar_nota: function(args){{
      if(!args.texto) return {{erro: 'texto obrigatório'}};
      const n = addNota(args.texto.trim());
      return {{ok: true, nota_salva: n, total_notas: getNotas().length}};
    }},
    listar_notas: function(){{
      return {{n: getNotas().length, notas: getNotas()}};
    }},
    apagar_nota: function(args){{
      if(!args.id) return {{erro: 'id obrigatório'}};
      delNota(args.id);
      return {{ok: true, restantes: getNotas().length}};
    }},
    classificar_contato: function(args){{
      const tipo = args.tipo || 'fornecedor';
      if(!args.nome || !args.categoria) return {{erro: 'nome e categoria obrigatórios'}};
      const c = addClassif(tipo, args.nome.trim(), args.categoria.trim());
      return {{ok: true, classificacao_salva: c}};
    }},
    definir_perfil: function(args){{
      if(typeof args.texto !== 'string') return {{erro: 'texto obrigatório'}};
      setPerfil(args.texto.trim());
      return {{ok: true, novo_perfil: getPerfil()}};
    }},
  }};

  // ── Esquema das tools pra API Anthropic ──
  const TOOL_SCHEMAS = [
    {{name:'consultar_fornecedor', description:'Retorna TUDO sobre um fornecedor: histórico total de pagamentos (com breakdown por mês, sem janela fixa), agregação recente do dashboard, e parcelas em aberto (a pagar futuro). Use sempre que o usuário perguntar sobre quanto pagou/vai pagar pra alguém.',
     input_schema:{{type:'object', properties:{{nome:{{type:'string', description:'nome ou parte do nome do fornecedor (case-insensitive, sem acentos)'}}}}, required:['nome']}}}},
    {{name:'consultar_cliente', description:'Retorna TUDO sobre um cliente: histórico total de recebimentos (com breakdown por mês) e parcelas a receber em aberto.',
     input_schema:{{type:'object', properties:{{nome:{{type:'string'}}}}, required:['nome']}}}},
    {{name:'consultar_pagamentos', description:'Busca pagamentos históricos (já feitos) com filtros combinados. Use quando a pergunta envolver período/mês/categoria específico. Ex: "quanto paguei em jan/26?", "gastos com Serviços PJ no trimestre".',
     input_schema:{{type:'object', properties:{{
       fornecedor:{{type:'string', description:'parcial do nome'}},
       categoria:{{type:'string', description:'parcial do nome da categoria'}},
       mes:{{type:'string', description:'mês exato YYYY-MM'}},
       data_inicio:{{type:'string', description:'YYYY-MM-DD inclusive'}},
       data_fim:{{type:'string', description:'YYYY-MM-DD inclusive'}}
     }}}}}},
    {{name:'consultar_recebimentos', description:'Busca recebimentos históricos com filtros. Análogo a consultar_pagamentos, para o lado de receita.',
     input_schema:{{type:'object', properties:{{
       cliente:{{type:'string'}}, mes:{{type:'string'}},
       data_inicio:{{type:'string'}}, data_fim:{{type:'string'}}
     }}}}}},
    {{name:'listar_vencidos', description:'Lista parcelas vencidas. tipo="pagar" para contas a pagar atrasadas, "receber" para recebimentos atrasados.',
     input_schema:{{type:'object', properties:{{tipo:{{type:'string', enum:['pagar','receber']}}}}, required:['tipo']}}}},
    {{name:'consultar_dre', description:'DRE mês a mês (receita, despesa, lucro). Sem filtros: últimos N meses (default 6). Com filtros: mes exato (YYYY-MM), data_inicio/data_fim (YYYY-MM), ou tipo (real/projetado). Retorna também totais agregados do período filtrado.',
     input_schema:{{type:'object', properties:{{
       mes:{{type:'string', description:'YYYY-MM exato'}},
       data_inicio:{{type:'string', description:'YYYY-MM inclusive'}},
       data_fim:{{type:'string', description:'YYYY-MM inclusive'}},
       tipo:{{type:'string', enum:['real','projetado']}},
       meses_recentes:{{type:'integer'}}
     }}}}}},
    {{name:'cobertura_temporal', description:'Retorna o range temporal de cada fonte de dados (pagamentos, recebimentos, DRE, Totvs). USE ESTA TOOL PRIMEIRO se a pergunta envolver um mês/ano que você não tem certeza se tem dados — antes de dizer "não tenho", confira.',
     input_schema:{{type:'object', properties:{{}}}}}},
    {{name:'consultar_auditoria', description:'Findings da auditoria tributária. severidade opcional: error, warn, info, ok.',
     input_schema:{{type:'object', properties:{{severidade:{{type:'string', enum:['error','warn','info','ok']}}}}}}}},
    {{name:'consultar_totvs', description:'Resumo geral ou detalhes das comissões Totvs por mês. Se mes informado (YYYY-MM), retorna só aquele mês.',
     input_schema:{{type:'object', properties:{{mes:{{type:'string', description:'YYYY-MM, opcional'}}}}}}}},
    {{name:'top_categoria', description:'Lista categorias com totais agregados OU top fornecedores de UMA categoria específica. Cobre a janela do dashboard (meses recentes).',
     input_schema:{{type:'object', properties:{{categoria:{{type:'string'}}, limit:{{type:'integer'}}}}}}}},
    {{name:'buscar', description:'Busca textual em todas as tabelas/cards do dashboard. Útil quando a pergunta menciona algo que não é claramente fornecedor/cliente (ex: número de documento, valor exato).',
     input_schema:{{type:'object', properties:{{termo:{{type:'string'}}}}, required:['termo']}}}},
    // ── Memória persistente ──
    {{name:'salvar_nota', description:'Grava uma anotação no localStorage do usuário. USE QUANDO ele disser "anota", "lembra", "salva", "guarda", "memoriza" — ou quando ele te der uma informação útil pra próximas conversas (ex: "X é acordo trabalhista"). Confirme depois pra ele.',
     input_schema:{{type:'object', properties:{{texto:{{type:'string', description:'texto da anotação (1-2 linhas, factual)'}}}}, required:['texto']}}}},
    {{name:'listar_notas', description:'Lista todas as anotações salvas. Use se o usuário pedir "minhas anotações", "o que eu te falei sobre X", etc.',
     input_schema:{{type:'object', properties:{{}}}}}},
    {{name:'apagar_nota', description:'Remove uma anotação pelo id (use listar_notas primeiro pra pegar o id).',
     input_schema:{{type:'object', properties:{{id:{{type:'number'}}}}, required:['id']}}}},
    {{name:'classificar_contato', description:'Salva uma classificação permanente de categoria pra um fornecedor ou cliente. USE QUANDO o usuário disser "X é da categoria Y" sobre um contato específico — útil quando o Bling tá com (sem categoria).',
     input_schema:{{type:'object', properties:{{
       tipo:{{type:'string', enum:['fornecedor','cliente']}},
       nome:{{type:'string'}}, categoria:{{type:'string'}}
     }}, required:['nome','categoria']}}}},
    {{name:'definir_perfil', description:'Substitui o perfil do negócio do usuário (texto livre que vai junto em todo system prompt). Use quando ele pedir explicitamente pra você gravar contexto da empresa, preferências de resposta etc.',
     input_schema:{{type:'object', properties:{{texto:{{type:'string'}}}}, required:['texto']}}}},
  ];

  // ── Histórico de conversa ──
  let history = [];
  try{{ history = JSON.parse(localStorage.getItem(LS_HIST) || '[]'); }} catch(e){{ history = []; }}

  function renderHistory(){{
    body.querySelectorAll('.ef-chat-msg').forEach(el => el.remove());
    history.forEach(m => appendMsg(m.role, m.text, false));
  }}
  function renderMarkdown(text){{
    return text
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/^\\|(.+)\\|$/gm, (m) => {{
        const cells = m.slice(1,-1).split('|').map(c => c.trim());
        const isSep = cells.every(c => /^-+$/.test(c));
        if(isSep) return '<!-- sep -->';
        const tag = m.includes('---') ? 'th' : 'td';
        return '<tr>' + cells.map(c => `<${{tag}}>${{c}}</${{tag}}>`).join('') + '</tr>';
      }})
      .replace(/(<tr>.*?<\\/tr>(\\n<!-- sep -->\\n)?)+/gs, m => '<table>' + m.replace(/\\n<!-- sep -->\\n/g,'\\n') + '</table>')
      .split(/\\n\\n+/).map(p => p.startsWith('<table>') ? p : '<p>'+p.replace(/\\n/g,'<br>')+'</p>').join('');
  }}
  function appendMsg(role, text, persist=true){{
    const div = document.createElement('div');
    if(role === 'toolcall'){{
      div.className = 'ef-chat-msg toolcall';
      div.textContent = text;
    }} else {{
      div.className = 'ef-chat-msg ' + (role === 'user' ? 'user' : (role === 'err' ? 'err' : (role === 'thinking' ? 'thinking' : 'ai')));
      if(role === 'ai') div.innerHTML = renderMarkdown(text);
      else div.textContent = text;
    }}
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
    if(persist && role !== 'thinking' && role !== 'err' && role !== 'toolcall'){{
      history.push({{role, text}});
      try{{ localStorage.setItem(LS_HIST, JSON.stringify(history.slice(-30))); }} catch(e){{}}
    }}
    return div;
  }}
  renderHistory();

  clrBtn.addEventListener('click', () => {{
    history = [];
    localStorage.removeItem(LS_HIST);
    body.querySelectorAll('.ef-chat-msg').forEach(el => el.remove());
    doSearch(input.value.trim());
  }});

  // ── System prompt enxuto (memória persistente é anexada dinamicamente em askAI) ──
  const SYSTEM_PROMPT_BASE = "Você é o CFO virtual da Easy Analytics (TI/serviços, Lucro Presumido, sede SBC-SP). " +
    "Analise dados financeiros e responda em PT-BR de forma executiva, direta e analítica. " +
    "Use Markdown leve (negrito, tabelas curtas, código pra valores). Use ⚠️ pra atenção. " +
    "Você tem um RESUMO inline (KPIs globais + coberturas temporais) + 16 TOOLS pra consultar detalhes do dashboard E pra gerenciar a memória persistente do usuário. " +
    "REGRA CRÍTICA: NÃO invente nada. Quando o usuário perguntar sobre um fornecedor, cliente, mês, categoria, período, comissão Totvs, finding de auditoria — CHAME A TOOL antes de responder. Os dados crescem a cada execução semanal: NÃO assuma janelas fixas. Sempre cite o período que a tool retornar. " +
    "MEMÓRIA: você tem 5 tools pra memória persistente — salvar_nota, listar_notas, apagar_nota, classificar_contato, definir_perfil. SEMPRE CHAME salvar_nota quando o usuário disser 'anota', 'lembra', 'salva', 'memoriza' — não tente apenas escrever na resposta. SEMPRE CHAME classificar_contato quando ele disser 'X é da categoria Y'. A memória atual já está anexada abaixo nesta mensagem (se houver) — leve em conta em todas as respostas. " +
    "COBERTURA: o RESUMO traz o range de cada fonte em `coberturas`. Pode responder sobre qualquer mês dentro desses ranges — passado realizado, em aberto futuro, DRE, comissões Totvs (até 3 anos). Fora dos ranges: diga que o snapshot não tem. Use `cobertura_temporal` se em dúvida. " +
    "PERÍODOS: passado realizado → consultar_pagamentos/consultar_recebimentos (mes=YYYY-MM, data_inicio, data_fim). Futuro/projetado → consultar_fornecedor/consultar_cliente. DRE filtrado → consultar_dre(mes? data_inicio? data_fim? tipo=real|projetado). " +
    "Cruze histórico realizado × futuro em aberto quando relevante. " +
    "Tools: consultar_fornecedor, consultar_cliente, consultar_pagamentos, consultar_recebimentos, listar_vencidos, consultar_dre, cobertura_temporal, consultar_auditoria, consultar_totvs, top_categoria, buscar, salvar_nota, listar_notas, apagar_nota, classificar_contato, definir_perfil.";

  // ── Chamada API com loop de tools ──
  async function askAI(question){{
    const key = getKey();
    if(!key){{
      cfgKey.value = ''; cfgModel.value = getModel(); cfgModal.classList.add('open');
      appendMsg('err', 'Configure sua chave Anthropic primeiro (clique no ⚙️).');
      return;
    }}
    appendMsg('user', question);

    // Estado da conversa (formato Anthropic messages API)
    const messages = [
      ...history.filter(m => m.role === 'user' || m.role === 'ai').slice(-10)
                .map(m => ({{role: m.role === 'ai' ? 'assistant' : 'user', content: m.text}})),
    ];
    // Última mensagem é a pergunta atual (a entrada acima já inclui via history se persistiu)
    // Mas como acabamos de appendMsg user, ela está em history → ok.
    if(!messages.length || messages[messages.length-1].role !== 'user' || messages[messages.length-1].content !== question){{
      messages.push({{role: 'user', content: question}});
    }}

    // Snapshot inline na primeira mensagem do user (pra Claude saber os KPIs)
    if(messages[messages.length-1].role === 'user'){{
      messages[messages.length-1].content =
        "RESUMO_DASHBOARD (use as TOOLS pra detalhes):\\n```json\\n" +
        JSON.stringify(EF_SUMMARY) + "\\n```\\n\\nPergunta: " + question;
    }}

    let thinking = appendMsg('thinking', 'pensando…', false);
    let safety = 0; // limite de iterações
    try{{
      while(safety++ < 6){{
        const resp = await fetch('https://api.anthropic.com/v1/messages', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'x-api-key': key,
            'anthropic-version': '2023-06-01',
            'anthropic-dangerous-direct-browser-access': 'true',
          }},
          body: JSON.stringify({{
            model: getModel(),
            max_tokens: 1500,
            system: SYSTEM_PROMPT_BASE + getMemoryBlock(),
            tools: TOOL_SCHEMAS,
            messages,
          }}),
        }});
        if(!resp.ok){{
          if(thinking) thinking.remove();
          const errText = await resp.text();
          appendMsg('err', 'Erro API (' + resp.status + '): ' + errText.slice(0, 250));
          return;
        }}
        const data = await resp.json();
        // Anexa a resposta do assistant ao histórico (pode ter text + tool_use)
        messages.push({{role: 'assistant', content: data.content}});

        if(data.stop_reason === 'tool_use'){{
          // Resolve todas as tool calls do turno
          const toolUses = (data.content || []).filter(b => b.type === 'tool_use');
          const toolResults = [];
          for(const tu of toolUses){{
            const fn = TOOLS[tu.name];
            let result;
            if(!fn) result = {{erro: 'tool desconhecida: ' + tu.name}};
            else {{
              try{{ result = fn(tu.input || {{}}); }}
              catch(e){{ result = {{erro: e.message || String(e)}}; }}
            }}
            const argsPretty = Object.entries(tu.input || {{}}).map(([k,v]) => k+'='+JSON.stringify(v)).join(', ');
            appendMsg('toolcall', `→ ${{tu.name}}(${{argsPretty}})`, false);
            toolResults.push({{type: 'tool_result', tool_use_id: tu.id, content: JSON.stringify(result)}});
          }}
          messages.push({{role: 'user', content: toolResults}});
          // Continua o loop pra IA responder com os resultados
          continue;
        }}

        // stop_reason = end_turn (resposta final)
        if(thinking){{ thinking.remove(); thinking = null; }}
        const textBlocks = (data.content || []).filter(b => b.type === 'text');
        const finalText = textBlocks.map(b => b.text).join('').trim();
        if(finalText) appendMsg('ai', finalText);
        else appendMsg('err', '(resposta vazia)');
        return;
      }}
      if(thinking) thinking.remove();
      appendMsg('err', 'Loop interrompido (excedeu 6 iterações de tools).');
    }} catch(e){{
      if(thinking) thinking.remove();
      appendMsg('err', 'Falha: ' + (e.message || e));
    }}
  }}

  askBtn.addEventListener('click', () => {{
    const q = input.value.trim();
    if(!q){{ input.focus(); return; }}
    askAI(q);
    input.value = '';
    doSearch('');
  }});
  input.addEventListener('keydown', e => {{
    if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){{
      askBtn.click();
    }}
  }});

  updateModelBadge();
}})();
</script>
"""
