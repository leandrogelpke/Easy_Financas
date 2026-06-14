#!/usr/bin/env python3
"""
fetch-bling.py - Baixa dados financeiros do Bling API v3 para alimentar o dashboard.

Uso:
    python3 ~/Documents/Easy_Financas/fetch-bling.py
    python3 ~/Documents/Easy_Financas/fetch-bling.py --out ~/algum/outro/lugar
    python3 ~/Documents/Easy_Financas/fetch-bling.py --quiet

Saidas (em <out>):
    contas_pagar_em_aberto_<YYYY-MM-DD>.csv
    contas_pagar_pagas_<YYYY-MM-DD>.csv
    contas_receber_em_aberto_<YYYY-MM-DD>.csv
    contas_receber_recebidas_<YYYY-MM-DD>.csv
    categorias_receitas_despesas.csv
    contatos.csv
    bling_data_<YYYY-MM-DD>.json   (snapshot consolidado para o build-html.py)

Pre-requisitos:
    .bling-oauth.json   (client_id, client_secret, redirect_uri)
    .bling-tokens.json  (gerado por bling-auth.py - access_token + refresh_token)
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# CONFIG_DIR: pasta com .bling-oauth.json e .bling-tokens.json.
# Usa a pasta do proprio script (suporta rodar em sandbox/cowork e local).
CONFIG_DIR = Path(__file__).resolve().parent
OAUTH_FILE = CONFIG_DIR / ".bling-oauth.json"
TOKENS_FILE = CONFIG_DIR / ".bling-tokens.json"
DEFAULT_OUT = Path.home() / "Documents" / "GRAP" / "Negociação Easy" / "Controles Easy" / "relatorios atuais"
FALLBACK_OUT = CONFIG_DIR / "relatorios"

API_BASE = "https://www.bling.com.br/Api/v3"
TOKEN_URL = f"{API_BASE}/oauth/token"

# Bling API v3 limita a 3 req/s, 120 req/min. Dormimos 0.35s entre chamadas (~2.85 req/s).
SLEEP_BETWEEN = 0.35


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    """Escreve via tmp + rename pra evitar arquivo truncado se o processo
    for interrompido no meio (timeout do bash, kill, etc)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


class BlingClient:
    def __init__(self, oauth_cfg: Dict[str, str], tokens: Dict[str, Any], quiet: bool = False):
        self.client_id = oauth_cfg["client_id"]
        self.client_secret = oauth_cfg["client_secret"]
        self.tokens = tokens
        self.quiet = quiet

    @property
    def access_token(self) -> str:
        return self.tokens["access_token"]

    def is_expired(self, slack_seconds: int = 60) -> bool:
        obtained = self.tokens.get("_obtained_at", 0)
        expires_in = int(self.tokens.get("expires_in", 0))
        return time.time() >= (obtained + expires_in - slack_seconds)

    def refresh(self) -> None:
        log("[token] refreshing access_token...", self.quiet)
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            new = json.loads(resp.read().decode())
        new["_obtained_at"] = int(time.time())
        self.tokens = new
        atomic_write_text(TOKENS_FILE, json.dumps(new, indent=2), mode=0o600)
        log("[token] novo access_token salvo (expires_in=%ss)" % new.get("expires_in"), self.quiet)

    def ensure_fresh_token(self) -> None:
        if self.is_expired():
            self.refresh()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 3) -> Dict[str, Any]:
        self.ensure_fresh_token()
        url = f"{API_BASE}{path}"
        if params:
            # Bling aceita arrays como key[]=v1&key[]=v2
            qs_parts: List[str] = []
            for k, v in params.items():
                if isinstance(v, (list, tuple)):
                    for item in v:
                        qs_parts.append(f"{urllib.parse.quote(k)}[]={urllib.parse.quote(str(item))}")
                else:
                    qs_parts.append(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}")
            url += "?" + "&".join(qs_parts)
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    log("[token] 401, tentando refresh", self.quiet)
                    self.refresh()
                    continue
                if e.code == 429:
                    wait = 2 ** attempt
                    log(f"[rate-limit] HTTP 429, dormindo {wait}s e retry", self.quiet)
                    time.sleep(wait)
                    continue
                # 5xx → retry; outros codigos → repassa
                if 500 <= e.code < 600 and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                body = e.read().decode(errors="replace")
                raise RuntimeError(f"HTTP {e.code} em GET {path}: {body}") from e
        raise RuntimeError(f"GET {path} falhou apos {retries} tentativas")

    def list_all(self, path: str, params: Optional[Dict[str, Any]] = None, page_size: int = 100) -> List[Dict[str, Any]]:
        params = dict(params or {})
        params["limite"] = page_size
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            params["pagina"] = page
            data = self.get(path, params)
            items = data.get("data", [])
            out.extend(items)
            if len(items) < page_size:
                break
            page += 1
            time.sleep(SLEEP_BETWEEN)
        return out


def fmt_money(v: Any) -> str:
    try:
        return f"{float(v):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def write_csv(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, delimiter=";")
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in columns})


SITUACAO_LABELS = {
    1: "Em aberto",
    2: "Paga/Recebida",  # contexto-dependente; situacao_label(sit, kind) resolve
    3: "Parcial",
    4: "Devolvida",
    5: "Cancelada",
}


def situacao_label(sit: int, kind: str) -> str:
    if sit == 2:
        return "Paga" if kind == "pagar" else "Recebida"
    return SITUACAO_LABELS.get(sit, f"sit_{sit}")


def fetch_categorias(client: BlingClient) -> List[Dict[str, Any]]:
    log("[fetch] categorias receitas/despesas...", client.quiet)
    items = client.list_all("/categorias/receitas-despesas")
    log(f"  -> {len(items)} categorias", client.quiet)
    return items


def fetch_contatos_idx(
    client: BlingClient,
    ids: Iterable[int],
    cache_path: Optional[Path] = None,
    save_every: int = 25,
) -> Dict[int, Dict[str, Any]]:
    """Busca detalhes de uma lista de contato_ids. Retorna dict id -> info.
    Resume-friendly: salva cache_path a cada save_every chamadas."""
    ids_unique = sorted({int(i) for i in ids if i})
    out: Dict[int, Dict[str, Any]] = {}
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if isinstance(cached, list):
                out = {int(x["id"]): x for x in cached if x.get("id")}
        except Exception:
            pass
    todo = [c for c in ids_unique if c not in out]
    log(f"[fetch] contatos detalhados ({len(out)} cache + {len(todo)} para buscar)...", client.quiet)
    for i, cid in enumerate(todo):
        try:
            d = client.get(f"/contatos/{cid}").get("data", {})
            out[cid] = {
                "id": cid,
                "nome": d.get("nome", ""),
                "numeroDocumento": d.get("numeroDocumento", ""),
                "tipo": d.get("tipo", ""),
            }
        except RuntimeError as e:
            log(f"  ! contato {cid}: {e}", client.quiet)
            out[cid] = {"id": cid, "nome": "", "numeroDocumento": "", "tipo": ""}
        time.sleep(SLEEP_BETWEEN)
        if (i + 1) % save_every == 0:
            log(f"  ... {len(out)}/{len(ids_unique)}", client.quiet)
            if cache_path:
                atomic_write_text(cache_path, json.dumps(list(out.values()), ensure_ascii=False))
    if cache_path:
        atomic_write_text(cache_path, json.dumps(list(out.values()), ensure_ascii=False))
    return out


def fetch_contas(
    client: BlingClient,
    kind: str,  # "pagar" ou "receber"
    situacao: int,
    detail: bool = True,
    max_items: Optional[int] = None,
    cache_path: Optional[Path] = None,
    save_every: int = 25,
    data_inicial: Optional[str] = None,
    data_final: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Lista resumida + detalhe individual de cada item.
    contas/receber ja vem com contato e contaContabil aninhados.
    contas/pagar so traz id basico, precisa de GET /id pra detalhar.
    Se detail=False, devolve so o que veio da listagem (sem GET por id).

    Se cache_path for passado, salva resultado parcial a cada save_every itens
    e ao retomar, ja descarta os que ja estao no cache.
    """
    log(f"[fetch] contas/{kind} situacao={situacao}...", client.quiet)
    list_params: Dict[str, Any] = {"situacoes": [situacao]}
    if data_inicial:
        list_params["dataEmissaoInicial"] = data_inicial
    if data_final:
        list_params["dataEmissaoFinal"] = data_final
    listed = client.list_all(f"/contas/{kind}", list_params)
    log(f"  -> {len(listed)} registros listados", client.quiet)
    if max_items is not None and len(listed) > max_items:
        listed = listed[:max_items]
        log(f"  (limitando a {max_items} para teste)", client.quiet)
    if not detail:
        return listed

    # Carrega cache parcial se existir
    detailed: List[Dict[str, Any]] = []
    done_ids: set = set()
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if isinstance(cached, list):
                detailed = cached
                done_ids = {int(x["id"]) for x in cached if x.get("id")}
                log(f"  [resume] {len(done_ids)} itens ja em cache", client.quiet)
        except Exception:
            pass

    todo = [x for x in listed if int(x.get("id", -1)) not in done_ids]
    if not todo:
        log(f"  [resume] tudo ja completo (cache hit)", client.quiet)
        return detailed

    for i, item in enumerate(todo):
        try:
            d = client.get(f"/contas/{kind}/{item['id']}").get("data", {})
            # mescla com a versao da lista (que pode ter campos adicionais como contato.nome em receber)
            for k, v in (item or {}).items():
                d.setdefault(k, v)
        except RuntimeError as e:
            log(f"  ! id {item['id']}: {e}", client.quiet)
            d = item
        detailed.append(d)
        time.sleep(SLEEP_BETWEEN)
        if (i + 1) % save_every == 0:
            log(f"  ... {len(detailed)}/{len(listed)}", client.quiet)
            if cache_path:
                atomic_write_text(cache_path, json.dumps(detailed, ensure_ascii=False))
    if cache_path:
        atomic_write_text(cache_path, json.dumps(detailed, ensure_ascii=False))
    return detailed


def enrich_and_dump(
    contas: List[Dict[str, Any]],
    kind: str,
    contatos_idx: Dict[int, Dict[str, Any]],
    cat_idx: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for c in contas:
        contato = c.get("contato") or {}
        cinfo = contatos_idx.get(int(contato.get("id") or 0), {}) if contato.get("id") else {}
        cat = c.get("categoria") or {}
        catinfo = cat_idx.get(int(cat.get("id") or 0), {}) if cat.get("id") else {}
        portador = c.get("portador") or {}
        rows.append({
            "id": c.get("id"),
            "situacao_codigo": c.get("situacao"),
            "situacao": situacao_label(c.get("situacao", 0), kind),
            "vencimento": c.get("vencimento", ""),
            "vencimento_original": c.get("vencimentoOriginal", ""),
            "data_emissao": c.get("dataEmissao", ""),
            "competencia": c.get("competencia", ""),
            "valor": fmt_money(c.get("valor", "")),
            "saldo": fmt_money(c.get("saldo", "")),
            "numero_documento": c.get("numeroDocumento", ""),
            "historico": c.get("historico", ""),
            "contato_id": contato.get("id", ""),
            "contato_nome": cinfo.get("nome") or (contato.get("nome", "")),
            "contato_documento": cinfo.get("numeroDocumento") or contato.get("numeroDocumento", ""),
            "categoria_id": cat.get("id", ""),
            "categoria_descricao": catinfo.get("descricao", ""),
            "categoria_tipo": catinfo.get("tipo", ""),
            "portador_id": portador.get("id", ""),
        })
    return rows


CONTAS_COLS = [
    "id", "situacao_codigo", "situacao",
    "vencimento", "vencimento_original", "data_emissao", "competencia",
    "valor", "saldo",
    "numero_documento", "historico",
    "contato_id", "contato_nome", "contato_documento",
    "categoria_id", "categoria_descricao", "categoria_tipo",
    "portador_id",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None, help="pasta de destino (default: relatorios atuais)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skip-pagas", action="store_true", help="pular contas pagar/receber com situacao=2 (mais lento)")
    ap.add_argument("--limit", type=int, default=None, help="limita N itens por dataset (smoke test)")
    ap.add_argument("--no-detail-receber", action="store_true",
                    help="receber ja vem com info rica na lista; pula GET /id pra acelerar")
    ap.add_argument("--no-detail-pagar-aberto", action="store_true",
                    help="pular GET /id para contas_pagar_em_aberto (ja tem vencimento+valor na lista, muito mais rapido)")
    ap.add_argument("--data-inicial", default=None,
                    help="filtra contas pagar/receber pelas datas de emissao a partir desta (YYYY-MM-DD)")
    ap.add_argument("--data-final", default=None,
                    help="filtra contas pagar/receber pelas datas de emissao ate esta (YYYY-MM-DD)")
    args = ap.parse_args()

    if not OAUTH_FILE.exists():
        print(f"ERRO: {OAUTH_FILE} nao encontrado", file=sys.stderr)
        return 1
    if not TOKENS_FILE.exists():
        print(f"ERRO: {TOKENS_FILE} nao encontrado. Rode bling-auth.py uma vez.", file=sys.stderr)
        return 1

    out_dir = args.out
    if out_dir is None:
        out_dir = DEFAULT_OUT if DEFAULT_OUT.exists() else FALLBACK_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    oauth_cfg = json.loads(OAUTH_FILE.read_text())
    tokens = json.loads(TOKENS_FILE.read_text())
    client = BlingClient(oauth_cfg, tokens, quiet=args.quiet)

    # 1) categorias (para enriquecer contas)
    categorias = fetch_categorias(client)
    cat_idx = {int(c["id"]): c for c in categorias}
    write_csv(
        out_dir / "categorias_receitas_despesas.csv",
        categorias,
        ["id", "idCategoriaPai", "descricao", "tipo", "idGrupoDre"],
    )

    # 2) contas a pagar e a receber em aberto + pagas/recebidas
    fetched = {}
    plan = [
        ("pagar", 1, "contas_pagar_em_aberto"),
        ("receber", 1, "contas_receber_em_aberto"),
    ]
    if not args.skip_pagas:
        plan += [
            ("pagar", 2, "contas_pagar_pagas"),
            ("receber", 2, "contas_receber_recebidas"),
        ]

    cache_dir = out_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for kind, sit, label in plan:
        skip_detail_receber = kind == "receber" and args.no_detail_receber
        skip_detail_pagar_aberto = kind == "pagar" and sit == 1 and getattr(args, "no_detail_pagar_aberto", False)
        do_detail = not (skip_detail_receber or skip_detail_pagar_aberto)
        cache_path = cache_dir / f"{kind}_sit{sit}.json"
        fetched[(kind, sit)] = fetch_contas(
            client, kind, sit,
            detail=do_detail,
            max_items=args.limit,
            cache_path=cache_path if do_detail else None,
            data_inicial=args.data_inicial,
            data_final=args.data_final,
        )

    # 3) coletar contato_ids de tudo e buscar detalhe (se ainda nao veio)
    contato_ids: set[int] = set()
    for items in fetched.values():
        for c in items:
            cid = (c.get("contato") or {}).get("id")
            if cid:
                contato_ids.add(int(cid))
    # contas/receber ja traz nome aninhado; mas vamos garantir que tudo tenha nome
    contatos_idx = fetch_contatos_idx(client, contato_ids, cache_path=cache_dir / "contatos.json")
    write_csv(
        out_dir / "contatos.csv",
        list(contatos_idx.values()),
        ["id", "nome", "numeroDocumento", "tipo"],
    )

    # 4) enriquecer e exportar CSVs por dataset
    json_snapshot: Dict[str, Any] = {
        "_metadata": {
            "fetched_at": datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M:%S"),
            "today": today,
        },
        "categorias": categorias,
        "contatos": list(contatos_idx.values()),
    }
    for (kind, sit), items in fetched.items():
        label = {
            ("pagar", 1): "contas_pagar_em_aberto",
            ("pagar", 2): "contas_pagar_pagas",
            ("receber", 1): "contas_receber_em_aberto",
            ("receber", 2): "contas_receber_recebidas",
        }[(kind, sit)]
        rows = enrich_and_dump(items, kind, contatos_idx, cat_idx)
        write_csv(out_dir / f"{label}_{today}.csv", rows, CONTAS_COLS)
        json_snapshot[label] = rows
        log(f"[ok] {label}: {len(rows)} linhas -> {label}_{today}.csv", args.quiet)

    snapshot_path = out_dir / f"bling_data_{today}.json"
    atomic_write_text(snapshot_path, json.dumps(json_snapshot, ensure_ascii=False, indent=2))
    log(f"[ok] snapshot consolidado -> {snapshot_path.name}", args.quiet)
    log(f"[done] tudo em {out_dir}", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
