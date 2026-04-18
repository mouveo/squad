#!/usr/bin/env bash
# Pre-flight check before launching a real Squad session.
# Exits non-zero on the first failure with a clear message.

set -euo pipefail

fail() { echo "❌ $1" >&2; exit 1; }
ok()   { echo "✅ $1"; }
info() { echo "ℹ  $1"; }

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

info "Pre-flight check for Squad (from $REPO_ROOT)"
echo

# ── Git state ─────────────────────────────────────────────────────────────────
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$CURRENT_BRANCH" == "main" ]] || fail "Current branch is '$CURRENT_BRANCH', expected 'main'."
ok "On branch main"

if ! git diff --quiet || ! git diff --staged --quiet; then
  fail "Uncommitted changes detected. Commit or stash before running a real session."
fi
ok "Working tree clean"

# ── Venv ──────────────────────────────────────────────────────────────────────
VENV_PY="$REPO_ROOT/.venv/bin/python3.11"
[[ -x "$VENV_PY" ]] || fail "Venv not found at .venv — run 'python3.11 -m venv .venv && .venv/bin/pip install -e \".[slack,dashboard,dev]\"'."
ok "Venv present ($VENV_PY)"

"$VENV_PY" -c "import squad, slack_bolt" 2>/dev/null || fail "squad or slack_bolt not importable in venv — run pip install -e \".[slack]\"."
ok "squad + slack_bolt importable"

# ── Claude CLI ────────────────────────────────────────────────────────────────
command -v claude >/dev/null || fail "claude CLI not on PATH."
if ! claude --print --max-turns 2 --model "claude-sonnet-4-6" "say ok" 2>/dev/null | grep -qi "ok"; then
  fail "Claude CLI auth check failed. Run 'claude' interactively to re-authenticate."
fi
ok "Claude CLI authenticated"

# ── Forge CLI (optional but recommended) ──────────────────────────────────────
if command -v forge >/dev/null; then
  ok "forge CLI present on PATH"
else
  info "forge CLI not on PATH (OK if you don't auto-submit plans)"
fi

# ── Slack tokens ──────────────────────────────────────────────────────────────
[[ -n "${SQUAD_SLACK_BOT_TOKEN:-}" ]] || fail "SQUAD_SLACK_BOT_TOKEN not set in environment."
[[ -n "${SQUAD_SLACK_APP_TOKEN:-}" ]] || fail "SQUAD_SLACK_APP_TOKEN not set in environment."
[[ "$SQUAD_SLACK_BOT_TOKEN" == xoxb-* ]] || fail "SQUAD_SLACK_BOT_TOKEN does not start with 'xoxb-'."
[[ "$SQUAD_SLACK_APP_TOKEN" == xapp-* ]] || fail "SQUAD_SLACK_APP_TOKEN does not start with 'xapp-'."
ok "Slack tokens present"

# ── Squad config ──────────────────────────────────────────────────────────────
CONFIG_FILE="$HOME/.squad/config.yaml"
[[ -f "$CONFIG_FILE" ]] || fail "$CONFIG_FILE not found — run 'squad init'."
grep -q 'bot_token:.*SQUAD_SLACK_BOT_TOKEN' "$CONFIG_FILE" || fail "$CONFIG_FILE missing slack.bot_token entry pointing to env var."
grep -q 'app_token:.*SQUAD_SLACK_APP_TOKEN' "$CONFIG_FILE" || fail "$CONFIG_FILE missing slack.app_token entry pointing to env var."
ok "Squad config OK ($CONFIG_FILE)"

# ── Target project ────────────────────────────────────────────────────────────
TARGET="${1:-$HOME/Developer/sitavista}"
[[ -d "$TARGET" ]] || fail "Target project directory '$TARGET' does not exist."
[[ -f "$TARGET/CLAUDE.md" ]] || info "No CLAUDE.md in $TARGET (Squad will still run but with less context)."
ok "Target project: $TARGET"

# ── No zombie serve ───────────────────────────────────────────────────────────
SERVE_COUNT="$(pgrep -f "squad serve" 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$SERVE_COUNT" -gt 1 ]]; then
  fail "$SERVE_COUNT squad serve processes running. Kill them with 'pkill -f \"squad serve\"' before starting a clean one."
fi
if [[ "$SERVE_COUNT" -eq 0 ]]; then
  info "No squad serve running. Start with 'squad serve' in a dedicated terminal before sending the /squad command."
else
  ok "One squad serve process running"
fi

# ── Deepsearch file ───────────────────────────────────────────────────────────
DEEPSEARCH="$TARGET/plans/deep-research/deep-research-report-crm-comparison-sitavista-vs-leaders.md"
if [[ -f "$DEEPSEARCH" ]]; then
  SIZE=$(wc -c < "$DEEPSEARCH" | tr -d ' ')
  ok "Deepsearch present ($SIZE bytes at $DEEPSEARCH)"
else
  info "No deepsearch at $DEEPSEARCH (OK if not applicable to this test)."
fi

echo
ok "All checks passed. Ready to launch Squad."
