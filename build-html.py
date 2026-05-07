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
from datetime import date, datetime
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
        "cat": "Serviços PJ", "st": "Parcial", "sc": "bga",
    },
    "EFATA TREINAMENTO": {
        "id": "efata", "n": "Efata Treinamento",
        "cat": "Serviços PJ", "st": "Parcial", "sc": "bga",
    },
    "M. DE Q. MACEDO - EPP": {
        "id": "macedo", "n": "M. de Q. Macedo",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "M. DE Q. MACEDO": {
        "id": "macedo", "n": "M. de Q. Macedo",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA EM TECNOLOGIA DE COMPUTACAO L": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA EM TECNOLOGIA DE COMPUTAÇÃO L": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "MILAJANU CONSULTORIA": {
        "id": "milajanu", "n": "Milajanu Consultoria TI",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "POLAR TECNICA COMERCIAL E INDUSTRIAL LTDA": {
        "id": "polar", "n": "Polar Técnica Comercial",
        "cat": "Outros", "st": "Classificar", "sc": "bgp",
    },
    "POLAR TECNICA": {
        "id": "polar", "n": "Polar Técnica Comercial",
        "cat": "Outros", "st": "Classificar", "sc": "bgp",
    },
    "EDUARDO FARIA DE GODOY 29853590824": {
        "id": "godoy", "n": "Eduardo Faria de Godoy",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
    },
    "EDUARDO FARIA DE GODOY": {
        "id": "godoy", "n": "Eduardo Faria de Godoy",
        "cat": "Serviços PJ", "st": "Não orçado", "sc": "bgr",
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
    "IRPJ": {
        "id": "irpj", "n": "IRPJ",
        "cat": "Outros", "st": "Não provis.", "sc": "bga",
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
        "cat": "Outros", "st": "Não provis.", "sc": "bga",
    },
}

# Grupos para o stack chart (top fornecedores agrupados)
STACK_GROUPS = [
    {"label": "Buy-out",     "ids": ["romulo"]},
    {"label": "Efata",       "ids": ["efata"]},
    {"label": "Mil.+Mac.",   "ids": ["milajanu", "macedo"]},
    {"label": "EP+Godoy",    "ids": ["ep", "godoy", "vtconn", "victor"]},
    {"label": "Geremias",    "ids": ["geremias"]},
    {"label": "Outros",      "ids": ["polar", "ivy", "cbyk", "irpj", "csll", "cartao",
                                     "plentech", "_outros"]},
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
    """Retorna as 4 últimas datas-mês (primeiro dia) com lançamentos."""
    months_seen: set[tuple[int, int]] = set()
    for p in pagas:
        d = parse_date(p.get("vencimento") or p.get("dataVencimento") or "")
        if d:
            months_seen.add((d.year, d.month))
    if not months_seen:
        # fallback: últimos 4 meses a partir de hoje
        today = date.today()
        result = []
        y, m = today.year, today.month
        for _ in range(4):
            result.insert(0, date(y, m, 1))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return result
    sorted_months = sorted(months_seen)[-4:]  # últimos 4
    return [date(y, m, 1) for y, m in sorted_months]


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
# RENDER PRINCIPAL
# ──────────────────────────────────────────────

def find_latest_snapshot(in_dir: Path) -> Path:
    candidates = sorted(in_dir.glob("bling_data_*.json"))
    if not candidates:
        raise FileNotFoundError(f"nenhum bling_data_*.json em {in_dir}")
    return candidates[-1]


def render(data: dict, snapshot: Path, template: Path, today: date) -> str:
    """Lê template.html e substitui todos os marcadores @@...@@ com dados ao vivo."""
    pagas = data.get("contas_pagar_pagas", [])
    receber = data.get("contas_receber_em_aberto", [])

    # ── Computar dados ──
    months = compute_last_4_months(pagas)
    if not months:
        print("[warn] sem meses em contas_pagar_pagas — usando últimos 4 meses do calendário")
        y, m = today.year, today.month
        months = []
        for _ in range(4):
            months.insert(0, date(y, m, 1))
            m -= 1
            if m == 0:
                m, y = 12, y - 1

    month_keys  = [month_key(d) for d in months]
    month_lbls  = {month_key(d): month_label(d) for d in months}
    month_names = {month_key(d): month_full_name(d) for d in months}

    rows    = compute_rows(pagas, months)
    cx_data = compute_cx_data(receber, today)

    # ── Serializar para JS ──
    js_rows       = rows_to_js(rows, month_keys)
    js_cx         = cx_data_to_js(cx_data)
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
    try:
        meta = json.loads(snapshot.read_text())
        fetched_at = meta.get("_metadata", {}).get("fetched_at", "")
    except Exception:
        pass
    snapshot_label = snapshot.stem.replace("bling_data_", "")

    # ── Ler template e substituir marcadores ──
    html = template.read_text(encoding="utf-8")

    html = html.replace("@@ROWS@@",           js_rows)
    html = html.replace("@@CF_MONTHS@@",      js_months)
    html = html.replace("@@CF_MONTH_LABELS@@", js_month_lbls)
    html = html.replace("@@CF_MONTH_NAMES@@",  js_month_nms)
    html = html.replace("@@CF_GROUPS@@",       js_groups)
    html = html.replace("@@CX_DATA@@",         js_cx)

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

    pagas = data.get("contas_pagar_pagas", [])
    receber = data.get("contas_receber_em_aberto", [])
    print(f"[ok] contas_pagar_pagas: {len(pagas)} registros")
    print(f"[ok] contas_receber_em_aberto: {len(receber)} registros")

    today = date.today()
    html_out = render(data, snapshot, args.template, today)

    # Verificar se todos os marcadores foram substituídos
    remaining = [m for m in ["@@ROWS@@", "@@CF_MONTHS@@", "@@CX_DATA@@",
                              "@@CF_GROUPS@@", "@@CF_MONTH_LABELS@@", "@@CF_MONTH_NAMES@@"]
                 if m in html_out]
    if remaining:
        print(f"[warn] marcadores não substituídos: {remaining}", file=sys.stderr)

    args.out.write_text(html_out, encoding="utf-8")
    size_kb = args.out.stat().st_size // 1024
    print(f"[ok] -> {args.out} ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
