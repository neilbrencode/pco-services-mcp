from __future__ import annotations

import pytest

from pco_services_mcp import credentials


@pytest.fixture
def store(monkeypatch):
    values = {}
    monkeypatch.setattr(credentials.keyring, "get_password", lambda s, k: values.get(k))
    monkeypatch.delenv(credentials.ENV_APP_ID, raising=False)
    monkeypatch.delenv(credentials.ENV_SECRET, raising=False)
    return values


def test_store_is_used_when_env_is_empty(store):
    store.update(application_id="stored-id", secret="stored-secret")
    assert credentials.load() == credentials.Credentials("stored-id", "stored-secret")


def test_env_pair_wins(store, monkeypatch):
    store.update(application_id="stored-id", secret="stored-secret")
    monkeypatch.setenv(credentials.ENV_APP_ID, "env-id")
    monkeypatch.setenv(credentials.ENV_SECRET, "env-secret")
    assert credentials.load() == credentials.Credentials("env-id", "env-secret")


@pytest.mark.parametrize("only", [credentials.ENV_APP_ID, credentials.ENV_SECRET])
def test_half_an_env_pair_is_refused_not_mixed(store, monkeypatch, only):
    store.update(application_id="stored-id", secret="stored-secret")
    monkeypatch.setenv(only, "env-value")
    with pytest.raises(credentials.CredentialsError, match="both"):
        credentials.load()


def test_missing_everything_names_login(store):
    with pytest.raises(credentials.CredentialsError, match="pco-services-mcp login"):
        credentials.load()


def test_a_failed_save_keeps_the_previous_pair(store, monkeypatch):
    store.update(application_id="old-id", secret="old-secret")

    def set_password(_service, key, value):
        if key == "secret" and value == "new-secret":
            raise RuntimeError("store unavailable")
        store[key] = value

    monkeypatch.setattr(credentials.keyring, "set_password", set_password)
    with pytest.raises(RuntimeError):
        credentials.save(credentials.Credentials("new-id", "new-secret"))
    assert credentials.load() == credentials.Credentials("old-id", "old-secret")
