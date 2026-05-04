"""Single source of truth for phase identifiers, statuses, modes and agent capabilities."""

# Phase identifiers (ASCII snake_case — used in code, DB and filesystem)
PHASE_CADRAGE = "cadrage"
PHASE_ETAT_DES_LIEUX = "etat_des_lieux"
PHASE_BENCHMARK = "benchmark"
PHASE_CONCEPTION = "conception"
PHASE_CHALLENGE = "challenge"
PHASE_SYNTHESE = "synthese"

PHASES: list[str] = [
    PHASE_CADRAGE,
    PHASE_ETAT_DES_LIEUX,
    PHASE_BENCHMARK,
    PHASE_CONCEPTION,
    PHASE_CHALLENGE,
    PHASE_SYNTHESE,
]

# Human-readable phase labels (display only)
PHASE_LABELS: dict[str, str] = {
    PHASE_CADRAGE: "Cadrage",
    PHASE_ETAT_DES_LIEUX: "État des lieux",
    PHASE_BENCHMARK: "Benchmark",
    PHASE_CONCEPTION: "Conception",
    PHASE_CHALLENGE: "Challenge",
    PHASE_SYNTHESE: "Synthèse",
}

# Filesystem directory names for each phase
PHASE_DIRS: dict[str, str] = {
    PHASE_CADRAGE: "1-cadrage",
    PHASE_ETAT_DES_LIEUX: "2-etat-des-lieux",
    PHASE_BENCHMARK: "3-benchmark",
    PHASE_CONCEPTION: "4-conception",
    PHASE_CHALLENGE: "5-challenge",
    PHASE_SYNTHESE: "6-synthese",
}

# Session statuses
STATUS_DRAFT = "draft"
STATUS_INTERVIEWING = "interviewing"
STATUS_WORKING = "working"
STATUS_REVIEW = "review"
STATUS_APPROVED = "approved"
STATUS_QUEUED = "queued"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

SESSION_STATUSES: list[str] = [
    STATUS_DRAFT,
    STATUS_INTERVIEWING,
    STATUS_WORKING,
    STATUS_REVIEW,
    STATUS_APPROVED,
    STATUS_QUEUED,
    STATUS_DONE,
    STATUS_FAILED,
]

# Terminal statuses — a session in one of these is no longer in progress.
# Single source of truth for "is this session active?" across the DB
# layer, Slack, CLI and dashboard.
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_DONE, STATUS_FAILED})
ACTIVE_STATUSES: frozenset[str] = frozenset(
    s for s in SESSION_STATUSES if s not in TERMINAL_STATUSES
)

# Human-readable French label for each status (dashboard badges + CLI).
STATUS_LABELS: dict[str, str] = {
    STATUS_DRAFT: "Brouillon",
    STATUS_INTERVIEWING: "Questions",
    STATUS_WORKING: "En cours",
    STATUS_REVIEW: "À relire",
    STATUS_APPROVED: "Approuvé",
    STATUS_QUEUED: "Forge",
    STATUS_DONE: "Terminé",
    STATUS_FAILED: "Échec",
}

# Semantic tone per status — kept abstract (not a CSS color) so the
# dashboard can map it to whichever palette Streamlit exposes without
# re-declaring the taxonomy here. One of:
# "neutral" | "progress" | "info" | "warning" | "success" | "muted" | "danger".
STATUS_TONES: dict[str, str] = {
    STATUS_DRAFT: "neutral",
    STATUS_INTERVIEWING: "warning",
    STATUS_WORKING: "progress",
    STATUS_REVIEW: "info",
    STATUS_APPROVED: "success",
    STATUS_QUEUED: "progress",
    STATUS_DONE: "muted",
    STATUS_FAILED: "danger",
}

# Session modes
MODE_APPROVAL = "approval"
MODE_AUTONOMOUS = "autonomous"

SESSION_MODES: list[str] = [MODE_APPROVAL, MODE_AUTONOMOUS]

# Declarative agent capabilities
CAP_WEB_SEARCH = "web_search"
CAP_WEB_FETCH = "web_fetch"
CAP_READ_FILES = "read_files"
CAP_WRITE_FILES = "write_files"
CAP_EXECUTE_COMMANDS = "execute_commands"
CAP_GLOB = "glob"
CAP_LIST_FILES = "list_files"
CAP_GREP_FILES = "grep_files"

AGENT_CAPABILITIES: list[str] = [
    CAP_WEB_SEARCH,
    CAP_WEB_FETCH,
    CAP_READ_FILES,
    CAP_WRITE_FILES,
    CAP_EXECUTE_COMMANDS,
    CAP_GLOB,
    CAP_LIST_FILES,
    CAP_GREP_FILES,
]
