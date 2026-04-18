"""Tests for agent markdown files — structure, completeness and constraints."""

import re
from pathlib import Path

import pytest

from squad.constants import AGENT_CAPABILITIES

AGENTS_DIR = Path(__file__).parent.parent / "agents"

AGENT_FILES = [
    "pm.md",
    "ux.md",
    "architect.md",
    "security.md",
    "growth.md",
    "data.md",
    "customer-success.md",
    "delivery.md",
    "sales.md",
    "ai-lead.md",
    "ideation.md",
]

REQUIRED_SECTIONS = [
    "## Identité",
    "## Mission",
    "## Réflexes",
    "## Questions clés",
    "## Livrable attendu",
    "## Erreurs à éviter",
    "## Outils autorisés",
]

CAPABILITIES = list(AGENT_CAPABILITIES)

# Patterns that would indicate a placeholder was left unreplaced
PLACEHOLDER_PATTERN = re.compile(r"\{[^}]+\}")


def _read(filename: str) -> str:
    return (AGENTS_DIR / filename).read_text(encoding="utf-8")


# ── presence ───────────────────────────────────────────────────────────────────


class TestAgentFilesExist:
    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_file_exists(self, filename: str):
        assert (AGENTS_DIR / filename).exists(), f"Missing agent file: {filename}"

    def test_exactly_eleven_agent_files(self):
        md_files = [f.name for f in AGENTS_DIR.glob("*.md")]
        assert set(AGENT_FILES) == set(md_files), (
            f"Expected exactly 11 agent .md files, found: {md_files}"
        )


# ── required sections ──────────────────────────────────────────────────────────


class TestRequiredSections:
    @pytest.mark.parametrize("filename", AGENT_FILES)
    @pytest.mark.parametrize("section", REQUIRED_SECTIONS)
    def test_section_present(self, filename: str, section: str):
        content = _read(filename)
        assert section in content, f"{filename} is missing section: {section!r}"


# ── capabilities ───────────────────────────────────────────────────────────────


class TestCapabilities:
    @pytest.mark.parametrize("filename", AGENT_FILES)
    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_capability_declared(self, filename: str, capability: str):
        content = _read(filename)
        assert f"- {capability}: " in content, (
            f"{filename} is missing capability declaration: {capability}"
        )

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_capability_values_are_oui_or_non(self, filename: str):
        content = _read(filename)
        for cap in CAPABILITIES:
            match = re.search(rf"- {cap}: (\w+)", content)
            assert match, f"{filename}: capability {cap} has no value"
            assert match.group(1) in {"oui", "non"}, (
                f"{filename}: {cap} value must be 'oui' or 'non', got {match.group(1)!r}"
            )


# ── pm-specific constraints ────────────────────────────────────────────────────


class TestPMConstraints:
    def test_pm_can_ask_questions(self):
        content = _read("pm.md")
        assert "Peut poser des questions à l'utilisateur : oui" in content

    def test_pm_has_mandatory_reflexe(self):
        content = _read("pm.md")
        expected = (
            "Je suis le seul interface avec l'utilisateur. "
            "Les autres agents travaillent avec mes inputs et leurs hypothèses."
        )
        assert expected in content, "pm.md is missing the mandatory reflexe"

    @pytest.mark.parametrize(
        "filename",
        [f for f in AGENT_FILES if f != "pm.md"],
    )
    def test_other_agents_cannot_ask_questions(self, filename: str):
        content = _read(filename)
        assert "Peut poser des questions à l'utilisateur : non" in content, (
            f"{filename} must have 'Peut poser des questions à l'utilisateur : non'"
        )


# ── no placeholders ────────────────────────────────────────────────────────────


class TestNoPlaceholders:
    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_no_unreplaced_placeholders(self, filename: str):
        content = _read(filename)
        matches = PLACEHOLDER_PATTERN.findall(content)
        assert not matches, f"{filename} contains unreplaced placeholders: {matches}"


# ── content depth ──────────────────────────────────────────────────────────────


class TestContentDepth:
    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_mission_is_not_empty(self, filename: str):
        content = _read(filename)
        # Extract text between ## Mission and the next ## section
        match = re.search(r"## Mission\n(.*?)\n##", content, re.DOTALL)
        assert match, f"{filename}: ## Mission section not found or malformed"
        mission_text = match.group(1).strip()
        assert len(mission_text) >= 50, (
            f"{filename}: ## Mission is too short ({len(mission_text)} chars)"
        )

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_reflexes_has_at_least_four_bullets(self, filename: str):
        content = _read(filename)
        match = re.search(r"## Réflexes\n(.*?)\n##", content, re.DOTALL)
        assert match, f"{filename}: ## Réflexes section not found"
        bullets = [line for line in match.group(1).splitlines() if line.strip().startswith("-")]
        assert len(bullets) >= 4, (
            f"{filename}: ## Réflexes has only {len(bullets)} bullet(s), expected ≥ 4"
        )

    @pytest.mark.parametrize("filename", AGENT_FILES)
    def test_erreurs_has_at_least_four_bullets(self, filename: str):
        content = _read(filename)
        match = re.search(r"## Erreurs à éviter\n(.*?)\n##", content, re.DOTALL)
        assert match, f"{filename}: ## Erreurs à éviter section not found"
        bullets = [line for line in match.group(1).splitlines() if line.strip().startswith("-")]
        assert len(bullets) >= 4, (
            f"{filename}: ## Erreurs à éviter has only {len(bullets)} bullet(s), expected ≥ 4"
        )
