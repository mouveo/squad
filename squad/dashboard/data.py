"""Read-only data layer backing the Streamlit dashboard.

LOT 1 only needs a session count for the wiring-check landing page;
later lots extend this module with typed rows, details and plan views.
"""

from pathlib import Path

from sqlite_utils import Database

from squad.config import get_global_db_path


def count_sessions(db_path: Path | None = None) -> int:
    """Return the total number of sessions stored in the registry."""
    path = db_path or get_global_db_path()
    if not path.exists():
        return 0
    db = Database(path)
    if "sessions" not in db.table_names():
        return 0
    return db["sessions"].count
