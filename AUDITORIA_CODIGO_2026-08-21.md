# Auditoria de código e lógica financeira — 21/ago/2026

> **STATUS (21/ago, mesmo dia):** correções executadas e publicadas.
> **Feito:** P0.1, P0.2, P0.4, P0.5 (abatimento automático), P0.8, P0.9,
> P1.2, P1.4, P1.6, P1.7, P1.9, P1.1 (captura do dado; regime segue por
> vencimento), aliases do P1.3, parse_money/PAGES do P2, P0.6 (rotulado como
> snapshot manual).
> **P0.3 resolvido (21/ago, tarde):** saldo de caixa agora vem do Bling a
> cada fetch (`fetch_saldo_caixa`; `caixa_config.json` virou fallback) — o
> endpoint de saldos é sonda não confirmada na doc pública: conferir o log
> do 1º run do CI. Provisões Efata encerradas via
> `provisoes_encerradas.json` (R$ 210K removidos; só gasto real fica).
> Recomendo ainda inativar as provisões no próprio Bling quando conveniente
> — o arquivo compensa, mas dado limpo na origem é melhor.
> **Lote final executado (21/ago, noite):** P0.7 (Projeção 100% calculada —
> `@@PROJ_CFG@@`), P0.6 completo (Pipeline → `pipeline_data.json`, contagens
> derivadas), P1.1 completo (regra `data_caixa()`: pagamento real quando
> existir), P1.3 completo (`fornecedores_classificacao.json` fonte única),
> P1.5 (contagem de clientes "auto" no subtítulo), P2 (LIMIARES nomeados,
> seed do donut, fBRx, weekly.sh bloqueado, gate node --check).
> **Não feito de propósito:** P1.8 (banner de horizonte no gráfico DRE — o
> aviso de "comparativo parcial" da matriz já cobre o essencial); PWD_HASH
> segue como proteção de fachada (decisão de produto, não bug).

Escopo: `build-html.py`, `dre_render.py`, `audit.py`, `template.html`,
`fetch-bling.py`, dados mesclados pós-`merge_historico`. Cada achado tem
arquivo:linha verificados. Prioridade: **P0** = número errado visível hoje ·
**P1** = semântica inconsistente entre visões · **P2** = higiene/risco latente.

Valores citados usam a base já corrigida (com `historico.json`).

---

## P0 — números errados no dashboard hoje

### P0.1 · Donut de Gastos esconde R$ ~250K e os percentuais não fecham 100%
`template.html:727` define `CF_CATS` com só 4 categorias (Buy-out, Pessoal,
Serviços PJ, Outros), mas `KNOWN_SUPPLIERS` emite 8. Em `template.html:1506-1519`
o total (`grand`) soma TODAS as categorias, mas a rosca só desenha as 4 —
Aporte (R$ 197K), Tributos e Contábil, Parceria TOTVS e Software somem do
gráfico e distorcem o % de todas as fatias.
**Mudança:** gerar `CF_CATS` do Python via marcador (`@@CF_CATS@@`) a partir
das categorias realmente presentes, ou desenhar categoria não listada como
fatia "Outros".

### P0.2 · Aporte de sócio: cada visão trata de um jeito (3 delas erradas)
"APORTE POLAR" (R$ 197K pago em 2026) + IVY (R$ 14K) estão em
`contas_pagar_pagas`. O comentário em `build-html.py:135-144` diz "NÃO é
despesa operacional", mas o flag `cat` nunca é usado pra excluir de soma:

| Visão | Hoje | Correto? |
|---|---|---|
| Gastos YTD (`compute_rows`, total do header L2089) | soma como gasto | ✗ |
| Runway/burn (`compute_overview_kpis_html` L1598-1640) | infla o burn em ~R$ 28K/mês → runway subestimado | ✗ |
| Gráfico DRE rec×desp (`compute_dre_data` L1377-80) | soma como despesa | ✗ |
| P&L/DRE (dre_render `PL_EXEC`) | linha separada "não somam" | ✓ |
| Fluxo mensal (`render_cashflow_html`) | saída de caixa | ✓ (é caixa real) |

**Mudança:** decidir a semântica UMA vez — sugestão: aporte/distribuição fica
FORA de despesa operacional (Gastos, DRE, burn do runway) e DENTRO das visões
de caixa puro (fluxo mensal). Implementar exclusão por `cat in ("Aporte/Sócios",)`
nos três pontos marcados ✗. O burn do runway passa a ser "burn operacional".

### P0.3 · Card Runway está aritmeticamente sem sentido hoje
`caixa_config.json` tem `saldo_caixa: 515.75` (quinhentos e quinze reais).
Com burn ~R$ 150K/mês, runway ≈ 0,003 meses. Ou o saldo real não é atualizado
desde sempre, ou faltou um fator de milhar. A aba Projeção, por sua vez, usa
OUTRO caixa inicial hardcoded: `S0=48000` (`template.html:1601`) com texto
"Partindo de ~R$ 48 K hoje". Dois números manuais, conflitantes, nenhum atual.
**Mudança:** (a) Leandro atualiza `saldo_caixa` com o valor real; (b) Projeção
passa a ler o MESMO `caixa_config.json` via marcador — um único ponto manual.

### P0.4 · Conta vencida e NÃO paga entra na DRE como se paga ("real")
`dre_render._build_matriz_2y` (ingestão de `em_aberto`): `kind = "real" if
ym < cutoff else "em_aberto"`. Hoje isso marca como "pago" R$ 95.157,56 que
estão vencidos em aberto (Rômulo 18/18 R$ 59.814 venc jun, provisão Efata jul
R$ 35K, ISS/DCTFWEB miúdos). O mês de junho parece ter desembolsado 59,8K que
nunca saíram do caixa. Mesmo padrão em `_build_matriz` (janela 6m).
**Mudança:** vencido em aberto mantém kind `em_aberto` (fundo azul) em
qualquer mês; nunca herda "real". Opcional: linha "vencido não pago" separada.

### P0.5 · Provisões Efata convivem com NF real → dupla contagem parcial
Provisões "PRESTAÇÃO SERVIÇOS XX/2026" de R$ 35K (jul–out) restauradas pelo
histórico convivem com NFs reais (jul: NF R$ 22.167 paga + provisão R$ 35K em
aberto = R$ 57K num mês cujo custo real é ~R$ 35K). O
`reconcile_em_aberto::PROVISAO_COBERTA` só remove quando realizado ≥ 90% da
provisão (`audit.py:934-940`) — jul não atinge (63%).
**Mudança:** (a) curto prazo: provisão parcialmente coberta vira saldo
remanescente (`max(0, provisão − realizado_mês)`) em vez de tudo-ou-nada, com
trilha no card de reconciliação; (b) definitivo: conferir no Bling se essas
provisões antigas (emissão set–nov/25) devem ser inativadas — o bootstrap de
24 meses vai tornar o snapshot autoritativo pra elas: se forem apagadas no
Bling, o merge as poda sozinho.

### P0.6 · Aba Pipeline é uma foto de mar–mai/26 apresentada como atual
`template.html:1262-1300`: `PP_DATA` com 31 oportunidades e valores R$ reais
hardcoded (Baianão, SESI, Hyundai…), KPIs "7 propostas" escritos no HTML
(L594-597), subtítulo "mar–mai/2026" (L586). Nada vem de dado vivo.
**Mudança:** ou alimentar de uma fonte (planilha no Drive → marcador
`@@PP_DATA@@`), ou estampar "snapshot manual de mai/26" bem visível. Dado de
funil com 3 meses de idade sem aviso é pior que não ter a aba.

### P0.7 · Aba Projeção é 100% premissa hardcoded — e desatualizada
`template.html:1598-1617`: `OP=31244, EF=35000, GSTOP=8, BO=59814, S0=48000`,
receita via `<option>` fixo (65/80K), custo extra `10000` inline sem rótulo.
O acordo Geremias (R$ 10K/mês até ago/27) não é modelado explicitamente — o
`g=10000` até ago/27 coincide por acaso, sem nome. Despesa operacional real
2026 ≈ R$ 75-90K/mês; `OP+EF = 66K` está otimista.
**Mudança:** gerar as premissas no Python a partir dos dados (média
operacional YTD real, parcelas do acordo restantes do próprio Bling, caixa do
`caixa_config.json`) e injetar via marcador único `@@PROJ_PREMISSAS@@`.

### P0.8 · Textos que afirmam fatos numéricos falsos
- `template.html:301` — KPI "Rômulo Lima · 4 parcelas" (foram 18).
- `template.html:626` — pill "Buy-out encerra jun/26" hardcoded.
- `template.html:684` — footer "02/mai/2026" fixo (existe `@@UPDATED_AT@@`).
- `template.html:572` — coluna "Uso abr/26" fixa.
- Nenhuma menção ao acordo trabalhista (20 parcelas → ago/27) em texto algum.
**Mudança:** varrer pra marcador ou remover.

### P0.9 · Card "próximas ações": maior atraso sempre zero
`build-html.py:1854` lê `r.get("dias", 0)` mas `compute_cx_data` (L428-435)
nunca cria a chave `dias`. O texto degrada pra "Atraso máximo: vencido".
**Mudança:** calcular `dias` no `compute_cx_data` (hoje − vencimento).

---

## P1 — semântica inconsistente entre visões

### P1.1 · "Pago" é alocado pelo VENCIMENTO, não pela data de pagamento
`fetch-bling.py::CONTAS_COLS` não traz `dataPagamento`; toda visão usa
`vencimento[:7]`. Título pago com 40 dias de atraso é creditado ao mês errado.
A legenda do DRE diz "Real (pago/recebido)" — na prática é regime de
vencimento. **Mudança:** adicionar `dataPagamento` ao fetch (o detalhe da API
v3 traz; verificar no primeiro run) e usar nas visões de caixa; ou renomear a
legenda. Decisão de regime, não de código.

### P1.2 · Mês corrente: três regras diferentes = três números diferentes
- `compute_dre_data` L1419-23: mês corrente = `projetado` (soma real+aberto);
- `compute_dre_detail` L1501-07: `projetado` só se `m > cutoff` — a abertura
  não bate com a barra;
- `render_cashflow_html` L774-76: mês corrente usa SÓ em aberto — descarta o
  realizado do mês (ago/26 aparece menor do que já se moveu).
**Mudança:** regra única "mês corrente = realizado + aberto restante, tipo
`parcial`" num helper compartilhado.

### P1.3 · Duas fontes de verdade de fornecedor divergindo
`KNOWN_SUPPLIERS` (build-html) × `FORNECEDOR_MAP` (dre_render). Já divergem:
ISABEL CRISTINA FELIX DA SILVA ME cai em Pessoal no build-html e em
desp_admin/Administrativas no DRE (R$ 2.279). LUIZ HENRIQUE AQUINO (reembolso
de visita Efata, R$ 161) e INST ESTUDOS PROTESTO sem mapa nos dois.
**Mudança:** extrair pra `fornecedores_classificacao.json` único (mesmo padrão
do de clientes), consumido pelos dois módulos; card de auditoria "fornecedor
não mapeado" quando valor YTD > R$ 1K.

### P1.4 · Matching de fornecedor por prefixo frouxo
`build-html.py:332`: `key.startswith(name_up[:20])` — pra nomes curtos vira
prefixo curtíssimo e o vencedor depende da ordem do dict. Misclassificação
silenciosa. **Mudança:** alias exato normalizado (upper, sem acento), como o
`_classify` do dre_render.

### P1.5 · Cliente novo nunca aparece como "sem classificação"
No caminho TOTVS (o padrão em produção), `build-html.py:1051-71` auto-rotula
qualquer cliente sem override como garantido/medio/alto — `@@CLI_VAL_SEM@@` é
sempre 0 e ninguém fica sabendo que um cliente novo nunca foi revisado.
**Mudança:** marcar origem `auto` vs `manual` e expor contagem "auto" no KPI.

### P1.6 · `compute_last_4_months` ignora a data do snapshot
`build-html.py:339` usa `date.today()` — rodar build de snapshot antigo gera
janela do relógio, não do dado. **Mudança:** receber `today` do snapshot
(`_metadata.today`), como o dre_render já faz.

### P1.7 · Cores do "a pagar mensal" por nome hardcoded
`build-html.py:602-606`: `if "ROMULO"... red / "EFATA"... amber` — inclui
ATACADÃO, que é cliente. **Mudança:** derivar cor da categoria do
`fornecedores_classificacao.json`.

### P1.8 · Horizonte assimétrico no gráfico DRE
Despesas lançadas até ago/27, receita contratada para em set/26 → gráfico
desenha "buraco de receita" out–dez/26 que é ausência de contrato, não
previsão de receita zero. **Mudança:** banner "receita contratada até X" ou
cortar horizonte no último mês com receita + N.

### P1.9 · `compute_cx_data` usa `valor` onde toda outra visão usa `saldo`
`build-html.py:404`. Hoje inócuo (0 parciais na base), mas o próprio código
detecta situação "Parcial" (L420-23) e exibe o valor cheio. Quando houver
recebimento parcial, KPI e drill-down vão discordar. Fix de 1 linha.

---

## P2 — higiene e riscos latentes

- **`parse_money` sem guarda de formato** (`build-html.py:46`, idêntico no
  dre_render): `"1234.56"` americano viraria 123456 silenciosamente. Adicionar
  heurística (se tem `.` e não tem `,` e ≤2 casas → decimal americano) + teste.
- **Limiares mágicos espalhados**: 5.000/10.000/3.000 (cortes de alerta),
  35%/50% (concentração), 3/6 meses (runway) — mover pra bloco `CONFIG` no
  topo do build-html com nomes.
- **`PAGES` desalinhado** (`template.html:707`): faltam `contas`/`audit` →
  realce de aba mobile dessincroniza (bug visual).
- **Seed estático do donut de caixa** (`template.html:777`) pisca valores de
  mai/26 no primeiro paint antes do `cxRender()` sobrescrever.
- **`PWD_HASH`**: SHA-256 no fonte + repo público = proteção de fachada
  (aceitável se for só anti-curioso; não tratar como controle de acesso).
- **Dead code**: `fBRx` com `'R $ '` quebrado (`template.html:772`).
- **`weekly.sh` deprecado** ainda presente e funcional — risco de alguém rodar
  com token local inválido; adicionar aviso/`exit` no topo.

---

## O que NÃO precisa mudar (verificado ok)

- Alíquotas e agregações tributárias (`audit.py::CONFIG`) — cobertas por
  13 testes, homologadas com Serrano. Não tocar sem ele.
- `reconcile_em_aberto` DUPLICATA_PAGA e `dedupe_receber` — determinísticos,
  idempotentes, testados.
- Os 14 `.find()` do template — todos com fallback (bug §6.9 não regrediu).
- `merge_historico` + gap fill desligado (mudanças de 21/ago) — testados,
  idempotentes, gates verdes.
- Fluxo mensal (`render_cashflow_html`) é a visão de caixa mais correta do
  sistema (exceto item P1.2 do mês corrente).

## Ordem sugerida de execução

1. **Dado manual (5 min, Leandro):** atualizar `caixa_config.json` com o saldo
   real; conferir no Bling as provisões Efata XX/2026 e inativar se substituídas.
2. **Lote A (código, baixo risco):** P0.1, P0.4, P0.9, P1.9, P1.6 + textos P0.8.
3. **Lote B (decisão de semântica + código):** P0.2 (aporte), P0.5 (provisão
   parcial), P1.1 (dataPagamento), P1.2 (mês corrente).
4. **Lote C (refactor):** P1.3/P1.4 (JSON único de fornecedores), P0.7
   (Projeção data-driven), P0.6 (Pipeline), P2.
5. Cada lote entra com teste correspondente no gate (`test_build.py` /
   `test_audit.py`) antes do push — regra §7 do CLAUDE.md.
