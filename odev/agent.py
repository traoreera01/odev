"""
odev.agent — Boucle agentique : appelle le modèle, dispatch les tool calls,
gère le streaming, le fallback de parsing, et la compression d'historique.
"""

import re
import json

from .tools import ToolRunner, NATIVE_TOOL_SCHEMAS, NATIVE_TOOL_NAMES, SUBAGENT_TOOL_SCHEMA
from .skills import skill_tool_schemas, run_skill, SKILLS_REGISTRY
from .agents import get_system_prompt, get_allowed_tools
from .logger import log


THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

# Tokens de template de chat qui fuient parfois dans le contenu avec les petits modèles
# (Qwen/ChatML, Llama, etc.) quand le stop n'a pas coupé assez tôt.
SPECIAL_TOKEN_RE = re.compile(
    r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|eot_id\|>|<\|start_header_id\|>.*?<\|end_header_id\|>",
    re.DOTALL,
)

STOP_TOKENS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|eot_id|>"]


def strip_think(text: str):
    """Retire les blocs <think> et les tokens spéciaux résiduels, retourne (texte_propre, pensée_ou_None)."""
    m = THINK_RE.search(text)
    if not m:
        thought, clean = None, text
    else:
        thought = m.group(1).strip()
        clean = THINK_RE.sub("", text).strip()
    clean = SPECIAL_TOKEN_RE.sub("", clean).strip()
    return clean, (thought or None)


def extract_fallback_tool_calls(text: str) -> list:
    """Filet de sécurité quand le modèle ne fait pas de vrai tool-calling natif
    mais émet des blocs ```bash``` / ```json``` / ``` ou du JSON dans le texte.
    Collecte tous les motifs puis les trie par position et déduplique."""
    found = []  # (position, name, args)

    def _is_known(name: str) -> bool:
        return name in NATIVE_TOOL_NAMES or (name.startswith("skill_") and name[6:] in SKILLS_REGISTRY)

    # 1. Blocs ```json ou ```tool — format explicite demandé dans le prompt
    for m in re.finditer(r'```(?:json|tool)\s*\n?(.*?)\n?```', text, re.DOTALL | re.IGNORECASE):
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        name = obj.get("name", "")
        args = obj.get("arguments") or obj.get("parameters") or {}
        if not name:
            continue
        if _is_known(name):
            found.append((m.start(), name, args))
        elif "command" in args:
            found.append((m.start(), "bash", {"command": args["command"]}))

    # 2. Blocs ```bash / ```sh / ```shell explicites
    for m in re.finditer(r'```(?:bash|sh|shell)\s*\n(.*?)\n```', text, re.DOTALL | re.IGNORECASE):
        cmd = m.group(1).strip()
        if cmd:
            found.append((m.start(), "bash", {"command": cmd}))

    # 3. JSON en texte libre avec "name" + "arguments"
    for m in re.finditer(r'(\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*"(?:arguments|parameters)"\s*:\s*(\{[^{}]*\})[^{}]*\})', text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        name = obj.get("name", "")
        args = obj.get("arguments") or obj.get("parameters") or {}
        if _is_known(name):
            found.append((m.start(), name, args))

    # 4. Pattern tool_name({"key": "val"}) que certains modèles émettent
    known_tools = NATIVE_TOOL_NAMES | {f"skill_{s}" for s in SKILLS_REGISTRY}
    for tool_name in known_tools:
        pattern = rf'\b{re.escape(tool_name)}\s*\(\s*(\{{.*?\}})\s*\)'
        for m in re.finditer(pattern, text, re.DOTALL):
            try:
                args = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            found.append((m.start(), tool_name, args))

    # 5. Blocs ``` sans langage — interprétés comme shell si ça ressemble à une commande
    for m in re.finditer(r'```\s*\n(.+?)\n```', text, re.DOTALL):
        content = m.group(1).strip()
        if not content or content.startswith(("{", "[", "(")):
            continue
        if "<" in content and ">" in content:
            continue
        first_word = content.split()[0] if content.split() else ""
        if not re.match(r'^[a-zA-Z0-9_\./\\-]+$', first_word):
            continue
        if '\n' not in content and len(content) < 200:
            found.append((m.start(), "bash", {"command": content}))

    found.sort(key=lambda item: item[0])

    # Déduplique : les patterns peuvent matcher le même appel (ex. JSON libre
    # à l'intérieur d'un bloc ```json fenced).
    results = []
    seen = set()
    for _, name, args in found:
        key = (name, json.dumps(args, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        results.append((name, args))
    return results


def strip_fallback_blocks(text: str) -> str:
    text = re.sub(r'```(?:json|tool)\s*\{.*?\}\s*```', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'```(?:bash|sh|shell)\s*\n.*?\n```', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'```\s*\n.*?\n```', '', text, flags=re.DOTALL)
    text = re.sub(r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,[^{}]*"(?:arguments|parameters)"\s*:\s*\{[^{}]*\}[^{}]*\}', '', text, flags=re.DOTALL)
    return text.strip()


def build_tools(agent_type: str = "auto") -> list:
    allowed = get_allowed_tools(agent_type)
    schemas = NATIVE_TOOL_SCHEMAS if allowed is None else [
        s for s in NATIVE_TOOL_SCHEMAS if s["function"]["name"] in allowed
    ]
    return list(schemas) + skill_tool_schemas()


TOOL_RESULT_KEEP_CHARS = 1500
TOOL_RESULT_HEAD_CHARS = 400
RECENT_MESSAGES_KEEP_FULL = 10


def _compress_tool_result(msg: dict) -> dict:
    """Remplace le contenu d'un vieux tool result trop long par un extrait.
    Retourne une copie — l'historique persisté n'est pas muté."""
    content = msg.get("content") or ""
    if msg.get("role") != "tool" or len(content) <= TOOL_RESULT_KEEP_CHARS:
        return msg
    head = content[:TOOL_RESULT_HEAD_CHARS]
    name = msg.get("name", "?")
    note = f"\n… [résultat {name} tronqué : {len(content)} caractères au total]"
    return {**msg, "content": head + note}


def trim_history(messages: list, max_messages: int) -> list:
    """Garde le system prompt + les N derniers messages, en compressant les
    tool results anciens pour limiter la taille du contexte."""
    if not messages:
        return messages
    system = messages[0]
    rest = list(messages[1:])

    cutoff = max(0, len(rest) - RECENT_MESSAGES_KEEP_FULL)
    rest = [_compress_tool_result(m) if i < cutoff else m for i, m in enumerate(rest)]

    if len(rest) <= max_messages:
        return [system] + rest
    tail = rest[-max_messages:]
    # Évite de couper au milieu d'un échange tool sans son message assistant déclencheur
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    return [system] + tail


class Agent:
    def __init__(self, client, cfg: dict, agent_type: str = "auto", confirm_callback=None, on_bash_line=None, on_text=None, on_tool_start=None, on_tool_result=None, on_thought=None, on_stream_start=None, on_stream_token=None, on_stream_end=None, on_wait_start=None, on_wait_stop=None, depth: int = 0):
        self.client = client
        self.cfg = cfg
        self.agent_type = agent_type
        self.depth = depth
        self.runner = ToolRunner(cfg, confirm_callback=confirm_callback, on_bash_line=on_bash_line)
        self.on_text = on_text or (lambda t: None)
        self.on_tool_start = on_tool_start or (lambda kind, payload: None)
        self.on_tool_result = on_tool_result or (lambda kind, result: None)
        self.on_thought = on_thought or (lambda t: None)
        self.on_stream_start = on_stream_start or (lambda: None)
        self.on_stream_token = on_stream_token or (lambda token: None)
        self.on_stream_end = on_stream_end or (lambda: None)
        self.on_wait_start = on_wait_start or (lambda: None)
        self.on_wait_stop = on_wait_stop or (lambda: None)

    def call_model(self, model: str, messages: list):
        tools = build_tools(self.agent_type)
        use_stream = bool(self.cfg.get("stream", True))
        self.on_wait_start()
        try:
            resp = self.client.chat(
                model=model,
                messages=messages,
                tools=tools,
                stream=use_stream,
                keep_alive=self.cfg.get("keep_alive", "10m"),
                options={
                    "temperature": self.cfg.get("temperature", 0.1),
                    "num_ctx": self.cfg.get("num_ctx", 16384),
                    "stop": STOP_TOKENS,
                },
            )
        except Exception as e:
            self.on_wait_stop()
            log.error(f"appel modèle échoué: {e}")
            return f"[Erreur modèle] {e}", [], False

        if use_stream:
            raw_text_parts = []
            tool_calls = []
            started = False
            wait_open = True
            try:
                for chunk in resp:
                    if wait_open:
                        self.on_wait_stop()
                        wait_open = False
                    chunk_msg = getattr(chunk, "message", None)
                    if chunk_msg is None and isinstance(chunk, dict):
                        chunk_msg = chunk.get("message")
                    if chunk_msg is None:
                        continue

                    content = getattr(chunk_msg, "content", None)
                    if content is None and isinstance(chunk_msg, dict):
                        content = chunk_msg.get("content")
                    if content:
                        if not started:
                            self.on_stream_start()
                            started = True
                        raw_text_parts.append(content)
                        self.on_stream_token(content)

                    chunk_calls = getattr(chunk_msg, "tool_calls", None)
                    if chunk_calls is None and isinstance(chunk_msg, dict):
                        chunk_calls = chunk_msg.get("tool_calls")
                    if chunk_calls:
                        tool_calls = list(chunk_calls)
            except Exception as e:
                log.error(f"stream modèle échoué: {e}")
                self.on_stream_end()
                return f"[Erreur modèle] {e}", [], False
            finally:
                if wait_open:
                    self.on_wait_stop()

            self.on_stream_end()
            raw_text = "".join(raw_text_parts)
            msg_tool_calls = tool_calls
        else:
            self.on_wait_stop()
            msg = resp.message
            raw_text = msg.content or ""
            msg_tool_calls = list(msg.tool_calls or [])

        text, thought = strip_think(raw_text)
        if thought:
            self.on_thought(thought)

        calls = []
        if msg_tool_calls:
            for tc in msg_tool_calls:
                name = tc.function.name
                args = tc.function.arguments or {}
                calls.append((name, args))
        else:
            fallback = extract_fallback_tool_calls(text)
            if fallback:
                calls = fallback
                text = strip_fallback_blocks(text)

        return text, calls, use_stream

    def dispatch(self, name: str, args: dict) -> str:
        # Validation des paramètres requis avant exécution
        required: dict[str, list[str]] = {
            "bash": ["command"],
            "read_file": ["path"],
            "write_file": ["path", "content"],
            "edit_file": ["path", "old", "new"],
            "apply_patch": ["patch"],
            "run_python": ["code"],
            "git": ["args"],
            "http_get": ["url"],
            "question": ["question"],
            "subagent": ["agent_type", "task"],
            "todo": ["tasks"],
        }
        if name in required:
            missing = [p for p in required[name] if not args.get(p)]
            if missing:
                return (
                    f"Erreur d'appel de l'outil '{name}' : paramètre(s) manquant(s) : {', '.join(missing)}. "
                    f"Réémets l'appel avec tous les paramètres requis : {required[name]}."
                )

        if name == "bash":
            return self.runner.bash(args["command"])
        if name == "read_file":
            return self.runner.read_file(args["path"], args.get("start"), args.get("end"))
        if name == "write_file":
            return self.runner.write_file(args["path"], args["content"], args.get("mode", "overwrite"))
        if name == "edit_file":
            return self.runner.edit_file(args["path"], args["old"], args["new"], args.get("count", 1))
        if name == "apply_patch":
            return self.runner.apply_patch(args["patch"])
        if name == "list_dir":
            return self.runner.list_dir(args.get("path", "."))
        if name == "run_python":
            return self.runner.run_python(args["code"])
        if name == "git":
            return self.runner.git(args["args"])
        if name == "http_get":
            return self.runner.http_get(args["url"])
        if name == "question":
            return self.runner.question(args["question"])
        if name == "todo":
            return self.runner.todo(args.get("tasks", []))
        if name == "subagent":
            return self.run_subagent(args["agent_type"], args["task"], args.get("context", ""))
        if name.startswith("skill_"):
            return run_skill(name[6:], args)
        known = ", ".join(sorted(NATIVE_TOOL_NAMES)) + (", " + ", ".join(f"skill_{s}" for s in SKILLS_REGISTRY) if SKILLS_REGISTRY else "")
        return f"Erreur : l'outil '{name}' n'existe pas. Outils disponibles : {known}."

    def run_subagent(self, sub_agent_type: str, task: str, context: str = "") -> str:
        """Crée un sous-agent spécialisé, exécute une tâche, retourne le résultat."""
        max_depth = self.cfg.get("max_subagent_depth", 2)
        if self.depth >= max_depth:
            return (
                f"[odev] Limite de profondeur des sous-agents atteinte "
                f"(profondeur {self.depth}/{max_depth}). Réponds avec les informations "
                "déjà collectées au lieu de déléguer plus loin."
            )
        sub_cfg = dict(self.cfg)
        sub_cfg["max_loops"] = min(sub_cfg.get("max_loops", 24), 12)
        sub_cfg["stream"] = True

        def on_sub_text(t):
            pass
        def on_sub_tool_start(k, p):
            self.on_tool_start(k, p)
        def on_sub_tool_result(k, r):
            self.on_tool_result(k, r)
        def on_sub_thought(t):
            self.on_thought(f"[{sub_agent_type}] {t}")
        def on_sub_stream_start():
            pass
        def on_sub_stream_token(tok):
            pass
        def on_sub_stream_end():
            pass

        sub_agent = Agent(
            self.client, sub_cfg, agent_type=sub_agent_type,
            on_bash_line=self.runner.on_bash_line,
            on_text=on_sub_text,
            on_tool_start=on_sub_tool_start,
            on_tool_result=on_sub_tool_result,
            on_thought=on_sub_thought,
            on_stream_start=on_sub_stream_start,
            on_stream_token=on_sub_stream_token,
            on_stream_end=on_sub_stream_end,
            depth=self.depth + 1,
        )

        prompt = get_system_prompt(sub_agent_type, "Tu es un sous-agent spécialisé. Réponds en français de manière concise.")
        if context:
            prompt += f"\n\nContexte fourni par l'agent principal:\n{context}"

        sub_messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task},
        ]

        sub_messages = sub_agent.turn(self.cfg["model"], sub_messages)

        result_parts = []
        for msg in sub_messages:
            if msg["role"] == "assistant" and msg.get("content", "").strip():
                result_parts.append(msg["content"].strip())
            if msg["role"] == "tool" and msg.get("content", "").strip():
                result_parts.append(f"[{msg['name']}] {msg['content'].strip()}")

        result = "\n".join(result_parts) if result_parts else "(aucun résultat)"
        self.on_tool_result(f"subagent:{sub_agent_type}", result)
        return result

    def _maybe_compact(self, model: str, messages: list) -> list:
        """Si compact='smart' et l'historique déborde, résume les messages
        anciens via un appel modèle dédié au lieu de les tronquer."""
        if self.cfg.get("compact") != "smart":
            return messages
        max_msgs = self.cfg.get("max_history_messages", 60)
        keep = 20
        if len(messages) <= max_msgs + 1 or len(messages) <= keep + 2:
            return messages
        system, middle, recent = messages[0], messages[1:-keep], messages[-keep:]
        if not middle:
            return messages
        transcript = "\n".join(
            f"{m.get('role')}: {(m.get('content') or '')[:400]}"
            for m in middle
        )
        try:
            resp = self.client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "Tu résumes des transcripts d'agent de façon concise en français."},
                    {"role": "user", "content": (
                        "Résume ces messages antérieurs en 10 lignes max. Garde : les décisions prises, "
                        "les fichiers modifiés, les résultats d'outils importants, la tâche en cours.\n\n"
                        + transcript
                    )},
                ],
                stream=False,
                options={"temperature": 0.1, "num_ctx": self.cfg.get("num_ctx", 16384)},
            )
            summary = ((getattr(resp, "message", None) or {}).get("content")
                       if isinstance(getattr(resp, "message", None), dict)
                       else getattr(getattr(resp, "message", None), "content", ""))
            summary = (summary or "").strip()
        except Exception:
            return messages
        if not summary:
            return messages
        self.on_text(f"[compact] historique résumé ({len(middle)} messages → 1)")
        return [
            system,
            {"role": "user", "content": "[Résumé des messages précédents]\n" + summary},
            {"role": "assistant", "content": "Compris, je continue avec ce contexte."},
        ] + recent

    def turn(self, model: str, messages: list) -> list:
        """Exécute un tour complet (jusqu'à boucle max, réponse finale, ou répétition détectée)."""
        loops = 0
        max_loops = self.cfg.get("max_loops", 24)
        last_signature = None
        repeat_count = 0
        empty_retry_used = False
        while loops < max_loops:
            loops += 1
            messages = self._maybe_compact(model, messages)
            messages = trim_history(messages, self.cfg.get("max_history_messages", 60))
            text, calls, streamed = self.call_model(model, messages)

            if not calls:
                if not text.strip():
                    if not empty_retry_used:
                        empty_retry_used = True
                        messages.append({
                            "role": "user",
                            "content": "(Réponds maintenant en texte clair avec ta réponse finale, sans appeler d'outil.)",
                        })
                        continue
                    empty_notice = (
                        "[odev] Le modèle n'a renvoyé aucune réponse exploitable "
                        "(ni outil, ni texte) après avoir consulté les informations "
                        "nécessaires. C'est une limite courante des très petits modèles "
                        "(<3B) sur les tâches en plusieurs étapes — réessaie la demande "
                        "directement, ou utilise un modèle plus capable "
                        "(ex: qwen2.5-coder:7b) pour ce type de synthèse."
                    )
                    self.on_text(empty_notice)
                    messages.append({"role": "assistant", "content": empty_notice})
                else:
                    if not streamed:
                        self.on_text(text)
                    messages.append({"role": "assistant", "content": text})
                return messages

            if text.strip() and not streamed:
                self.on_text(text)

            # Détecte un appel identique répété (signe d'un modèle bloqué/trop petit)
            signature = "|".join(sorted(
                f"{name}:{json.dumps(args, sort_keys=True, default=str)}" for name, args in calls
            ))
            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 0
            last_signature = signature

            tool_calls_payload = [
                {"function": {"name": name, "arguments": args}} for name, args in calls
            ]
            messages.append({
                "role": "assistant",
                "content": text or "",
                "tool_calls": tool_calls_payload,
            })

            if repeat_count >= 2:
                warning = (
                    "[odev] Le même appel d'outil a été répété 3 fois de suite sans "
                    "progresser. Arrêt de ce tour pour éviter une boucle infinie — "
                    "reformule ta demande ou utilise un modèle plus capable pour le "
                    "tool-calling (ex: qwen2.5-coder:7b ou plus)."
                )
                messages.append({"role": "tool", "content": warning, "name": "_loop_guard"})
                self.on_tool_result("_loop_guard", warning)
                messages.append({"role": "assistant", "content": warning})
                return messages

            tool_errors = []
            for name, args in calls:
                self.on_tool_start(name, args)
                result = self.dispatch(name, args)
                self.on_tool_result(name, result)
                messages.append({"role": "tool", "content": result, "name": name})
                if result.startswith("Erreur"):
                    tool_errors.append(f"[{name}] {result}")

            if tool_errors:
                hint = "Un ou plusieurs outils ont retourné une erreur :\n" + "\n".join(tool_errors)
                hint += "\nAnalyse ces erreurs et corrige tes appels. Ne répète pas la même commande en échec."
                messages.append({"role": "user", "content": hint})

            if repeat_count >= 1:
                messages.append({
                    "role": "user",
                    "content": "Tu répètes la même commande. Ne refais pas d'appel outil. Donne ta réponse finale en français avec les résultats obtenus.",
                })

        messages.append({
            "role": "assistant",
            "content": f"[Arrêt : limite de {max_loops} itérations d'outils atteinte pour ce tour. Demande-moi de continuer si besoin.]",
        })
        return messages
