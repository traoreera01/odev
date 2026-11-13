"""
odev.providers — Backends LLM : Ollama, API compatible OpenAI, Anthropic.

Chaque provider expose la même interface qu'ollama.Client :
    chat(model=..., messages=..., tools=..., stream=..., options={...})
    list() -> objet avec .models (chaque modèle a .model)

Les réponses sont normalisées en objets "ollama-like" (attributs .message.content
et .message.tool_calls) pour que Agent n'ait pas à connaître le provider.
"""

import json
import os
import time
import urllib.request
import urllib.error
from types import SimpleNamespace

import ollama


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


# ── Ollama ────────────────────────────────────────────────────────────────────

class OllamaProvider:
    def __init__(self, host: str):
        self._client = ollama.Client(host=host)

    def chat(self, **kwargs):
        return self._client.chat(**kwargs)

    def list(self):
        return self._client.list()


# ── Conversion de messages (tool_call_id pour OpenAI/Anthropic) ───────────────

def _tool_calls_of(msg: dict) -> list:
    """Normalise les tool_calls stockés (format ollama: {"function": {...}})."""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", tc) if isinstance(tc, dict) else getattr(tc, "function", tc)
        if isinstance(fn, dict):
            out.append({"name": fn.get("name", ""), "arguments": fn.get("arguments") or {}})
        else:
            out.append({"name": getattr(fn, "name", ""), "arguments": getattr(fn, "arguments", {}) or {}})
    return out


def _to_openai_messages(messages: list) -> list:
    out = []
    pending_ids = []
    for idx, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            out.append({"role": "system", "content": content})
        elif role == "tool":
            cid = pending_ids.pop(0) if pending_ids else (m.get("name") or "call")
            out.append({"role": "tool", "tool_call_id": cid, "content": content})
        elif role == "assistant" and m.get("tool_calls"):
            tcs = []
            for j, call in enumerate(_tool_calls_of(m)):
                cid = f"call_{idx}_{j}"
                pending_ids.append(cid)
                tcs.append({
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                })
            out.append({"role": "assistant", "content": content or None, "tool_calls": tcs})
        else:
            out.append({"role": role if role in ("user", "assistant") else "user", "content": content})
    return out


def _to_anthropic_messages(messages: list):
    """Retourne (system_prompt, messages) au format Anthropic."""
    system_parts = []
    out = []
    pending_ids = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            system_parts.append(content)
        elif role == "tool":
            # Regroupe les tool results consécutifs dans un seul message user
            blocks = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                cid = pending_ids.pop(0) if pending_ids else (tm.get("name") or "tool")
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": cid,
                    "content": tm.get("content") or "",
                })
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for j, call in enumerate(_tool_calls_of(m)):
                cid = f"toolu_{i}_{j}"
                pending_ids.append(cid)
                blocks.append({
                    "type": "tool_use",
                    "id": cid,
                    "name": call["name"],
                    "input": call["arguments"],
                })
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({
                "role": role if role in ("user", "assistant") else "user",
                "content": content,
            })
        i += 1
    return "\n\n".join(p for p in system_parts if p), out


# ── Helpers HTTP ──────────────────────────────────────────────────────────────

USER_AGENT = "odev/2.0"
MAX_RETRIES = 3
RETRYABLE_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def _http_json(method: str, url: str, payload: dict = None, headers: dict = None, timeout: int = 300) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", USER_AGENT)  # Cloudflare bloque le UA Python-urllib par défaut
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"HTTP {e.code} : {detail}")
            if e.code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
                raise last_error from e
            retry_after = e.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(2 ** attempt * 2, 30)
            except ValueError:
                delay = min(2 ** attempt * 2, 30)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = RuntimeError(f"réseau : {e}")
            if attempt == MAX_RETRIES:
                raise last_error from e
            time.sleep(min(2 ** attempt * 2, 30))
    raise last_error


def _sse_lines(resp) -> list:
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith("data:"):
            yield line[5:].strip()


def _open_stream(url: str, body: bytes, headers: dict, extra: dict = None):
    """Ouvre un stream SSE avec retry sur codes retryables."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("User-Agent", USER_AGENT)
        for k, v in headers.items():
            req.add_header(k, v)
        for k, v in (extra or {}).items():
            req.add_header(k, v)
        try:
            return urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            last_error = RuntimeError(f"HTTP {e.code} : {detail}")
            if e.code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
                raise last_error from e
            time.sleep(min(2 ** attempt * 2, 30))
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = RuntimeError(f"réseau : {e}")
            if attempt == MAX_RETRIES:
                raise last_error from e
            time.sleep(min(2 ** attempt * 2, 30))
    raise last_error


# ── API compatible OpenAI (OpenAI, OpenRouter, Groq, Together, DeepSeek…) ─────

class OpenAICompatProvider:
    DEFAULT_BASE = "https://api.openai.com/v1"

    def __init__(self, base: str = None, api_key: str = None):
        self.base = (base or self.DEFAULT_BASE).rstrip("/")
        self.api_key = api_key or ""

    def _headers(self) -> dict:
        h = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list(self):
        data = _http_json("GET", f"{self.base}/models", headers=self._headers(), timeout=30)
        return _ns(models=[_ns(model=m.get("id", "")) for m in data.get("data", [])])

    def chat(self, model, messages, tools=None, stream=False, options=None, keep_alive=None, **_):
        options = options or {}
        payload = {
            "model": model,
            "messages": _to_openai_messages(messages),
            "stream": bool(stream),
        }
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            return self._stream(payload)
        data = _http_json("POST", f"{self.base}/chat/completions", payload, self._headers())
        return _openai_to_ollama(data)

    def _stream(self, payload):
        body = json.dumps(payload).encode()
        resp = _open_stream(f"{self.base}/chat/completions", body, self._headers(),
                            extra={"Content-Type": "application/json"})
        return self._gen(resp)

    def _gen(self, resp):
        tool_acc = {}
        for data in _sse_lines(resp):
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            err = chunk.get("error")
            if err:
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise RuntimeError(f"API : {msg}")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            content = delta.get("content")
            if content:
                yield _ns(message=_ns(content=content, tool_calls=None))
        if tool_acc:
            calls = []
            for slot in tool_acc.values():
                try:
                    args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(_ns(function=_ns(name=slot["name"], arguments=args)))
            yield _ns(message=_ns(content="", tool_calls=calls))


def _openai_to_ollama(data: dict):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {}
        calls.append(_ns(function=_ns(name=fn.get("name", ""), arguments=args or {})))
    return _ns(message=_ns(content=msg.get("content") or "", tool_calls=calls or None))


# ── Anthropic ─────────────────────────────────────────────────────────────────

class AnthropicProvider:
    DEFAULT_BASE = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(self, base: str = None, api_key: str = None):
        self.base = (base or self.DEFAULT_BASE).rstrip("/")
        self.api_key = api_key or ""

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }

    def list(self):
        data = _http_json("GET", f"{self.base}/v1/models", headers=self._headers(), timeout=30)
        return _ns(models=[_ns(model=m.get("id", "")) for m in data.get("data", [])])

    def chat(self, model, messages, tools=None, stream=False, options=None, keep_alive=None, **_):
        options = options or {}
        system, convo = _to_anthropic_messages(messages)
        payload = {
            "model": model,
            "max_tokens": options.get("max_tokens", 8192),
            "messages": convo,
        }
        if system:
            payload["system"] = system
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if tools:
            payload["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters") or {"type": "object"},
                }
                for t in tools
            ]
        if stream:
            return self._stream(payload)
        data = _http_json("POST", f"{self.base}/v1/messages", payload, self._headers())
        return _anthropic_to_ollama(data)

    def _stream(self, payload):
        body = json.dumps(payload).encode()
        resp = _open_stream(f"{self.base}/v1/messages", body, self._headers(),
                            extra={"Content-Type": "application/json", "Accept": "text/event-stream"})
        return self._gen(resp)

    def _gen(self, resp):
        tool_blocks = {}
        current_event = None
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "error" or obj.get("error"):
                    err = obj.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"API : {msg}")
                if current_event == "content_block_start":
                    cb = obj.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        tool_blocks[obj.get("index", 0)] = {
                            "name": cb.get("name", ""),
                            "json": "",
                        }
                elif current_event == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield _ns(message=_ns(content=delta["text"], tool_calls=None))
                    elif delta.get("type") == "input_json_delta":
                        idx = obj.get("index", 0)
                        if idx in tool_blocks:
                            tool_blocks[idx]["json"] += delta.get("partial_json", "")
                elif current_event in ("message_stop", "error"):
                    break
        if tool_blocks:
            calls = []
            for block in tool_blocks.values():
                try:
                    args = json.loads(block["json"]) if block["json"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(_ns(function=_ns(name=block["name"], arguments=args)))
            yield _ns(message=_ns(content="", tool_calls=calls))


def _anthropic_to_ollama(data: dict):
    text_parts = []
    calls = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            calls.append(_ns(function=_ns(name=block.get("name", ""), arguments=block.get("input") or {})))
    return _ns(message=_ns(content="".join(text_parts), tool_calls=calls or None))


# ── Factory ───────────────────────────────────────────────────────────────────

def get_client(cfg: dict):
    """Retourne le client LLM selon cfg['provider'] : ollama | openai | anthropic."""
    provider = cfg.get("provider", "ollama")
    key = os.environ.get(cfg.get("api_key_env", "ODEV_API_KEY"), "")
    if provider == "openai":
        return OpenAICompatProvider(cfg.get("api_base") or None, key)
    if provider == "anthropic":
        return AnthropicProvider(cfg.get("api_base") or None, key)
    return OllamaProvider(cfg.get("ollama_host", "http://localhost:11434"))
