#!/usr/bin/env python3
"""
ingest_totvs_interno.py — Adaptador PONTUAL para alimentar a aba Totvs a partir
da PLANILHA INTERNA de faturamento da Easy (FATURAMENTO_MM.AAAA.xlsx), quando o
Relatório de Comissão oficial da TOTVS (ASE806) ainda não foi emitido para o mês.

Lê as linhas de uma categoria (default "Easy Mobile TOTVS") da aba
"Faturamento_MM.AAAA" e injeta UM documento de comissão (tipo BASE) para aquela
competência no totvs_snapshot.json. O documento é marcado com `source` interno
para ser auditável e SUBSTITUÍVEL quando o ASE806 oficial chegar.

Uso:
    python3 ingest_totvs_interno.py \
        --xlsx "/caminho/FATURAMENTO_05.2026.xlsx" \
        --competencia 2026-05 \
        --snapshot data/bling-api/totvs_snapshot.json

Idempotente: remove qualquer doc anterior da mesma competência marcado como
interno antes de inserir. NÃO mexe em docs de outras competências nem nos
oficiais. Stdlib only (lê .xlsx como zip).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
SOURCE_TAG = "planilha-interna-FATURAMENTO"


def _excel_date(v) -> str | None:
    try:
        return (date(1899, 12, 30) + timedelta(days=int(float(v)))).isoformat()
    except (ValueError, TypeError):
        return None


def _read_sheet(xlsx: Path, sheet_name: str) -> list[dict]:
    z = zipfile.ZipFile(xlsx)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    name2rid = {s.get("name"): s.get(f"{RNS}id") for s in wb.iter(f"{NS}sheet")}
    if sheet_name not in name2rid:
        raise ValueError(f"aba '{sheet_name}' não encontrada em {xlsx.name}")
    rid2tgt = {r.get("Id"): r.get("Target")
               for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    ss: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{NS}si"):
            ss.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    tgt = rid2tgt[name2rid[sheet_name]]
    tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt

    def cv(c):
        v = c.find(f"{NS}v")
        if v is None:
            return None
        if c.get("t") == "s":
            try:
                return ss[int(v.text)]
            except (ValueError, IndexError):
                return v.text
        try:
            return float(v.text)
        except (ValueError, TypeError):
            return v.text

    rows: list[dict] = []
    for r in ET.fromstring(z.read(tgt)).iter(f"{NS}row"):
        cells = {}
        for c in r.iter(f"{NS}c"):
            col = "".join(ch for ch in c.get("r", "") if ch.isalpha())
            cells[col] = cv(c)
        rows.append(cells)
    return rows


def build_doc(xlsx: Path, competencia: str, tipo_cob: str) -> dict:
    y, m = competencia.split("-")[:2]
    sheet = f"Faturamento_{m}.{y}"
    rows = _read_sheet(xlsx, sheet)
    if not rows:
        raise ValueError(f"aba '{sheet}' vazia")
    # mapeia cabeçalho (linha 1) por nome -> coluna
    hdr = {str(v).strip(): k for k, v in rows[0].items() if v}
    def col(name):  # primeira coluna cujo header contém `name`
        for h, k in hdr.items():
            if name.lower() in h.lower():
                return k
        return None
    cA = col("Tipo Cob") or "A"
    cCli = col("Cliente") or "B"
    cEmi = col("Emiss") or "D"
    cVen = col("Vencimento") or "E"
    cVal = col("Valor") or "F"
    cNF = col("NFS") or col("NF") or "H"
    cCNPJ = col("CNPJ") or "I"

    clientes, filiais = [], []
    total = 0.0
    for r in rows[1:]:
        if (str(r.get(cA) or "").strip()) != tipo_cob:
            continue
        val = r.get(cVal)
        if not isinstance(val, (int, float)):
            continue
        nome = str(r.get(cCli) or "").strip() or "(sem nome)"
        cnpj = str(r.get(cCNPJ) or "").replace(".", "").replace("/", "").replace("-", "").strip()
        emi = _excel_date(r.get(cEmi))
        ven = _excel_date(r.get(cVen))
        nf = r.get(cNF)
        nf = str(int(nf)) if isinstance(nf, float) else (str(nf) if nf else "")
        total += val
        clientes.append({
            "cnpj_filial": cnpj, "cod_cliente": None, "cliente": nome,
            "produto_cod": None, "produto_desc": tipo_cob,
            "valor_comissao": round(val, 2), "base_comissao": None,
            "valor_bruto_item": None, "perc_total": None,
            "nota_fiscal": nf, "emissao_nf": emi, "dt_vencto": ven,
            "referencia": f"{y}-{m}-01", "situacao": str(r.get("C") or ""),
            "recorrente": None,
        })
        filiais.append({"razao_social": nome, "cnpj": cnpj, "total": round(val, 2),
                        "cod_empresa": None, "cod_fornecedor": None, "num_pedido": nf})

    if not clientes:
        raise ValueError(f"nenhuma linha '{tipo_cob}' na aba {sheet}")

    h = hashlib.sha256(json.dumps(clientes, sort_keys=True).encode()).hexdigest()[:40]
    return {
        "tipo": "BASE",
        "competencia": f"{y}-{m}-01",
        "total_geral": round(total, 2),
        "cnpjs_filial": filiais,
        "clientes": clientes,
        "source_file": f"{xlsx.name} (planilha interna · {tipo_cob})",
        "source_email_subject": f"[INTERNO] {tipo_cob} {m}/{y} — derivado da planilha de faturamento",
        "source_email_from": SOURCE_TAG,
        "source_email_date": None,
        "source_note": ("Competência derivada da planilha interna de faturamento "
                        "(NFS-e Easy → TOTVS). Substituir pelo Relatório de Comissão "
                        "ASE806 oficial quando a TOTVS emitir."),
        "n_linhas": len(clientes),
        "hash": h,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, required=True)
    ap.add_argument("--competencia", required=True, help="YYYY-MM")
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--tipo-cob", default="Easy Mobile TOTVS")
    ap.add_argument("--only-if-absent", action="store_true",
                    help="não injeta se já existir doc OFICIAL (não-interno) para a competência; "
                         "remove o doc interno se o oficial tiver chegado. Use no weekly.sh para "
                         "o bridge se auto-desativar quando o ASE806 oficial for emitido.")
    args = ap.parse_args()

    comp = f"{args.competencia}-01" if len(args.competencia) == 7 else args.competencia

    snap = json.loads(args.snapshot.read_text(encoding="utf-8"))
    docs = snap.get("documentos", [])
    antes = len(docs)
    oficial = [d for d in docs
               if d.get("competencia") == comp and d.get("source_email_from") != SOURCE_TAG]

    if args.only_if_absent and oficial:
        # ASE806 oficial chegou → remove qualquer doc interno e NÃO injeta.
        docs = [d for d in docs if not (
            d.get("competencia") == comp and d.get("source_email_from") == SOURCE_TAG)]
        snap["documentos"] = docs
        args.snapshot.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] {comp}: doc OFICIAL presente — bridge interno desativado/removido "
              f"(docs {antes}→{len(docs)}).")
        return 0

    doc = build_doc(args.xlsx, args.competencia, args.tipo_cob)
    # remove docs internos da MESMA competência (idempotência); preserva oficiais
    docs = [d for d in docs if not (
        (d.get("competencia") == comp) and (d.get("source_email_from") == SOURCE_TAG))]
    if oficial:
        print(f"[aviso] já existe doc OFICIAL para {comp} — o interno será ADICIONADO ao lado.")
    docs.append(doc)
    snap["documentos"] = docs
    args.snapshot.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {comp}: {doc['n_linhas']} linhas · total R$ {doc['total_geral']:,.2f} "
          f"injetado em {args.snapshot} (docs {antes}→{len(docs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
