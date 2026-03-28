from __future__ import annotations

import os
import time
from typing import Callable, Iterable

from sqlalchemy import text
from sqlalchemy.exc import OperationalError


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def db_test_policy() -> str:
    """Return DB-backed test policy: required|optional.

    Policy resolution (in order):
    1. ``XYN_DB_TEST_POLICY`` explicit override (required|optional)
    2. ``XYN_REQUIRE_DB_TESTS`` boolean override
    3. ``CI=true`` implies required
    4. default optional for local/dev convenience
    """

    explicit = str(os.getenv("XYN_DB_TEST_POLICY", "")).strip().lower()
    if explicit in {"required", "optional"}:
        return explicit
    if _as_bool(os.getenv("XYN_REQUIRE_DB_TESTS")):
        return "required"
    if _as_bool(os.getenv("CI")):
        return "required"
    return "optional"


def db_tests_required() -> bool:
    return db_test_policy() == "required"


def _probe_db(
    *,
    session_factory: Callable[[], object],
    required_tables: Iterable[str] = (),
) -> tuple[bool, str]:
    db = session_factory()
    try:
        db.execute(text("SELECT 1"))
        for table_name in required_tables:
            row = db.execute(text("SELECT to_regclass(:name)"), {"name": str(table_name)}).first()
            resolved = row[0] if row else None
            if not resolved:
                return False, f"required table '{table_name}' is missing"
        return True, ""
    except OperationalError as exc:
        return False, f"database unavailable: {exc}"
    except Exception as exc:
        return False, f"database readiness probe failed: {exc}"
    finally:
        db.close()


def require_db_or_skip(
    testcase,
    *,
    session_factory: Callable[[], object],
    required_tables: Iterable[str] = (),
    retries: int = 8,
    sleep_seconds: float = 0.25,
) -> None:
    """Enforce DB-backed test readiness with CI-vs-local policy split.

    - required mode (CI/explicit): fail loudly when DB is unavailable/unready
    - optional mode (local default): skip with explicit reason
    """

    attempts = max(1, int(retries))
    last_reason = "database readiness probe did not run"
    for _ in range(attempts):
        ok, reason = _probe_db(session_factory=session_factory, required_tables=required_tables)
        if ok:
            return
        last_reason = reason
        time.sleep(max(0.0, float(sleep_seconds)))

    msg = (
        "DB-backed test prerequisites not satisfied: "
        f"{last_reason}. policy={db_test_policy()} "
        "(set XYN_DB_TEST_POLICY=optional for local skip behavior)."
    )
    if db_tests_required():
        testcase.fail(msg)
    testcase.skipTest(msg)
