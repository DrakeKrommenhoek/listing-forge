"""Authorisation for the private review interface.

The review UI can change prices and approve publication, so it is the most
sensitive surface in the system. Two properties matter and both are tested
here: it is genuinely closed without the right token, and it is genuinely
*open* when the token is configured by any of the ways this process actually
gets started.

The second one is the regression. ``_token()` used to read ``os.environ``
directly, which only works when systemd starts the service with
``EnvironmentFile=``. Under ``make run``, an activated venv, Docker Compose,
or a developer laptop, ``.env`` is loaded by pydantic-settings and
``os.environ`` is never touched — so every route answered "Not authorised"
and the page explained that the variable must be unset, while it sat in
``.env`` the whole time. Unfalsifiable from the browser, and it cost an
evening.
"""

from __future__ import annotations

import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-review-auth-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate._compat import Settings  # noqa: E402
from estate import api as estate_api  # noqa: E402

TOKEN = "test-review-token-do-not-use-anywhere-real"


@pytest.fixture(autouse=True)
def _reset_warning():
    estate_api._TOKEN_WARNED = False
    yield
    estate_api._TOKEN_WARNED = False


def _with_settings(monkeypatch, **overrides):
    """Point get_settings() at an isolated Settings, never the real .env.

    ``_env_file=None`` is mandatory here: a bare ``Settings()`` loads the
    production file, and an assertion failure would then print the whole
    object — bot token included — into the terminal. That has happened once
    already and cost a credential rotation. tests/unit/test_config.py has an
    AST guard that fails the build if this is forgotten.
    """
    settings = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(estate_api, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        "estate._compat.get_settings", lambda: settings, raising=False
    )
    return settings


class _Request:
    """The two things _authorised() reads off a request."""

    def __init__(self, token: str = "", cookie: str = ""):
        self.query_params = {"token": token} if token else {}
        self.cookies = {estate_api.COOKIE: cookie} if cookie else {}


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_the_token_is_read_from_settings_not_just_the_process_environment(
    monkeypatch
):
    """The whole point: configured in .env, never exported, still works."""
    monkeypatch.delenv("ESTATE_REVIEW_TOKEN", raising=False)
    _with_settings(monkeypatch, estate_review_token=TOKEN)

    assert estate_api._token() == TOKEN
    assert estate_api._authorised(_Request(token=TOKEN)) is True


def test_a_token_in_the_process_environment_still_works(monkeypatch):
    """systemd's EnvironmentFile= path must keep working unchanged."""
    monkeypatch.setenv("ESTATE_REVIEW_TOKEN", TOKEN)
    _config.get_settings.cache_clear()
    try:
        assert estate_api._token() == TOKEN
        assert estate_api._authorised(_Request(token=TOKEN)) is True
    finally:
        _config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Still closed
# ---------------------------------------------------------------------------


def test_an_unset_token_disables_the_interface_entirely(monkeypatch):
    monkeypatch.delenv("ESTATE_REVIEW_TOKEN", raising=False)
    _with_settings(monkeypatch, estate_review_token="")

    assert estate_api._token() == ""
    assert estate_api._authorised(_Request(token="")) is False
    # And an empty supplied token must not match an empty expected one.
    assert estate_api._authorised(_Request(token="anything")) is False


def test_a_wrong_token_is_refused(monkeypatch):
    _with_settings(monkeypatch, estate_review_token=TOKEN)
    assert estate_api._authorised(_Request(token="not-the-token")) is False


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(monkeypatch):
    _with_settings(monkeypatch, estate_review_token=TOKEN)
    assert estate_api._authorised(_Request(token=TOKEN[:-1])) is False


def test_surrounding_whitespace_in_configuration_is_tolerated(monkeypatch):
    """A trailing space in .env should not silently lock the operator out."""
    _with_settings(monkeypatch, estate_review_token=f"  {TOKEN}  ")
    assert estate_api._token() == TOKEN
    assert estate_api._authorised(_Request(token=TOKEN)) is True


def test_the_cookie_is_accepted_so_the_token_is_typed_once(monkeypatch):
    _with_settings(monkeypatch, estate_review_token=TOKEN)
    assert estate_api._authorised(_Request(cookie=TOKEN)) is True
    assert estate_api._authorised(_Request(cookie="wrong")) is False


# ---------------------------------------------------------------------------
# Diagnosability
# ---------------------------------------------------------------------------


def test_the_unset_case_is_logged_once_server_side(monkeypatch):
    """An operator cannot debug "Not authorised" from the browser.

    Logged once rather than per request, so a scanner hitting the endpoint
    cannot flood the journal.
    """
    records = []
    monkeypatch.setattr(estate_api.logger, "warning", lambda payload: records.append(payload))
    _with_settings(monkeypatch, estate_review_token="")

    estate_api._token()
    estate_api._token()
    estate_api._token()

    assert len(records) == 1
    assert records[0]["action"] == "estate_review_disabled"
    assert "restart" in records[0]["fix"]


def test_the_token_value_is_never_logged(monkeypatch):
    records = []
    monkeypatch.setattr(estate_api.logger, "warning", lambda payload: records.append(payload))
    _with_settings(monkeypatch, estate_review_token=TOKEN)

    estate_api._token()
    assert all(TOKEN not in str(r) for r in records)


def test_a_broken_configuration_closes_the_door_rather_than_500ing(monkeypatch):
    """A config error must not turn every route into a stack trace."""
    def explode():
        raise RuntimeError("config is broken")

    monkeypatch.setattr(estate_api, "get_settings", explode, raising=False)
    assert estate_api._token() == ""
    assert estate_api._authorised(_Request(token=TOKEN)) is False
