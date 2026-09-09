"""Config resolution — in particular where the knowledge base lives.

The default is cwd-relative, so the installed `comsol-support` console
script addresses a different (usually empty) database when run from
outside the repo. COMSOL_DB is the escape hatch, and it is the same
variable the MCP server and orchestrator already speak.
"""

from pathlib import Path

from comsol_support._source_checkout import SOURCE_ROOT
from comsol_support.config import DEFAULT_DB_RELPATH, Config, default_db_path

CHECKOUT_DB = SOURCE_ROOT / DEFAULT_DB_RELPATH


def test_default_db_path_without_env(monkeypatch):
    monkeypatch.delenv("COMSOL_DB", raising=False)
    assert default_db_path() == CHECKOUT_DB
    assert Config().db_path == CHECKOUT_DB
    assert CHECKOUT_DB.is_absolute()


def test_comsol_db_env_overrides_default(monkeypatch, tmp_path):
    db = tmp_path / "campaign.db"
    monkeypatch.setenv("COMSOL_DB", str(db))
    assert default_db_path() == db
    # Read at construction time, so a Config built after the export sees
    # it — the field must not be a shared class-level default.
    assert Config().db_path == db


def test_db_default_is_not_shared_between_instances(monkeypatch, tmp_path):
    monkeypatch.delenv("COMSOL_DB", raising=False)
    a = Config()
    monkeypatch.setenv("COMSOL_DB", str(tmp_path / "other.db"))
    b = Config()
    assert a.db_path == CHECKOUT_DB
    assert b.db_path == tmp_path / "other.db"


def test_explicit_db_path_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COMSOL_DB", str(tmp_path / "env.db"))
    cfg = Config(db_path=Path("explicit.db"))
    assert cfg.db_path == Path("explicit.db")


def test_default_db_does_not_depend_on_cwd(monkeypatch, tmp_path):
    """1.0: the default is the checkout's data/comsol.db from any cwd —
    previously a fresh cwd silently got its own empty database."""
    monkeypatch.delenv("COMSOL_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_db_path() == CHECKOUT_DB
    assert not (tmp_path / "data").exists()
