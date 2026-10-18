"""Personal Access Token storage in the OS credential store.

The token is never written to a file. Environment variables override the
store, for hosts without one (CI, containers).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "pco-services-mcp"
_APP_ID_KEY = "application_id"
_SECRET_KEY = "secret"
ENV_APP_ID = "PCO_APPLICATION_ID"
ENV_SECRET = "PCO_SECRET"


@dataclass(frozen=True)
class Credentials:
    application_id: str
    secret: str


class CredentialsError(RuntimeError):
    pass


def load() -> Credentials:
    """Both values from the environment, or both from the store — never a mix."""
    env_app_id, env_secret = os.environ.get(ENV_APP_ID), os.environ.get(ENV_SECRET)
    if env_app_id or env_secret:
        if not (env_app_id and env_secret):
            raise CredentialsError(
                f"Set both {ENV_APP_ID} and {ENV_SECRET}, or neither to use the stored token."
            )
        return Credentials(env_app_id, env_secret)
    app_id = keyring.get_password(SERVICE, _APP_ID_KEY)
    secret = keyring.get_password(SERVICE, _SECRET_KEY)
    if not (app_id and secret):
        raise CredentialsError(
            "No Planning Center Personal Access Token found. Run `pco-services-mcp login` "
            "in a terminal on this computer."
        )
    return Credentials(app_id, secret)


def save(creds: Credentials) -> None:
    """Store both halves, or leave the previous pair as it was."""
    previous = {key: keyring.get_password(SERVICE, key) for key in (_APP_ID_KEY, _SECRET_KEY)}
    try:
        keyring.set_password(SERVICE, _APP_ID_KEY, creds.application_id)
        keyring.set_password(SERVICE, _SECRET_KEY, creds.secret)
    except Exception:
        for key, value in previous.items():
            if value is None:
                _delete(key)
            else:
                keyring.set_password(SERVICE, key, value)
        raise


def _delete(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except PasswordDeleteError:
        pass


def delete() -> None:
    for key in (_APP_ID_KEY, _SECRET_KEY):
        _delete(key)
