"""Tools exercised through a real in-process MCP client against the fake API."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp.client import Client

from pco_services_mcp import server

from .conftest import ME, error, jsonapi

PLAN = "/services/v2/service_types/20/plans/300"
ARGS = {"service_type_id": "20", "plan_id": "300"}


async def call(name: str, args: dict | None = None):
    async with Client(server.mcp) as c:
        result = await c.call_tool(name, args or {})
    text = result.content[0].text
    return (True, text) if result.is_error else (False, json.loads(text))


def schedule(sid: str, when: str, plan: str = "300", status: str = "C") -> dict:
    """A schedule entry; `when` is local wall-clock time with a "Z", as Planning Center sends it."""
    return {
        "type": "Schedule",
        "id": sid,
        "attributes": {
            "dates": when[:10],
            "sort_date": when,
            "service_type_name": "Sunday",
            "team_name": "Band",
            "team_position_name": "Keys",
            "status": status,
        },
        "relationships": {
            "service_type": {"data": {"type": "ServiceType", "id": "20"}},
            "plan": {"data": {"type": "Plan", "id": plan}},
        },
    }


def on_my_schedule(fake, scheduled: bool = True) -> None:
    fake.route(
        "GET",
        f"{PLAN}/my_schedules",
        jsonapi([schedule("9", "2099-01-01T09:00:00Z")] if scheduled else []),
    )


async def test_my_schedule_skips_the_past_and_respects_limit(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi(
            [
                schedule("1", "2000-01-01T09:00:00Z"),
                schedule("2", "2099-01-08T09:00:00Z"),
                schedule("3", "2099-01-01T09:00:00Z"),
            ]
        ),
    )
    is_error, out = await call("my_schedule", {"limit": 1})
    assert not is_error
    assert [s["dates"] for s in out["schedules"]] == ["2099-01-01"]
    assert out["schedules"][0]["status"] == "confirmed"
    assert out["incomplete"] is False


def plan_time(tid: str, kind: str, starts: str, ends: str) -> dict:
    attrs = {"time_type": kind, "name": kind.title(), "starts_at": starts, "ends_at": ends}
    return {"type": "PlanTime", "id": tid, "attributes": attrs}


async def test_my_schedule_gives_service_and_rehearsal_times_in_local_time(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", "2099-01-01T09:00:00Z")]),
    )
    fake.route(
        "GET",
        f"{PLAN}/plan_times",
        jsonapi(
            [
                plan_time("2", "service", "2099-01-01T17:00:00Z", "2099-01-01T18:15:00Z"),
                plan_time("1", "rehearsal", "2099-01-01T15:30:00Z", "2099-01-01T16:30:00Z"),
            ]
        ),
    )
    _, out = await call("my_schedule")
    assert out["time_zone"] == "America/Los_Angeles"
    [entry] = out["schedules"]
    assert "sort_date" not in entry
    assert entry["times"] == [
        {
            "type": "rehearsal",
            "name": "Rehearsal",
            "starts": "2099-01-01 07:30",
            "ends": "2099-01-01 08:30",
        },
        {
            "type": "service",
            "name": "Service",
            "starts": "2099-01-01 09:00",
            "ends": "2099-01-01 10:15",
        },
    ]


async def test_my_schedule_reports_hidden_plan_times_as_null(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", "2099-01-01T09:00:00Z")]),
    )
    fake.route("GET", f"{PLAN}/plan_times", error(403))
    _, out = await call("my_schedule")
    assert out["schedules"][0]["times"] is None


async def test_my_schedule_fetches_each_plans_times_once_and_reports_truncation(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", "2099-01-01T09:00:00Z"), schedule("2", "2099-01-01T09:00:00Z")]),
    )
    more = f"https://api.planningcenteronline.com{PLAN}/plan_times?offset=1"
    fake.route("GET", f"{PLAN}/plan_times", jsonapi([], next_url=more))
    _, out = await call("my_schedule")
    assert len(out["schedules"]) == 2
    assert out["incomplete"] is True
    # The fake answers every page with another next link, so ten pages were read, once.
    assert fake.paths().count(f"{PLAN}/plan_times") == 10


async def test_my_schedule_puts_undated_entries_after_dated_ones(fake):
    undated = schedule("1", "undated", plan="301")
    undated["attributes"]["sort_date"] = None
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([undated, schedule("2", "2099-01-01T09:00:00Z")]),
    )
    _, out = await call("my_schedule", {"limit": 1})
    assert [s["plan_id"] for s in out["schedules"]] == ["300"]


async def test_my_schedule_reads_sort_date_as_local_wall_clock_time(fake):
    # Ten local hours ago is still today's service. Read as UTC, the same sort_date would be
    # 17 or 18 hours ago in Los Angeles and fall outside the 12-hour window.
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime.now(zone)
    recent = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", stale, plan="301"), schedule("2", recent)]),
    )
    _, out = await call("my_schedule")
    assert [s["plan_id"] for s in out["schedules"]] == ["300"]


async def test_my_schedule_finds_the_next_service_after_many_pages_of_history(fake):
    url = f"https://api.planningcenteronline.com/services/v2/people/{ME}/schedules"
    pages = iter(
        [
            jsonapi(
                [schedule(str(i), "2000-01-01T00:00:00Z") for i in range(100)],
                next_url=url + "?offset=" + str(n),
            )
            for n in range(1, 7)
        ]
        + [jsonapi([schedule("999", "2099-02-01T09:00:00Z")])]
    )
    fake.route("GET", f"/services/v2/people/{ME}/schedules", lambda r: next(pages))
    _, out = await call("my_schedule")
    assert [s["dates"] for s in out["schedules"]] == ["2099-02-01"]


async def test_my_schedule_reports_an_unfinished_traversal(fake):
    url = f"https://api.planningcenteronline.com/services/v2/people/{ME}/schedules?offset=1"
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", "2000-01-01T00:00:00Z")], next_url=url),
    )
    _, out = await call("my_schedule")
    assert out == {"time_zone": "America/Los_Angeles", "schedules": [], "incomplete": True}


@pytest.mark.parametrize(
    "tool",
    ["plan_team", "team_contacts", "service_order", "list_attachments", "download_attachments"],
)
@pytest.mark.parametrize("reply", ["empty", "404"])
async def test_plan_tools_refuse_plans_not_on_my_schedule(fake, tool, reply):
    if reply == "empty":
        on_my_schedule(fake, scheduled=False)
    else:
        fake.route("GET", f"{PLAN}/my_schedules", error(404))
    is_error, text = await call(tool, ARGS)
    assert is_error and "isn't on your schedule" in text
    assert fake.paths() == [f"{PLAN}/my_schedules"]


async def test_plan_ids_must_be_numeric(fake):
    is_error, text = await call("plan_team", {"service_type_id": "20", "plan_id": "300/../1"})
    assert is_error and "numeric" in text
    assert fake.requests == []


def member(mid: str, team: str, name: str, person: str | None = None) -> dict:
    relationships = {"team": {"data": {"type": "Team", "id": team}}}
    if person:
        relationships["person"] = {"data": {"type": "Person", "id": person}}
    return {
        "type": "PlanPerson",
        "id": mid,
        "attributes": {
            "name": name,
            "team_position_name": "Vocals",
            "status": "U",
            "notes": "private",
        },
        "relationships": relationships,
    }


def team_members_route(fake, *members, plan: str = PLAN):
    fake.route(
        "GET",
        f"{plan}/team_members",
        jsonapi(
            list(members),
            included=[
                {"type": "Team", "id": "t1", "attributes": {"name": "Band"}},
                {"type": "Team", "id": "t2", "attributes": {"name": "Tech"}},
            ],
        ),
    )


async def test_plan_team_groups_by_team(fake):
    on_my_schedule(fake)
    team_members_route(fake, member("1", "t1", "Alex Example"), member("2", "t2", "Sam Sample"))
    _, out = await call("plan_team", ARGS)
    assert out["teams"] == {
        "Band": [{"name": "Alex Example", "position": "Vocals", "status": "unconfirmed"}],
        "Tech": [{"name": "Sam Sample", "position": "Vocals", "status": "unconfirmed"}],
    }


def contact_routes(fake):
    """2001 lets you see their details, 2002 doesn't; 1001 is you."""
    fake.route(
        "GET",
        "/services/v2/people/2001/emails",
        jsonapi(
            [
                {
                    "type": "Email",
                    "id": "e1",
                    "attributes": {
                        "address": "alex@example.com",
                        "primary": True,
                        "location": "Home",
                    },
                }
            ]
        ),
    )
    fake.route(
        "GET",
        "/people/v2/people/2001/phone_numbers",
        jsonapi(
            [
                {
                    "type": "PhoneNumber",
                    "id": "p1",
                    "attributes": {
                        "number": "555-0100",
                        "location": "Mobile",
                        "primary": True,
                        "carrier": "x",
                    },
                }
            ]
        ),
    )
    fake.route("GET", "/services/v2/people/2002/emails", error(404))
    fake.route("GET", "/people/v2/people/2002/phone_numbers", error(403))


ALEX = {
    "name": "Alex Example",
    "teams": ["Band", "Tech"],
    "emails": [{"address": "alex@example.com", "primary": True}],
    "phones": [{"number": "555-0100", "location": "Mobile", "primary": True}],
}
SAM = {"name": "Sam Sample", "teams": ["Tech"], "emails": None, "phones": None}


async def test_team_contacts_shows_what_planning_center_allows(fake):
    on_my_schedule(fake)
    team_members_route(
        fake,
        member("m1", "t1", "Alex Example", "2001"),
        member("m2", "t2", "Alex Example", "2001"),
        member("m3", "t2", "Sam Sample", "2002"),
        member("m4", "t1", "Me Myself", ME),
    )
    contact_routes(fake)
    _, out = await call("team_contacts", ARGS)
    assert out == {"people": [ALEX, SAM], "truncated": False}
    assert not any(f"/people/{ME}/" in p for p in fake.paths())


async def test_a_contact_lookup_failure_is_not_shown_as_hidden(fake):
    on_my_schedule(fake)
    team_members_route(fake, member("m1", "t1", "Alex Example", "2001"))
    contact_routes(fake)
    fake.route("GET", "/people/v2/people/2001/phone_numbers", error(500))
    is_error, text = await call("team_contacts", ARGS)
    assert is_error and "500" in text


async def test_contact_info_searches_my_upcoming_plans_by_name(fake):
    other = "/services/v2/service_types/20/plans/301"
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi(
            [
                schedule("1", "2099-01-01T09:00:00Z"),
                schedule("2", "2099-01-01T09:00:00Z"),
                schedule("3", "2099-01-08T09:00:00Z", plan="301"),
            ]
        ),
    )
    team_members_route(fake, member("m1", "t1", "Alex Example", "2001"))
    team_members_route(
        fake,
        member("m2", "t2", "Alex Example", "2001"),
        member("m3", "t2", "Sam Sample", "2002"),
        plan=other,
    )
    contact_routes(fake)
    _, out = await call("contact_info", {"name": "  example ALEX "})
    assert out == {
        "matches": [ALEX],
        "plans_searched": 2,
        "plans_not_visible": 0,
        "incomplete": False,
    }
    assert not any("/people/2002/" in p for p in fake.paths())
    _, out = await call("contact_info", {"name": "Sam"})
    assert out["matches"] == [SAM]

    # A plan whose team list you can't see makes the search incomplete, not silently empty.
    fake.route("GET", f"{other}/team_members", error(403))
    _, out = await call("contact_info", {"name": "Sam"})
    assert out == {"matches": [], "plans_searched": 1, "plans_not_visible": 1, "incomplete": True}


async def test_a_contact_list_cut_short_is_refused(fake):
    on_my_schedule(fake)
    team_members_route(fake, member("m1", "t1", "Alex Example", "2001"))
    contact_routes(fake)
    more = "https://api.planningcenteronline.com/services/v2/people/2001/emails?offset=1"
    fake.route("GET", "/services/v2/people/2001/emails", jsonapi([], next_url=more))
    is_error, text = await call("team_contacts", ARGS)
    assert is_error and "too long" in text


async def test_contact_info_asks_for_more_of_a_common_name(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/schedules",
        jsonapi([schedule("1", "2099-01-01T09:00:00Z")]),
    )
    team_members_route(
        fake, *(member(f"m{i}", "t1", f"Pat Example {i}", str(3000 + i)) for i in range(6))
    )
    _, out = await call("contact_info", {"name": "pat"})
    assert len(out["too_many_matches"]) == 6
    assert not any(p.endswith(("/emails", "/phone_numbers")) for p in fake.paths())


async def test_contact_info_needs_a_name(fake):
    is_error, text = await call("contact_info", {"name": "   "})
    assert is_error and "part of the person's name" in text
    assert fake.requests == []


def items_route(fake):
    fake.route(
        "GET",
        f"{PLAN}/items",
        jsonapi(
            [
                {
                    "type": "Item",
                    "id": "i2",
                    "attributes": {
                        "sequence": 2,
                        "title": "Example Song",
                        "item_type": "song",
                        "key_name": "D",
                        "length": 245,
                    },
                    "relationships": {
                        "song": {"data": {"type": "Song", "id": "s1"}},
                        "arrangement": {"data": {"type": "Arrangement", "id": "a1"}},
                        "key": {"data": {"type": "Key", "id": "k1"}},
                    },
                },
                {
                    "type": "Item",
                    "id": "i1",
                    "attributes": {"sequence": 1, "title": "Welcome", "item_type": "item"},
                    "relationships": {},
                },
            ],
            included=[
                {"type": "Song", "id": "s1", "attributes": {"author": "A. Writer"}},
                {"type": "Arrangement", "id": "a1", "attributes": {"name": "Default"}},
            ],
        ),
    )


async def test_service_order_is_sorted_and_described(fake):
    on_my_schedule(fake)
    fake.route(
        "GET",
        PLAN,
        jsonapi({"type": "Plan", "id": "300", "attributes": {"dates": "January 1, 2099"}}),
    )
    items_route(fake)
    _, out = await call("service_order", ARGS)
    assert [i["title"] for i in out["items"]] == ["Welcome", "Example Song"]
    assert out["items"][1] | {} == {
        "title": "Example Song",
        "type": "song",
        "when": None,
        "key": "D",
        "length": "4:05",
        "author": "A. Writer",
        "arrangement": "Default",
        "sequence": None,
        "description": None,
    }


def attachment(
    aid: str, kind: str, rid: str, *, downloadable=True, filename="Chart.pdf", filetype="pdf"
):
    return {
        "type": "Attachment",
        "id": aid,
        "attributes": {
            "filename": filename,
            "filetype": filetype,
            "downloadable": downloadable,
            "linked_url": "https://services.planningcenteronline.com/attachments/x",
            "updated_at": "2099-01-01T00:00:00Z",
        },
        "relationships": {"attachable": {"data": {"type": kind, "id": rid}}},
        "links": {"self": f"https://api.planningcenteronline.com{PLAN}/all_attachments/{aid}"},
    }


def attachments_route(fake, *atts):
    fake.route("GET", f"{PLAN}/all_attachments", jsonapi(list(atts)))


def plan_and_service_type_routes(fake):
    fake.route(
        "GET",
        PLAN,
        jsonapi({"type": "Plan", "id": "300", "attributes": {"sort_date": "2099-01-01T09:00:00Z"}}),
    )
    fake.route(
        "GET",
        "/services/v2/service_types/20",
        jsonapi({"type": "ServiceType", "id": "20", "attributes": {"name": "Sunday AM"}}),
    )


def open_route(fake, aid: str):
    """`/open` hands back a storage URL whose path is /b/<aid>."""
    fake.route(
        "POST",
        f"{PLAN}/all_attachments/{aid}/open",
        jsonapi(
            {
                "type": "AttachmentActivity",
                "id": "1",
                "attributes": {"attachment_url": f"https://s3.amazonaws.com/b/{aid}?sig=x"},
            }
        ),
    )


async def test_attachments_are_labelled_by_song(fake):
    on_my_schedule(fake)
    items_route(fake)
    attachments_route(
        fake,
        attachment("50", "Key", "k1"),
        attachment("51", "Item", "i1", downloadable=False, filetype="video"),
    )
    _, out = await call("list_attachments", ARGS)
    by_id = {a["id"]: a for a in out["attachments"]}
    assert by_id["50"]["for"] == "Example Song" and by_id["50"]["link"] is None
    assert by_id["51"]["for"] == "Welcome" and not by_id["51"]["downloadable"]
    assert by_id["51"]["link"]


async def test_download_refuses_an_attachment_from_another_plan(fake):
    on_my_schedule(fake)
    items_route(fake)
    attachments_route(fake, attachment("50", "Key", "k1"))
    is_error, text = await call("download_attachments", {**ARGS, "attachment_ids": ["999"]})
    assert is_error and "Not attachments of this plan: 999" in text
    assert not any(p.endswith("/open") for p in fake.paths())


async def test_download_saves_once_then_reuses(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PCO_DOWNLOAD_DIR", str(tmp_path))
    on_my_schedule(fake)
    items_route(fake)
    plan_and_service_type_routes(fake)
    attachments_route(
        fake,
        attachment("50", "Key", "k1"),
        attachment("52", "Song", "s1", filetype="audio", filename="a.mp3"),
    )
    open_route(fake, "50")
    fake.route("GET", "/b/50", httpx.Response(200, content=b"%PDF-synthetic"))

    _, first = await call("download_attachments", {**ARGS, "file_types": ["PDF"]})
    assert first["saved"] == ["Chart.pdf"] and first["failed"] == []
    assert (tmp_path / "2099-01-01 Sunday AM" / "Chart.pdf").read_bytes() == b"%PDF-synthetic"
    _, second = await call("download_attachments", {**ARGS, "file_types": ["pdf"]})
    assert second["saved"] == [] and second["already_current"] == ["Chart.pdf"]
    assert fake.paths("POST").count(f"{PLAN}/all_attachments/50/open") == 1


async def test_an_unfinished_attachment_list_is_refused(fake):
    on_my_schedule(fake)
    items_route(fake)
    more = f"https://api.planningcenteronline.com{PLAN}/all_attachments?offset=1"
    fake.route(
        "GET", f"{PLAN}/all_attachments", jsonapi([attachment("50", "Key", "k1")], next_url=more)
    )
    is_error, text = await call("list_attachments", ARGS)
    assert is_error and "too long" in text
    is_error, text = await call("download_attachments", ARGS)
    assert is_error and "too long" in text
    assert not any(p.endswith("/open") for p in fake.paths())


async def test_concurrent_downloads_of_one_plan_keep_both_files(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PCO_DOWNLOAD_DIR", str(tmp_path))
    on_my_schedule(fake)
    items_route(fake)
    plan_and_service_type_routes(fake)
    attachments_route(fake, attachment("50", "Key", "k1"), attachment("53", "Song", "s1"))

    async def slow_file(request):
        await asyncio.sleep(0.05)  # lets the other call run, as real network I/O would
        return httpx.Response(200, content=b"%PDF-" + request.url.path[-2:].encode())

    for aid in ("50", "53"):
        open_route(fake, aid)
        fake.route("GET", f"/b/{aid}", slow_file)

    results = await asyncio.gather(
        *(call("download_attachments", {**ARGS, "attachment_ids": [a]}) for a in ("50", "53"))
    )
    assert sorted(name for _, out in results for name in out["saved"]) == [
        "Chart (53).pdf",
        "Chart.pdf",
    ]
    folder = tmp_path / "2099-01-01 Sunday AM"
    contents = {p.read_bytes() for p in folder.glob("Chart*.pdf")}
    assert contents == {b"%PDF-50", b"%PDF-53"}
    manifest = json.loads((folder / ".pco-attachments.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"50", "53"}


async def test_one_failed_download_does_not_stop_or_pollute_the_rest(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PCO_DOWNLOAD_DIR", str(tmp_path))
    on_my_schedule(fake)
    items_route(fake)
    plan_and_service_type_routes(fake)
    attachments_route(
        fake,
        attachment("50", "Key", "k1"),
        attachment("53", "Song", "s1", filename="Lead.pdf"),
        attachment("54", "Song", "s1", filename="Vocal.pdf"),
    )
    fake.route("POST", f"{PLAN}/all_attachments/50/open", error(500))
    for aid in ("53", "54"):
        open_route(fake, aid)
    fake.route("GET", "/b/53", httpx.Response(200, content=b"%PDF-53"))
    fake.route("GET", "/b/54", httpx.Response(403))

    _, out = await call("download_attachments", ARGS)
    assert out["saved"] == ["Lead.pdf"]
    assert [f["id"] for f in out["failed"]] == ["50", "54"]
    folder = tmp_path / "2099-01-01 Sunday AM"
    assert sorted(p.name for p in folder.iterdir()) == [".pco-attachments.json", "Lead.pdf"]
    manifest = json.loads((folder / ".pco-attachments.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"53"}


async def test_errors_reach_the_client_with_their_text(fake):
    fake.route("GET", "/services/v2/me", error(401, "token revoked"))
    is_error, text = await call("list_blockouts")
    assert is_error and "token revoked" in text and "pco-services-mcp login" in text


SCHEDULES = f"/services/v2/people/{ME}/schedules"


def answerable(fake, status: str, sid: str = "7", declined_times: int = 0) -> dict:
    """A request Planning Center reads back directly; accept/decline change its status.

    declined_times > 0 is a split-time request with that many of its times declined.
    """
    held = {"status": status, "declined": declined_times}

    def answer(new: str):
        def handler(_request):
            held["status"] = new
            held["declined"] = 0
            return httpx.Response(204)

        return handler

    fake.route(
        "GET",
        f"{SCHEDULES}/{sid}",
        lambda r: jsonapi(schedule(sid, "2099-01-01T09:00:00Z", status=held["status"])),
    )
    fake.route(
        "GET",
        f"{SCHEDULES}/{sid}/declined_plan_times",
        lambda r: jsonapi([{"type": "PlanTime", "id": "40"}] * held["declined"]),
    )
    fake.route("POST", f"{SCHEDULES}/{sid}/accept", answer("C"))
    fake.route("POST", f"{SCHEDULES}/{sid}/decline", answer("D"))
    return held


async def test_my_schedule_gives_schedule_ids_and_can_include_declined(fake):
    fake.route("GET", SCHEDULES, jsonapi([schedule("5", "2099-01-01T09:00:00Z", status="D")]))
    _, out = await call("my_schedule")
    assert out["schedules"][0]["schedule_id"] == "5"
    await call("contact_info", {"name": "Example"})
    assert all("filter" not in r.url.params for r in fake.requests if r.url.path == SCHEDULES)
    _, out = await call("my_schedule", {"include_declined": True})
    last = [r for r in fake.requests if r.url.path == SCHEDULES][-1]
    assert last.url.params["filter"] == "with_declined"
    assert out["schedules"][0]["status"] == "declined"


@pytest.mark.parametrize("status", ["U", "D"])
async def test_accept_request_answers_or_changes_my_answer(fake, status):
    answerable(fake, status)
    is_error, out = await call("accept_request", {"schedule_id": "7"})
    assert not is_error, out
    assert fake.paths("POST") == [f"{SCHEDULES}/7/accept"]
    assert fake.bodies("POST", f"{SCHEDULES}/7/accept") == [{}]
    assert out["status"] == "confirmed" and out["verified"] is True


@pytest.mark.parametrize(("status", "reason"), [("U", "Away that week"), ("C", None)])
async def test_decline_request_sends_only_the_reason(fake, status, reason):
    answerable(fake, status)
    args = {"schedule_id": "7"} | ({"reason": reason} if reason else {})
    _, out = await call("decline_request", args)
    # Exactly this body: no replacement_id or replacement_confirmed, which would make
    # Planning Center contact someone else.
    assert fake.bodies("POST", f"{SCHEDULES}/7/decline") == [
        {
            "data": {
                "type": "ScheduleDecline",
                "attributes": {"reason": reason or ""},
                "relationships": None,
            }
        }
    ]
    assert out["status"] == "declined" and out["verified"] is True


@pytest.mark.parametrize(("tool", "status"), [("accept_request", "C"), ("decline_request", "D")])
async def test_an_answer_already_given_is_not_sent_again(fake, tool, status):
    answerable(fake, status)
    _, out = await call(tool, {"schedule_id": "7"})
    assert out["unchanged"] is True
    assert fake.paths("POST") == []


async def test_accept_request_accepts_the_declined_times_of_a_confirmed_request(fake):
    answerable(fake, "C", declined_times=1)
    _, out = await call("accept_request", {"schedule_id": "7"})
    assert fake.paths("POST") == [f"{SCHEDULES}/7/accept"]
    assert out["verified"] is True and "unchanged" not in out


async def test_an_accept_that_leaves_times_declined_is_not_verified(fake):
    answerable(fake, "C", declined_times=1)
    # Planning Center takes the accept but keeps the time declined.
    fake.route("POST", f"{SCHEDULES}/7/accept", httpx.Response(204))
    _, out = await call("accept_request", {"schedule_id": "7"})
    assert out["verified"] is False and "times still show as declined" in out["note"]


@pytest.mark.parametrize("tool", ["accept_request", "decline_request"])
async def test_answering_refuses_a_bad_id_before_any_request(fake, tool):
    is_error, text = await call(tool, {"schedule_id": "7/../8"})
    assert is_error and "numeric" in text
    assert fake.requests == []


async def test_answering_refuses_a_request_not_on_my_schedule(fake):
    fake.route("GET", f"{SCHEDULES}/7", error(404))
    fake.route("GET", SCHEDULES, jsonapi([schedule("8", "2099-01-01T09:00:00Z")]))
    is_error, text = await call("decline_request", {"schedule_id": "7"})
    assert is_error and "isn't on your schedule" in text
    assert fake.paths("POST") == []


async def test_a_declined_request_is_found_on_a_later_page(fake):
    held = {"status": "D"}
    more = f"https://api.planningcenteronline.com{SCHEDULES}?filter=with_declined&offset=1"

    def listing(r):
        assert r.url.params["filter"] == "with_declined"
        if "offset" in r.url.params:
            return jsonapi([schedule("7", "2099-01-01T09:00:00Z", status=held["status"])])
        return jsonapi([schedule("8", "2099-01-01T09:00:00Z")], next_url=more)

    def accept(_request):
        held["status"] = "C"
        return httpx.Response(204)

    fake.route("GET", f"{SCHEDULES}/7", error(404))
    fake.route("GET", f"{SCHEDULES}/7/declined_plan_times", jsonapi([]))
    fake.route("GET", SCHEDULES, listing)
    fake.route("POST", f"{SCHEDULES}/7/accept", accept)
    _, out = await call("accept_request", {"schedule_id": "7"})
    assert out["status"] == "confirmed" and out["verified"] is True


async def test_an_unfinished_search_for_the_request_refuses_without_writing(fake):
    more = f"https://api.planningcenteronline.com{SCHEDULES}?offset=1"
    fake.route("GET", f"{SCHEDULES}/7", error(404))
    fake.route("GET", SCHEDULES, jsonapi([schedule("8", "2099-01-01T09:00:00Z")], next_url=more))
    is_error, text = await call("accept_request", {"schedule_id": "7"})
    assert is_error and "too long to search" in text
    assert fake.paths("POST") == []


@pytest.mark.parametrize(
    ("reread", "status", "note"),
    [
        (error(404), None, "no longer appears"),
        (error(500), None, "returned 500"),
        (jsonapi(schedule("7", "2099-01-01T09:00:00Z", status="U")), "unconfirmed", "still shows"),
    ],
)
async def test_a_sent_answer_that_cannot_be_read_back_is_reported_not_raised(
    fake, reread, status, note
):
    reads = iter([jsonapi(schedule("7", "2099-01-01T09:00:00Z", status="U")), reread])
    fake.route("GET", f"{SCHEDULES}/7", lambda r: next(reads))
    fake.route("GET", SCHEDULES, jsonapi([]))
    fake.route("POST", f"{SCHEDULES}/7/decline", httpx.Response(204))
    is_error, out = await call("decline_request", {"schedule_id": "7"})
    assert not is_error, out
    assert out["verified"] is False and out["status"] == status
    assert note in out["note"] and out["dates"] == "2099-01-01"
    assert len(fake.paths("POST")) == 1


def unreachable(request):
    raise httpx.ConnectError("synthetic", request=request)


@pytest.mark.parametrize(
    ("reply", "uncertain"), [(unreachable, True), (error(502), True), (error(403), False)]
)
async def test_a_lost_answer_says_it_may_have_gone_through(fake, reply, uncertain):
    answerable(fake, "U")
    fake.route("POST", f"{SCHEDULES}/7/accept", reply)
    is_error, text = await call("accept_request", {"schedule_id": "7"})
    assert is_error and ("may or may not have reached" in text) is uncertain


def blockout(bid: str, starts: str, ends: str, **attrs) -> dict:
    return {
        "type": "Blockout",
        "id": bid,
        "attributes": {"starts_at": starts, "ends_at": ends, **attrs},
    }


async def test_list_blockouts_hides_finished_ones_but_keeps_live_series(fake):
    fake.route(
        "GET",
        f"/services/v2/people/{ME}/blockouts",
        jsonapi(
            [
                blockout("old", "2000-01-01T08:00:00Z", "2000-01-02T07:59:59Z"),
                blockout(
                    "series", "2000-01-01T08:00:00Z", "2000-01-02T07:59:59Z", repeat_period="weekly"
                ),
                blockout(
                    "ended",
                    "2000-01-01T08:00:00Z",
                    "2000-01-02T07:59:59Z",
                    repeat_period="weekly",
                    repeat_until="2000-06-01",
                ),
                blockout("soon", "2099-01-01T08:00:00Z", "2099-01-02T07:59:59Z", reason="Trip"),
            ]
        ),
    )
    _, out = await call("list_blockouts")
    assert [b["id"] for b in out["blockouts"]] == ["series", "soon"]
    assert out["blockouts"][1]["starts"] == "2099-01-01 00:00"
    _, everything = await call("list_blockouts", {"include_past": True})
    assert len(everything["blockouts"]) == 4


def test_a_blockout_that_ended_last_night_locally_is_finished():
    from zoneinfo import ZoneInfo

    # 23:59:59 on 1 January in Los Angeles is 07:59:59 on 2 January in UTC.
    b = blockout("x", "2099-01-01T08:00:00Z", "2099-01-02T07:59:59Z")
    zone = ZoneInfo("America/Los_Angeles")
    assert server._still_relevant(b, date(2099, 1, 1), zone)
    assert not server._still_relevant(b, date(2099, 1, 2), zone)


async def test_add_blockout_sends_the_built_payload_for_me(fake):
    fake.route(
        "POST",
        f"/services/v2/people/{ME}/blockouts",
        lambda r: jsonapi(
            {
                "type": "Blockout",
                "id": "77",
                "attributes": json.loads(r.content)["data"]["attributes"],
            }
        ),
    )
    is_error, out = await call(
        "add_blockout",
        {
            "start": "2099-03-01",
            "repeat": "monthly",
            "monthly_by": "week_1",
            "repeat_until": "2099-12-31",
            "reason": "Away",
        },
    )
    assert not is_error, out
    [body] = fake.bodies("POST", f"/services/v2/people/{ME}/blockouts")
    assert body == {
        "data": {
            "type": "Blockout",
            "attributes": {
                "starts_at": "2099-03-01T08:00:00Z",
                "ends_at": "2099-03-02T07:59:59Z",
                "reason": "Away",
                "repeat_period": "monthly",
                "repeat_frequency": "every_1",
                "repeat_interval": "week_of_month_1",
                "repeat_until": "2099-12-31",
            },
        }
    }
    assert out["created"]["id"] == "77"


async def test_time_zone_override(fake, monkeypatch):
    monkeypatch.setenv("PCO_TIME_ZONE", "America/New_York")
    fake.route(
        "POST",
        f"/services/v2/people/{ME}/blockouts",
        lambda r: jsonapi({"type": "Blockout", "id": "1", "attributes": {}}),
    )
    await call("add_blockout", {"start": "2099-01-10"})
    [body] = fake.bodies("POST", f"/services/v2/people/{ME}/blockouts")
    assert body["data"]["attributes"]["starts_at"] == "2099-01-10T05:00:00Z"
    assert "/services/v2" not in fake.paths("GET")


async def test_unusable_org_zone_refuses_without_writing(fake):
    fake.route(
        "GET",
        "/services/v2",
        jsonapi({"type": "Organization", "id": "1", "attributes": {"time_zone": "Mars/Base"}}),
    )
    is_error, text = await call("add_blockout", {"start": "2099-01-10"})
    assert is_error and "PCO_TIME_ZONE" in text
    assert fake.paths("POST") == []


async def test_remove_and_skip_always_target_me(fake):
    fake.route("DELETE", f"/services/v2/people/{ME}/blockouts/5", httpx.Response(204))
    fake.route(
        "POST",
        f"/services/v2/people/{ME}/blockouts/5/blockout_exceptions",
        jsonapi({"type": "BlockoutException", "id": "1"}),
    )
    assert (await call("remove_blockout", {"blockout_id": "5"}))[1] == {"removed": "5"}
    assert (await call("skip_blockout_date", {"blockout_id": "5", "date": "2099-01-03"}))[1][
        "available_on"
    ] == "2099-01-03"
    [body] = fake.bodies("POST", f"/services/v2/people/{ME}/blockouts/5/blockout_exceptions")
    assert body["data"]["attributes"] == {"date": "2099-01-03"}
    assert (await call("remove_blockout", {"blockout_id": "5/../../2"}))[0]


async def test_write_tools_are_annotated():
    async with Client(server.mcp) as c:
        tools = {t.name: t.annotations for t in (await c.list_tools()).tools}
    assert (
        tools["remove_blockout"].destructive_hint and tools["skip_blockout_date"].destructive_hint
    )
    assert tools["add_blockout"].read_only_hint is False
    assert tools["decline_request"].destructive_hint and tools["decline_request"].idempotent_hint
    accept = tools["accept_request"]
    assert accept.read_only_hint is False and accept.destructive_hint is False
    for name in ("my_schedule", "plan_team", "service_order", "list_attachments", "list_blockouts"):
        assert tools[name].read_only_hint
