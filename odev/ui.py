"""
odev.ui — Rendu terminal via Rich : markdown colorisé, thèmes complets, panneaux d'outils.
"""

import difflib
import re
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.theme import Theme
from rich.markup import escape
from rich import box
from rich.text import Text
from rich.rule import Rule

from .completion import setup as setup_completion

MAX_PREVIEW_LINES = 60

# Mode compact : les panneaux d'outils n'affichent qu'un petit aperçu
# (le contenu complet reste dans l'historique du modèle, pas besoin de
# noyer le terminal — comme les tool calls repliés de Devin/Claude Code).
_compact = True


def set_compact(enabled: bool):
    global _compact
    _compact = bool(enabled)


def _preview_limit(default: int) -> int:
    return 12 if _compact else default

STYLE_PRESETS = {
    "codex": {
        "agent": "bold cyan",
        "user": "bold bright_cyan",
        "tool": "bold magenta",
        "result": "white",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "dim": "dim white",
        "border": "cyan",
        "tool_border": "bright_black",
        "thought_border": "bright_black",
        "brand": "odev",
        "subtitle": "local code agent",
        "icon": "◆",
        "code_theme": "monokai",
    },
    "claude": {
        "agent": "bold bright_magenta",
        "user": "bold white",
        "tool": "bold bright_red",
        "result": "white",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "dim": "dim white",
        "border": "magenta",
        "tool_border": "red",
        "thought_border": "bright_black",
        "brand": "Claude",
        "subtitle": "clean assistant workspace",
        "icon": "✦",
        "code_theme": "dracula",
    },
    "gemini": {
        "agent": "bold bright_green",
        "user": "bold bright_blue",
        "tool": "bold bright_yellow",
        "result": "white",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "dim": "dim white",
        "border": "blue",
        "tool_border": "yellow",
        "thought_border": "bright_black",
        "brand": "Gemini",
        "subtitle": "multimodal workspace",
        "icon": "◇",
        "code_theme": "one-dark",
    },
    "nord": {
        "agent": "bold #88C0D0",
        "user": "bold #8FBCBB",
        "tool": "bold #D08770",
        "result": "#ECEFF4",
        "warn": "bold #EBCB8B",
        "error": "bold #BF616A",
        "success": "bold #A3BE8C",
        "dim": "dim #D8DEE9",
        "border": "#81A1C1",
        "tool_border": "#5E81AC",
        "thought_border": "#4C566A",
        "brand": "Nord",
        "subtitle": "arctic terminal",
        "icon": "▸",
        "code_theme": "nord",
    },
    "catppuccin": {
        "agent": "bold #89B4FA",
        "user": "bold #A6E3A1",
        "tool": "bold #F38BA8",
        "result": "#CDD6F4",
        "warn": "bold #F9E2AF",
        "error": "bold #F38BA8",
        "success": "bold #A6E3A1",
        "dim": "dim #6C7086",
        "border": "#89B4FA",
        "tool_border": "#585B70",
        "thought_border": "#45475A",
        "brand": "Catppuccin",
        "subtitle": "mocha terminal",
        "icon": "⋆",
        "code_theme": "catppuccin-frappe",
    },
}

_current_style = "codex"
_preset = STYLE_PRESETS[_current_style]
console = Console(theme=Theme({
    "agent": _preset["agent"],
    "user": _preset["user"],
    "tool": _preset["tool"],
    "result": _preset["result"],
    "warn": _preset["warn"],
    "error": _preset["error"],
    "success": _preset["success"],
    "dim": _preset["dim"],
}), highlight=False, emoji=False)


def available_styles() -> list:
    return sorted(STYLE_PRESETS)


def get_style() -> str:
    return _current_style


def set_style(style: str) -> str:
    global console, _current_style, _preset
    normalized = (style or "codex").strip().lower()
    if normalized not in STYLE_PRESETS:
        normalized = "codex"
    _current_style = normalized
    _preset = STYLE_PRESETS[_current_style]
    console = Console(theme=Theme({
        "agent": _preset["agent"],
        "user": _preset["user"],
        "tool": _preset["tool"],
        "result": _preset["result"],
        "warn": _preset["warn"],
        "error": _preset["error"],
        "success": _preset["success"],
        "dim": _preset["dim"],
    }), highlight=False, emoji=False)
    return _current_style


def _preview(text: str, limit: int = 120) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _render_md(text: str) -> Markdown:
    return Markdown(text, code_theme=_preset.get("code_theme", "monokai"))


def banner(model: str, skills: list, cwd: str, version: str = "2.0", memory: Optional[dict] = None, agent_type: str = "auto"):
    console.clear()
    console.print()
    summary = ""
    tasks = 0
    if memory:
        summary = _preview(memory.get("project_summary") or "", 110)
        tasks = len(memory.get("active_tasks", []))

    body = [
        f" {_preset['icon']} [agent]{_preset['brand']}[/agent] v{version}  [dim]{_preset['subtitle']}[/dim]",
        "",
        f"  [dim]theme[/dim]   [success]{_current_style}[/success]",
        f"  [dim]model[/dim]   {model}",
        f"  [dim]agent[/dim]   {agent_type}",
        f"  [dim]cwd[/dim]     {cwd}",
    ]
    if summary:
        body.append(f"  [dim]summary[/dim] {summary}")
    if tasks:
        body.append(f"  [dim]tasks[/dim]   {tasks} active")
    if skills:
        body.append(f"  [dim]skills[/dim]  {', '.join(skills)}")
    body.append("")
    body.append(f"  [dim]{_preset['icon']} /help for commands[/dim]")

    console.print(Panel("\n".join(body), border_style=_preset["border"], padding=(0, 2)))
    console.print()


def show_question(question: str) -> str:
    console.print()
    console.print(Panel(
        f"[warn]❓ {question}[/warn]",
        title="[warn]question[/warn]",
        border_style=_preset["warn"],
        padding=(0, 1),
    ))
    answer = console.input(f"[user]{_preset['icon']}[/user]  ").strip()
    return answer


def show_user_prompt(cwd: str, memory: dict = None, agent_type: str = "auto", model: str = "") -> str:
    from .memory import is_project_mode as _is_proj
    setup_completion(cwd, memory)
    mode = " 📁" if _is_proj(cwd) else ""
    label = f"[dim]{Path(cwd).name}{mode}[/dim]"
    if agent_type != "auto":
        label += f" [tool]‹{agent_type}›[/tool]"
    if model:
        label += f" [dim]·[/dim] [dim]{model}[/dim]"
    console.print()
    console.print(f"[dim]{label}[/dim]")
    prompt = f"[user]{_preset['icon']}[/user]  "
    console.print(prompt, end="")
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        raise


def show_thought(thought: str):
    lines = thought.splitlines()
    display = "\n".join(lines[:MAX_PREVIEW_LINES * 2])
    if len(lines) > MAX_PREVIEW_LINES * 2:
        display += f"\n[dim]… ({len(lines) - MAX_PREVIEW_LINES * 2} lines more)[/dim]"
    try:
        rendered = _render_md(display)
    except Exception:
        rendered = Text(display)
    console.print(Panel(
        rendered,
        title=f"[dim]{_preset['icon']} réflexion[/dim]",
        border_style=_preset["thought_border"],
        padding=(0, 1),
        width=min(console.width, 120),
    ))


# ── Indicateur d'attente modèle ────────────────────────────────────────────

_wait_status = None
_wait_t0 = 0.0


class _WaitRenderable:
    """Affiche 'réflexion… Ns' — le temps est recalculé à chaque refresh."""

    def __rich__(self):
        elapsed = time.monotonic() - _wait_t0
        return Text(f"réflexion… {elapsed:.0f}s", style="dim")


def start_wait():
    """Démarre le spinner d'attente (thread-safe : appelé avant l'appel modèle)."""
    global _wait_status, _wait_t0
    stop_wait()
    _wait_t0 = time.monotonic()
    try:
        _wait_status = console.status(_WaitRenderable(), spinner="dots")
        _wait_status.start()
    except Exception:
        _wait_status = None


def stop_wait():
    global _wait_status
    if _wait_status is not None:
        try:
            _wait_status.stop()
        except Exception:
            pass
        _wait_status = None


# ── Streaming live (Markdown temps réel) ──────────────────────────────────

_live: Optional[Live] = None
_stream_buffer: list[str] = []


_NOTICE_RE = re.compile(r"^\[(Erreur modèle|odev|compact)\]")


def _notice_panel(text: str):
    """Panneau pour les notices système : erreurs modèle, loop guard, compact."""
    if text.startswith("[Erreur modèle]"):
        title, border, style = "erreur modèle", _preset["error"], "error"
        body = text.replace("[Erreur modèle]", "", 1).strip()
    elif text.startswith("[compact]"):
        title, border, style = "compact", _preset["tool_border"], "dim"
        body = text.replace("[compact]", "", 1).strip()
    else:
        title, border, style = "odev", _preset["warn"], "warn"
        body = text.replace("[odev]", "", 1).strip()
    console.print()
    console.print(Panel(
        f"[{style}]{escape(body)}[/{style}]",
        title=f"[dim]{_preset['icon']} {title}[/dim]",
        border_style=border,
        padding=(0, 1),
    ))


def show_text(text: str):
    if not text.strip():
        return
    console.print()
    if _NOTICE_RE.match(text.strip()):
        _notice_panel(text.strip())
        return
    console.print(f"[agent]{_preset['icon']} odev[/agent]")
    try:
        md = _render_md(text)
        console.print(md)
    except Exception:
        console.print(text)


def start_stream():
    global _stream_buffer, _live
    _stream_buffer = []
    console.print()
    live_md = _render_md("")
    _live = Live(live_md, console=console, refresh_per_second=12, transient=False, vertical_overflow="visible")
    try:
        _live.start()
    except Exception:
        _live = None


def stream_token(token: str):
    global _stream_buffer
    _stream_buffer.append(token)
    if _live:
        text = "".join(_stream_buffer)
        try:
            md = _render_md(text)
            _live.update(md)
        except Exception:
            pass


def end_stream():
    global _stream_buffer, _live
    text = "".join(_stream_buffer)
    _stream_buffer = []
    if _live:
        try:
            if text.strip():
                md = _render_md(text)
                _live.update(md)
            _live.stop()
        except Exception:
            pass
        _live = None


# ── Outils ─────────────────────────────────────────────────────────────────

_TOOL_ICONS = {
    "bash": "$",
    "write_file": "✎",
    "edit_file": "✎",
    "apply_patch": "🩹",
    "read_file": "👁",
    "list_dir": "📁",
    "run_python": "🐍",
    "git": "⎇",
    "http_get": "🌐",
    "question": "❓",
    "todo": "☑",
    "subagent": "🔄",
}


_bash_streaming = False
_last_tool_args: dict = {}

# Extension → lexer Pygments pour coloriser les aperçus de fichiers
_LEXERS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".json": "json", ".md": "markdown", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".html": "html", ".css": "css", ".scss": "scss",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".rb": "ruby", ".php": "php", ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
    ".vue": "html", ".svelte": "html", ".lua": "lua", ".kt": "kotlin",
    ".swift": "swift", ".ex": "elixir", ".exs": "elixir", ".hs": "haskell",
    ".scala": "scala", ".tf": "hcl",
}


def _lexer_for(path: str) -> Optional[str]:
    name = Path(path).name.lower()
    if name in ("dockerfile", "makefile", "gemfile", "rakefile"):
        return {"dockerfile": "docker", "makefile": "make", "gemfile": "ruby", "rakefile": "ruby"}[name]
    return _LEXERS.get(Path(path).suffix.lower())


def _code_block(text: str, path: str = ""):
    """Retourne un Syntax coloré si le langage est connu, sinon du texte brut."""
    lexer = _lexer_for(path)
    if lexer:
        try:
            return Syntax(text, lexer, theme=_preset.get("code_theme", "monokai"),
                          line_numbers=False, word_wrap=True)
        except Exception:
            pass
    return Text(text)


_EXIT_RE = re.compile(r"\[exit code: (\d+)\]\s*$")

_STATUS_ICONS = {"ok": "✓", "error": "✗", "info": "•"}
_STATUS_STYLES = {"ok": "success", "error": "error", "info": "tool"}


def _tool_status(name: str, result: str) -> str:
    """'ok' | 'error' | 'info' — déduit du résultat pour colorer le panneau."""
    if name.startswith("subagent:"):
        return "info"
    head = result.lstrip()[:200]
    if head.startswith(("Erreur", "✗")) or "⨯" in head:
        return "error"
    m = _EXIT_RE.search(result)
    if m and int(m.group(1)) != 0:
        return "error"
    if head.startswith(("✓", "(inchangé")):
        return "ok"
    return "info"


def bash_line(line: str):
    """Appelé pour chaque ligne de sortie bash en temps réel."""
    global _bash_streaming
    if not _bash_streaming:
        return
    # Masque la ligne de marqueur interne CWD
    if "___CWD___" in line:
        return
    console.print(f"  [dim]{escape(line)}[/dim]")


def show_tool_start(name: str, args: dict):
    global _bash_streaming, _last_tool_args
    _last_tool_args = args or {}
    if name == "subagent" or name.startswith("subagent:"):
        agent_type = name[9:] if name.startswith("subagent:") else args.get("agent_type", "?")
        task = args.get("task", "")
        console.print(f"\n  [tool]🔄 subagent:[/tool][tool]{agent_type}[/tool] [dim]{_preview(task, 100)}[/dim]")
        return
    icon = _TOOL_ICONS.get(name, "⚙")
    detail = ""
    if "question" in args:
        detail = args["question"]
    elif "command" in args:
        detail = args["command"]
    elif "path" in args:
        detail = args["path"]
    elif "args" in args:
        detail = args["args"]
    elif "url" in args:
        detail = args["url"]
    elif name.startswith("skill_"):
        detail = str(args)
    label = f"  [tool]{icon} {name}[/tool]"
    if detail:
        label += f" [dim]{_preview(detail, 150)}[/dim]"
    console.print()
    console.print(label)
    # Active le streaming ligne par ligne pour bash
    _bash_streaming = name == "bash"


def show_tool_result(name: str, result: str, full: bool = False):
    global _bash_streaming
    _bash_streaming = False  # toujours désactiver à la fin
    args = _last_tool_args
    status = _tool_status(name, result)
    status_tag = f"[{_STATUS_STYLES[status]}]{_STATUS_ICONS[status]}[/] "
    border = _preset["error"] if status == "error" else (
        _preset["success"] if status == "ok" else _preset["tool_border"])

    if name.startswith("subagent:"):
        agent_type = name[9:]
        lines = result.split("\n")
        preview = "\n".join(lines[:_preview_limit(MAX_PREVIEW_LINES)])
        if len(lines) > _preview_limit(MAX_PREVIEW_LINES):
            preview += f"\n[dim]… ({len(lines) - _preview_limit(MAX_PREVIEW_LINES)} lines more)[/dim]"
        console.print(Panel(
            preview,
            title=f"[tool]subagent:{agent_type}[/tool]",
            border_style=_preset["tool_border"],
            padding=(0, 1),
        ))
        return

    # bash : déjà affiché ligne par ligne — juste afficher le résumé si tronqué
    if name == "bash":
        lines = result.split("\n")
        if len(lines) > MAX_PREVIEW_LINES * 4:
            console.print(f"  [dim]… ({len(lines) - MAX_PREVIEW_LINES * 4} lignes supplémentaires tronquées)[/dim]")
        # Une ligne de statut si la commande a échoué
        m = _EXIT_RE.search(result)
        if (m and int(m.group(1)) != 0) or result.lstrip().startswith("Erreur"):
            console.print(f"  [error]✗ bash a échoué[/error]")
        return

    # edit_file : montrer le diff old → new plutôt que le texte brut
    if name == "edit_file" and status == "ok" and args.get("old") is not None:
        old_text = str(args.get("old", ""))
        new_text = str(args.get("new", ""))
        diff = list(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="avant", tofile="après", lineterm="",
        ))
        limit = _preview_limit(MAX_PREVIEW_LINES * 3)
        body_lines = []
        for line in diff[:limit]:
            if line.startswith("+") and not line.startswith("+++"):
                body_lines.append(f"[success]{escape(line)}[/success]")
            elif line.startswith("-") and not line.startswith("---"):
                body_lines.append(f"[error]{escape(line)}[/error]")
            else:
                body_lines.append(f"[dim]{escape(line)}[/dim]")
        if len(diff) > limit:
            body_lines.append(f"[dim]… ({len(diff) - limit} lignes de diff omises)[/dim]")
        console.print(Panel(
            "\n".join(body_lines) if body_lines else "[dim](diff vide)[/dim]",
            title=f"[tool]✎ edit_file[/tool] [dim]{_preview(args.get('path', ''), 60)}[/dim]",
            border_style=border,
            padding=(0, 1),
        ))
        _print_result_notes(result)
        return

    # write_file : aperçu du contenu écrit, coloré selon l'extension
    if name == "write_file" and status == "ok" and args.get("content"):
        content = str(args["content"])
        all_lines = content.split("\n")
        limit = _preview_limit(MAX_PREVIEW_LINES)
        omitted = max(0, len(all_lines) - limit)
        preview = "\n".join(all_lines[:limit])
        console.print(Panel(
            _code_block(preview, args.get("path", "")),
            title=f"[tool]✎ write_file[/tool] [dim]{_preview(args.get('path', ''), 60)}[/dim]",
            border_style=border,
            padding=(0, 1),
        ))
        if omitted:
            console.print(f"  [dim]… ({omitted} lignes omises)[/dim]")
        _print_result_notes(result)
        return

    # read_file : colorer le contenu selon l'extension du fichier
    if name == "read_file" and args.get("path"):
        limit = _preview_limit(MAX_PREVIEW_LINES * 4)
        lines = result.split("\n")
        omitted = max(0, len(lines) - limit)
        # Retire le marqueur de troncature _truncate avant coloration
        body = "\n".join(lines[:limit])
        body = body.split("\n… [tronqué")[0]
        console.print(Panel(
            _code_block(body, args.get("path", "")),
            title=f"[tool]👁 read_file[/tool] [dim]{_preview(args.get('path', ''), 60)}[/dim]",
            border_style=border,
            padding=(0, 1),
        ))
        if omitted:
            hint = " — contenu complet dans le contexte du modèle" if _compact else ""
            console.print(f"  [dim]… ({omitted} lignes supplémentaires non affichées{hint})[/dim]")
        return

    limit = _preview_limit(MAX_PREVIEW_LINES * 4) if name in ("run_python",) else _preview_limit(MAX_PREVIEW_LINES)
    lines = result.split("\n")
    preview = "\n".join(lines[:limit])
    if len(lines) > limit:
        preview += f"\n[dim]… ({len(lines) - limit} lines more)[/dim]"
    console.print(Panel(
        escape(preview),
        title=f"{status_tag}[tool]{name}[/tool]",
        border_style=border,
        padding=(0, 1),
    ))


def _print_result_notes(result: str):
    """Affiche les lignes de résultat non couvertes par l'aperçu (check syntaxe, etc.)."""
    lines = [l for l in result.split("\n")[1:] if l.strip()]
    for line in lines[:5]:
        style = "warn" if line.lstrip().startswith(("⚠", "Erreur")) else "dim"
        console.print(f"  [{style}]{escape(line)}[/{style}]")


# ── Tables ─────────────────────────────────────────────────────────────────

def show_skills_table(registry: dict):
    if not registry:
        console.print("[dim]Aucun skill chargé.[/dim]")
        return
    table = Table(border_style=_preset["tool_border"], show_header=True, header_style="bold", box=box.SIMPLE)
    table.add_column("Skill")
    table.add_column("Description")
    table.add_column("File", style="dim")
    for name, data in registry.items():
        table.add_row(name, data["description"], str(data["file"].name))
    console.print(table)


def show_sessions_table(sessions: list):
    if not sessions:
        console.print("[dim]Aucune session sauvegardée.[/dim]")
        return
    table = Table(border_style=_preset["tool_border"], show_header=True, header_style="bold", box=box.SIMPLE)
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Agent")
    table.add_column("Messages")
    table.add_column("Updated", style="dim")
    for s in sessions:
        table.add_row(s["name"], s["model"], s.get("agent_type", "?"), str(s["n_messages"]), s["updated"])
    console.print(table)


CONFIG_DESCRIPTIONS = {
    "model": "modèle LLM utilisé",
    "provider": "backend : ollama | openai | anthropic",
    "api_base": "URL de base API (vide = défaut du provider)",
    "api_key_env": "variable d'env contenant la clé API",
    "ollama_host": "URL du serveur Ollama",
    "keep_alive": "durée de maintien du modèle en mémoire",
    "max_loops": "itérations max de la boucle agent par tour",
    "num_ctx": "taille du contexte (tokens)",
    "temperature": "créativité du modèle (0 = déterministe)",
    "max_history_messages": "messages conservés dans l'historique",
    "max_subagent_depth": "profondeur max de délégation subagent",
    "bash_timeout": "timeout des commandes bash (secondes)",
    "confirm_dangerous": "demander confirmation avant commandes risquées",
    "stream": "afficher la réponse en streaming",
    "auto_save_session": "sauvegarder la session automatiquement",
    "repo_map": "injecter la carte du dépôt dans le prompt",
    "compact": "trim = tronquer, smart = résumer via le modèle",
    "ui_style": "thème : codex | claude | gemini | nord | catppuccin",
    "ui_compact": "True = aperçus d'outils courts, False = détaillé",
    "editor": "éditeur utilisé par /edit",
}


def show_config_table(cfg: dict):
    table = Table(border_style=_preset["tool_border"], show_header=True,
                  header_style="bold", box=box.SIMPLE)
    table.add_column("Clé", style="tool")
    table.add_column("Valeur")
    table.add_column("Description", style="dim")
    for key in sorted(cfg):
        table.add_row(key, str(cfg[key]), CONFIG_DESCRIPTIONS.get(key, ""))
    console.print(table)


_HELP_COMMANDS = [
    ("/agent <type>", "changer d'agent spécialisé (auto, coder, reviewer, explorer, planner, debugger)"),
    ("/model <nom>", "changer de modèle (historique réinitialisé)"),
    ("/models", "lister les modèles disponibles"),
    ("/theme <style>", "changer le thème UI (codex, claude, gemini, nord, catppuccin)"),
    ("/skills", "lister les skills actifs"),
    ("/reload", "recharger les skills depuis le disque"),
    ("/memory", "afficher la mémoire persistante"),
    ("/summary [texte]", "voir ou définir le résumé projet"),
    ("/task <texte> /done <id> /tasks", "gestion des tâches"),
    ("/note <texte>", "ajouter une note à la mémoire"),
    ("/init", "initialiser la mémoire projet (.odev/)"),
    ("/save /load /sessions", "gérer les sessions"),
    ("/cd <path>", "changer de dossier de travail"),
    ("/config /set <k> <v>", "voir / modifier la configuration"),
    ("/clear", "vider l'historique de conversation"),
    ("/exit", "quitter"),
]


def help_panel(skills_dir: Path):
    table = Table(border_style=_preset["tool_border"], show_header=False,
                  box=box.SIMPLE, padding=(0, 2))
    table.add_column("Commande", style="tool", no_wrap=True)
    table.add_column("Description", style="dim")
    for cmd, desc in _HELP_COMMANDS:
        table.add_row(cmd, desc)
    console.print(Panel(
        table,
        title=f"[agent]{_preset['icon']} odev — {get_style()}[/agent]",
        subtitle=f"[dim]skills : {skills_dir}[/dim]",
        border_style=_preset["border"],
        padding=(0, 1),
    ))
