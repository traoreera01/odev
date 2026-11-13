#!/usr/bin/env python3
"""
odev — Agent de code et d'administration système local (v2)
Outils natifs étendus · Skills dynamiques · Sessions persistantes · TUI Rich
"""

import os
import re
import sys
import argparse

try:
    import ollama
except ImportError:
    print("Installe les dépendances nécessaires : pip install ollama rich")
    sys.exit(1)

from .config import load_config, save_config, SKILLS_DIR
from .memory import (
    load_memory,
    save_memory,
    set_project_summary,
    add_task,
    complete_task,
    add_note,
    init_project,
    is_project_mode,
    render_memory,
    build_memory_block,
)
from .logger import log
from .skills import load_skills, SKILLS_REGISTRY
from .sessions import list_sessions, save_session, load_session, delete_session
from .agents import AGENT_TYPES, get_system_prompt
from .agent import Agent
from . import ui
from . import repomap
from . import providers

CURRENT_AGENT_TYPE = "auto"


def build_system_prompt(memory: dict, agent_type: str = None, cfg: dict = None) -> str:
    base = get_system_prompt(agent_type or "auto")
    parts = [base]
    memory_block = build_memory_block(memory)
    if memory_block:
        parts.append(memory_block)
    if cfg is None or cfg.get("repo_map", True):
        repo_map = repomap.get_repo_map()
        if repo_map:
            parts.append(repo_map + "\nUtilise cette carte pour naviguer ; read_file pour le détail.")
    return "\n\n".join(parts)

SHELL_BUILTINS = {
    "ls", "ll", "la", "pwd", "cat", "less", "head", "tail", "grep", "find",
    "git", "pip", "pip3", "uv", "npm", "yarn", "cargo", "make", "docker",
    "python", "python3", "node", "bash", "sh", "echo", "touch", "mkdir",
    "cp", "mv", "rm", "chmod", "chown", "ps", "top", "htop", "df", "du",
    "curl", "wget", "ssh", "scp", "tar", "zip", "unzip", "which", "whereis",
    "env", "export", "source", "systemctl", "journalctl", "pacman", "yay",
    "apt", "apt-get", "dnf", "awk", "sed", "sort", "uniq", "wc", "diff",
}


def is_shell_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    first_word = stripped.split()[0].lstrip("./")
    return (
        first_word in SHELL_BUILTINS
        or stripped.startswith("./")
        or stripped.startswith("../")
        or (len(stripped.split()) == 1 and "/" in stripped)
        or re.match(r'^[a-z0-9_-]+ +-', stripped) is not None
    )


# Modèles non conversationnels fréquents sur les APIs (Groq, etc.) —
# à exclure du fallback pour ne pas basculer sur whisper/TTS/gardes.
_NON_CHAT_HINTS = ("whisper", "orpheus", "guard", "safeguard", "tts", "embed", "rerank")


def _is_chat_model(name: str) -> bool:
    n = name.lower()
    return not any(h in n for h in _NON_CHAT_HINTS)


def check_model(client, model: str, allow_fallback: bool = True):
    """Retourne le nom du modèle à utiliser, ou None si introuvable
    (quand allow_fallback=False — l'appelant garde son modèle courant)."""
    try:
        available = [m.model for m in client.list().models]
    except Exception:
        ui.console.print(f"[error]✗ Impossible de contacter le provider[/error]")
        sys.exit(1)
    if model in available:
        return model
    ui.console.print(f"[warn]⚠ '{model}' non trouvé[/warn]")
    ui.console.print(f"[dim]Disponibles : {', '.join(available)}[/dim]")
    if not allow_fallback:
        return None
    chat_models = [m for m in available if _is_chat_model(m)]
    pool = chat_models or available
    if pool:
        model = pool[0]
        ui.console.print(f"[dim]→ fallback sur {model}[/dim]")
        return model
    sys.exit(1)


def confirm_callback(message: str) -> bool:
    answer = ui.console.input(f"[warn]{message}[/warn]").strip().lower()
    return answer in ("o", "oui", "y", "yes")


def make_agent(client, cfg, agent_type: str = "auto") -> Agent:
    return Agent(
        client, cfg, agent_type=agent_type,
        confirm_callback=confirm_callback,
        on_bash_line=ui.bash_line,
        on_text=ui.show_text,
        on_tool_start=ui.show_tool_start,
        on_tool_result=ui.show_tool_result,
        on_thought=ui.show_thought,
        on_stream_start=ui.start_stream,
        on_stream_token=ui.stream_token,
        on_stream_end=ui.end_stream,
        on_wait_start=ui.start_wait,
        on_wait_stop=ui.stop_wait,
    )


def apply_ui_style(style: str) -> str:
    chosen = ui.set_style(style)
    return chosen


def handle_slash(cmd: str, model: str, messages: list, client, cfg: dict, memory: dict, agent_type: str = "auto"):
    parts = cmd.strip().split(None, 1)
    action = parts[0].lower()

    if action in ("/exit", "/quit", "/q"):
        if cfg.get("auto_save_session"):
            save_session("_last", model, messages, agent_type)
        ui.console.print("[dim]À bientôt.[/dim]")
        sys.exit(0)

    elif action == "/clear":
        messages = [messages[0]]
        ui.console.clear()
        ui.console.print("[dim]Historique vidé.[/dim]")

    elif action == "/models":
        try:
            ui.console.print()
            for m in client.list().models:
                mark = "[agent]●[/agent]" if m.model == model else "[dim]○[/dim]"
                size = f"{m.size / 1e9:.1f} Go" if getattr(m, "size", 0) else ""
                tag = "" if _is_chat_model(m.model) else " [dim](non-chat)[/dim]"
                ui.console.print(f"  {mark} {m.model}  [dim]{size}[/dim]{tag}")
            ui.console.print()
        except Exception as e:
            ui.console.print(f"[error]✗ {e}[/error]")

    elif action == "/model":
        if len(parts) < 2:
            ui.console.print(f"[dim]actuel : {model}[/dim]")
        else:
            arg = parts[1].strip()
            if arg.lower() in ("list", "ls"):
                ui.console.print("[dim]Pour lister les modèles : /models[/dim]")
                new_model = None
            else:
                new_model = check_model(client, arg, allow_fallback=False)
            if new_model:
                model = new_model
                messages = [messages[0]]
                ui.console.print(f"[dim]→ {model} (historique réinitialisé)[/dim]")
            elif arg.lower() not in ("list", "ls"):
                ui.console.print(f"[warn]Modèle inchangé : {model}[/warn]")

    elif action == "/cd":
        if len(parts) > 1:
            try:
                os.chdir(os.path.expanduser(parts[1]))
                ui.console.print(f"[dim]→ {os.getcwd()}[/dim]")
            except Exception as e:
                ui.console.print(f"[error]✗ {e}[/error]")

    elif action == "/skills":
        ui.show_skills_table(SKILLS_REGISTRY)

    elif action == "/reload":
        loaded, errors = load_skills()
        ui.console.print(f"[dim]Skills chargés : {', '.join(loaded) if loaded else 'aucun'}[/dim]")
        for e in errors:
            ui.console.print(f"[error]✗ {e}[/error]")

    elif action == "/theme":
        if len(parts) < 2:
            ui.console.print(f"[dim]style actuel : {ui.get_style()}[/dim]")
        else:
            chosen = apply_ui_style(parts[1].strip())
            cfg["ui_style"] = chosen
            save_config(cfg)
            ui.console.print(f"[success]✓ Style UI: {chosen}[/success]")

    elif action == "/agent":
        if len(parts) < 2:
            ui.console.print(f"[dim]agent actuel : {agent_type}[/dim]")
            ui.console.print(f"[dim]disponibles : {', '.join(AGENT_TYPES)}[/dim]")
        else:
            chosen = parts[1].strip().lower()
            if chosen in AGENT_TYPES:
                agent_type = chosen
                messages[0]["content"] = build_system_prompt(memory, agent_type, cfg)
                ui.console.print(f"[success]✓ Agent : {AGENT_TYPES[chosen]['name']}[/success]")
            else:
                ui.console.print(f"[warn]Agent inconnu. Disponibles : {', '.join(AGENT_TYPES)}[/warn]")

    elif action == "/init":
        path = init_project()
        ui.console.print(f"[success]✓ Projet initialisé : {path}[/success]")

    elif action == "/memory":
        mode = "📁 projet" if is_project_mode() else "🌐 global"
        block = render_memory(memory)
        msg = f"{mode}\n{block}" if block else f"{mode} — [dim]Aucune mémoire persistante.[/dim]"
        ui.console.print(msg)

    elif action == "/summary":
        if len(parts) == 1:
            ui.console.print(memory.get("project_summary") or "[dim]Aucun résumé projet défini.[/dim]")
        else:
            memory = set_project_summary(memory, parts[1].strip())
            ui.console.print("[success]✓ Résumé projet mis à jour.[/success]")

    elif action == "/tasks":
        tasks = memory.get("active_tasks", [])
        if not tasks:
            ui.console.print("[dim]Aucune tâche active.[/dim]")
        else:
            for task in tasks:
                status = "✓" if task.get("done") else "•"
                ui.console.print(f"{status} #{task.get('id')} {task.get('title')}")

    elif action == "/task":
        if len(parts) < 2:
            ui.console.print("[warn]Usage : /task <texte>[/warn]")
        else:
            memory = add_task(memory, parts[1].strip())
            ui.console.print("[success]✓ Tâche ajoutée.[/success]")

    elif action == "/done":
        if len(parts) < 2:
            ui.console.print("[warn]Usage : /done <id>[/warn]")
        else:
            try:
                task_id = int(parts[1].strip())
                if complete_task(memory, task_id):
                    ui.console.print(f"[success]✓ Tâche #{task_id} marquée comme terminée.[/success]")
                else:
                    ui.console.print(f"[warn]Tâche #{task_id} introuvable.[/warn]")
            except ValueError:
                ui.console.print("[warn]L'identifiant doit être un nombre.[/warn]")

    elif action == "/note":
        if len(parts) < 2:
            ui.console.print("[warn]Usage : /note <texte>[/warn]")
        else:
            memory = add_note(memory, parts[1].strip())
            ui.console.print("[success]✓ Note enregistrée.[/success]")

    elif action == "/save":
        name = parts[1].strip() if len(parts) > 1 else "default"
        path = save_session(name, model, messages, agent_type)
        ui.console.print(f"[success]✓ Session sauvegardée : {path}[/success]")

    elif action == "/load":
        if len(parts) < 2:
            ui.console.print("[warn]Usage : /load <nom>[/warn]")
        else:
            result = load_session(parts[1].strip())
            if result is None:
                ui.console.print(f"[error]✗ Session '{parts[1].strip()}' introuvable.[/error]")
            else:
                loaded_model, loaded_messages, loaded_agent = result
                model = loaded_model or model
                messages = loaded_messages
                agent_type = loaded_agent
                ui.console.print(f"[success]✓ Session chargée ({len(messages)} messages, {model}, agent:{agent_type})[/success]")

    elif action == "/sessions":
        ui.show_sessions_table(list_sessions())

    elif action == "/config":
        ui.show_config_table(cfg)

    elif action == "/set":
        if len(parts) < 2 or len(parts[1].split(None, 1)) < 2:
            ui.console.print("[warn]Usage : /set <clé> <valeur>[/warn]")
        else:
            key, val = parts[1].split(None, 1)
            if key not in cfg:
                ui.console.print(f"[warn]Clé inconnue : {key}[/warn]")
            else:
                # Coercition de type basique selon la valeur par défaut existante
                old = cfg[key]
                try:
                    if isinstance(old, bool):
                        cfg[key] = val.strip().lower() in ("1", "true", "o", "oui", "yes")
                    elif isinstance(old, int):
                        cfg[key] = int(val)
                    elif isinstance(old, float):
                        cfg[key] = float(val)
                    else:
                        cfg[key] = val
                    save_config(cfg)
                    if key == "ui_style":
                        apply_ui_style(str(cfg[key]))
                    elif key == "ui_compact":
                        ui.set_compact(bool(cfg[key]))
                    ui.console.print(f"[success]✓ {key} = {cfg[key]}[/success]")
                except ValueError:
                    ui.console.print(f"[error]✗ Valeur invalide pour {key}[/error]")

    elif action in ("/help", "/?"):
        ui.help_panel(SKILLS_DIR)

    else:
        ui.console.print(f"[warn]?[/warn] commande inconnue — [dim]/help[/dim]")

    save_memory(memory)
    if messages:
        messages[0]["content"] = build_system_prompt(memory, agent_type, cfg)
    else:
        messages = [{"role": "system", "content": build_system_prompt(memory, agent_type, cfg)}]
    return model, messages, memory, agent_type


def main():
    parser = argparse.ArgumentParser(description="odev — agent système & code local")
    parser.add_argument("--model", "-m", default=None, help="Modèle Ollama à utiliser")
    parser.add_argument("--host", default=None, help="URL du serveur Ollama")
    parser.add_argument("--resume", action="store_true", help="Reprend la dernière session")
    parser.add_argument("message", nargs="*")
    args = parser.parse_args()

    cfg = load_config()
    apply_ui_style(cfg.get("ui_style", "codex"))
    ui.set_compact(cfg.get("ui_compact", True))
    if args.model:
        cfg["model"] = args.model
    if args.host:
        cfg["ollama_host"] = args.host

    client = providers.get_client(cfg)
    model = check_model(client, cfg["model"])
    if cfg.get("num_ctx", 16384) < 32768:
        ui.console.print(
            f"[warn]⚠ num_ctx={cfg.get('num_ctx')} est faible pour un agent "
            "(32k+ recommandé). L'historique sera tronqué plus vite — "
            "/set num_ctx 65536 si votre VRAM le permet.[/warn]"
        )
    loaded, errors = load_skills()
    memory = load_memory()

    agent_type = "auto"
    messages = [{"role": "system", "content": build_system_prompt(memory, agent_type, cfg)}]
    if args.resume:
        result = load_session("_last")
        if result:
            model, messages, agent_type = result
            if not messages:
                messages = [{"role": "system", "content": build_system_prompt(memory, agent_type, cfg)}]
            elif messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": build_system_prompt(memory, agent_type, cfg)})
            messages[0]["content"] = build_system_prompt(memory, agent_type, cfg)

    ui.banner(model, loaded, os.getcwd(), memory=memory, agent_type=agent_type)
    for e in errors:
        ui.console.print(f"[error]✗ skill : {e}[/error]")

    agent = make_agent(client, cfg, agent_type)

    if args.message:
        msg = " ".join(args.message)
        ui.console.print(f"[user]vous[/user]  {msg}")
        messages.append({"role": "user", "content": msg})
        messages = agent.turn(model, messages)

    while True:
        try:
            inp = ui.show_user_prompt(os.getcwd(), memory, agent_type, model).strip()
        except (EOFError, KeyboardInterrupt):
            ui.console.print("\n[dim]À bientôt.[/dim]")
            if cfg.get("auto_save_session"):
                save_session("_last", model, messages, agent_type)
            break

        if not inp:
            continue

        if inp.startswith("/"):
            model, messages, memory, agent_type = handle_slash(inp, model, messages, client, cfg, memory, agent_type)
            agent.cfg = cfg
            agent.runner.cfg = cfg
            agent.agent_type = agent_type
            agent.runner.cwd = os.getcwd()  # resync après /cd ou autre changement de cwd
            continue

        if is_shell_command(inp):
            result = agent.runner.bash(inp)
            ui.show_tool_result("bash", result)
            ui.console.print()
            messages.append({"role": "user", "content": f"$ {inp}"})
            messages.append({"role": "assistant", "content": result})
            continue

        messages.append({"role": "user", "content": inp})
        messages = agent.turn(model, messages)

        if cfg.get("auto_save_session"):
            save_session("_last", model, messages, agent_type)


if __name__ == "__main__":
    main()
