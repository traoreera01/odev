# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**odev** is a local CLI agent powered by Ollama. It exposes a set of native tools (shell, file I/O, git, HTTP, sandboxed Python) to a local LLM and lets it autonomously complete multi-step tasks. The agent loop runs in Python; there is no server, no cloud backend.

Prerequisite: Ollama must be running (`ollama serve`) with a function-calling model pulled (e.g. `ollama pull qwen2.5-coder:7b`).

## Commands

```bash
# Run
python3 odev_main.py          # without install
make run                      # alias

# Install as CLI
pip install -e .
odev                          # interactive session
odev "résume ce projet"       # one-shot
odev --model llama3.1:8b
odev --resume                 # resume last session

# Tests
make test                     # unittest discover in tests/
python3 -m unittest tests.test_core.ClassName.method_name  # single test

# Syntax check
make check

# Clean bytecode
make clean
```

Tests use stdlib `unittest` (no pytest). The project runs on Python 3.9+; the venv uses 3.14.

## Architecture

```
odev_main.py       Entry point — creates Ollama client, loads config/skills, launches CLI
odev/
  cli.py           REPL loop, slash commands (/help, /save, /load, /model, /reload…)
  agent.py         Agent class — call_model → dispatch → tool execution loop
  agents/__init__  Agent registry: system prompts + tool restrictions per agent type
  tools.py         ToolRunner — executes native tools (bash, read_file, write_file…)
  skills.py        Dynamic skill loader (project skills + ~/.config/odev/skills/)
  skills/          Built-in skills (code_review, architecture_mapper, tdd_guide, etc.)
  memory.py        Persistent session memory
  sessions.py      Save/load conversation history
  config.py        Config file path constants (~/.config/odev/config.json)
  ui.py            Rich-based terminal UI helpers
  completion.py    Shell/readline completion
```

### Agent types and tool access

Six agent types are defined in `odev/agents/__init__.py`:

| Type | Role | Can write files |
|---|---|---|
| `auto` | Orchestrator — delegates to others via `subagent()` | yes |
| `coder` | Writes and edits code | yes |
| `reviewer` | Code audit (may suggest patches) | yes |
| `explorer` | Read-only codebase analysis | no |
| `planner` | Task decomposition, no implementation | no |
| `debugger` | Root-cause analysis and fix | yes |

The `auto` agent is instructed to delegate all non-trivial tasks to specialized subagents. Subagents are invoked via the `subagent` native tool inside `Agent.run_subagent()`.

### Skills system

Skills are Python files exposing `get_description() -> str` and `run(ctx, args: dict) -> str`, with an optional `get_schema() -> dict`. They are loaded from two locations in priority order:

1. `odev/skills/` (built-in, bundled with the package)
2. `~/.config/odev/skills/` (user-defined, hot-reloadable with `/reload`)

Each skill becomes a tool named `skill_<filename_stem>` that the LLM can call. The schema is passed to the model as the function's parameter spec.

### Fallback tool-call parsing

When a model doesn't emit structured tool calls (common with small models), `extract_fallback_tool_calls()` in `agent.py` parses ` ```bash ``` ` blocks and JSON blobs from the raw text. This is the main robustness mechanism for sub-7B models.

### Configuration

`~/.config/odev/config.json` — key options:

- `model` — Ollama model name
- `ollama_host` — default `http://localhost:11434`
- `max_loops` — max tool-call iterations per turn (default 24)
- `num_ctx` — context window (default 16384)
- `temperature` — default 0.1
- `bash_timeout` — shell timeout in seconds
- `confirm_dangerous` — prompt before destructive shell commands
- `auto_save_session` — persist history automatically

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
