# AGENTS.md — odev v2

## Quick start

- `pip install -e .` → `odev` command; `python3 odev_main.py` without install.
- Ollama required (`ollama serve` + tool-calling model, default `qwen2.5-coder:7b`).
- `odev --resume` restores `_last.json` session.

## Commands

```
make run/dev   → python3 odev_main.py
make test      → python3 -m unittest discover -s tests -p 'test_*.py'
make check     → python3 -m compileall odev odev_main.py
make clean     → find . -type d -name __pycache__ -prune -exec rm -rf {} +
```

CI: `pip install -e .` → `make test` → `make check`.

## Layout

`odev/cli.py` — argparse, slash commands, shell detection, main loop, `/agent` switcher.  
`odev/agent.py` — Agent class with tool-calling loop, streaming, history trim, repeat guard, fallback parser, **sub-agent delegation**.  
`odev/tools.py` — ToolRunner (persistent cwd): bash, read/write/edit_file, apply_patch, list_dir, run_python, git, http_get, question, **todo** (stateless todo list), **subagent tool schema**.  
`odev/agents/` — registry of specialized agents (coder, reviewer, explorer, planner) with dedicated system prompts.  
`odev/config.py` — loads `~/.config/odev/config.json` (overridable via `ODEV_HOME`).  
`odev/sessions.py` — persist/load sessions as JSON in `sessions/`.  
`odev/memory.py` — persistent summary/tasks/notes in `memory.json`.  
`odev/repomap.py` — AST-based repo map (Python symbols, ~800 tokens) injected into the system prompt when `repo_map: true` and ≥5 `.py` files exist.  
`odev/providers.py` — LLM backends: `ollama` (default), `openai` (any OpenAI-compatible API: OpenRouter, Groq, DeepSeek…), `anthropic`. Responses normalized to ollama-like objects; API key read from env var named by `api_key_env` (default `ODEV_API_KEY`).
`odev/skills.py` — dynamic skill loader (bundled `odev/skills/` + user `~/.config/odev/skills/`).  
`odev/ui.py` — Rich TUI: 5 themes (`codex`, `claude`, `gemini`, `nord`, `catppuccin`), syntax highlighting, thinking/subagent panels.  
`odev/logger.py` — rotating file logger → `odev.log`.

Entry: `pyproject.toml` → `odev.cli:main`; `odev_main.py` sets `sys.path`.

## Multi-agent system

`/agent <type>` switches the model's persona. Available types:

| Type | Role |
|---|---|
| `auto` | Choix automatique selon la tâche (défaut) |
| `coder` | Écriture et débogage de code |
| `reviewer` | Revue de code et analyse qualité |
| `explorer` | Exploration et compréhension de codebase (lecture seule) |
| `planner` | Planification de tâches complexes |

Agents can delegate to each other recursively via the **`subagent`** tool:
- `subagent("explorer", "trouve les points d'entrée", context="...")` → explore la base
- `subagent("reviewer", "revise ce fichier")` → analyse qualité
- `subagent("planner", "planifie cette feature")` → décompose en étapes

Each sub-agent runs in isolation with its own specialized system prompt, then returns results to the caller. Delegation depth is capped by `max_subagent_depth` (default 2) and `max_loops` is capped at 12 for sub-agents.

## Testing quirks

- Tests **must** use `import_with_temp_home()` which sets `ODEV_HOME` to a temp dir and re-imports `odev.*` modules — do not break this isolation.
- `AgentLoopTests` uses a `_FakeClient` (queued `chat()` responses, non-streaming) to test `Agent.turn()` without Ollama — extend it for loop/dispatch changes.
- `test_apply_patch` requires `patch` on `$PATH`.
- Dependencies: `ollama>=0.4.0`, `rich>=13.0.0`, `pygments>=2.15.0` (uv.lock present, pip also works).

## Non-obvious behavior

- `cd` in bash tool is **intercepted** by ToolRunner (not run via subprocess). `cd` into a non-existent dir returns an error string, does not throw.
- `/cd` resyncs `agent.runner.cwd` after every slash command (`cli.py`) — the ToolRunner cwd is independent of `os.getcwd()`.
- Known limitation: `explorer`/`planner` tool restrictions remove write tools but they keep `bash`/`git`/`run_python`, so a sub-agent can still modify files via shell. `AGENT_TYPES[*].readonly` is metadata only (`is_readonly` is unused).
- `bash`/`run_python` results always end with `[exit code: N]` — the model can tell success from failure.
- `write_file`/`edit_file`/`apply_patch` on `.py` files auto-run a `compile()` syntax check and append the result (no `__pycache__` created).
- `edit_file` `count=0` replaces **only the first occurrence** (count>0 still requires an exact occurrence match).
- `read_file` caches per (path, start, end) keyed on mtime — a repeated read of an unchanged file returns `(inchangé …)` instead of the content.
- `trim_history` also compresses old tool results (>1500 chars → 400-char head + note), keeping the last 10 messages untouched; persisted history is never mutated (copies are made).
- Raw shell commands typed at the CLI prompt (matched against `SHELL_BUILTINS` in `cli.py:61`) **skip the agent entirely** and run directly via ToolRunner.
- `run_python` uses `python3 -I -S` (isolated, no site packages).
- For `openai`/`anthropic` providers, `num_ctx`/`keep_alive` are ignored (no equivalent); Anthropic `max_tokens` defaults to 8192. Tool-call ids are synthesized (`call_{i}_{j}` / `toolu_{i}_{j}`) since odev history doesn't store provider ids.
- `apply_patch` auto-detects strip level (1 if patch starts with `--- a/`, else 0).
- History trimmed to 60 messages (`max_history_messages`) + tool-result compression. 3 identical tool calls in a row → loop guard triggered.
- Fallback parser extracts tool calls from ```bash and ```json fences (for small models without native tool-calling).
- `<think>...</think>` blocks are captured and displayed as a thinking panel in the UI. Special tokens (`<|im_start|>`, `<|eot_id|>`) are stripped from output.
- Empty model response → one retry with "reply in plain text" prompt.
- Agent config (`cfg`) is **mutated on the agent object** after every slash command (`cli.py:357-358`).

## Skills

Write a `.py` to `~/.config/odev/skills/` with `get_description()`, optional `get_schema()`, and `run(ctx, args)`.  
Bundled: `odev/skills/` (no `__init__.py` — flat file collection).  
`/reload` hot-reloads from disk.

## Configuration (`/set` or `~/.config/odev/config.json`)

`model`, `provider` (ollama/openai/anthropic), `api_base` (empty = provider default), `api_key_env` (`ODEV_API_KEY`), `ollama_host` (also `OLLAMA_HOST` env), `num_ctx` (16384), `temperature` (0.1), `max_loops` (24), `max_subagent_depth` (2), `bash_timeout` (60), `confirm_dangerous`, `auto_save_session`, `repo_map` (true), `compact` (`trim`|`smart` — smart résume l'historique via le modèle au lieu de tronquer), `ui_style` (codex/claude/gemini/nord/catppuccin).

<!-- memexa:start -->
## Memexa memory

Memexa is this project's persistent memory: decisions, bugs, conventions and progress. It outlives your context window and is shared with the team. Use it without being asked.

1. At the start of every task or session, call get_context before reading code or answering. It tells you what was decided, what is in progress and what is known to be broken.
2. When you or the user choose between alternatives (library, pattern, architecture, convention), call add_decision right away with the title, the reason and the alternatives rejected.
3. After you diagnose and fix a bug, call log_bug with the root cause and the fix.
4. Before proposing a design or reversing earlier work, call search_memory to check that you do not contradict a previous decision.
5. At the end of a session, call update_context once with what is done, what is in progress, the next step and any blockers.

Rules:
- Record the why, which the code does not show. Do not record what the diff already says.
- Never store secrets, tokens, passwords or personal data.
- One entry per decision: search before adding, do not duplicate.
- If a Memexa call fails, carry on with the task and mention it. Never block on Memexa.
<!-- memexa:end -->
