from __future__ import annotations

import httpx
import pytest

from pco_services_mcp import client as client_mod
from pco_services_mcp.client import PcoClient, PcoError, api_url, check_id, check_storage_url

from .conftest import FAKE_CREDS, error, jsonapi


@pytest.mark.parametrize(
    "url",
    [
        "/services/v2",
        "/services/v2/me",
        "https://api.planningcenteronline.com/services/v2/people/1/schedules?offset=100",
    ],
)
def test_api_url_allows_services(url):
    assert api_url(url).startswith("https://api.planningcenteronline.com/services/v2")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/services/v2/me",
        "http://api.planningcenteronline.com/services/v2/me",
        "https://user@api.planningcenteronline.com/services/v2/me",
        "https://api.planningcenteronline.com:8443/services/v2/me",
        "https://api.planningcenteronline.com.evil.example/services/v2/me",
        "/services/v2/../people/v2/people",
        "/people/v2/me",
        "/giving/v2/donations",
        "//evil.example/services/v2/me",
    ],
)
def test_api_url_refuses_everything_else(url):
    with pytest.raises(PcoError):
        api_url(url)


@pytest.mark.parametrize("value", ["12", " 34 "])
def test_check_id_accepts_digits(value):
    assert check_id(value) == value.strip()


@pytest.mark.parametrize("value", ["", "1/2", "../1", "1?x=1", "abc", "1" * 21])
def test_check_id_refuses_non_digits(value):
    with pytest.raises(PcoError):
        check_id(value)


async def test_hostile_next_link_is_refused_before_any_request(fake, client_for):
    fake.route(
        "GET",
        "/services/v2/people/1/blockouts",
        jsonapi([{"type": "Blockout", "id": "1"}], next_url="https://evil.example/services/v2/x"),
    )
    with pytest.raises(PcoError):
        await client_for.get_all("/services/v2/people/1/blockouts")
    assert fake.hosts() == {"api.planningcenteronline.com"}


async def test_traversal_bound_marks_incomplete(fake, client_for):
    url = "https://api.planningcenteronline.com/services/v2/people/1/blockouts?offset=1"
    fake.route(
        "GET", "/services/v2/people/1/blockouts", jsonapi([{"type": "B", "id": "1"}], next_url=url)
    )
    doc = await client_for.get_all("/services/v2/people/1/blockouts", max_pages=3)
    assert doc.incomplete and len(doc.data) == 3


async def test_429_is_retried_then_succeeds(fake, client_for, monkeypatch):
    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)
    replies = iter(
        [httpx.Response(429, headers={"Retry-After": "0"})] * 2 + [jsonapi({"id": ME_ID})]
    )
    fake.route("GET", "/services/v2/me", lambda r: next(replies))
    assert await client_for.me_id() == ME_ID
    assert len(fake.paths()) == 3


async def test_429_gives_up_after_three_attempts(fake, client_for, monkeypatch):
    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)
    fake.route("GET", "/services/v2/me", httpx.Response(429, headers={"Retry-After": "0"}))
    with pytest.raises(PcoError, match="rate limit"):
        await client_for.me_id()
    assert len(fake.paths()) == 3


async def test_401_names_the_remedy(fake, client_for):
    fake.route("GET", "/services/v2/me", error(401, "bad token"))
    with pytest.raises(PcoError, match="bad token.*pco-services-mcp login"):
        await client_for.me_id()


@pytest.mark.parametrize(
    "url", ["https://s3.amazonaws.com/b/k?sig=1", "https://bucket.s3.amazonaws.com/k?sig=1"]
)
def test_storage_url_allowed(url):
    assert check_storage_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://s3.amazonaws.com/b/k",
        "https://s3.amazonaws.com.evil.example/k",
        "https://evil.example/k",
        "https://127.0.0.1/k",
        "https://localhost/k",
        "https://x@s3.amazonaws.com/k",
    ],
)
def test_storage_url_refused(url):
    with pytest.raises(PcoError):
        check_storage_url(url)


async def test_redirect_to_unlisted_host_is_refused_before_connecting(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest"})

    pco = PcoClient(FAKE_CREDS, transport=httpx.MockTransport(handler))
    with pytest.raises(PcoError, match="unrecognised host"):
        await pco.download(
            "https://s3.amazonaws.com/b/k?X-Amz-" + "Signature=secret", tmp_path / "f.pdf"
        )
    assert seen == ["s3.amazonaws.com"]
    assert list(tmp_path.iterdir()) == []


async def test_download_never_sends_credentials(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, content=b"%PDF-1.7 synthetic")

    pco = PcoClient(FAKE_CREDS, transport=httpx.MockTransport(handler))
    assert await pco.download("https://s3.amazonaws.com/b/k", tmp_path / "f.pdf") == 18
    assert seen == [None]


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"%PDF-partial"
        raise httpx.ReadError("connection dropped")


async def test_interrupted_download_leaves_nothing_and_hides_the_url(tmp_path):
    pco = PcoClient(
        FAKE_CREDS,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=_BrokenStream())),
    )
    with pytest.raises(PcoError) as info:
        await pco.download(
            "https://s3.amazonaws.com/b/k?X-Amz-" + "Signature=topsecret", tmp_path / "f.pdf"
        )
    assert "topsecret" not in str(info.value) and "s3.amazonaws.com/b" not in str(info.value)
    assert list(tmp_path.iterdir()) == []


ME_ID = "1001"


async def _no_sleep(_seconds):
    return None
