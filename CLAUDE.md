# CLAUDE.md — Guia para agentes IA editando este projeto

Este arquivo consolida aprendizados de ~8 semanas de evolução do dashboard
Easy Analytics. Leia antes de tocar em qualquer coisa — vários erros já
foram cometidos e corrigidos; não os repita.

---

## 1. O que é este projeto

Dashboard financeiro pessoal do Leandro (Easy Analytics):

- **Fonte de dados:** API Bling v3 (OAuth2) + relatórios Totvs (.eml/.xlsx
  vindos do Google Drive).
- **Saída:** `index.html` estático publicado em GitHub Pages
  (`https://leandrogelpke.github.io/Easy_Financas/`).
- **Cadência:** **automatizada no GitHub Actions** — `update.yml` roda
  3×/dia (07h/12h/16h BRT) e publica sozinho. O `weekly.sh` virou
  ferramenta **local** para atualizar Totvs/Cartão (que vêm do Drive). É
  idempotente — pode rodar várias vezes no mesmo dia sem duplicar nada.

> **Automação (CI):** a arquitetura de workflows, gates, secrets e rotação
> de token está documentada em **`DOCS.md`**. Leia antes de mexer em
> `.github/workflows/` ou em `ci/`.

**Princípio rígido:** TUDO que é número no dashboard deve vir de dados
vivos do Bling / Totvs / auditoria. Nada deve ser hardcoded no template
(já caímos nessa armadilha — ver §6.1).

---

## 2. Estrutura dos arquivos

### Scripts Python

| Arquivo | Função |
|---|---|
| `fetch-bling.py` | Pull dos 4 datasets Bling com cache incremental (.cache/) |
| `fetch-totvs.py` | Processa .eml/.xlsx em `totvs/raw/` (dedupe por hash) |
| `build-html.py` | **Gera `index.html` a partir do `template.html`** |
| `audit.py` | Auditoria tributária. CONFIG é fonte única de alíquotas |
| `dre_render.py` | `_build_matriz()` que abastece DRE + audit |
| `contas_view.py` | Render das tabelas A Pagar / A Receber |
| `totvs_render.py` | Aba Comissões Totvs |
| `fetch-cartao.py` | Parseia faturas Bradesco PDF → `cartao_snapshot.json` (usa `pdftotext`) |
| `cartao_render.py` | Aba "Cartão" (ntab/mobtab/pg) a partir do snapshot |
| `chat_widget.py` | Widget de busca/chat IA injetado no fim |
| `test_audit.py` | Self-tests do audit (10 testes) — gate de regressão |
| `bling-auth.py` | Fluxo OAuth2 inicial (raramente usado) |

### Shell / config

- `weekly.sh` — orquestrador. **Tem gate de testes obrigatório** (§7).
- `setup-token.sh` — instala PAT do GitHub.
- `.bling-oauth.json` + `.bling-tokens.json` — credenciais (chmod 600).
- `.git-token-store` — PAT pra push (chmod 600).

### Dados de classificação versionados (committados)

- `clientes_classificacao.json` — mapeia nome Bling → {id, display, cat
  (garantido/medio/alto), uso, abr, obs}.
- `KNOWN_SUPPLIERS` em `build-html.py` — fornecedores: mapeia nome Bling
  upper → {id, display, cat (Buy-out/Serviços PJ/Tributos e
  Contábil/Aporte/Sócios/Parceria TOTVS/Software/Reembolsos/Outros), st,
  sc}.
- `STACK_GROUPS` em `build-html.py` — agrupamento do gráfico empilhado.

### Templates

- `template.html` — fonte do dashboard com **marcadores `@@NOME@@`** que
  o `build-html.py` substitui. Veja §3.2.
- `index.html` — saída gerada. **Listado com `--skip-worktree` no git
  local**, mas commitado no repo público.

### Paths

- Working dir: `~/Documents/Easy_Financas` (mount Cowork:
  `/sessions/<id>/mnt/Easy_Financas/`).
- Snapshots Bling: `~/Documents/GRAP/Negociação Easy/Controles
  Easy/relatorios atuais/bling-api/` (mount: `/sessions/<id>/mnt/relatorios
  atuais/bling-api/`).
- Quando rodar `build-html.py` manualmente, **passe `--in` explícito**
  porque o default do script assume layout Mac nativo:

  ```bash
  python3 build-html.py --in "/sessions/<id>/mnt/relatorios atuais/bling-api"
  ```

---

## 3. Fluxo de dados

### 3.1 Pipeline geral

```
fetch-bling.py  →  bling_data_YYYY-MM-DD.json   ┐
fetch-totvs.py  →  totvs_snapshot.json          ├→  build-html.py  →  index.html  →  git push
                                                │       ↑
audit.py        ←  _build_matriz()              ┘   substitui @@MARCADORES@@
                       ↑
                    matriz: receita_por_mes,
                    items, grupos, month_types
```

### 3.2 Convenção dos marcadores `@@NOME@@`

Todo número/HTML dinâmico no `template.html` é um marcador `@@NOME@@`.
O `build-html.py` faz `html.replace("@@NOME@@", valor)` no final do
fluxo. **Se um marcador escapar sem ser substituído**, ele aparece
literalmente no dashboard.

Sempre rode esta verificação após mudar templates:

```bash
grep -oE "@@[A-Z_]+@@" index.html | sort -u
# (lista deve sair vazia)
```

### 3.3 Marcadores ativos hoje

`@@OVERVIEW_HERO_SUB@@`, `@@OVERVIEW_PILLS@@`, `@@OVERVIEW_KPIS@@`,
`@@OVERVIEW_RISCOS@@`, `@@OVERVIEW_POSITIVOS@@`,
`@@NEXT_ACTIONS_IMEDIATAS@@`, `@@NEXT_ACTIONS_ACOMPANHAR@@`,
`@@GASTOS_HERO_TITLE/SUB@@`, `@@PAGAR_MENSAL_HTML@@`, `@@CAIXA_KPIS@@`,
`@@CLI_*@@` (KPIs, nº, valores, %), `@@CLI_DATA@@` (array JS),
`@@CLI_DONUT_DATA@@`, `@@ROWS@@`, `@@CF_*@@`, `@@CX_DATA@@`,
`@@PAGAR_DATA@@`, `@@RECEBIDO_DATA@@`, `@@DRE_DATA@@`,
`@@DRE_DETAIL@@`, `@@CONTAS_*@@`, `@@AUDIT_*@@`, `@@TOTVS_*@@`,
`@@UPDATED_AT@@`, `@@LOGIN_DATE@@`, `@@PWD_HASH@@`,
`@@CARTAO_NTAB@@`, `@@CARTAO_MOBTAB@@`, `@@CARTAO_PG@@`.

---

## 4. Regras tributárias (homologadas com Serrano em mai/2026)

**Regime:** Lucro Presumido · CNAE 6209-1/00 · sede São Bernardo do Campo - SP.

**Não tem folha relevante** (prestadores são PJ) → Simples Nacional não se aplica.

| Tributo | Base | Alíquota | Efetivo s/ receita |
|---|---|---|---|
| ISS-SBC | Receita bruta | 2,00% | 2,00% |
| PIS | Receita bruta | 0,65% | 0,65% |
| COFINS | Receita bruta | 3,00% | 3,00% |
| IRPJ | 32% da receita (presumida) | 15,00% | 4,80% |
| IRPJ adicional | Lucro presumido **>** R$ 60.000/trim | 10,00% | variável |
| CSLL | 32% da receita (presumida) | **9,00%** ← NÃO É 15% | 2,88% |
| **Carga total efetiva** | | | **~13,33%** |

### Retenções (PJ contratante retém na fonte da Easy)

- **CSRF** (PIS+COFINS+CSLL retidos) = **4,65%** (0,65 + 3,00 + 1,00).
- **IRRF** serviços profissionais = **1,5%**.
- Piso para reter quando Easy contrata PJ: **R$ 215,05/mês** (Lei 10.833).
- Total esperado de retenção em NF da Easy: **4,65% + 1,5% = 6,15%**.
- Quando há retenção, ela **abate** PIS/COFINS/CSLL/IRPJ a recolher —
  nunca tratar como custo adicional.

### Trimestrais (IRPJ + CSLL)

- Pagamento em até **3 quotas mensais** (art. 5º Lei 9.430/96).
- `audit.py` agrupa quotas via `_agregar_pago_lucro_por_trimestre()` —
  combina trimestre via `_trimestre_do_historico()` ("1º TRIMETRE/2026")
  + fallback pelo mês de pagamento.
- Não comparar 1 quota com total esperado (bug histórico).

### Fonte única de verdade

Todas as alíquotas estão em `audit.py::CONFIG`. **Não duplique** em outros
módulos. O `test_audit.py::test_aliquotas_config` trava regressão.

---

## 5. Convenções obrigatórias

### 5.1 Classificação de fornecedor / cliente

**Fornecedor**: editar `KNOWN_SUPPLIERS` em `build-html.py`. Adicione
todos os aliases que o Bling usa (com e sem acento, com e sem sufixo de
CPF). Categorias canônicas:

- `Buy-out`
- `Pessoal` — prestadores PJ que o Leandro reporta como "Despesas com
  pessoal" (Alan, Isabel, Macedo, Efata, Milajanu, Eduardo). Espelha o
  grupo `desp_pessoal` do DRE (`dre_render.py::FORNECEDOR_MAP`). **Não é
  folha CLT** — é escolha gerencial de reporte.
- `Serviços PJ`
- `Tributos e Contábil`
- `Parceria TOTVS`
- `Aporte/Sócios`
- `Software`
- `Reembolsos`
- `Outros`

**Cliente**: editar `clientes_classificacao.json`. Categorias de risco:

- `garantido` — uso ativo, paga em dia
- `medio` — paga mas intermitente
- `alto` — vencido / churn flagged / cancelamento

Clientes do Bling **sem entrada no JSON** caem em `sem_classificacao`.

### 5.2 Aliases — atenção ao truncamento do Bling

O Bling **trunca contato_nome em 80 chars** sem aviso. Ex.: FUNPAR vira
`"FUNDACAO DA UNIVERSIDADE FEDERAL DO PARANA PARA O DESENVOLVIMENTO DA
CIENCIA, TE"`. Sempre inclua **ambas** as variantes no array `aliases`.

### 5.3 Janelas de tempo (year-to-date)

- Gráfico "Evolução mensal por grupo" e tabela "Detalhamento por
  fornecedor" usam **YTD** (jan do ano corrente → mês atual).
  Função: `compute_last_4_months()` em `build-html.py` (nome legado
  mantido; comportamento é YTD desde jun/2026).
- Aba Clientes usa **rolling 12 meses fechados** (não mês corrente).
  Captura clientes intermitentes tipo Tele Rio (~5×/ano).
- DRE mostra **toda a série** disponível, não filtra janela.

### 5.4 Padrão dos `compute_*_html()`

Cada função retorna HTML pronto pra `replace()`. Nunca retorne só dados
"crus" que o template precisa parsear — o template é HTML estático;
deixe a montagem em Python.

---

## 6. Armadilhas conhecidas (não repetir)

### 6.1 Hardcode no template

**Histórico**: a aba Clientes ficou com 27 clientes hardcoded por meses
até alguém perceber que Tele Rio mostrava R$ 10.978 enquanto o real era
R$ 5.537. Mesma coisa com tabelas "Maio/2026 — total R$ 155.645" da aba
Caixa.

**Regra:** se você está prestes a colocar um número R$ literal no
`template.html`, **pare**. Crie um marcador `@@NOME@@` e um `compute_*`
que puxe dos dados.

**Exceções aceitáveis** (qualitativas, não-cálculo):
- Textos descritivos de seções
- Selects de opções de cenário
- Notas explicativas

### 6.2 HTML mal-balanceado escondendo abas

`.pg` (página) tem `display:none` por padrão; só vira `block` com
`.active`. Se uma página esquece de fechar uma `<div>`, **todas as abas
seguintes ficam aninhadas dentro dela** e somem ao trocar de aba.

**Sintoma**: "abas em branco a partir da X". Já aconteceu uma vez (aba
Caixa esqueceu 1 `</div>` quando o bloco hardcoded foi envelopado em
`display:none` pra ser removido depois).

**Verificação obrigatória** após qualquer edição em `template.html`:

```bash
python3 -c "
import re
html = open('index.html').read()
no_js = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
pgs = list(re.finditer(r'<div class=\"pg(?:\s+active)?\" id=\"pg-([\w-]+)\"', no_js))
for i, m in enumerate(pgs):
    end = pgs[i+1].start() if i+1 < len(pgs) else len(no_js)
    bloco = no_js[m.start():end]
    o, c = bloco.count('<div'), bloco.count('</div>')
    print(f'  {m.group(1):14s} opens={o:3d} closes={c:3d} delta={o-c:+d}')
"
```

Todas as `pg` devem ter `delta=+0`. **Considere mover esse check pra
test_build.py e plugar no gate do weekly.sh.**

### 6.3 Base64 corrompido no Drive pull

**Histórico**: `download_file_content` do Drive MCP retorna base64. Em
25/05/2026, salvar a string via `Write` strippou o `==` final do
padding, e `base64 -d` (GNU) rejeitou. Resultado: vários `.xlsx`
corrompidos com magic ZIP inválido.

**Solução obrigatória**: decodificar via Python tolerante com re-padding
e validar magic ZIP `PK\x03\x04`. Padrão completo no
`SKILL.md` (`runbook semanal`), etapa 1.

**Nunca**:
- `echo "$B64" | base64 -d`
- `Write` da string base64 num arquivo

### 6.4 Categorização ISS — TOMADOR ≠ PRESTADOR

O Bling registra ISS-TOMADOR (Easy retém ao pagar PJ de fora de SBC) na
mesma categoria de ISS-PRESTADOR (Easy paga sobre sua receita).
**Histórico** (`audit.py` bug 1): somava os dois e gerava falso erro
"ISS divergente".

**Regra**: em qualquer apuração de ISS sobre receita, filtre
`_is_iss_tomador(item)` (busca "TOMADOR" no histórico). Já implementado
em `check_iss_sbc(excluir_tomador=True)`.

### 6.5 Competência ≠ data de pagamento

O Bling guarda data de pagamento, mas o histórico tem a **competência
real** ("ISS S/ FATURAMENTO - 01/2026"). Atribuir competência pela regra
"pago em N+1 = competência N" gera falsos positivos quando há
antecipação ou atraso.

**Regra**: usar `_competencia_do_historico(hist, fallback)` no audit. O
fallback é a regra N+1, mas só quando o histórico não traz competência.

### 6.6 Cache do GitHub Pages

Após `git push`, o GitHub Pages leva **1-2 minutos** pra propagar.
Avise o usuário pra atualizar a página com Ctrl+Shift+R (force reload)
se ele não vir as mudanças.

### 6.9 Crash em JS de render derruba abas seguintes

**Histórico** (10/06): `cliRender()` fazia `CAT_CLI.find(c=>c.cat===row.cat).label`
sem guarda. Quando o snapshot tinha cliente `sem_classificacao` (categoria
fora do `CAT_CLI`, que só tem garantido/medio/alto), `find` retornava
`undefined` → `.label` lançava erro. Como `cliRender()` roda no bloco INIT,
o erro **interrompia o resto do script** → Projeção e Clientes ficavam em
branco (mas as abas anteriores funcionavam, mascarando a causa).

**Regra**: todo `.find(...)` em dados vivos deve ter fallback
(`var o=arr.find(...); var x=o?o.label:'default'`). Categorias `sem_*`
sempre existem (clientes/fornecedores novos não mapeados).

### 6.10 Lançamentos fantasma no a pagar (pago + em aberto simultâneos)

**Histórico** (12/06): a Visão Geral mostrava R$ 178.748 "vencidos a pagar"
enquanto a aba Gastos mostrava os mesmos fornecedores (Rômulo, Efata,
Macedo…) como **pagos**. Causa: o Bling mantém o lançamento original em
`contas_pagar_em_aberto` mesmo depois de pago — a baixa não fecha a cópia.
Dois padrões:

- **DUPLICATA_PAGA**: mesma (contato · vencimento · valor) existe em
  `pagas` E `em_aberto`. Ex.: Rômulo PARC 17 (R$ 59.814, venc 25/05).
- **PROVISAO_COBERTA**: provisão genérica (histórico com PREVISÃO /
  PROVISÃO / placeholder `XX/AAAA`) já substituída por NF real paga no
  mesmo mês de vencimento. Ex.: Efata "PRESTAÇÃO SERVIÇOS XX/2026"
  R$ 35.000 coberta por 2× NF de R$ 17.500.

**Regra**: nunca trate `pagas` e `em_aberto` como disjuntos. Toda leitura
de `contas_pagar_em_aberto` passa por `audit.py::reconcile_em_aberto(pagas,
em_aberto, today)` → `(limpo, ajustes)`. Já plugado em `build-html.py`
(`render()` + preview), `contas_view.py` e `dre_render.py::_load_bling_csvs`.
A trilha de auditoria sai no card `check_reconciliacao_pagar` (aba
Auditoria) a cada rodada. Testes: `test_reconcile_*` em `test_audit.py`.
A função é idempotente (consome no máx. N gêmeos pra N pagos).

**Verificação**: rodar o JS principal num shim de DOM com Node pega isso
antes do push:
```bash
# extrai o <script> principal pra /tmp/main_mod.js, depois:
node -e "global.document={getElementById:()=>({style:{},classList:{add(){}},
  appendChild(){},getContext:()=>({}),querySelectorAll:()=>[],value:'80000',
  set innerHTML(v){},set textContent(v){}}),querySelectorAll:()=>[],
  createElement:()=>({}),addEventListener(){}};global.window={addEventListener(){}};
  global.Chart=function(){return{data:{datasets:[{},{}]},update(){},destroy(){}}};
  Chart.defaults={font:{}};eval(require('fs').readFileSync('/tmp/main_mod.js','utf8'));
  console.log('INIT ok')"
```

### 6.7 Timeouts na bash tool

`fetch-bling.py` baixa ~120 itens novos com ~1,5s/item por rate limit
do Bling. **A bash tool tem timeout de 45s por chamada** → o
`weekly.sh` pode timeoutar no meio. **Não é problema** — o cache
`.cache/` retoma do ponto onde parou. Estratégia: rodar `weekly.sh` até
5x até completar. `fetch-totvs.py` deduplica por hash, então rerun é
seguro.

### 6.8 Permissão de remover do iCloud

`rm` em arquivos do iCloud Drive pode falhar com "Operation not
permitted" quando o arquivo está bloqueado pelo iCloud. **Não é fatal**
— a limpeza de snapshots antigos no `weekly.sh` ignora esses erros.
Avisar usuário se acumular muito.

### 6.11 A janela do fetch é por EMISSÃO — parcelamento longo some inteiro

**Histórico** (21/ago): o Leandro notou que a linha "Buy-out + Acordo
trabalhista" da DRE mostrava R$ 120K em 2026 quando o real era
R$ 478.885,56. O buy-out do Rômulo (6 parcelas de R$ 59.814,26 vencendo
jan–jun/26) tinha sumido de **todos** os snapshots desde 1º/ago.

Causa: `update.yml` chama `fetch-bling.py --data-inicial`, que vira
`dataEmissaoInicial` na API. Um parcelamento é emitido **uma vez só** —
o do Rômulo em `2025-12-08` — e vence por 18 meses. Enquanto a janela de
7 meses começava em 2025-12-01, tudo aparecia. Em 1º/ago ela passou a
começar em 2026-01-01 e as 6 parcelas evaporaram juntas. Nada quebrou:
os gates validam consistência, não **completude histórica**.

Junto foram embora Efata (R$ 210K em aberto, emissão set–nov/25),
Milajanu, IRPJ/CSLL do 4T25 e R$ 100K a receber da TOTVS.

**Regra**: nunca tratar o snapshot do dia como a verdade completa.
`ci/merge_historico.py` mantém `data/bling-api/historico.json` (versionado)
acumulando por `id`:

- registro no snapshot novo → snapshot vence;
- só no histórico e com emissão **fora** da janela → preserva;
- só no histórico e com emissão **dentro** da janela → remove (ali o
  snapshot é autoritativo, então sumiu = foi apagado no Bling).

O merge roda logo após o fetch e reescreve o snapshot do dia **e os
CSVs** — `build-html.py` lê o JSON, `dre_render.py` lê os CSVs; os dois
precisam do conjunto completo. Testes: `test_merge_historico.py` (gate no
`update.yml`).

**Bootstrap**: sem `historico.json`, a rodada usa janela de 24 meses
automaticamente pra semear o arquivo. Pra forçar depois, rode o workflow
`update` pelo Actions com o input `janela_meses`.

**Cuidado ao mexer**: o arquivo pode ressuscitar um lançamento que o
Bling apagou, se a emissão dele for anterior à janela. É o preço de não
perder parcelamento. A correção é rodar com `janela_meses` grande, que
traz a emissão pra dentro da janela e deixa o merge podar de verdade.

---

## 7. Gate de testes obrigatório

`weekly.sh` etapa 1c (`=== 1c. testes de regressão (audit) ===`):

```bash
python3 "$EF/test_audit.py" || exit 2
```

**Trava a rodada** se algum teste quebrar. Testes cobrem:

1. Alíquotas (PIS/COFINS/ISS/IRPJ/CSLL/IRRF/CSRF — incluindo CSLL=9%).
2. Parser de competência por histórico (MM/AAAA).
3. Parser de trimestre por histórico ("TRIMETRE" typo aceito).
4. Filtro de ISS-TOMADOR.
5. Agregação ISS por competência (jan/26 pago em dez/25, etc).
6. Agregação trimestral IRPJ/CSLL com 3 quotas.

**Antes de qualquer commit que toque `audit.py`, rode**:
```bash
python3 test_audit.py
```

Adicionar testes equivalentes pra `build-html.py` se mexer em
agregações (já planejado: contagem de divs por `.pg`).

---

## 8. Comandos úteis

### Rodar build localmente
```bash
python3 build-html.py --in "/sessions/<id>/mnt/relatorios atuais/bling-api"
```

### Rodar audit standalone
```bash
python3 audit.py --bling-dir "/sessions/<id>/mnt/relatorios atuais/bling-api"
```

### Rodar weekly completo
```bash
bash weekly.sh
```

### Listar marcadores não-substituídos
```bash
grep -oE "@@[A-Z_]+@@" index.html | sort -u
```

### Forçar push (ignorar cache iCloud)
Usa `mktemp -d /tmp/ef-push-XXXXXX` (não tenta commitar no working tree
local, que tem `.git/index.lock` bloqueado pelo iCloud).

### Investigar lançamentos de um cliente / fornecedor
```python
import json
d = json.load(open('bling_data_YYYY-MM-DD.json'))
for r in d['contas_receber_recebidas']:
    if 'NOME' in (r.get('contato_nome') or '').upper():
        print(r['vencimento'], r['valor'], r.get('historico'))
```

---

## 9. Quando o usuário pedir uma mudança

1. **Identifique** se é dado vivo (vai pra `compute_*` + marcador) ou
   classificação manual (vai pra `KNOWN_SUPPLIERS` /
   `clientes_classificacao.json` / `audit.py::CONFIG`).
2. **Nunca** adicione número literal R$ no `template.html`.
3. **Rode** `test_audit.py` antes de gerar o HTML.
4. **Verifique** divs balanceadas em cada `.pg` (§6.2) antes de push.
5. **Confirme** que `grep "@@[A-Z_]+@@" index.html` está vazio.
6. **Commit** com mensagem descritiva do que mudou (ex.: `clientes:
   Bling-driven · Tele Rio R$ 10.978→5.537`).

---

## 10. Histórico de mudanças estruturais (timeline 2026)

| Quando | O quê | Onde |
|---|---|---|
| 07/05 | OAuth2 Bling v3 + fetch-bling.py + build-html.py | `RUNBOOK.md` v2 |
| Mai/26 | Confirmação alíquotas com Serrano | `audit.py::CONFIG` |
| 25/05 | Bug base64 do Drive → adotado Python tolerante | SKILL.md |
| 01/06 | YTD na aba Gastos (era janela móvel 4m) | `compute_last_4_months()` |
| 01/06 | 3 bugs do audit corrigidos: TOMADOR, competência, multi-quota | `audit.py` + `test_audit.py` |
| 01/06 | Gate de testes no weekly.sh | `weekly.sh` 1c |
| 01/06 | Explosão do bucket "Outros" em Tributos/TOTVS/Aporte/etc | `KNOWN_SUPPLIERS` + `STACK_GROUPS` |
| 03/06 | Aba Clientes Bling-driven (era 27 hardcoded) | `compute_cli_data()` + `clientes_classificacao.json` |
| 03/06 | Tabelas Mai/Jun da Caixa Bling-driven | `render_pagar_mensal_html()` |
| 03/06 | Próximas ações Bling-driven (eram hardcoded) | `compute_next_actions()` |
| 03/06 | Fix </div> faltando na Caixa que escondia 9 abas | `template.html` |
| 10/06 | Data de lançamento (dataEmissao) nas contas a pagar da Caixa; `--no-detail-pagar-aberto` removido do weekly | `build-html.py` + `weekly.sh` |
| 10/06 | Nova aba "Cartão" — faturas Bradesco PDF (jan–jun/26) | `fetch-cartao.py` + `cartao_render.py` |
| 10/06 | Cartão via Drive: pasta `Cartao_Credito` (ID 1CojhJ7BUW4na5CQZIYdXJglsh3YyCffs) → `cartao/raw/`; runbook etapa 1.5; dedup por mês+cartão | `weekly.sh` + scheduled task |
| 10/06 | Caixa: seção Vencidos (100%) + janela a pagar = atual+2m + gráfico fluxo mensal (entradas×saídas) | `render_vencidos_html` / `render_cashflow_html` |
| 10/06 | Fix cliRender quebrava (`CAT_CLI.find().label` em cliente `sem_classificacao`) e derrubava o resto do script → Projeção/Clientes em branco | `template.html` cliRender defensivo |
| 12/06 | Reconciliação a pagar: remove fantasmas que inflavam "vencidos" (Visão Geral marcava vencido o que Gastos mostrava pago). Vencido a pagar R$ 178.748 → R$ 11.686 | `audit.py::reconcile_em_aberto` + plugado em build-html/contas_view/dre_render + 4 testes |
| 14/06 | **Migração para GitHub Actions** — `update.yml` (3×/dia) + `audit.yml` (auditor encadeado). Secrets `BLING_TOKEN`/`BLING_OAUTH`/`GH_PAT`; rotação automática do token Bling | `.github/workflows/` + `ci/` · ver `DOCS.md` |
| 14/06 | Cards: Runway (`caixa_config.json`), Concentração de receita, Retenções—crédito tributário (`audit.py::check_retencao_credito_resumo` + `retencoes_compensadas.json`) | build-html.py + audit.py |
| 14/06 | **Gotcha:** `TOTVS_SNAP.parent` define o dir dos CSVs do Bling → snapshot Totvs movido p/ `data/bling-api/`. Sem isso, abas Auditoria/P&L/DRE/Contas saem vazias | `update.yml` |
| 15/06 | **Gate pré-commit** (`ci/precommit_check.py`): bloqueia publicação de dado/artefato ruim; commit/push agora condicional a fetch+build+gates+precommit verdes | `update.yml` + `ci/` |
| 20/06 | **Drive pull movido pro CI** — `ci/drive_pull.py` (service account Google, Secret `GDRIVE_SA_KEY`) baixa Totvs/Cartão no próprio Actions; `ci/snapshot_guard.py` só commita se houver dado novo (ignora `_meta`, piso de sanidade 80%). `weekly.sh` local fica **deprecado** (Bling já é do CI; token local quebra na rotação). No-op seguro sem o Secret | `update.yml` + `ci/drive_pull.py` + `ci/snapshot_guard.py` |
| 10/07 | Bling: 72 lançamentos "sem categoria" classificados via UI + 3 categorias novas (Salários Comercial/Operação, Hosting/Sistemas) + categoria padrão nos contatos. **Gotcha:** recorrência do Bling não deixa editar categoria (só Inativar); as 3 mensais (Receita Federal/Cartão/Alan) seguem gerando sem categoria | Bling (fora do repo) |
| 21/08 | **Arquivo histórico do Bling** (`ci/merge_historico.py` + `data/bling-api/historico.json`): a janela de 7 meses por data de EMISSÃO tinha derrubado o buy-out do Rômulo inteiro em 1º/ago. Buy-out 2026 R$ 120K → **R$ 478.885,56**. Bootstrap automático de 24 meses + input `janela_meses` no `workflow_dispatch` + gate `test_merge_historico.py` | ver §6.11 |
| 21/08 | **Gap fill retroativo desligado** no `_build_matriz_2y`: mês passado sem lançamento virou `sem_dado` (travessão), não mais média. Antes o DRE fabricava a coluna 2025 inteira a partir da média de 2026 — despesas 2025 "R$ 607K" com **zero** real na base. Banner de "comparativo parcial" quando falta mês | `dre_render.py` |
| 10/07 | **Grupo "Pessoal"**: 6 PJ (Alan, Isabel, Macedo, Efata, Milajanu, Eduardo) movidos p/ Despesas com pessoal em todo o BI. `FORNECEDOR_MAP` → `desp_pessoal`/subgrupo "Prestadores PJ"; DCTFWEB isolado via `_sum_subgroup("Encargos sobre pró-labore")` p/ não inflar auditoria INSS. `KNOWN_SUPPLIERS` cat=`Pessoal` + `STACK_GROUPS` grupo Pessoal + KPI no template. DRE pessoal R$1.8K→R$600K | `dre_render.py` + `build-html.py` + `template.html` |

---

## 11. Princípios

1. **Verdade vem dos dados.** Bling, Totvs e auditoria são a fonte. O
   template só decora.
2. **Tudo idempotente.** `weekly.sh` rodando 5x produz o mesmo
   resultado. Nenhum estado mutável fora dos snapshots.
3. **Fail loud.** Erro de teste, alíquota errada, marcador escapado:
   bloqueia o push. Silenciar é pior que travar.
4. **Não invente regras tributárias.** Sempre confirme com Serrano antes
   de mudar `audit.py::CONFIG`. Histórico de "achismo" gerou falsos
   positivos.
5. **Adicione teste antes de re-fixar bug.** Cada um dos 3 bugs do
   audit tem um teste em `test_audit.py`. Próximo bug de template/HTML
   merece um `test_build.py`.
