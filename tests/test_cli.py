"""`login` checks a token with Planning Center before it replaces the stored one."""

from __future__ import annotations

import httpx
import pytest

from pco_services_mcp import cli, credentials
from pco_services_mcp.client import PcoClient

from .conftest import error


@pytest.fixture
def login(fake, monkeypatch):
    """Run `login` with typed-in values, against the fake; returns what reached the store."""
    saved: list[tuple[credentials.Credentials, int]] = []
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(cli, "PcoClient", lambda creds: PcoClient(creds, transport=transport))
    # Each save records how many requests had reached Planning Center by then.
    monkeypatch.setattr(
        credentials, "save", lambda creds: saved.append((creds, len(fake.requests)))
    )

    def run(*typed: str):
        answers = iter(typed)
        monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
        cli.cmd_login()

    return run, saved


def test_a_verified_token_is_saved_after_the_check(fake, login, capsys):
    run, saved = login
    run(" new-id ", "new-secret")
    assert saved == [(credentials.Credentials("new-id", "new-secret"), 1)]
    assert fake.paths() == ["/services/v2/me"]
    assert "verified and saved" in capsys.readouterr().out


def test_a_rejected_token_is_never_saved(fake, login):
    run, saved = login
    fake.route("GET", "/services/v2/me", error(401, "bad token"))
    with pytest.raises(SystemExit, match="Not saved"):
        run("new-id", "new-secret")
    assert saved == []


def test_a_blank_value_is_refused_before_any_request(fake, login):
    run, saved = login
    with pytest.raises(SystemExit, match="nothing was saved"):
        run("new-id", "   ")
    assert saved == [] and fake.requests == []
