"""Structured JSON contracts produced by flow-driving agents.

The human-readable deliverable for each agent remains free-form markdown.
When a phase drives the flow (pauses on questions, retries on blockers,
hands a synthesis to the plan generator), the agent must also embed a
small JSON block that downstream code can parse without regex on prose.

Examples:

    ```json
    {"questions": [{"id": "q1", "question": "..."}], "needs_pause": true}
    ```

    ```json
    {"blockers": [{"id": "b1", "severity": "blocking", "constraint": "..."}]}
    ```

    ```json
    {"decision_summary": "...", "open_questions": [], "plan_inputs": ["..."]}
    ```
"""

import json
import re
from dataclasses import dataclass

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Allowed severity labels for challenge blockers
BLOCKER_SEVERITIES: tuple[str, ...] = ("blocking", "major", "minor", "info")


class ContractError(ValueError):
    """Raised when a required structured JSON block is missing or malformed."""


# ── dataclasses ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Question:
    id: str
    question: str


@dataclass(frozen=True)
class QuestionsContract:
    questions: tuple[Question, ...]
    needs_pause: bool


@dataclass(frozen=True)
class Blocker:
    id: str
    severity: str
    constraint: str


@dataclass(frozen=True)
class BlockersContract:
    blockers: tuple[Blocker, ...]

    @property
    def has_blocking(self) -> bool:
        return any(b.severity == "blocking" for b in self.blockers)


@dataclass(frozen=True)
class SynthesisContract:
    decision_summary: str
    open_questions: tuple[str, ...]
    plan_inputs: tuple[str, ...]


# ── JSON extraction ────────────────────────────────────────────────────────────


def extract_json_block(text: str) -> dict:
    """Return the first JSON object found in the given text.

    Scans for fenced ```json { ... } ``` blocks first, then falls back to
    the first balanced {...} object found anywhere in the text. Only dict
    payloads are accepted — top-level arrays raise ``ContractError``.
    """
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    # Fallback: scan for balanced {...} blocks anywhere in the text
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)

    raise ContractError("No JSON object found in text")


# ── contract parsers ───────────────────────────────────────────────────────────


def parse_questions_contract(text: str) -> QuestionsContract:
    """Parse a questions contract from an agent output."""
    data = extract_json_block(text)
    if "questions" not in data or not isinstance(data["questions"], list):
        raise ContractError("Missing or invalid 'questions' list in contract")
    questions: list[Question] = []
    for raw in data["questions"]:
        if not isinstance(raw, dict) or "id" not in raw or "question" not in raw:
            raise ContractError(f"Malformed question entry: {raw!r}")
        questions.append(Question(id=str(raw["id"]), question=str(raw["question"])))
    needs_pause = bool(data.get("needs_pause", len(questions) > 0))
    return QuestionsContract(questions=tuple(questions), needs_pause=needs_pause)


def parse_blockers_contract(text: str) -> BlockersContract:
    """Parse a blockers contract from a challenge-agent output."""
    data = extract_json_block(text)
    if "blockers" not in data or not isinstance(data["blockers"], list):
        raise ContractError("Missing or invalid 'blockers' list in contract")
    blockers: list[Blocker] = []
    for raw in data["blockers"]:
        if not isinstance(raw, dict):
            raise ContractError(f"Malformed blocker entry: {raw!r}")
        for key in ("id", "severity", "constraint"):
            if key not in raw:
                raise ContractError(f"Blocker missing {key!r}: {raw!r}")
        severity = str(raw["severity"])
        if severity not in BLOCKER_SEVERITIES:
            raise ContractError(f"Unknown blocker severity: {severity!r}")
        blockers.append(
            Blocker(
                id=str(raw["id"]),
                severity=severity,
                constraint=str(raw["constraint"]),
            )
        )
    return BlockersContract(blockers=tuple(blockers))


def parse_synthesis_contract(text: str) -> SynthesisContract:
    """Parse a synthesis contract from the PM output of the synthese phase."""
    data = extract_json_block(text)
    for key in ("decision_summary", "open_questions", "plan_inputs"):
        if key not in data:
            raise ContractError(f"Missing {key!r} in synthesis contract")
    if not isinstance(data["open_questions"], list) or not isinstance(data["plan_inputs"], list):
        raise ContractError("'open_questions' and 'plan_inputs' must be lists")
    return SynthesisContract(
        decision_summary=str(data["decision_summary"]),
        open_questions=tuple(str(q) for q in data["open_questions"]),
        plan_inputs=tuple(str(p) for p in data["plan_inputs"]),
    )
