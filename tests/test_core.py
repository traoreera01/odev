import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
import unittest


def import_with_temp_home(module_name: str, temp_home: Path):
    os.environ["ODEV_HOME"] = str(temp_home)
    for name in list(sys.modules):
        if name == "odev" or name.startswith("odev."):
            sys.modules.pop(name)
    return importlib.import_module(module_name)


class MemoryTests(unittest.TestCase):
    def test_memory_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            memory_module = import_with_temp_home("odev.memory", temp_home)
            memory = memory_module.load_memory()
            memory_module.set_project_summary(memory, "CLI agent local")
            memory_module.add_task(memory, "Écrire les tests")
            memory_module.add_note(memory, "Préférer apply_patch")

            reloaded = memory_module.load_memory()
            self.assertEqual(reloaded["project_summary"], "CLI agent local")
            self.assertEqual(reloaded["active_tasks"][0]["title"], "Écrire les tests")
            self.assertIn("Préférer apply_patch", reloaded["notes"])

    def test_render_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            memory_module = import_with_temp_home("odev.memory", temp_home)
            memory = {
                "project_summary": "Résumé",
                "active_tasks": [{"id": 1, "title": "Tester", "done": False}],
                "notes": ["Note"],
            }
            rendered = memory_module.render_memory(memory)
            self.assertIn("Résumé projet:", rendered)
            self.assertIn("#1 Tester", rendered)
            self.assertIn("Notes récentes:", rendered)


class ToolTests(unittest.TestCase):
    def test_run_python_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            output = runner.run_python("import sys\nprint(sys.flags.isolated)")
            self.assertIn("1", output)

    @unittest.skipUnless(shutil.which("patch"), "patch command not available")
    def test_apply_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            runner.cwd = str(temp_home)
            target = temp_home / "sample.txt"
            target.write_text("hello\nworld\n", encoding="utf-8")
            patch_text = """--- a/sample.txt
+++ b/sample.txt
@@ -1,2 +1,2 @@
-hello
+hola
"""
            result = runner.apply_patch(patch_text)
            self.assertNotIn("Erreur patch", result)
            self.assertIn("hola", target.read_text(encoding="utf-8"))

    def test_bash_reports_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            self.assertIn("[exit code: 1]", runner.bash("false"))
            self.assertIn("[exit code: 0]", runner.bash("true"))

    def test_read_file_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            runner.cwd = str(temp_home)
            target = temp_home / "a.txt"
            target.write_text("hello\n", encoding="utf-8")
            first = runner.read_file("a.txt")
            self.assertIn("hello", first)
            second = runner.read_file("a.txt")
            self.assertIn("inchangé", second)
            target.write_text("changed\n", encoding="utf-8")
            os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1_000_000))
            third = runner.read_file("a.txt")
            self.assertIn("changed", third)

    def test_edit_file_count_zero_replaces_first_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            runner.cwd = str(temp_home)
            target = temp_home / "b.txt"
            target.write_text("x x x\n", encoding="utf-8")
            result = runner.edit_file("b.txt", "x", "y", count=0)
            self.assertIn("1 occurrence", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "y x x\n")

    def test_write_file_python_syntax_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            runner.cwd = str(temp_home)
            ok = runner.write_file("good.py", "x = 1\n")
            self.assertIn("synthaxe Python OK", ok)
            bad = runner.write_file("bad.py", "def broken(:\n")
            self.assertIn("Erreur de syntaxe", bad)


class AgentHelpersTests(unittest.TestCase):
    def test_extract_fallback_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            agent_module = import_with_temp_home("odev.agent", temp_home)
            calls = agent_module.extract_fallback_tool_calls(
                "```bash\npwd\n```\n```json\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"README.md\"}}\n```"
            )
            self.assertEqual(calls[0][0], "bash")
            self.assertEqual(calls[1][0], "read_file")

    def test_trim_history_keeps_system_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            agent_module = import_with_temp_home("odev.agent", temp_home)
            messages = [{"role": "system", "content": "system"}]
            for index in range(10):
                messages.append({"role": "user", "content": f"u{index}"})
                messages.append({"role": "assistant", "content": f"a{index}"})
            trimmed = agent_module.trim_history(messages, 4)
            self.assertEqual(trimmed[0]["role"], "system")
            self.assertLessEqual(len(trimmed), 5)

    def test_trim_history_compresses_old_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            agent_module = import_with_temp_home("odev.agent", temp_home)
            big = "x" * 5000
            messages = [{"role": "system", "content": "system"}]
            messages.append({"role": "assistant", "content": "a"})
            messages.append({"role": "tool", "content": big, "name": "bash"})
            for index in range(15):
                messages.append({"role": "user", "content": f"u{index}"})
                messages.append({"role": "assistant", "content": f"a{index}"})
            trimmed = agent_module.trim_history(messages, 60)
            tool_msg = next(m for m in trimmed if m.get("role") == "tool")
            self.assertIn("tronqué", tool_msg["content"])
            self.assertLess(len(tool_msg["content"]), 1000)
            # l'historique original n'est pas muté
            self.assertEqual(messages[2]["content"], big)


class SubagentDepthTests(unittest.TestCase):
    def test_subagent_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            agent_module = import_with_temp_home("odev.agent", temp_home)
            agent = agent_module.Agent(client=None, cfg={}, agent_type="auto", depth=99)
            result = agent.run_subagent("explorer", "analyse le projet")
            self.assertIn("profondeur", result.lower())

    def test_subagent_depth_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            agent_module = import_with_temp_home("odev.agent", temp_home)
            agent = agent_module.Agent(client=None, cfg={"model": "test-model"}, agent_type="auto", depth=0)
            captured = {}

            original_agent = agent_module.Agent

            class FakeAgent:
                def __init__(self, *args, **kwargs):
                    captured["depth"] = kwargs.get("depth")

                def turn(self, model, messages):
                    return messages

            agent_module.Agent = FakeAgent
            try:
                agent.run_subagent("explorer", "tâche")
            finally:
                agent_module.Agent = original_agent
            self.assertEqual(captured["depth"], 1)


class _FakeFunc:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class _FakeToolCall:
    def __init__(self, name, args):
        self.function = _FakeFunc(name, args)


class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeResp:
    def __init__(self, message):
        self.message = message


class _FakeClient:
    """Simule ollama.Client.chat en mode non-streaming."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeClient : plus de réponses programmées")
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def _agent(self, temp_home: Path, client, cfg_extra=None):
        agent_module = import_with_temp_home("odev.agent", temp_home)
        cfg = {"model": "fake", "stream": False, "max_loops": 24, "max_history_messages": 60}
        cfg.update(cfg_extra or {})
        return agent_module.Agent(client, cfg, agent_type="auto")

    def test_turn_executes_tool_then_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            client = _FakeClient([
                _FakeResp(_FakeMsg("", [_FakeToolCall("list_dir", {"path": "."})])),
                _FakeResp(_FakeMsg("Voici le résultat.")),
            ])
            agent = self._agent(temp_home, client)
            out = agent.turn("fake", [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "liste le dossier"},
            ])
            tool_msgs = [m for m in out if m.get("role") == "tool"]
            self.assertTrue(tool_msgs)
            self.assertEqual(tool_msgs[0]["name"], "list_dir")
            self.assertEqual(out[-1]["role"], "assistant")
            self.assertIn("résultat", out[-1]["content"])

    def test_loop_guard_stops_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            call = _FakeToolCall("list_dir", {"path": "."})
            client = _FakeClient([_FakeResp(_FakeMsg("", [call])) for _ in range(3)])
            agent = self._agent(temp_home, client)
            out = agent.turn("fake", [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "boucle"},
            ])
            self.assertTrue(any(m.get("name") == "_loop_guard" for m in out if m.get("role") == "tool"))
            self.assertEqual(len(client.calls), 3)

    def test_empty_response_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            client = _FakeClient([
                _FakeResp(_FakeMsg("")),
                _FakeResp(_FakeMsg("réponse finale")),
            ])
            agent = self._agent(temp_home, client)
            out = agent.turn("fake", [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "salut"},
            ])
            self.assertEqual(out[-1]["content"], "réponse finale")
            self.assertTrue(any("Réponds maintenant" in (m.get("content") or "") for m in out))

    def test_smart_compact_summarizes_old_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            client = _FakeClient([
                _FakeResp(_FakeMsg("Résumé: tâche X en cours")),
                _FakeResp(_FakeMsg("ok")),
            ])
            agent = self._agent(temp_home, client, {"compact": "smart"})
            messages = [{"role": "system", "content": "s"}]
            for i in range(70):
                role = "user" if i % 2 == 0 else "assistant"
                messages.append({"role": role, "content": f"message {i}"})
            out = agent.turn("fake", messages)
            self.assertIn("Résumé des messages précédents", out[1]["content"])
            self.assertIn("tâche X", out[1]["content"])


class RepoMapTests(unittest.TestCase):
    def _make_repo(self, root: Path, n: int):
        for i in range(n):
            (root / f"mod{i}.py").write_text(
                f"def func{i}():\n    pass\n\nclass Klass{i}:\n    def method(self):\n        pass\n",
                encoding="utf-8",
            )

    def test_repo_map_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp) / "home"
            root = Path(tmp) / "repo"
            root.mkdir()
            self._make_repo(root, 6)
            repomap_module = import_with_temp_home("odev.repomap", temp_home)
            rm = repomap_module.build_repo_map(root)
            self.assertIn("Repo map", rm)
            self.assertIn("mod0.py", rm)
            self.assertIn("func0()", rm)
            self.assertIn("Klass0", rm)

    def test_repo_map_too_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp) / "home"
            root = Path(tmp) / "repo"
            root.mkdir()
            self._make_repo(root, 2)
            repomap_module = import_with_temp_home("odev.repomap", temp_home)
            self.assertEqual(repomap_module.build_repo_map(root), "")


class TodoToolTests(unittest.TestCase):
    def test_todo_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            tools_module = import_with_temp_home("odev.tools", temp_home)
            runner = tools_module.ToolRunner({})
            out = runner.todo([
                {"title": "Analyser", "status": "done"},
                {"title": "Coder", "status": "in_progress"},
                {"title": "Tester", "status": "pending"},
            ])
            self.assertIn("✓ 1. Analyser", out)
            self.assertIn("▶ 2. Coder", out)
            self.assertIn("• 3. Tester", out)
            self.assertIn("vidée", runner.todo([]))


class ProviderConversionTests(unittest.TestCase):
    def _messages(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fais X"},
            {"role": "assistant", "content": "j'appelle", "tool_calls": [
                {"function": {"name": "bash", "arguments": {"command": "ls"}}},
            ]},
            {"role": "tool", "content": "fichier.txt", "name": "bash"},
            {"role": "assistant", "content": "terminé"},
        ]

    def test_openai_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            providers_module = import_with_temp_home("odev.providers", Path(tmp) / "home")
            out = providers_module._to_openai_messages(self._messages())
            self.assertEqual(out[0]["role"], "system")
            assistant = out[2]
            self.assertEqual(assistant["role"], "assistant")
            self.assertEqual(len(assistant["tool_calls"]), 1)
            self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "bash")
            self.assertIn('"command"', assistant["tool_calls"][0]["function"]["arguments"])
            tool = out[3]
            self.assertEqual(tool["role"], "tool")
            self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])

    def test_anthropic_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            providers_module = import_with_temp_home("odev.providers", Path(tmp) / "home")
            system, out = providers_module._to_anthropic_messages(self._messages())
            self.assertEqual(system, "sys")
            assistant = out[1]
            self.assertEqual(assistant["role"], "assistant")
            tool_use = [b for b in assistant["content"] if b["type"] == "tool_use"][0]
            tool_result_msg = out[2]
            self.assertEqual(tool_result_msg["role"], "user")
            result = tool_result_msg["content"][0]
            self.assertEqual(result["type"], "tool_result")
            self.assertEqual(result["tool_use_id"], tool_use["id"])

    def test_openai_stream_surfaces_sse_error(self):
        """Un événement error SSE (ex. Groq tool_use_failed) doit lever une
        exception au lieu de produire une réponse vide silencieuse."""
        with tempfile.TemporaryDirectory() as tmp:
            providers_module = import_with_temp_home("odev.providers", Path(tmp) / "home")
            provider = providers_module.OpenAICompatProvider("https://x", "k")

            class _Resp:
                def __iter__(self):
                    yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                    yield b'data: {"error": {"message": "tool_use_failed", "code": "tool_use_failed"}}\n\n'

            gen = provider._gen(_Resp())
            chunks = []
            with self.assertRaises(RuntimeError) as ctx:
                for c in gen:
                    chunks.append(c)
            self.assertIn("tool_use_failed", str(ctx.exception))


class SessionNameTests(unittest.TestCase):
    def test_session_name_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            sessions_module = import_with_temp_home("odev.sessions", temp_home)
            path = sessions_module.save_session("../../evil", "model", [])
            self.assertTrue(str(path).startswith(str(sessions_module.SESSIONS_DIR)))
            self.assertNotIn("/", path.name)
            self.assertTrue(sessions_module.load_session("../../evil") is not None)


class SkillLoadingTests(unittest.TestCase):
    def test_local_skills_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            skills_module = import_with_temp_home("odev.skills", temp_home)
            loaded, errors = skills_module.load_skills()
            self.assertFalse(errors)
            for expected in {
                "code_review",
                "code_review_deep",
                "tdd_guide",
                "bug_triage",
                "refactor_planner",
                "api_review",
                "debug_python",
                "test_generator",
                "architecture_mapper",
                "ci_triage",
                "generate_docs",
            }:
                self.assertIn(expected, loaded)
                self.assertIn(expected, skills_module.SKILLS_REGISTRY)
            schema_names = [item["function"]["name"] for item in skills_module.skill_tool_schemas()]
            self.assertIn("skill_code_review", schema_names)


class UiStyleTests(unittest.TestCase):
    def test_style_switching(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_home = Path(tmp)
            try:
                ui_module = import_with_temp_home("odev.ui", temp_home)
            except ModuleNotFoundError as exc:
                if exc.name == "rich":
                    self.skipTest("rich is not installed in this environment")
                raise
            self.assertIn("codex", ui_module.available_styles())
            self.assertEqual(ui_module.set_style("claude"), "claude")
            self.assertEqual(ui_module.get_style(), "claude")
            self.assertEqual(ui_module.set_style("gemini"), "gemini")


if __name__ == "__main__":
    unittest.main()
