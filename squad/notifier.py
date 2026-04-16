"""Slack notifications via webhook — questions pending, plans ready, agent errors."""

import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


def _get_webhook() -> str | None:
    """Return the configured Slack webhook URL, with SQUAD_ taking priority over FORGE_."""
    return os.environ.get("SQUAD_SLACK_WEBHOOK") or os.environ.get("FORGE_SLACK_WEBHOOK")


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _send(payload: dict) -> None:
    """Post a Slack notification. No-ops silently if no webhook is configured."""
    webhook = _get_webhook()
    if not webhook:
        logger.warning(
            "Slack notification skipped: no webhook configured "
            "(set SQUAD_SLACK_WEBHOOK or FORGE_SLACK_WEBHOOK)"
        )
        return
    try:
        response = httpx.post(webhook, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to send Slack notification: %s", exc)


def notify_questions_pending(session_id: str, title: str, count: int) -> None:
    """Notify that questions are awaiting user answers."""
    _send(
        {
            "text": (
                f"*[Squad]* {count} question(s) en attente de réponse\n"
                f"Projet : *{title}*\n"
                f"Session : `{session_id}`\n"
                f"_Répondez via `squad answer {session_id}`_"
            ),
            "session_id": session_id,
            "title": title,
            "timestamp": _now_iso(),
        }
    )


def notify_plans_ready(session_id: str, title: str, plan_count: int) -> None:
    """Notify that generated plans are ready for review."""
    _send(
        {
            "text": (
                f"*[Squad]* {plan_count} plan(s) prêt(s) pour validation\n"
                f"Projet : *{title}*\n"
                f"Session : `{session_id}`\n"
                f"_Consultez via `squad review {session_id}`_"
            ),
            "session_id": session_id,
            "title": title,
            "timestamp": _now_iso(),
        }
    )


def notify_agent_error(session_id: str, title: str, agent: str, error: str) -> None:
    """Notify of an agent or step execution error."""
    _send(
        {
            "text": (
                f"*[Squad]* :warning: Erreur agent `{agent}`\n"
                f"Projet : *{title}*\n"
                f"Session : `{session_id}`\n"
                f"Erreur : {error[:200]}"
            ),
            "session_id": session_id,
            "title": title,
            "timestamp": _now_iso(),
        }
    )
