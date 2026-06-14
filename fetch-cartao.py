#!/usr/bin/env python3
"""fetch-cartao.py — parser das faturas de cartão Bradesco (PDF) → snapshot.

Lê os extratos PDF em <in>/MM.AAAA/*.PDF (faturas Bradesco "Extrato"),
extrai os lançamentos de cada cartão, categoriza por fornecedor e gera
`cartao_snapshot.json` consumido pelo build-html.py (aba "Cartão").

Idempotente: reprocessa tudo do zero a cada rodada (PDFs são a fonte).
Requer `pdftotext` (poppler-utils) no PATH.

Uso:
    python3 fetch-cartao.py --in "<pasta Cartão de Crédito>/2026" \
        --out cartao_snapshot.json
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
MES_ABBR = ["jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez"]

# Linhas que NÃO são gasto real (saldo carregado / pagamento da fatura)
NAO_GASTO = ("SALDO ANTERIOR", "PAGTO. POR DEB", "PAGAMENTO", "PAGTO ")

# Mapa fornecedor → (rótulo canônico, cor para o donut)
VENDOR_RULES = [
    ("AMAZON WEB SERVICES", "AWS"),
    ("AWS BRAZIL", "AWS"),
    ("AWS ", "AWS"),
    ("OPENAI", "OpenAI"),
    ("CHATGPT", "OpenAI"),
    ("GITHUB", "GitHub"),
    ("MIRO", "Miro"),
    ("ANUIDADE", "Anuidade"),
    ("IOF", "IOF / impostos"),
    ("CUSTO TRANS. EXTERIOR", "IOF / impostos"),
]
VENDOR_COLORS = {
    "AWS": "#e8821f",
    "OpenAI": "#1a7a80",
    "GitHub": "#3d3d4e",
    "Miro": "#f2c037",
    "Anuidade": "#8a6d3b",
    "IOF / impostos": "#c0392b",
    "Outros": "#3d5a80",
}


def parse_money(s: str) -> float:
    """'10.061,12' / '- 5.738,66' → float (negativo preservado)."""
    s = (s or "").strip()
    neg = s.startswith("-") or s.startswith("- ")
    s = s.replace("-", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def vendor_of(hist: str) -> str:
    up = (hist or "").upper()
    for needle, label in VENDOR_RULES:
        if needle in up:
            return label
    return "Outros"


def pdftotext(path: Path) -> str:
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout or ""
    except Exception as e:
        print(f"  ! pdftotext falhou em {path.name}: {e}", file=sys.stderr)
        return ""


LINE_RE = re.compile(
    r"^\s*(\d{2}/\d{2})\s+(.+?)\s+(-?\s*[\d.]+,\d{2})\s+(-?\s*[\d.]+,\d{2})\s*$"
)


def parse_pdf(path: Path) -> dict | None:
    txt = pdftotext(path)
    if "Detalhe do Extrato" not in txt:
        return None
    nome = ""
    card_last = ""
    venc = ""
    mes_txt = ""
    m = re.search(r"Nome:\s*(.+)", txt)
    if m:
        nome = m.group(1).strip()
    m = re.search(r"Número do cart[ãa]o:\s*X+\.X+\.X+\.(\d{3,4})", txt)
    if m:
        card_last = m.group(1).strip()
    m = re.search(r"Data de vencimento:\s*(\d{2}/\d{2}/\d{4})", txt)
    if m:
        venc = m.group(1).strip()
    m = re.search(r"M[êe]s:\s*([A-Za-zçÇ]+)/(\d{4})", txt)
    if m:
        mes_txt = f"{m.group(1)}/{m.group(2)}"

    # mês de competência da fatura: usa o vencimento
    comp = ""
    if venc:
        d = datetime.strptime(venc, "%d/%m/%Y")
        comp = f"{d.year:04d}-{d.month:02d}"
    elif mes_txt:
        nm, yr = mes_txt.split("/")
        mo = MESES_PT.get(nm.lower(), 0)
        comp = f"{int(yr):04d}-{mo:02d}"

    # corta a seção de lançamentos (entre o cabeçalho "Data ... R$" e "Taxas Mensais")
    sec = txt
    if "Taxas Mensais" in sec:
        sec = sec.split("Taxas Mensais")[0]

    itens = []
    for line in sec.splitlines():
        mm = LINE_RE.match(line)
        if not mm:
            continue
        data, hist, usd, brl = mm.groups()
        hist = hist.strip()
        if any(tok in hist.upper() for tok in NAO_GASTO):
            continue
        if hist.upper().startswith("TOTAL"):
            continue
        brl_v = parse_money(brl)
        usd_v = parse_money(usd)
        if brl_v == 0 and usd_v == 0:
            continue
        itens.append({
            "comp": comp,
            "card": nome,
            "card_last": card_last,
            "data": data,
            "historico": hist,
            "usd": round(usd_v, 2),
            "brl": round(brl_v, 2),
            "vendor": vendor_of(hist),
        })
    return {
        "comp": comp, "card": nome, "card_last": card_last,
        "venc": venc, "itens": itens, "source": path.name,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True,
                    help="pasta raiz com subpastas MM.AAAA contendo PDFs")
    ap.add_argument("--out", default="cartao_snapshot.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.indir)
    if not root.exists():
        print(f"[skip] pasta de cartão não encontrada: {root}", file=sys.stderr)
        sys.exit(3)

    pdfs = sorted(root.glob("**/*.PDF")) + sorted(root.glob("**/*.pdf"))
    pdfs = sorted(set(pdfs))
    if not pdfs:
        print(f"[skip] nenhum PDF em {root}", file=sys.stderr)
        sys.exit(3)

    # Parseia todos e DEDUPLICA por (competência, cartão): uma fatura de um
    # cartão num mês é única. Protege contra o mesmo extrato presente sob
    # nomes diferentes (ex.: original Bradesco_* + cópia renomeada, ou
    # re-upload). Mantém a versão com mais lançamentos (tie: primeira).
    parsed = []
    for p in pdfs:
        res = parse_pdf(p)
        if not res:
            print(f"  ! ignorado (sem 'Detalhe do Extrato'): {p.name}", file=sys.stderr)
            continue
        parsed.append(res)

    best: dict[tuple, dict] = {}
    for res in parsed:
        key = (res["comp"], res["card_last"])
        cur = best.get(key)
        if cur is None or len(res["itens"]) > len(cur["itens"]):
            best[key] = res
    n_dup = len(parsed) - len(best)
    if n_dup > 0:
        print(f"[dedup] {n_dup} extrato(s) duplicado(s) ignorado(s) "
              f"(mesmo mês+cartão)", file=sys.stderr)

    transacoes = []
    faturas = []
    for res in sorted(best.values(), key=lambda r: (r["comp"], r["card_last"])):
        faturas.append({
            "comp": res["comp"], "card": res["card"],
            "card_last": res["card_last"], "venc": res["venc"],
            "total_brl": round(sum(i["brl"] for i in res["itens"]), 2),
            "total_usd": round(sum(i["usd"] for i in res["itens"]), 2),
            "n_itens": len(res["itens"]), "source": res["source"],
        })
        transacoes.extend(res["itens"])
        if not args.quiet:
            print(f"[add] {res['comp']} · {res['card']} ({res['card_last']}) "
                  f"· {len(res['itens'])} itens · R$ "
                  f"{sum(i['brl'] for i in res['itens']):,.2f}")

    # ── agregações ──
    comps = sorted({t["comp"] for t in transacoes if t["comp"]})
    cards = sorted({t["card"] for t in transacoes if t["card"]})
    vendors = sorted({t["vendor"] for t in transacoes})

    by_month = {c: round(sum(t["brl"] for t in transacoes if t["comp"] == c), 2)
                for c in comps}
    by_month_card = {}
    for c in comps:
        by_month_card[c] = {
            cd: round(sum(t["brl"] for t in transacoes
                          if t["comp"] == c and t["card"] == cd), 2)
            for cd in cards
        }
    by_vendor = {v: round(sum(t["brl"] for t in transacoes if t["vendor"] == v), 2)
                 for v in vendors}
    by_vendor_month = {}
    for v in vendors:
        by_vendor_month[v] = {
            c: round(sum(t["brl"] for t in transacoes
                         if t["vendor"] == v and t["comp"] == c), 2)
            for c in comps
        }

    total = round(sum(t["brl"] for t in transacoes), 2)
    total_usd = round(sum(t["usd"] for t in transacoes), 2)

    snap = {
        "_meta": {
            "generated_at": datetime.now(timezone(timedelta(hours=-3)))
            .strftime("%Y-%m-%d %H:%M:%S"),
            "input_dir": str(root),
            "n_pdfs": len(pdfs),
            "n_faturas": len(faturas),
            "n_transacoes": len(transacoes),
            "tool": "fetch-cartao.py",
        },
        "comps": comps,
        "cards": cards,
        "vendors": vendors,
        "vendor_colors": {v: VENDOR_COLORS.get(v, "#3d5a80") for v in vendors},
        "total_brl": total,
        "total_usd": total_usd,
        "by_month": by_month,
        "by_month_card": by_month_card,
        "by_vendor": by_vendor,
        "by_vendor_month": by_vendor_month,
        "faturas": faturas,
        "transacoes": transacoes,
    }
    Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    print(f"[done] {len(transacoes)} transações · {len(comps)} meses · "
          f"R$ {total:,.2f} · snapshot: {args.out}")


if __name__ == "__main__":
    main()
