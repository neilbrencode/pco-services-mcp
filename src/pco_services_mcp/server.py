"""MCP tools for a volunteer's own Planning Center Services data."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from pco_services_mcp import __version__, credentials, downloads
from pco_services_mcp.blockouts import MonthlyBy, Repeat, build_attributes, resolve_zone
from pco_services_mcp.client import (
    PEOPLE,
    SERVICES,
    Document,
    MorePagesError,
    PcoClient,
    PcoError,
    check_id,
    related_id,
)

ENV_TIME_ZONE = "PCO_TIME_ZONE"
_STATUS = {"C": "confirmed", "U": "unconfirmed", "D": "declined"}
# A service that started a few hours ago is still "today's" service.
_STILL_CURRENT = timedelta(hours=12)
# Schedule entries without a date sort after the dated ones.
_UNDATED = datetime.max.replace(tzinfo=timezone.utc)

INSTRUCTIONS = """\
Planning Center Services for the signed-in volunteer only. Start with my_schedule: it gives
the service_type_id and plan_id the other plan tools need. plan_team, team_contacts,
service_order and the attachment tools only work for plans on the user's own schedule.
contact_info finds someone by name among the people on the user's upcoming plans. Blockout
dates given as YYYY-MM-DD cover the whole day in the church's time zone."""


def _client() -> PcoClient:
    try:
        return PcoClient(credentials.load())
    except credentials.CredentialsError as exc:
        raise PcoError(str(exc)) from None


@dataclass
class State:
    factory: Callable[[], PcoClient] = _client
    client: PcoClient | None = None
    zone_name: str | None = None
    service_type_names: dict[str, str] = field(default_factory=dict)

    def api(self) -> PcoClient:
        if self.client is None:
            self.client = self.factory()
        return self.client

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


state = State()


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        await state.close()


mcp = MCPServer(
    "pco-services-mcp",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
    version=__version__,
    log_level="WARNING",
)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# One download at a time per plan folder: the manifest is read, extended and written back.
_folder_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


async def _zone():
    if state.zone_name is None:
        state.zone_name = os.environ.get(ENV_TIME_ZONE) or (await state.api().get(SERVICES))[
            "data"
        ]["attributes"].get("time_zone")
    return resolve_zone(state.zone_name)


def _local(value: str | None, zone) -> str | None:
    moment = _when(value)
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M") if moment else None


def _wall_clock(value: str | None, zone) -> datetime | None:
    """A plan's sort_date: the church's local wall-clock time, despite its UTC "Z"."""
    moment = _when(value)
    return moment.replace(tzinfo=zone) if moment else None


def _plan_path(service_type_id: str, plan_id: str) -> str:
    st = check_id(service_type_id, "service_type_id")
    plan = check_id(plan_id, "plan_id")
    return f"{SERVICES}/service_types/{st}/plans/{plan}"


async def _my_plan(service_type_id: str, plan_id: str) -> str:
    """The plan's API path, once the user is confirmed to be scheduled on it."""
    path = _plan_path(service_type_id, plan_id)
    refusal = PcoError("That plan isn't on your schedule, so it's not available here.")
    try:
        mine = await state.api().get(f"{path}/my_schedules")
    except PcoError as exc:
        if exc.status == 404:
            raise refusal from None
        raise
    if not mine.get("data"):
        raise refusal
    return path


def _complete(doc: Document, what: str) -> Document:
    if doc.incomplete:
        raise PcoError(f"The {what} list is too long to read in full; nothing was changed.")
    return doc


# ── Schedule ────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def my_schedule(limit: int = 10) -> dict:
    """When you're next serving: date, service and rehearsal times, team, position and status.

    Times are local to the church, in the returned time_zone; times is null where Planning
    Center doesn't show you the plan. Returns service_type_id and plan_id for plan_team,
    service_order and attachments.
    """
    found, incomplete = await _upcoming(limit)
    zone = await _zone()
    # Several positions on one plan share its times, so fetch each plan's once.
    by_plan: dict[tuple, tuple[list | None, bool]] = {}
    for entry in found:
        plan = (entry["service_type_id"], entry["plan_id"])
        if plan not in by_plan:
            by_plan[plan] = await _plan_times(*plan, zone)
        entry["times"], cut_short = by_plan[plan]
        incomplete = incomplete or cut_short
    return {"time_zone": str(zone), "schedules": found, "incomplete": incomplete}


async def _plan_times(
    service_type_id: str | None, plan_id: str | None, zone
) -> tuple[list | None, bool]:
    """A plan's times in local time (None if not visible), and whether the list was cut short."""
    if not (service_type_id and plan_id):
        return None, False
    try:
        doc = await state.api().get_all(f"{_plan_path(service_type_id, plan_id)}/plan_times")
    except PcoError as exc:
        if exc.status in _HIDDEN:
            return None, False
        raise
    times = [
        {
            "type": t["attributes"].get("time_type"),
            "name": t["attributes"].get("name"),
            "starts": _local(t["attributes"].get("starts_at"), zone),
            "ends": _local(t["attributes"].get("ends_at"), zone),
        }
        for t in doc.data
    ]
    return sorted(times, key=lambda t: t["starts"] or ""), doc.incomplete


async def _upcoming(limit: int) -> tuple[list[dict], bool]:
    """Your next `limit` schedule entries, and whether the search stopped early."""
    api = state.api()
    me = await api.me_id()
    zone = await _zone()
    cutoff = datetime.now(timezone.utc) - _STILL_CURRENT
    found: list[tuple[datetime, dict]] = []
    incomplete = False
    try:
        async for page in api.pages(f"{SERVICES}/people/{me}/schedules", order="starts_at"):
            for sched in page.get("data") or []:
                attrs = sched["attributes"]
                when = _wall_clock(attrs.get("sort_date"), zone)
                if when and when < cutoff:
                    continue
                found.append(
                    (
                        when or _UNDATED,
                        {
                            "dates": attrs.get("dates"),
                            "service_type": attrs.get("service_type_name"),
                            "team": attrs.get("team_name"),
                            "position": attrs.get("team_position_name"),
                            "status": _STATUS.get(attrs.get("status"), attrs.get("status")),
                            "plan_visible": attrs.get("plan_visible_to_me"),
                            "service_type_id": related_id(sched, "service_type"),
                            "plan_id": related_id(sched, "plan"),
                        },
                    )
                )
            if len(found) >= limit:
                break
    except MorePagesError:
        incomplete = True
    found.sort(key=lambda f: f[0])
    return [entry for _, entry in found[:limit]], incomplete


def _team_name(doc: Document, member: dict) -> str:
    return ((doc.related(member, "team") or {}).get("attributes") or {}).get("name") or "Other"


@mcp.tool(annotations=READ)
async def plan_team(service_type_id: str, plan_id: str) -> dict:
    """Who is scheduled on one of your plans, grouped by team, with position and status."""
    path = await _my_plan(service_type_id, plan_id)
    doc = await state.api().get_all(f"{path}/team_members", include="team")
    teams: dict[str, list[dict]] = defaultdict(list)
    for member in doc.data:
        attrs = member["attributes"]
        teams[_team_name(doc, member)].append(
            {
                "name": attrs.get("name"),
                "position": attrs.get("team_position_name"),
                "status": _STATUS.get(attrs.get("status"), attrs.get("status")),
            }
        )
    return {"teams": dict(teams), "truncated": doc.incomplete}


# ── Contacts ────────────────────────────────────────────

# Planning Center's answer when you may not see someone's details: 404 from Services, 403 from
# People. Shown as "not visible", never worked around.
_HIDDEN = (403, 404)
_PARALLEL_LOOKUPS = 4
_MAX_MATCHES = 5
_SEARCH_SCHEDULES = 10


def _add_people(people: dict[str, dict], doc: Document, me: str) -> None:
    """Merge a plan's team members into `people`: person id → name and teams."""
    for member in doc.data:
        pid = related_id(member, "person")
        if not pid or pid == me:
            continue
        entry = people.setdefault(
            check_id(pid, "person id"), {"name": member["attributes"].get("name"), "teams": []}
        )
        if (team := _team_name(doc, member)) not in entry["teams"]:
            entry["teams"].append(team)


async def _visible(path: str, pick: Callable[[dict], dict]) -> list[dict] | None:
    """The records at `path`, or None when Planning Center won't show them to you."""
    try:
        doc = await state.api().get_all(path)
    except PcoError as exc:
        if exc.status in _HIDDEN:
            return None
        raise
    if doc.incomplete:
        raise PcoError("A contact list is too long to read in full.")
    return [pick(r.get("attributes") or {}) for r in doc.data]


def _email(a: dict) -> dict:
    return {"address": a.get("address"), "primary": bool(a.get("primary"))}


def _phone(a: dict) -> dict:
    return {
        "number": a.get("number"),
        "location": a.get("location"),
        "primary": bool(a.get("primary")),
    }


async def _contacts(people: dict[str, dict]) -> list[dict]:
    """Each person with `emails` and `phones`; either is None where it isn't visible to you."""
    gate = asyncio.Semaphore(_PARALLEL_LOOKUPS)

    async def one(pid: str, person: dict) -> dict:
        async with gate:
            emails = await _visible(f"{SERVICES}/people/{pid}/emails", _email)
            phones = await _visible(f"{PEOPLE}/people/{pid}/phone_numbers", _phone)
        return {**person, "emails": emails, "phones": phones}

    found = await asyncio.gather(*(one(pid, p) for pid, p in people.items()))
    return sorted(found, key=lambda p: (p["name"] or "").lower())


@mcp.tool(annotations=READ)
async def team_contacts(service_type_id: str, plan_id: str) -> dict:
    """Email addresses and phone numbers for the people on one of your plans.

    emails or phones is null where Planning Center doesn't let you see that person's details.
    """
    path = await _my_plan(service_type_id, plan_id)
    api = state.api()
    doc = await api.get_all(f"{path}/team_members", include="team")
    people: dict[str, dict] = {}
    _add_people(people, doc, await api.me_id())
    return {"people": await _contacts(people), "truncated": doc.incomplete}


@mcp.tool(annotations=READ)
async def contact_info(name: str) -> dict:
    """Email and phone for someone on your upcoming plans, found by any part of their name.

    Searches the people scheduled on the plans of your next 10 schedule entries. emails or
    phones is null where Planning Center doesn't let you see that person's details.
    """
    words = name.lower().split()
    if not words:
        raise PcoError("Give at least part of the person's name.")
    api = state.api()
    me = await api.me_id()
    schedules, incomplete = await _upcoming(_SEARCH_SCHEDULES)
    plans = dict.fromkeys(
        (s["service_type_id"], s["plan_id"])
        for s in schedules
        if s["service_type_id"] and s["plan_id"]
    )
    people: dict[str, dict] = {}
    not_visible = 0
    for st, plan in plans:
        try:
            doc = await api.get_all(f"{_plan_path(st, plan)}/team_members", include="team")
        except PcoError as exc:
            if exc.status in _HIDDEN:
                not_visible += 1
                continue
            raise
        incomplete = incomplete or doc.incomplete
        _add_people(people, doc, me)
    matches = {
        pid: p for pid, p in people.items() if all(w in (p["name"] or "").lower() for w in words)
    }
    if len(matches) > _MAX_MATCHES:
        return {
            "too_many_matches": sorted(p["name"] or "" for p in matches.values()),
            "hint": "Give more of the name.",
        }
    return {
        "matches": await _contacts(matches),
        "plans_searched": len(plans) - not_visible,
        # Plans on your schedule whose team list Planning Center won't show you.
        "plans_not_visible": not_visible,
        "incomplete": incomplete or bool(not_visible),
    }


# ── Service order and attachments ───────────────────────


def _length(seconds: int | None) -> str | None:
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else None


async def _items(path: str) -> Document:
    doc = await state.api().get_all(f"{path}/items", include="song,arrangement,key")
    doc.data.sort(key=lambda i: i["attributes"].get("sequence") or 0)
    return doc


@mcp.tool(annotations=READ)
async def service_order(service_type_id: str, plan_id: str) -> dict:
    """The order of service for one of your plans: songs, keys, arrangements and lengths."""
    path = await _my_plan(service_type_id, plan_id)
    plan = (await state.api().get(path))["data"]["attributes"]
    doc = await _items(path)
    items = []
    for item in doc.data:
        attrs = item["attributes"]
        song = (doc.related(item, "song") or {}).get("attributes", {})
        arrangement = (doc.related(item, "arrangement") or {}).get("attributes", {})
        items.append(
            {
                "title": attrs.get("title"),
                "type": attrs.get("item_type"),
                "when": attrs.get("service_position"),
                "key": attrs.get("key_name"),
                "length": _length(attrs.get("length")),
                "author": song.get("author"),
                "arrangement": arrangement.get("name"),
                "sequence": attrs.get("custom_arrangement_sequence_short") or None,
                "description": attrs.get("description") or None,
            }
        )
    return {
        "dates": plan.get("dates"),
        "title": plan.get("title"),
        "series": plan.get("series_title"),
        "items": items,
        "truncated": doc.incomplete,
    }


def _labels(items: Document) -> dict[tuple[str, str], str]:
    """Map each thing an attachment can hang off to the plan item it belongs to."""
    labels: dict[tuple[str, str], str] = {}
    for item in items.data:
        title = item["attributes"].get("title") or "Untitled"
        labels[("Item", item["id"])] = title
        for rel, kind in (("song", "Song"), ("arrangement", "Arrangement"), ("key", "Key")):
            if rid := related_id(item, rel):
                labels.setdefault((kind, rid), title)
    return labels


def _attachment(att: dict, labels: dict) -> dict:
    attrs = att["attributes"]
    ref = (att.get("relationships", {}).get("attachable") or {}).get("data") or {}
    return {
        "id": att["id"],
        "filename": attrs.get("filename") or attrs.get("display_name"),
        "filetype": attrs.get("filetype"),
        "for": labels.get((ref.get("type"), ref.get("id"))),
        "attached_to": ref.get("type"),
        "downloadable": bool(attrs.get("downloadable")),
        # Every attachment has a linked_url (its Planning Center page); it is only worth
        # showing for files that can't be downloaded, such as YouTube links.
        "link": None if attrs.get("downloadable") else attrs.get("linked_url"),
        "size": attrs.get("file_size"),
    }


async def _attachments(path: str) -> tuple[Document, dict]:
    api = state.api()
    atts = _complete(await api.get_all(f"{path}/all_attachments"), "attachment")
    return atts, _labels(await _items(path))


@mcp.tool(annotations=READ)
async def list_attachments(service_type_id: str, plan_id: str) -> dict:
    """Files attached to one of your plans (charts, lead sheets, audio), labelled by song."""
    path = await _my_plan(service_type_id, plan_id)
    atts, labels = await _attachments(path)
    return {"attachments": [_attachment(a, labels) for a in atts.data]}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
)
async def download_attachments(
    service_type_id: str,
    plan_id: str,
    attachment_ids: list[str] | None = None,
    file_types: list[str] | None = None,
) -> dict:
    """Download attachments for one of your plans into the configured download folder.

    attachment_ids (from list_attachments) and file_types (e.g. ["pdf"]) narrow the set; with
    neither, every downloadable file is fetched. Files already current are skipped.
    """
    path = await _my_plan(service_type_id, plan_id)
    atts, labels = await _attachments(path)
    by_id = {a["id"]: a for a in atts.data}
    if attachment_ids:
        wanted = [check_id(i, "attachment id") for i in attachment_ids]
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            raise PcoError(f"Not attachments of this plan: {', '.join(unknown)}.")
        chosen = [by_id[i] for i in wanted]
    else:
        chosen = atts.data
    if file_types:
        types = {t.lower().lstrip(".") for t in file_types}
        chosen = [a for a in chosen if (a["attributes"].get("filetype") or "").lower() in types]

    folder = await _plan_folder(path, service_type_id)
    async with _folder_locks[str(folder)]:
        result = await _download_into(folder, chosen, labels)
    return {"folder": str(folder), **result}


async def _download_into(folder, chosen: list[dict], labels: dict) -> dict[str, list]:
    manifest = downloads.Manifest.load(folder)
    api = state.api()
    result: dict[str, list] = {
        "saved": [],
        "already_current": [],
        "not_downloadable": [],
        "failed": [],
    }
    for att in chosen:
        info = _attachment(att, labels)
        if not info["downloadable"]:
            result["not_downloadable"].append({k: info[k] for k in ("id", "filename", "link")})
            continue
        try:
            dest, current = manifest.target(
                att["id"], info["filename"] or att["id"], att["attributes"].get("updated_at")
            )
            if current:
                result["already_current"].append(dest.name)
                continue
            opened = await api.request("POST", att["links"]["self"] + "/open")
            size = await api.download(opened["data"]["attributes"]["attachment_url"], dest)
            manifest.record(att["id"], dest, att["attributes"].get("updated_at"), size)
            result["saved"].append(dest.name)
        except PcoError as exc:
            result["failed"].append(
                {"id": att["id"], "filename": info["filename"], "error": str(exc)}
            )
    return result


async def _plan_folder(path: str, service_type_id: str):
    api = state.api()
    plan = (await api.get(path))["data"]["attributes"]
    st = check_id(service_type_id, "service_type_id")
    if st not in state.service_type_names:
        data = (await api.get(f"{SERVICES}/service_types/{st}"))["data"]["attributes"]
        state.service_type_names[st] = data.get("name") or "Service"
    # sort_date is local wall-clock time, so its date part is the local date.
    day = (plan.get("sort_date") or "")[:10] or "undated"
    base = downloads.root()
    folder = downloads.confined(base, downloads.safe_name(f"{day} {state.service_type_names[st]}"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── Blockouts ───────────────────────────────────────────


def _blockout(b: dict, zone) -> dict:
    attrs = b["attributes"]
    period = attrs.get("repeat_period")
    repeating = period not in (None, "", "no_repeat")
    return {
        "id": b["id"],
        "reason": attrs.get("reason") or None,
        "starts": _local(attrs.get("starts_at"), zone),
        "ends": _local(attrs.get("ends_at"), zone),
        "repeat": (
            {
                "period": period,
                "every": attrs.get("repeat_frequency"),
                "monthly_by": attrs.get("repeat_interval"),
                "until": attrs.get("repeat_until"),
            }
            if repeating
            else None
        ),
    }


def _still_relevant(b: dict, today: date, zone) -> bool:
    attrs = b["attributes"]
    ends = _when(attrs.get("ends_at"))
    if ends and ends.astimezone(zone).date() >= today:
        return True
    if attrs.get("repeat_period") in (None, "", "no_repeat"):
        return False
    until = attrs.get("repeat_until")
    return not until or date.fromisoformat(until) >= today


@mcp.tool(annotations=READ)
async def list_blockouts(include_past: bool = False) -> dict:
    """Your blockout dates (times shown in the church's time zone)."""
    api = state.api()
    me = await api.me_id()
    zone = await _zone()
    doc = await api.get_all(f"{SERVICES}/people/{me}/blockouts")
    today = datetime.now(zone).date()
    rows = [b for b in doc.data if include_past or _still_relevant(b, today, zone)]
    rows.sort(key=lambda b: b["attributes"].get("starts_at") or "")
    return {"blockouts": [_blockout(b, zone) for b in rows], "truncated": doc.incomplete}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
)
async def add_blockout(
    start: str,
    end: str | None = None,
    reason: str | None = None,
    repeat: Repeat = "none",
    every: int = 1,
    monthly_by: MonthlyBy | None = None,
    repeat_until: str | None = None,
) -> dict:
    """Add a blockout so you aren't scheduled.

    start/end: YYYY-MM-DD for whole days (end defaults to start), or ISO date-times (end
    required; no offset means church time). repeat with every=N repeats every N periods;
    monthly_by picks the same date or the Nth week of the month. repeat_until is a date.
    """
    api = state.api()
    me = await api.me_id()
    zone = await _zone()
    attrs = build_attributes(
        start=start,
        end=end,
        reason=reason,
        repeat=repeat,
        every=every,
        monthly_by=monthly_by,
        repeat_until=repeat_until,
        zone=zone,
    )
    created = await api.request(
        "POST",
        f"{SERVICES}/people/{me}/blockouts",
        json={"data": {"type": "Blockout", "attributes": attrs}},
    )
    return {"created": _blockout(created["data"], zone)}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
    )
)
async def remove_blockout(blockout_id: str) -> dict:
    """Delete one of your blockouts. For a repeating blockout this deletes the WHOLE series;
    use skip_blockout_date to drop a single date instead."""
    api = state.api()
    me = await api.me_id()
    bid = check_id(blockout_id, "blockout_id")
    await api.request("DELETE", f"{SERVICES}/people/{me}/blockouts/{bid}")
    return {"removed": bid}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def skip_blockout_date(blockout_id: str, date: str) -> dict:
    """Make you available on one date of a repeating blockout, keeping the rest of the series.

    This can't be undone through the API (only in Planning Center itself, or by deleting the
    series).
    """
    api = state.api()
    me = await api.me_id()
    bid = check_id(blockout_id, "blockout_id")
    try:
        day = datetime.strptime(date.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise PcoError("date must be YYYY-MM-DD.") from None
    await api.request(
        "POST",
        f"{SERVICES}/people/{me}/blockouts/{bid}/blockout_exceptions",
        json={"data": {"type": "BlockoutException", "attributes": {"date": day}}},
    )
    return {"blockout_id": bid, "available_on": day}


def main() -> None:
    mcp.run(transport="stdio")
