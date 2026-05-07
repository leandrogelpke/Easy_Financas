#!/usr/bin/env bash
# weekly.sh — runbook semanal automatizado.
# Rodar via: bash ~/Documents/Easy_Financas/weekly.sh
# Ou em sessao Cowork: "rode o runbook semanal" e eu invoco isso pela bash.

set -e

# Resolver paths — funciona tanto no Mac nativo quanto no sandbox Cowork
if [ -d "$HOME/Documents/Easy_Financas" ]; then
    # Mac nativo
    EF="$HOME/Documents/Easy_Financas"
    PROD="$HOME/Documents/GRAP/Negociação Easy/Controles Easy/relatorios atuais/bling-api"
elif [ -d "/sessions" ]; then
    # Sandbox Cowork — encontra a sessao atual
    SESSION_DIR=$(ls -d /sessions/*/mnt 2>/dev/null | head -1)
    EF="$SESSION_DIR/Easy_Financas"
    PROD="$SESSION_DIR/relatorios atuais/bling-api"
else
    echo "ERRO: nao consegui localizar Easy_Financas" >&2
    exit 1
fi
TMP="/tmp/ef-push"

echo "[paths] EF=$EF"
echo "[paths] PROD=$PROD"
echo ""

echo "=== 1. pull dos dados Bling ==="
python3 "$EF/fetch-bling.py" \
    --out "$PROD" \
    --data-inicial 2025-10-01 \
    --no-detail-receber

echo ""
echo "=== 2. regenerando bling-live.html ==="
python3 "$EF/build-html.py" --in "$PROD" --out "$EF/bling-live.html"

echo ""
echo "=== 3. push pro GitHub Pages (via clone temporario fora do iCloud) ==="
rm -rf "$TMP"
git clone --depth 1 https://github.com/leandrogelpke/Easy_Financas.git "$TMP"
cp "$EF/.git-token-store" "$TMP/"
cd "$TMP"
git config credential.helper "store --file=$TMP/.git-token-store"
git config user.email "leandrogelpke@gmail.com"
git config user.name "Leandro Gelpke"
# copia arquivos publicaveis (NAO copia .bling-* que sao gitignored mesmo)
cp "$EF/bling-live.html" . 2>/dev/null || true
cp "$EF/index.html" . 2>/dev/null || true
cp "$EF/DRE_Projetado_Realizado.html" . 2>/dev/null || true
cp "$EF/RUNBOOK.md" . 2>/dev/null || true
cp "$EF/NEXT-SESSION-CONTEXT.md" . 2>/dev/null || true
cp "$EF/weekly.sh" . 2>/dev/null || true
cp "$EF/fetch-bling.py" . 2>/dev/null || true
cp "$EF/build-html.py" . 2>/dev/null || true
cp "$EF/bling-auth.py" . 2>/dev/null || true
git add .
if git diff-index --quiet HEAD --; then
    echo "[ok] nada novo pra commitar"
else
    git commit -m "weekly snapshot $(date +%Y-%m-%d)"
    git push origin main
    echo "[ok] pushed"
fi
echo ""
echo "[done] dashboard atualizado em https://leandrogelpke.github.io/Easy_Financas/bling-live.html"
