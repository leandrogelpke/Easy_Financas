#!/usr/bin/env python3
"""
snapshot_guard.py — Decide se um snapshot recém-gerado (totvs/cartao) deve ser
COMMITADO ou DESCARTADO, implementando a regra do Leandro:

    "atualiza se houver arquivo novo; se não houver, deixa os dados que estão."

Problema que resolve: fetch-totvs.py / fetch-cartao.py reescrevem o `_meta`
(timestamp, input_dir) a cada rodada → o arquivo SEMPRE difere, mesmo sem dado
novo. Se commitássemos cego, todo cron geraria um commit espúrio. Este guard
compara o conteúdo SUBSTANTIVO (ignorando `_meta`) contra a versão em HEAD:

  - conteúdo igual  → restaura a versão de HEAD (git vê arquivo limpo) → rc 10
  - conteúdo mudou  → mantém o novo                                    → rc 0
  - novo vazio/menor que piso de sanidade → restaura HEAD + alerta     → rc 20

Piso de sanidade: protege contra um Drive pull parcial/falho que regeneraria
um snapshot encolhido e apagaria histórico. Rejeita se a contagem-núcleo cair
abaixo de 80% da versão commitada.

Uso:
    python3 ci/snapshot_guard.py --file data/bling-api/totvs_snapshot.json --kind totvs
    python3 ci/snapshot_guard.py --file cartao_snapshot.json --kind cartao

Stdlib only. rc 0 = mudou (commitar) · rc 10 = igual (restaurado) ·
rc 20 = rejeitado por sanidade (restaurado) · rc 1 = erro.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def _strip_meta(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "_meta"}


def _core_count(d: dict, kind: str) -> int:
    if kind == "totvs":
        return len(d.get("documentos", []))
    # cartao
    return int((d.get("_meta") or {}).get("n_transacoes", 0)) or len(d.get("faturas", []))


def _head_version(path: str):
    """Conteúdo do arquivo em HEAD (ou None se novo/inexistente no git)."""
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _restore(path: str) -> None:
    subprocess.run(["git", "checkout", "--", path], check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--kind", required=True, choices=["totvs", "cartao"])
    ap.add_argument("--floor", type=float, default=0.8,
                    help="fração mínima da contagem commitada (default 0.8)")
    args = ap.parse_args()

    try:
        new = json.load(open(args.file))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[guard] novo snapshot ilegível ({e}) — abortando, mantém HEAD.")
        _restore(args.file)
        return 20

    old = _head_version(args.file)
    if old is None:
        print(f"[guard] {args.file}: sem versão em HEAD — tratando como novo (commitar).")
        return 0

    new_n, old_n = _core_count(new, args.kind), _core_count(old, args.kind)

    # sanidade: nunca aceitar regen encolhido (Drive pull parcial/falho)
    if old_n > 0 and new_n < args.floor * old_n:
        print(f"[guard] REJEITADO: {args.kind} caiu de {old_n} → {new_n} "
              f"(< {args.floor:.0%}). Provável Drive pull incompleto. Restaurando HEAD.")
        _restore(args.file)
        return 20

    if _strip_meta(new) == _strip_meta(old):
        print(f"[guard] {args.kind}: sem dado novo (só _meta mudou) — restaurando HEAD.")
        _restore(args.file)
        return 10

    print(f"[guard] {args.kind}: dado novo detectado ({old_n} → {new_n} núcleo) — commitar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
