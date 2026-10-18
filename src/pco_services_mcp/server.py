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

INSTRUCTIONS = """\
Planning Center Services for the signed-in volunteer only. Start with my_schedule: it gives
the service_type_id and plan_id the other plan tools need. plan_team, service_order and the
attachment tools only work for plans on the user's own schedule. Blockout dates given as
YYYY-MM-DD cover the whole day in the church's time zone."""


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
        if "returned 404" in str(exc):
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
    """When you're next serving: date, service, team, position and response status.

    Returns service_type_id and plan_id for plan_team, service_order and attachments.
    """
    api = state.api()
    me = await api.me_id()
    cutoff = datetime.now(timezone.utc) - _STILL_CURRENT
    found: list[dict] = []
    incomplete = False
    try:
        async for page in api.pages(f"{SERVICES}/people/{me}/schedules", order="starts_at"):
            for sched in page.get("data") or []:
                attrs = sched["attributes"]
                if (_when(attrs.get("sort_date")) or cutoff) < cutoff:
                    continue
                found.append(
                    {
                        "dates": attrs.get("dates"),
                        "sort_date": attrs.get("sort_date"),
                        "service_type": attrs.get("service_type_name"),
                        "team": attrs.get("team_name"),
                        "position": attrs.get("team_position_name"),
                        "status": _STATUS.get(attrs.get("status"), attrs.get("status")),
                        "plan_visible": attrs.get("plan_visible_to_me"),
                        "service_type_id": related_id(sched, "service_type"),
                        "plan_id": related_id(sched, "plan"),
                    }
                )
            if len(found) >= limit:
                break
    except MorePagesError:
        incomplete = True
    found.sort(key=lambda s: s["sort_date"] or "")
    return {"schedules": found[:limit], "incomplete": incomplete}


@mcp.tool(annotations=READ)
async def plan_team(service_type_id: str, plan_id: str) -> dict:
    """Who is scheduled on one of your plans, grouped by team, with position and status."""
    path = await _my_plan(service_type_id, plan_id)
    doc = await state.api().get_all(f"{path}/team_members", include="team")
    teams: dict[str, list[dict]] = defaultdict(list)
    for member in doc.data:
        attrs = member["attributes"]
        team = doc.related(member, "team")
        teams[(team or {}).get("attributes", {}).get("name") or "Other"].append(
            {
                "name": attrs.get("name"),
                "position": attrs.get("team_position_name"),
                "status": _STATUS.get(attrs.get("status"), attrs.get("status")),
            }
        )
    return {"teams": dict(teams), "truncated": doc.incomplete}


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
