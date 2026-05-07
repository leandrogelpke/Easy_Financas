# Contexto para continuar na próxima conversa Cowork

## Estado atual (final da sessão de 07/05/2026)

### O que está pronto e rodando
- **OAuth2 Bling v3 funcionando** — app "Easy Dashboard Financeiro" (id 333584) cadastrado com 5 escopos: Caixas e Bancos, Categorias Receitas e Despesas, Clientes e Fornecedores, Contas a Pagar, Contas a Receber.
- **`fetch-bling.py`** — baixa 4 datasets do Bling com retomada automática via cache `.cache/`. Validado: 97 contas a pagar em aberto, 112 pagas (filtradas desde out/2025), 20 a receber em aberto, 91 recebidas, 48 contatos, 63 categorias.
- **`build-html.py`** — gera `bling-live.html` (~20KB) com 4 KPIs, buckets de vencimento, tabelas de pagar/receber, top categorias e top clientes. Estilo idêntico ao `index.html`.
- **`bling-auth.py`** — fluxo inicial OAuth2 (sobe localhost:8080, abre browser, captura code, troca por tokens).
- **`RUNBOOK.md`** novo — versão 2.0 do runbook com fluxo automatizado de 3 comandos.
- **Credenciais salvas** em `.bling-oauth.json` (client_id + secret) e `.bling-tokens.json` (access + refresh token, refresh válido ~30d).
- **`.gitignore`** atualizado com `.bling-oauth.json` e `.bling-tokens.json`.

### O que falta — não-bloqueante

**1. `setup-token.sh` da PAT do GitHub** ainda pendente — rodar 1x se quiser que `git push` funcione direto do sandbox Cowork sem abrir Terminal nativo.

**2. Refactor do `index.html`** — ainda tem dados hardcoded (CX_DATA contas a receber, ROWS gastos) que poderiam ser substituídos pelos dados Bling. Decisão atual: manter `index.html` para Pipeline + Projeção (informação não-Bling) e usar `bling-live.html` para a parte financeira realtime.

**3. Categorização no Bling** — R$693K dos pagamentos estão "(sem categoria)". Classificar no Bling pra alimentar o DRE corretamente.

**4. TOTVS aparece como 3 contatos** (filiais com CNPJs diferentes). Pode consolidar no Bling ou aceitar duplicidade.

## Como rodar o ciclo semanal

```
python3 ~/Documents/Easy_Financas/fetch-bling.py --data-inicial 2025-10-01 --no-detail-receber
python3 ~/Documents/Easy_Financas/build-html.py
cd ~/Documents/Easy_Financas && git pull && git add . && git commit -m "weekly snapshot $(date +%F)" && git push
```

Em sessão Cowork, basta dizer: **"rode o runbook semanal"** — ver `RUNBOOK.md` para detalhes.

## Próximos passos sugeridos (não obrigatórios)

Em ordem de valor:
1. Rodar runbook 1x ao vivo numa quinta-feira pra validar fluxo end-to-end
2. Se `bling-live.html` ficou útil, decidir se substitui as abas Overview/Cashflow do `index.html` (refactor seletivo)
3. Webhook do Bling pra atualização em tempo real (em vez de pull semanal)
4. Adicionar gráfico Chart.js no `bling-live.html` para evolução mensal de receita/despesa

## Histórico de versões do dashboard

- **v0** (~02/05/2026): runbook manual, 4 downloads UI Bling, edição manual do index.html
- **v1** (07/05/2026 manhã): RUNBOOK-FIXES.md identificando 3 fricções do v0
- **v2** (07/05/2026 tarde): API OAuth2 + fetch-bling.py + build-html.py + bling-live.html ← AGORA
