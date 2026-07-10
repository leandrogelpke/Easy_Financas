#!/usr/bin/env python3
"""
build-html.py — lê o snapshot bling_data_*.json e gera index.html com dados ao vivo.

Uso:
    python3 build-html.py                         # padrões automáticos
    python3 build-html.py --in <pasta> --out <arquivo>

Fluxo:
    1. Encontra o bling_data_*.json mais recente em --in (ou default)
    2. Computa ROWS (contas_pagar_pagas — últimos 4 meses por fornecedor)
    3. Computa CX_DATA (contas_receber_em_aberto)
    4. Lê template.html (junto ao script)
    5. Substitui marcadores @@MARKER@@ com dados ao vivo
    6. Escreve index.html (ou --out)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

DEFAULT_IN_CANDIDATES = [
    Path.home() / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api",
    HERE / "relatorios",
]
DEFAULT_TEMPLATE = HERE / "template.html"
DEFAULT_OUT = HERE / "index.html"

# ──────────────────────────────────────────────
# HELPERS DE PARSE
# ──────────────────────────────────────────────

def parse_money(s: Any) -> float:
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
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


# Mês em PT — 3 letras, minúsculas
_MONTH_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez"]
_MONTH_PT_FULL = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                  "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]


def month_key(d: date) -> str:
    """Chave de mês: 'jan26', 'fev26', etc."""
    return _MONTH_PT[d.month - 1] + str(d.year)[-2:]


def month_label(d: date) -> str:
    """Label curto: 'Jan/26'"""
    return _MONTH_PT[d.month - 1].capitalize() + "/" + str(d.year)[-2:]


def month_full_name(d: date) -> str:
    """Nome completo: 'Janeiro'"""
    return _MONTH_PT_FULL[d.month - 1]


def fmt_date_br(d: date | None) -> str:
    if not d:
        return ""
    return f"{d.day:02d}/{_MONTH_PT[d.month-1]}/{str(d.year)[-2:]}"


def js_str(s: str) -> str:
    """Escapa string para uso seguro em JS."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


# ──────────────────────────────────────────────
# MAPEAMENTO DE FORNECEDORES CONHECIDOS
# (nome Bling uppercase → {id, display, cat, status, badge_class})
# ──────────────────────────────────────────────

KNOWN_SUPPLIERS: dict[str, dict] = {
    "ROMULO FERREIRA LIMA": {
        "id": "romulo", "n": "Rômulo Ferreira Lima",
        "cat": "Buy-out", "st": "Não orçado", "sc": "bgr",
    },
    "EFATA TREINAMENTO EM TECNOLOGIA LTDA": {
        "id": "efata", "n": "Efata Treinamento",
        "cat": "Pessoal", "st": "Parcial", "sc": "bga",
    },
    "EFATA TREINAMENTO": {
        "id": "efata", "n": "Efata Treinamento",
        "cat": "Pessoal", "st": "Parcial", "sc": "bga",
    },
    "M. DE Q. MACEDO - EPP": {
        "id": "macedo", "n": "M. de Q. Macedo",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "M. DE Q. MACEDO": {
        "id": "macedo", "n": "M. de Q. Macedo",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA EM TECNOLOGIA DE COMPUTACAO L": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA EM TECNOLOGIA DE COMPUTAÇÃO L": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    # Polar: histórico "APORTE POLAR" — natureza de aporte de sócio/parceiro,
    # NÃO despesa operacional. Categorizada como Aporte/Sócios.
    "POLAR TECNICA COMERCIAL E INDUSTRIAL LTDA": {
        "id": "polar", "n": "Polar Técnica Comercial",
        "cat": "Aporte/Sócios", "st": "Aporte", "sc": "bga",
    },
    "POLAR TECNICA": {
        "id": "polar", "n": "Polar Técnica Comercial",
        "cat": "Aporte/Sócios", "st": "Aporte", "sc": "bga",
    },
    "EDUARDO FARIA DE GODOY 29853590824": {
        "id": "godoy", "n": "Eduardo Faria de Godoy",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "EDUARDO FARIA DE GODOY": {
        "id": "godoy", "n": "Eduardo Faria de Godoy",
        "cat": "Pessoal", "st": "Não orçado", "sc": "bgr",
    },
    "EP SERVIÇOS DE TECNOLOGIA LTDA": {
        "id": "ep", "n": "EP Serviços Tecnologia",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "EP SERVICOS DE TECNOLOGIA LTDA": {
        "id": "ep", "n": "EP Serviços Tecnologia",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "GEREMIAS FERREIRA LIMA 37305076805": {
        "id": "geremias", "n": "Geremias Ferreira Lima",
        "cat": "Outros", "st": "Não orçado", "sc": "bgr",
    },
    "GEREMIAS FERREIRA LIMA": {
        "id": "geremias", "n": "Geremias Ferreira Lima",
        "cat": "Outros", "st": "Não orçado", "sc": "bgr",
    },
    "CARTAO DE CREDITO": {
        "id": "cartao", "n": "Cartão de Crédito",
        "cat": "Outros", "st": "Não orçado", "sc": "bgr",
    },
    "CARTÃO DE CRÉDITO": {
        "id": "cartao", "n": "Cartão de Crédito",
        "cat": "Outros", "st": "Não orçado", "sc": "bgr",
    },
    # Impostos sobre lucro (IRPJ/CSLL trimestrais) + tributos federais e
    # municipais — todos consolidados em "Tributos e Contábil".
    "IRPJ": {
        "id": "irpj", "n": "IRPJ",
        "cat": "Tributos e Contábil", "st": "Imposto", "sc": "bga",
    },
    "VTCONN CONSULTORIA E SISTEMAS LTDA": {
        "id": "vtconn", "n": "Vtconn Consultoria",
        "cat": "Serviços PJ", "st": "Encerrou?", "sc": "bga",
    },
    "VTCONN CONSULTORIA": {
        "id": "vtconn", "n": "Vtconn Consultoria",
        "cat": "Serviços PJ", "st": "Encerrou?", "sc": "bga",
    },
    "IVY GROUP HOLDING S/A": {
        "id": "ivy", "n": "IVY Group Holding S/A",
        "cat": "Outros", "st": "Identificar", "sc": "bgr",
    },
    "IVY GROUP HOLDING": {
        "id": "ivy", "n": "IVY Group Holding S/A",
        "cat": "Outros", "st": "Identificar", "sc": "bgr",
    },
    "PLENTECH LTDA": {
        "id": "plentech", "n": "Plentech LTDA",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "VICTOR HUGO DE OLIVEIRA": {
        "id": "victor", "n": "Victor Hugo de Oliveira",
        "cat": "Serviços PJ", "st": "Encerrou?", "sc": "bga",
    },
    "CBYK CONSULTORIA EM GESTAO E TECNOLOGIA LTDA": {
        "id": "cbyk", "n": "CBYK Consultoria",
        "cat": "Outros", "st": "Identificar", "sc": "bgr",
    },
    "CBYK CONSULTORIA": {
        "id": "cbyk", "n": "CBYK Consultoria",
        "cat": "Outros", "st": "Identificar", "sc": "bgr",
    },
    "CSLL": {
        "id": "csll", "n": "CSLL",
        "cat": "Tributos e Contábil", "st": "Imposto", "sc": "bga",
    },

    # ── Tributos e Contábil ──────────────────────────────────────────
    "SERRANO CONTABILIDADE LIMITADA": {
        "id": "serrano", "n": "Serrano Contabilidade",
        "cat": "Tributos e Contábil", "st": "Recorrente", "sc": "bga",
    },
    "SERRANO CONTABILIDADE": {
        "id": "serrano", "n": "Serrano Contabilidade",
        "cat": "Tributos e Contábil", "st": "Recorrente", "sc": "bga",
    },
    "RECEITA FEDERAL": {
        "id": "receita", "n": "Receita Federal",
        "cat": "Tributos e Contábil", "st": "Imposto", "sc": "bga",
    },
    # ISS-SBC (PRESTADOR/TOMADOR/S/FATURAMENTO) — município São Bernardo
    "PREFEITURA DO MUNICIPIO DE SAO BERNARDO DO CAMPO": {
        "id": "prefeitura", "n": "Prefeitura SBC (ISS)",
        "cat": "Tributos e Contábil", "st": "Imposto", "sc": "bga",
    },
    "PREFEITURA DO MUNICIPIO DE SAO BERNARDO": {
        "id": "prefeitura", "n": "Prefeitura SBC (ISS)",
        "cat": "Tributos e Contábil", "st": "Imposto", "sc": "bga",
    },
    # TFE (Taxa de Fiscalização de Estabelecimento) — também SBC mas
    # paga 1x/ano em parcela única, então fica como id próprio.
    "MUNICIPIO DE SAO BERNARDO DO CAMPO": {
        "id": "municipio_sbc", "n": "Município SBC (TFE)",
        "cat": "Tributos e Contábil", "st": "Anual", "sc": "bga",
    },

    # ── Parceria TOTVS ───────────────────────────────────────────────
    # Renovação anual da Parceria Easy & TOTVS — despesa estratégica
    # recorrente, separada do grupo "Outros".
    "TOTVS S.A.": {
        "id": "totvs", "n": "TOTVS S.A.",
        "cat": "Parceria TOTVS", "st": "Anual", "sc": "bga",
    },
    "TOTVS SA": {
        "id": "totvs", "n": "TOTVS S.A.",
        "cat": "Parceria TOTVS", "st": "Anual", "sc": "bga",
    },

    # ── Serviços PJ adicionais ───────────────────────────────────────
    # Cralus: suporte AWS/App recorrente (mensal, ~R$ 940).
    "CRALUS INFORMATICA LTDA": {
        "id": "cralus", "n": "Cralus Informática",
        "cat": "Serviços PJ", "st": "Recorrente", "sc": "bga",
    },
    "CRALUS INFORMATICA": {
        "id": "cralus", "n": "Cralus Informática",
        "cat": "Serviços PJ", "st": "Recorrente", "sc": "bga",
    },

    # ── Software / SaaS ──────────────────────────────────────────────
    # LWSA é a dona do Bling (assinatura mensal do sistema usado pelo
    # próprio Leandro pra rodar este dashboard).
    "LWSA S/A": {
        "id": "lwsa", "n": "LWSA (Bling)",
        "cat": "Software", "st": "Recorrente", "sc": "bga",
    },
    "LWSA SA": {
        "id": "lwsa", "n": "LWSA (Bling)",
        "cat": "Software", "st": "Recorrente", "sc": "bga",
    },

    # ── Pessoal — prestadores PJ (classificação gerencial) ───────────
    # Contam como "Despesas com pessoal" no reporte do Leandro (não folha
    # CLT). Mesmo grupo no DRE (desp_pessoal) e na aba Gastos (cat Pessoal).
    "ISABEL FELIX": {
        "id": "isabel", "n": "Isabel Felix",
        "cat": "Pessoal", "st": "Reembolso", "sc": "bga",
    },
    "ALAN BARBOSA RAMOS GESTAO EMPRESARIAL": {
        "id": "alan", "n": "Alan Barbosa",
        "cat": "Pessoal", "st": "Recorrente", "sc": "bgr",
    },
    "ALAN BARBOSA": {
        "id": "alan", "n": "Alan Barbosa",
        "cat": "Pessoal", "st": "Recorrente", "sc": "bgr",
    },
}

# Grupos para o stack chart (top fornecedores agrupados).
# Estrutura redesenhada em jun/2026 — bucket "Outros" antigo foi explodido
# em Tributos, Parceria TOTVS, Aporte e Outros (resíduo).
STACK_GROUPS = [
    {"label": "Buy-out",     "ids": ["romulo"]},
    {"label": "Pessoal",     "ids": ["alan", "isabel", "macedo", "efata",
                                     "milajanu", "godoy"]},
    {"label": "Serv. PJ",    "ids": ["ep", "vtconn", "victor", "plentech",
                                     "cralus"]},
    {"label": "Tributos",    "ids": ["irpj", "csll", "serrano", "receita",
                                     "prefeitura", "municipio_sbc"]},
    {"label": "Parc. TOTVS", "ids": ["totvs"]},
    {"label": "Aporte",      "ids": ["polar"]},
    {"label": "Outros",      "ids": ["geremias", "cartao", "ivy", "cbyk",
                                     "lwsa", "_outros"]},
]


# ──────────────────────────────────────────────
# COMPUTAÇÃO DE DADOS
# ──────────────────────────────────────────────

def find_supplier(name: str) -> dict | None:
    """Encontra fornecedor pelo nome (match exato ou prefixo)."""
    if not name:
        return None
    name_up = name.upper().strip()
    if name_up in KNOWN_SUPPLIERS:
        return KNOWN_SUPPLIERS[name_up]
    # prefixo: procura chave que é prefixo do nome ou vice-versa
    for key, info in KNOWN_SUPPLIERS.items():
        if name_up.startswith(key) or key.startswith(name_up[:20]):
            return info
    return None


def compute_last_4_months(pagas: list[dict]) -> list[date]:
    """YTD: jan do ano corrente até o mês atual (inclusive). Cresce a cada mês."""
    today = date.today()
    return [date(today.year, m, 1) for m in range(1, today.month + 1)]


def compute_rows(pagas: list[dict], months: list[date]) -> list[dict]:
    """Agrega contas_pagar_pagas por fornecedor × mês → ROWS."""
    month_keys = [month_key(d) for d in months]

    # {supplier_id: {month_key: valor, ...}}
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    supplier_info: dict[str, dict] = {}

    for p in pagas:
        name = (p.get("contato_nome") or p.get("nomeContato") or "").strip()
        d = parse_date(p.get("vencimento") or p.get("dataVencimento") or "")
        valor = parse_money(p.get("valor"))
        if not d or valor == 0:
            continue
        mk = month_key(d)
        if mk not in month_keys:
            continue  # fora do janela de 4 meses

        info = find_supplier(name)
        if info:
            sid = info["id"]
            sums[sid][mk] += valor
            supplier_info[sid] = info
        else:
            # fornecedor desconhecido → bucket "_outros"
            sums["_outros"][mk] += valor
            if "_outros" not in supplier_info:
                supplier_info["_outros"] = {
                    "id": "_outros", "n": "Outros (não classificados)",
                    "cat": "Outros", "st": "Identificar", "sc": "bgr",
                }

    # Montar ROWS ordenados por total decrescente
    rows = []
    for sid, month_totals in sums.items():
        info = supplier_info[sid]
        total = sum(month_totals.values())
        row: dict = {
            "id":    info["id"],
            "n":     info["n"],
            "cat":   info["cat"],
            "st":    info["st"],
            "sc":    info["sc"],
            "total": round(total),
        }
        for mk in month_keys:
            row[mk] = round(month_totals.get(mk, 0))
        rows.append(row)

    rows.sort(key=lambda r: -r["total"])
    return rows


def compute_cx_data(receber: list[dict], today: date) -> list[dict]:
    """Converte contas_receber_em_aberto → CX_DATA."""
    from datetime import timedelta
    items = []
    for i, c in enumerate(receber):
        nome = (c.get("contato_nome") or c.get("nomeContato") or "(sem cliente)").strip()
        doc = c.get("numeroDocumento") or c.get("documento") or str(i + 1)
        venc = parse_date(c.get("vencimento") or c.get("dataVencimento") or "")
        valor = parse_money(c.get("valor"))
        if valor == 0:
            continue

        # Determinar situação e badge
        if venc and venc < today:
            sit = "Atrasada"
            bgc = "bgr"
        elif venc and venc <= today + timedelta(days=60):
            sit = "Em aberto"
            bgc = "bgb"
        else:
            sit = "Previsto"
            bgc = "bgg"

        # Verificar se é parcela parcial
        situacao_bling = (c.get("situacao") or c.get("status") or "").lower()
        if "parcial" in situacao_bling:
            sit = "Parcial"
            bgc = "bga"

        label = f"{nome} — doc {doc}"[:50]
        item_id = f"rx{i+1:03d}"

        items.append({
            "id":   item_id,
            "c":    label,
            "venc": fmt_date_br(venc) if venc else "—",
            "sit":  sit,
            "val":  round(valor),
            "bgc":  bgc,
        })

    # Ordenar: Atrasadas → Parcial → Em aberto → Previsto
    order = {"Atrasada": 0, "Parcial": 1, "Em aberto": 2, "Previsto": 3}
    items.sort(key=lambda r: (order.get(r["sit"], 9), r["venc"]))
    return items


# ──────────────────────────────────────────────
# SERIALIZAÇÃO JS
# ──────────────────────────────────────────────

def _js_num(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def rows_to_js(rows: list[dict], month_keys: list[str]) -> str:
    parts = []
    for r in rows:
        fields = [
            f"id:'{js_str(r['id'])}'",
            f"n:'{js_str(r['n'])}'",
            f"cat:'{js_str(r['cat'])}'",
            f"st:'{js_str(r['st'])}'",
            f"sc:'{js_str(r['sc'])}'",
            f"total:{_js_num(r['total'])}",
        ]
        for mk in month_keys:
            fields.append(f"{mk}:{_js_num(r.get(mk, 0))}")
        parts.append("{" + ",".join(fields) + "}")
    return "[\n  " + ",\n  ".join(parts) + "\n]"


def cx_data_to_js(items: list[dict]) -> str:
    parts = []
    for it in items:
        parts.append(
            "{" +
            f"id:'{js_str(it['id'])}'," +
            f"c:'{js_str(it['c'])}'," +
            f"venc:'{js_str(it['venc'])}'," +
            f"sit:'{js_str(it['sit'])}'," +
            f"val:{_js_num(it['val'])}," +
            f"bgc:'{js_str(it['bgc'])}'" +
            "}"
        )
    return "[\n  " + ",\n  ".join(parts) + "\n]"


def groups_to_js(groups: list[dict]) -> str:
    parts = []
    for g in groups:
        ids_js = "[" + ",".join(f"'{i}'" for i in g["ids"]) + "]"
        parts.append("{" + f"label:'{js_str(g['label'])}',ids:{ids_js}" + "}")
    return "[" + ",".join(parts) + "]"


# ──────────────────────────────────────────────
# PAGAR EM ABERTO  (tab Financeiro)
# ──────────────────────────────────────────────

def _pagar_window(venc: date | None, today: date) -> str:
    """Classifica o vencimento em janelas de risco."""
    if venc is None:
        return "90plus"
    delta = (venc - today).days
    if delta < 0:
        return "vencido"
    if delta <= 30:
        return "30d"
    if delta <= 60:
        return "60d"
    if delta <= 90:
        return "90d"
    return "90plus"


def compute_pagar_data(em_aberto: list[dict], today: date) -> list[dict]:
    """Converte contas_pagar_em_aberto → PAGAR_DATA."""
    items = []
    for i, c in enumerate(em_aberto):
        nome = (c.get("contato_nome") or c.get("nomeContato") or "(sem fornecedor)").strip()
        # Usar saldo quando disponível, senão valor
        valor = parse_money(c.get("saldo") or c.get("valor"))
        if valor <= 0:
            continue

        doc = (c.get("numero_documento") or c.get("numeroDocumento") or
               c.get("documento") or str(i + 1))
        cat = (c.get("categoria_descricao") or c.get("categoriaDescricao") or
               c.get("categoria") or "(sem categoria)").strip()
        venc = parse_date(c.get("vencimento") or c.get("dataVencimento") or "")
        window = _pagar_window(venc, today)
        item_id = f"pa{i+1:03d}"

        items.append({
            "id":     item_id,
            "n":      nome,
            "cat":    cat,
            "doc":    str(doc)[:20],
            "venc":   fmt_date_br(venc) if venc else "—",
            "window": window,
            "val":    round(valor),
        })

    # Ordenar: vencido primeiro, depois por valor desc
    win_order = {"vencido": 0, "30d": 1, "60d": 2, "90d": 3, "90plus": 4}
    items.sort(key=lambda r: (win_order.get(r["window"], 9), -r["val"]))
    return items


def render_pagar_mensal_html(em_aberto: list[dict], today: date,
                              months_ahead: int = 2) -> str:
    """Renderiza tabelas HTML "Mês/Ano — total: R$ X" puxando de em_aberto.

    Mostra `months_ahead` meses a partir do mês atual, agregando contas
    com mesmo fornecedor no mesmo mês. Substitui as tabelas hardcoded
    da aba Caixa que estavam desatualizadas desde mai/2026.
    """
    from collections import defaultdict
    MN_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun",
                "jul", "ago", "set", "out", "nov", "dez"]
    MN_FULL  = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

    # Distribui meses
    target_months = []
    y, m = today.year, today.month
    for _ in range(months_ahead):
        target_months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1

    # Agrupa em_aberto por (ano,mês,fornecedor)
    by_month: dict[tuple[int, int], dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list))
    for c in em_aberto:
        venc = parse_date(c.get("vencimento") or "")
        if not venc:
            continue
        if (venc.year, venc.month) not in target_months:
            continue
        nome = (c.get("contato_nome") or "(sem fornecedor)").strip()
        valor = parse_money(c.get("saldo") or c.get("valor"))
        if valor <= 0:
            continue
        by_month[(venc.year, venc.month)][nome].append({
            "venc": venc, "valor": valor,
            "historico": (c.get("historico") or "").strip(),
            "emissao": parse_date(c.get("data_emissao") or ""),
        })

    cards = []
    for (yr, mo) in target_months:
        forn_dict = by_month.get((yr, mo), {})
        # Soma por fornecedor
        rows = []
        for nome, lcts in forn_dict.items():
            total = sum(x["valor"] for x in lcts)
            vencs = sorted(set(x["venc"].day for x in lcts))
            venc_label = (f"{vencs[0]:02d}/{MN_ABREV[mo-1]}"
                          if len(vencs) == 1
                          else f"{vencs[0]:02d}–{vencs[-1]:02d}/{MN_ABREV[mo-1]}")
            # Categoria (semáforo amber/red para alguns nomes-chave)
            color = ""
            nome_up = _strip_accents_upper(nome)
            if "ROMULO" in nome_up or "ATACADAO" in nome_up:
                color = "color:var(--red)"
            elif "EFATA" in nome_up or "MACEDO" in nome_up or "MILAJANU" in nome_up \
                    or "GEREMIAS" in nome_up or "TOTVS" in nome_up:
                color = "color:var(--amber)"
            display_name = _trunc(nome, 32)
            # Datas de lançamento (data de emissão Bling), todas, ordenadas únicas
            emi_dates = sorted({x["emissao"] for x in lcts if x.get("emissao")})
            lanc_label = ", ".join(f"{d.day:02d}/{d.month:02d}/{str(d.year)[-2:]}"
                                   for d in emi_dates)
            rows.append({"nome": display_name, "venc": venc_label,
                         "val": total, "color": color, "lanc": lanc_label})
        rows.sort(key=lambda r: -r["val"])

        total_mes = sum(r["val"] for r in rows)
        n_lct = sum(len(forn_dict[n]) for n in forn_dict)

        # HTML
        from html import escape as _esc_html
        tbody = "\n".join(
            f'        <tr><td>{_esc_html(r["nome"])}'
            + (f'<div style="font-size:10px;color:var(--t3);margin-top:2px;font-weight:400">lançado: {_esc_html(r["lanc"])}</div>'
               if r["lanc"] else "")
            + f'</td>'
            f'<td class="r">{r["venc"]}</td>'
            f'<td class="r" style="{r["color"]}">R$ {_brl_num(r["val"])}</td></tr>'
            for r in rows
        ) or '        <tr><td colspan="3" style="text-align:center;color:var(--t3)">Nenhuma conta em aberto</td></tr>'

        cards.append(f"""  <div class="card">
    <div class="ct">{MN_FULL[mo-1]}/{yr} — total: R$ {_brl_num(total_mes)} <span style="color:var(--t3);font-size:11px">({n_lct} lcto)</span></div>
    <table class="dt">
      <thead><tr><th>Fornecedor</th><th class="r">Venc.</th><th class="r">Valor</th></tr></thead>
      <tbody>
{tbody}
      </tbody>
    </table>
  </div>""")

    return '<div class="g2">\n' + "\n".join(cards) + "\n</div>"


def render_vencidos_html(em_aberto: list[dict], receber: list[dict],
                          today: date) -> str:
    """Abre o KPI 'Total vencidos' da Caixa: lista item a item as contas
    a pagar e a receber com vencimento < hoje (auditável). Bling-driven.
    """
    from html import escape as _esc

    def _coleta(rows: list[dict]) -> list[dict]:
        out = []
        for c in rows:
            venc = parse_date(c.get("vencimento") or "")
            if not venc or venc >= today:
                continue
            valor = parse_money(c.get("saldo") or c.get("valor"))
            if valor <= 0:
                continue
            out.append({
                "nome": (c.get("contato_nome") or "(sem nome)").strip(),
                "hist": (c.get("historico") or "").strip(),
                "venc": venc,
                "dias": (today - venc).days,
                "val": valor,
            })
        out.sort(key=lambda r: -r["val"])
        return out

    pagar = _coleta(em_aberto)
    receb = _coleta(receber)
    tot_p = sum(r["val"] for r in pagar)
    tot_r = sum(r["val"] for r in receb)

    def _tbody(items: list[dict], color: str) -> str:
        if not items:
            return ('<tr><td colspan="3" style="text-align:center;'
                    'color:var(--t3)">Nada vencido 🎉</td></tr>')
        linhas = []
        for r in items:
            venc_lbl = f"{r['venc'].day:02d}/{_MONTH_PT[r['venc'].month-1]}"
            hist = _trunc(r["hist"], 34) if r["hist"] else ""
            sub = (f'<div style="font-size:10px;color:var(--t3);margin-top:2px;'
                   f'font-weight:400">{_esc(hist)}</div>' if hist else "")
            linhas.append(
                f'<tr><td>{_esc(_trunc(r["nome"], 30))}{sub}</td>'
                f'<td class="r">{venc_lbl}'
                f'<div style="font-size:10px;color:var(--red);margin-top:2px">'
                f'{r["dias"]}d atraso</div></td>'
                f'<td class="r" style="color:{color}">R$ {_brl_num(r["val"])}</td></tr>'
            )
        return "\n".join(linhas)

    return f"""<div class="sl">Vencidos — detalhamento <span style="color:var(--t3);font-size:11px;text-transform:none;letter-spacing:0">— abertura do KPI "Total vencidos" (Bling, venc. &lt; hoje)</span></div>
<div class="g2">
  <div class="card">
    <div class="ct">A pagar vencido <span class="ct-tag">{len(pagar)} itens</span> <span style="float:right;font-family:var(--mono);color:var(--red)">R$ {_brl_num(tot_p)}</span></div>
    <table class="dt">
      <thead><tr><th>Fornecedor</th><th class="r">Venc.</th><th class="r">Saldo</th></tr></thead>
      <tbody>
{_tbody(pagar, "var(--red)")}
      </tbody>
    </table>
  </div>
  <div class="card">
    <div class="ct">A receber vencido <span class="ct-tag">{len(receb)} itens</span> <span style="float:right;font-family:var(--mono);color:var(--amber)">R$ {_brl_num(tot_r)}</span></div>
    <table class="dt">
      <thead><tr><th>Cliente</th><th class="r">Venc.</th><th class="r">Saldo</th></tr></thead>
      <tbody>
{_tbody(receb, "var(--amber)")}
      </tbody>
    </table>
  </div>
</div>"""


def render_cashflow_html(pagas: list[dict], recebidas: list[dict],
                          em_aberto: list[dict], receber: list[dict],
                          today: date) -> str:
    """Gráfico de barras (CSS) entradas x saídas mês a mês — regime de caixa.

    Meses anteriores ao atual: REALIZADO (recebidas/pagas). Mês atual e
    futuros: PROJETADO (em aberto). Janela: jan do ano corrente → atual+2.
    Tudo Bling-driven; barras em CSS (resiliente, sem dependência de JS).
    """
    from collections import defaultdict

    def _vv(r: dict, field_first: str) -> float:
        v = r.get(field_first)
        if v in (None, ""):
            v = r.get("valor")
        return parse_money(v)

    def _bucket(rows: list[dict], use_saldo: bool) -> dict[str, float]:
        b: dict[str, float] = defaultdict(float)
        for r in rows:
            venc = parse_date(r.get("vencimento") or "")
            if not venc:
                continue
            val = parse_money(r.get("saldo")) if use_saldo else parse_money(r.get("valor"))
            if use_saldo and val == 0:
                val = parse_money(r.get("valor"))
            b[f"{venc.year}-{venc.month:02d}"] += val
        return b

    re_real = _bucket(recebidas, False)
    pa_real = _bucket(pagas, False)
    re_open = _bucket(receber, True)
    pa_open = _bucket(em_aberto, True)

    # Janela: jan/ano-atual → atual + 2 meses
    meses: list[tuple[int, int]] = []
    y0 = today.year
    for m in range(1, 13):
        meses.append((y0, m))
    # adiciona meses do próximo ano se atual+2 ultrapassar dez
    extra = []
    ym = today.month + 2
    yy = today.year
    while ym > 12:
        ym -= 12
        yy += 1
        for mm in range(1, ym + 1):
            extra.append((yy, mm))
    # corta de jan/atual até atual+2
    cur_idx = today.year * 12 + today.month
    janela = [(yy_, mm_) for (yy_, mm_) in (meses + extra)
              if yy_ * 12 + mm_ <= cur_idx + 2]

    cur_key = today.year * 12 + today.month
    rows = []
    for (yy_, mm_) in janela:
        key = f"{yy_:04d}-{mm_:02d}"
        is_proj = (yy_ * 12 + mm_) >= cur_key
        ent = re_open.get(key, 0) if is_proj else re_real.get(key, 0)
        sai = pa_open.get(key, 0) if is_proj else pa_real.get(key, 0)
        rows.append({"ano": yy_, "mes": mm_, "ent": ent, "sai": sai,
                     "proj": is_proj})

    maxv = max([max(r["ent"], r["sai"]) for r in rows] + [1])
    PX = 150  # altura máx do plot

    cols = []
    for r in rows:
        mn = _MONTH_PT[r["mes"] - 1].capitalize()
        h_ent = round(r["ent"] / maxv * PX)
        h_sai = round(r["sai"] / maxv * PX)
        op = "0.5" if r["proj"] else "1"
        dash = "border:1px dashed var(--bd);" if r["proj"] else ""
        net = r["ent"] - r["sai"]
        net_col = "var(--green)" if net >= 0 else "var(--red)"
        cols.append(
            '<div style="display:flex;flex-direction:column;align-items:center;flex:1;min-width:0">'
            f'<div style="height:{PX}px;display:flex;align-items:flex-end;gap:3px">'
            f'<div title="Entradas {_brl(r["ent"])}" style="width:13px;height:{h_ent}px;background:var(--green);opacity:{op};{dash}border-radius:2px 2px 0 0"></div>'
            f'<div title="Saídas {_brl(r["sai"])}" style="width:13px;height:{h_sai}px;background:var(--red);opacity:{op};{dash}border-radius:2px 2px 0 0"></div>'
            '</div>'
            f'<div style="font-family:var(--mono);font-size:9.5px;color:var(--t3);margin-top:5px">{mn}/{str(r["ano"])[2:]}</div>'
            f'<div style="font-family:var(--mono);font-size:9px;color:{net_col};margin-top:1px">{_brl_signed_k(net)}</div>'
            '</div>'
        )

    leg = (
        '<div style="display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:10.5px;color:var(--t2)">'
        '<span style="display:flex;align-items:center;gap:4px"><span style="width:10px;height:10px;border-radius:2px;background:var(--green)"></span>Entradas</span>'
        '<span style="display:flex;align-items:center;gap:4px"><span style="width:10px;height:10px;border-radius:2px;background:var(--red)"></span>Saídas</span>'
        '<span style="display:flex;align-items:center;gap:4px"><span style="width:10px;height:10px;border-radius:2px;background:var(--t3);opacity:0.5;border:1px dashed var(--bd)"></span>Projetado (em aberto)</span>'
        '<span style="color:var(--t3)">· abaixo de cada mês: resultado líquido (entrada − saída)</span>'
        '</div>'
    )

    return (
        '<div class="card" style="margin-bottom:12px">'
        '<div class="ct">Fluxo de caixa mensal — entradas vs saídas '
        '<span class="ct-tag">realizado + projetado</span></div>'
        f'<div style="display:flex;gap:6px;align-items:flex-end;margin-top:12px;padding:0 4px">{"".join(cols)}</div>'
        f'{leg}</div>'
    )


def _brl_signed_k(v: float) -> str:
    """Resultado compacto com sinal: +R$ 12K / -R$ 8K."""
    sign = "+" if v >= 0 else "-"
    a = abs(v)
    if a >= 1000:
        return f"{sign}R$ {a/1000:.0f}K"
    return f"{sign}R$ {round(a)}"


def _strip_accents_upper(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.upper())
                   if unicodedata.category(c) != "Mn")


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def pagar_to_js(items: list[dict]) -> str:
    parts = []
    for it in items:
        parts.append(
            "{" +
            f"id:'{js_str(it['id'])}'," +
            f"n:'{js_str(it['n'])}'," +
            f"cat:'{js_str(it['cat'])}'," +
            f"doc:'{js_str(it['doc'])}'," +
            f"venc:'{js_str(it['venc'])}'," +
            f"window:'{js_str(it['window'])}'," +
            f"val:{_js_num(it['val'])}" +
            "}"
        )
    return "[\n  " + ",\n  ".join(parts) + "\n]"


# ──────────────────────────────────────────────
# RECEITA POR CLIENTE  (tab Financeiro)
# ──────────────────────────────────────────────

def compute_recebido_data(recebidas: list[dict]) -> list[dict]:
    """Agrega contas_receber_recebidas por cliente → RECEBIDO_DATA."""
    from collections import defaultdict
    by_client: dict[str, dict] = defaultdict(lambda: {"val": 0.0, "count": 0})
    for c in recebidas:
        nome = (c.get("contato_nome") or c.get("nomeContato") or "(sem cliente)").strip()
        valor = parse_money(c.get("valor"))
        if valor <= 0:
            continue
        by_client[nome]["val"] += valor
        by_client[nome]["count"] += 1

    items = []
    for i, (nome, agg) in enumerate(
        sorted(by_client.items(), key=lambda x: -x[1]["val"])
    ):
        items.append({
            "id":    f"rc{i+1:03d}",
            "n":     nome,
            "val":   round(agg["val"]),
            "count": agg["count"],
        })
    return items


# ──────────────────────────────────────────────────────────────────────
# CLIENTES (aba "Clientes") — Bling-driven com classificação manual
# ──────────────────────────────────────────────────────────────────────
def _load_clientes_classificacao(path: Path | None = None) -> dict:
    """Lê o arquivo clientes_classificacao.json (se existir).

    Retorna {alias_upper: {id, display, cat, uso, abr, obs}}.
    Se o arquivo não existir, devolve dict vazio — clientes ficarão sem
    classificação manual (cat='sem_classificacao').
    """
    p = path or (HERE / "clientes_classificacao.json")
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for cid, info in (raw.get("clientes") or {}).items():
        for alias in info.get("aliases", []):
            out[alias.upper().strip()] = {
                "id":      cid,
                "display": info.get("display", cid),
                "cat":     info.get("cat", "sem_classificacao"),
                "uso":     info.get("uso", "?"),
                "abr":     info.get("abr", "?"),
                "obs":     info.get("obs", ""),
            }
    return out


def compute_cli_data_totvs(totvs_snapshot_path: Path,
                           classif: dict,
                           today: date,
                           window_months: int = 12) -> list[dict]:
    """Constrói lista de clientes FINAIS a partir do snapshot Totvs.

    Os relatórios Totvs (.eml/.xlsx do email comissoes.cst@totvs.com.br)
    trazem a abertura por cliente que USA o software — não por contato
    Bling (que é só TOTVS x N filiais). Esta é a fonte verdadeira da
    composição da carteira da Easy.

    Estratégia:
      - Itera docs do snapshot na janela de N meses fechados
      - Considera apenas tipos REAL, BASE e COMPLEMENTAR (ignora PROVISAO
        pra não duplicar — PROVISAO é estimativa antecipada de BASE)
      - Agrega por nome do cliente final (clientes[].cliente) somando
        valor_comissao (= valor que entra na Easy)
      - Captura recorrente (meses de contrato), qtd_mes_cli, último mês
        com pagamento
      - Classifica automaticamente (regras abaixo) ou usa override do
        classif (clientes_classificacao.json)

    Regras automáticas de classificação:
      - 'garantido' = ativo nos últimos 2 meses + recorrente >= 12
      - 'medio'     = ativo mas intermitente OU recorrente < 12
      - 'alto'      = sem pagamento há > 3 meses (risco churn)
      - 'sem_classificacao' nunca acontece (sempre cai num dos 3)
    """
    if not totvs_snapshot_path.exists():
        return []

    snap = json.loads(totvs_snapshot_path.read_text(encoding="utf-8"))
    docs = snap.get("documentos", [])

    # Janela: últimos N meses fechados (não conta mês corrente)
    end_y, end_m = today.year, today.month - 1
    if end_m == 0:
        end_y -= 1
        end_m = 12

    start_y, start_m = end_y, end_m
    for _ in range(window_months - 1):
        start_m -= 1
        if start_m == 0:
            start_m = 12
            start_y -= 1

    def comp_int(ym: str) -> int:
        try:
            return int(ym[:4]) * 12 + int(ym[5:7])
        except Exception:
            return 0

    start_int = start_y * 12 + start_m
    end_int = end_y * 12 + end_m

    # Razão social das filiais TOTVS (cnpj → nome curto), capturado do
    # campo cnpjs_filial dos docs
    filiais_meta: dict[str, str] = {}

    by_id: dict[str, dict] = {}
    for doc in docs:
        tipo = doc.get("tipo")
        comp = (doc.get("competencia") or "")[:7]
        if not comp:
            continue
        cint = comp_int(comp)
        if cint < start_int or cint > end_int:
            continue
        if tipo not in ("REAL", "BASE", "COMPLEMENTAR"):
            continue
        for f in doc.get("cnpjs_filial", []):
            cnpj = f.get("cnpj")
            razao = (f.get("razao_social") or "").strip()
            if cnpj and razao and cnpj not in filiais_meta:
                filiais_meta[cnpj] = razao
        for c in doc.get("clientes", []):
            nome = (c.get("cliente") or "").strip()
            if not nome:
                continue
            val = float(c.get("valor_comissao") or 0)
            if val <= 0:
                continue

            override = classif.get(nome.upper())
            cid = (override or {}).get("id") or \
                f"t_{re.sub(r'[^A-Z0-9]+', '_', nome.upper())[:30]}"
            display = (override or {}).get("display") or _trunc(nome.title(), 38)

            rec = by_id.setdefault(cid, {
                "id":         cid,
                "n":          display,
                "total":      0.0,
                "meses":      set(),
                "last_mes":   "",
                "recorrente": 0,
                "qtd_mes":    0,
                "contratos":  set(),
                "override":   override,
                "aliases":    set(),
                "por_filial": {},  # cnpj → total na janela
            })
            rec["total"] += val
            rec["meses"].add(comp)
            if comp > rec["last_mes"]:
                rec["last_mes"] = comp
            rec_v = int(c.get("recorrente") or 0)
            if rec_v > rec["recorrente"]:
                rec["recorrente"] = rec_v
            qmes = int(c.get("qtd_mes_cli") or 0)
            if qmes > rec["qtd_mes"]:
                rec["qtd_mes"] = qmes
            if c.get("contrato"):
                rec["contratos"].add(c["contrato"])
            rec["aliases"].add(nome)
            cnpj_f = c.get("cnpj_filial")
            if cnpj_f:
                rec["por_filial"][cnpj_f] = \
                    rec["por_filial"].get(cnpj_f, 0) + val

    # Classifica e monta saída
    # Referência: o snapshot Totvs sempre fica ~1-2 meses atrás (TOTVS
    # envia o relatório do mês N no email do mês N+1). Por isso comparamos
    # last_mes do cliente contra a competência MAIS RECENTE do snapshot,
    # não contra "hoje".
    last_snap_int = max((comp_int(r["last_mes"]) for r in by_id.values()),
                        default=0)
    items = []
    for cid, rec in by_id.items():
        n_meses = len(rec["meses"])
        valor_mes = round(rec["total"] / n_meses) if n_meses else 0
        last_int = comp_int(rec["last_mes"])
        # Quantos meses o cliente está atrasado vs a competência mais
        # recente disponível no snapshot.
        meses_atras = last_snap_int - last_int

        # Classificação automática (override prevalece)
        if rec["override"]:
            cat = rec["override"].get("cat", "garantido")
            uso = rec["override"].get("uso", "Ativo")
            obs = rec["override"].get("obs", "")
        else:
            if meses_atras >= 3:
                cat, uso, obs = "alto", "Inativo", \
                    (f"Sem comissão há {meses_atras+1} meses "
                     f"(último: {rec['last_mes'][:7]})")
            elif meses_atras >= 1 or n_meses < window_months // 2:
                cat, uso, obs = "medio", "Intermitente", \
                    (f"{n_meses}/{window_months} meses ativos · "
                     f"contrato {rec['recorrente']}m")
            elif rec["recorrente"] < 12:
                cat, uso, obs = "medio", "Ativo", \
                    (f"Contrato recente ({rec['recorrente']}m) · "
                     f"{n_meses}/{window_months}m ativos")
            else:
                cat, uso, obs = "garantido", "Ativo", \
                    (f"Contrato {rec['recorrente']}m · "
                     f"{n_meses}/{window_months}m ativos")

        abr_label = "Ativo" if meses_atras == 0 else (
            "—" if meses_atras >= 3 else "?")

        # Identifica a filial PRINCIPAL (que mais contribui no valor)
        principal_cnpj = ""
        if rec["por_filial"]:
            principal_cnpj = max(rec["por_filial"],
                                 key=rec["por_filial"].get)

        items.append({
            "id":            cid,
            "n":             rec["n"],
            "cat":           cat,
            "valor":         valor_mes,
            "uso":           uso,
            "abr":           abr_label,
            "obs":           obs,
            "total":         round(rec["total"]),
            "n_meses":       n_meses,
            "last_mes":      rec["last_mes"],
            "recorrente":    rec["recorrente"],
            "contratos":     sorted(rec["contratos"]),
            "aliases":       sorted(rec["aliases"]),
            "filial_cnpj":   principal_cnpj,
            "filial_nome":   filiais_meta.get(principal_cnpj, ""),
            "por_filial":    dict(rec["por_filial"]),
        })

    cat_order = {"garantido": 0, "medio": 1, "alto": 2,
                 "sem_classificacao": 3}
    items.sort(key=lambda r: (cat_order.get(r["cat"], 9), -r["valor"]))
    return items, filiais_meta


def compute_filiais_totvs(cli_data: list[dict],
                          filiais_meta: dict[str, str]) -> list[dict]:
    """Agrega clientes finais por filial TOTVS intermediadora.

    Cada item da lista é um intermediário (matriz, BH, SC, Large) com:
      - cnpj, display
      - total_mes (= sum dos clientes / 12m)
      - n_clientes_ativos (cat != alto)
      - n_clientes_total
      - top_clientes (top 5 por valor)
    """
    from collections import defaultdict
    by_cnpj: dict[str, dict] = defaultdict(lambda: {
        "total":    0.0,
        "clis_ativos": [],
        "clis_todos":  [],
    })
    for c in cli_data:
        for cnpj, val in (c.get("por_filial") or {}).items():
            ag = by_cnpj[cnpj]
            ag["total"] += val
            ag["clis_todos"].append(
                {"n": c["n"], "valor_janela": val, "cat": c["cat"]})
            if c["cat"] != "alto":
                ag["clis_ativos"].append(c)

    # Display amigável (mapa estável + fallback ao razao_social capturado)
    nomes_amig = {
        "53113791000122": "TOTVS Matriz (SP)",
        "53113791001285": "TOTVS BH",
        "53113791001790": "TOTVS Santa Catarina",
        "82373077000252": "TOTVS Large Enterprise",
    }

    out = []
    for cnpj, ag in by_cnpj.items():
        display = nomes_amig.get(
            cnpj, filiais_meta.get(cnpj, f"CNPJ {cnpj[-4:]}"))
        # Top 5 por valor (na janela)
        top = sorted(ag["clis_todos"],
                     key=lambda x: -x["valor_janela"])[:5]
        out.append({
            "cnpj":     cnpj,
            "display":  display,
            "total_12m": round(ag["total"]),
            "media_mes": round(ag["total"] / 12),
            "n_total":  len(ag["clis_todos"]),
            "n_ativos": len(ag["clis_ativos"]),
            "top":      [{"n": t["n"], "v": round(t["valor_janela"])}
                         for t in top],
        })
    out.sort(key=lambda x: -x["total_12m"])
    return out


def render_filiais_html(filiais: list[dict]) -> str:
    """Renderiza cards horizontais da abertura por filial TOTVS."""
    if not filiais:
        return ""
    cards = []
    from html import escape as _esc
    for f in filiais:
        top_html = "".join(
            f'<div style="display:flex;justify-content:space-between;'
            f'font-size:10.5px;font-family:var(--mono);color:var(--t2);'
            f'padding:2px 0">'
            f'<span style="overflow:hidden;text-overflow:ellipsis;'
            f'white-space:nowrap;max-width:180px">{_esc(t["n"])}</span>'
            f'<span style="color:var(--blue);font-weight:600">'
            f'R$ {_brl_num(t["v"])}</span></div>'
            for t in f["top"]
        )
        resto = max(0, f["n_total"] - len(f["top"]))
        resto_html = (
            f'<div style="font-size:10.5px;color:var(--t3);'
            f'padding:2px 0;font-style:italic">+ {resto} outros</div>'
            if resto > 0 else "")

        cards.append(f'''  <div class="card" style="padding:14px">
    <div style="font-size:10.5px;color:var(--t3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px">Intermediário</div>
    <div style="font-size:14px;font-weight:600;margin-bottom:6px">{_esc(f["display"])}</div>
    <div style="font-size:22px;font-weight:700;color:var(--blue);font-family:var(--mono)">R$ {_brl_num(f["media_mes"])}<span style="font-size:11px;color:var(--t3);font-weight:400">/mês</span></div>
    <div style="font-size:11px;color:var(--t2);margin-bottom:10px">{f["n_ativos"]} ativos · {f["n_total"]} total · R$ {_brl_num(f["total_12m"])} em 12m</div>
    <div style="border-top:1px solid var(--br);padding-top:8px">
      <div style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px">Top clientes (12m)</div>
      {top_html}
      {resto_html}
    </div>
  </div>''')

    n_cards = len(cards)
    grid_class = "g4" if n_cards == 4 else ("g3" if n_cards == 3 else "g2")
    return (
        f'<div class="sl">Abertura por intermediário TOTVS '
        f'<span style="color:var(--t3);font-size:11px">— qual filial '
        f'faz o repasse de cada cliente</span></div>\n'
        f'<div class="{grid_class}" style="margin-bottom:14px">\n'
        + "\n".join(cards) + "\n</div>"
    )


def compute_cli_data(recebidas: list[dict],
                     em_aberto_rec: list[dict],
                     classif: dict,
                     today: date,
                     window_months: int = 6) -> list[dict]:
    """Constrói lista de clientes a partir do Bling.

    Para cada cliente lógico (após merge de aliases):
      - val_mes  = média mensal recebida na janela (window_months meses fechados,
                   contando só meses que tiveram lançamento)
      - n_meses  = quantos meses tiveram lançamento na janela
      - total    = soma na janela
      - last_mes = última competência com lançamento
      - cat/uso/abr/obs vêm do classif (manual)

    Clientes sem entrada em classif recebem cat='sem_classificacao'.
    """
    # Janela: window_months meses fechados, terminando no mês anterior ao atual.
    end_y, end_m = today.year, today.month - 1
    if end_m == 0:
        end_y -= 1
        end_m = 12
    end_ym = f"{end_y:04d}-{end_m:02d}"

    start_y, start_m = end_y, end_m
    for _ in range(window_months - 1):
        start_m -= 1
        if start_m == 0:
            start_m = 12
            start_y -= 1
    start_ym = f"{start_y:04d}-{start_m:02d}"

    # Agrega por cliente lógico
    by_id: dict[str, dict] = {}
    fallback_count = 0
    for c in recebidas:
        nome = (c.get("contato_nome") or "").strip()
        if not nome:
            continue
        valor = parse_money(c.get("valor"))
        if valor <= 0:
            continue
        venc = (c.get("vencimento") or "")[:7]

        info = classif.get(nome.upper())
        if info:
            cid = info["id"]
        else:
            # Cliente sem classificação — id deduzido do nome.
            cid = f"_unk_{nome[:20].upper().replace(' ', '_')}"
            fallback_count += 1

        rec = by_id.setdefault(cid, {
            "id":       cid,
            "n":        (info or {}).get("display", nome),
            "cat":      (info or {}).get("cat", "sem_classificacao"),
            "uso":      (info or {}).get("uso", "?"),
            "abr":      (info or {}).get("abr", "?"),
            "obs":      (info or {}).get("obs", ""),
            "total":    0.0,
            "meses":    set(),
            "last_mes": "",
            "aliases_bling": set(),
        })
        rec["aliases_bling"].add(nome)
        if venc:
            rec["last_mes"] = max(rec["last_mes"], venc)
            # Conta na janela?
            if start_ym <= venc <= end_ym:
                rec["meses"].add(venc)
                rec["total"] += valor

    items = []
    for cid, rec in by_id.items():
        n_meses = len(rec["meses"])
        val_mes = round(rec["total"] / n_meses) if n_meses else 0
        items.append({
            "id":       cid,
            "n":        rec["n"],
            "cat":      rec["cat"],
            "valor":    val_mes,
            "uso":      rec["uso"],
            "abr":      rec["abr"],
            "obs":      rec["obs"],
            "total":    round(rec["total"]),
            "n_meses":  n_meses,
            "last_mes": rec["last_mes"],
            "aliases":  sorted(rec["aliases_bling"]),
        })

    # Ordena: por categoria (garantido > medio > alto > sem_classif) e depois
    # por valor decrescente.
    cat_order = {"garantido": 0, "medio": 1, "alto": 2, "sem_classificacao": 3}
    items.sort(key=lambda r: (cat_order.get(r["cat"], 9), -r["valor"]))
    return items


def cli_data_to_js(items: list[dict]) -> str:
    parts = []
    for it in items:
        parts.append(
            "{"
            + f"id:'{js_str(it['id'])}',"
            + f"n:'{js_str(it['n'])}',"
            + f"cat:'{js_str(it['cat'])}',"
            + f"valor:{_js_num(it['valor'])},"
            + f"uso:'{js_str(it['uso'])}',"
            + f"abr:'{js_str(it['abr'])}',"
            + f"obs:'{js_str(it['obs'])}'"
            + "}"
        )
    return "[\n  " + ",\n  ".join(parts) + "\n]"


def cli_kpis(items: list[dict]) -> dict[str, int | float]:
    """Agrega KPIs por categoria de risco."""
    out = {"garantido": 0, "medio": 0, "alto": 0, "sem_classificacao": 0,
           "n_garantido": 0, "n_medio": 0, "n_alto": 0, "n_sem": 0,
           "tot_val": 0, "n_tot": 0}
    for it in items:
        cat = it["cat"]
        out[cat] = out.get(cat, 0) + it["valor"]
        if cat == "garantido": out["n_garantido"] += 1
        elif cat == "medio":   out["n_medio"]    += 1
        elif cat == "alto":    out["n_alto"]     += 1
        else:                  out["n_sem"]      += 1
        out["tot_val"] += it["valor"]
        out["n_tot"]   += 1
    return out


def recebido_to_js(items: list[dict]) -> str:
    parts = []
    for it in items:
        parts.append(
            "{" +
            f"id:'{js_str(it['id'])}'," +
            f"n:'{js_str(it['n'])}'," +
            f"val:{_js_num(it['val'])}," +
            f"count:{it['count']}" +
            "}"
        )
    return "[\n  " + ",\n  ".join(parts) + "\n]"


# ──────────────────────────────────────────────
# DRE — RECEITA VS DESPESAS (REAL + PROJETADO)
# ──────────────────────────────────────────────

def compute_dre_data(
    pagas: list[dict],
    recebidas: list[dict],
    em_aberto: list[dict],
    receber_em_aberto: list[dict],
    today: date,
) -> list[dict]:
    """
    Monta o DRE mês a mês misturando real (pagos/recebidos) com projetado (em aberto).
    Meses passados completos → tipo 'real'. Mês atual + futuros → tipo 'projetado'.
    Retorna lista de dicts ordenados por mês.
    """
    # ── Acumuladores por mês (chave: 'YYYY-MM') ──
    desp_real: dict[str, float] = defaultdict(float)
    rec_real:  dict[str, float] = defaultdict(float)
    desp_plan: dict[str, float] = defaultdict(float)
    rec_plan:  dict[str, float] = defaultdict(float)

    cutoff = today.strftime("%Y-%m")   # mês atual não é "real" ainda

    for r in pagas:
        m = (r.get("vencimento") or "")[:7]
        if m:
            desp_real[m] += parse_money(r.get("valor", 0))

    for r in recebidas:
        m = (r.get("vencimento") or "")[:7]
        if m:
            rec_real[m] += parse_money(r.get("valor", 0))

    for r in em_aberto:
        m = (r.get("vencimento") or "")[:7]
        if m:
            v = parse_money(r.get("saldo") or r.get("valor", 0))
            desp_plan[m] += v

    for r in receber_em_aberto:
        m = (r.get("vencimento") or "")[:7]
        if m:
            v = parse_money(r.get("saldo") or r.get("valor", 0))
            rec_plan[m] += v

    # ── Conjunto de meses com algum dado ──
    all_months = sorted(
        set(desp_real) | set(rec_real) | set(desp_plan) | set(rec_plan)
    )

    MN = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

    result = []
    for m in all_months:
        try:
            yr, mo = int(m[:4]), int(m[5:7])
        except ValueError:
            continue
        label = MN[mo - 1] + "/" + str(yr)[-2:]
        # mês passado = real; mês atual = misto (real + a vencer); futuro = projetado
        if m < cutoff:
            # passado: pagos/recebidos + qualquer overdue ainda em aberto
            desp = desp_real.get(m, 0) + desp_plan.get(m, 0)
            rec  = rec_real.get(m, 0) + rec_plan.get(m, 0)
            tipo = "real"
        elif m == cutoff:
            # mês atual: pagos/recebidos até agora + em_aberto pra o resto do mês
            desp = desp_real.get(m, 0) + desp_plan.get(m, 0)
            rec  = rec_real.get(m, 0) + rec_plan.get(m, 0)
            tipo = "projetado"  # mês ainda em curso
        else:
            # futuro: só projetado
            desp = desp_plan.get(m, 0)
            rec  = rec_plan.get(m, 0)
            tipo = "projetado"
        if desp == 0 and rec == 0:
            continue
        result.append({
            "month": m,
            "label": label,
            "tipo":  tipo,
            "rec":   round(rec, 2),
            "desp":  round(desp, 2),
        })

    return result


def dre_to_js(items: list[dict]) -> str:
    """Serializa DRE_DATA para JS."""
    parts = []
    for it in items:
        parts.append(
            "{"
            + f"month:'{it['month']}',"
            + f"label:'{it['label']}',"
            + f"tipo:'{it['tipo']}',"
            + f"rec:{_js_num(it['rec'])},"
            + f"desp:{_js_num(it['desp'])}"
            + "}"
        )
    return "[\n  " + ",\n  ".join(parts) + "\n]"


def compute_dre_detail(
    pagas: list[dict],
    recebidas: list[dict],
    em_aberto: list[dict],
    receber_em_aberto: list[dict],
    today: date,
) -> dict[str, dict]:
    """
    Retorna {month: {"rec": [items], "desp": [items]}} com abertura completa
    por mês. Cada item tem: contato, historico, valor, doc, venc, categoria, tipo.
    Meses passados (< mês atual) = real; mês atual e futuros = projetado.
    """
    from collections import defaultdict
    result: dict[str, dict] = defaultdict(lambda: {"rec": [], "desp": []})
    cutoff = today.strftime("%Y-%m")

    def to_item(r: dict, tipo: str) -> dict:
        v = parse_money(r.get("saldo")) if tipo == "projetado" and r.get("saldo") else parse_money(r.get("valor", 0))
        return {
            "contato": (r.get("contato_nome") or "—").strip(),
            "historico": (r.get("historico") or "").strip(),
            "valor": round(v, 2),
            "doc": (r.get("numero_documento") or "").strip(),
            "venc": r.get("vencimento", ""),
            "categoria": (r.get("categoria_descricao") or "").strip(),
            "tipo": tipo,
        }

    # Pagos/recebidos vão em qualquer mês (passado ou atual)
    for r in pagas:
        m = (r.get("vencimento") or "")[:7]
        if m:
            result[m]["desp"].append(to_item(r, "real"))
    for r in recebidas:
        m = (r.get("vencimento") or "")[:7]
        if m:
            result[m]["rec"].append(to_item(r, "real"))

    # Em aberto: futuro = projetado; passado ou atual = real (atrasado/pendente)
    for r in em_aberto:
        m = (r.get("vencimento") or "")[:7]
        if not m:
            continue
        tipo = "projetado" if m > cutoff else "real"
        result[m]["desp"].append(to_item(r, tipo))
    for r in receber_em_aberto:
        m = (r.get("vencimento") or "")[:7]
        if not m:
            continue
        tipo = "projetado" if m > cutoff else "real"
        result[m]["rec"].append(to_item(r, tipo))

    # Ordena por valor desc dentro de cada mês
    for m in result:
        result[m]["rec"].sort(key=lambda x: -x["valor"])
        result[m]["desp"].sort(key=lambda x: -x["valor"])

    return dict(result)


def dre_detail_to_js(detail: dict[str, dict]) -> str:
    """Serializa DRE_DETAIL como JSON (válido JS)."""
    import json
    return json.dumps(detail, ensure_ascii=False, separators=(",", ":"))


# ──────────────────────────────────────────────
# HELPERS DE FORMATAÇÃO
# ──────────────────────────────────────────────

def _brl(v: float) -> str:
    """R$ 1.234 (separador de milhar PT-BR)."""
    return "R$ " + f"{round(v):,}".replace(",", ".")


def _brl_num(v: float) -> str:
    """1.234 (só o número, sem 'R$ ')."""
    return f"{round(v):,}".replace(",", ".")


def _kbrl(v: float) -> str:
    """R$ 1.234 ou R$ 1 K para valores >= 1000."""
    if abs(v) >= 1_000_000:
        return f"R$ {round(v / 1_000_000, 1)} M"
    if abs(v) >= 1_000:
        return f"R$ {round(v / 1_000)} K"
    return _brl(v)


def _signed_kbrl(v: float) -> str:
    prefix = "+" if v > 0 else ""
    return prefix + _kbrl(v)


# ──────────────────────────────────────────────
# OVERVIEW — KPIs E ALERTAS DINÂMICOS
# ──────────────────────────────────────────────

def load_caixa_config() -> dict:
    """Lê caixa_config.json (mantido manualmente, como clientes_classificacao.json).
    Contém o saldo em caixa atual p/ o cálculo de Runway. O Bling não expõe
    saldo bancário, então esse valor é informado pelo usuário. Ausente → {}."""
    try:
        p = Path(__file__).resolve().parent / "caixa_config.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def compute_overview_hero_sub(months: list[date], total_pago: int) -> str:
    if not months:
        return "fonte: Bling ERP"
    inicio = month_label(months[0])
    fim    = month_label(months[-1])
    return f"{inicio}–{fim} · total pago: {_brl(total_pago)} · fonte: Bling ERP"


def compute_overview_pills(pagar_data: list, cx_data: list) -> str:
    pills = []
    venc_rec = sum(r["val"] for r in cx_data if r["sit"] == "Atrasada")
    venc_pag = sum(r["val"] for r in pagar_data if r["window"] == "vencido")
    sem_cat  = sum(r["val"] for r in pagar_data if r["cat"] == "(sem categoria)")
    prox30   = sum(r["val"] for r in pagar_data if r["window"] == "30d")

    if venc_rec > 0:
        pills.append(f'<span class="pill pr"><span class="pdot"></span>{_brl(venc_rec)} vencidos a receber</span>')
    if venc_pag > 0:
        pills.append(f'<span class="pill pr"><span class="pdot"></span>{_brl(venc_pag)} vencidos a pagar</span>')
    if sem_cat > 0:
        pills.append(f'<span class="pill pa"><span class="pdot"></span>{_kbrl(sem_cat)} sem categoria</span>')
    if prox30 > 0 and not venc_pag:
        pills.append(f'<span class="pill pa"><span class="pdot"></span>{_kbrl(prox30)} a pagar em 30d</span>')
    if not pills:
        pills.append('<span class="pill pg2"><span class="pdot"></span>Tudo em dia</span>')
    return "\n    ".join(pills)


def compute_overview_kpis_html(rows: list, pagar_data: list, cx_data: list, months: list[date],
                               saldo_caixa: float | None = None) -> str:
    n = len(months) or 1
    total_pago   = sum(r["total"] for r in rows)
    media        = total_pago / n
    total_rec    = sum(r["val"] for r in cx_data)
    venc_rec     = sum(r["val"] for r in cx_data if r["sit"] == "Atrasada")
    n_rec        = len(cx_data)
    pagar_30d    = sum(r["val"] for r in pagar_data if r["window"] in ("vencido", "30d"))
    venc_pag     = sum(r["val"] for r in pagar_data if r["window"] == "vencido")
    sem_cat_val  = sum(r["val"] for r in pagar_data if r["cat"] == "(sem categoria)")
    sem_cat_n    = sum(1 for r in pagar_data if r["cat"] == "(sem categoria)")

    rec_cor  = "red"   if venc_rec > 0 else "green"
    pag_cor  = "red"   if venc_pag > 0 else "amber"
    sc_cor   = "amber" if sem_cat_val > 0 else "green"

    rec_sub  = f"{_brl(venc_rec)} vencidos" if venc_rec > 0 else "nenhum vencido"
    pag_sub  = f"{_brl(venc_pag)} vencidos" if venc_pag > 0 else "nenhum vencido"
    sc_sub   = f"{sem_cat_n} lançamentos s/ cat." if sem_cat_n > 0 else "todos classificados"

    cards = [
        f'<div class="met blue"><div class="ml">Total pago ({n} meses)</div>'
        f'<div class="mv blue" title="{_brl(total_pago)}">{_kbrl(total_pago)}</div>'
        f'<div class="ms">fonte: Bling · confirmados</div></div>',

        f'<div class="met blue"><div class="ml">Média mensal</div>'
        f'<div class="mv blue" title="{_brl(media)}">{_kbrl(media)}</div>'
        f'<div class="ms">últimos {n} meses</div></div>',

        f'<div class="met {rec_cor}"><div class="ml">A receber em aberto</div>'
        f'<div class="mv {rec_cor}" title="{_brl(total_rec)}">{_kbrl(total_rec)}</div>'
        f'<div class="ms">{n_rec} lançamentos · {rec_sub}</div></div>',

        f'<div class="met {pag_cor}"><div class="ml">A pagar — vencido + 30d</div>'
        f'<div class="mv {pag_cor}" title="{_brl(pagar_30d)}">{_kbrl(pagar_30d)}</div>'
        f'<div class="ms">{pag_sub}</div></div>',

    ]

    # Runway = quanto o caixa atual dura se a receita parar (caixa ÷ burn bruto
    # mensal). burn = `media` (média mensal de saídas pagas, fonte Bling).
    # saldo_caixa vem de caixa_config.json (mantido manualmente) — sem ele,
    # mostramos a chamada pra informar o saldo em vez de inventar número.
    if saldo_caixa and media > 0:
        runway = saldo_caixa / media
        rw_cor = "red" if runway < 3 else ("amber" if runway < 6 else "green")
        rw_val = (f"{runway:.1f}".replace(".", ",") + " meses") if runway < 100 else "99+ meses"
        rw_sub = f"caixa {_kbrl(saldo_caixa)} ÷ burn {_kbrl(media)}/mês"
    else:
        rw_cor = "amber"
        rw_val = "—"
        rw_sub = "informe saldo_caixa em caixa_config.json"
    cards.append(
        f'<div class="met {rw_cor}"><div class="ml">Runway (caixa ÷ burn)</div>'
        f'<div class="mv {rw_cor}">{rw_val}</div>'
        f'<div class="ms">{rw_sub}</div></div>'
    )
    return "\n  ".join(cards)


def compute_overview_riscos_html(cx_data: list, pagar_data: list,
                                 cli_data: list | None = None) -> str:
    alerts = []

    # Concentração de receita — dependência de poucos clientes (risco de caixa
    # típico de empresa de software/serviços). Base: receita rolling por cliente
    # (campo "total" de cli_data, derivado do Bling/Totvs).
    if cli_data:
        ranked = sorted(cli_data, key=lambda x: -x.get("total", 0))
        tot = sum(x.get("total", 0) for x in cli_data) or 1
        top3 = [x for x in ranked[:3] if x.get("total", 0) > 0]
        if top3:
            pct = round(100 * sum(x.get("total", 0) for x in top3) / tot)
            nomes = ", ".join(x["n"] for x in top3)
            if pct >= 50:
                alerts.append(
                    f'<div class="alt alr"><div class="ats">Concentração de receita: top 3 = {pct}%</div>'
                    f'<div class="asd">{nomes} — dependência alta; diversificar reduz risco de caixa.</div></div>'
                )
            elif pct >= 35:
                alerts.append(
                    f'<div class="alt ala"><div class="ats">Concentração moderada: top 3 = {pct}%</div>'
                    f'<div class="asd">{nomes} — monitorar dependência da carteira.</div></div>'
                )

    # Vencidos a receber
    venc_rec = [r for r in cx_data if r["sit"] == "Atrasada"]
    if venc_rec:
        total_v = sum(r["val"] for r in venc_rec)
        clientes = list({r["c"].split("—")[0].strip() for r in venc_rec})[:3]
        alerts.append(
            f'<div class="alt alr"><div class="ats">{_brl(total_v)} vencidos a receber '
            f'({len(venc_rec)} lançamentos)</div>'
            f'<div class="asd">{", ".join(clientes)} — acionar cobrança.</div></div>'
        )

    # Vencidos a pagar
    venc_pag = [r for r in pagar_data if r["window"] == "vencido"]
    if venc_pag:
        total_v = sum(r["val"] for r in venc_pag)
        forn = list({r["n"][:28] for r in venc_pag})[:3]
        alerts.append(
            f'<div class="alt alr"><div class="ats">{_brl(total_v)} a pagar vencidos '
            f'({len(venc_pag)} lançamentos)</div>'
            f'<div class="asd">{", ".join(forn)}</div></div>'
        )

    # Sem categoria
    sem_cat = [r for r in pagar_data if r["cat"] == "(sem categoria)"]
    if sem_cat:
        total_sc = sum(r["val"] for r in sem_cat)
        alerts.append(
            f'<div class="alt ala"><div class="ats">{_kbrl(total_sc)} sem categoria '
            f'({len(sem_cat)} lançamentos)</div>'
            f'<div class="asd">Classifique no Bling — impacta DRE.</div></div>'
        )

    # A pagar próximos 30 dias
    prox30 = [r for r in pagar_data if r["window"] == "30d"]
    if prox30:
        total_30 = sum(r["val"] for r in prox30)
        alerts.append(
            f'<div class="alt ala"><div class="ats">{_kbrl(total_30)} vencem em até 30 dias '
            f'({len(prox30)} lançamentos)</div>'
            f'<div class="asd">Planejar disponibilidade de caixa.</div></div>'
        )

    if not alerts:
        alerts.append(
            '<div class="alt alg"><div class="ats">Sem alertas críticos</div>'
            '<div class="asd">Nenhum lançamento vencido e tudo categorizado.</div></div>'
        )
    return "\n    ".join(alerts)


def compute_overview_positivos_html(cx_data: list, pagar_data: list, rows: list,
                                    cli_data: list | None = None) -> str:
    positivos = []

    # Carteira diversificada (contraponto da concentração em Riscos)
    if cli_data:
        tot = sum(x.get("total", 0) for x in cli_data) or 1
        ranked = sorted(cli_data, key=lambda x: -x.get("total", 0))[:3]
        pct = round(100 * sum(x.get("total", 0) for x in ranked) / tot)
        if pct < 35 and tot > 1:
            positivos.append(
                '<div class="alt alg"><div class="ats">Carteira diversificada</div>'
                f'<div class="asd">Top 3 clientes = {pct}% da receita — baixa dependência.</div></div>'
            )

    venc_rec = any(r["sit"] == "Atrasada" for r in cx_data)
    venc_pag = any(r["window"] == "vencido" for r in pagar_data)
    sem_cat  = any(r["cat"] == "(sem categoria)" for r in pagar_data)

    if not venc_rec:
        positivos.append(
            '<div class="alt alg"><div class="ats">Nenhum recebível vencido</div>'
            '<div class="asd">Toda a carteira de recebíveis está em dia.</div></div>'
        )
    if not venc_pag:
        positivos.append(
            '<div class="alt alg"><div class="ats">Nenhum pagamento vencido</div>'
            '<div class="asd">Todos os fornecedores dentro do prazo.</div></div>'
        )
    if not sem_cat:
        positivos.append(
            '<div class="alt alg"><div class="ats">100% dos lançamentos categorizados</div>'
            '<div class="asd">DRE completo e sem lacunas de classificação.</div></div>'
        )

    # Fornecedores com pagamentos estáveis (baixa variância entre meses)
    stable = []
    for r in rows:
        vals = [v for k, v in r.items() if len(k) == 5 and k[:3].isalpha() and v > 0]
        if len(vals) >= 3:
            avg = sum(vals) / len(vals)
            variance_pct = max(abs(v - avg) / avg for v in vals) if avg else 1
            if variance_pct < 0.05 and avg > 5_000:
                stable.append((r["n"], avg))
    if stable:
        stable.sort(key=lambda x: -x[1])
        nomes = ", ".join(n for n, _ in stable[:3])
        positivos.append(
            f'<div class="alt alb"><div class="ats">Fornecedores com valores estáveis</div>'
            f'<div class="asd">{nomes} — variação &lt; 5% entre meses.</div></div>'
        )

    if not positivos:
        positivos.append(
            '<div class="alt alb"><div class="ats">Dashboard atualizado com dados da semana</div>'
            '<div class="asd">Dados carregados via Bling API.</div></div>'
        )
    return "\n    ".join(positivos[:4])


# ──────────────────────────────────────────────
# PRÓXIMAS AÇÕES — gerador automático
# ──────────────────────────────────────────────
# Substituiu o bloco hardcoded de "Esta semana" / "Mai–Jun 2026" que
# ficava desatualizado entre rodadas. Cada ação é derivada dos dados
# vivos do Bling + auditoria + classificação de clientes.

def _action_card(nivel: str, titulo: str, sub: str) -> str:
    """Renderiza um item .tli da timeline com cor de severidade.

    nivel ∈ {'red', 'amber', 'blue', 'green'}.
    """
    color_var = {"red": "var(--red)", "amber": "var(--amber)",
                 "blue": "var(--blue)", "green": "var(--green)"}.get(
                     nivel, "var(--blue)")
    from html import escape as _e
    return (f'<div class="tli">'
            f'<div class="tld" style="background:{color_var}"></div>'
            f'<div><div class="tlt">{_e(titulo)}</div>'
            f'<div class="tls">{_e(sub)}</div></div></div>')


def compute_next_actions(
    cx_data: list,
    pagar_data: list,
    cli_data: list,
    audit_findings: list,
    em_aberto: list,
    receber_em_aberto: list,
    today: date,
) -> tuple[list[dict], list[dict]]:
    """Gera duas listas de próximas ações a partir dos dados vivos.

    Retorna (imediatas, acompanhar). Cada ação é dict com nivel/titulo/sub.
    Ordenação: severidade (red > amber > blue) e depois valor desc.

    Regras (todas Bling-driven):
      imediatas:
        - Contas a receber VENCIDAS por cliente (1 por cliente, top 4)
        - Erros de auditoria (top 3)
        - Contas a pagar VENCIDAS por fornecedor (top 3)
        - Clientes de risco "alto" com R$/mês > 5K (churn flagged)
        - Fornecedores sem categoria com valor > 10K em aberto
      acompanhar:
        - Vencimentos a pagar nos próximos 30d, top 4 por valor
        - Provisões tributárias trimestrais (info findings IRPJ/CSLL)
        - Warns de auditoria (até 2)
        - Clientes "médio" sem pagamento nos últimos 60d (até 3)
    """
    imediatas: list[dict] = []
    acompanhar: list[dict] = []

    # ── A RECEBER vencidos (red) — agrupa por cliente ──
    from collections import defaultdict
    venc_rec_by_cli: dict[str, dict] = defaultdict(
        lambda: {"val": 0.0, "n": 0, "maior_atraso": 0})
    for r in cx_data:
        if r.get("sit") != "Atrasada":
            continue
        cli = r["c"].split("—")[0].strip()
        venc_rec_by_cli[cli]["val"] += r.get("val", 0)
        venc_rec_by_cli[cli]["n"] += 1
        venc_rec_by_cli[cli]["maior_atraso"] = max(
            venc_rec_by_cli[cli]["maior_atraso"], r.get("dias", 0))
    venc_rec_top = sorted(
        venc_rec_by_cli.items(), key=lambda x: -x[1]["val"])[:4]
    for cli, info in venc_rec_top:
        dias_txt = (f"{info['maior_atraso']} dias"
                    if info["maior_atraso"] > 0 else "vencido")
        n_txt = f" ({info['n']} parcelas)" if info["n"] > 1 else ""
        imediatas.append({
            "nivel": "red",
            "titulo": f"Cobrar {cli[:38]} — R$ {_brl_num(info['val'])} vencidos{n_txt}",
            "sub":    f"Atraso máximo: {dias_txt}. Enviar 2ª via + contato direto.",
            "sort":   info["val"],
        })

    # ── Erros de auditoria (red, top 3) ──
    audit_errs = [f for f in (audit_findings or [])
                  if getattr(f, "status", "") == "error"]
    for f in audit_errs[:3]:
        diff = getattr(f, "diff", None)
        val_txt = (f" (diff R$ {_brl_num(abs(diff))})" if diff else "")
        imediatas.append({
            "nivel": "red",
            "titulo": f"{f.title}{val_txt}",
            "sub":    (getattr(f, "action", None)
                       or getattr(f, "detail", "")[:120]),
            "sort":   abs(diff or 0),
        })

    # ── A PAGAR vencidos (red) — agrupa por fornecedor ──
    venc_pag_by_forn: dict[str, dict] = defaultdict(
        lambda: {"val": 0.0, "n": 0})
    for r in pagar_data:
        if r.get("window") != "vencido":
            continue
        venc_pag_by_forn[r["n"]]["val"] += r.get("val", 0)
        venc_pag_by_forn[r["n"]]["n"] += 1
    for forn, info in sorted(
            venc_pag_by_forn.items(), key=lambda x: -x[1]["val"])[:3]:
        n_txt = f" ({info['n']} lcto)" if info["n"] > 1 else ""
        imediatas.append({
            "nivel": "red",
            "titulo": (f"Pagar {forn[:38]} — R$ {_brl_num(info['val'])} "
                       f"vencidos{n_txt}"),
            "sub":    "Já passou do vencimento. Quitar ou renegociar.",
            "sort":   info["val"],
        })

    # ── Clientes "alto" risco com receita relevante (amber) ──
    high_risk_cli = [c for c in (cli_data or [])
                     if c.get("cat") == "alto" and c.get("valor", 0) >= 5000]
    for c in sorted(high_risk_cli, key=lambda c: -c["valor"])[:2]:
        imediatas.append({
            "nivel": "amber",
            "titulo": (f"Risco churn: {c['n']} (R$ {_brl_num(c['valor'])}/mês)"),
            "sub":    (c.get("obs") or "Cliente classificado como risco alto."),
            "sort":   c["valor"],
        })

    # ── Fornecedores sem categoria em aberto com valor relevante ──
    sem_cat = defaultdict(lambda: {"val": 0.0, "n": 0})
    for c in em_aberto:
        cat = (c.get("categoria_descricao") or "").strip()
        if cat and cat != "(sem categoria)":
            continue
        nome = (c.get("contato_nome") or "").strip()
        valor = parse_money(c.get("saldo") or c.get("valor"))
        if valor <= 0:
            continue
        sem_cat[nome]["val"] += valor
        sem_cat[nome]["n"] += 1
    for nome, info in sorted(
            sem_cat.items(), key=lambda x: -x[1]["val"])[:2]:
        if info["val"] < 10_000:
            continue
        imediatas.append({
            "nivel": "amber",
            "titulo": (f"Classificar {nome[:38]} no Bling — "
                       f"R$ {_brl_num(info['val'])} sem categoria"),
            "sub":    "Sem categoria impacta DRE e o gráfico de gastos.",
            "sort":   info["val"],
        })

    # ── ACOMPANHAR: próximos 30d a pagar ──
    prox30 = [r for r in pagar_data if r.get("window") == "30d"]
    top_30 = sorted(prox30, key=lambda r: -r.get("val", 0))[:4]
    for r in top_30:
        if r.get("val", 0) < 3000:
            continue
        cat = r.get("cat", "?") or ""
        cat_txt = (f"Categoria: {cat}" if cat and cat != "(sem categoria)"
                   else "⚠ sem categoria no Bling — classificar")
        acompanhar.append({
            "nivel": "amber",
            "titulo": (f"{r['n'][:38]} vence em {r.get('venc', '?')} — "
                       f"R$ {_brl_num(r['val'])}"),
            "sub":    cat_txt,
            "sort":   r["val"],
        })

    # ── ACOMPANHAR: provisões tributárias (IRPJ/CSLL info) ──
    provisoes = [f for f in (audit_findings or [])
                 if getattr(f, "status", "") == "info"
                 and getattr(f, "check_id", "").startswith(("irpj_", "csll_"))]
    for f in provisoes[:2]:
        esp = getattr(f, "expected", None) or 0
        acompanhar.append({
            "nivel": "amber",
            "titulo": (f"Provisionar {f.title} — esperado "
                       f"R$ {_brl_num(esp)}"),
            "sub":    (getattr(f, "action", None) or "Apurar com Serrano."),
            "sort":   esp,
        })

    # ── ACOMPANHAR: warns de auditoria (até 2) ──
    audit_warns = [f for f in (audit_findings or [])
                   if getattr(f, "status", "") == "warn"]
    audit_warns.sort(key=lambda f: -abs(getattr(f, "diff", 0) or 0))
    for f in audit_warns[:2]:
        diff = getattr(f, "diff", None)
        val_txt = (f" (diff R$ {_brl_num(abs(diff))})" if diff else "")
        acompanhar.append({
            "nivel": "amber",
            "titulo": f"{f.title}{val_txt}",
            "sub":    (getattr(f, "action", None)
                       or getattr(f, "detail", "")[:120]),
            "sort":   abs(diff or 0),
        })

    # ── ACOMPANHAR: clientes "medio" inativos (sem pagar >60d) ──
    cutoff_ym = (today.replace(day=1) - timedelta(days=60)).strftime("%Y-%m")
    medio_inativo = [c for c in (cli_data or [])
                     if c.get("cat") == "medio"
                     and (c.get("last_mes") or "") <= cutoff_ym]
    for c in sorted(medio_inativo, key=lambda c: -c.get("valor", 0))[:3]:
        acompanhar.append({
            "nivel": "blue",
            "titulo": (f"Reativar {c['n']} — sem pagamento desde "
                       f"{c.get('last_mes', '?')}"),
            "sub":    (f"Ticket histórico R$ {_brl_num(c['valor'])}/mês · "
                       + (c.get("obs") or "potencial de retomada")),
            "sort":   c.get("valor", 0),
        })

    # Ordenação final
    nivel_order = {"red": 0, "amber": 1, "blue": 2, "green": 3}
    imediatas.sort(key=lambda a: (nivel_order[a["nivel"]], -a["sort"]))
    acompanhar.sort(key=lambda a: (nivel_order[a["nivel"]], -a["sort"]))

    return imediatas[:7], acompanhar[:6]


def render_next_actions_html(
    imediatas: list[dict], acompanhar: list[dict]
) -> tuple[str, str]:
    """Devolve dois fragmentos HTML para os marcadores
    @@NEXT_ACTIONS_IMEDIATAS@@ e @@NEXT_ACTIONS_ACOMPANHAR@@.
    """
    def empty(label: str) -> str:
        return (f'<div class="tli"><div class="tld" '
                f'style="background:var(--green)"></div>'
                f'<div><div class="tlt">✓ Nada {label} no momento</div>'
                f'<div class="tls">Próxima rodada do weekly.sh '
                f'recalcula automaticamente.</div></div></div>')

    if imediatas:
        h_imed = "\n    ".join(
            _action_card(a["nivel"], a["titulo"], a["sub"]) for a in imediatas)
    else:
        h_imed = empty("crítico")

    if acompanhar:
        h_acomp = "\n    ".join(
            _action_card(a["nivel"], a["titulo"], a["sub"])
            for a in acompanhar)
    else:
        h_acomp = empty("a acompanhar")

    return h_imed, h_acomp


# ──────────────────────────────────────────────
# CAIXA — KPIs DINÂMICOS
# ──────────────────────────────────────────────

def compute_caixa_kpis_html(pagar_data: list, cx_data: list) -> str:
    total_pagar  = sum(r["val"] for r in pagar_data)
    total_rec    = sum(r["val"] for r in cx_data)
    venc_pag     = sum(r["val"] for r in pagar_data if r["window"] == "vencido")
    venc_rec     = sum(r["val"] for r in cx_data if r["sit"] == "Atrasada")
    pagar_30d    = sum(r["val"] for r in pagar_data if r["window"] in ("vencido", "30d"))
    gap          = total_rec - pagar_30d
    n_pagar      = len(pagar_data)
    n_rec        = len(cx_data)
    vencidos_tot = venc_pag + venc_rec

    gap_cor  = "green" if gap >= 0 else "red"
    pag_cor  = "red"   if venc_pag > 0 else "amber"
    rec_cor  = "red"   if venc_rec > 0 else "green"
    venc_cor = "red"   if vencidos_tot > 0 else "green"

    gap_val  = _signed_kbrl(gap)
    gap_full = ("+" if gap > 0 else "") + _brl(gap)
    venc_lbl = _brl(vencidos_tot) if vencidos_tot > 0 else "R$ 0"
    venc_sub = "nenhum vencido" if vencidos_tot == 0 else f"pagar {_brl(venc_pag)} + receber {_brl(venc_rec)}"

    cards = [
        f'<div class="met {pag_cor}"><div class="ml">A pagar em aberto</div>'
        f'<div class="mv {pag_cor}" title="{_brl(total_pagar)}">{_kbrl(total_pagar)}</div>'
        f'<div class="ms">{n_pagar} lançamentos</div></div>',

        f'<div class="met {rec_cor}"><div class="ml">A receber em aberto</div>'
        f'<div class="mv {rec_cor}" id="cxvRec" title="{_brl(total_rec)}">{_kbrl(total_rec)}</div>'
        f'<div class="ms" id="cxsRec">{n_rec} lançamentos</div></div>',

        f'<div class="met {gap_cor}"><div class="ml">Gap receber − pagar 30d</div>'
        f'<div class="mv {gap_cor}" title="{gap_full}">{gap_val}</div>'
        f'<div class="ms">receber − (vencido + 30d)</div></div>',

        f'<div class="met {venc_cor}"><div class="ml">Total vencidos</div>'
        f'<div class="mv {venc_cor}" title="{venc_lbl}">{venc_lbl}</div>'
        f'<div class="ms">{venc_sub}</div></div>',
    ]
    return "\n  ".join(cards)


# ──────────────────────────────────────────────
# GASTOS — HEADER DINÂMICO
# ──────────────────────────────────────────────

def compute_gastos_header(months: list[date], rows: list) -> tuple[str, str]:
    """Retorna (titulo, subtitulo) para a aba Gastos."""
    if not months:
        return "Gastos Reais", "fonte: relatório de pagamentos Bling"
    inicio = month_label(months[0])
    fim    = month_label(months[-1])
    total  = sum(r["total"] for r in rows)
    n_forn = len(rows)
    title  = f"Gastos Reais — {inicio} a {fim}"
    sub    = f"fonte: Bling ERP · total: {_brl(total)} · {n_forn} fornecedores"
    return title, sub


# ──────────────────────────────────────────────
# RENDER PRINCIPAL
# ──────────────────────────────────────────────

def find_latest_snapshot(in_dir: Path) -> Path:
    candidates = sorted(in_dir.glob("bling_data_*.json"))
    if not candidates:
        raise FileNotFoundError(f"nenhum bling_data_*.json em {in_dir}")
    return candidates[-1]


def render(data: dict, snapshot: Path, template: Path, today: date) -> str:
    """Lê template.html e substitui todos os marcadores @@...@@ com dados ao vivo."""
    pagas      = data.get("contas_pagar_pagas", [])
    receber    = data.get("contas_receber_em_aberto", [])
    em_aberto  = data.get("contas_pagar_em_aberto", [])
    recebidas  = data.get("contas_receber_recebidas", [])

    # ── Reconciliação: remove lançamentos fantasma do a pagar (duplicatas
    #    já pagas no Bling + provisões cobertas por NF). Fonte única em
    #    audit.py — mesma limpeza que a aba Auditoria e contas_view usam,
    #    pra Visão Geral / Caixa / DRE não inflarem "vencidos". ──
    try:
        from audit import reconcile_em_aberto  # type: ignore
        em_aberto, _recon_ajustes = reconcile_em_aberto(pagas, em_aberto, today)
        if _recon_ajustes:
            _rt = sum(a["valor"] for a in _recon_ajustes)
            print(f"[recon] {len(_recon_ajustes)} lançamento(s) fantasma "
                  f"removidos do a pagar (R$ {_rt:,.0f})")
    except Exception as _e:  # pragma: no cover
        print(f"[recon] reconciliação ignorada: {_e}", file=sys.stderr)

    # ── Deduplicação do A RECEBER: remove recebíveis idênticos (mesmo
    #    contato/valor/vencimento sem documento) que inflavam Entradas/receita.
    #    Mesma fonte (audit.py) usada na aba Auditoria. ──
    try:
        from audit import dedupe_receber  # type: ignore
        recebidas, _dup_rec = dedupe_receber(recebidas)
        receber,  _dup_ren = dedupe_receber(receber)
        _nd = len(_dup_rec) + len(_dup_ren)
        if _nd:
            _vd = sum(a["valor"] for a in _dup_rec + _dup_ren)
            print(f"[dedup-receber] {_nd} duplicata(s) removida(s) do a receber "
                  f"(R$ {_vd:,.0f}) — {len(_dup_rec)} recebidas + {len(_dup_ren)} em aberto")
    except Exception as _e:  # pragma: no cover
        print(f"[dedup-receber] ignorado: {_e}", file=sys.stderr)

    # ── Computar dados ──
    months = compute_last_4_months(pagas)
    if not months:
        print("[warn] sem meses computados — usando YTD do calendário")
        months = [date(today.year, mm, 1) for mm in range(1, today.month + 1)]

    month_keys  = [month_key(d) for d in months]
    month_lbls  = {month_key(d): month_label(d) for d in months}
    month_names = {month_key(d): month_full_name(d) for d in months}

    rows        = compute_rows(pagas, months)
    cx_data     = compute_cx_data(receber, today)
    pagar_data  = compute_pagar_data(em_aberto, today)
    recebido    = compute_recebido_data(recebidas)
    dre_data    = compute_dre_data(pagas, recebidas, em_aberto, receber, today)
    dre_detail  = compute_dre_detail(pagas, recebidas, em_aberto, receber, today)

    # ── Aba Clientes (Bling-driven, com classificação manual) ──
    cli_classif = _load_clientes_classificacao()
    # Aba Clientes: fonte verdadeira é o snapshot Totvs (abre por cliente
    # final que usa o software), não os contatos Bling (que são só as
    # filiais TOTVS intermediárias). Cai pra Bling apenas se o snapshot
    # Totvs não existir (sandbox sem dados).
    cli_data = []
    cli_filiais = []  # lista de intermediários TOTVS
    import os as _os_local, sys as _sys_local
    try:
        _totvs_snap_p = _os_local.path.expanduser("~/Documents/GRAP/"
                "Negociação Easy/Controles Easy/relatorios atuais/"
                "bling-api/totvs_snapshot.json")
        _snap = Path(_os_local.environ.get("TOTVS_SNAP", _totvs_snap_p))
        if _snap.exists():
            cli_data, _filiais_meta = compute_cli_data_totvs(
                _snap, cli_classif, today, window_months=12)
            cli_filiais = compute_filiais_totvs(cli_data, _filiais_meta)
            print(f"[cli] {len(cli_data)} clientes finais via Totvs · "
                  f"{len(cli_filiais)} filiais intermediárias",
                  file=_sys_local.stderr)
    except Exception as _e:
        print(f"[cli] Totvs snapshot falhou: {_e}",
              file=_sys_local.stderr)
    if not cli_data:
        # Fallback: contatos Bling (perde abertura dos clientes finais)
        cli_data = compute_cli_data(recebidas, receber, cli_classif, today,
                                    window_months=12)
        print(f"[cli] fallback Bling: {len(cli_data)} clientes",
              file=_sys_local.stderr)
    cli_kpi     = cli_kpis(cli_data)
    js_cli      = cli_data_to_js(cli_data)

    # ── Serializar para JS ──
    js_rows       = rows_to_js(rows, month_keys)
    js_cx         = cx_data_to_js(cx_data)
    js_pagar      = pagar_to_js(pagar_data)
    js_recebido   = recebido_to_js(recebido)
    js_dre        = dre_to_js(dre_data)
    js_dre_detail = dre_detail_to_js(dre_detail)
    js_months     = "[" + ",".join(f"'{k}'" for k in month_keys) + "]"
    js_month_lbls = "{" + ",".join(f"'{k}':'{v}'" for k, v in month_lbls.items()) + "}"
    js_month_nms  = "{" + ",".join(f"'{k}':'{v}'" for k, v in month_names.items()) + "}"
    js_groups     = groups_to_js(STACK_GROUPS)

    # ── Datas de exibição ──
    months_br = [_MONTH_PT[d.month - 1].capitalize() + "/" + str(d.year)[-2:] for d in months]
    periodo   = f"{months_br[0]}–{months_br[-1]}" if len(months_br) >= 2 else (months_br[0] if months_br else "")
    total_pago = sum(r["total"] for r in rows)
    total_fmt  = f"R$ {total_pago:,.0f}".replace(",", ".")
    today_fmt  = f"{today.day:02d} {_MONTH_PT[today.month-1]} {today.year}"
    today_hint = f"{today.day:02d}/{_MONTH_PT[today.month-1]}/{str(today.year)[-2:]}"
    fetched_at = ""
    updated_at = today_fmt
    try:
        meta = json.loads(snapshot.read_text())
        fetched_at = meta.get("_metadata", {}).get("fetched_at", "")
        if fetched_at:
            dt = datetime.strptime(fetched_at[:16], "%Y-%m-%d %H:%M")
            updated_at = (
                f"{dt.day:02d}/{_MONTH_PT[dt.month-1]}/{str(dt.year)[-2:]}"
                f" · {dt.hour:02d}:{dt.minute:02d} BRT"
            )
    except Exception:
        pass
    snapshot_label = snapshot.stem.replace("bling_data_", "")

    # ── Marcadores dinâmicos do dashboard ──
    overview_hero_sub   = compute_overview_hero_sub(months, total_pago)
    overview_pills      = compute_overview_pills(pagar_data, cx_data)
    saldo_caixa         = load_caixa_config().get("saldo_caixa")
    overview_kpis       = compute_overview_kpis_html(rows, pagar_data, cx_data, months, saldo_caixa)
    overview_riscos     = compute_overview_riscos_html(cx_data, pagar_data, cli_data)
    overview_positivos  = compute_overview_positivos_html(cx_data, pagar_data, rows, cli_data)
    caixa_kpis          = compute_caixa_kpis_html(pagar_data, cx_data)
    gastos_title, gastos_sub = compute_gastos_header(months, rows)

    # Hash SHA-256 da senha (hardcoded — mudar aqui se alterar a senha)
    PWD_HASH = "1d440828f091badb6389c03a81f1d23e77045a455257dd2db15d815b70bca22d"

    # Data de login (dd/mmm/aa)
    login_date = f"{today.day:02d}/{_MONTH_PT[today.month-1]}/{str(today.year)[-2:]}"

    # ── Ler template e substituir marcadores ──
    html = template.read_text(encoding="utf-8")

    # Marcadores JS (dados para os gráficos)
    html = html.replace("@@ROWS@@",            js_rows)
    html = html.replace("@@CF_MONTHS@@",       js_months)
    html = html.replace("@@CF_MONTH_LABELS@@", js_month_lbls)
    html = html.replace("@@CF_MONTH_NAMES@@",  js_month_nms)
    html = html.replace("@@CF_GROUPS@@",       js_groups)
    html = html.replace("@@CX_DATA@@",         js_cx)
    html = html.replace("@@PAGAR_DATA@@",      js_pagar)
    html = html.replace("@@RECEBIDO_DATA@@",   js_recebido)

    # ── Tabelas mensais de Contas a Pagar (Caixa) ───────────────────
    html = html.replace(
        "@@PAGAR_MENSAL_HTML@@",
        render_pagar_mensal_html(em_aberto, today, months_ahead=3))
    html = html.replace(
        "@@CAIXA_VENCIDOS_HTML@@",
        render_vencidos_html(em_aberto, receber, today))
    html = html.replace(
        "@@CAIXA_FLUXO_HTML@@",
        render_cashflow_html(pagas, recebidas, em_aberto, receber, today))

    # ── Aba Clientes ────────────────────────────────────────────────
    html = html.replace("@@CLI_DATA@@",        js_cli)
    html = html.replace("@@CLI_FILIAIS_HTML@@",
                        render_filiais_html(cli_filiais))
    html = html.replace("@@CLI_VAL_GAR@@",     _brl_num(cli_kpi["garantido"]))
    html = html.replace("@@CLI_VAL_MED@@",     _brl_num(cli_kpi["medio"]))
    html = html.replace("@@CLI_VAL_ALT@@",     _brl_num(cli_kpi["alto"]))
    html = html.replace("@@CLI_VAL_SEM@@",     _brl_num(cli_kpi.get("sem_classificacao", 0)))
    html = html.replace("@@CLI_VAL_TOT@@",     _brl_num(cli_kpi["tot_val"]))
    html = html.replace("@@CLI_N_GAR@@",       str(cli_kpi["n_garantido"]))
    html = html.replace("@@CLI_N_MED@@",       str(cli_kpi["n_medio"]))
    html = html.replace("@@CLI_N_ALT@@",       str(cli_kpi["n_alto"]))
    html = html.replace("@@CLI_N_SEM@@",       str(cli_kpi["n_sem"]))
    html = html.replace("@@CLI_N_TOT@@",       str(cli_kpi["n_tot"]))
    # Percentuais (proteção contra divisão por zero)
    tot = cli_kpi["tot_val"] or 1
    html = html.replace("@@CLI_PCT_GAR@@", f"{round(100*cli_kpi['garantido']/tot)}")
    html = html.replace("@@CLI_PCT_MED@@", f"{round(100*cli_kpi['medio']/tot)}")
    html = html.replace("@@CLI_PCT_ALT@@", f"{round(100*cli_kpi['alto']/tot)}")
    html = html.replace(
        "@@CLI_VAL_MED_PLUS_ALT@@",
        _brl_num(cli_kpi["medio"] + cli_kpi["alto"]))
    html = html.replace(
        "@@CLI_DONUT_DATA@@",
        f"{cli_kpi['garantido']},{cli_kpi['medio']},{cli_kpi['alto']}")
    # Subtítulo: janela
    cli_sub = (f"{cli_kpi['n_tot']} clientes ativos · "
               f"R$ {_brl_num(cli_kpi['tot_val'])}/mês mapeado · "
               f"janela: últimos 12 meses fechados (rolling)")
    html = html.replace("@@CLI_HSUB@@", cli_sub)

    html = html.replace("@@DRE_DATA@@",        js_dre)
    html = html.replace("@@DRE_DETAIL@@",      js_dre_detail)

    # Marcadores HTML dinâmicos
    html = html.replace("@@UPDATED_AT@@",        updated_at)
    html = html.replace("@@LOGIN_DATE@@",         login_date)
    html = html.replace("@@PWD_HASH@@",           PWD_HASH)
    html = html.replace("@@OVERVIEW_HERO_SUB@@",  overview_hero_sub)
    html = html.replace("@@OVERVIEW_PILLS@@",     overview_pills)
    html = html.replace("@@OVERVIEW_KPIS@@",      overview_kpis)
    html = html.replace("@@OVERVIEW_RISCOS@@",    overview_riscos)
    html = html.replace("@@OVERVIEW_POSITIVOS@@", overview_positivos)
    html = html.replace("@@CAIXA_KPIS@@",         caixa_kpis)
    html = html.replace("@@GASTOS_HERO_TITLE@@",  gastos_title)
    html = html.replace("@@GASTOS_HERO_SUB@@",    gastos_sub)

    # ── Aba Comissões Totvs (idempotente: degrada graciosamente se sem snapshot)
    import os as _os, sys as _sys
    _sys.path.insert(0, str(HERE))
    def _resolve_totvs_snap() -> Path:
        env = _os.environ.get("TOTVS_SNAP")
        if env:
            return Path(env)
        # Mac nativo
        mac = HERE.parent / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api" / "totvs_snapshot.json"
        if mac.exists():
            return mac
        # Cowork sandbox mount (sem prefixo GRAP)
        cw = HERE.parent / "relatorios atuais" / "bling-api" / "totvs_snapshot.json"
        if cw.exists():
            return cw
        return mac  # devolve o default mesmo que não exista (preserva erro útil)

    try:
        from totvs_render import render_totvs  # type: ignore
        _totvs_snap = _resolve_totvs_snap()
        _totvs_bling = _totvs_snap.parent
        t = render_totvs(_totvs_snap, _totvs_bling)
        html = html.replace("@@TOTVS_NTAB@@",   t["ntab"])
        html = html.replace("@@TOTVS_MOBTAB@@", t["mobtab"])
        html = html.replace("@@TOTVS_PG@@",     t["pg"])
        print(f"[totvs] {t['n_docs']} docs · R$ {t['total_real']:,.2f} real", file=_sys.stderr)
    except Exception as _e:
        print(f"[totvs] erro (degrada para vazio): {_e}", file=_sys.stderr)
        html = html.replace("@@TOTVS_NTAB@@",   "")
        html = html.replace("@@TOTVS_MOBTAB@@", "")
        html = html.replace("@@TOTVS_PG@@",     "")

    # ── Aba Cartão de Crédito (snapshot cartao_snapshot.json) ───────
    def _resolve_cartao_snap() -> Path:
        env = _os.environ.get("CARTAO_SNAP")
        if env:
            return Path(env)
        return HERE / "cartao_snapshot.json"

    try:
        from cartao_render import render_cartao  # type: ignore
        _cart_snap = _resolve_cartao_snap()
        cr = render_cartao(_cart_snap)
        html = html.replace("@@CARTAO_NTAB@@",   cr["ntab"])
        html = html.replace("@@CARTAO_MOBTAB@@", cr["mobtab"])
        html = html.replace("@@CARTAO_PG@@",     cr["pg"])
        print(f"[cartao] {cr['n_transacoes']} transações · R$ {cr['total_brl']:,.2f}", file=_sys.stderr)
    except Exception as _e:
        print(f"[cartao] erro (degrada para vazio): {_e}", file=_sys.stderr)
        html = html.replace("@@CARTAO_NTAB@@",   "")
        html = html.replace("@@CARTAO_MOBTAB@@", "")
        html = html.replace("@@CARTAO_PG@@",     "")

    # ── Abas P&L Executivo + DRE Contábil (mesma fonte: dre_render.py)
    try:
        from dre_render import render_pl_and_dre  # type: ignore
        _totvs_snap = _resolve_totvs_snap()
        _bling_dir = _totvs_snap.parent
        d = render_pl_and_dre(_bling_dir, _totvs_snap, today=today)
        html = html.replace("@@PL_NTAB@@",   d["pl_ntab"])
        html = html.replace("@@PL_MOBTAB@@", d["pl_mobtab"])
        html = html.replace("@@PL_PG@@",     d["pl_pg"])
        html = html.replace("@@DRE_PG@@",    d["dre_pg"])
        print(f"[pl/dre] {d['n_lancamentos_classificados']} lançamentos classificados", file=_sys.stderr)
    except Exception as _e:
        import traceback
        print(f"[pl/dre] erro (degrada para vazio): {_e}", file=_sys.stderr)
        traceback.print_exc(file=_sys.stderr)
        html = html.replace("@@PL_NTAB@@",   "")
        html = html.replace("@@PL_MOBTAB@@", "")
        html = html.replace("@@PL_PG@@",     "")
        html = html.replace("@@DRE_PG@@",    "")

    # ── Aba A Pagar / A Receber (contas_view) — lançado + previsto pelo histórico
    try:
        from contas_view import render_contas, write_contas_snapshot  # type: ignore
        _totvs_snap = _resolve_totvs_snap()
        _bling_dir = _totvs_snap.parent
        _c = render_contas(_bling_dir, today)
        html = html.replace("@@CONTAS_NTAB@@",   _c["ntab"])
        html = html.replace("@@CONTAS_MOBTAB@@", _c["mobtab"])
        html = html.replace("@@CONTAS_PG@@",     _c["pg"])
        # Snapshot histórico (pra weekly.sh ler no resumo)
        try:
            write_contas_snapshot(_c["summary"], _bling_dir / "contas_snapshot.json")
        except Exception as _e_snap:
            print(f"[contas] warning: snapshot histórico falhou: {_e_snap}",
                  file=_sys.stderr)
        print(f"[contas] fornecedores {_c['n_forn_lancados']}L+{_c['n_forn_previstos']}P · "
              f"clientes {_c['n_cli_lancados']}L+{_c['n_cli_previstos']}P · "
              f"a pagar R$ {_c['pagar_total']:,.0f} · "
              f"a receber R$ {_c['receber_total']:,.0f}".replace(",", "."),
              file=_sys.stderr)
    except Exception as _e:
        import traceback
        print(f"[contas] erro (degrada para vazio): {_e}", file=_sys.stderr)
        traceback.print_exc(file=_sys.stderr)
        html = html.replace("@@CONTAS_NTAB@@",   "")
        html = html.replace("@@CONTAS_MOBTAB@@", "")
        html = html.replace("@@CONTAS_PG@@",     "")

    # ── Aba Auditoria — roda os checks e renderiza
    # (variáveis _findings e _totvs_pm são reutilizadas pelo chat_widget abaixo)
    _findings = None
    _totvs_pm = None
    try:
        from audit import (run_audit, render_audit_pg,  # type: ignore
                           write_audit_snapshot)
        from dre_render import (_load_bling_csvs, _build_matriz,  # type: ignore
                                _load_totvs_por_mes)
        _totvs_snap = _resolve_totvs_snap()
        _bling_dir = _totvs_snap.parent
        _pagas, _em_aberto, _recebidas, _receber = _load_bling_csvs(_bling_dir)
        _matriz = _build_matriz(_pagas, _recebidas, _em_aberto, _receber, today)
        _totvs_pm = _load_totvs_por_mes(_totvs_snap)
        _findings = run_audit(_matriz, _totvs_pm, _bling_dir, today)
        _audit = render_audit_pg(_findings, today)
        html = html.replace("@@AUDIT_NTAB@@",   _audit["ntab"])
        html = html.replace("@@AUDIT_MOBTAB@@", _audit["mobtab"])
        html = html.replace("@@AUDIT_PG@@",     _audit["pg"])
        # Snapshot histórico
        try:
            write_audit_snapshot(_findings, _bling_dir / "audit_findings.json")
        except Exception as _e_snap:
            print(f"[audit] warning: snapshot histórico falhou: {_e_snap}",
                  file=_sys.stderr)
        print(f"[audit] {_audit['n_findings']} findings: "
              f"{_audit['n_error']} erros · "
              f"{_audit['n_warn']} atenções · "
              f"{_audit['n_info']} info · "
              f"{_audit['n_ok']} OK · "
              f"valor em risco ~R$ {_audit['valor_risco']:,.0f}",
              file=_sys.stderr)
    except Exception as _e:
        import traceback
        print(f"[audit] erro (degrada para vazio): {_e}", file=_sys.stderr)
        traceback.print_exc(file=_sys.stderr)
        html = html.replace("@@AUDIT_NTAB@@",   "")
        html = html.replace("@@AUDIT_MOBTAB@@", "")
        html = html.replace("@@AUDIT_PG@@",     "")

    # ── Próximas ações (Bling-driven, derivadas dos dados vivos) ──
    try:
        _imed, _acomp = compute_next_actions(
            cx_data=cx_data,
            pagar_data=pagar_data,
            cli_data=cli_data,
            audit_findings=(_findings or []),
            em_aberto=em_aberto,
            receber_em_aberto=receber,
            today=today,
        )
        _h_imed, _h_acomp = render_next_actions_html(_imed, _acomp)
        html = html.replace("@@NEXT_ACTIONS_IMEDIATAS@@",  _h_imed)
        html = html.replace("@@NEXT_ACTIONS_ACOMPANHAR@@", _h_acomp)
        print(f"[actions] {len(_imed)} imediatas · {len(_acomp)} a acompanhar",
              file=_sys.stderr)
    except Exception as _e:
        import traceback
        print(f"[actions] erro (degrada para vazio): {_e}", file=_sys.stderr)
        traceback.print_exc(file=_sys.stderr)
        html = html.replace("@@NEXT_ACTIONS_IMEDIATAS@@",  "")
        html = html.replace("@@NEXT_ACTIONS_ACOMPANHAR@@", "")

    # ── Widget de busca local + chat IA (injeta antes de </body>)
    try:
        from chat_widget import build_chat_widget, build_chat_context  # type: ignore
        # Tenta carregar resumo Totvs do snapshot (se existir)
        _totvs_sum = {}
        _docs = []
        try:
            _totvs_snap_p = _resolve_totvs_snap()
            if _totvs_snap_p.exists():
                _ts = json.loads(_totvs_snap_p.read_text())
                _docs = _ts.get("documentos", [])
                def _vt(d):
                    return float(d.get("total_geral") or d.get("valor") or 0)
                _real = sum(_vt(d) for d in _docs if d.get("tipo") == "REAL")
                _prov = sum(_vt(d) for d in _docs if d.get("tipo") in ("BASE", "COMPLEMENTAR", "PROVISAO"))
                from collections import Counter
                _by_tipo = Counter(d.get("tipo") for d in _docs)
                _totvs_sum = {
                    "n_docs": len(_docs),
                    "acumulado_real": round(_real, 2),
                    "acumulado_provisao": round(_prov, 2),
                    "por_tipo": {k: int(v) for k, v in _by_tipo.items()},
                }
        except Exception:
            pass
        _ctx = build_chat_context(
            rows=rows,
            months=months,
            pagar_data=pagar_data,
            cx_data=cx_data,
            dre_data=dre_data,
            recebido_data=recebido,
            dre_detail=dre_detail,
            audit_findings=_findings,
            totvs_summary=_totvs_sum,
            totvs_por_mes=_totvs_pm,
            totvs_docs=_docs,
            pagas_raw=pagas,
            recebidas_raw=recebidas,
            em_aberto_raw=em_aberto,
            receber_aberto_raw=receber,
            updated_at=updated_at,
        )
        _widget = build_chat_widget(_ctx)
        html = html.replace("</body>", _widget + "\n</body>")
        print(f"[chat] widget injetado · contexto ~{len(json.dumps(_ctx))//1024} KB",
              file=_sys.stderr)
    except Exception as _e:
        import traceback
        print(f"[chat] erro (segue sem widget): {_e}", file=_sys.stderr)
        traceback.print_exc(file=_sys.stderr)

    return html


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Gera index.html com dados ao vivo do Bling")
    ap.add_argument("--in", dest="in_dir", type=Path, default=None,
                    help="pasta com bling_data_*.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"arquivo de saída (default: {DEFAULT_OUT})")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                    help=f"template HTML (default: {DEFAULT_TEMPLATE})")
    args = ap.parse_args()

    # Encontrar pasta de dados
    in_dir = args.in_dir
    if in_dir is None:
        for c in DEFAULT_IN_CANDIDATES:
            if c.exists() and any(c.glob("bling_data_*.json")):
                in_dir = c
                break
    if in_dir is None:
        print("ERRO: nenhuma pasta com snapshot encontrada. Execute fetch-bling.py primeiro.", file=sys.stderr)
        return 1

    # Verificar template
    if not args.template.exists():
        print(f"ERRO: template não encontrado: {args.template}", file=sys.stderr)
        print("Certifique-se de que template.html está em", HERE, file=sys.stderr)
        return 1

    snapshot = find_latest_snapshot(in_dir)
    print(f"[ok] lendo snapshot: {snapshot.name}")
    data = json.loads(snapshot.read_text(encoding="utf-8"))

    pagas      = data.get("contas_pagar_pagas", [])
    receber    = data.get("contas_receber_em_aberto", [])
    em_aberto  = data.get("contas_pagar_em_aberto", [])
    recebidas  = data.get("contas_receber_recebidas", [])
    print(f"[ok] contas_pagar_pagas: {len(pagas)} registros")
    print(f"[ok] contas_receber_em_aberto: {len(receber)} registros")
    print(f"[ok] contas_pagar_em_aberto: {len(em_aberto)} registros")
    print(f"[ok] contas_receber_recebidas: {len(recebidas)} registros")
    # reconcilia o a pagar (fantasmas) antes do preview — espelha render()
    try:
        from audit import reconcile_em_aberto as _recon  # type: ignore
        em_aberto, _aj = _recon(pagas, em_aberto, date.today())
        if _aj:
            print(f"[recon] {len(_aj)} fantasma(s) removidos do a pagar "
                  f"(R$ {sum(a['valor'] for a in _aj):,.0f})")
    except Exception:
        pass
    # log DRE months preview
    dre_preview = compute_dre_data(pagas, recebidas, em_aberto, receber, date.today())
    print(f"[ok] dre_data: {len(dre_preview)} meses ({dre_preview[0]['label'] if dre_preview else '—'} → {dre_preview[-1]['label'] if dre_preview else '—'})")

    today = date.today()
    html_out = render(data, snapshot, args.template, today)

    # Verificar se todos os marcadores foram substituídos
    remaining = [m for m in ["@@ROWS@@", "@@CF_MONTHS@@", "@@CX_DATA@@",
                              "@@CF_GROUPS@@", "@@CF_MONTH_LABELS@@", "@@CF_MONTH_NAMES@@",
                              "@@PAGAR_DATA@@", "@@RECEBIDO_DATA@@",
                              "@@DRE_DATA@@", "@@DRE_DETAIL@@",
                              "@@TOTVS_NTAB@@", "@@TOTVS_MOBTAB@@", "@@TOTVS_PG@@",
                              "@@PL_NTAB@@", "@@PL_MOBTAB@@", "@@PL_PG@@", "@@DRE_PG@@",
                              "@@CONTAS_NTAB@@", "@@CONTAS_MOBTAB@@", "@@CONTAS_PG@@",
                              "@@AUDIT_NTAB@@", "@@AUDIT_MOBTAB@@", "@@AUDIT_PG@@"]
                 if m in html_out]
    if remaining:
        print(f"[warn] marcadores não substituídos: {remaining}", file=sys.stderr)

    args.out.write_text(html_out, encoding="utf-8")
    size_kb = args.out.stat().st_size // 1024
    print(f"[ok] -> {args.out} ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
