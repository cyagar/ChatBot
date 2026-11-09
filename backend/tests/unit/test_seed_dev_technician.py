"""seed_dev_technician.py: the APP_ENV guard must run BEFORE
run_migrations() in main()'s real execution path -- a guard checked only
inside seed_dev_technician() itself would run too late, after main() has
already applied migrations to whatever database is configured.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import seed_dev_technician as script  # noqa: E402


def test_main_refuses_before_running_migrations_outside_development(test_env, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    migrations_ran = []
    monkeypatch.setattr(script, "run_migrations", lambda: migrations_ran.append(True))
    monkeypatch.setattr(sys, "argv", ["seed_dev_technician.py", "--password", "irrelevant123"])

    with pytest.raises(SystemExit):
        script.main()

    assert migrations_ran == [], "migrations must never run before the APP_ENV guard has passed"
    get_settings.cache_clear()


def test_seed_dev_technician_function_itself_also_refuses_outside_development(test_env, monkeypatch):
    """Defense in depth: a future direct caller of this function (not through
    main()) must still be refused, not just main()'s own call path."""
    from app.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="development"):
            script.seed_dev_technician("someone@example.com", "irrelevant123")
    finally:
        get_settings.cache_clear()
