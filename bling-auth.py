#!/usr/bin/env python3
"""
bling-auth.py - Fluxo inicial OAuth2 do Bling API v3.

Uso:
    python3 ~/Documents/Easy_Financas/bling-auth.py

O que faz:
  1. Le client_id/client_secret de .bling-oauth.json
  2. Sobe um servidor HTTP local em localhost:8080 para capturar o callback
  3. Abre seu browser na URL de autorizacao do Bling
  4. Apos voce clicar 'Permitir', recebe o 'code' no callback
  5. Troca o code por access_token + refresh_token
  6. Salva tudo em .bling-tokens.json (chmod 600)

Depois disso, fetch-bling.py renova access_token automaticamente via refresh_token.
"""
import base64
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CONFIG_DIR = Path.home() / "Documents" / "Easy_Financas"
OAUTH_FILE = CONFIG_DIR / ".bling-oauth.json"
TOKENS_FILE = CONFIG_DIR / ".bling-tokens.json"

AUTHORIZE_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"  # authorize é página web, segue no www
TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"
CALLBACK_PORT = 8080

# globais para o handler comunicar com a thread principal
_received = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _received["code"] = params.get("code", [None])[0]
        _received["state"] = params.get("state", [None])[0]
        _received["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _received["error"]:
            body = f"<h1>Erro: {_received['error']}</h1><p>Pode fechar esta aba.</p>"
        else:
            body = "<h1>Autorizacao recebida</h1><p>Voce ja pode fechar esta aba e voltar pro terminal.</p>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # silencia os logs do BaseHTTPRequestHandler


def main():
    if not OAUTH_FILE.exists():
        print(f"ERRO: nao achei {OAUTH_FILE}", file=sys.stderr)
        sys.exit(1)
    cfg = json.loads(OAUTH_FILE.read_text())
    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]
    redirect_uri = cfg.get("redirect_uri", f"http://localhost:{CALLBACK_PORT}/callback")

    state = secrets.token_urlsafe(16)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "state": state,
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    # sobe servidor em thread — allow_reuse_address precisa estar na CLASSE
    # antes do bind, senão a porta fica em TIME_WAIT entre re-runs rápidos.
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[ok] listener subiu em http://localhost:{CALLBACK_PORT}/callback")

    print(f"[ok] abrindo browser em:\n     {auth_url}\n")
    webbrowser.open(auth_url)
    print("Aguardando voce autorizar no Bling (timeout 5min)...")

    # espera o callback
    timeout = 300  # 5 min
    elapsed = 0
    while not _received.get("code") and not _received.get("error") and elapsed < timeout:
        threading.Event().wait(1)
        elapsed += 1
    server.shutdown()

    if _received.get("error"):
        print(f"[falha] Bling devolveu erro: {_received['error']}", file=sys.stderr)
        sys.exit(2)
    if not _received.get("code"):
        print("[falha] Timeout esperando callback.", file=sys.stderr)
        sys.exit(3)
    if _received.get("state") != state:
        print(f"[falha] state mismatch (esperado {state}, recebido {_received.get('state')})", file=sys.stderr)
        sys.exit(4)

    code = _received["code"]
    print(f"[ok] code recebido (len={len(code)}). Trocando por tokens...")

    # troca code por tokens
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[falha] HTTP {e.code} no /token:\n{e.read().decode()}", file=sys.stderr)
        sys.exit(5)

    # adiciona timestamp pra controlar expiracao
    import time
    tokens["_obtained_at"] = int(time.time())
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    os.chmod(TOKENS_FILE, 0o600)
    print(f"[ok] tokens salvos em {TOKENS_FILE}")
    print(f"     access_token expira em {tokens.get('expires_in', '?')}s")
    print(f"     refresh_token (~30 dias) tambem salvo")


if __name__ == "__main__":
    main()
