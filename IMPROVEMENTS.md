# IMPROVEMENTS.md — Recomendações acumuladas (revisão humana)

> Arquivo mantido **automaticamente** pelo agente auditor (`ci/ci_auditor.py`). Lista recomendações que apareceram em **2 ou mais** auditorias nos últimos **7 dias**, para realimentar o agente principal. Não editar à mão — será sobrescrito.

_Última atualização: 2026-08-22 12:59 BRT · base: 29 relatório(s) de auditoria na janela._

## Recomendações recorrentes

- 🔴 **(4x)** Verificar validade do token Bling (refresh/rotação) e disponibilidade da API v3.
  - _checks:_ `fetch_bling`
- 🔴 **(4x)** Conferir permissão contents:write e conflito de push.
  - _checks:_ `push_pages`
- 🔴 **(3x)** Falha recorrente em 'fetch' — priorizar correção de causa-raiz.
  - _checks:_ `falha_recorrente`
- 🔴 **(3x)** Falha recorrente em 'push' — priorizar correção de causa-raiz.
  - _checks:_ `falha_recorrente`
