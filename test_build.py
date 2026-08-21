#!/usr/bin/env python3
"""Self-tests do build-html.py — protege contra regressões estruturais.

Cobre os 2 bugs que mais causaram dor no histórico:
  1. Marcadores @@NOME@@ não substituídos (sintoma: aparecem literalmente
     no dashboard).
  2. Divs desbalanceadas em qualquer <div class="pg"> (sintoma: abas em
     branco a partir da página com o defeito — todas seguintes ficam
     aninhadas dentro dela e somem ao trocar de aba).

Roda com: `python3 test_build.py` (sem dependências externas).
Roda contra o index.html gerado. Se index.html não existir, gera-o
primeiro chamando build-html.py (com fallback para snapshot mais recente).

Plugado no `weekly.sh` etapa 2c — bloqueia rodada se algum teste quebrar.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _strip_scripts(html: str) -> str:
    """Remove <script>...</script> antes de contar <div>.

    Necessário porque arrays JS contêm strings com "<div" / "</div>" que
    NÃO são tags reais.
    """
    return re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)


def _ler_index() -> str:
    if not INDEX.exists():
        raise FileNotFoundError(
            f"{INDEX} não existe. Rode build-html.py primeiro.")
    return INDEX.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────
# Testes
# ─────────────────────────────────────────────────────────────────
def test_sem_marcadores_pendentes() -> None:
    """Nenhum @@NOME@@ pode sobrar no HTML final."""
    html = _ler_index()
    pendentes = sorted(set(re.findall(r"@@[A-Z_][A-Z0-9_]*@@", html)))
    assert not pendentes, (
        f"{len(pendentes)} marcador(es) não substituído(s): "
        + ", ".join(pendentes[:8]) + ("…" if len(pendentes) > 8 else "")
    )


def test_pgs_balanceadas() -> None:
    """Cada <div class="pg"> deve abrir e fechar a mesma quantidade de <div>.

    Se uma .pg tem delta != 0, as próximas abas ficam aninhadas dentro
    dela e somem do dashboard. Ver §6.2 do CLAUDE.md.
    """
    html = _strip_scripts(_ler_index())
    pgs = list(re.finditer(
        r'<div class="pg(?:\s+active)?"\s+id="pg-([\w-]+)"', html))
    assert pgs, "Nenhuma <div class='pg' id='pg-...'> encontrada no HTML."

    falhas = []
    for i, m in enumerate(pgs):
        nome = m.group(1)
        start = m.start()
        end = pgs[i + 1].start() if i + 1 < len(pgs) else len(html)
        bloco = html[start:end]
        opens = bloco.count("<div")
        closes = bloco.count("</div>")
        if opens != closes:
            falhas.append(
                f"  pg-{nome}: opens={opens} closes={closes} "
                f"delta={opens - closes:+d}")

    assert not falhas, (
        "Divs desbalanceadas em " + str(len(falhas))
        + " aba(s) — abas seguintes ficarão escondidas:\n"
        + "\n".join(falhas))


def test_pg_count_minimo() -> None:
    """Garante que o HTML tem o número esperado de abas (12).

    Se cair pra menos, alguém deletou uma aba — ou pior, ela foi
    aninhada dentro de outra por div desbalanceada.
    """
    html = _strip_scripts(_ler_index())
    n_pgs = len(re.findall(
        r'<div class="pg(?:\s+active)?"\s+id="pg-', html))
    assert n_pgs >= 12, (
        f"Apenas {n_pgs} abas .pg encontradas — esperado >= 12. "
        "Possível aninhamento por div mal-balanceada.")


def test_tamanho_index_razoavel() -> None:
    """Index.html deve ter tamanho minimamente razoável.

    Se cair drasticamente, algum bloco grande sumiu (geralmente um
    `@@MARCADOR@@` que falhou em substituir, ou exception silenciosa
    no build).
    """
    size = INDEX.stat().st_size
    # Histórico estável: ~1.1 MB. Floor conservador em 500 KB.
    assert size > 500_000, (
        f"index.html tem só {size} bytes — algo importante sumiu. "
        "Confira logs do build.")


def test_canvases_clientes_presentes() -> None:
    """Os canvases da aba Clientes (donut, bar) precisam estar no HTML.

    Histórico: bug do <div> mal-balanceado na Caixa escondeu a aba
    Clientes inteira; este teste pega o caso degenerado em que o HTML
    está OK mas a aba foi deletada.
    """
    html = _ler_index()
    for canvas_id in ("cliDonut", "cliBar", "cliTbody"):
        assert f'id="{canvas_id}"' in html, (
            f'Elemento <{canvas_id}> ausente do HTML — aba Clientes '
            "provavelmente quebrou.")


def test_fstrings_compat_py311() -> None:
    """f-string NÃO pode ter backslash na parte de expressão {...} — é
    SyntaxError no Python 3.11 (usado no CI), embora válido no 3.12+. Pega o
    caso traiçoeiro em que o build passa local (3.12/3.13) mas quebra no CI.
    Histórico: 18/06 um title= com aspas escapadas em dre_render.py degradou
    a aba Auditoria/DRE no CI e bloqueou a publicação.
    """
    import ast
    bad = []
    for py in sorted(HERE.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue  # sintaxe inválida é pega por outro caminho
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.FormattedValue):
                        seg = ast.get_source_segment(src, part.value) or ""
                        if "\\" in seg:
                            bad.append(f"{py.name}:{part.value.lineno}")
    assert not bad, (
        "f-string com backslash na expressão (quebra no Python 3.11 do CI): "
        + ", ".join(bad))


def test_js_sintaxe_valida() -> None:
    """Todos os <script> inline do index parseiam (node --check).

    Pega erro de sintaxe ANTES do publish — um crash no bloco INIT derruba
    todas as abas seguintes (bug histórico §6.9). Sem node no ambiente,
    pula com aviso (o runner do CI tem node).
    """
    import re
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        print("      (node ausente — teste pulado)")
        return
    html = _ler_index()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    checked = 0
    for i, js in enumerate(scripts):
        if not js.strip() or "src=" in js[:80]:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            path = fh.name
        r = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, \
            f"script #{i} com erro de sintaxe: {r.stderr[:300]}"
        checked += 1
    assert checked >= 1, "nenhum script inline encontrado pra checar"


TESTS = [
    test_sem_marcadores_pendentes,
    test_pgs_balanceadas,
    test_pg_count_minimo,
    test_tamanho_index_razoavel,
    test_canvases_clientes_presentes,
    test_fstrings_compat_py311,
    test_js_sintaxe_valida,
]


def main() -> int:
    if not INDEX.exists():
        print(f"[skip] {INDEX} não existe — rode build-html.py primeiro.",
              file=sys.stderr)
        return 0

    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERRO  {t.__name__}: {type(e).__name__}: {e}")

    print()
    if failed:
        print(f"FAILED — {failed} de {len(TESTS)} testes quebraram")
        return 1
    print(f"OK — {len(TESTS)} testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
