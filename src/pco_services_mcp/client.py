"""Minimal async client for the Planning Center Services API (JSON:API).

Two trust boundaries live here:

- The Basic-auth client only ever talks to the Planning Center API origin, on
  an allowlisted path. Every URL, including `links.next` and `links.self` from
  a response, is checked before the request is built.
- Attachment files come from pre-signed storage URLs. They are fetched by a
  second client that carries no credentials, only from allowlisted storage
  hosts, with each redirect hop re-checked.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from pco_services_mcp import __version__
from pco_services_mcp.credentials import Credentials

API_HOST = "api.planningcenteronline.com"
API_ROOT = f"https://{API_HOST}"
SERVICES = "/services/v2"
USER_AGENT = f"pco-services-mcp/{__version__}"
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 3

# Hosts that serve attachment files, matched exactly or as a dot-suffix.
# Observed live: `/open` returns pre-signed S3 URLs.
STORAGE_HOSTS: tuple[str, ...] = ("s3.amazonaws.com",)

_ID = re.compile(r"^\d{1,20}$")

# httpx logs every request URL at INFO. Over stdio that reaches the MCP host's
# log files, and a pre-signed download URL is a bearer credential.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class PcoError(ToolError):
    """An anticipated failure; its text reaches the MCP client as-is."""


def check_id(value: str, what: str = "id") -> str:
    value = str(value).strip()
    if not _ID.match(value):
        raise PcoError(f"{what} must be a numeric Planning Center id.")
    return value


def api_url(path_or_url: str) -> str:
    """Resolve against the API root and refuse anything off the allowlist."""
    url = urljoin(API_ROOT + "/", path_or_url)
    parts = urlsplit(url)
    path = parts.path
    if (
        parts.scheme != "https"
        or parts.hostname != API_HOST
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or posixpath.normpath(path) != path.rstrip("/")
        or not (path == SERVICES or path.startswith(SERVICES + "/"))
    ):
        raise PcoError("Refused a request outside the Planning Center Services API.")
    return url


def check_storage_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = any(host == h or host.endswith("." + h) for h in STORAGE_HOSTS)
    if parts.scheme != "https" or parts.username or parts.password or not allowed:
        raise PcoError(f"Refused to download from an unrecognised host ({host or 'none'}).")
    return url


@dataclass
class Document:
    """The resources of one or more JSON:API pages, with `included` indexed."""

    data: list[dict[str, Any]] = field(default_factory=list)
    included: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # True when pages remained after the traversal bound was reached.
    incomplete: bool = False

    def related(self, resource: dict[str, Any], name: str) -> dict[str, Any] | None:
        ref = ((resource.get("relationships") or {}).get(name) or {}).get("data")
        if not isinstance(ref, dict):
            return None
        return self.included.get((ref["type"], ref["id"]))


def related_id(resource: dict[str, Any], name: str) -> str | None:
    ref = ((resource.get("relationships") or {}).get(name) or {}).get("data")
    return ref.get("id") if isinstance(ref, dict) else None


def _describe(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("errors") or []
        detail = "; ".join(e.get("detail") or e.get("title") or "" for e in errors).strip("; ")
    except ValueError:
        detail = ""
    hint = {
        401: "the Personal Access Token was rejected; run `pco-services-mcp login` again",
        403: "your Planning Center permissions do not allow this",
        404: "not found, or not visible to your account",
        429: "Planning Center's rate limit; try again in a minute",
    }.get(resp.status_code, "")
    parts = [p for p in (detail, hint) if p]
    return f"Planning Center returned {resp.status_code}" + (
        f": {' - '.join(parts)}" if parts else ""
    )


class PcoClient:
    def __init__(self, creds: Credentials, *, transport: httpx.AsyncBaseTransport | None = None):
        self._api = httpx.AsyncClient(
            auth=httpx.BasicAuth(creds.application_id, creds.secret),
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
        )
        self._files = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=120.0,
            follow_redirects=False,
            transport=transport,
        )
        self._me: str | None = None

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._files.aclose()

    async def request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict[str, Any] | None:
        url = api_url(path)
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await self._api.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:
                raise PcoError(f"Could not reach Planning Center ({type(exc).__name__}).") from None
            if resp.status_code != 429 or attempt == MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(min(float(resp.headers.get("Retry-After", "2")), 30.0))
        if resp.status_code >= 400:
            raise PcoError(_describe(resp))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.request("GET", path, params=params or None) or {}

    async def pages(self, path: str, *, max_pages: int = 10, **params: Any):
        """Yield each page in turn, following `links.next` up to `max_pages`."""
        params.setdefault("per_page", 100)
        url: str | None = path
        for _ in range(max_pages):
            page = await self.get(url, **params) if url == path else await self.get(url)
            yield page
            url = (page.get("links") or {}).get("next")
            if not url:
                return
        raise MorePagesError()

    async def get_all(self, path: str, *, max_pages: int = 10, **params: Any) -> Document:
        doc = Document()
        try:
            async for page in self.pages(path, max_pages=max_pages, **params):
                data = page.get("data") or []
                doc.data.extend(data if isinstance(data, list) else [data])
                for res in page.get("included") or []:
                    doc.included[(res["type"], res["id"])] = res
        except MorePagesError:
            doc.incomplete = True
        return doc

    async def me_id(self) -> str:
        if self._me is None:
            self._me = check_id((await self.get(f"{SERVICES}/me"))["data"]["id"], "person id")
        return self._me

    async def download(self, url: str, dest: Path) -> int:
        """Stream a pre-signed file to `dest` (already confined by the caller)."""
        for _ in range(MAX_REDIRECTS + 1):
            check_storage_url(url)
            try:
                async with self._files.stream("GET", url) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("Location", ""))
                        continue
                    if resp.status_code >= 400:
                        raise PcoError(f"File download returned {resp.status_code}.")
                    return await _write_atomically(resp, dest)
            except httpx.HTTPError as exc:
                # The pre-signed URL is a bearer credential: never echo it.
                raise PcoError(f"File download failed ({type(exc).__name__}).") from None
        raise PcoError("File download redirected too many times.")


class MorePagesError(Exception):
    pass


async def _write_atomically(resp: httpx.Response, dest: Path) -> int:
    fd, tmp_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=dest.parent)
    tmp = Path(tmp_name)
    size = 0
    try:
        with open(fd, "wb") as fh:
            async for chunk in resp.aiter_bytes():
                fh.write(chunk)
                size += len(chunk)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return size
