"""CLI entry point: `pco-services-mcp [serve|login|status|logout]`."""

from __future__ import annotations

import asyncio
import getpass
import sys

from pco_services_mcp import credentials
from pco_services_mcp.client import PcoClient, PcoError


async def _check(creds: credentials.Credentials) -> str:
    client = PcoClient(creds)
    try:
        return await client.me_id()
    finally:
        await client.aclose()


def cmd_login() -> None:
    print(
        "Create a Personal Access Token at https://api.planningcenteronline.com/oauth/applications"
    )
    app_id = getpass.getpass("Application ID (hidden): ").strip()
    secret = getpass.getpass("Secret (hidden): ").strip()
    if not (app_id and secret):
        sys.exit("Both values are required; nothing was saved.")
    creds = credentials.Credentials(app_id, secret)
    try:
        asyncio.run(_check(creds))
    except PcoError as exc:
        sys.exit(f"Not saved: {exc}")
    credentials.save(creds)
    print("Token verified and saved to the system credential store.")


def cmd_status() -> None:
    try:
        creds = credentials.load()
        asyncio.run(_check(creds))
    except (credentials.CredentialsError, PcoError) as exc:
        sys.exit(str(exc))
    print("Signed in: the token works.")


def cmd_logout() -> None:
    credentials.delete()
    print(
        "Stored token removed. Revoke it too at https://api.planningcenteronline.com/oauth/applications"
    )


def cmd_serve() -> None:
    from pco_services_mcp.server import main

    main()


COMMANDS = {"serve": cmd_serve, "login": cmd_login, "status": cmd_status, "logout": cmd_logout}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "serve"
    command = COMMANDS.get(name)
    if command is None:
        sys.exit(f"Usage: pco-services-mcp [{'|'.join(COMMANDS)}]")
    command()
