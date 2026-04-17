"""Wrapper for Claude CLI — loads agent definitions, runs agents, parses NDJSON output.

All calls to the Claude Code CLI are centralised here. External modules must use
`run_agent`, `run_task_text`, or `run_task_json`; never call `claude` directly.
"""

import json
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from squad.constants import CAP_READ_FILES, CAP_WEB_FETCH, CAP_WEB_SEARCH

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).parent.parent / "agents"
_MODEL = "claude-opus-4-6"
_MODEL_LIGHT = "claude-sonnet-4-6"
_TIMEOUT = 900  # 15 minutes per agent
_TIMEOUT_SHORT = 120  # 2 minutes for lightweight classification tasks

# Only the three tools supported at Plan 1 scope
_CAPABILITY_TO_TOOL: dict[str, str] = {
    CAP_READ_FILES: "Read",
    CAP_WEB_SEARCH: "WebSearch",
    CAP_WEB_FETCH: "WebFetch",
}


class AgentError(RuntimeError):
    """Raised when a Claude CLI agent execution fails after retries."""


# ── agent definition ───────────────────────────────────────────────────────────


def load_agent_definition(agent_name: str) -> str:
    """Load the markdown definition for a given agent from agents/."""
    path = _AGENTS_DIR / f"{agent_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")
    return path.read_text(encoding="utf-8")


def parse_agent_capabilities(agent_definition: str) -> dict[str, bool]:
    """Parse the 'Outils autorisés' section and return {capability: enabled}."""
    section = re.search(r"## Outils autorisés\n(.*?)(?:\n##|$)", agent_definition, re.DOTALL)
    if not section:
        return {}
    result: dict[str, bool] = {}
    for match in re.finditer(r"-\s+(\w+):\s+(oui|non)", section.group(1)):
        result[match.group(1)] = match.group(2) == "oui"
    return result


def map_allowed_tools(capabilities: dict[str, bool]) -> list[str]:
    """Map agent capabilities to Claude CLI --allowedTools identifiers (Plan 1 subset)."""
    return [tool for cap, tool in _CAPABILITY_TO_TOOL.items() if capabilities.get(cap, False)]


# ── prompt builder ─────────────────────────────────────────────────────────────


def build_agent_prompt(
    agent_name: str,
    session_id: str,
    phase: str,
    context_sections: list[str] | None = None,
    *,
    cumulative_context: str | None = None,
    phase_instruction: str | None = None,
) -> str:
    """Build the full prompt string for a single agent invocation.

    The executor stays a CLI wrapper: prompt shape is the only thing it
    owns. Callers in the orchestration layer can pass either a list of
    ``context_sections`` (joined with ``---``) or a single pre-built
    ``cumulative_context`` string (produced by ``squad.context_builder``).
    A ``phase_instruction`` — typically a retry directive with additional
    constraints — is rendered as a dedicated block when provided.
    """
    definition = load_agent_definition(agent_name)
    parts = [
        f"# Agent: {agent_name}\n\n{definition}",
        f"## Task\nSession: {session_id}\nPhase: {phase}",
    ]
    sections: list[str] = list(context_sections or [])
    if cumulative_context:
        sections.append(cumulative_context)
    if sections:
        parts.append("## Context\n\n" + "\n\n---\n\n".join(sections))
    if phase_instruction:
        parts.append(f"## Phase instruction\n\n{phase_instruction}")
    parts.append(
        "Produce your analysis and deliverable exactly as described in your definition above."
    )
    return "\n\n".join(parts)


# ── NDJSON parsing ─────────────────────────────────────────────────────────────


def _extract_text(ndjson_output: str) -> str:
    """Concatenate all 'type: text' payloads from a Claude CLI NDJSON stream."""
    texts: list[str] = []
    for line in ndjson_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "text":
                texts.append(obj.get("text", ""))
        except json.JSONDecodeError:
            continue
    return "".join(texts)


# ── subprocess interface ───────────────────────────────────────────────────────


def _call_claude_cli(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run the Claude CLI subprocess. Isolated here to allow mocking in tests."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,  # claude --print waits on stdin by default
    )


def _build_cmd(prompt: str, allowed_tools: list[str], model: str = _MODEL) -> list[str]:
    # Claude CLI takes the prompt as a positional arg: `claude [options] [prompt]`
    # `--output-format stream-json` also requires `--verbose`.
    cmd = [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if allowed_tools:
        # Use --allowedTools=<list> form (with =) to avoid the CLI parser
        # greedily consuming the following prompt token as a tool name.
        cmd.append(f"--allowedTools={','.join(allowed_tools)}")
    cmd.append(prompt)
    return cmd


def _extract_json(text: str) -> dict:
    """Extract a JSON object from text, handling optional markdown code fences."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Try ```json ... ``` or ``` ... ``` fences
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Last resort: first { ... } block
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No JSON object found in task output: {stripped[:200]!r}")


# ── agent execution ────────────────────────────────────────────────────────────


def run_agent(
    agent_name: str,
    session_id: str,
    phase: str,
    context_sections: list[str] | None = None,
    *,
    cumulative_context: str | None = None,
    phase_instruction: str | None = None,
) -> str:
    """Run a single agent via Claude CLI. Retries once on failure or empty output.

    ``cumulative_context`` and ``phase_instruction`` are forwarded to
    ``build_agent_prompt``. They exist so orchestration code (pipeline,
    recovery) can pass a pre-built context and a phase-specific directive
    without moving prompt-shape logic out of the executor.
    """
    definition = load_agent_definition(agent_name)
    capabilities = parse_agent_capabilities(definition)
    allowed_tools = map_allowed_tools(capabilities)
    prompt = build_agent_prompt(
        agent_name,
        session_id,
        phase,
        context_sections,
        cumulative_context=cumulative_context,
        phase_instruction=phase_instruction,
    )
    cmd = _build_cmd(prompt, allowed_tools)

    last_error: Exception | None = None
    for attempt in range(2):
        if attempt > 0:
            logger.warning("Retrying agent %r (attempt %d/2)", agent_name, attempt + 1)
        try:
            completed = _call_claude_cli(cmd, timeout=_TIMEOUT)
            if completed.returncode != 0:
                last_error = AgentError(
                    f"Agent {agent_name!r} exited with code {completed.returncode}: "
                    f"{completed.stderr[:200]}"
                )
                continue
            text = _extract_text(completed.stdout)
            if not text.strip():
                last_error = AgentError(f"Agent {agent_name!r} returned empty output")
                continue
            return text
        except subprocess.TimeoutExpired:
            last_error = AgentError(f"Agent {agent_name!r} timed out after {_TIMEOUT}s")

    raise last_error or AgentError(f"Agent {agent_name!r} failed after 2 attempts")


def run_agents_parallel(
    agents_list: list[str],
    session_id: str,
    phase: str,
    context_sections_by_agent: dict[str, list[str]] | None = None,
    *,
    cumulative_context: str | None = None,
    phase_instruction: str | None = None,
) -> dict[str, str]:
    """Run multiple agents concurrently. Raises AgentError if any agent fails.

    A shared ``cumulative_context`` or ``phase_instruction`` is forwarded
    to every agent. Per-agent ``context_sections_by_agent`` still applies
    when callers need agent-specific content on top of the shared context.
    """
    if not agents_list:
        return {}

    context_map = context_sections_by_agent or {}
    results: dict[str, str] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(agents_list)) as pool:
        futures = {
            pool.submit(
                run_agent,
                agent,
                session_id,
                phase,
                context_map.get(agent),
                cumulative_context=cumulative_context,
                phase_instruction=phase_instruction,
            ): agent
            for agent in agents_list
        }
        for future in as_completed(futures):
            agent = futures[future]
            try:
                results[agent] = future.result()
            except Exception as exc:
                logger.error("Agent %r failed: %s", agent, exc)
                errors[agent] = str(exc)

    if errors:
        failed = ", ".join(errors)
        raise AgentError(f"The following agents failed: {failed}")

    return results


def run_agents_tolerant(
    agents_list: list[str],
    session_id: str,
    phase: str,
    context_sections_by_agent: dict[str, list[str]] | None = None,
    *,
    cumulative_context: str | None = None,
    phase_instruction: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Run multiple agents concurrently and always return ``(results, errors)``.

    Unlike ``run_agents_parallel``, this helper never raises on a partial
    failure. The orchestration layer is then free to apply
    ``phase_config.is_critical_agent`` and decide whether to continue
    (non-critical failure) or to mark the session as failed (critical
    failure).
    """
    if not agents_list:
        return {}, {}

    context_map = context_sections_by_agent or {}
    results: dict[str, str] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(agents_list)) as pool:
        futures = {
            pool.submit(
                run_agent,
                agent,
                session_id,
                phase,
                context_map.get(agent),
                cumulative_context=cumulative_context,
                phase_instruction=phase_instruction,
            ): agent
            for agent in agents_list
        }
        for future in as_completed(futures):
            agent = futures[future]
            try:
                results[agent] = future.result()
            except Exception as exc:
                logger.error("Agent %r failed: %s", agent, exc)
                errors[agent] = str(exc)

    return results, errors


# ── generic task helpers ───────────────────────────────────────────────────────


def run_task_text(
    prompt: str,
    model: str = _MODEL,
    timeout: int = _TIMEOUT,
    allowed_tools: list[str] | None = None,
) -> str:
    """Run a generic text task via Claude CLI.

    Unlike run_agent, this helper is not tied to an agent markdown definition.
    Use it for classification, summarisation, or any one-off Claude invocation.

    Args:
        prompt: Full prompt to send.
        model: Claude model identifier (defaults to Opus).
        timeout: Subprocess timeout in seconds.
        allowed_tools: Claude CLI tool identifiers to allow (e.g. ["WebSearch"]).

    Returns:
        The concatenated text output from the NDJSON stream.

    Raises:
        AgentError: On non-zero exit, empty output, or timeout.
    """
    cmd = _build_cmd(prompt, allowed_tools or [], model=model)
    try:
        completed = _call_claude_cli(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"Task timed out after {timeout}s") from exc
    if completed.returncode != 0:
        raise AgentError(f"Task failed with code {completed.returncode}: {completed.stderr[:200]}")
    text = _extract_text(completed.stdout)
    if not text.strip():
        raise AgentError("Task returned empty output")
    return text


def run_task_json(
    prompt: str,
    model: str = _MODEL,
    timeout: int = _TIMEOUT,
    allowed_tools: list[str] | None = None,
) -> dict:
    """Run a structured task via Claude CLI and parse the JSON response.

    The model is expected to return a JSON object, optionally wrapped in a
    markdown code fence. Uses the same Claude CLI path as all other executors.

    Args:
        prompt: Full prompt instructing the model to return JSON.
        model: Claude model identifier (defaults to Opus).
        timeout: Subprocess timeout in seconds.
        allowed_tools: Claude CLI tool identifiers to allow.

    Returns:
        Parsed dict from the model's JSON response.

    Raises:
        AgentError: On execution failure.
        ValueError: If the output cannot be parsed as JSON.
    """
    text = run_task_text(prompt, model=model, timeout=timeout, allowed_tools=allowed_tools)
    return _extract_json(text)
