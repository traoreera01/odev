"""
odev.tools — Outils natifs exposés au modèle.
Chaque outil : (1) un schema JSON pour le function-calling, (2) une fonction d'exécution.
"""

import os
import re
import json
import subprocess
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

from .logger import log
from . import ui as _ui

DANGEROUS_PATTERNS = (
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){:|:&};:", ":(){",
    "chmod -R 777 /", "sudo rm -rf", "> /dev/sda", "mv /* ", "chown -R root /",
    "wipefs", "shutdown", "reboot", "init 0", "> /dev/null 2>&1 &",
)

MAX_OUTPUT_CHARS = 12000


def is_dangerous(command: str) -> bool:
    cmd = command.strip().lower()
    return any(p in cmd for p in DANGEROUS_PATTERNS)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return (
        text[:half]
        + f"\n\n… [tronqué — {len(text) - MAX_OUTPUT_CHARS} caractères omis] …\n\n"
        + text[-half:]
    )


class ToolRunner:
    """Garde l'état (cwd) entre les appels, comme un vrai shell persistant."""

    def __init__(self, cfg: dict, confirm_callback=None, on_bash_line=None):
        self.cfg = cfg
        self.cwd = os.getcwd()
        self.confirm_callback = confirm_callback or (lambda msg: input(msg).strip().lower() in ("o", "oui", "y", "yes"))
        # Callback appelé pour chaque ligne de sortie bash en temps réel
        self.on_bash_line = on_bash_line or (lambda line: None)
        # Cache read_file : clé (path, start, end) → (mtime_ns, résultat)
        self._read_cache = {}

    # ── bash ────────────────────────────────────────────────────────────────
    def bash(self, command: str) -> str:
        log.info(f"bash: {command}")
        if is_dangerous(command) and self.cfg.get("confirm_dangerous", True):
            ok = self.confirm_callback(f"⚠ Commande potentiellement destructrice :\n  {command}\nConfirmer ? [o/N] ")
            if not ok:
                return "Commande annulée par l'utilisateur."

        cd_match = re.match(r'^\s*cd\s*(.*?)\s*$', command)
        if cd_match:
            target = cd_match.group(1).strip().strip("'\"") or str(Path.home())
            try:
                new_path = Path(target).expanduser()
                if not new_path.is_absolute():
                    new_path = Path(self.cwd) / new_path
                new_path = new_path.resolve()
                os.chdir(new_path)
                self.cwd = str(new_path)
                return f"→ {self.cwd}"
            except Exception as e:
                return f"Erreur cd : {e}"

        try:
            wrapped = f"{command} && echo '___CWD___' && pwd"
            proc = subprocess.Popen(
                wrapped, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=self.cwd, executable="/bin/bash",
            )
            lines = []
            timeout = self.cfg.get("bash_timeout", 60)
            import threading, time
            timed_out = [False]

            def _kill_after():
                time.sleep(timeout)
                if proc.poll() is None:
                    timed_out[0] = True
                    proc.kill()

            t = threading.Thread(target=_kill_after, daemon=True)
            t.start()

            for line in proc.stdout:
                lines.append(line)
                self.on_bash_line(line.rstrip("\n"))

            proc.wait()

            if timed_out[0]:
                return f"Erreur: timeout ({timeout}s)"

            full = "".join(lines)
            if "___CWD___" in full:
                before, after = full.split("___CWD___", 1)
                new_cwd = after.strip().splitlines()[0].strip() if after.strip() else ""
                if new_cwd and os.path.isdir(new_cwd):
                    self.cwd = new_cwd
                full = before
            out = full.strip() or "(aucune sortie)"
            return _truncate(out) + f"\n[exit code: {proc.returncode}]"
        except Exception as e:
            return f"Erreur: {e}"

    # ── fichiers ────────────────────────────────────────────────────────────
    def read_file(self, path: str, start: int = None, end: int = None) -> str:
        try:
            p = self._resolve(path)
            mtime = p.stat().st_mtime_ns
            key = (str(p), start, end)
            cached = self._read_cache.get(key)
            if cached and cached[0] == mtime:
                return f"(inchangé — contenu identique à la lecture précédente de {path})"
            text = p.read_text(encoding="utf-8", errors="replace")
            if start is not None or end is not None:
                lines = text.splitlines()
                s = (start or 1) - 1
                e = end if end is not None else len(lines)
                text = "\n".join(lines[max(s, 0):e])
            result = _truncate(text)
            self._read_cache[key] = (mtime, result)
            return result
        except Exception as e:
            return f"Erreur lecture : {e}"

    def write_file(self, path: str, content: str, mode: str = "overwrite") -> str:
        try:
            p = self._resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.write_text(content, encoding="utf-8")
            check = self._post_write_check(path)
            suffix = f"\n{check}" if check else ""
            return f"✓ Écrit ({len(content)} caractères) → {p}{suffix}"
        except Exception as e:
            return f"Erreur écriture : {e}"

    def edit_file(self, path: str, old: str, new: str, count: int = 1) -> str:
        try:
            p = self._resolve(path)
            text = p.read_text(encoding="utf-8")
            occurrences = text.count(old)
            if occurrences == 0:
                return f"Erreur : chaîne introuvable dans {p}"
            if count == 0:
                # 0 = pas de vérification de count : on ne remplace que la 1re occurrence
                text = text.replace(old, new, 1)
                p.write_text(text, encoding="utf-8")
                check = self._post_write_check(path)
                suffix = f"\n{check}" if check else ""
                return f"✓ Remplacé 1 occurrence (sur {occurrences}) dans {p}{suffix}"
            if occurrences != count:
                return f"Erreur : {occurrences} occurrence(s) trouvée(s), attendu {count}. Précise davantage le contexte."
            text = text.replace(old, new)
            p.write_text(text, encoding="utf-8")
            check = self._post_write_check(path)
            suffix = f"\n{check}" if check else ""
            return f"✓ Remplacé {occurrences} occurrence(s) dans {p}{suffix}"
        except Exception as e:
            return f"Erreur édition : {e}"

    def apply_patch(self, patch_text: str) -> str:
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as handle:
                handle.write(patch_text)
                patch_path = handle.name
            try:
                stripped = patch_text.lstrip()
                strip_level = "1" if stripped.startswith("--- a/") or stripped.startswith("diff --git ") else "0"
                command = ["patch", f"-p{strip_level}", "--forward", "--batch", "-i", patch_path]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    cwd=self.cwd,
                )
                output = (result.stdout + (("\n" + result.stderr) if result.stderr else "")).strip()
                if result.returncode != 0:
                    return f"Erreur patch : {output or 'échec inconnu'}"
                checks = []
                for m in re.finditer(r'^\+\+\+ b/(.+\.py)\s*$', patch_text, re.MULTILINE):
                    check = self._post_write_check(m.group(1))
                    if check:
                        checks.append(f"{m.group(1)} : {check}")
                suffix = "\n" + "\n".join(checks) if checks else ""
                return (output or "✓ Patch appliqué") + suffix
            finally:
                try:
                    os.unlink(patch_path)
                except Exception:
                    pass
        except Exception as e:
            return f"Erreur patch : {e}"

    def list_dir(self, path: str = ".") -> str:
        try:
            raw = path.strip() or "."
            p = self._resolve(raw)
            if not p.exists():
                return f"Erreur : '{raw}' n'existe pas. Le dossier courant est '{self.cwd}'. Essaie 'list_dir(\".\")' ou 'bash(\"ls\")'."
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            lines = []
            for e in entries:
                tag = "/" if e.is_dir() else ""
                size = "" if e.is_dir() else f" ({e.stat().st_size}o)"
                lines.append(f"{e.name}{tag}{size}")
            return "\n".join(lines) if lines else "(dossier vide)"
        except Exception as e:
            return f"Erreur : {e}"

    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(self.cwd) / p
        return p

    def _check_python_syntax(self, path: Path) -> str:
        """Vérifie la syntaxe d'un fichier .py sans créer de __pycache__."""
        try:
            p = path if path.is_absolute() else Path(self.cwd) / path
            code = "import sys\ncompile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')"
            r = subprocess.run(
                ["python3", "-I", "-S", "-c", code, str(p)],
                capture_output=True, text=True, timeout=15, cwd=self.cwd,
            )
            if r.returncode == 0:
                return "✓ synthaxe Python OK"
            err = (r.stderr or "").strip().splitlines()
            detail = "\n".join(err[-5:]) if err else "erreur inconnue"
            return f"⚠ Erreur de syntaxe Python :\n{detail}"
        except Exception as e:
            return f"⚠ Vérification syntaxe impossible : {e}"

    def _post_write_check(self, path: str) -> str:
        p = self._resolve(path)
        if p.suffix == ".py" and p.exists():
            return self._check_python_syntax(p)
        return ""

    # ── python sandbox (process séparé, pas d'accès direct à l'agent) ───────
    def run_python(self, code: str) -> str:
        log.info("run_python")
        try:
            r = subprocess.run(
                ["python3", "-I", "-S", "-c", code],
                capture_output=True, text=True, timeout=30, cwd=self.cwd,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(Path.home()),
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                },
            )
            out = (r.stdout + (("\n" + r.stderr) if r.stderr else "")).strip()
            out = out if out else "(aucune sortie)"
            return _truncate(out) + f"\n[exit code: {r.returncode}]"
        except subprocess.TimeoutExpired:
            return "Erreur: timeout (30s)"
        except Exception as e:
            return f"Erreur: {e}"

    # ── git ───────────────────────────────────────────────────────────────
    def git(self, args: str) -> str:
        return self.bash(f"git {args}")

    # ── question utilisateur ──────────────────────────────────────────────
    def question(self, question: str) -> str:
        return _ui.show_question(question)

    # ── todo list (état renvoyé tel quel, stateless) ────────────────────────
    def todo(self, tasks: list) -> str:
        icons = {"pending": "•", "in_progress": "▶", "done": "✓"}
        lines = []
        for i, t in enumerate(tasks or [], 1):
            if not isinstance(t, dict):
                continue
            title = str(t.get("title", "")).strip()
            if not title:
                continue
            status = t.get("status", "pending")
            if status not in icons:
                status = "pending"
            lines.append(f"{icons[status]} {i}. {title}")
        if not lines:
            return "Todo list vidée."
        return "Todo list mise à jour :\n" + "\n".join(lines)

    # ── http (lecture seule, pas d'env réseau arbitraire dangereux) ─────────
    def http_get(self, url: str) -> str:
        log.info(f"http_get: {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "odev-agent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(500_000).decode("utf-8", errors="replace")
                return _truncate(data)
        except urllib.error.URLError as e:
            return f"Erreur réseau : {e}"
        except Exception as e:
            return f"Erreur : {e}"


# ── Schémas JSON exposés au modèle ──────────────────────────────────────────
SUBAGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": "Délègue une tâche à un sous-agent spécialisé (coder, reviewer, explorer, planner) et récupère son résultat. Utilise ça pour diviser une tâche complexe en sous-tâches confiées à des experts.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "enum": ["coder", "reviewer", "explorer", "planner"],
                    "description": "Type de sous-agent",
                },
                "task": {
                    "type": "string",
                    "description": "Description précise de la tâche",
                },
                "context": {
                    "type": "string",
                    "description": "Contexte utile au sous-agent (optionnel)",
                },
            },
            "required": ["agent_type", "task"],
        },
    },
}

NATIVE_TOOL_SCHEMAS = [SUBAGENT_TOOL_SCHEMA,
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Exécute une commande shell. Gère fichiers, paquets, réseau, processus, compilation, tests.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Commande bash exacte."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lit le contenu d'un fichier texte, en entier ou par plage de lignes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "Ligne de début (1-indexée), optionnel."},
                    "end": {"type": "integer", "description": "Ligne de fin, optionnel."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Écrit ou ajoute du contenu dans un fichier (le crée si besoin).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Remplace une chaîne exacte (old) par une autre (new) dans un fichier existant. Utilise ceci plutôt que de réécrire tout le fichier pour des modifications ciblées.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "Texte exact à remplacer."},
                    "new": {"type": "string", "description": "Texte de remplacement."},
                    "count": {"type": "integer", "description": "Nombre d'occurrences attendu. 0 = remplacer uniquement la première occurrence.", "default": 1},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Applique un diff unifié sur le projet courant. Préfère cet outil pour les modifications multi-lignes ou les changements structurés.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Contenu complet du patch unifié."},
                },
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Liste le contenu d'un dossier avec tailles.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Exécute un script Python dans un sous-processus isolé (30s max) et retourne stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Code Python complet à exécuter."}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Exécute une sous-commande git (status, diff, log, add, commit, branch, etc.) dans le dossier courant.",
            "parameters": {
                "type": "object",
                "properties": {"args": {"type": "string", "description": "Arguments git, ex: 'status -s' ou 'commit -m \"msg\"'."}},
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": "Remplace la todo list de la session. Utilise-la pour planifier un travail multi-étapes et la mettre à jour au fur et à mesure (un seul item in_progress à la fois).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "Liste complète des tâches (remplace l'ancienne liste).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Description de la tâche."},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"], "default": "pending"},
                            },
                            "required": ["title", "status"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "Pose une question à l'utilisateur et attend sa réponse textuelle. Utilise ceci quand tu as besoin d'une clarification, d'un choix, ou de précisions avant d'agir. N'essaie JAMAIS de deviner quand le contexte est ambigu.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "Question claire et concise pour l'utilisateur."}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Récupère le contenu brut d'une URL (GET uniquement, lecture seule, 500Ko max).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

NATIVE_TOOL_NAMES = {t["function"]["name"] for t in NATIVE_TOOL_SCHEMAS}
