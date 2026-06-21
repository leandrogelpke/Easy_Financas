#!/usr/bin/env python3
"""
drive_pull.py — Puxa os relatórios Totvs (Comissões) e as faturas de Cartão
do Google Drive DENTRO do GitHub Actions, para que o dashboard fique 100%
online sem depender da máquina local.

Substitui as etapas 1 e 1.5 do runbook local (weekly.sh): em vez de o agente
Cowork baixar do Drive e commitar os snapshots, o próprio CI autentica via
SERVICE ACCOUNT do Google, faz o walk das pastas e materializa os arquivos em
`totvs/raw/` e `cartao/raw/`. Depois o update.yml roda fetch-totvs.py /
fetch-cartao.py por cima desses raws.

Credencial:
    Secret do GitHub `GDRIVE_SA_KEY` = JSON da service account (com Drive API
    habilitada). As DUAS pastas do Drive precisam estar COMPARTILHADAS (leitor)
    com o e-mail da service account. O update.yml grava o secret em arquivo e
    aponta GOOGLE_APPLICATION_CREDENTIALS / GDRIVE_SA_KEY_FILE para ele.

NO-OP seguro: se a credencial não estiver presente, o script imprime [skip] e
sai com 0 SEM tocar em nada — assim o CI continua usando os snapshots já
commitados (comportamento "se não houver arquivo novo, mantém o que está").

Saída de status: escreve `ci/.drive_pull_status.json` com as contagens, que o
workflow usa para decidir se vale regenerar os snapshots (nunca regenera a
partir de raw vazio — isso apagaria dados).

Pastas (fixas):
  Comissoes_Totvs : 1hGxpUGo3Bu5mMl7C1OIe0iXZLEwbmZPq   (flat)
  Cartao_Credito  : 1CojhJ7BUW4na5CQZIYdXJglsh3YyCffs   (walk recursivo: ANO/MM.AAAA)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOTVS_FOLDER_ID = "1hGxpUGo3Bu5mMl7C1OIe0iXZLEwbmZPq"
CARTAO_FOLDER_ID = "1CojhJ7BUW4na5CQZIYdXJglsh3YyCffs"
FOLDER_MIME = "application/vnd.google-apps.folder"
STATUS_FILE = Path("ci/.drive_pull_status.json")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_credentials():
    """Carrega a service account de GDRIVE_SA_KEY_FILE (caminho),
    GOOGLE_APPLICATION_CREDENTIALS (caminho) ou GDRIVE_SA_KEY (JSON inline).
    Retorna o objeto Credentials ou None se nada estiver configurado."""
    try:
        from google.oauth2 import service_account
    except ImportError:
        _log("[erro] google-auth ausente — `pip install google-auth google-api-python-client`")
        return None

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    for var in ("GDRIVE_SA_KEY_FILE", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = os.environ.get(var)
        if p and Path(p).is_file():
            return service_account.Credentials.from_service_account_file(p, scopes=scopes)
    inline = os.environ.get("GDRIVE_SA_KEY")
    if inline and inline.strip().startswith("{"):
        info = json.loads(inline)
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return None


def _list_children(service, folder_id: str):
    """Lista TODOS os filhos diretos de uma pasta (pagina até o fim)."""
    out, page_token = [], None
    q = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = (
            service.files()
            .list(
                q=q,
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def _download(service, file_id: str, dest: Path, magic: bytes) -> bool:
    """Baixa file_id para dest, validando o magic byte. Retorna True se ok."""
    from googleapiclient.http import MediaIoBaseDownload
    import io

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    data = buf.getvalue()
    if not data.startswith(magic):
        _log(f"  ! magic inválido em {dest.name} (esperava {magic!r}) — ignorado")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    _log(f"  + {dest.name} ({len(data)} bytes)")
    return True


def pull_totvs(service, dest_dir: Path) -> int:
    """Pasta flat: baixa .xlsx e .eml (pula Google-native e test_*.txt)."""
    n = 0
    for f in _list_children(service, TOTVS_FOLDER_ID):
        name, mime = f["name"], f.get("mimeType", "")
        if mime == FOLDER_MIME or name.startswith("test_"):
            continue
        low = name.lower()
        if low.endswith(".xlsx"):
            if _download(service, f["id"], dest_dir / name, b"PK\x03\x04"):
                n += 1
        elif low.endswith(".eml"):
            # .eml é texto (não tem magic fixo) — baixa sem validar magic
            if _download_text(service, f["id"], dest_dir / name):
                n += 1
    return n


def _download_text(service, file_id: str, dest: Path) -> bool:
    from googleapiclient.http import MediaIoBaseDownload
    import io

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    data = buf.getvalue()
    if len(data) < 100:
        _log(f"  ! {dest.name} muito pequeno ({len(data)}b) — ignorado")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    _log(f"  + {dest.name} ({len(data)} bytes)")
    return True


def pull_cartao(service, dest_dir: Path) -> int:
    """Walk recursivo (ANO/MM.AAAA): baixa todo .pdf (flat em dest_dir)."""
    n = 0
    queue = [CARTAO_FOLDER_ID]
    seen = set()
    while queue:
        fid = queue.pop()
        if fid in seen:
            continue
        seen.add(fid)
        for f in _list_children(service, fid):
            name, mime = f["name"], f.get("mimeType", "")
            if mime == FOLDER_MIME:
                queue.append(f["id"])
            elif name.lower().endswith(".pdf") and not name.startswith("test_"):
                if _download(service, f["id"], dest_dir / name, b"%PDF"):
                    n += 1
    return n


def main() -> int:
    creds = _load_credentials()
    if creds is None:
        _log("[skip] credencial GDRIVE_SA_KEY ausente — mantendo snapshots commitados (nada baixado).")
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps({"skipped": True, "totvs": 0, "cartao": 0}))
        return 0

    try:
        from googleapiclient.discovery import build
    except ImportError:
        _log("[erro] google-api-python-client ausente.")
        return 1

    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    totvs_dir = Path("totvs/raw")
    cartao_dir = Path("cartao/raw")
    totvs_dir.mkdir(parents=True, exist_ok=True)
    cartao_dir.mkdir(parents=True, exist_ok=True)

    _log("== Drive pull: Comissões Totvs ==")
    n_totvs = pull_totvs(service, totvs_dir)
    _log(f"   {n_totvs} arquivo(s) Totvs")

    _log("== Drive pull: Cartão (walk recursivo) ==")
    n_cartao = pull_cartao(service, cartao_dir)
    _log(f"   {n_cartao} PDF(s) de cartão")

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({"skipped": False, "totvs": n_totvs, "cartao": n_cartao}))
    _log(f"[ok] drive pull concluído: {n_totvs} Totvs + {n_cartao} cartão")
    return 0


if __name__ == "__main__":
    sys.exit(main())
