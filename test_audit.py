#!/usr/bin/env python3
"""Self-tests do audit.py.

Roda com: `python3 test_audit.py`  (sem dependências externas).
Cobre os 3 bugs corrigidos na rodada 2026-06-01:
  1. ISS TOMADOR sendo somado como ISS sobre vendas
  2. Competência sendo inferida pelo mês de pagamento (regra N+1) em vez
     de extraída do histórico do Bling
  3. IRPJ/CSLL trimestral capturando só 1 quota em vez de até 3

Cada teste é um caso isolado; falhas printam o caso quebrado.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402


def test_competencia_do_historico() -> None:
    cases = [
        ("ISS S/ FATURAMENTO - 01/2026", None, "2026-01"),
        ("ISS PRESTADOR - 02/2026",      None, "2026-02"),
        ("ISS - 04/2026",                None, "2026-04"),
        ("PIS REFERENTE 03/2026",        None, "2026-03"),
        ("COFINS DEZEMBRO 2025",         "2025-12", "2025-12"),  # fallback
        ("algo sem mes",                 "2026-05", "2026-05"),  # fallback
        (None,                           "2026-05", "2026-05"),
        ("NF 12/345 sem competencia",    "2026-09", "2026-09"),
        ("2026-03 retrocompat",          None, "2026-03"),       # YYYY-MM
    ]
    for hist, fb, esp in cases:
        got = audit._competencia_do_historico(hist, fb)
        assert got == esp, f"_competencia_do_historico({hist!r}, {fb!r}) = {got!r}, esperado {esp!r}"


def test_trimestre_do_historico() -> None:
    cases = [
        ("IRPJ - 1º TRIMETRE/2026",                  "2026-T1"),
        ("CSLL 4° TRIMESTRE 2025 - 3º Quota",        "2025-T4"),
        ("IRPJ 4° TRIMESTRE 2025",                   "2025-T4"),
        ("IRPJ - LUCRO PRESUMIDO - 1º TRIMETRE/2026", "2026-T1"),
        ("IRPJ - DEMAIS - 2 TRIMESTRE/2025",         "2025-T2"),
        ("CSLL 3 TRIMESTRE 2026",                    "2026-T3"),
        ("IRPJ sem trimestre",                       None),
        ("",                                         None),
        (None,                                       None),
    ]
    for hist, esp in cases:
        got = audit._trimestre_do_historico(hist)
        assert got == esp, f"_trimestre_do_historico({hist!r}) = {got!r}, esperado {esp!r}"


def test_is_iss_tomador() -> None:
    cases = [
        ({"historico": "ISS TOMADOR - 02/2026"},          True),
        ({"historico": "ISS S/ FATURAMENTO - TOMADOR"},    True),
        ({"historico": "ISS PRESTADOR - 02/2026"},        False),
        ({"historico": "ISS S/ FATURAMENTO - 01/2026"},   False),
        ({"historico": ""},                                False),
        ({},                                               False),
    ]
    for it, esp in cases:
        got = audit._is_iss_tomador(it)
        assert got == esp, f"_is_iss_tomador({it!r}) = {got!r}, esperado {esp!r}"


def test_agregar_pago_iss_por_competencia() -> None:
    """Bugs 1 + 2 combinados: ISS de jan/26 pago em dez/25, ISS PRESTADOR +
    TOMADOR em fev/26 (tomador deve ser ignorado), ISS abr/26 pago em jun/26."""
    matriz = {"items": {"deducoes_iss": {"Impostos sobre vendas": [
        {"ym": "2025-12", "valor": 2793.88, "historico": "ISS S/ FATURAMENTO - 01/2026"},
        {"ym": "2026-03", "valor": 1538.91, "historico": "ISS PRESTADOR - 02/2026"},
        {"ym": "2026-03", "valor":  103.20, "historico": "ISS TOMADOR - 02/2026"},
        {"ym": "2026-06", "valor": 1365.50, "historico": "ISS - 04/2026"},
        # caso de fallback: sem historico → competência = ym - 1 mês
        {"ym": "2026-04", "valor":  500.00, "historico": ""},
    ]}}}

    com_filtro = audit._agregar_pago_por_competencia(
        matriz, "deducoes_iss", excluir_tomador=True)
    assert com_filtro.get("2026-01") == 2793.88, com_filtro
    assert com_filtro.get("2026-02") == 1538.91, com_filtro  # tomador excluído
    assert com_filtro.get("2026-04") == 1365.50, com_filtro
    assert com_filtro.get("2026-03") == 500.00, com_filtro   # fallback ym-1
    assert "2026-05" not in com_filtro, com_filtro  # não deve atribuir a mai/26
    assert "2025-12" not in com_filtro, com_filtro  # competência é jan/26

    sem_filtro = audit._agregar_pago_por_competencia(
        matriz, "deducoes_iss", excluir_tomador=False)
    assert abs(sem_filtro.get("2026-02", 0) - (1538.91 + 103.20)) < 0.01


def test_agregar_pago_lucro_por_trimestre() -> None:
    """Bug 3: 3 quotas de T4/25 + 2 quotas de T1/26 (uma sem histórico,
    inferida pelo mês de pagamento)."""
    matriz = {"items": {"impostos_lucro": {"Impostos sobre lucro": [
        {"valor": 3400.38, "contato": "IRPJ",
         "historico": "IRPJ 4° TRIMESTRE 2025", "ym": "2026-01"},
        {"valor": 3366.74, "contato": "IRPJ",
         "historico": "IRPJ 4° TRIMESTRE 2025", "ym": "2026-02"},
        {"valor": 3366.72, "contato": "IRPJ",
         "historico": "IRPJ 4° TRIMESTRE 2025 - 3º Quota", "ym": "2026-03"},
        {"valor": 4113.88, "contato": "IRPJ",
         "historico": "IRPJ - LUCRO PRESUMIDO - 1º TRIMETRE/2026",
         "ym": "2026-04"},
        {"valor": 1768.32, "contato": "CSLL",
         "historico": "CSLL - DEMAIS - 1º TRIMETRE/2026", "ym": "2026-04"},
        # sem histórico → fallback pelo mês de pagamento (mai/26 = quota 2 de T1/26)
        {"valor": 4155.00, "contato": "IRPJ", "historico": "", "ym": "2026-05"},
    ]}}}

    irpj_p, irpj_q = audit._agregar_pago_lucro_por_trimestre(matriz, "IRPJ")
    assert abs(irpj_p.get("2025-T4", 0)
               - (3400.38 + 3366.74 + 3366.72)) < 0.01
    assert irpj_q.get("2025-T4") == 3
    assert abs(irpj_p.get("2026-T1", 0) - (4113.88 + 4155.00)) < 0.01
    assert irpj_q.get("2026-T1") == 2

    csll_p, csll_q = audit._agregar_pago_lucro_por_trimestre(matriz, "CSLL")
    assert abs(csll_p.get("2026-T1", 0) - 1768.32) < 0.01
    assert csll_q.get("2026-T1") == 1
    # CSLL não deve ter T4/25 (todos os lançamentos T4/25 são IRPJ)
    assert csll_p.get("2025-T4", 0) == 0


def test_aliquotas_config() -> None:
    """Garante que ninguém troque por engano as alíquotas-chave."""
    cfg = audit.CONFIG
    assert cfg["iss_sbc_aliq"]            == 0.02
    assert cfg["pis_aliq"]                == 0.0065
    assert cfg["cofins_aliq"]             == 0.03
    assert cfg["irpj_base_presumida"]     == 0.32
    assert cfg["irpj_aliq"]               == 0.15
    assert cfg["irpj_adicional_aliq"]     == 0.10
    assert cfg["irpj_adicional_limite_trim"] == 60_000.0
    assert cfg["csll_base_presumida"]     == 0.32
    assert cfg["csll_aliq"]               == 0.09       # <- 9%, NÃO 15%
    assert cfg["ret_irrf_pj_aliq"]        == 0.015
    assert cfg["ret_pis_cofins_csll_aliq"] == 0.0465

    # Carga efetiva total
    carga = (cfg["iss_sbc_aliq"]
             + cfg["pis_aliq"]
             + cfg["cofins_aliq"]
             + cfg["irpj_base_presumida"] * cfg["irpj_aliq"]
             + cfg["csll_base_presumida"] * cfg["csll_aliq"])
    assert abs(carga - 0.1333) < 1e-4, f"carga total {carga:.4%} != 13,33%"


def test_reconcile_duplicata_paga() -> None:
    """Lançamento em aberto idêntico a um já pago é removido (DUPLICATA_PAGA)."""
    pagas = [
        {"contato_id": "1", "contato_nome": "ROMULO", "vencimento": "2026-05-25",
         "valor": "59.814", "historico": "PAGTO QUOTAS - PARC 17"},
    ]
    em_aberto = [
        # gêmeo exato do pago acima → deve sair
        {"contato_id": "1", "contato_nome": "ROMULO", "vencimento": "2026-05-25",
         "valor": "59.814", "historico": "PAGTO QUOTAS - PARC 17"},
        # parcela 18, futura, legítima → deve ficar
        {"contato_id": "1", "contato_nome": "ROMULO", "vencimento": "2026-06-25",
         "valor": "59.814", "historico": "PAGTO QUOTAS - PARC 18"},
    ]
    limpo, aj = audit.reconcile_em_aberto(pagas, em_aberto)
    assert len(limpo) == 1, f"esperado 1 restante, veio {len(limpo)}"
    assert limpo[0]["vencimento"] == "2026-06-25"
    assert len(aj) == 1 and aj[0]["tipo"] == "DUPLICATA_PAGA"
    assert round(aj[0]["valor"], 2) == 59814.0


def test_reconcile_provisao_coberta() -> None:
    """Provisão genérica coberta por NF paga no mês de venc → removida."""
    pagas = [
        {"contato_id": "9", "contato_nome": "EFATA", "vencimento": "2026-05-20",
         "valor": "17.500", "historico": "Ref. a NF nº 000024"},
        {"contato_id": "9", "contato_nome": "EFATA", "vencimento": "2026-05-20",
         "valor": "17.500", "historico": "Ref. a NF nº 000026"},
    ]
    em_aberto = [
        {"contato_id": "9", "contato_nome": "EFATA", "vencimento": "2026-05-20",
         "valor": "35.000", "historico": "PRESTAÇÃO SERVIÇOS XX/2026"},
    ]
    limpo, aj = audit.reconcile_em_aberto(pagas, em_aberto)
    assert limpo == [], "provisão coberta deveria ter sido removida"
    assert len(aj) == 1 and aj[0]["tipo"] == "PROVISAO_COBERTA"


def test_reconcile_provisao_abatida_parcial() -> None:
    """Cobertura parcial (< 90%): abate o realizado, mantém só o restante.

    Caso real Efata jul/26: NF paga de R$ 22.167 + provisão de R$ 35.000 no
    mesmo mês somavam R$ 57K de 'despesa' para um custo real de R$ 35K.
    """
    pagas = [
        {"contato_id": "9", "contato_nome": "EFATA", "vencimento": "2026-07-20",
         "valor": "22.167,00", "historico": "Ref. a NF nº 000028"},
    ]
    em_aberto = [
        {"contato_id": "9", "contato_nome": "EFATA", "vencimento": "2026-07-20",
         "valor": "35.000,00", "historico": "PRESTAÇÃO SERVIÇOS XX/2026"},
    ]
    limpo, aj = audit.reconcile_em_aberto(pagas, em_aberto)
    assert len(aj) == 1 and aj[0]["tipo"] == "PROVISAO_ABATIDA", aj
    assert abs(aj[0]["restante"] - 12833.0) < 0.01, aj[0]
    assert len(limpo) == 1
    assert abs(audit._recon_money(limpo[0]["valor"]) - 12833.0) < 0.01, limpo[0]
    # idempotência: segunda passada sobre o resultado não abate de novo
    limpo2, aj2 = audit.reconcile_em_aberto(pagas, limpo)
    assert aj2 == [] and len(limpo2) == 1, (aj2, limpo2)
    assert abs(audit._recon_money(limpo2[0]["valor"]) - 12833.0) < 0.01


def test_recon_money_formato_americano() -> None:
    """Guarda contra decimal americano: '1234.56' não pode virar 123456."""
    assert audit._recon_money("1234.56") == 1234.56
    assert audit._recon_money("-99.9") == -99.9
    # formatos BR continuam intactos
    assert audit._recon_money("1.234,56") == 1234.56
    assert audit._recon_money("59.814,26") == 59814.26
    assert audit._recon_money("35.000") == 35000.0   # milhar sem centavos
    assert audit._recon_money(120.5) == 120.5


def test_reconcile_preserva_legitimo() -> None:
    """Vencido real (sem gêmeo pago, sem provisão coberta) é preservado."""
    pagas = [
        {"contato_id": "5", "contato_nome": "EFATA", "vencimento": "2026-05-20",
         "valor": "17.500", "historico": "Ref. a NF nº 000024"},
    ]
    em_aberto = [
        # cartão: não é gêmeo de pago, não casa padrão provisão → fica
        {"contato_id": "7", "contato_nome": "CARTAO DE CREDITO", "vencimento": "2026-05-15",
         "valor": "10.558", "historico": "CARTÃO DE CREDITO - DÉBITO AUTOMAT"},
        # provisão SEM cobertura (nenhum pago a esse contato no mês) → fica
        {"contato_id": "8", "contato_nome": "RECEITA FEDERAL", "vencimento": "2026-05-20",
         "valor": "80", "historico": "DCTFWEB PREVISÃO 12/2025"},
    ]
    limpo, aj = audit.reconcile_em_aberto(pagas, em_aberto)
    assert len(limpo) == 2, f"nada deveria ser removido, sobraram {len(limpo)}"
    assert aj == []


def test_reconcile_idempotente() -> None:
    """Rodar a reconciliação sobre o resultado já limpo não remove nada a mais."""
    pagas = [
        {"contato_id": "1", "contato_nome": "X", "vencimento": "2026-05-25",
         "valor": "100,00", "historico": "h"},
    ]
    em_aberto = [
        {"contato_id": "1", "contato_nome": "X", "vencimento": "2026-05-25",
         "valor": "100,00", "historico": "h"},
        {"contato_id": "1", "contato_nome": "X", "vencimento": "2026-07-25",
         "valor": "100,00", "historico": "h"},
    ]
    limpo1, aj1 = audit.reconcile_em_aberto(pagas, em_aberto)
    limpo2, aj2 = audit.reconcile_em_aberto(pagas, limpo1)
    assert len(aj1) == 1 and aj2 == [], "segunda passada não deve remover nada"
    assert limpo1 == limpo2


def test_dedupe_receber_remove_identicas() -> None:
    """Recebíveis idênticos (mesmo contato/valor/venc) SEM documento → colapsa p/ 1."""
    rows = [
        {"contato_id": "9", "contato_nome": "ATACADAO", "vencimento": "2026-05-15",
         "valor": "7.038,75", "numero_documento": ""},
        {"contato_id": "9", "contato_nome": "ATACADAO", "vencimento": "2026-05-15",
         "valor": "7.038,75", "numero_documento": ""},
        {"contato_id": "3", "contato_nome": "OUTRO", "vencimento": "2026-05-16",
         "valor": "100,00", "numero_documento": ""},
    ]
    limpo, aj = audit.dedupe_receber(rows)
    assert len(limpo) == 2, f"deveria sobrar 2, sobraram {len(limpo)}"
    assert len(aj) == 1 and round(aj[0]["valor"], 2) == 7038.75


def test_dedupe_receber_preserva_com_documento() -> None:
    """Mesmo valor/venc mas com documentos distintos → parcelas reais, preserva."""
    rows = [
        {"contato_id": "9", "contato_nome": "X", "vencimento": "2026-05-15",
         "valor": "500,00", "numero_documento": "NF-1"},
        {"contato_id": "9", "contato_nome": "X", "vencimento": "2026-05-15",
         "valor": "500,00", "numero_documento": "NF-2"},
    ]
    limpo, aj = audit.dedupe_receber(rows)
    assert len(limpo) == 2 and aj == [], "com documento distinto não pode deduplicar"


def test_dedupe_receber_idempotente() -> None:
    rows = [
        {"contato_id": "9", "contato_nome": "A", "vencimento": "2026-05-15",
         "valor": "10,00", "numero_documento": ""},
        {"contato_id": "9", "contato_nome": "A", "vencimento": "2026-05-15",
         "valor": "10,00", "numero_documento": ""},
    ]
    limpo1, aj1 = audit.dedupe_receber(rows)
    limpo2, aj2 = audit.dedupe_receber(limpo1)
    assert len(aj1) == 1 and aj2 == [] and limpo1 == limpo2


TESTS = [
    test_competencia_do_historico,
    test_trimestre_do_historico,
    test_is_iss_tomador,
    test_agregar_pago_iss_por_competencia,
    test_agregar_pago_lucro_por_trimestre,
    test_aliquotas_config,
    test_reconcile_duplicata_paga,
    test_reconcile_provisao_coberta,
    test_reconcile_provisao_abatida_parcial,
    test_recon_money_formato_americano,
    test_reconcile_preserva_legitimo,
    test_reconcile_idempotente,
    test_dedupe_receber_remove_identicas,
    test_dedupe_receber_preserva_com_documento,
    test_dedupe_receber_idempotente,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print()
    if failed:
        print(f"FAILED — {failed} de {len(TESTS)} testes quebraram")
        return 1
    print(f"OK — {len(TESTS)} testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
