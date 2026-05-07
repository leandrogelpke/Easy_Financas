# Correções ao runbook semanal — Easy Analytics

Documento gerado em 02/05/2026 22:38 após executar a tarefa pela primeira vez e identificar 3 fricções.

## Fix 1 — Acesso a `~/Downloads` desde o início

**Problema:** O sandbox do Cowork não tinha acesso a `~/Downloads`, então os comandos `mv ~/Downloads/...` da tarefa falhavam até eu pedir acesso no meio.

**Correção no runbook (PRÉ-REQUISITOS):**

```
✅ O usuário já concedeu acesso às pastas:
   - ~/Documents/Easy_Financas
   - ~/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais
   - ~/Downloads          ← ADICIONAR
```

Na próxima sessão Cowork, na hora de selecionar pastas, incluir `~/Downloads` na seleção.

---

## Fix 2 — `git push` sem precisar do Terminal nativo

**Problema:** Meu sandbox bash não tem suas credenciais do GitHub (estão no Keychain do macOS), então o `git push` falhava com `could not read Username for 'https://github.com'` e você teve que abrir o Terminal nativo para completar.

**Correção feita:**
1. `.gitignore` recebeu `.git-token-store`
2. `git config credential.helper` foi configurado para `store --file=...` apontando para 2 caminhos (Mac + sandbox) — assim funciona dos dois lados
3. Script `setup-token.sh` criado no root do repo

**Para finalizar (você precisa fazer 1x):**

1. Criar uma Personal Access Token no GitHub com scope `repo`:
   https://github.com/settings/tokens/new
2. No Terminal: `bash ~/Documents/Easy_Financas/setup-token.sh`
3. Colar a PAT quando o script pedir (não aparece na tela, é hidden input)

A partir daí, na próxima rodada da tarefa semanal, o `git push origin main` funciona direto do meu sandbox sem te interromper.

---

## Fix 3 — Caminho correto do CSV de caixa no Bling

**Problema:** A tarefa diz "Financeiro → Fluxo de Caixa". Mas:
- "Fluxo de Caixa" (em Relatórios) → projeção, não realizado
- "Relatório de Controle de Caixa" (em Relatórios) → não retorna dados quando Loja=Todas + Tipo=Analítico

**Caminho correto:** `Financeiro → Caixas e Bancos` (URL: `bling.com.br/caixa.php`)

### Passos exatos validados nesta sessão

1. URL direto: `https://www.bling.com.br/caixa.php`
2. No dropdown ao lado do título "Caixas e Bancos", selecionar **"Todas contas"**
3. No filtro de período (botão azul-verde topo da tabela), abrir e clicar **"Período customizado"**
4. Definir `Início: 01/01/2026` e `Fim: <data atual>` (não usar 31/12 — caixa é histórico realizado, datas futuras retornam 0)
5. Clicar **Filtrar**
6. Confirmar à direita "Quantidade de registros: ~248" (varia conforme data)
7. Clicar no botão **"Exportar extrato"** (próximo a "Imprimir saldos")
8. CSV cai em `~/Downloads` com nome `caixa_<YYYY-MM-DD HH_MM_SS>.csv`
9. Mover/renomear para `caixa_<YYYY-MM-DD>.csv` na pasta destino

**Substituir no runbook a seção ARQUIVO 3 por:**

```
─────────────────────────────────────────
ARQUIVO 3 — Extrato de Caixa
─────────────────────────────────────────
- Navegar para: Financeiro → Caixas e Bancos (bling.com.br/caixa.php)
- Selecionar "Todas contas" no dropdown ao lado do título
- Botão de período: clicar em "Período customizado"
- Início: 01/01/2026 · Fim: data atual de hoje
- Clicar Filtrar
- Clicar "Exportar extrato"
- Aguardar download em ~/Downloads (nome: caixa_<data hora>.csv)
```

---

## Validações executadas hoje (02/05/2026 22:37)

- ARQUIVO 1 baixado fresco: `Relatório de Pagamentos` → Período 01/01–31/12/2026 → Situação=Pagas → Agrupar por=Não agrupar → Exportar CSV (9654 bytes, 87 linhas, R$ 894.401,04 total)
- ARQUIVO 2 baixado fresco: `Relatório de contas a pagar Leandro` (custom) → Visualizar → Exportar CSV (18990 bytes, 74 linhas, R$ 715.196,72 em aberto)
- ARQUIVO 3 hoje usei o existente; o caminho correto agora está documentado acima
- ARQUIVO 4 baixado fresco: `Contas a receber (Personalizado)` → Visualizar → Exportar PDF (4963 bytes, 1 cliente em aberto R$ 203,97)
