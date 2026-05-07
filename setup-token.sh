#!/bin/bash
# Salva sua PAT do GitHub em ~/Documents/Easy_Financas/.git-token-store
# para que git push funcione sem precisar do Keychain do macOS.
# A PAT NÃO aparece na tela enquanto você digita.

set -e
cd "$(dirname "$0")"

echo "──────────────────────────────────────────────"
echo "  Setup do token GitHub para Easy_Financas"
echo "──────────────────────────────────────────────"
echo ""
echo "Cole sua Personal Access Token abaixo (não vai aparecer na tela)"
echo "e aperte Enter:"
echo ""
read -rs TOKEN
echo ""

if [ -z "$TOKEN" ]; then
  echo "Token vazio. Cancelado."
  exit 1
fi

# Formato esperado pelo git credential.helper store
echo "https://leandrogelpke:${TOKEN}@github.com" > .git-token-store
chmod 600 .git-token-store

echo "✓ Token salvo em .git-token-store (modo 600)"
echo "✓ .gitignore já protege esse arquivo do commit"
echo ""
echo "Testando conectividade…"
if git ls-remote origin HEAD > /dev/null 2>&1; then
  echo "✓ git ls-remote funcionou — token é válido."
  echo ""
  echo "Pronto! Da próxima vez não precisa mais ir no Terminal — eu faço push direto."
else
  echo "⚠ git ls-remote falhou. Verifique se a PAT tem scope 'repo'."
fi
