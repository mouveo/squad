"""Wrapper for Claude CLI — loads agent definitions, runs agents, parses NDJSON output."""

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
_TIMEOUT = 900  # 15 minutes per agent

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
    section = re.search(
        r"## Outils autorisés\n(.*?)(?:\n##|$)", agent_definition, re.DOTALL
    )
    if not section:
        return {}
    result: dict[str, bool] = {}
    for match in re.finditer(r"-\s+(\w+):\s+(oui|non)", section.group(1)):
        result[match.group(1)] = match.group(2) == "oui"
    return result


def map_allowed_tools(capabilities: dict[str, bool]) -> list[str]:
    """Map agent capabilities to Claude CLI --allowedTools identifiers (Plan 1 subset)."""
    return [
        tool
        for cap, tool in _CAPABILITY_TO_TOOL.items()
        if capabilities.get(cap, False)
    ]


# ── prompt builder ─────────────────────────────────────────────────────────────


def build_agent_prompt(
    agent_name: str,
    session_id: str,
    phase: str,
    context_sections: list[str] | None = None,
) -> str:
    """Build the full prompt string for a single agent invocation."""
    definition = load_agent_definition(agent_name)
    parts = [
        f"# Agent: {agent_name}\n\n{definition}",
        f"## Task\nSession: {session_id}\nPhase: {phase}",
    ]
    if context_sections:
        parts.append("## Context\n\n" + "\n\n---\n\n".join(context_sections))
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


def _call_claude_cli(
    cmd: list[str], timeout: int
) -> subprocess.CompletedProcess:
    """Run the Claude CLI subprocess. Isolated here to allow mocking in tests."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _build_cmd(prompt: str, allowed_tools: list[str]) -> list[str]:
    cmd = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--model", _MODEL,
        "--prompt", prompt,
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    return cmd


# ── agent execution ────────────────────────────────────────────────────────────


def run_agent(
    agent_name: str,
    session_id: str,
    phase: str,
    context_sections: list[str] | None = None,
) -> str:
    """Run a single agent via Claude CLI. Retries once on failure or empty output."""
    definition = load_agent_definition(agent_name)
    capabilities = parse_agent_capabilities(definition)
    allowed_tools = map_allowed_tools(capabilities)
    prompt = build_agent_prompt(agent_name, session_id, phase, context_sections)
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
            last_error = AgentError(
                f"Agent {agent_name!r} timed out after {_TIMEOUT}s"
            )

    raise last_error or AgentError(f"Agent {agent_name!r} failed after 2 attempts")


def run_agents_parallel(
    agents_list: list[str],
    session_id: str,
    phase: str,
    context_sections_by_agent: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Run multiple agents concurrently. Raises AgentError if any agent fails."""
    if not agents_list:
        return {}

    context_map = context_sections_by_agent or {}
    results: dict[str, str] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(agents_list)) as pool:
        futures = {
            pool.submit(run_agent, agent, session_id, phase, context_map.get(agent)): agent
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
