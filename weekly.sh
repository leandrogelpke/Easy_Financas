#!/usr/bin/env bash
# weekly.sh — runbook semanal automatizado.
# Rodar via: bash ~/Documents/Easy_Financas/weekly.sh
# Ou em sessao Cowork: "rode o runbook semanal" e eu invoco isso pela bash.

set -euo pipefail

# Resolver paths — Cowork sandbox tem prioridade pq o mount pode estar
# montado simultaneamente com o $HOME local (caso da sessao agentica).
# A heuristica: se EF nativo existir E contiver os .py, usa-o; senao, /sessions.
if [ -d "$HOME/Documents/Easy_Financas" ] && [ -f "$HOME/Documents/Easy_Financas/fetch-bling.py" ]; then
    EF="$HOME/Documents/Easy_Financas"
    PROD="$HOME/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais/bling-api"
elif [ -d "/sessions" ]; then
    SESSION_DIR=$(ls -d /sessions/*/mnt 2>/dev/null | head -1 || true)
    if [ -z "$SESSION_DIR" ]; then
        echo "ERRO: /sessions existe mas sem mount montado" >&2
        exit 1
    fi
    EF="$SESSION_DIR/Easy_Financas"
    PROD="$SESSION_DIR/relatorios atuais/bling-api"
else
    echo "ERRO: nao consegui localizar Easy_Financas" >&2
    exit 1
fi

# Sanity check — falha cedo, com mensagem util
if [ ! -f "$EF/fetch-bling.py" ]; then
    echo "ERRO: $EF/fetch-bling.py nao existe — caminho EF resolvido errado?" >&2
    exit 1
fi
if [ ! -d "$PROD" ] && [ "${ALLOW_MKDIR_PROD:-0}" = "1" ]; then
    mkdir -p "$PROD"
fi

# TMP criado e trap registrada imediatamente — se script morrer, limpa
TMP=$(mktemp -d /tmp/ef-push-XXXXXX)
trap 'rm -rf "$TMP"' EXIT

echo "[paths] EF=$EF"
echo "[paths] PROD=$PROD"
echo ""

echo "=== 1. pull dos dados Bling ==="
# data-inicial = 7 meses atrás (janela deslizante automática)
DATA_INICIAL=$(python3 -c "
from datetime import date, timedelta
d = date.today().replace(day=1)
for _ in range(7):
    d = (d - timedelta(days=1)).replace(day=1)
print(d.strftime('%Y-%m-%d'))
") || { echo 'ERRO: nao consegui calcular DATA_INICIAL' >&2; exit 1; }
echo "[fetch] janela: $DATA_INICIAL → hoje"
# nota: --no-detail-pagar-aberto removido em jun/2026 — precisamos do
# GET /id pra trazer dataEmissao (data de lançamento) das contas a pagar
# em aberto, exibida na aba Caixa. Cache .cache/pagar_sit1.json retoma.
python3 "$EF/fetch-bling.py" \
    --out "$PROD" \
    --data-inicial "$DATA_INICIAL" \
    --no-detail-receber

echo ""
echo "=== 1b. processando relatórios Totvs (Drive=fonte-da-verdade quando manifest presente) ==="
mkdir -p "$EF/totvs/raw"
# Manifest do Drive: escrito pelo agente Cowork na etapa 1 (Drive pull).
# Se existir, fetch-totvs.py opera em modo Drive=truth: snapshot regenerado
# do zero contendo só docs cujo .eml/.xlsx esteja em Drive.
# Se não existir (ex.: rodada manual do Mac sem passar pelo Cowork), fetch-totvs.py
# cai no modo legado (cumulativo sobre tudo em raw/).
MANIFEST="$EF/totvs/drive_manifest.json"
python3 "$EF/fetch-totvs.py" \
    --in "$EF/totvs/raw" \
    --out "$PROD/totvs_snapshot.json" \
    --drive-manifest "$MANIFEST" || \
  echo "[warn] fetch-totvs.py falhou (segue mesmo assim)"

# ── BRIDGE TEMPORÁRIO: Totvs maio/26 da planilha interna ────────────
# O fetch-totvs acima regenera o snapshot do zero e NÃO conhece a planilha
# interna de faturamento — então reinjetamos a competência maio/26 aqui.
# `--only-if-absent` faz isto se auto-desativar: quando o Relatório de
# Comissão ASE806 oficial de maio chegar em totvs/raw (e virar doc oficial),
# o bridge para de injetar e remove o doc interno. REMOVER este bloco quando
# o oficial estiver consolidado. Ver: ingest_totvs_interno.py.
CTRL="$(dirname "$(dirname "$PROD")")"   # .../Controles Easy
for cand in \
    "$CTRL/FATURAMENTO_05.2026.xlsx" \
    "$EF/totvs/raw/FATURAMENTO_05.2026.xlsx" \
    "$(ls -d /sessions/*/mnt/FATURAMENTO_05.2026.xlsx 2>/dev/null | head -1)"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then
        python3 "$EF/ingest_totvs_interno.py" \
            --xlsx "$cand" --competencia 2026-05 \
            --snapshot "$PROD/totvs_snapshot.json" \
            --only-if-absent \
          && echo "[totvs] bridge maio/26 aplicado (fonte: $(basename "$cand"))" \
          || echo "[warn] bridge totvs maio/26 falhou (segue mesmo assim)"
        break
    fi
done

echo ""
echo "=== 1b2. processando faturas de Cartão de Crédito (PDF Bradesco) ==="
# Fonte-da-verdade: $EF/cartao/raw/ — onde o agente Cowork deposita os PDFs
# baixados do Drive (pasta Cartao_Credito) na etapa 1.5 do runbook. Esse
# diretório persiste e acumula histórico (jan/26+). Idempotente.
# Backfill opcional: se a pasta local de faturas estiver montada (Mac nativo
# ou mount Cowork), copia PDFs ainda não presentes em cartao/raw/.
CARTAO_RAW="$EF/cartao/raw"
mkdir -p "$CARTAO_RAW"
for cand in \
    "$HOME/Documents/GRAP/Negociação Easy/Controles Easy/Cartão de Crédito 2026/2026" \
    "$(ls -d /sessions/*/mnt/2026 2>/dev/null | head -1)"; do
    if [ -n "$cand" ] && [ -d "$cand" ]; then
        # cp -n: não sobrescreve; -r p/ pegar subpastas MM.AAAA
        find "$cand" -type f \( -iname '*.pdf' \) -print0 2>/dev/null | \
            while IFS= read -r -d '' f; do
                cp -n "$f" "$CARTAO_RAW/" 2>/dev/null || true
            done
        echo "[cartao] backfill da pasta local: $cand"
    fi
done
n_pdf=$(find "$CARTAO_RAW" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
if [ "$n_pdf" -gt 0 ]; then
    python3 "$EF/fetch-cartao.py" --in "$CARTAO_RAW" \
        --out "$EF/cartao_snapshot.json" \
      && echo "[cartao] snapshot atualizado ($n_pdf PDFs em cartao/raw/)" \
      || echo "[warn] fetch-cartao.py falhou (segue com snapshot anterior)"
else
    echo "[cartao] sem PDFs em cartao/raw/ — mantém snapshot anterior"
fi

echo ""
echo "=== 1c. testes de regressão (audit) ==="
# Trava a rodada se algum teste do audit.py quebrar — protege contra mudanças
# que invalidem alíquotas ou parsers de competência (ver test_audit.py).
if [ -f "$EF/test_audit.py" ]; then
    if ! python3 "$EF/test_audit.py"; then
        echo "[ERRO] test_audit.py falhou — interrompendo rodada." >&2
        echo "       corrigir os testes/lógica antes de gerar novo index.html." >&2
        exit 2
    fi
else
    echo "[warn] test_audit.py não encontrado em $EF — pulando gate"
fi

echo ""
echo "=== 2. regenerando index.html (inclui auditoria tributária) ==="
# Passa TOTVS_SNAP explicito porque o default do build-html.py assume
# layout Mac nativo (~/Documents/GRAP/...) que nao bate com o mount Cowork.
export TOTVS_SNAP="$PROD/totvs_snapshot.json"
export CARTAO_SNAP="$EF/cartao_snapshot.json"
python3 "$EF/build-html.py" --in "$PROD" --out "$EF/index.html"

echo ""
echo "=== 2c. testes estruturais do HTML gerado (test_build) ==="
# Pega marcadores @@...@@ não substituídos, divs desbalanceadas por aba,
# canvas sumido, tamanho mínimo do index. Bloqueia push se quebrar.
# Ver §6.2 do CLAUDE.md — bug histórico de </div> faltando escondia 9 abas.
if [ -f "$EF/test_build.py" ]; then
    if ! python3 "$EF/test_build.py"; then
        echo "[ERRO] test_build.py falhou — interrompendo rodada." >&2
        echo "       corrija o template antes de gerar novo index.html." >&2
        exit 3
    fi
else
    echo "[warn] test_build.py não encontrado em $EF — pulando gate"
fi

# Resumo da auditoria pro log (audit_findings.json é gravado por build-html)
if [ -f "$PROD/audit_findings.json" ]; then
    python3 -c "
import json, sys
d = json.load(open('$PROD/audit_findings.json'))
n = d['n_findings']
by = d['by_status']
print()
print('=== 2b. resumo da auditoria tributaria ===')
print(f'[audit] total: {n} checks')
print(f'  erros (acao imediata): {by.get(\"error\",0)}')
print(f'  atencoes:              {by.get(\"warn\",0)}')
print(f'  info (provisoes/etc):  {by.get(\"info\",0)}')
print(f'  em ordem:              {by.get(\"ok\",0)}')
errs = [f for f in d['findings'] if f['status']=='error'][:5]
if errs:
    print()
    print('  top 5 erros:')
    for f in errs:
        print(f'   - {f[\"title\"]}')
" || true
fi

# Resumo de contas a pagar / receber (contas_snapshot.json gravado por build-html)
if [ -f "$PROD/contas_snapshot.json" ]; then
    python3 -c "
import json
d = json.load(open('$PROD/contas_snapshot.json'))
t = d['totais']
horizon = d['horizon_meses']
hr = d['horizon_range']
def brl(v):
    s = f'{abs(v):,.0f}'.replace(',', '.')
    return f'R\$ {s}' if v >= 0 else f'(R\$ {s})'
print()
print('=== 2c. resumo contas a pagar / a receber ({h}m: {a} -> {b}) ==='.format(
    h=horizon, a=hr[0], b=hr[1]))
print(f'[contas] A RECEBER ({len(d[\"clientes\"])} clientes)')
print(f'  Lancado:  {brl(t[\"receber_lancado\"])}')
print(f'  Previsto: {brl(t[\"receber_previsto\"])}')
print(f'  Vencido:  {brl(t[\"receber_vencido\"])} {\"<-- atencao\" if t[\"receber_vencido\"]>0 else \"\"}')
print()
print(f'[contas] A PAGAR ({len(d[\"fornecedores\"])} fornecedores)')
print(f'  Lancado:  {brl(t[\"pagar_lancado\"])}')
print(f'  Previsto: {brl(t[\"pagar_previsto\"])}')
print(f'  Vencido:  {brl(t[\"pagar_vencido\"])} {\"<-- atencao\" if t[\"pagar_vencido\"]>0 else \"\"}')
print()
gap = t['gap_3m']
print(f'[contas] GAP {horizon}m (receber - pagar): {brl(gap)} {\"OK\" if gap>=0 else \"<-- deficit projetado\"}')
# Top 3 fornecedores e clientes com previsao (oportunidade de antecipar)
print()
top_p = sorted([c for c in d['fornecedores'] if c['total_horizonte']>0],
               key=lambda c:-c['total_horizonte'])[:3]
print('[contas] top 3 fornecedores ({h}m):'.format(h=horizon))
for c in top_p:
    venc_tag = ' VENCIDO' if c['vencido_total']>0 else ''
    print(f'   - {c[\"nome\"][:42]:42s}  {brl(c[\"total_horizonte\"])} ({c[\"confidence\"]}, {c[\"n_meses_historico\"]}/6m){venc_tag}')
top_c = sorted([c for c in d['clientes'] if c['total_horizonte']>0],
               key=lambda c:-c['total_horizonte'])[:3]
print('[contas] top 3 clientes ({h}m):'.format(h=horizon))
for c in top_c:
    venc_tag = ' VENCIDO' if c['vencido_total']>0 else ''
    print(f'   - {c[\"nome\"][:42]:42s}  {brl(c[\"total_horizonte\"])} ({c[\"confidence\"]}, {c[\"n_meses_historico\"]}/6m){venc_tag}')
" || true
fi

echo ""
echo "=== 3. push pro GitHub Pages (via clone temporario fora do iCloud) ==="
if [ ! -f "$EF/.git-token-store" ]; then
    echo "ERRO: $EF/.git-token-store nao existe — rode setup-token.sh" >&2
    exit 1
fi
git clone --depth 1 https://github.com/leandrogelpke/Easy_Financas.git "$TMP"
cp "$EF/.git-token-store" "$TMP/"
(
    cd "$TMP"
    git config credential.helper "store --file=$TMP/.git-token-store"
    git config user.email "leandrogelpke@gmail.com"
    git config user.name "Leandro Gelpke"
    # copia apenas arquivos publicaveis (sem scripts, credenciais ou docs internos)
    cp "$EF/index.html" . 2>/dev/null || true
    cp "$EF/DRE_Projetado_Realizado.html" . 2>/dev/null || true
    git add .
    if git diff-index --quiet HEAD --; then
        echo "[ok] nada novo pra commitar"
    else
        git commit -m "weekly snapshot $(date +%Y-%m-%d)"
        git push origin main
        echo "[ok] pushed"
    fi
)
# limpeza de snapshots antigos (manter apenas os últimos 4)
echo ""
echo "=== 4. limpeza de snapshots antigos ==="
# `find -print0` + array é robusto a paths com espaço e a estado vazio
SNAPS=()
while IFS= read -r -d '' f; do SNAPS+=("$f"); done < <(
    find "$PROD" -maxdepth 1 -name 'bling_data_*.json' -print0 2>/dev/null | sort -z
)
N_SNAP=${#SNAPS[@]}
if [ "$N_SNAP" -gt 4 ]; then
    N_DEL=$(( N_SNAP - 4 ))
    for ((i=0; i<N_DEL; i++)); do
        rm -f "${SNAPS[i]}" && echo "[limpeza] removido: $(basename "${SNAPS[i]}")"
    done
else
    echo "[limpeza] $N_SNAP snapshots — nenhum removido"
fi

# Boa prática: marca index.html como --skip-worktree na primeira rodada.
# Por que: o build-html.py reescreve index.html toda semana, deixando o
# git status local sempre "dirty" (M index.html). O remoto correto vive
# em origin/main (push acontece via clone temp em /tmp para evitar que o
# iCloud corrompa o .git/ local — não dá pra fazer 'git pull' aqui pelo
# mesmo motivo: iCloud bloqueia escritas em .git/refs e .git/objects).
# A solução padrão é dizer ao git pra ignorar mudanças no working-tree
# desse arquivo. Idempotente: se já estiver setado, não faz nada.
echo ""
echo "=== 5. higiene do repo local (--skip-worktree em index.html) ==="
if git -C "$EF" rev-parse --git-dir >/dev/null 2>&1; then
    if git -C "$EF" ls-files --stage index.html >/dev/null 2>&1; then
        already=$(git -C "$EF" ls-files -v index.html 2>/dev/null | grep -c '^S' || true)
        if [ "$already" = "0" ]; then
            if git -C "$EF" update-index --skip-worktree index.html 2>/dev/null; then
                echo "[ok] index.html marcado como skip-worktree (git status agora fica limpo)"
            else
                echo "[warn] update-index bloqueado pelo iCloud (esperado quando rodando da sandbox Cowork)."
                echo "[warn] Pra eliminar o 'M index.html' do git status, rode UMA VEZ no Terminal nativo do Mac:"
                echo "[warn]   cd ~/Documents/Easy_Financas && git update-index --skip-worktree index.html"
            fi
        else
            echo "[ok] index.html já está como skip-worktree"
        fi
    fi
else
    echo "[warn] .git/ não está acessível — pulando"
fi

echo ""
echo "[done] dashboard atualizado em https://leandrogelpke.github.io/Easy_Financas/"
