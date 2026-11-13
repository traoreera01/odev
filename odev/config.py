"""
odev.config — Configuration centralisée, chargement/écriture du fichier config.json
"""

import json
import os
from pathlib import Path

BASE_DIR     = Path(os.environ.get("ODEV_HOME", Path.home() / ".config" / "odev"))
ODEV_DIR     = BASE_DIR
SKILLS_DIR   = ODEV_DIR / "skills"
SESSIONS_DIR = ODEV_DIR / "sessions"
MEMORY_FILE  = ODEV_DIR / "memory.json"
LOG_FILE     = ODEV_DIR / "odev.log"
CONFIG_FILE  = ODEV_DIR / "config.json"
HISTORY_FILE = ODEV_DIR / "shell_history"

for d in (ODEV_DIR, SKILLS_DIR, SESSIONS_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "model": "qwen2.5-coder:7b",
    "provider": "ollama",          # ollama | openai | anthropic
    "api_base": "",                # base URL pour openai/anthropic (vide = défaut)
    "api_key_env": "ODEV_API_KEY", # nom de la variable d'env contenant la clé
    "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
    "keep_alive": "10m",
    "max_loops": 24,
    "num_ctx": 16384,
    "temperature": 0.1,
    "max_history_messages": 60,   # au-delà, on compresse/tronque
    "max_subagent_depth": 2,      # profondeur max de délégation subagent
    "bash_timeout": 60,
    "confirm_dangerous": True,
    "stream": True,
    "auto_save_session": True,
    "repo_map": True,
    "compact": "trim",           # "trim" = troncature, "smart" = résumé par le modèle
    "ui_style": "codex",
    "ui_compact": True,       # False = panneaux d'outils détaillés
    "editor": os.environ.get("EDITOR", "nano"),
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
