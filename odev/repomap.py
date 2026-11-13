"""
odev.repomap — Carte légère du repository (symboles Python via ast).

Génère une vue compacte des fichiers .py, classes et fonctions pour le
system prompt, dans un budget de ~800 tokens. Inspiré du repo map d'aider,
en version stdlib-only (pas de tree-sitter).
"""

import ast
import os
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mcp", ".memexa", ".devin", ".gemini", ".vscode", ".idea",
    "dist", "build", ".tox", ".pytest_cache", ".ruff_cache", "target",
}

MAX_FILES = 400
MAX_CHARS = 3200  # ~800 tokens
MIN_FILES = 5  # en dessous, la carte n'apporte rien

_cache: dict = {}  # cwd -> repo map string


def _iter_py_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield Path(dirpath) / fn
                count += 1
                if count >= MAX_FILES:
                    return


def _extract_symbols(tree: ast.Module) -> list:
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"{node.name}()")
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if methods:
                shown = ", ".join(methods[:8])
                if len(methods) > 8:
                    shown += ", …"
                out.append(f"class {node.name}({shown})")
            else:
                out.append(f"class {node.name}")
    return out


_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)


def _module_name(rel_path: Path) -> str:
    return str(rel_path.with_suffix("")).replace(os.sep, ".").replace("/", ".")


def _imports_of(text: str) -> set:
    mods = set()
    for m in _IMPORT_RE.finditer(text):
        mods.add(m.group(1))
        # 'from pkg import sub' peut aussi désigner pkg.sub
        parts = m.group(1).split(".")
        for i in range(1, len(parts)):
            mods.add(".".join(parts[:i]))
    return mods


def build_repo_map(root=None, max_chars: int = MAX_CHARS) -> str:
    """Construit la carte du repo. Retourne '' si trop petit ou erreur."""
    root = Path(root or ".").resolve()
    files = list(_iter_py_files(root))
    if len(files) < MIN_FILES:
        return ""

    info = {}   # module_name -> {"rel": str, "symbols": [...], "imports": set}
    for f in files:
        try:
            rel = f.relative_to(root)
            text = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except Exception:
            continue
        symbols = _extract_symbols(tree)
        if not symbols:
            continue
        info[_module_name(rel)] = {
            "rel": str(rel),
            "symbols": symbols,
            "imports": _imports_of(text),
        }

    if len(info) < MIN_FILES:
        return ""

    # Indegree : combien de fichiers importent chaque module
    indegree = {name: 0 for name in info}
    for name, data in info.items():
        for imp in data["imports"]:
            if imp in indegree:
                indegree[imp] += 1

    ranked = sorted(
        info.items(),
        key=lambda kv: (indegree[kv[0]] * 3 + len(kv[1]["symbols"]), len(kv[1]["symbols"])),
        reverse=True,
    )

    lines = ["## Repo map (symboles clés du projet)"]
    used = len(lines[0])
    for name, data in ranked:
        syms = ", ".join(data["symbols"][:12])
        if len(data["symbols"]) > 12:
            syms += ", …"
        line = f"- {data['rel']}: {syms}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def get_repo_map(cwd=None) -> str:
    """Version cachée par cwd — recalculée seulement si le cwd change."""
    key = str(Path(cwd or ".").resolve())
    if key not in _cache:
        try:
            _cache[key] = build_repo_map(key)
        except Exception:
            _cache[key] = ""
    return _cache[key]
