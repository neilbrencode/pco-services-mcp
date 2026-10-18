"""A fake Planning Center behind httpx.MockTransport. Every value here is synthetic."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
import pytest

from pco_services_mcp import server
from pco_services_mcp.client import PcoClient
from pco_services_mcp.credentials import Credentials

ME = "1001"
FAKE_CREDS = Credentials("test-application-id", "test-secret-value")

Handler = Callable[[httpx.Request], httpx.Response]


def jsonapi(data, *, included=None, next_url=None, status=200) -> httpx.Response:
    body = {"data": data, "included": included or [], "links": {}}
    if next_url:
        body["links"]["next"] = next_url
    return httpx.Response(status, json=body)


def error(status: int, detail: str = "synthetic failure") -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"detail": detail}]})


class FakePco:
    """Routes by (method, path); records every request that reached the network."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []
        self.route("GET", "/services/v2/me", lambda r: jsonapi({"type": "Person", "id": ME}))
        self.route(
            "GET",
            "/services/v2",
            lambda r: jsonapi(
                {
                    "type": "Organization",
                    "id": "1",
                    "attributes": {"time_zone": "America/Los_Angeles"},
                }
            ),
        )

    def route(self, method: str, path: str, handler: Handler | httpx.Response) -> None:
        self.routes[(method, path)] = handler if callable(handler) else (lambda r, h=handler: h)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return error(404, f"no fake route for {request.method} {request.url.path}")
        return handler(request)

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method in (None, r.method)]

    def bodies(self, method: str, path: str) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == method and urlsplit(str(r.url)).path == path
        ]


@pytest.fixture
def fake(monkeypatch) -> FakePco:
    fake = FakePco()
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(server.state, "factory", lambda: PcoClient(FAKE_CREDS, transport=transport))
    monkeypatch.setattr(server.state, "client", None)
    monkeypatch.setattr(server.state, "zone_name", None)
    monkeypatch.setattr(server.state, "service_type_names", {})
    monkeypatch.delenv("PCO_TIME_ZONE", raising=False)
    return fake


@pytest.fixture
def client_for(fake):
    """A bare PcoClient wired to the fake, for client-level tests."""
    return PcoClient(FAKE_CREDS, transport=httpx.MockTransport(fake))
