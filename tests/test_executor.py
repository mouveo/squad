"""Tests for squad/executor.py — NDJSON parsing, tool mapping, run_agent, parallel execution."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from squad.executor import (
    AgentError,
    _extract_json,
    _extract_text,
    build_agent_prompt,
    load_agent_definition,
    map_allowed_tools,
    parse_agent_capabilities,
    run_agent,
    run_agents_parallel,
    run_task_json,
    run_task_text,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _ndjson(*texts: str) -> str:
    """Build a fake NDJSON stream with type=text lines."""
    return "\n".join(json.dumps({"type": "text", "text": t}) for t in texts)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    mock = MagicMock(spec=subprocess.CompletedProcess)
    mock.stdout = stdout
    mock.returncode = returncode
    mock.stderr = stderr
    return mock


# ── load_agent_definition ──────────────────────────────────────────────────────


class TestLoadAgentDefinition:
    def test_loads_real_agent(self):
        content = load_agent_definition("pm")
        assert "# Agent: PM" in content

    def test_raises_for_unknown_agent(self):
        with pytest.raises(FileNotFoundError):
            load_agent_definition("nonexistent_agent_xyz")


# ── parse_agent_capabilities ───────────────────────────────────────────────────


class TestParseAgentCapabilities:
    def test_parses_pm_capabilities(self):
        content = load_agent_definition("pm")
        caps = parse_agent_capabilities(content)
        assert caps["read_files"] is True
        assert caps["web_search"] is False
        assert caps["web_fetch"] is False
        assert caps["execute_commands"] is False

    def test_parses_ux_capabilities(self):
        content = load_agent_definition("ux")
        caps = parse_agent_capabilities(content)
        assert caps["web_search"] is True
        assert caps["web_fetch"] is True
        assert caps["read_files"] is True

    def test_returns_empty_dict_for_missing_section(self):
        caps = parse_agent_capabilities("# Agent: Test\n## Mission\nNo tools section.")
        assert caps == {}

    def test_all_five_capabilities_present(self):
        content = load_agent_definition("pm")
        caps = parse_agent_capabilities(content)
        expected_keys = {"web_search", "web_fetch", "read_files", "write_files", "execute_commands"}
        assert set(caps.keys()) == expected_keys


# ── map_allowed_tools ──────────────────────────────────────────────────────────


class TestMapAllowedTools:
    def test_read_files_maps_to_read(self):
        tools = map_allowed_tools({"read_files": True})
        assert "Read" in tools

    def test_web_search_maps_to_websearch(self):
        tools = map_allowed_tools({"web_search": True})
        assert "WebSearch" in tools

    def test_web_fetch_maps_to_webfetch(self):
        tools = map_allowed_tools({"web_fetch": True})
        assert "WebFetch" in tools

    def test_write_files_not_mapped(self):
        tools = map_allowed_tools({"write_files": True})
        assert tools == []

    def test_execute_commands_not_mapped(self):
        tools = map_allowed_tools({"execute_commands": True})
        assert tools == []

    def test_pm_gets_only_read(self):
        content = load_agent_definition("pm")
        caps = parse_agent_capabilities(content)
        tools = map_allowed_tools(caps)
        assert tools == ["Read"]

    def test_ux_gets_read_and_web(self):
        content = load_agent_definition("ux")
        caps = parse_agent_capabilities(content)
        tools = map_allowed_tools(caps)
        assert set(tools) == {"Read", "WebSearch", "WebFetch"}

    def test_no_enabled_caps_returns_empty(self):
        tools = map_allowed_tools({"web_search": False, "read_files": False})
        assert tools == []

    def test_glob_maps_to_glob(self):
        assert map_allowed_tools({"glob": True}) == ["Glob"]

    def test_list_files_maps_to_ls(self):
        assert map_allowed_tools({"list_files": True}) == ["LS"]

    def test_grep_files_maps_to_grep(self):
        assert map_allowed_tools({"grep_files": True}) == ["Grep"]

    def test_all_caps_preserve_deterministic_order(self):
        tools = map_allowed_tools(
            {
                "read_files": True,
                "web_search": True,
                "web_fetch": True,
                "glob": True,
                "list_files": True,
                "grep_files": True,
            }
        )
        assert tools == ["Read", "WebSearch", "WebFetch", "Glob", "LS", "Grep"]


# ── _extract_text ──────────────────────────────────────────────────────────────


class TestExtractText:
    def test_extracts_single_text_line(self):
        ndjson = json.dumps({"type": "text", "text": "Hello world"})
        assert _extract_text(ndjson) == "Hello world"

    def test_concatenates_multiple_text_lines(self):
        ndjson = _ndjson("Hello ", "world")
        assert _extract_text(ndjson) == "Hello world"

    def test_ignores_non_text_types(self):
        lines = [
            json.dumps({"type": "tool_use", "name": "Read"}),
            json.dumps({"type": "text", "text": "result"}),
            json.dumps({"type": "end"}),
        ]
        assert _extract_text("\n".join(lines)) == "result"

    def test_ignores_invalid_json_lines(self):
        ndjson = "not json\n" + json.dumps({"type": "text", "text": "ok"})
        assert _extract_text(ndjson) == "ok"

    def test_returns_empty_string_for_empty_input(self):
        assert _extract_text("") == ""


# ── build_agent_prompt ─────────────────────────────────────────────────────────


class TestBuildAgentPrompt:
    def test_contains_agent_definition(self):
        prompt = build_agent_prompt("pm", "sess-1", "cadrage")
        assert "Agent: PM" in prompt

    def test_contains_session_and_phase(self):
        prompt = build_agent_prompt("pm", "sess-abc", "cadrage")
        assert "sess-abc" in prompt
        assert "cadrage" in prompt

    def test_contains_context_sections(self):
        prompt = build_agent_prompt("pm", "sess-1", "cadrage", ["context A", "context B"])
        assert "context A" in prompt
        assert "context B" in prompt

    def test_no_context_sections_by_default(self):
        prompt = build_agent_prompt("pm", "sess-1", "cadrage")
        assert "## Context" not in prompt


# ── run_agent ──────────────────────────────────────────────────────────────────


class TestRunAgent:
    @patch("squad.executor._call_claude_cli")
    def test_returns_text_on_success(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("Agent output text"))
        result = run_agent("pm", "sess-1", "cadrage")
        assert result == "Agent output text"

    @patch("squad.executor._call_claude_cli")
    def test_retry_on_nonzero_exit_then_success(self, mock_cli):
        mock_cli.side_effect = [
            _completed(returncode=1, stderr="error"),
            _completed(stdout=_ndjson("Success on retry")),
        ]
        result = run_agent("pm", "sess-1", "cadrage")
        assert result == "Success on retry"
        assert mock_cli.call_count == 2

    @patch("squad.executor._call_claude_cli")
    def test_retry_on_empty_output_then_success(self, mock_cli):
        mock_cli.side_effect = [
            _completed(stdout=""),
            _completed(stdout=_ndjson("Non-empty result")),
        ]
        result = run_agent("pm", "sess-1", "cadrage")
        assert result == "Non-empty result"
        assert mock_cli.call_count == 2

    @patch("squad.executor._call_claude_cli")
    def test_raises_after_two_nonzero_exits(self, mock_cli):
        mock_cli.return_value = _completed(returncode=1, stderr="fail")
        with pytest.raises(AgentError, match="exited with code"):
            run_agent("pm", "sess-1", "cadrage")
        assert mock_cli.call_count == 2

    @patch("squad.executor._call_claude_cli")
    def test_raises_after_two_empty_outputs(self, mock_cli):
        mock_cli.return_value = _completed(stdout="")
        with pytest.raises(AgentError, match="empty output"):
            run_agent("pm", "sess-1", "cadrage")
        assert mock_cli.call_count == 2

    @patch("squad.executor._call_claude_cli")
    def test_retry_on_timeout_then_success(self, mock_cli):
        mock_cli.side_effect = [
            subprocess.TimeoutExpired(cmd=[], timeout=900),
            _completed(stdout=_ndjson("Late but ok")),
        ]
        result = run_agent("pm", "sess-1", "cadrage")
        assert result == "Late but ok"

    @patch("squad.executor._call_claude_cli")
    def test_raises_after_two_timeouts(self, mock_cli):
        mock_cli.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=900)
        with pytest.raises(AgentError, match="timed out"):
            run_agent("pm", "sess-1", "cadrage")
        assert mock_cli.call_count == 2

    @patch("squad.executor._call_claude_cli")
    def test_no_allowed_tools_flag_when_capabilities_empty(self, mock_cli):
        """--allowedTools is omitted when map_allowed_tools returns []."""
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        with patch("squad.executor.map_allowed_tools", return_value=[]):
            run_agent("pm", "sess-1", "cadrage")
        cmd_used = mock_cli.call_args[0][0]
        assert "--allowedTools" not in cmd_used

    @patch("squad.executor._call_claude_cli")
    def test_allowed_tools_passed_for_ux(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ux output"))
        run_agent("ux", "sess-1", "conception")
        cmd_used = mock_cli.call_args[0][0]
        tools_flag = next(
            (a for a in cmd_used if a.startswith("--allowedTools=")), None
        )
        assert tools_flag is not None
        tools = tools_flag.split("=", 1)[1]
        assert "WebSearch" in tools
        assert "WebFetch" in tools
        assert "Read" in tools


# ── run_agents_parallel ────────────────────────────────────────────────────────


class TestRunAgentsParallel:
    @patch("squad.executor.run_agent")
    def test_runs_all_agents(self, mock_run):
        mock_run.side_effect = lambda agent, *a, **kw: f"output of {agent}"
        results = run_agents_parallel(["pm", "ux"], "sess-1", "cadrage")
        assert results == {"pm": "output of pm", "ux": "output of ux"}

    @patch("squad.executor.run_agent")
    def test_returns_empty_dict_for_empty_list(self, mock_run):
        results = run_agents_parallel([], "sess-1", "cadrage")
        assert results == {}
        mock_run.assert_not_called()

    @patch("squad.executor.run_agent")
    def test_raises_if_any_agent_fails(self, mock_run):
        def side_effect(agent, *a, **kw):
            if agent == "ux":
                raise AgentError("ux failed")
            return f"output of {agent}"

        mock_run.side_effect = side_effect
        with pytest.raises(AgentError, match="ux"):
            run_agents_parallel(["pm", "ux"], "sess-1", "cadrage")

    @patch("squad.executor.run_agent")
    def test_passes_context_sections_per_agent(self, mock_run):
        mock_run.return_value = "output"
        context_map = {"pm": ["section A"], "ux": ["section B"]}
        run_agents_parallel(["pm", "ux"], "sess-1", "cadrage", context_map)

        calls = {c.args[0]: c.args[3] for c in mock_run.call_args_list}
        assert calls["pm"] == ["section A"]
        assert calls["ux"] == ["section B"]

    @patch("squad.executor.run_agent")
    def test_passes_none_context_when_not_specified(self, mock_run):
        mock_run.return_value = "output"
        run_agents_parallel(["pm"], "sess-1", "cadrage")
        assert mock_run.call_args.args[3] is None


# ── _extract_json ──────────────────────────────────────────────────────────────


class TestExtractJson:
    def test_parses_bare_json(self):
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parses_json_in_code_fence(self):
        text = '```json\n{"subject_type": "saas"}\n```'
        assert _extract_json(text) == {"subject_type": "saas"}

    def test_parses_json_in_generic_fence(self):
        text = '```\n{"a": 1}\n```'
        assert _extract_json(text) == {"a": 1}

    def test_extracts_json_from_prose(self):
        text = 'Here is the result:\n{"score": 42}\nThat is all.'
        assert _extract_json(text) == {"score": 42}

    def test_raises_value_error_for_non_json(self):
        with pytest.raises(ValueError, match="No JSON object found"):
            _extract_json("This is just plain text with no JSON.")

    def test_parses_nested_json(self):
        data = {"outer": {"inner": [1, 2, 3]}}
        assert _extract_json(json.dumps(data)) == data


# ── run_task_text ──────────────────────────────────────────────────────────────


class TestRunTaskText:
    @patch("squad.executor._call_claude_cli")
    def test_returns_text_on_success(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("Hello from task"))
        result = run_task_text("Do something")
        assert result == "Hello from task"

    @patch("squad.executor._call_claude_cli")
    def test_raises_on_nonzero_exit(self, mock_cli):
        mock_cli.return_value = _completed(returncode=1, stderr="boom")
        with pytest.raises(AgentError, match="Task failed with code"):
            run_task_text("Do something")

    @patch("squad.executor._call_claude_cli")
    def test_raises_on_empty_output(self, mock_cli):
        mock_cli.return_value = _completed(stdout="")
        with pytest.raises(AgentError, match="empty output"):
            run_task_text("Do something")

    @patch("squad.executor._call_claude_cli")
    def test_raises_on_timeout(self, mock_cli):
        mock_cli.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=120)
        with pytest.raises(AgentError, match="timed out"):
            run_task_text("Do something", timeout=120)

    @patch("squad.executor._call_claude_cli")
    def test_passes_model_to_cmd(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_task_text("prompt", model="claude-sonnet-4-6")
        cmd = mock_cli.call_args[0][0]
        assert "claude-sonnet-4-6" in cmd

    @patch("squad.executor._call_claude_cli")
    def test_passes_allowed_tools_to_cmd(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_task_text("prompt", allowed_tools=["WebSearch"])
        cmd = mock_cli.call_args[0][0]
        tools_flag = next((a for a in cmd if a.startswith("--allowedTools=")), None)
        assert tools_flag is not None
        assert "WebSearch" in tools_flag.split("=", 1)[1]

    @patch("squad.executor._call_claude_cli")
    def test_no_allowed_tools_flag_when_none(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_task_text("prompt", allowed_tools=None)
        cmd = mock_cli.call_args[0][0]
        assert "--allowedTools" not in cmd


# ── run_task_json ──────────────────────────────────────────────────────────────


class TestRunTaskJson:
    @patch("squad.executor._call_claude_cli")
    def test_returns_dict_on_success(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson('{"type": "saas"}'))
        result = run_task_json("Classify this")
        assert result == {"type": "saas"}

    @patch("squad.executor._call_claude_cli")
    def test_returns_dict_from_fenced_output(self, mock_cli):
        fenced = '```json\n{"result": true}\n```'
        mock_cli.return_value = _completed(stdout=_ndjson(fenced))
        assert run_task_json("Classify") == {"result": True}

    @patch("squad.executor._call_claude_cli")
    def test_raises_value_error_for_non_json_output(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("Not JSON at all."))
        with pytest.raises(ValueError, match="No JSON object found"):
            run_task_json("Classify")

    @patch("squad.executor._call_claude_cli")
    def test_raises_agent_error_on_cli_failure(self, mock_cli):
        mock_cli.return_value = _completed(returncode=1, stderr="error")
        with pytest.raises(AgentError):
            run_task_json("Classify")


# ── prompt boundary: cumulative_context + phase_instruction (LOT 4) ────────────


class TestBuildAgentPromptPromptBoundary:
    def test_cumulative_context_rendered_as_section(self):
        prompt = build_agent_prompt(
            "pm",
            "sess-1",
            "cadrage",
            cumulative_context="## Idée\n\nBuild a CRM",
        )
        assert "## Context" in prompt
        assert "Build a CRM" in prompt

    def test_phase_instruction_rendered_as_block(self):
        prompt = build_agent_prompt(
            "pm",
            "sess-1",
            "cadrage",
            phase_instruction="Retry with tighter scope",
        )
        assert "## Phase instruction" in prompt
        assert "Retry with tighter scope" in prompt

    def test_cumulative_context_joins_with_sections(self):
        prompt = build_agent_prompt(
            "pm",
            "sess-1",
            "cadrage",
            context_sections=["first"],
            cumulative_context="second",
        )
        assert "first" in prompt
        assert "second" in prompt
        # Sections are separated by "---"
        assert "---" in prompt.split("## Context")[1]

    def test_no_context_block_when_nothing_provided(self):
        prompt = build_agent_prompt("pm", "sess-1", "cadrage")
        assert "## Context" not in prompt


class TestRunAgentForwardsPromptParams:
    def test_run_agent_forwards_cumulative_context(self):
        captured: dict = {}

        def _fake_cli(cmd, timeout, cwd=None):
            # The Claude CLI takes the prompt as the last positional arg.
            captured["prompt"] = cmd[-1]
            return _completed(_ndjson("ok output"), returncode=0)

        with patch("squad.executor._call_claude_cli", side_effect=_fake_cli):
            out = run_agent(
                "pm",
                "sess-1",
                "cadrage",
                cumulative_context="## Idée\n\nTest idea",
                phase_instruction="Be concise",
            )
        assert out == "ok output"
        assert "Test idea" in captured["prompt"]
        assert "## Phase instruction" in captured["prompt"]
        assert "Be concise" in captured["prompt"]

    def test_run_agents_parallel_forwards_shared_context(self):
        captured: list[str] = []

        def _fake_cli(cmd, timeout, cwd=None):
            captured.append(cmd[-1])
            return _completed(_ndjson("agent ok"), returncode=0)

        with patch("squad.executor._call_claude_cli", side_effect=_fake_cli):
            results = run_agents_parallel(
                ["pm"],
                "sess-1",
                "cadrage",
                cumulative_context="Shared idea text",
                phase_instruction="Apply constraints X",
            )
        assert results == {"pm": "agent ok"}
        assert all("Shared idea text" in p for p in captured)
        assert all("Apply constraints X" in p for p in captured)


# ── run_agents_tolerant (LOT 5) ────────────────────────────────────────────────


from squad.executor import run_agents_tolerant  # noqa: E402


class TestRunAgentsTolerant:
    def test_returns_results_and_errors_tuple(self):
        def _fake(cmd, timeout, cwd=None):
            return _completed(_ndjson("ok"), returncode=0)

        with patch("squad.executor._call_claude_cli", side_effect=_fake):
            results, errors = run_agents_tolerant(["pm"], "s", "cadrage")
        assert results == {"pm": "ok"}
        assert errors == {}

    def test_partial_failure_returns_both(self):
        def _fake(cmd, timeout, cwd=None):
            # Detect the agent from the dedicated "# Agent: <name>" header
            # (the prompt is the last positional arg of the Claude CLI).
            prompt = cmd[-1]
            if prompt.startswith("# Agent: ux"):
                return _completed("", returncode=1, stderr="ux crash")
            return _completed(_ndjson("ok"), returncode=0)

        with patch("squad.executor._call_claude_cli", side_effect=_fake):
            results, errors = run_agents_tolerant(["pm", "ux"], "s", "cadrage")
        assert results == {"pm": "ok"}
        assert "ux" in errors

    def test_empty_agents_returns_empty_tuple(self):
        assert run_agents_tolerant([], "s", "cadrage") == ({}, {})

    def test_forwards_phase_instruction(self):
        captured: list[str] = []

        def _fake(cmd, timeout, cwd=None):
            captured.append(cmd[-1])
            return _completed(_ndjson("ok"), returncode=0)

        with patch("squad.executor._call_claude_cli", side_effect=_fake):
            run_agents_tolerant(
                ["pm"],
                "s",
                "cadrage",
                cumulative_context="ctx",
                phase_instruction="Retry with X",
            )
        assert any("Retry with X" in p for p in captured)

    def test_never_raises_on_agent_failure(self):
        def _fake(cmd, timeout, cwd=None):
            return _completed("", returncode=1, stderr="down")

        with patch("squad.executor._call_claude_cli", side_effect=_fake):
            results, errors = run_agents_tolerant(["pm", "ux"], "s", "cadrage")
        assert results == {}
        assert set(errors) == {"pm", "ux"}


# ── cwd forwarding (LOT 1 — Plan 5) ────────────────────────────────────────────


class TestCwdForwarding:
    @patch("squad.executor._call_claude_cli")
    def test_run_agent_forwards_cwd_to_cli(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_agent("pm", "sess-1", "cadrage", cwd="/tmp/foo")
        assert mock_cli.call_args.kwargs["cwd"] == "/tmp/foo"

    @patch("squad.executor._call_claude_cli")
    def test_run_agent_defaults_cwd_none(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_agent("pm", "sess-1", "cadrage")
        assert mock_cli.call_args.kwargs["cwd"] is None

    @patch("squad.executor._call_claude_cli")
    def test_run_task_text_forwards_cwd_and_tools(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson("ok"))
        run_task_text("prompt", cwd="/tmp/foo", allowed_tools=["Glob"])
        assert mock_cli.call_args.kwargs["cwd"] == "/tmp/foo"
        cmd = mock_cli.call_args[0][0]
        tools_flag = next((a for a in cmd if a.startswith("--allowedTools=")), None)
        assert tools_flag is not None
        assert tools_flag.split("=", 1)[1] == "Glob"

    @patch("squad.executor._call_claude_cli")
    def test_run_task_json_forwards_cwd_and_tools(self, mock_cli):
        mock_cli.return_value = _completed(stdout=_ndjson('{"ok": true}'))
        result = run_task_json("prompt", cwd="/tmp/foo", allowed_tools=["Glob"])
        assert result == {"ok": True}
        assert mock_cli.call_args.kwargs["cwd"] == "/tmp/foo"
        cmd = mock_cli.call_args[0][0]
        tools_flag = next((a for a in cmd if a.startswith("--allowedTools=")), None)
        assert tools_flag is not None
        assert tools_flag.split("=", 1)[1] == "Glob"

    def test_call_claude_cli_default_cwd_signature(self):
        """_call_claude_cli accepts cwd keyword; default is None (no behaviour change)."""
        import inspect

        from squad.executor import _call_claude_cli

        sig = inspect.signature(_call_claude_cli)
        assert "cwd" in sig.parameters
        assert sig.parameters["cwd"].default is None


class TestCwdByAgentRouting:
    @patch("squad.executor.run_agent")
    def test_run_agents_parallel_routes_cwd_per_agent(self, mock_run):
        mock_run.side_effect = lambda agent, *a, **kw: f"out-{agent}"
        run_agents_parallel(
            ["pm", "ux"],
            "sess-1",
            "cadrage",
            cwd_by_agent={"ux": "/tmp/foo"},
        )
        cwd_by_agent = {
            call.args[0]: call.kwargs.get("cwd") for call in mock_run.call_args_list
        }
        assert cwd_by_agent["ux"] == "/tmp/foo"
        assert cwd_by_agent["pm"] is None

    @patch("squad.executor.run_agent")
    def test_run_agents_parallel_no_cwd_map_defaults_to_none(self, mock_run):
        mock_run.return_value = "out"
        run_agents_parallel(["pm"], "sess-1", "cadrage")
        assert mock_run.call_args.kwargs.get("cwd") is None

    @patch("squad.executor.run_agent")
    def test_run_agents_tolerant_routes_cwd_per_agent(self, mock_run):
        mock_run.side_effect = lambda agent, *a, **kw: f"out-{agent}"
        run_agents_tolerant(
            ["pm", "ux"],
            "sess-1",
            "cadrage",
            cwd_by_agent={"ux": "/tmp/foo"},
        )
        cwd_by_agent = {
            call.args[0]: call.kwargs.get("cwd") for call in mock_run.call_args_list
        }
        assert cwd_by_agent["ux"] == "/tmp/foo"
        assert cwd_by_agent["pm"] is None

    @patch("squad.executor.run_agent")
    def test_run_agents_tolerant_no_cwd_map_defaults_to_none(self, mock_run):
        mock_run.return_value = "out"
        run_agents_tolerant(["pm"], "sess-1", "cadrage")
        assert mock_run.call_args.kwargs.get("cwd") is None
