# Runbook semanal — Easy Analytics Dashboard (API Bling v3)

**Versão 2.0 — 07/05/2026** · primeira versão automatizada via API.

A versão 1 fazia download manual de 4 arquivos no UI do Bling. Esta versão lê tudo via API OAuth2 e regenera o `bling-live.html` automaticamente. O `index.html` (dashboard com 6 abas e dados de pipeline/projeção) continua sendo editado manualmente quando esses números mudam — ele não é alimentado pela API porque o Bling não tem essa informação.

## Pré-requisitos (uma vez)

1. **Pastas com acesso no Cowork:**
   - `~/Documents/Easy_Financas` — scripts, .gitignore, credenciais, HTML público
   - `~/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais` — saída CSV/JSON

2. **Credenciais Bling salvas (chmod 600):**
   - `~/Documents/Easy_Financas/.bling-oauth.json` — client_id + client_secret
   - `~/Documents/Easy_Financas/.bling-tokens.json` — access_token + refresh_token

   O refresh_token vale ~30 dias e renova sozinho o access_token. Se passar mais de 30d sem rodar, é necessário rodar `bling-auth.py` de novo.

3. **GitHub PAT instalada** (para `git push`):
   - `bash ~/Documents/Easy_Financas/setup-token.sh` (uma vez por máquina)

## Fluxo semanal — comando único

Numa conversa Cowork, basta pedir: **"rode o runbook semanal"**. 100% online — não precisa do Terminal nativo.

Os comandos abaixo são o que rodam (em sandbox bash):

```bash
# 1. pull dos dados Bling (com retomada incremental)
python3 ~/Documents/Easy_Financas/fetch-bling.py \
  --data-inicial 2025-10-01 \
  --no-detail-receber

# 2. regenera bling-live.html
python3 ~/Documents/Easy_Financas/build-html.py

# 3. push pro GitHub Pages — usa clone temporario em /tmp porque
#    o iCloud Drive bloqueia .git/index.lock (Operation not permitted)
TMP=/tmp/ef-push
rm -rf "$TMP"
git clone https://github.com/leandrogelpke/Easy_Financas.git "$TMP"
cp ~/Documents/Easy_Financas/.git-token-store "$TMP/"
cd "$TMP"
git config credential.helper "store --file=$TMP/.git-token-store"
git config user.email "leandrogelpke@gmail.com"
git config user.name "Leandro Gelpke"
cp ~/Documents/Easy_Financas/bling-live.html .
cp ~/Documents/Easy_Financas/index.html . 2>/dev/null || true
cp ~/Documents/Easy_Financas/DRE_Projetado_Realizado.html . 2>/dev/null || true
git add .
git commit -m "weekly snapshot $(date +%Y-%m-%d)"
git push origin main
```

> **Por que o clone temporário?** A pasta `~/Documents/Easy_Financas/` está em iCloud Drive. Locks como `.git/index.lock` ficam com flag de "Operation not permitted" para o usuário do sandbox, então `git commit` direto na pasta sincronizada falha. O workaround é commitar de uma cópia em `/tmp/` (fora do iCloud) e push.

## O que cada script faz

### `fetch-bling.py`
Baixa do Bling 4 datasets para `~/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais/bling-api/`:
- `contas_pagar_em_aberto_<data>.csv` — todas em aberto
- `contas_pagar_pagas_<data>.csv` — pagas desde `--data-inicial` (default sem filtro = tudo)
- `contas_receber_em_aberto_<data>.csv`
- `contas_receber_recebidas_<data>.csv`
- `categorias_receitas_despesas.csv` — referência de DRE
- `contatos.csv` — clientes/fornecedores
- `bling_data_<data>.json` — snapshot consolidado para o build

**Retomada automática:** salva cache em `.cache/` a cada 25 itens. Se a sessão Cowork timeoutar, é só rodar de novo — continua de onde parou.

**Tempo:** ~3-5 min em execução completa (limita-se a ~3 req/s pela API).

### `build-html.py`
Lê o `bling_data_<data>.json` mais recente e gera `~/Documents/Easy_Financas/bling-live.html` com:
- 4 KPIs no topo (vencidos, próximos 30d, a receber, recebido 12m)
- Buckets de pagar (vencidos / 30d / 31-60d / 61-90d)
- Tabelas de contas a pagar/receber (vencidos + próximos 30 itens)
- Top categorias de gastos
- Top clientes pagantes

**Tempo:** <1s.

## Manter o `index.html` atualizado

O `index.html` (dashboard principal) é editado manualmente quando:
- Pipeline comercial muda (oportunidades, propostas)
- Premissas de projeção mudam (receita base, recuperação Atacadão, Efata)
- Status dos clientes muda (cancelamento, novo deal)

**Não atualizar:** os números financeiros vão estar desatualizados, mas o `bling-live.html` cobre essa parte com dados frescos do dia.

## Troubleshooting

### `bling-auth.py` precisa rodar de novo
- Token revogado por mudança de escopo no app Bling
- Refresh_token expirou (>30d sem rodar)
- Mudou client_secret

Como fazer:
```bash
python3 ~/Documents/Easy_Financas/bling-auth.py
```
Browser abre, autoriza, novos tokens salvos.

### Categoria "(sem categoria)" tem muito valor
Atualizar classificação no Bling — esses lançamentos não tem categoria associada, então não vão pro DRE corretamente.

### TOTVS aparece como 3 clientes diferentes
Filiais com CNPJs diferentes no Bling. Pode consolidar editando os contatos no Bling, ou aceitar a duplicidade no dashboard.

## Arquitetura

```
~/Documents/Easy_Financas/                    Repo público (GitHub Pages)
├── index.html                                Dashboard principal (manual)
├── DRE_Projetado_Realizado.html              DRE (manual)
├── bling-live.html                           Live data (gerado por build-html.py)
├── bling-auth.py                             Inicial OAuth flow
├── fetch-bling.py                            Pull semanal
├── build-html.py                             Render HTML
├── .bling-oauth.json                         Credenciais (gitignored)
├── .bling-tokens.json                        Tokens (gitignored, regenerados)
└── .gitignore                                Bloqueia segredos

~/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais/bling-api/
├── bling_data_<data>.json                    Snapshot consolidado
├── contas_*_<data>.csv                       Por dataset
├── categorias_*.csv
├── contatos.csv
└── .cache/                                   Pra retomada incremental
```
