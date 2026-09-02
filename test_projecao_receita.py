#!/usr/bin/env python3
"""Self-tests da projeção de receita recorrente (dre_render).

Regras aprovadas pelo Leandro em 02/09/2026:
  - Meses futuros: receita = max(média recorrente dos últimos 2 meses
    reais, recorrente já faturado) + pontuais faturados + contratos
    projetados (ex.: TOTVS 50K/mês set-dez/26).
  - "Pontual" = customização ~R$ 12,5K (single ou soma de parcelas do
    mesmo cliente/valor). Fora da média. Clientes recorrentes_sempre
    nunca caem na regra de valor.
  - Anti-dupla-contagem: contrato já lançado no Bling no mês NÃO é
    projetado de novo; recorrente faturado entra via max(), não soma.

Roda com: `python3 test_projecao_receita.py` (sem dependências externas).
Plugado nos gates (weekly.sh e .github/workflows/update.yml).
"""
from __future__ import annotations

import sys
from datetime import date

import dre_render as dr

CFG = {
    "media_meses": 2,
    "pontual_valor_alvo": 12500.0,
    "pontual_tolerancia_pct": 15,
    "recorrentes_sempre": ["TOTVS", "1000 MARCAS"],
    "pontuais_sempre": [],
    "contratos_projetados": [
        {"contato": "TOTVS S.A.", "valor": 50000.0, "de": "2026-09", "ate": "2026-12"},
    ],
}


def _rec(venc: str, contato: str, valor: str) -> dict:
    return {"vencimento": venc, "contato_nome": contato, "valor": valor,
            "historico": "", "numero_documento": "", "categoria_descricao": ""}


def test_classes_pontual_por_valor() -> None:
    rows = [
        _rec("2026-06-10", "CLIENTE X", "12.400,00"),      # ~12,5K → pontual
        _rec("2026-06-10", "TOTVS S.A. SANTA CATARINA", "12.785,57"),  # recorrente_sempre
        _rec("2026-06-10", "CLIENTE Y", "3.000,00"),        # normal → recorrente
    ]
    cls = dr._receita_classes(rows, CFG)
    assert cls == ["pontual", "recorrente", "recorrente"], cls


def test_classes_pontual_parcelado() -> None:
    # 2 parcelas de 7.038,75 = 14.077,50 (dentro de ±15% de 12,5K) → pontual
    rows = [
        _rec("2026-05-22", "ATACADAO LTDA", "7.038,75"),
        _rec("2026-06-22", "ATACADAO LTDA", "7.038,75"),
    ]
    assert dr._receita_classes(rows, CFG) == ["pontual", "pontual"]


def test_classes_contrato() -> None:
    rows = [_rec("2026-09-15", "TOTVS S.A.", "50.000,00")]
    assert dr._receita_classes(rows, CFG) == ["contrato"]


def test_media_recorrente_exclui_pontual_e_contrato() -> None:
    recebidas = [
        _rec("2026-07-16", "CLIENTE A", "60.000,00"),
        _rec("2026-07-20", "CLIENTE PONTUAL", "12.500,00"),   # fora da média
        _rec("2026-08-16", "CLIENTE A", "70.000,00"),
        _rec("2026-08-20", "TOTVS S.A.", "50.000,00"),        # contrato: fora
        _rec("2026-05-16", "CLIENTE A", "999.999,00"),        # fora da janela 2m
    ]
    media, meses = dr._media_recorrente(recebidas, CFG, "2026-09")
    assert meses == ["2026-07", "2026-08"], meses
    assert abs(media - 65000.0) < 0.01, media


def test_projecao_mes_sem_faturamento() -> None:
    # Out/26: nada faturado → média + contrato 50K
    partes = dr._projecao_receita_mes("2026-10", [], 65000.0, ["2026-07", "2026-08"], CFG)
    assert abs(sum(v for _, v in partes) - 115000.0) < 0.01, partes
    assert any("Contrato" in lb for lb, _ in partes)


def test_projecao_anti_dupla_contagem() -> None:
    # Set/26: contrato 50K JÁ lançado + 4K recorrente faturado.
    billed = [
        (_rec("2026-09-15", "TOTVS S.A.", "50.000,00"), "contrato"),
        (_rec("2026-09-25", "ATLANTIS", "4.000,00"), "recorrente"),
    ]
    partes = dr._projecao_receita_mes("2026-09", billed, 65000.0, ["2026-07", "2026-08"], CFG)
    # complemento recorrente = 65.000 - 4.000 = 61.000; contrato NÃO reprojetado
    assert abs(sum(v for _, v in partes) - 61000.0) < 0.01, partes
    assert not any("Contrato" in lb for lb, _ in partes)


def test_projecao_recorrente_faturado_acima_da_media() -> None:
    # Recorrente faturado (80K) > média (65K) → max() vence, sem complemento
    billed = [(_rec("2026-10-10", "CLIENTE A", "80.000,00"), "recorrente")]
    partes = dr._projecao_receita_mes("2026-10", billed, 65000.0, ["2026-07", "2026-08"], CFG)
    assert abs(sum(v for _, v in partes) - 50000.0) < 0.01, partes  # só o contrato


def test_matriz_2y_projeta_set_dez() -> None:
    today = date(2026, 9, 2)
    recebidas = [
        _rec("2026-07-16", "CLIENTE A", "60.000,00"),
        _rec("2026-08-16", "CLIENTE A", "70.000,00"),
    ]
    receber = [_rec("2026-09-15", "TOTVS S.A.", "50.000,00")]
    pagas = [_rec("2026-08-05", "FORNECEDOR Z", "10.000,00")]
    em_aberto = [_rec("2026-11-05", "FORNECEDOR Z", "10.000,00")]
    m2 = dr._build_matriz_2y(pagas, recebidas, em_aberto, receber, today)
    cfg = dr._load_receitas_cfg()
    media, _ = dr._media_recorrente(recebidas, cfg, "2026-09")
    for ym in ("2026-09", "2026-10", "2026-11", "2026-12"):
        rec = m2["receita_por_mes"].get(ym, 0)
        assert rec > 0, f"{ym} sem receita projetada"
        assert m2["receita_kinds"].get(ym) == "projetado", (ym, m2["receita_kinds"].get(ym))
        # consistência grupo vs receita_por_mes
        g = m2["grupos"]["receita_servicos"].get(ym, 0)
        assert abs(g - rec) < 0.02, (ym, g, rec)


def test_matriz_pl_janela_ate_dezembro() -> None:
    today = date(2026, 9, 2)
    m1 = dr._build_matriz([], [_rec("2026-08-16", "CLIENTE A", "70.000,00")], [], [], today)
    assert m1["months"][-1] == "2026-12", m1["months"]
    # dezembro tem receita projetada (média = ago = 70K, sem pontual/contrato lançado)
    assert m1["receita_por_mes"].get("2026-12", 0) > 0


TESTS = [
    test_classes_pontual_por_valor,
    test_classes_pontual_parcelado,
    test_classes_contrato,
    test_media_recorrente_exclui_pontual_e_contrato,
    test_projecao_mes_sem_faturamento,
    test_projecao_anti_dupla_contagem,
    test_projecao_recorrente_faturado_acima_da_media,
    test_matriz_2y_projeta_set_dez,
    test_matriz_pl_janela_ate_dezembro,
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
        except Exception as e:
            failed += 1
            print(f"  ERRO  {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"FAILED — {failed} de {len(TESTS)} testes quebraram")
        return 1
    print(f"OK — {len(TESTS)} testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
