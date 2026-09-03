#!/usr/bin/env python3
"""
dre_render.py — Geração das abas P&L Executivo e DRE Contábil para o
dashboard Easy Analytics.

Funções públicas:
    render_pl(snapshot, totvs_snap, bling_dir) -> dict (fragments)
    render_dre(snapshot, totvs_snap, bling_dir) -> dict (fragments)

Filosofia:
    - Mapeamento explícito fornecedor → categoria gerencial (FORNECEDOR_MAP)
    - Categorias hierárquicas (grupo → subgrupo → fornecedor) com drill
    - Cálculo de impostos esperados (Lucro Presumido 32%) com alertas
    - Matriz: linhas = categoria, colunas = mês YYYY-MM + Total + AV%
    - Reconciliação Bling × Totvs por mês (totais)
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any

# CONFIG tributária centralizada em audit.py — fonte única de verdade
try:
    from audit import CONFIG as _AUDIT_CFG  # type: ignore
except Exception:
    _AUDIT_CFG = {
        "pis_aliq": 0.0065, "cofins_aliq": 0.03, "iss_sbc_aliq": 0.029,
        "irpj_aliq": 0.15, "irpj_base_presumida": 0.32,
        "irpj_adicional_aliq": 0.10, "irpj_adicional_limite_trim": 60_000.0,
        "csll_aliq": 0.09, "csll_base_presumida": 0.32,
        "cbs_teste_aliq": 0.009, "ibs_teste_aliq": 0.001,
    }

# ============ MAPEAMENTO FORNECEDOR → CATEGORIA GERENCIAL ============
# Cada fornecedor cai em (GRUPO_DRE, SUBGRUPO).
# GRUPO_DRE: tag pro DRE Contábil (deducoes, custos, desp_pessoal, desp_admin,
#            desp_comercial, financ_receita, financ_despesa, impostos_lucro,
#            nao_recorrente, outras_receitas, outras_despesas).
# SUBGRUPO: tag pro P&L Executivo (gerencial).
# A chave é um match substring (case-insensitive, sem acentos) no nome do contato.

# Estrutura: lista de tuplas (pattern, GRUPO_DRE, SUBGRUPO, label_friendly)
FORNECEDOR_MAP = [
    # Itens não recorrentes / estruturais (informativos, mas separados no P&L)
    ("ROMULO FERREIRA LIMA",      "nao_recorrente", "Buy-out de quotas",      "Rômulo Lima (buy-out)"),
    ("GEREMIAS FERREIRA LIMA",    "nao_recorrente", "Acordo trabalhista",     "Geremias Lima (acordo)"),

    # Pessoal — prestadores PJ. Classificação gerencial do Leandro: contam
    # como "Despesas com pessoal" (não é folha CLT). Subgrupo "Prestadores PJ"
    # é separado de "Encargos sobre pró-labore" (DCTFWEB) de propósito, pra
    # não contaminar a auditoria de INSS — ver _calc_pago_tributo("DCTFWEB").
    ("ALAN BARBOSA",              "desp_pessoal", "Prestadores PJ", "Alan Barbosa (PJ)"),
    ("M. DE Q. MACEDO",           "desp_pessoal", "Prestadores PJ", "Macedo (PJ)"),
    ("EFATA TREINAMENTO",         "desp_pessoal", "Prestadores PJ", "Efata (PJ)"),
    ("MILAJANU CONSULTORIA",      "desp_pessoal", "Prestadores PJ", "Milajanu (PJ)"),
    ("EDUARDO FARIA DE GODOY",    "desp_pessoal", "Prestadores PJ", "Eduardo Godoy (PJ)"),
    ("ISABEL FELIX",              "desp_pessoal", "Prestadores PJ", "Isabel Felix (PJ)"),
    # razão social completa não contém "ISABEL FELIX" (tem CRISTINA no meio) —
    # sem este alias o lançamento caía em desp_admin (P1.3)
    ("ISABEL CRISTINA FELIX",     "desp_pessoal", "Prestadores PJ", "Isabel Felix (PJ)"),
    ("LUIZ HENRIQUE AQUINO",      "desp_admin",   "Administrativas", "Reembolso visitas (L. Aquino)"),

    # Serviços estratégicos / operação core (despesas administrativas)
    ("EP SERVIÇOS DE TECNOLOGIA", "desp_admin", "Serviços estratégicos", "EP Serviços"),
    ("VTCONN CONSULTORIA",        "desp_admin", "Serviços estratégicos", "Vtconn (encerrado)"),
    ("PLENTECH",                  "desp_admin", "Serviços estratégicos", "Plentech"),
    ("CRALUS INFORMATICA",        "desp_admin", "Serviços estratégicos", "Cralus Informática"),
    ("VICTOR HUGO",               "desp_admin", "Serviços estratégicos", "Victor Hugo (advogado)"),
    ("CBYK CONSULTORIA",          "desp_admin", "Serviços estratégicos", "CBYK Consultoria"),
    ("ATRIA SERVICOS",            "desp_admin", "Serviços estratégicos", "Atria Serviços"),
    ("INNOVA LOG",                "desp_admin", "Serviços estratégicos", "Innova Log"),

    # Despesas administrativas
    ("SERRANO CONTABILIDADE",     "desp_admin", "Administrativas",       "Serrano (contabilidade)"),
    ("LWSA",                      "desp_admin", "Administrativas",       "LWSA / Bling"),
    ("CARTAO DE CREDITO",         "desp_admin", "Administrativas",       "Cartão de crédito"),
    ("CARTÃO DE CREDITO",         "desp_admin", "Administrativas",       "Cartão de crédito"),
    ("SAO PAULO MARCAS",          "desp_admin", "Administrativas",       "SP Marcas e Patentes"),
    ("TOTVS S.A.",                "desp_admin", "Administrativas",       "Totvs (parceria)"),
    ("TOTVS SA",                  "desp_admin", "Administrativas",       "Totvs (parceria)"),

    # Impostos (Lucro Presumido)
    ("IRPJ",                      "impostos_lucro", "Impostos sobre lucro", "IRPJ"),
    ("CSLL",                      "impostos_lucro", "Impostos sobre lucro", "CSLL"),
    ("PREFEITURA DO MUNICIPIO",   "deducoes_iss", "Impostos sobre vendas", "ISS"),
    ("MUNICIPIO DE SAO BERNARDO", "desp_admin", "Administrativas", "TFE / Taxas municipais"),
    ("GOVERNO DO ESTADO",         "desp_admin", "Administrativas", "Taxas estaduais (Jucesp)"),
    ("RECEITA FEDERAL",           "desp_pessoal", "Encargos sobre pró-labore", "DCTFWEB (INSS)"),

    # Aportes / movimentações de sócio (NÃO entram no DRE como despesa)
    ("POLAR TECNICA",             "aporte_socio", "Aporte/Distribuição", "Polar (aporte sócio)"),
    ("IVY GROUP",                 "aporte_socio", "Aporte/Distribuição", "IVY Group (a classificar)"),
]

# ── FONTE ÚNICA (P1.3): fornecedores_classificacao.json ──
# A lista literal acima virou FALLBACK. O JSON é compartilhado com
# build-html.py; a ordem do array define a prioridade do match substring.
def _load_fornecedor_map_json() -> list:
    try:
        p = Path(__file__).resolve().parent / "fornecedores_classificacao.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        out = []
        for e in d.get("fornecedores", []):
            dre = e.get("dre")
            if not dre:
                continue
            for al in e.get("aliases", []):
                out.append((al, dre["grupo"], dre["sub"], dre["label"]))
        return out
    except Exception:
        return []


_fm_json = _load_fornecedor_map_json()
if _fm_json:
    FORNECEDOR_MAP = _fm_json

# Catch-all pra impostos detectados via HISTÓRICO (quando o contato é genérico)
HIST_IMPOSTO_PATTERNS = [
    (r"\bISS\b",         "deducoes_iss", "Impostos sobre vendas", "ISS"),
    (r"\bIRPJ\b",        "impostos_lucro", "Impostos sobre lucro", "IRPJ"),
    (r"\bCSLL\b",        "impostos_lucro", "Impostos sobre lucro", "CSLL"),
    (r"\bDCTFWEB\b",     "desp_pessoal", "Encargos sobre pró-labore", "DCTFWEB (INSS)"),
    (r"\bPIS\b",         "deducoes_pis", "Impostos sobre vendas", "PIS"),
    (r"\bCOFINS\b",      "deducoes_cofins", "Impostos sobre vendas", "COFINS"),
]


# ============ ESTRUTURA DAS DRE/P&L (linhas hierárquicas) ============
DRE_CONTABIL = [
    # (key, label, tipo, [subkeys] ou None)
    # tipo: header | line | subtotal | total
    ("RECEITA_BRUTA",  "RECEITA BRUTA",                "subtotal_pos", ["receita_servicos", "outras_receitas"]),
    ("receita_servicos", "  Receita de serviços",       "line", None),
    ("outras_receitas",  "  Outras receitas",           "line", None),
    ("DEDUCOES",       "(−) DEDUÇÕES DA RECEITA",      "subtotal_neg",
        ["deducoes_iss", "deducoes_pis", "deducoes_cofins",
         "deducoes_pis_esperado", "deducoes_cofins_esperado"]),
    ("deducoes_iss",            "  ISS",                                "line", None),
    ("deducoes_pis",            "  PIS (pago)",                         "line", None),
    ("deducoes_cofins",         "  COFINS (pago)",                      "line", None),
    ("deducoes_pis_esperado",   "  PIS (esperado – 0,65%)",             "line_alert", None),
    ("deducoes_cofins_esperado","  COFINS (esperado – 3%)",             "line_alert", None),
    ("RECEITA_LIQUIDA","= RECEITA LÍQUIDA",            "subtotal_calc", ["RECEITA_BRUTA", "-DEDUCOES"]),
    ("DESP_PESSOAL",   "(−) DESPESAS COM PESSOAL",     "subtotal_neg", ["desp_pessoal"]),
    ("desp_pessoal",   "  Pró-labore + prestadores PJ", "line", None),
    ("DESP_ADMIN",     "(−) DESPESAS ADMINISTRATIVAS", "subtotal_neg", ["desp_admin"]),
    ("desp_admin",     "  Serviços / administração",   "line", None),
    ("EBITDA",         "= EBITDA",                     "subtotal_calc", ["RECEITA_LIQUIDA", "-DESP_PESSOAL", "-DESP_ADMIN"]),
    ("FINANCEIRO",     "(±) RESULTADO FINANCEIRO",     "subtotal", ["financ_receita", "-financ_despesa"]),
    ("financ_receita", "  Receitas financeiras",       "line", None),
    ("financ_despesa", "  Despesas financeiras",       "line", None),
    ("LAIR",           "= LAIR",                       "subtotal_calc", ["EBITDA", "FINANCEIRO"]),
    ("IMPOSTOS_LUCRO", "(−) IMPOSTOS SOBRE LUCRO",     "subtotal_neg", ["impostos_lucro"]),
    ("impostos_lucro", "  IRPJ + CSLL",                "line", None),
    ("LUCRO_LIQUIDO",  "= LUCRO LÍQUIDO",              "subtotal_calc", ["LAIR", "-IMPOSTOS_LUCRO"]),
    # Itens estruturais — abaixo da linha
    ("SEP_NR",         "ITENS ESTRUTURAIS (informativo, fora do DRE operacional)", "header", None),
    ("nao_recorrente", "  Buy-out + Acordo trabalhista", "line", None),
    ("aporte_socio",   "  Aportes / Sócio (não-receita)","line", None),
]

PL_EXEC = [
    ("RECEITA",       "RECEITA OPERACIONAL",          "subtotal_pos", ["receita_servicos", "outras_receitas"]),
    ("receita_servicos", "  Receita de serviços",     "line", None),
    ("outras_receitas",  "  Outras receitas",         "line", None),
    ("DESP_OP",       "(−) DESPESAS OPERACIONAIS",    "subtotal_neg",
        ["desp_admin", "desp_pessoal", "DEDUCOES_VENDAS", "impostos_lucro"]),
    ("desp_admin",      "  Serviços estratégicos + Adm.", "line", None),
    ("desp_pessoal",    "  Pessoal / PJ / encargos",   "line", None),
    ("DEDUCOES_VENDAS", "  Impostos sobre vendas (ISS/PIS/COFINS)", "subtotal_neg",
        ["deducoes_iss", "deducoes_pis", "deducoes_cofins",
         "deducoes_pis_esperado", "deducoes_cofins_esperado"]),
    ("impostos_lucro",  "  IRPJ + CSLL",              "line", None),
    ("RESULT_OP",     "= RESULTADO OPERACIONAL",      "subtotal_calc", ["RECEITA", "-DESP_OP"]),
    # Estrutural
    ("SEP_NR",        "ITENS ESTRUTURAIS",            "header", None),
    ("nao_recorrente",  "  Buy-out + Acordo trabalhista", "line", None),
    ("aporte_socio",    "  Aportes do sócio (não somam)", "line", None),
    ("RESULT_CAIXA",  "= RESULTADO DE CAIXA",         "subtotal_calc", ["RESULT_OP", "-nao_recorrente"]),
]


# ============ HELPERS ============
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _norm(s: str) -> str:
    return _strip_accents((s or "").upper().strip())


def _classify(contato_nome: str, historico: str) -> tuple[str, str, str]:
    """Retorna (grupo, subgrupo, label) pra um lançamento."""
    n = _norm(contato_nome)
    h = _norm(historico)
    # Primeiro tenta match por fornecedor
    for pat, grupo, sub, label in FORNECEDOR_MAP:
        if _norm(pat) in n:
            return grupo, sub, label
    # Depois tenta match por histórico (impostos genéricos)
    for pat, grupo, sub, label in HIST_IMPOSTO_PATTERNS:
        if re.search(pat, h):
            return grupo, sub, label
    # Fallback: despesas administrativas — "outros"
    return "desp_admin", "Administrativas", contato_nome or "(sem nome)"


def _parse_money(s: Any) -> float:
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    # Guarda de formato (P2): "1234.56" americano — ponto decimal, sem vírgula
    # — viraria 123456 com o replace cego (inflação de 100x silenciosa).
    if "," not in s and re.fullmatch(r"-?\d+\.\d{1,2}", s):
        try:
            return float(s)
        except ValueError:
            return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


_MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez"]


def _fmt_brl(v: float, compact: bool = False) -> str:
    if v is None:
        return "—"
    if compact and abs(v) >= 1_000:
        if abs(v) >= 1_000_000:
            return f"R$ {v/1_000_000:.2f}M".replace(".", ",")
        return f"R$ {v/1_000:.0f}K"
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}" if v >= 0 else f"(R$ {s})"


def _fmt_comp(ym: str) -> str:
    if not ym or len(ym) < 7:
        return ym or "—"
    return f"{_MESES_PT[int(ym[5:7])-1]}/{ym[2:4]}"


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + 1
    if m > 12:
        m = 1; y += 1
    return f"{y:04d}-{m:02d}"


# ============ PROJEÇÃO DE RECEITA (recorrente vs pontual) ============
# Regras aprovadas pelo Leandro em 02/09/2026:
#   - Meses futuros projetam receita = max(média recorrente dos últimos N
#     meses reais, recorrente já faturado no mês) + pontuais faturados +
#     contratos projetados (streams por fora da média, ex.: TOTVS 50K/mês).
#   - "Pontual" = customização avulsa na ordem de R$ 12,5K (lançamento único
#     ou soma de parcelas do mesmo cliente/valor). Fica FORA da média rolante.
#   - Clientes em recorrentes_sempre nunca caem na regra de valor (comissões
#     TOTVS oscilam em 11-14K e colidiriam com o alvo).
# Config versionada em receitas_classificacao.json (nada hardcoded aqui).

_RECEITAS_CFG_PATH = Path(__file__).resolve().parent / "receitas_classificacao.json"
_RECEITAS_CFG_DEFAULT: dict[str, Any] = {
    "media_meses": 2,
    "pontual_valor_alvo": 12500.0,
    "pontual_tolerancia_pct": 15,
    "recorrentes_sempre": [],
    "pontuais_sempre": [],
    "contratos_projetados": [],
}


def _load_receitas_cfg() -> dict[str, Any]:
    cfg = dict(_RECEITAS_CFG_DEFAULT)
    try:
        raw = json.loads(_RECEITAS_CFG_PATH.read_text(encoding="utf-8"))
        for k in _RECEITAS_CFG_DEFAULT:
            if k in raw:
                cfg[k] = raw[k]
    except Exception:
        pass  # sem config → só média recorrente, sem pontuais/contratos
    return cfg


def _receita_classes(rows: list[dict], cfg: dict[str, Any]) -> list[str]:
    """
    Classifica cada lançamento de receita: "recorrente" | "pontual" | "contrato".
    Paralela a rows (mesma ordem). Contrato = match de contratos_projetados
    (padrão no contato + valor ±1%). Pontual = regra do valor alvo (single ou
    soma de parcelas mesmo cliente/mesmo valor), exceto recorrentes_sempre.
    """
    alvo = float(cfg.get("pontual_valor_alvo") or 0)
    tol = float(cfg.get("pontual_tolerancia_pct") or 0) / 100.0
    rec_sempre = [_norm(p) for p in cfg.get("recorrentes_sempre", [])]
    pont_sempre = [_norm(p) for p in cfg.get("pontuais_sempre", [])]
    contratos = cfg.get("contratos_projetados", [])

    def _near(v: float) -> bool:
        return alvo > 0 and abs(v - alvo) <= alvo * tol

    # Grupos de parcelas: (contato_norm, valor) → contagem
    grp_count: dict[tuple[str, float], int] = defaultdict(int)
    parsed = []
    for r in rows:
        n = _norm(r.get("contato_nome", ""))
        v = round(_parse_money(r.get("valor", 0)), 2)
        parsed.append((n, v))
        grp_count[(n, v)] += 1

    out = []
    for (n, v) in parsed:
        classe = "recorrente"
        for c in contratos:
            cv = float(c.get("valor") or 0)
            if _norm(c.get("contato", "")) in n and cv > 0 and abs(v - cv) <= cv * 0.01:
                classe = "contrato"
                break
        if classe == "recorrente":
            if any(p in n for p in pont_sempre):
                classe = "pontual"
            elif not any(p in n for p in rec_sempre):
                total_grp = v * grp_count[(n, v)]
                if _near(v) or (grp_count[(n, v)] > 1 and _near(total_grp)):
                    classe = "pontual"
        out.append(classe)
    return out


def _media_recorrente(
    recebidas: list[dict], cfg: dict[str, Any], cutoff: str
) -> tuple[float, list[str]]:
    """
    Média rolante da receita RECORRENTE dos últimos N meses reais (< cutoff)
    que tiveram receita recorrente > 0. Retorna (média, meses usados).
    """
    n_meses = int(cfg.get("media_meses") or 2)
    classes = _receita_classes(recebidas, cfg)
    por_mes: dict[str, float] = defaultdict(float)
    for r, cl in zip(recebidas, classes):
        ym = (r.get("vencimento") or "")[:7]
        if cl == "recorrente" and ym and ym < cutoff:
            por_mes[ym] += _parse_money(r.get("valor", 0))
    meses = sorted([m for m, v in por_mes.items() if v > 0], reverse=True)[:n_meses]
    if not meses:
        return 0.0, []
    media = sum(por_mes[m] for m in meses) / len(meses)
    return round(media, 2), sorted(meses)


def _projecao_receita_mes(
    ym: str,
    billed: list[tuple[dict, str]],  # (lançamento em aberto no mês, classe)
    media: float,
    meses_media: list[str],
    cfg: dict[str, Any],
) -> list[tuple[str, float]]:
    """
    Complementos de projeção para um mês futuro (rótulo, valor), a SOMAR ao
    que já está faturado no mês. Fórmula: max(média recorrente, recorrente
    faturado) + pontuais faturados + contratos (faturado se houver, senão o
    valor do contrato). Como pontuais/contratos faturados já estão na matriz,
    aqui saem só os deltas — sempre >= 0, nunca duplica.
    """
    rec_fat = sum(_parse_money(r.get("valor", 0)) for r, cl in billed if cl == "recorrente")
    partes: list[tuple[str, float]] = []
    delta_rec = max(media, rec_fat) - rec_fat
    if delta_rec > 0.005:
        ref = "+".join(_fmt_comp(m) for m in meses_media) or "sem base"
        partes.append((f"Projeção recorrente (média {ref})", round(delta_rec, 2)))
    for c in cfg.get("contratos_projetados", []):
        de, ate = c.get("de", ""), c.get("ate", "")
        cv = float(c.get("valor") or 0)
        if not (de and ate and de <= ym <= ate and cv > 0):
            continue
        fat_contrato = sum(
            _parse_money(r.get("valor", 0)) for r, cl in billed
            if cl == "contrato" and _norm(c.get("contato", "")) in _norm(r.get("contato_nome", ""))
        )
        if fat_contrato <= 0.005:  # nada lançado no Bling neste mês → projeta
            partes.append((f"Contrato {c.get('contato','')} (projetado)", round(cv, 2)))
    return partes


def _totvs_valor(mdict: Any) -> float:
    """Valor de comissão de um mês do totvs_por_mes: REAL > BASE > COMPLEMENTAR."""
    if not isinstance(mdict, dict):
        return _parse_money(mdict) if mdict else 0.0
    for k in ("REAL", "BASE", "COMPLEMENTAR"):
        if mdict.get(k):
            return _parse_money(mdict[k])
    return 0.0


def receita_sintetica_por_mes(
    recebidas: list[dict],
    receber_em_aberto: list[dict],
    today: date,
    totvs_por_mes: dict | None = None,
    ate: str | None = None,
    pagas: list[dict] | None = None,
) -> dict[str, list[tuple[str, float, str]]]:
    """
    Lançamentos SINTÉTICOS de receita, a somar aos dados Bling, por mês:
      {ym: [(rótulo, valor, tipo), ...]}

    - Meses PASSADOS sem nenhuma receita no Bling (nem recebida nem em
      aberto) e com comissão no snapshot Totvs → comissão Totvs, tipo
      "totvs". Caso real: receita jan-ago/2025 só existe no Totvs (Bling
      começou a registrar contas a receber em set/2025) — o DRE mostrava
      R$ 289K de receita 2025 contra despesas do ano inteiro (02/09/2026).
      Quando `pagas` é fornecido, o fill Totvs se limita aos meses em que o
      Bling tem alguma despesa paga: o snapshot Totvs volta até 2023, e
      preencher receita em mês sem NENHUM dado de despesa criaria o erro
      espelhado (meses só-receita parecendo lucro puro).
    - Mês atual + futuros até `ate` (default: dez do ano corrente) →
      complementos da projeção recorrente (_projecao_receita_mes), tipo
      "projetado".

    Fonte única para _build_matriz, _build_matriz_2y e para o gráfico
    Receita vs Despesas do build-html — as abas têm que contar a mesma
    história.
    """
    cutoff = today.strftime("%Y-%m")
    cfg = _load_receitas_cfg()
    media, meses_media = _media_recorrente(recebidas, cfg, cutoff)
    classes_ab = _receita_classes(receber_em_aberto, cfg)

    bling_por_mes: dict[str, float] = defaultdict(float)
    for r in recebidas:
        bling_por_mes[(r.get("vencimento") or "")[:7]] += _parse_money(r.get("valor", 0))
    for r in receber_em_aberto:
        bling_por_mes[(r.get("vencimento") or "")[:7]] += _parse_money(r.get("valor", 0))

    out: dict[str, list[tuple[str, float, str]]] = defaultdict(list)

    meses_com_despesa: set[str] | None = None
    if pagas is not None:
        meses_com_despesa = {(r.get("vencimento") or "")[:7] for r in pagas}

    # Passado: fill Totvs onde o Bling está mudo
    for ym, mdict in (totvs_por_mes or {}).items():
        if not (len(ym) == 7 and ym < cutoff):
            continue
        if meses_com_despesa is not None and ym not in meses_com_despesa:
            continue  # mês sem nenhum dado Bling → fica fora (não é só-receita)
        if bling_por_mes.get(ym, 0) > 0:
            continue  # Bling tem receita no mês (mais completa) → Bling vence
        v = _totvs_valor(mdict)
        if v > 0:
            out[ym].append(("Comissões Totvs (relatório de comissões)", round(v, 2), "totvs"))

    # Presente/futuro: projeção recorrente
    ate = ate or f"{today.year}-12"
    ym = cutoff
    while ym <= ate:
        billed = [(r, cl) for r, cl in zip(receber_em_aberto, classes_ab)
                  if (r.get("vencimento") or "")[:7] == ym]
        for label, v in _projecao_receita_mes(ym, billed, media, meses_media, cfg):
            out[ym].append((label, v, "projetado"))
        ym = _next_month(ym)

    return dict(out)


# ============ AGREGAÇÃO ============
def _build_matriz(
    pagas: list[dict],
    recebidas: list[dict],
    em_aberto: list[dict],
    receber_em_aberto: list[dict],
    today: date,
    months_window: int = 6,
) -> dict[str, Any]:
    """
    Retorna estrutura:
    {
      "months": ["YYYY-MM", ...],  # ordenados
      "month_types": {"YYYY-MM": "real"|"projetado"},
      "grupos": {grupo_key: {month: float, ...}, ...},
      "subgrupos": {grupo_key: {sub_key: {month: float, ...}, ...}, ...},
      "items": {grupo_key: {sub_key: [items], ...}, ...},  # detalhes pra drill
      "receita_por_mes": {month: float, ...},  # pra estimativa imposto
    }
    """
    cutoff = today.strftime("%Y-%m")
    # Janela: últimos N meses + projeção próximos meses
    def month_range(start_ym: str, n: int) -> list[str]:
        y, m = int(start_ym[:4]), int(start_ym[5:7])
        out = []
        for _ in range(n):
            out.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0: m = 12; y -= 1
        return list(reversed(out))

    months = month_range(cutoff, months_window)  # últimos N até mês atual (projetado)
    # Meses futuros: até dezembro do ano corrente (mín. 2) — pedido de
    # 02/09/2026: o P&L precisa mostrar set–dez projetados, não só +2.
    n_fut = max(2, 12 - int(cutoff[5:7]))
    for i in range(n_fut):
        months.append(_next_month(months[-1]))
    month_types = {m: ("real" if m < cutoff else "projetado") for m in months}

    grupos: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    subgrupos: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    receita_por_mes: dict[str, float] = defaultdict(float)

    def add_desp(r: dict, ym: str, kind: str):
        nome = r.get("contato_nome", "") or ""
        hist = r.get("historico", "") or ""
        grupo, sub, label = _classify(nome, hist)
        v = _parse_money(r.get("valor", 0))
        grupos[grupo][ym] += v
        subgrupos[grupo][sub][ym] += v
        items[grupo][sub].append({
            "ym": ym, "venc": r.get("vencimento", ""),
            "contato": nome, "historico": hist,
            "valor": round(v, 2), "doc": r.get("numero_documento", ""),
            "cat_bling": r.get("categoria_descricao", ""),
            "tipo": kind,
        })

    def add_rec(r: dict, ym: str, kind: str):
        v = _parse_money(r.get("valor", 0))
        grupos["receita_servicos"][ym] += v
        subgrupos["receita_servicos"]["Serviços"][ym] += v
        items["receita_servicos"]["Serviços"].append({
            "ym": ym, "venc": r.get("vencimento", ""),
            "contato": r.get("contato_nome", "") or "",
            "historico": r.get("historico", "") or "",
            "valor": round(v, 2), "doc": r.get("numero_documento", ""),
            "cat_bling": r.get("categoria_descricao", ""),
            "tipo": kind,
        })
        receita_por_mes[ym] += v

    # cutoff aqui = mês atual. Tratamento:
    #   - pagas/recebidas: "real" (dinheiro que se moveu).
    #   - em_aberto: SEMPRE "em_aberto"/"projetado" — inclusive vencido.
    #     Vencido não pago fica no mês de vencimento (senão some do DRE), mas
    #     JAMAIS herda o rótulo "real": rótulo de pago em conta não paga fazia
    #     junho/26 exibir R$ 59,8K do Rômulo como desembolsado quando o caixa
    #     nunca viu esse dinheiro sair (auditoria 21/ago, P0.4).
    for r in pagas:
        ym = (r.get("vencimento") or "")[:7]
        if ym in month_types and ym <= cutoff:
            add_desp(r, ym, "real")
    for r in recebidas:
        ym = (r.get("vencimento") or "")[:7]
        if ym in month_types and ym <= cutoff:
            add_rec(r, ym, "real")
    # Em aberto: mês atual + futuros + atrasados (passado mas ainda não pago)
    for r in em_aberto:
        ym = (r.get("vencimento") or "")[:7]
        if ym in month_types:
            tipo = "em_aberto" if ym <= cutoff else "projetado"
            add_desp(r, ym, tipo)
    for r in receber_em_aberto:
        ym = (r.get("vencimento") or "")[:7]
        if ym in month_types:
            tipo = "em_aberto" if ym <= cutoff else "projetado"
            add_rec(r, ym, tipo)

    # ─── PROJEÇÃO DE RECEITA (mês atual + futuros) ───
    # max(média recorrente últimos N meses, recorrente faturado) + pontuais
    # faturados + contratos projetados. Ver receitas_classificacao.json.
    _cfg_rec = _load_receitas_cfg()
    _media, _meses_media = _media_recorrente(recebidas, _cfg_rec, cutoff)
    _classes_ab = _receita_classes(receber_em_aberto, _cfg_rec)
    for ym in months:
        if ym < cutoff:
            continue
        billed = [(r, cl) for r, cl in zip(receber_em_aberto, _classes_ab)
                  if (r.get("vencimento") or "")[:7] == ym]
        for label, v in _projecao_receita_mes(ym, billed, _media, _meses_media, _cfg_rec):
            add_rec({"contato_nome": label, "historico": "Projeção automática",
                     "vencimento": f"{ym}-28", "valor": v}, ym, "projetado")

    # Calcula PIS/COFINS esperado (alíquotas Lucro Presumido) — só quando NÃO
    # houve pagamento real do tributo no mês (evita duplicar). Alíquotas vêm
    # do CONFIG central (audit.py).
    pis_alq = _AUDIT_CFG["pis_aliq"]
    cofins_alq = _AUDIT_CFG["cofins_aliq"]
    for ym, rec in receita_por_mes.items():
        if ym not in month_types:
            continue
        # meses reais E meses projetados (>= cutoff) com receita: sem as
        # deduções esperadas a projeção de resultado ficava otimista.
        if month_types[ym] != "real" and ym < cutoff:
            continue
        pis_pago = grupos.get("deducoes_pis", {}).get(ym, 0)
        if not pis_pago:
            grupos["deducoes_pis_esperado"][ym] = round(rec * pis_alq, 2)
        cofins_pago = grupos.get("deducoes_cofins", {}).get(ym, 0)
        if not cofins_pago:
            grupos["deducoes_cofins_esperado"][ym] = round(rec * cofins_alq, 2)

    return {
        "months": months,
        "month_types": month_types,
        "grupos": {k: dict(v) for k, v in grupos.items()},
        "subgrupos": {k: {sk: dict(sv) for sk, sv in v.items()} for k, v in subgrupos.items()},
        "items": {k: dict(v) for k, v in items.items()},
        "receita_por_mes": dict(receita_por_mes),
    }


# ============ AGREGAÇÃO ESTENDIDA — 2 anos completos com gap fill ============
def _build_matriz_2y(
    pagas: list[dict],
    recebidas: list[dict],
    em_aberto: list[dict],
    receber_em_aberto: list[dict],
    today: date,
    start_year: int = 2025,
    end_year: int = 2026,
    totvs_por_mes: dict | None = None,
) -> dict[str, Any]:
    """
    Retorna matriz cobrindo jan/start_year a dez/end_year (24 meses por padrão).

    month_types possíveis:
      - "real"        — mês passado com dados pagos/recebidos
      - "em_aberto"   — mês com base em contas em aberto (vencimento futuro / parcial)
      - "projetado"   — mês futuro extrapolado (gap fill)
      - "sem_dado"    — mês passado sem nenhum lançamento no Bling

    Gap fill:
      - Meses FUTUROS: em aberto + complemento com média dos meses reais.
      - Meses PASSADOS sem dado: NÃO são preenchidos. Ficam zerados e são
        marcados "sem_dado", renderizando como travessão.

    Por que mês passado não é extrapolado (mudança de 21/ago/2026):
        A versão anterior preenchia mês passado vazio com a média dos meses
        reais — inclusive puxando a média de OUTRO ano quando o ano não tinha
        nenhum dado. Na prática isso fabricava a coluna 2025 inteira a partir
        de 2026: com o snapshot sem nenhuma conta a pagar de 2025, o DRE
        exibia "2025: despesas R$ 607K" onde o valor real conhecido era zero,
        e o buy-out aparecia como R$ 53K (= média do acordo trabalhista de
        2026 espalhada por 8 meses de 2025). O comparativo "2025 vs 2026"
        estava, na prática, comparando 2026 contra uma projeção dele mesmo.

        Ausência de dado agora aparece como ausência de dado. Se um ano
        antigo precisa aparecer de verdade, a correção é trazer o dado —
        rodar o workflow `update` com `janela_meses` maior e deixar o
        `ci/merge_historico.py` acumular — não inventar a média.

    Cada linha de detalhe em "items" recebe campo "kind" indicando origem.
    """
    cutoff = today.strftime("%Y-%m")

    # Gerar lista completa de meses
    months: list[str] = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            months.append(f"{y:04d}-{m:02d}")

    grupos: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    subgrupos: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    receita_por_mes: dict[str, float] = defaultdict(float)
    # marcador: quais meses tiveram ao menos 1 lançamento real
    meses_com_dado_real: set[str] = set()
    meses_com_dado_aberto: set[str] = set()

    def add_desp(r: dict, ym: str, kind: str):
        nome = r.get("contato_nome", "") or ""
        hist = r.get("historico", "") or ""
        grupo, sub, label = _classify(nome, hist)
        v = _parse_money(r.get("valor", 0))
        grupos[grupo][ym] += v
        subgrupos[grupo][sub][ym] += v
        items[grupo][sub].append({
            "ym": ym, "venc": r.get("vencimento", ""),
            "contato": nome, "historico": hist,
            "valor": round(v, 2), "doc": r.get("numero_documento", ""),
            "cat_bling": r.get("categoria_descricao", ""),
            "kind": kind,
        })
        if kind == "real":
            meses_com_dado_real.add(ym)
        else:
            meses_com_dado_aberto.add(ym)

    def add_rec(r: dict, ym: str, kind: str):
        v = _parse_money(r.get("valor", 0))
        grupos["receita_servicos"][ym] += v
        subgrupos["receita_servicos"]["Serviços"][ym] += v
        items["receita_servicos"]["Serviços"].append({
            "ym": ym, "venc": r.get("vencimento", ""),
            "contato": r.get("contato_nome", "") or "",
            "historico": r.get("historico", "") or "",
            "valor": round(v, 2), "doc": r.get("numero_documento", ""),
            "cat_bling": r.get("categoria_descricao", ""),
            "kind": kind,
        })
        receita_por_mes[ym] += v
        if kind == "real":
            meses_com_dado_real.add(ym)
        else:
            meses_com_dado_aberto.add(ym)

    # Ingestão dos dados Bling
    months_set = set(months)
    for r in pagas:
        ym = (r.get("vencimento") or "")[:7]
        if ym in months_set and ym <= cutoff:
            add_desp(r, ym, "real")
    for r in recebidas:
        ym = (r.get("vencimento") or "")[:7]
        if ym in months_set and ym <= cutoff:
            add_rec(r, ym, "real")
    # Em aberto NUNCA vira "real" — nem quando o vencimento já passou. Vencido
    # não pago permanece no mês (compromisso), mas com a marca de em aberto;
    # rotular como pago escondia R$ 95K de inadimplência própria (P0.4).
    for r in em_aberto:
        ym = (r.get("vencimento") or "")[:7]
        if ym in months_set:
            add_desp(r, ym, "em_aberto")
    for r in receber_em_aberto:
        ym = (r.get("vencimento") or "")[:7]
        if ym in months_set:
            add_rec(r, ym, "em_aberto")

    # PIS/COFINS esperado (só nos meses reais com receita)
    pis_alq = _AUDIT_CFG["pis_aliq"]
    cofins_alq = _AUDIT_CFG["cofins_aliq"]
    for ym in meses_com_dado_real:
        rec = receita_por_mes.get(ym, 0)
        if rec <= 0:
            continue
        pis_pago = grupos.get("deducoes_pis", {}).get(ym, 0)
        if not pis_pago:
            grupos["deducoes_pis_esperado"][ym] = round(rec * pis_alq, 2)
        cofins_pago = grupos.get("deducoes_cofins", {}).get(ym, 0)
        if not cofins_pago:
            grupos["deducoes_cofins_esperado"][ym] = round(rec * cofins_alq, 2)

    # Classificar tipos de mês (granularidade de mês, não de célula)
    month_types: dict[str, str] = {}
    for m in months:
        if m in meses_com_dado_real:
            month_types[m] = "real"
        elif m in meses_com_dado_aberto:
            month_types[m] = "em_aberto"
        elif m < cutoff:
            # Mês passado sem lançamento nenhum. Não extrapola (ver docstring).
            month_types[m] = "sem_dado"
        else:
            month_types[m] = "projetado"

    # ─── GAP FILL CÉLULA-A-CÉLULA ───
    # cell_kinds[grupo][month] = origem do valor:
    #   "real"        — vem direto de contas pagas/recebidas
    #   "em_aberto"   — vem de contas em aberto (vencimento futuro real)
    #   "sem_dado"    — mês passado sem lançamento (fica zerado, vira "—")
    #   "projetado"   — valor preenchido por média histórica (mês futuro)
    # Estratégia de fill (só para meses FUTUROS com célula = 0):
    #   1) média do MESMO grupo nos meses real do MESMO ano
    #   2) fallback: média no MESMO grupo em todos os anos
    #   3) se nada, fica 0 (sem dado histórico para projetar)
    # Mês passado sem dado não entra no fill — ausência não vira média.

    # 1. Marcar todas as células que vieram de dados ingeridos (não-zero)
    cell_kinds: dict[str, dict[str, str]] = defaultdict(dict)
    for gkey, mvals in grupos.items():
        for m, v in mvals.items():
            if v == 0:
                continue
            mt = month_types.get(m, "projetado")
            cell_kinds[gkey][m] = mt  # vai ser "real" ou "em_aberto"

    # 2. Reference months: separados por (kind, year) para fallback escalonado
    real_months_by_year: dict[int, list[str]] = defaultdict(list)
    for m, t in month_types.items():
        if t == "real":
            real_months_by_year[int(m[:4])].append(m)
    all_real_months = sum(real_months_by_year.values(), [])

    def _media_grupo(gkey: str, ref: list[str]) -> float:
        vals = [grupos.get(gkey, {}).get(rm, 0) for rm in ref]
        if not any(vals):
            return 0.0
        return sum(vals) / len(vals)

    # 3. Para cada (grupo, mês não-real) sem valor, calcula fill
    all_grupo_keys = list(grupos.keys()) or []
    for ym in months:
        mt = month_types[ym]
        if mt in ("real", "sem_dado"):
            continue
        year = int(ym[:4])
        for gkey in all_grupo_keys:
            if gkey == "receita_servicos" and ym >= cutoff:
                continue  # receita futura tem projeção própria (média recorrente)
            current = grupos.get(gkey, {}).get(ym, 0)
            if current != 0:
                continue  # já tem valor real ou em_aberto
            # Tier 1: mesmo ano
            media = _media_grupo(gkey, real_months_by_year.get(year, []))
            origem = "same_year"
            # Tier 2: todos os anos
            if media == 0:
                media = _media_grupo(gkey, all_real_months)
                origem = "all_years"
            if media != 0:
                grupos[gkey][ym] = round(media, 2)
                cell_kinds[gkey][ym] = "projetado"

    # 4. Receita por mês
    receita_kinds: dict[str, str] = {}
    for m in months:
        if receita_por_mes.get(m, 0) > 0:
            receita_kinds[m] = month_types[m]

    # 4a. Receita sintética (fonte única: receita_sintetica_por_mes):
    #   - meses passados sem receita no Bling → comissões Totvs, kind "totvs"
    #     (caso real: jan-ago/2025 — Bling só registra a receber desde set/25)
    #   - meses >= cutoff → projeção recorrente (regras de 02/09/2026), kind
    #     "projetado". Ver receitas_classificacao.json.
    _sint = receita_sintetica_por_mes(recebidas, receber_em_aberto, today,
                                      totvs_por_mes=totvs_por_mes,
                                      ate=f"{end_year:04d}-12", pagas=pagas)
    for ym, partes in sorted(_sint.items()):
        if ym not in months_set:
            continue
        if ym >= cutoff and month_types[ym] == "real":
            continue  # mês corrente já com recebimento real: não complementa
        aplicou = None
        for label, v, tipo in partes:
            grupos["receita_servicos"][ym] += v
            subgrupos["receita_servicos"]["Serviços"][ym] += v
            receita_por_mes[ym] += v
            items["receita_servicos"]["Serviços"].append({
                "ym": ym, "venc": f"{ym}-28", "contato": label,
                "historico": "Projeção automática" if tipo == "projetado"
                             else "Receita via relatório Totvs (sem lançamento no Bling)",
                "valor": round(v, 2),
                "doc": "", "cat_bling": "", "kind": tipo,
            })
            aplicou = tipo
        if not aplicou:
            continue
        grupos["receita_servicos"][ym] = round(grupos["receita_servicos"][ym], 2)
        receita_por_mes[ym] = round(receita_por_mes[ym], 2)
        receita_kinds[ym] = aplicou
        cell_kinds["receita_servicos"][ym] = aplicou
        # Deduções esperadas sobre a receita sintética (mesma regra: só se o
        # tributo não foi pago no mês) — sem isso o mês preenchido fica sem
        # PIS/COFINS e o resultado sai otimista.
        rec_m = receita_por_mes[ym]
        if rec_m > 0:
            if not grupos.get("deducoes_pis", {}).get(ym) and not grupos.get("deducoes_pis_esperado", {}).get(ym):
                grupos["deducoes_pis_esperado"][ym] = round(rec_m * pis_alq, 2)
                cell_kinds["deducoes_pis_esperado"][ym] = aplicou
            if not grupos.get("deducoes_cofins", {}).get(ym) and not grupos.get("deducoes_cofins_esperado", {}).get(ym):
                grupos["deducoes_cofins_esperado"][ym] = round(rec_m * cofins_alq, 2)
                cell_kinds["deducoes_cofins_esperado"][ym] = aplicou

    # 4b. Meses passados não-reais: extrapola se zero (comportamento antigo)
    for ym in months:
        mt = month_types[ym]
        if mt in ("real", "sem_dado") or receita_por_mes.get(ym, 0) > 0 or ym >= cutoff:
            continue
        year = int(ym[:4])
        ref = real_months_by_year.get(year, []) or all_real_months
        rec_vals = [receita_por_mes.get(rm, 0) for rm in ref]
        if any(rec_vals):
            rec_media = sum(rec_vals) / len(rec_vals)
            receita_por_mes[ym] = round(rec_media, 2)
            receita_kinds[ym] = "projetado"

    # 5. PIS/COFINS esperado para meses não-reais com receita projetada
    for ym in months:
        if month_types[ym] in ("real", "sem_dado"):
            continue
        rec = receita_por_mes.get(ym, 0)
        if rec <= 0:
            continue
        if not grupos.get("deducoes_pis", {}).get(ym):
            grupos["deducoes_pis_esperado"][ym] = round(rec * pis_alq, 2)
            cell_kinds["deducoes_pis_esperado"][ym] = "projetado"
        if not grupos.get("deducoes_cofins", {}).get(ym):
            grupos["deducoes_cofins_esperado"][ym] = round(rec * cofins_alq, 2)
            cell_kinds["deducoes_cofins_esperado"][ym] = "projetado"

    return {
        "months": months,
        "month_types": month_types,
        "cell_kinds": {k: dict(v) for k, v in cell_kinds.items()},
        "receita_kinds": receita_kinds,
        "grupos": {k: dict(v) for k, v in grupos.items()},
        "subgrupos": {k: {sk: dict(sv) for sk, sv in v.items()} for k, v in subgrupos.items()},
        "items": {k: dict(v) for k, v in items.items()},
        "receita_por_mes": dict(receita_por_mes),
        "start_year": start_year,
        "end_year": end_year,
    }


# ============ SOMA DE LINHAS COMPOSTAS ============
def _line_value(matriz: dict, key: str, month: str) -> float:
    """Resolve valor de uma linha (simples ou agregada) num mês."""
    if key in matriz["grupos"]:
        return matriz["grupos"][key].get(month, 0)
    return 0.0


def _resolve_subtotal(matriz: dict, struct_def: list, key: str, month: str, _cache: dict | None = None) -> float:
    """Resolve subtotais recursivamente seguindo a estrutura DRE/P&L."""
    if _cache is None:
        _cache = {}
    cache_key = (key, month)
    if cache_key in _cache:
        return _cache[cache_key]
    # Procura na struct
    for k, label, tipo, subs in struct_def:
        if k == key:
            if tipo == "line" or tipo == "line_alert":
                v = _line_value(matriz, key, month)
                _cache[cache_key] = v
                return v
            elif tipo in ("subtotal_pos", "subtotal_neg", "subtotal", "subtotal_calc"):
                total = 0.0
                for sk in (subs or []):
                    sign = 1
                    if sk.startswith("-"):
                        sign = -1
                        sk = sk[1:]
                    total += sign * _resolve_subtotal(matriz, struct_def, sk, month, _cache)
                _cache[cache_key] = total
                return total
    # Fallback
    v = _line_value(matriz, key, month)
    _cache[cache_key] = v
    return v


# ============ TOTVS RECONCILIAÇÃO ============
def _load_totvs_por_mes(totvs_snap: Path) -> dict[str, dict]:
    """Retorna {ym: {tipo: total, ...}, ...} a partir do snapshot Totvs."""
    if not totvs_snap.exists():
        return {}
    try:
        d = json.loads(totvs_snap.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for doc in d.get("documentos", []):
        ym = (doc.get("competencia") or "")[:7]
        tipo = doc.get("tipo", "")
        if ym and tipo:
            out[ym][tipo] += (doc.get("total_geral") or 0)
    return {ym: dict(v) for ym, v in out.items()}


# ============ HTML RENDER ============
def _render_matriz(matriz: dict, struct_def: list, title: str, with_alert: bool = True) -> str:
    """Renderiza a matriz como uma tabela HTML responsiva com drill."""
    months = matriz["months"]
    p = []
    p.append(f'<div class="card" style="margin-bottom:14px"><div class="ct"><b>{escape(title)}</b><span>{len(months)} meses · clique nos itens para abrir lançamentos</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt dre-mat"><thead><tr>')
    p.append('<th style="position:sticky;left:0;background:var(--s1);z-index:2">Item</th>')
    for m in months:
        cls = "" if matriz["month_types"][m] == "real" else "style=\"color:var(--amber)\""
        p.append(f'<th class="r" {cls}>{_fmt_comp(m)}</th>')
    p.append('<th class="r" style="border-left:1px solid var(--bd)">Total</th>')
    p.append('<th class="r">AV%</th>')
    p.append('</tr></thead><tbody>')

    # Linha receita pra cálculo de AV%
    cache = {}
    receita_totals = {m: _resolve_subtotal(matriz, struct_def, "RECEITA" if any(k[0] == "RECEITA" for k in struct_def) else "RECEITA_BRUTA", m, cache) for m in months}
    receita_total_periodo = sum(receita_totals.values()) or 1

    for k, label, tipo, subs in struct_def:
        if tipo == "header":
            p.append(f'<tr style="background:var(--s3)"><td colspan="{len(months)+3}" style="padding:8px 10px;font-family:var(--mono);font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.08em;font-weight:500">{escape(label)}</td></tr>')
            continue

        if tipo in ("subtotal_pos", "subtotal_neg", "subtotal", "subtotal_calc"):
            # Total da linha
            total = sum(_resolve_subtotal(matriz, struct_def, k, m, cache) for m in months)
            av = (total / receita_total_periodo * 100) if receita_total_periodo and "RECEITA" not in k else (100 if "RECEITA" in k else 0)
            color = "var(--green)" if total >= 0 and tipo == "subtotal_pos" else ("var(--red)" if tipo == "subtotal_neg" else ("var(--green)" if total >= 0 else "var(--red)"))
            bg = "var(--s2)" if tipo == "subtotal_calc" else "transparent"
            bd = "2px solid var(--bd2)" if tipo == "subtotal_calc" else "1px solid var(--bd)"
            p.append(f'<tr style="background:{bg};border-top:{bd}"><td style="position:sticky;left:0;background:{bg};z-index:1;font-weight:500;font-size:12px;padding:8px 10px">{escape(label)}</td>')
            for m in months:
                v = _resolve_subtotal(matriz, struct_def, k, m, cache)
                c = color
                p.append(f'<td class="r" title="{_fmt_brl(v)}" style="font-family:var(--mono);font-weight:500;color:{c};padding:8px 10px">{_fmt_brl(v, True)}</td>')
            p.append(f'<td class="r" title="{_fmt_brl(total)}" style="font-family:var(--mono);font-weight:500;color:{color};padding:8px 10px;border-left:1px solid var(--bd)">{_fmt_brl(total, True)}</td>')
            p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t3)">{av:+.1f}%</td>')
            p.append('</tr>')

        elif tipo == "line" or tipo == "line_alert":
            is_alert = tipo == "line_alert"
            # Valor por mês
            total = sum(_line_value(matriz, k, m) for m in months)
            av = (total / receita_total_periodo * 100) if receita_total_periodo else 0
            color = "var(--red)" if is_alert else ("var(--t2)")
            has_items = (k in matriz.get("items", {}))
            clickable = has_items
            row_attrs = f'onclick="dreToggleRow(\'{k}\')"' if clickable else ''
            cursor = "cursor:pointer" if clickable else ""
            caret = f'<span id="dreCaret-{k}" style="color:var(--t3);font-family:var(--mono);font-size:10px;margin-right:5px;user-select:none">▸</span>' if clickable else '<span style="margin-right:14px"></span>'
            alert_icon = '<span style="margin-left:6px" title="Não pago — verificar com contador">⚠</span>' if is_alert and total > 0 else ''
            p.append(f'<tr class="dre-row" {row_attrs} style="{cursor}"><td style="position:sticky;left:0;background:var(--s1);z-index:1;padding:6px 10px;font-size:11.5px;{("color:"+color+";" if is_alert else "")}">{caret}{escape(label)}{alert_icon}</td>')
            for m in months:
                v = _line_value(matriz, k, m)
                cm = color if is_alert else "var(--t1)"
                _ttl = f" title='{_fmt_brl(v)}'" if v else ""
                p.append(f'<td class="r"{_ttl} style="font-family:var(--mono);font-size:11px;color:{cm};padding:6px 8px">{_fmt_brl(v, True) if v else "—"}</td>')
            p.append(f'<td class="r" title="{_fmt_brl(total)}" style="font-family:var(--mono);font-size:11.5px;font-weight:500;color:{color};padding:6px 8px;border-left:1px solid var(--bd)">{_fmt_brl(total, True)}</td>')
            p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t3)">{av:+.1f}%</td>')
            p.append('</tr>')
            # Linha de drilldown (oculta)
            if clickable:
                p.append(f'<tr class="dre-detail" id="dreDetailRow-{k}" style="display:none;background:var(--s2)"><td colspan="{len(months)+3}" id="dreDetailBody-{k}" style="padding:0"></td></tr>')

    p.append('</tbody></table></div></div>')
    return "".join(p)


def _render_impostos_check(matriz: dict, totvs: dict) -> str:
    """Tabela de impostos: pago vs esperado, mês a mês."""
    months = [m for m in matriz["months"] if matriz["month_types"][m] == "real"]
    if not months:
        return ""

    p = []
    cfg = _AUDIT_CFG
    iss_aliq = cfg["iss_sbc_aliq"]
    irpj_efetivo = cfg["irpj_aliq"] * cfg["irpj_base_presumida"]
    csll_efetivo = cfg["csll_aliq"] * cfg["csll_base_presumida"]
    cbs_ibs_aliq = cfg["cbs_teste_aliq"] + cfg["ibs_teste_aliq"]

    p.append(f'<div class="card" style="margin-bottom:14px"><div class="ct"><b>Verificação tributária — Lucro Presumido (32%) · sede SBC</b><span>Pago vs esperado · ⚠ = pode estar faltando · alíquotas: PIS {cfg["pis_aliq"]*100:.2f}% | COFINS {cfg["cofins_aliq"]*100:.0f}% | ISS-SBC {iss_aliq*100:.1f}% | IRPJ {irpj_efetivo*100:.1f}% (+10% acima R$ 60K/trim) | CSLL {csll_efetivo*100:.2f}% | CBS/IBS teste 2026 {cbs_ibs_aliq*100:.1f}%</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Tributo</th>')
    for m in months:
        p.append(f'<th class="r">{_fmt_comp(m)}</th>')
    p.append('<th class="r" style="border-left:1px solid var(--bd)">Total pago</th>')
    p.append('<th class="r">Total esperado</th>')
    p.append('<th class="r">Δ</th>')
    p.append('<th>Status</th>')
    p.append('</tr></thead><tbody>')

    # Receita por mês
    rec_por_mes = matriz["receita_por_mes"]

    tributos = [
        ("PIS",      cfg["pis_aliq"],     None,                "Sobre receita bruta (cumulativo)"),
        ("COFINS",   cfg["cofins_aliq"],  None,                "Sobre receita bruta (cumulativo)"),
        ("ISS",      iss_aliq,            "deducoes_iss",      f"SBC · {iss_aliq*100:.1f}% (placeholder — validar c/ Serrano)"),
        ("IRPJ",     irpj_efetivo,        "impostos_lucro_irpj","Base 32% × 15% — efetivo (sem adicional 10%)"),
        ("CSLL",     csll_efetivo,        "impostos_lucro_csll","Base 32% × 9%"),
        ("CBS/IBS",  cbs_ibs_aliq,        "cbs_ibs",           "Reforma 2026 · 100% compensável (placeholder)"),
        ("DCTFWEB",  None,                "desp_pessoal",      "INSS sobre pró-labore (DARF)"),
    ]

    # Calcular pagos: por enquanto sub-totalizar nos grupos
    # ISS = grupos["deducoes_iss"] ou subgrupos["deducoes"]["Impostos sobre vendas"]
    # IRPJ/CSLL = subgrupos["impostos_lucro"]["Impostos sobre lucro"]
    # DCTFWEB = subgrupos["desp_pessoal"]["Encargos sobre pró-labore"]
    def pago_no_mes(tributo: str, ym: str) -> float:
        # Procura nos items pelo label
        for grupo, subs in matriz.get("items", {}).items():
            for sub, lst in subs.items():
                for it in lst:
                    if it["ym"] != ym: continue
                    n = _norm(it["contato"])
                    h = _norm(it["historico"])
                    if tributo == "PIS"  and re.search(r"\bPIS\b", h): return float(it["valor"])
                    if tributo == "COFINS" and re.search(r"\bCOFINS\b", h): return float(it["valor"])
                    if tributo == "ISS" and "ISS" in h and "PREFEITURA" in n:
                        return _sum_for(matriz, "deducoes_iss", ym)
                    if tributo == "IRPJ" and ("IRPJ" in n or re.search(r"\bIRPJ\b", h)):
                        return _sum_irpj_csll(matriz, "IRPJ", ym)
                    if tributo == "CSLL" and ("CSLL" in n or re.search(r"\bCSLL\b", h)):
                        return _sum_irpj_csll(matriz, "CSLL", ym)
                    if tributo == "DCTFWEB" and "DCTFWEB" in h:
                        return _sum_subgroup(matriz, "desp_pessoal", "Encargos sobre pró-labore", ym)
        # Fallback by group
        if tributo == "ISS":     return _sum_for(matriz, "deducoes_iss", ym)
        if tributo == "DCTFWEB": return _sum_subgroup(matriz, "desp_pessoal", "Encargos sobre pró-labore", ym)
        return 0.0

    for tributo, alq, _gk, descr in tributos:
        p.append(f'<tr><td style="padding:6px 10px;font-size:11.5px"><b>{tributo}</b><div style="font-size:9px;color:var(--t3);font-family:var(--mono)">{escape(descr)}</div></td>')
        total_pago = 0.0
        total_esp = 0.0
        for m in months:
            rec = rec_por_mes.get(m, 0)
            esp = (rec * alq) if alq else None
            pago = _calc_pago_tributo(matriz, tributo, m)
            total_pago += pago
            if esp is not None:
                total_esp += esp
            cell_color = "var(--t1)"
            if alq is not None:
                if pago == 0 and esp and esp > 10:
                    cell_color = "var(--red)"
                elif esp and abs(pago - esp) / max(esp,1) > 0.3:
                    cell_color = "var(--amber)"
            label = _fmt_brl(pago, True) if pago else ("—" if not esp else f'<span style="color:var(--red)">⚠ {_fmt_brl(esp, True)}</span>')
            p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:{cell_color};padding:6px 8px">{label}</td>')

        # Total
        p.append(f'<td class="r" style="font-family:var(--mono);font-weight:500;padding:6px 10px;border-left:1px solid var(--bd)">{_fmt_brl(total_pago, True)}</td>')
        if alq:
            p.append(f'<td class="r" style="font-family:var(--mono);color:var(--t3);font-size:10.5px">{_fmt_brl(total_esp, True)}</td>')
            diff = total_pago - total_esp
            dc = "var(--green)" if diff >= 0 else "var(--red)"
            p.append(f'<td class="r" style="font-family:var(--mono);font-size:10.5px;color:{dc}">{_fmt_brl(diff, True)}</td>')
            if total_pago == 0 and total_esp > 10:
                p.append(f'<td><span class="pill pr">⚠ Faltando</span></td>')
            elif abs(diff)/max(total_esp,1) < 0.15:
                p.append(f'<td><span class="pill pg2">OK</span></td>')
            else:
                p.append(f'<td><span class="pill pa">Verificar</span></td>')
        else:
            p.append(f'<td class="r" style="color:var(--t3)">—</td><td class="r" style="color:var(--t3)">—</td><td><span class="pill pb2">Informativo</span></td>')
        p.append('</tr>')

    p.append('</tbody></table></div>')
    p.append('<div style="margin-top:10px;padding:10px 12px;background:var(--amber-bg);border:1px solid var(--amber-br);border-radius:8px;font-size:11px;color:var(--amber);line-height:1.5"><b>⚠ Importante:</b> Estimativas baseadas no regime <b>Lucro Presumido — Serviços (base 32%)</b>. PIS/COFINS podem estar sendo retidos na fonte pelo cliente (Totvs faz isso em algumas NFs — campo IMPOSTONF no relatório). <b>Verificar com contador antes de tomar ação.</b></div>')
    p.append('</div>')
    return "".join(p)


def _sum_for(matriz: dict, group_key: str, ym: str) -> float:
    return matriz.get("grupos", {}).get(group_key, {}).get(ym, 0)


def _sum_subgroup(matriz: dict, group_key: str, sub_key: str, ym: str) -> float:
    """Soma só um subgrupo (não o grupo inteiro). Usado pra isolar o DCTFWEB
    (subgrupo 'Encargos sobre pró-labore') dos prestadores PJ que agora também
    vivem em desp_pessoal — senão a auditoria de INSS contaria os PJ."""
    return (matriz.get("subgrupos", {})
                  .get(group_key, {})
                  .get(sub_key, {})
                  .get(ym, 0))


def _sum_irpj_csll(matriz: dict, which: str, ym: str) -> float:
    """Soma valores que casam IRPJ ou CSLL no grupo impostos_lucro."""
    tot = 0.0
    for it in matriz.get("items", {}).get("impostos_lucro", {}).get("Impostos sobre lucro", []):
        if it["ym"] != ym: continue
        n = _norm(it["contato"])
        h = _norm(it["historico"])
        if which in n or re.search(rf"\b{which}\b", h):
            tot += float(it["valor"])
    return tot


def _calc_pago_tributo(matriz: dict, tributo: str, ym: str) -> float:
    """Versão consolidada de busca de pagamento por tributo (após a refatoração
    de _classify, cada tributo tem seu próprio grupo)."""
    if tributo == "ISS":
        return _sum_for(matriz, "deducoes_iss", ym)
    if tributo == "PIS":
        return _sum_for(matriz, "deducoes_pis", ym)
    if tributo == "COFINS":
        return _sum_for(matriz, "deducoes_cofins", ym)
    if tributo == "DCTFWEB":
        # DCTFWEB (INSS) vive no subgrupo "Encargos sobre pró-labore" de
        # desp_pessoal. Os prestadores PJ também estão em desp_pessoal, mas
        # em outro subgrupo — por isso somamos só o subgrupo, não o grupo.
        return _sum_subgroup(matriz, "desp_pessoal", "Encargos sobre pró-labore", ym)
    if tributo == "IRPJ":
        return _sum_irpj_csll(matriz, "IRPJ", ym)
    if tributo == "CSLL":
        return _sum_irpj_csll(matriz, "CSLL", ym)
    return 0.0


def _render_reconc_bling_totvs(matriz: dict, totvs: dict) -> str:
    """Bloco de reconciliação Bling × Totvs por mês."""
    months_real = [m for m in matriz["months"] if matriz["month_types"][m] == "real"]
    if not months_real:
        return ""
    p = []
    p.append('<div class="card" style="margin-bottom:14px"><div class="ct"><b>Reconciliação Receita — Bling × Totvs</b><span>NF Easy emitida (Bling) vs. comissão competência (Totvs ASE806)</span></div>')
    p.append('<div class="tbl-wrap"><table class="dt"><thead><tr><th>Mês NF</th><th class="r">Bling (NFs emitidas)</th><th>Compet. Totvs</th><th class="r">Totvs (Real)</th><th class="r">Δ</th><th>Status</th></tr></thead><tbody>')
    for m in months_real:
        bling_v = matriz["receita_por_mes"].get(m, 0)
        # Mês competência Totvs = mês anterior (Totvs cobre comissão da competência X, NF Easy é no mês X+1)
        y, mo = int(m[:4]), int(m[5:7])
        prev_m = f"{y:04d}-{mo-1:02d}" if mo > 1 else f"{y-1}-12"
        totvs_data = totvs.get(prev_m, {})
        totvs_real = (totvs_data.get("BASE", 0) + totvs_data.get("COMPLEMENTAR", 0)) or 0
        diff = bling_v - totvs_real
        if totvs_real == 0:
            status = "Sem comp. Totvs"; tone = "pa"
        elif abs(diff) / max(totvs_real, 1) < 0.05:
            status = "OK"; tone = "pg2"
        elif bling_v > totvs_real * 1.5:
            status = "NF inclui outros"; tone = "pa"
        else:
            status = "Verificar"; tone = "pr"
        p.append(f'<tr><td class="mono">{_fmt_comp(m)}</td><td class="r">{_fmt_brl(bling_v)}</td><td class="mono" style="color:var(--t3)">{_fmt_comp(prev_m)}</td><td class="r">{_fmt_brl(totvs_real)}</td><td class="r" style="color:{("var(--green)" if diff >= 0 else "var(--red)")}">{_fmt_brl(diff)}</td><td><span class="pill {tone}">{escape(status)}</span></td></tr>')
    p.append('</tbody></table></div></div>')
    return "".join(p)


def _render_drill_script(matriz: dict) -> str:
    """Script JS que carrega os items detalhados sob demanda."""
    items_json = json.dumps(matriz.get("items", {}), ensure_ascii=False, separators=(",", ":"))
    months_json = json.dumps(matriz["months"])
    return f"""<script>
var DRE_ITEMS={items_json};
var DRE_MONTHS={months_json};
function _dreFmt(v){{return v===0||v==null?'—':'R$\\u00a0'+Math.abs(v).toLocaleString('pt-BR',{{minimumFractionDigits:2,maximumFractionDigits:2}});}}
function _dreEsc(s){{return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function dreToggleRow(k){{
  var row=document.getElementById('dreDetailRow-'+k);
  var caret=document.getElementById('dreCaret-'+k);
  var body=document.getElementById('dreDetailBody-'+k);
  if(!row||!body) return;
  if(row.style.display==='none'){{
    if(!body.dataset.loaded){{
      var grupo=DRE_ITEMS[k]||{{}};
      var rows='';
      var totBySub={{}};
      for(var sub in grupo){{
        var list=grupo[sub];
        var sumSub=list.reduce(function(s,x){{return s+(x.valor||0);}},0);
        totBySub[sub]=sumSub;
        rows+='<tr style="background:var(--s3)"><td colspan="5" style="padding:6px 12px;font-family:var(--mono);font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.08em">'+_dreEsc(sub)+' · '+list.length+' lan\\u00e7amentos · '+_dreFmt(sumSub)+'</td></tr>';
        list.sort(function(a,b){{return b.valor-a.valor;}}).forEach(function(it){{
          var venc=it.venc||'';
          var dt=venc?venc.substring(8,10)+'/'+venc.substring(5,7):'—';
          var hist=it.historico?'<div style="font-size:10px;color:var(--t3);margin-top:1px">'+_dreEsc(it.historico.substring(0,80))+(it.historico.length>80?'\\u2026':'')+'</div>':'';
          var tipo=it.tipo==='projetado'?'<span class="pill pa" style="font-size:9px;padding:0 5px;margin-left:4px">prev</span>':'';
          rows+='<tr><td style="padding:4px 10px;font-family:var(--mono);font-size:10px;color:var(--t3)">'+dt+'</td><td style="padding:4px 10px;font-size:11px">'+_dreEsc(it.contato)+tipo+hist+'</td><td style="padding:4px 10px;font-size:10px;color:var(--t3)">'+(it.cat_bling?_dreEsc(it.cat_bling):'<span style="color:var(--amber);font-style:italic">sem categoria</span>')+'</td><td style="padding:4px 10px;font-family:var(--mono);font-size:10px;color:var(--t3)">'+_dreEsc(it.doc||'')+'</td><td class="r" style="padding:4px 10px;font-family:var(--mono);font-size:11px;white-space:nowrap">'+_dreFmt(it.valor)+'</td></tr>';
        }});
      }}
      body.innerHTML='<div style="padding:0"><table class="dt" style="margin:0;width:100%"><thead><tr><th style="width:50px">Venc.</th><th>Contato</th><th>Cat. Bling</th><th>Doc</th><th class="r">Valor</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
      body.dataset.loaded='1';
    }}
    row.style.display='table-row';
    if(caret) caret.textContent='\\u25BE';
  }} else {{
    row.style.display='none';
    if(caret) caret.textContent='\\u25B8';
  }}
}}
</script>"""


def _load_bling_csvs(bling_dir: Path) -> tuple:
    """Retorna (pagas, em_aberto, recebidas, receber_em_aberto) lidos dos CSVs mais recentes."""
    if not bling_dir.exists():
        return [], [], [], []
    def latest(prefix: str) -> Path | None:
        files = sorted(bling_dir.glob(f"{prefix}_*.csv"))
        return files[-1] if files else None

    def load(p: Path | None) -> list:
        if not p or not p.exists(): return []
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                return list(csv.DictReader(fh, delimiter=";"))
        except Exception:
            return []
    pagas = load(latest("contas_pagar_pagas"))
    em_aberto = load(latest("contas_pagar_em_aberto"))
    recebidas = load(latest("contas_receber_recebidas"))
    receber_em_aberto = load(latest("contas_receber_em_aberto"))

    # Reconciliação: remove fantasmas do a pagar (duplicatas já pagas +
    # provisões cobertas) antes de montar a matriz/DRE. Fonte única em
    # audit.py — evita dupla contagem na projeção e mantém DRE/P&L e a
    # auditoria alinhados com Visão Geral / Caixa.
    try:
        from audit import reconcile_em_aberto  # type: ignore
        em_aberto, _ = reconcile_em_aberto(pagas, em_aberto)
    except Exception:
        pass
    # Deduplicação do a receber (recebíveis idênticos sem documento) — mantém
    # DRE/P&L/auditoria alinhados com Caixa/Visão Geral (sem inflar receita).
    try:
        from audit import dedupe_receber  # type: ignore
        recebidas, _ = dedupe_receber(recebidas)
        receber_em_aberto, _ = dedupe_receber(receber_em_aberto)
    except Exception:
        pass
    return pagas, em_aberto, recebidas, receber_em_aberto


# ═══════════════════════════════════════════════════════════════════
# DRE 2-YEAR FULL RENDERER (jan/2025 – dez/2026)
# Inclui: 3 modos (anual, mensal, comparativo), 2 visões (Executivo,
# Operacional), KPIs comparativos, linhas de margem, sinalização de
# meses extrapolados/projetados.
# ═══════════════════════════════════════════════════════════════════

# Linhas % de margem (calculadas, não vêm dos dados)
# (key, label, tipo, {num_key, den_key, after_key})
MARGIN_LINES = [
    ("MARGEM_BRUTA",   "  → Margem bruta %",    "margin", {"num": "RECEITA_LIQUIDA", "den": "RECEITA_BRUTA", "after": "RECEITA_LIQUIDA"}),
    ("MARGEM_EBITDA",  "  → Margem EBITDA %",   "margin", {"num": "EBITDA",          "den": "RECEITA_BRUTA", "after": "EBITDA"}),
    ("MARGEM_LIQUIDA", "  → Margem líquida %",  "margin", {"num": "LUCRO_LIQUIDO",   "den": "RECEITA_BRUTA", "after": "LUCRO_LIQUIDO"}),
]


def _inject_margin_lines(struct_def: list) -> list:
    """Retorna struct com linhas % de margem inseridas após o subtotal correspondente."""
    out: list = []
    margins_by_after = {m[3]["after"]: m for m in MARGIN_LINES}
    for row in struct_def:
        out.append(row)
        if row[0] in margins_by_after:
            out.append(margins_by_after[row[0]])
    return out


def _sum_range(matriz: dict, struct_def: list, key: str, months: list[str], cache: dict) -> float:
    """Soma valor de uma linha (line/subtotal) em um intervalo de meses."""
    return sum(_resolve_subtotal(matriz, struct_def, key, m, cache) for m in months)


def _calc_margin(matriz: dict, struct_def: list, margin_key: str, months: list[str], cache: dict) -> float:
    """Calcula uma margem (em %) sobre um intervalo de meses."""
    meta = next((m[3] for m in MARGIN_LINES if m[0] == margin_key), None)
    if not meta:
        return 0.0
    num = _sum_range(matriz, struct_def, meta["num"], months, cache)
    den = _sum_range(matriz, struct_def, meta["den"], months, cache)
    if den == 0:
        return 0.0
    return num / den * 100


def _fmt_pct_signed(v: float) -> str:
    return f"{v:+.1f}%"


def _fmt_brl_compact_signed(v: float) -> str:
    """R$ 1,2M / R$ 800K / R$ 50 — com sinal explícito."""
    if v is None:
        return "—"
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    a = abs(v)
    if a >= 1_000_000:
        s = f"R$ {a/1_000_000:.2f}M".replace(".", ",")
    elif a >= 1_000:
        s = f"R$ {a/1_000:.0f}K"
    else:
        s = f"R$ {a:.0f}"
    return f"{sign}{s}" if sign else s


# ─── HELPERS PARA COLORIR CÉLULAS POR ORIGEM ───
# real < em_aberto < projetado < sem_dado. "sem_dado" é o pior porque não é
# uma estimativa ruim, é a ausência de informação — o subtotal que encosta
# nele precisa herdar isso. "extrapolado" ficou no mapa só por compatibilidade
# com matrizes serializadas antigas; o gap fill não gera mais esse kind.
_KIND_ORDER = {"real": 0, "totvs": 1, "em_aberto": 2, "extrapolado": 3, "projetado": 4, "sem_dado": 5}


def _cell_kind_for_line(matriz: dict, key: str, month: str) -> str:
    """Origem de uma célula simples (line) — fallback pro month_type se grupo sem registro."""
    ck = matriz.get("cell_kinds", {})
    if key in ck and month in ck[key]:
        return ck[key][month]
    return matriz.get("month_types", {}).get(month, "real")


def _cell_kind_for_subtotal(matriz: dict, struct_def: list, key: str, month: str) -> str:
    """Pior kind entre os componentes do subtotal (real < em_aberto < extrapolado < projetado)."""
    # Procura na struct
    for k, label, tipo, subs in struct_def:
        if k != key:
            continue
        if tipo in ("line", "line_alert"):
            return _cell_kind_for_line(matriz, key, month)
        if tipo in ("subtotal_pos", "subtotal_neg", "subtotal", "subtotal_calc"):
            worst = "real"
            for sk in (subs or []):
                if sk.startswith("-"):
                    sk = sk[1:]
                comp_kind = _cell_kind_for_subtotal(matriz, struct_def, sk, month)
                if _KIND_ORDER.get(comp_kind, 0) > _KIND_ORDER.get(worst, 0):
                    worst = comp_kind
            return worst
    return _cell_kind_for_line(matriz, key, month)


def _cell_style_for_kind(kind: str, base_color: str = "var(--t1)") -> tuple[str, str, str]:
    """Retorna (color, extra_style, bg) baseado na origem da célula.
    - real        → cor normal
    - em_aberto   → cor normal + bg azul sutil
    - sem_dado    → cinza, sem destaque (mês passado sem lançamento)
    - extrapolado → italic + cor secundária + bg amber sutil (legado)
    - projetado   → italic + cor secundária + bg amber um pouco mais forte
    """
    if kind == "real":
        return base_color, "", ""
    if kind == "totvs":
        # Receita real vinda do relatório Totvs (Bling sem lançamento no mês)
        return base_color, "", "background:rgba(60,170,150,0.08);"
    if kind == "em_aberto":
        return base_color, "", "background:rgba(70,130,200,0.05);"
    if kind == "sem_dado":
        # Nada de âmbar aqui: âmbar significa "estimado". Sem dado não é
        # estimativa, é buraco — tem que parecer buraco.
        return "var(--t3)", "", ""
    if kind == "extrapolado":
        return "var(--t3)", "font-style:italic;", "background:rgba(190,140,80,0.06);"
    # projetado
    return "var(--t3)", "font-style:italic;", "background:rgba(190,140,80,0.09);"


def _render_kpi_cards_2y(matriz: dict, struct_def: list) -> str:
    """Renderiza 4 cards comparativos 2025 vs 2026 com Δ."""
    cache: dict = {}
    months_25 = [m for m in matriz["months"] if m.startswith("2025")]
    months_26 = [m for m in matriz["months"] if m.startswith("2026")]

    # Indicadores: (chave, label, tipo)
    # tipo: "valor" (R$), "valor_margem" (R$ + %), "margem" (só %)
    kpis = [
        ("RECEITA_BRUTA",  "Receita bruta",   "valor_margem", "RECEITA_BRUTA"),
        ("EBITDA",         "EBITDA",          "valor_margem", "EBITDA"),
        ("LUCRO_LIQUIDO",  "Lucro líquido",   "valor_margem", "LUCRO_LIQUIDO"),
        ("MARGEM_BRUTA",   "Margem bruta",    "margem",       None),
    ]

    p = ['<div class="g4" style="margin-bottom:14px">']
    for kpi_key, label, kind, sub_for_margin in kpis:
        if kind == "margem":
            v25 = _calc_margin(matriz, struct_def, kpi_key, months_25, cache)
            v26 = _calc_margin(matriz, struct_def, kpi_key, months_26, cache)
            delta_pp = v26 - v25
            tone = "green" if delta_pp >= 0 else "amber"
            arrow = "▲" if delta_pp >= 0 else "▼"
            main_25 = f"{v25:.1f}%"
            main_26 = f"{v26:.1f}%"
            sub_26  = f"{arrow} {delta_pp:+.1f} pp vs 2025"
        else:
            v25 = _sum_range(matriz, struct_def, kpi_key, months_25, cache)
            v26 = _sum_range(matriz, struct_def, kpi_key, months_26, cache)
            delta_pct = ((v26 - v25) / v25 * 100) if v25 else 0
            tone = "green" if (v26 - v25) >= 0 and v26 >= 0 else "amber"
            arrow = "▲" if delta_pct >= 0 else "▼"
            main_25 = _fmt_brl(v25, compact=True)
            main_26 = _fmt_brl(v26, compact=True)
            if kind == "valor_margem":
                m25 = _calc_margin(matriz, struct_def,
                                   {"RECEITA_BRUTA": "MARGEM_BRUTA",
                                    "EBITDA": "MARGEM_EBITDA",
                                    "LUCRO_LIQUIDO": "MARGEM_LIQUIDA"}.get(kpi_key, "MARGEM_BRUTA"),
                                   months_25, cache)
                m26 = _calc_margin(matriz, struct_def,
                                   {"RECEITA_BRUTA": "MARGEM_BRUTA",
                                    "EBITDA": "MARGEM_EBITDA",
                                    "LUCRO_LIQUIDO": "MARGEM_LIQUIDA"}.get(kpi_key, "MARGEM_BRUTA"),
                                   months_26, cache)
                sub_26 = f"{arrow} {delta_pct:+.1f}% · margem {m26:.1f}% (vs {m25:.1f}%)"
            else:
                sub_26 = f"{arrow} {delta_pct:+.1f}% vs 2025"

        p.append(
            f'<div class="met {tone}">'
            f'<div class="ml">{escape(label)}</div>'
            f'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px">'
            f'<div class="mv {tone}">{main_26}</div>'
            f'<div style="font-size:11px;color:var(--t3);font-family:var(--mono)">2025: {main_25}</div>'
            f'</div>'
            f'<div class="ms">{sub_26}</div>'
            f'</div>'
        )
    p.append('</div>')
    return "".join(p)


def _render_matriz_2y(matriz: dict, struct_def_raw: list, mode: str, view_id: str, title: str) -> str:
    """
    Renderiza matriz 2025 vs 2026 em um dos 3 modos:
      mode='anual'  → Item | 2025 | 2026 | Δ R$ | Δ%
      mode='mensal' → Item | 12 cols 25 | T25 | 12 cols 26 | T26 | Δ R$ | Δ%
      mode='comp'   → Item | T25 | 12 cols 26 | T26 | Δ R$ | Δ%  (default)

    view_id usado pra dar id único aos elementos (exec, op).
    """
    struct_def = _inject_margin_lines(struct_def_raw)
    months_25 = [m for m in matriz["months"] if m.startswith("2025")]
    months_26 = [m for m in matriz["months"] if m.startswith("2026")]
    month_types = matriz["month_types"]
    cache: dict = {}

    # Decide quais colunas mostrar
    show_months_25 = mode == "mensal"
    show_months_26 = mode in ("mensal", "comp")
    show_t25 = True
    show_t26 = True
    show_delta = True

    # Legenda visual de origem
    legenda_html = (
        '<div style="display:flex;flex-wrap:wrap;gap:10px;font-size:10px;color:var(--t3);align-items:center;margin-top:4px">'
        '<span><span style="display:inline-block;width:10px;height:10px;background:transparent;border:1px solid var(--bd);vertical-align:middle;margin-right:4px"></span>Real (pago/recebido)</span>'
        '<span><span style="display:inline-block;width:10px;height:10px;background:rgba(70,130,200,0.18);border:1px solid var(--bd);vertical-align:middle;margin-right:4px"></span>Em aberto (comprometido)</span>'
        '<span><span style="display:inline-block;width:10px;height:10px;background:rgba(190,140,80,0.20);border:1px solid var(--bd);vertical-align:middle;margin-right:4px"></span><i>Projetado (média recorrente 2m + contratos, só meses futuros)</i></span>'
        '<span><span style="display:inline-block;width:10px;height:10px;background:rgba(60,170,150,0.25);border:1px solid var(--bd);vertical-align:middle;margin-right:4px"></span>Receita via relatório Totvs (mês sem lançamento no Bling)</span>'
        '<span style="opacity:.6"><span style="display:inline-block;width:10px;height:10px;background:transparent;border:1px dashed var(--bd2);vertical-align:middle;margin-right:4px"></span>Sem dados no Bling — não estimado, não somado</span>'
        '</div>'
    )

    # Cobertura por ano: se faltam meses, o total anual e o Δ% são parciais e
    # precisam dizer isso. Comparar um ano cheio com um ano pela metade sem
    # aviso foi exatamente o que produziu o falso "2025 vs 2026".
    def _sem_dado(ms: list[str]) -> list[str]:
        return [m for m in ms if month_types.get(m) == "sem_dado"]

    def _sem_despesa(ms: list[str]) -> list[str]:
        """
        Meses com receita mas ZERO despesa. Tecnicamente "real" (tem
        lançamento), na prática meio-mês: a janela do fetch trouxe o a receber
        e deixou o a pagar pra trás. Some no gap de mês vazio, mas envenena o
        total anual igual — precisa avisar separado.
        """
        gsrc = matriz.get("grupos", {})
        # `*_esperado` (PIS/COFINS) é derivado da receita, não é lançamento do
        # Bling. Se contar como despesa, um mês que só tem a receita parece um
        # mês completo e o aviso nunca dispara.
        desp_keys = [k for k in gsrc
                     if (k.startswith(("desp_", "deducoes_", "impostos_")) or k == "nao_recorrente")
                     and not k.endswith("_esperado")]
        out = []
        for m in ms:
            if month_types.get(m) == "sem_dado":
                continue
            if matriz.get("receita_por_mes", {}).get(m, 0) <= 0:
                continue
            if sum(gsrc.get(k, {}).get(m, 0) for k in desp_keys) == 0:
                out.append(m)
        return out

    gaps_25, gaps_26 = _sem_dado(months_25), _sem_dado(months_26)
    meio_25, meio_26 = _sem_despesa(months_25), _sem_despesa(months_26)
    aviso_html = ""
    if gaps_25 or gaps_26 or meio_25 or meio_26:
        partes = []
        if gaps_25:
            partes.append(f"2025 tem {len(gaps_25)} mês(es) sem lançamento no Bling")
        if gaps_26:
            partes.append(f"2026 tem {len(gaps_26)} mês(es) sem lançamento no Bling")
        if meio_25:
            partes.append(f"2025 tem {len(meio_25)} mês(es) com receita mas nenhuma despesa registrada")
        if meio_26:
            partes.append(f"2026 tem {len(meio_26)} mês(es) com receita mas nenhuma despesa registrada")
        aviso_html = (
            '<div style="margin-top:6px;padding:6px 9px;border-left:2px solid var(--amber);'
            'background:rgba(190,140,80,0.07);font-size:10px;color:var(--t2);line-height:1.5">'
            f'<b>Comparativo parcial.</b> {" · ".join(partes)}. '
            'Esses meses entram como zero no total anual e no Δ — não são estimados. '
            'Pra fechar o histórico, rode o workflow <span style="font-family:var(--mono)">update</span> '
            'com <span style="font-family:var(--mono)">janela_meses</span> maior.'
            '</div>'
        )

    p = []
    p.append(f'<div class="card" style="margin-bottom:14px"><div class="ct"><b>{escape(title)}</b><span>2025 vs 2026 · click na linha pra abrir lançamentos</span></div>')
    p.append(f'<div style="padding:0 14px 8px 14px">{legenda_html}{aviso_html}</div>')
    p.append('<div class="tbl-wrap"><table class="dt dre-mat"><thead><tr>')
    p.append('<th style="position:sticky;left:0;background:var(--s1);z-index:2;min-width:230px">Item</th>')

    def _header_style(m: str) -> str:
        mt = month_types.get(m, "real")
        if mt == "real":
            return "font-size:10px"
        if mt == "em_aberto":
            return "font-size:10px;color:var(--blue);background:rgba(70,130,200,0.06)"
        if mt == "sem_dado":
            return "font-size:10px;color:var(--t3);opacity:.55"
        # extrapolado / projetado
        return "font-size:10px;color:var(--amber);background:rgba(190,140,80,0.06);font-style:italic"

    def _header_label(m: str) -> str:
        """Mês sem dado leva marcação explícita — senão parece mês zerado."""
        lbl = _fmt_comp(m)
        if month_types.get(m) == "sem_dado":
            return f'{lbl}<br><span style="font-size:8px;letter-spacing:.04em">s/ dados</span>'
        return lbl

    # Headers
    if show_months_25:
        for m in months_25:
            p.append(f'<th class="r" style="{_header_style(m)}">{_header_label(m)}</th>')
    if show_t25:
        p.append('<th class="r" style="border-left:2px solid var(--bd2);background:var(--s2);font-weight:600">2025</th>')
    if show_months_26:
        for m in months_26:
            p.append(f'<th class="r" style="{_header_style(m)}">{_header_label(m)}</th>')
    if show_t26:
        p.append('<th class="r" style="border-left:2px solid var(--bd2);background:var(--s2);font-weight:600">2026</th>')
    if show_delta:
        p.append('<th class="r" style="border-left:2px solid var(--bd2);background:var(--blue-bg);color:var(--blue);font-weight:600">Δ R$</th>')
        p.append('<th class="r" style="background:var(--blue-bg);color:var(--blue);font-weight:600">Δ%</th>')
    p.append('</tr></thead><tbody>')

    def render_value_td(v: float, is_value: bool, color: str, bold: bool = False, big_left: bool = False, mono_size: int = 11) -> str:
        bd = "border-left:2px solid var(--bd2);" if big_left else ""
        weight = "font-weight:500;" if bold else ""
        bg = "background:var(--s2);" if big_left else ""
        if is_value:
            txt = _fmt_brl(v, True) if v else "—"
        else:
            txt = f"{v:.1f}%"
        return f'<td class="r" style="font-family:var(--mono);font-size:{mono_size}px;{weight}color:{color};padding:6px 8px;{bd}{bg}">{txt}</td>'

    for row in struct_def:
        k, label, tipo, subs = row[0], row[1], row[2], row[3]

        if tipo == "header":
            n_cols = 1
            if show_months_25: n_cols += len(months_25)
            if show_t25: n_cols += 1
            if show_months_26: n_cols += len(months_26)
            if show_t26: n_cols += 1
            if show_delta: n_cols += 2
            p.append(f'<tr style="background:var(--s3)"><td colspan="{n_cols}" style="padding:8px 10px;font-family:var(--mono);font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.08em;font-weight:500">{escape(label)}</td></tr>')
            continue

        if tipo == "margin":
            # Linha % calculada
            val_25 = _calc_margin(matriz, struct_def, k, months_25, cache)
            val_26 = _calc_margin(matriz, struct_def, k, months_26, cache)
            delta_pp = val_26 - val_25
            color = "var(--green)" if val_26 >= 0 else "var(--red)"
            p.append(f'<tr style="background:var(--s2)"><td style="position:sticky;left:0;background:var(--s2);z-index:1;font-size:11px;color:var(--t2);font-style:italic;padding:5px 10px">{escape(label)}</td>')
            if show_months_25:
                for m in months_25:
                    v = _calc_margin(matriz, struct_def, k, [m], cache)
                    mt = month_types.get(m, "real")
                    cbg = "background:rgba(190,140,80,0.06);" if mt in ("extrapolado", "projetado") else ("background:rgba(70,130,200,0.04);" if mt == "em_aberto" else "")
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t3);font-style:italic;{cbg}">{v:.0f}%</td>')
            if show_t25:
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:{color};font-weight:500;border-left:2px solid var(--bd2);background:var(--s2);font-style:italic">{val_25:.1f}%</td>')
            if show_months_26:
                for m in months_26:
                    v = _calc_margin(matriz, struct_def, k, [m], cache)
                    mt = month_types.get(m, "real")
                    cbg = "background:rgba(190,140,80,0.06);" if mt in ("extrapolado", "projetado") else ("background:rgba(70,130,200,0.04);" if mt == "em_aberto" else "")
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t3);font-style:italic;{cbg}">{v:.0f}%</td>')
            if show_t26:
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:{color};font-weight:500;border-left:2px solid var(--bd2);background:var(--s2);font-style:italic">{val_26:.1f}%</td>')
            if show_delta:
                delta_color = "var(--green)" if delta_pp >= 0 else "var(--red)"
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:{delta_color};border-left:2px solid var(--bd2);background:var(--blue-bg);font-style:italic">{delta_pp:+.1f} pp</td>')
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t3);background:var(--blue-bg);font-style:italic">—</td>')
            p.append('</tr>')
            continue

        # subtotal_* ou line
        is_sub = tipo in ("subtotal_pos", "subtotal_neg", "subtotal", "subtotal_calc")
        is_alert = tipo == "line_alert"

        if is_sub:
            val_25 = _resolve_subtotal(matriz, struct_def, k, "2025-12", cache) if False else _sum_range(matriz, struct_def, k, months_25, cache)
            val_26 = _sum_range(matriz, struct_def, k, months_26, cache)
            color = "var(--green)" if val_26 >= 0 and tipo == "subtotal_pos" else ("var(--red)" if tipo == "subtotal_neg" else ("var(--green)" if val_26 >= 0 else "var(--red)"))
            bg = "var(--s2)" if tipo == "subtotal_calc" else "transparent"
            bd = "2px solid var(--bd2)" if tipo == "subtotal_calc" else "1px solid var(--bd)"
            p.append(f'<tr style="background:{bg};border-top:{bd}"><td style="position:sticky;left:0;background:{bg};z-index:1;font-weight:500;font-size:12px;padding:8px 10px">{escape(label)}</td>')
            if show_months_25:
                for m in months_25:
                    v = _resolve_subtotal(matriz, struct_def, k, m, cache)
                    kind = _cell_kind_for_subtotal(matriz, struct_def, k, m)
                    cc, extra, cbg = _cell_style_for_kind(kind, base_color=color)
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;font-weight:500;color:{cc};{extra}{cbg}padding:6px 6px">{_fmt_brl(v, True) if v else "—"}</td>')
            if show_t25:
                p.append(render_value_td(val_25, True, color, bold=True, big_left=True))
            if show_months_26:
                for m in months_26:
                    v = _resolve_subtotal(matriz, struct_def, k, m, cache)
                    kind = _cell_kind_for_subtotal(matriz, struct_def, k, m)
                    cc, extra, cbg = _cell_style_for_kind(kind, base_color=color)
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;font-weight:500;color:{cc};{extra}{cbg}padding:6px 6px">{_fmt_brl(v, True) if v else "—"}</td>')
            if show_t26:
                p.append(render_value_td(val_26, True, color, bold=True, big_left=True))
            if show_delta:
                delta_abs = val_26 - val_25
                delta_pct = (delta_abs / val_25 * 100) if val_25 else 0
                # interpretação de "bom/ruim" depende do tipo
                if tipo == "subtotal_neg":
                    delta_color = "var(--green)" if delta_abs <= 0 else "var(--red)"
                else:
                    delta_color = "var(--green)" if delta_abs >= 0 else "var(--red)"
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:{delta_color};font-weight:500;border-left:2px solid var(--bd2);background:var(--blue-bg)">{_fmt_brl_compact_signed(delta_abs)}</td>')
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:{delta_color};background:var(--blue-bg);font-weight:500">{delta_pct:+.1f}%</td>')
            p.append('</tr>')

        elif tipo in ("line", "line_alert"):
            val_25 = sum(_line_value(matriz, k, m) for m in months_25)
            val_26 = sum(_line_value(matriz, k, m) for m in months_26)
            color = "var(--red)" if is_alert else "var(--t1)"
            color_label = "var(--red)" if is_alert else "var(--t2)"
            has_items = (k in matriz.get("items", {}))
            row_attrs = f'onclick="dreToggleRow(\'{k}\')"' if has_items else ''
            cursor = "cursor:pointer" if has_items else ""
            caret = f'<span id="dreCaret-{k}" style="color:var(--t3);font-family:var(--mono);font-size:10px;margin-right:5px;user-select:none">▸</span>' if has_items else '<span style="margin-right:14px"></span>'
            alert_icon = '<span style="margin-left:6px" title="Não pago — verificar">⚠</span>' if is_alert and val_26 > 0 else ''
            p.append(f'<tr class="dre-row" {row_attrs} style="{cursor}"><td style="position:sticky;left:0;background:var(--s1);z-index:1;padding:6px 10px;font-size:11.5px;color:{color_label}">{caret}{escape(label)}{alert_icon}</td>')
            if show_months_25:
                for m in months_25:
                    v = _line_value(matriz, k, m)
                    base = color if is_alert else "var(--t1)"
                    kind = _cell_kind_for_line(matriz, k, m)
                    cc, extra, cbg = _cell_style_for_kind(kind, base_color=base)
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:{cc};{extra}{cbg}padding:5px 6px">{_fmt_brl(v, True) if v else "—"}</td>')
            if show_t25:
                cm = color if is_alert else "var(--t1)"
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;font-weight:500;color:{cm};border-left:2px solid var(--bd2);background:var(--s2);padding:6px 8px">{_fmt_brl(val_25, True) if val_25 else "—"}</td>')
            if show_months_26:
                for m in months_26:
                    v = _line_value(matriz, k, m)
                    base = color if is_alert else "var(--t1)"
                    kind = _cell_kind_for_line(matriz, k, m)
                    cc, extra, cbg = _cell_style_for_kind(kind, base_color=base)
                    p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:{cc};{extra}{cbg}padding:5px 6px">{_fmt_brl(v, True) if v else "—"}</td>')
            if show_t26:
                cm = color if is_alert else "var(--t1)"
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;font-weight:500;color:{cm};border-left:2px solid var(--bd2);background:var(--s2);padding:6px 8px">{_fmt_brl(val_26, True) if val_26 else "—"}</td>')
            if show_delta:
                delta_abs = val_26 - val_25
                delta_pct = (delta_abs / val_25 * 100) if val_25 else 0
                # Para "line" não sabemos se é positivo/negativo semanticamente — usa cinza neutro
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:11px;color:var(--t2);border-left:2px solid var(--bd2);background:var(--blue-bg)">{_fmt_brl_compact_signed(delta_abs)}</td>')
                p.append(f'<td class="r" style="font-family:var(--mono);font-size:10px;color:var(--t2);background:var(--blue-bg)">{delta_pct:+.1f}%</td>')
            p.append('</tr>')
            if has_items:
                n_cols_total = 1 + (len(months_25) if show_months_25 else 0) + (1 if show_t25 else 0) + (len(months_26) if show_months_26 else 0) + (1 if show_t26 else 0) + (2 if show_delta else 0)
                p.append(f'<tr class="dre-detail" id="dreDetailRow-{k}" style="display:none;background:var(--s2)"><td colspan="{n_cols_total}" id="dreDetailBody-{k}" style="padding:0"></td></tr>')

    p.append('</tbody></table></div></div>')
    return "".join(p)


def _render_dre_full(matriz: dict, totvs: dict) -> str:
    """Orquestra a aba DRE v2: KPIs + toggle Exec/Op + toggle modo × 6 matrizes pré-renderizadas."""
    # Pré-renderiza 6 matrizes (2 visões × 3 modos)
    matrices = {}
    for view_id, struct_def, label in [("exec", PL_EXEC, "P&L Executivo"),
                                       ("op",   DRE_CONTABIL, "DRE Operacional")]:
        for mode in ("anual", "comp", "mensal"):
            matrices[(view_id, mode)] = _render_matriz_2y(matriz, struct_def, mode, view_id, f"{label} — {mode}")

    # KPI cards (usa DRE_CONTABIL pra ter todos os subtotais)
    kpi_html = _render_kpi_cards_2y(matriz, _inject_margin_lines(DRE_CONTABIL))

    # Toggles
    toggles_html = '''
<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
  <div style="display:inline-flex;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:3px;gap:2px">
    <button class="dre-view active" data-view="op"   onclick="dreView('op')"   style="padding:6px 18px;font-size:12px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:var(--s1);color:var(--t1);font-family:var(--sans);box-shadow:0 1px 2px rgba(60,90,130,0.08)">Operacional</button>
    <button class="dre-view"        data-view="exec" onclick="dreView('exec')" style="padding:6px 18px;font-size:12px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans)">Executivo</button>
  </div>
  <div style="display:inline-flex;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:3px;gap:2px">
    <button class="dre-mode active" data-mode="comp"   onclick="dreMode('comp')"   style="padding:6px 14px;font-size:11px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:var(--s1);color:var(--t1);font-family:var(--sans);box-shadow:0 1px 2px rgba(60,90,130,0.08)">Comparativo</button>
    <button class="dre-mode"        data-mode="anual"  onclick="dreMode('anual')"  style="padding:6px 14px;font-size:11px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans)">Anual</button>
    <button class="dre-mode"        data-mode="mensal" onclick="dreMode('mensal')" style="padding:6px 14px;font-size:11px;font-weight:500;cursor:pointer;border-radius:6px;border:0;background:none;color:var(--t2);font-family:var(--sans)">Mensal (24m)</button>
  </div>
  <span style="font-size:10.5px;color:var(--t3);font-family:var(--mono);text-transform:uppercase;letter-spacing:.06em">Visão · Modo</span>
</div>'''

    # Divs com cada matriz pré-renderizada
    matrices_html = []
    for (view_id, mode), html in matrices.items():
        is_default = (view_id == "op" and mode == "comp")
        style = "" if is_default else "display:none"
        matrices_html.append(f'<div class="dre-matrix" data-view="{view_id}" data-mode="{mode}" style="{style}">{html}</div>')

    js_toggles = '''
<script>
function dreView(v){
  document.querySelectorAll('.dre-view').forEach(function(b){
    var on = b.dataset.view === v;
    b.style.background = on ? 'var(--s1)' : 'none';
    b.style.color      = on ? 'var(--t1)' : 'var(--t2)';
    b.style.boxShadow  = on ? '0 1px 2px rgba(60,90,130,0.08)' : 'none';
    b.classList.toggle('active', on);
  });
  _dreApplyVisibility();
}
function dreMode(m){
  document.querySelectorAll('.dre-mode').forEach(function(b){
    var on = b.dataset.mode === m;
    b.style.background = on ? 'var(--s1)' : 'none';
    b.style.color      = on ? 'var(--t1)' : 'var(--t2)';
    b.style.boxShadow  = on ? '0 1px 2px rgba(60,90,130,0.08)' : 'none';
    b.classList.toggle('active', on);
  });
  _dreApplyVisibility();
}
function _dreApplyVisibility(){
  var v = document.querySelector('.dre-view.active').dataset.view;
  var m = document.querySelector('.dre-mode.active').dataset.mode;
  document.querySelectorAll('.dre-matrix').forEach(function(d){
    d.style.display = (d.dataset.view === v && d.dataset.mode === m) ? 'block' : 'none';
  });
}
</script>'''

    return kpi_html + toggles_html + "".join(matrices_html) + js_toggles


# ═══════════ CASCATA (WATERFALL) DO RESULTADO ═══════════
# Gráfico em cascata Receita bruta → Lucro líquido, 100% derivado da
# matriz_2y (zero hardcode). Duas variantes: "ytd" (jan → mês corrente,
# real + em aberto) e "full" (ano cheio incl. projetado). Clique na barra
# abre duas tabelas: composição por fornecedor/cliente e mês a mês.

_CASC_DED_KEYS = ["deducoes_iss", "deducoes_pis", "deducoes_cofins",
                  "deducoes_pis_esperado", "deducoes_cofins_esperado"]
_CASC_DED_LABELS = {
    "deducoes_iss": "ISS", "deducoes_pis": "PIS (pago)",
    "deducoes_cofins": "COFINS (pago)",
    "deducoes_pis_esperado": "PIS (esperado – 0,65%)",
    "deducoes_cofins_esperado": "COFINS (esperado – 3%)",
}


def _casc_forn_rows(matriz: dict, keys: list[str], mset: set,
                    sign: float = 1.0, top: int = 14) -> list:
    """Agrega items por contato dentro dos meses. [[nome, valor, n], ...]"""
    agg: dict[str, list] = {}
    for k in keys:
        for sub, lst in matriz["items"].get(k, {}).items():
            for it in lst:
                if it.get("ym") in mset:
                    nome = (it.get("contato") or sub or "—").strip() or "—"
                    a = agg.setdefault(nome, [0.0, 0])
                    a[0] += it.get("valor", 0.0) or 0.0
                    a[1] += 1
    rows = sorted(([n, round(sign * v, 2), c] for n, (v, c) in agg.items()),
                  key=lambda r: -abs(r[1]))
    if len(rows) > top:
        resto = rows[top:]
        rows = rows[:top] + [[f"Outros ({len(resto)})",
                              round(sum(r[1] for r in resto), 2),
                              sum(r[2] for r in resto)]]
    return rows


def _casc_mes_vals(matriz: dict, keys: list[str], months: list[str],
                   sign: float = 1.0) -> dict[str, float]:
    g = matriz["grupos"]
    return {m: sign * sum(g.get(k, {}).get(m, 0.0) for k in keys)
            for m in months}


def _casc_mes_rows(matriz: dict, vals: dict[str, float],
                   months: list[str]) -> list:
    mt = matriz["month_types"]
    return [[m, round(vals.get(m, 0.0), 2), mt.get(m, "")] for m in months]


def _casc_variant(matriz: dict, months: list[str], include_fin: bool) -> dict:
    mset = set(months)
    g = matriz["grupos"]

    def gsum(keys: list[str]) -> float:
        return sum(g.get(k, {}).get(m, 0.0) for k in keys for m in months)

    rec_keys = ["receita_servicos", "outras_receitas"]
    rb = gsum(rec_keys)
    ded = gsum(_CASC_DED_KEYS)
    rl = rb - ded
    pes = gsum(["desp_pessoal"])
    adm = gsum(["desp_admin"])
    ebitda = rl - pes - adm
    fin = gsum(["financ_receita"]) - gsum(["financ_despesa"])
    imp = gsum(["impostos_lucro"])
    ll = ebitda + (fin if include_fin else 0.0) - imp

    m_rb = _casc_mes_vals(matriz, rec_keys, months)
    m_ded = _casc_mes_vals(matriz, _CASC_DED_KEYS, months, sign=-1.0)
    m_pes = _casc_mes_vals(matriz, ["desp_pessoal"], months, sign=-1.0)
    m_adm = _casc_mes_vals(matriz, ["desp_admin"], months, sign=-1.0)
    m_imp = _casc_mes_vals(matriz, ["impostos_lucro"], months, sign=-1.0)
    m_finr = _casc_mes_vals(matriz, ["financ_receita"], months)
    m_find = _casc_mes_vals(matriz, ["financ_despesa"], months, sign=-1.0)
    m_fin = {m: m_finr[m] + m_find[m] for m in months}
    m_rl = {m: m_rb[m] + m_ded[m] for m in months}
    m_ebt = {m: m_rl[m] + m_pes[m] + m_adm[m] for m in months}
    m_ll = {m: m_ebt[m] + (m_fin[m] if include_fin else 0.0) + m_imp[m]
            for m in months}

    def mes(vals):
        return _casc_mes_rows(matriz, vals, months)

    # Deduções: composição por tributo (não por contato — o "quem" é o fisco)
    ded_comp = [[_CASC_DED_LABELS[k], round(-sum(g.get(k, {}).get(m, 0.0)
                for m in months), 2), None] for k in _CASC_DED_KEYS
                if abs(sum(g.get(k, {}).get(m, 0.0) for m in months)) >= 0.005]

    fin_comp = (_casc_forn_rows(matriz, ["financ_receita"], mset)
                + _casc_forn_rows(matriz, ["financ_despesa"], mset, sign=-1.0))
    fin_comp.sort(key=lambda r: -abs(r[1]))

    steps = [
        {"k": "rb", "label": "Receita bruta", "t": "pos", "v": round(rb, 2),
         "comp": _casc_forn_rows(matriz, rec_keys, mset), "mes": mes(m_rb)},
        {"k": "ded", "label": "Deduções", "t": "neg", "v": round(-ded, 2),
         "comp": ded_comp, "mes": mes(m_ded)},
        {"k": "rl", "label": "= Receita líquida", "t": "sub", "v": round(rl, 2),
         "comp": [["Receita bruta", round(rb, 2), None],
                  ["(−) Deduções", round(-ded, 2), None]], "mes": mes(m_rl)},
        {"k": "pes", "label": "Pessoal", "t": "neg", "v": round(-pes, 2),
         "comp": _casc_forn_rows(matriz, ["desp_pessoal"], mset, sign=-1.0),
         "mes": mes(m_pes)},
        {"k": "adm", "label": "Administrativas", "t": "neg", "v": round(-adm, 2),
         "comp": _casc_forn_rows(matriz, ["desp_admin"], mset, sign=-1.0),
         "mes": mes(m_adm)},
        {"k": "ebt", "label": "= EBITDA", "t": "sub", "v": round(ebitda, 2),
         "comp": [["Receita líquida", round(rl, 2), None],
                  ["(−) Pessoal", round(-pes, 2), None],
                  ["(−) Administrativas", round(-adm, 2), None]],
         "mes": mes(m_ebt)},
    ]
    if include_fin:
        steps.append({"k": "fin", "label": "Financeiro", "t": "delta",
                      "v": round(fin, 2), "comp": fin_comp, "mes": mes(m_fin)})
    steps.append({"k": "imp", "label": "IRPJ + CSLL", "t": "neg",
                  "v": round(-imp, 2),
                  "comp": _casc_forn_rows(matriz, ["impostos_lucro"], mset,
                                          sign=-1.0),
                  "mes": mes(m_imp)})
    ll_comp = [["EBITDA", round(ebitda, 2), None]]
    if include_fin:
        ll_comp.append(["(±) Financeiro", round(fin, 2), None])
    ll_comp.append(["(−) IRPJ + CSLL", round(-imp, 2), None])
    steps.append({"k": "ll", "label": "= Lucro líquido", "t": "sub",
                  "v": round(ll, 2), "comp": ll_comp, "mes": mes(m_ll)})
    return {"steps": steps}


def _render_cascata(matriz: dict, today: date) -> str:
    """Card com o waterfall do ano corrente + drill de duas tabelas."""
    year = today.year
    cutoff = today.strftime("%Y-%m")
    yprefix = f"{year:04d}-"
    months_full = [m for m in matriz["months"] if m.startswith(yprefix)]
    months_ytd = [m for m in months_full if m <= cutoff]
    if not months_ytd:
        months_ytd = months_full[:1]

    g = matriz["grupos"]
    fin_total = max(
        abs(sum(g.get("financ_receita", {}).get(m, 0.0) for m in months_full)
            - sum(g.get("financ_despesa", {}).get(m, 0.0) for m in months_full)),
        abs(sum(g.get("financ_receita", {}).get(m, 0.0) for m in months_ytd)
            - sum(g.get("financ_despesa", {}).get(m, 0.0) for m in months_ytd)))
    include_fin = fin_total >= 1.0  # mesmo nº de barras nas 2 variantes

    casc = {
        "ytd": _casc_variant(matriz, months_ytd, include_fin),
        "full": _casc_variant(matriz, months_full, include_fin),
    }
    casc_json = json.dumps(casc, ensure_ascii=False,
                           separators=(",", ":")).replace("</", "<\\/")

    # Nota de itens estruturais (fora da cascata, como no DRE operacional)
    nr = sum(g.get("nao_recorrente", {}).get(m, 0.0) for m in months_full)
    ap = sum(g.get("aporte_socio", {}).get(m, 0.0) for m in months_full)
    partes = []
    if nr >= 1:
        partes.append(f"buy-out/acordo {_fmt_brl(nr, compact=True)}")
    if ap >= 1:
        partes.append(f"aportes {_fmt_brl(ap, compact=True)}")
    nota_estrut = ("itens estruturais fora da cascata (" + " · ".join(partes)
                   + " no ano) — ver matriz") if partes else ""

    lbl_ytd = f"Realizado (jan–{_fmt_comp(months_ytd[-1])[:3]})"
    lbl_full = "Ano cheio (c/ projetado)"

    btn_on = ("padding:6px 16px;font-size:11.5px;font-weight:500;cursor:pointer;"
              "border-radius:6px;border:0;background:var(--s1);color:var(--t1);"
              "font-family:var(--sans);box-shadow:0 1px 2px rgba(60,90,130,0.08)")
    btn_off = ("padding:6px 16px;font-size:11.5px;font-weight:500;cursor:pointer;"
               "border-radius:6px;border:0;background:none;color:var(--t2);"
               "font-family:var(--sans)")

    html_part = f"""
<div class="sl">Cascata do resultado — {year}</div>
<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>DRE em cascata — {year}</b><span class="ct-tag">toque numa barra para abrir a composição</span></div>
  <div style="display:flex;gap:14px;flex-wrap:wrap;margin:2px 0 10px;align-items:center">
    <div style="display:inline-flex;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:3px;gap:2px">
      <button class="casc-per active" data-per="ytd" onclick="cascPer('ytd')" style="{btn_on}">{lbl_ytd}</button>
      <button class="casc-per" data-per="full" onclick="cascPer('full')" style="{btn_off}">{lbl_full}</button>
    </div>
    <span style="font-size:10.5px;color:var(--t3);font-family:var(--mono);text-transform:uppercase;letter-spacing:.05em">R$ mil · barras neutras = subtotais{(" · " + nota_estrut) if nota_estrut else ""}</span>
  </div>
  <div class="cw" style="height:320px"><canvas id="cascChart"></canvas></div>
</div>
<div id="cascDrill" style="display:none"></div>
"""

    js = """
<script>
var CASC=__CASC_JSON__;
var cascVar='ytd',cascSel=-1,cascChartObj=null;
function _cascCss(n,fb){try{if(typeof EA_VAR==='function')return EA_VAR(n,fb);var v=getComputedStyle(document.documentElement).getPropertyValue(n).trim();return v||fb;}catch(e){return fb;}}
function cascFmtK(v){var k=v/1000,a=Math.abs(k);var s=a>=100?Math.round(a):Math.round(a*10)/10;return (v<0?'-':'')+s.toLocaleString('pt-BR');}
function cascFmtR(v){return (v<0?'\\u2212':'')+'R$\\u00a0'+Math.abs(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});}
function cascEsc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function cascMesLbl(ym){var M=['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];return M[parseInt(ym.substring(5,7),10)-1]+'/'+ym.substring(2,4);}
function cascBuild(){
  if(typeof Chart==='undefined')return;
  var el=document.getElementById('cascChart');if(!el)return;
  var d=CASC[cascVar];if(!d)return;
  var bars=[],cums=[],cum=0;
  d.steps.forEach(function(s){
    if(s.t==='sub'){bars.push([Math.min(0,s.v),Math.max(0,s.v)]);cum=s.v;}
    else{var a=cum,b=cum+s.v;bars.push([Math.min(a,b),Math.max(a,b)]);cum=b;}
    cums.push(cum);
  });
  var neu=_cascCss('--t3','#8a97a8'),red=_cascCss('--red','#c0392b'),grn=_cascCss('--green','#1e7e4f'),t1=_cascCss('--t1','#16202c'),bd=_cascCss('--bd','rgba(60,90,130,0.12)'),t2=_cascCss('--t2','#4a5f7a');
  var cols=d.steps.map(function(s){return s.t==='sub'?neu:(s.v<0?red:grn);});
  if(cascChartObj){cascChartObj.destroy();cascChartObj=null;}
  cascChartObj=new Chart(el,{type:'bar',
    data:{labels:d.steps.map(function(s){return s.label;}),
      datasets:[{data:bars,backgroundColor:cols,borderRadius:5,borderSkipped:false,barPercentage:0.72,categoryPercentage:0.92}]},
    options:{responsive:true,maintainAspectRatio:false,layout:{padding:{top:22,bottom:4}},
      onClick:function(ev,els){if(els&&els.length)cascDrill(els[0].index);},
      onHover:function(ev,els){if(ev.native&&ev.native.target)ev.native.target.style.cursor=els.length?'pointer':'default';},
      plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){var s=d.steps[c.dataIndex];return cascFmtR(s.v);}}}},
      scales:{x:{grid:{display:false},ticks:{color:t2,font:{size:10},maxRotation:0,autoSkip:false}},
              y:{grid:{color:bd},ticks:{color:t2,callback:function(v){return cascFmtK(v);}}}}},
    plugins:[{id:'cascLbl',afterDatasetsDraw:function(ch){
      var c=ch.ctx,meta=ch.getDatasetMeta(0),ys=ch.scales.y;
      c.save();
      c.strokeStyle=t2;c.setLineDash([3,3]);c.lineWidth=1;c.globalAlpha=0.45;
      for(var i=0;i+1<meta.data.length;i++){
        var yPx=ys.getPixelForValue(cums[i]);
        var a=meta.data[i],b=meta.data[i+1];
        c.beginPath();c.moveTo(a.x+a.width/2,yPx);c.lineTo(b.x-b.width/2,yPx);c.stroke();
      }
      c.setLineDash([]);c.globalAlpha=1;
      c.font='600 11px '+_cascCss('--mono','monospace');c.fillStyle=t1;c.textAlign='center';
      meta.data.forEach(function(elb,i){
        var s=d.steps[i],lo=bars[i][0],hi=bars[i][1];
        var up=(s.t==='sub'?s.v>=0:s.v>=0);
        var y=up?ys.getPixelForValue(hi)-7:ys.getPixelForValue(lo)+15;
        c.fillText(cascFmtK(s.v),elb.x,y);
      });
      c.restore();
    }}]});
}
function cascPer(p){
  cascVar=p;
  document.querySelectorAll('.casc-per').forEach(function(b){
    var on=b.dataset.per===p;
    b.style.background=on?'var(--s1)':'none';
    b.style.color=on?'var(--t1)':'var(--t2)';
    b.style.boxShadow=on?'0 1px 2px rgba(60,90,130,0.08)':'none';
    b.classList.toggle('active',on);
  });
  cascBuild();
  if(cascSel>=0)cascDrill(cascSel);
}
function cascDrill(i){
  var d=CASC[cascVar];if(!d||!d.steps[i])return;
  cascSel=i;
  var s=d.steps[i],box=document.getElementById('cascDrill');
  var compHdr=(s.k==='ded')?'Tributo':(s.t==='sub'?'Componente':'Fornecedor / cliente');
  var comp='';
  (s.comp||[]).forEach(function(r){
    var n=(r[2]!=null)?' <span style="color:var(--t3);font-size:10px">\\u00b7 '+r[2]+' lan\\u00e7.</span>':'';
    comp+='<tr><td style="padding:5px 10px;font-size:11.5px">'+cascEsc(r[0])+n+'</td><td class="r" style="padding:5px 10px;font-family:var(--mono);font-size:11px;white-space:nowrap;color:'+(r[1]<0?'var(--red)':'var(--t1)')+'">'+cascFmtR(r[1])+'</td></tr>';
  });
  if(!comp)comp='<tr><td colspan="2" style="padding:8px 10px;color:var(--t3);font-size:11px">Sem lan\\u00e7amentos no per\\u00edodo.</td></tr>';
  var mes='';
  (s.mes||[]).forEach(function(r){
    var tag='';
    if(r[2]==='em_aberto')tag=' <span class="pill pa" style="font-size:9px;padding:0 5px">aberto</span>';
    else if(r[2]==='projetado')tag=' <span class="pill pa" style="font-size:9px;padding:0 5px">prev</span>';
    else if(r[2]==='sem_dado')tag=' <span style="color:var(--t3);font-size:9px">s/ dado</span>';
    var val=(r[2]==='sem_dado'&&Math.abs(r[1])<0.005)?'\\u2014':cascFmtR(r[1]);
    mes+='<tr><td style="padding:5px 10px;font-family:var(--mono);font-size:11px;white-space:nowrap">'+cascMesLbl(r[0])+tag+'</td><td class="r" style="padding:5px 10px;font-family:var(--mono);font-size:11px;white-space:nowrap;color:'+(r[1]<0?'var(--red)':'var(--t1)')+'">'+val+'</td></tr>';
  });
  box.innerHTML='<div class="g2" style="margin-bottom:12px">'
    +'<div class="card"><div class="ct"><b>Composi\\u00e7\\u00e3o \\u2014 '+cascEsc(s.label)+'</b><span class="ct-tag">'+cascFmtR(s.v)+'</span></div>'
    +'<div class="tbl-wrap" style="max-height:340px;overflow:auto"><table class="dt"><thead><tr><th>'+compHdr+'</th><th class="r">Valor</th></tr></thead><tbody>'+comp+'</tbody></table></div></div>'
    +'<div class="card"><div class="ct"><b>M\\u00eas a m\\u00eas \\u2014 '+cascEsc(s.label)+'</b><span class="ct-tag">sazonalidade</span></div>'
    +'<div class="tbl-wrap" style="max-height:340px;overflow:auto"><table class="dt"><thead><tr><th>M\\u00eas</th><th class="r">Valor</th></tr></thead><tbody>'+mes+'</tbody></table></div></div>'
    +'</div>';
  box.style.display='block';
}
(function(){
  function init(){cascBuild();}
  if(document.readyState==='complete')init();
  else window.addEventListener('load',init);
  try{new MutationObserver(function(){if(cascChartObj)cascBuild();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});}catch(e){}
})();
</script>
"""
    return html_part + js.replace("__CASC_JSON__", casc_json)



# ============ API PÚBLICA ============
def render_pl_and_dre(
    bling_dir: Path,
    totvs_snap: Path,
    today: date | None = None,
    months_window: int = 6,
) -> dict[str, Any]:
    """
    Gera fragments HTML para as abas P&L Executivo e DRE Contábil.
    Retorna dict com chaves:
      pl_ntab, pl_mobtab, pl_pg, dre_pg_replacement (substitui pg-dre existente)
    """
    today = today or date.today()
    pagas, em_aberto, recebidas, receber_em_aberto = _load_bling_csvs(bling_dir)
    totvs = _load_totvs_por_mes(Path(totvs_snap))
    matriz = _build_matriz(pagas, recebidas, em_aberto, receber_em_aberto, today, months_window)
    # Matriz estendida 2025-01 → 2026-12 com gap fill (usada na aba DRE v2).
    # totvs_por_mes preenche receita de meses em que o Bling está mudo
    # (jan-ago/2025) — ver receita_sintetica_por_mes.
    matriz_2y = _build_matriz_2y(pagas, recebidas, em_aberto, receber_em_aberto, today,
                                 totvs_por_mes=totvs)

    proj_ate = _fmt_comp(matriz["months"][-1])
    drill_script    = _render_drill_script(matriz)
    drill_script_2y = _render_drill_script(matriz_2y)

    # P&L Executivo
    pl_pg = f"""<!-- ═══ P&L EXECUTIVO ═══ -->
<div class="pg" id="pg-pl">
<div class="hero">
  <div>
    <div class="htitle">P&L Executivo<br><span style="font-size:14px;color:var(--t2);font-weight:400">Visão gerencial · {months_window} meses + projeção até {proj_ate}</span></div>
    <div class="hsub">Categorização gerencial por subgrupo · drill para lançamentos · alertas tributários</div>
  </div>
  <div class="pills">
    <span class="pill pg2"><span class="pdot"></span>Receita Bling</span>
    <span class="pill pa"><span class="pdot"></span>Itens estruturais separados</span>
  </div>
</div>

{_render_reconc_bling_totvs(matriz, totvs)}

{_render_matriz(matriz, PL_EXEC, "P&L Executivo — Matriz Categorias × Meses", with_alert=False)}

{_render_impostos_check(matriz, totvs)}

{drill_script}
</div>"""

    # DRE — visão completa 2025 vs 2026 (24 meses jan/25 a dez/26 com gap fill)
    # Dois toggles: Visão (Executivo|Operacional) × Modo (Anual|Comparativo|Mensal)
    # KPIs comparativos no topo, linhas de margem dentro da matriz.
    dre_pg = f"""<!-- ═══ DRE CONTÁBIL ═══ -->
<div class="pg" id="pg-dre">
<div class="hero">
  <div>
    <div class="htitle">DRE — 2025 vs 2026<br><span style="font-size:14px;color:var(--t2);font-weight:400">Comparativo anual com projeção · Lucro Presumido</span></div>
    <div class="hsub">jan/2025 → dez/2026 · meses futuros projetados em âmbar · mês passado sem dado fica em branco (não é estimado) · margens calculadas (bruta · EBITDA · líquida)</div>
  </div>
  <div class="pills">
    <span class="pill pg2"><span class="pdot"></span>Real (Bling)</span>
    <span class="pill pa"><span class="pdot"></span>Extrapolado / Projetado</span>
    <span class="pill pb2"><span class="pdot"></span>Lucro Presumido</span>
  </div>
</div>

{_render_cascata(matriz_2y, today)}

{_render_dre_full(matriz_2y, totvs)}

<div class="sl" style="margin-top:18px">Apoio · meses reais (legado)</div>
{_render_impostos_check(matriz, totvs)}

<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Receita vs Despesas — mês a mês</b><span>Real opaco · projetado translúcido</span></div>
  <div class="cw" style="height:300px"><canvas id="dreBar"></canvas></div>
  <div id="dreLegend" style="margin-top:10px;display:flex;flex-wrap:wrap;gap:12px;font-size:10.5px;color:var(--t2)"></div>
</div>

<div class="card" style="margin-bottom:12px">
  <div class="ct"><b>Resultado mensal — Receita − Despesas</b></div>
  <div class="cw" style="height:180px"><canvas id="dreResult"></canvas></div>
</div>

<div class="card">
  <div class="ct"><b>Detalhamento mensal</b></div>
  <div class="tbl-wrap">
    <table class="dt">
      <thead><tr>
        <th>Mês</th><th>Tipo</th>
        <th class="r">Receita</th><th class="r">Despesas</th>
        <th class="r">Resultado</th><th class="r">Acumulado</th>
      </tr></thead>
      <tbody id="dreTbody"></tbody>
    </table>
  </div>
  <div class="totals" id="dreTotals" style="margin-top:10px"></div>
</div>

<div id="dreKpis" style="display:none"></div>
<div id="dreHsub" style="display:none"></div>

{drill_script_2y}
</div>"""

    return {
        "pl_ntab":   '<button class="ntab" onclick="sp(\'pl\',this)">P&L</button>',
        "pl_mobtab": '<button class="mobtab" onclick="sp(\'pl\',this,1)">P&L</button>',
        "pl_pg": pl_pg,
        "dre_pg": dre_pg,
        "n_lancamentos_classificados": sum(len(lst) for g in matriz.get("items", {}).values() for lst in g.values()),
    }


if __name__ == "__main__":
    import sys
    home = Path.home()
    bling = home / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais" / "bling-api"
    snap = bling / "totvs_snapshot.json"
    r = render_pl_and_dre(bling, snap)
    print(f"[ok] n_lancamentos={r['n_lancamentos_classificados']}", file=sys.stderr)
    print(r["pl_pg"][:2000])
