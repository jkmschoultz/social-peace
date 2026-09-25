"""Posting schedule: approved renders go out `per_slot` at a time at the daily
`schedule.slots` (wall-clock times in `project.timezone`).

The slot times live only in config.yaml. A timer / cron runs `publish-due` every
few minutes; each tick asks "has a slot passed that we haven't served yet?" and,
if so, posts the head of the queue. The last served slot is kept in
logs/schedule_state.json, so ticks are idempotent and a missed slot (machine
asleep) fires late within `grace_minutes`, or is skipped rather than bursting.

Queue = approved renders not yet live anywhere, ordered by sidecar
`schedule.order` (set by reordering in the review UI), then oldest render first.
A render that went live on some platforms but failed on others is retried on
each following slot — alongside that slot's fresh post — until it succeeds or
runs out of `max_attempts` for that platform.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from social_peace import ledger
from social_peace.config import Config
from social_peace.publish.runner import (
    DONE_STATUSES,
    available_platforms,
    done_platforms,
    publish_sidecar,
)

log = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": True,
    "slots": ["08:00", "12:30", "19:00"],
    "per_slot": 1,
    "grace_minutes": 90,
    "max_attempts": 3,
    "low_queue_days": 1.0,
}


def settings(cfg: Config) -> dict:
    return {**_DEFAULTS, **(cfg.raw.get("schedule") or {})}


def tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.raw.get("project", {}).get("timezone") or "UTC")


def slot_times(cfg: Config) -> list[time]:
    out = []
    for s in settings(cfg)["slots"]:
        hh, mm = str(s).split(":")
        out.append(time(int(hh), int(mm)))
    return sorted(out)


def posts_per_day(cfg: Config) -> int:
    return len(slot_times(cfg)) * int(settings(cfg)["per_slot"])


def _slots_on(cfg: Config, day) -> list[datetime]:
    zone = tz(cfg)
    return [datetime.combine(day, t, tzinfo=zone) for t in slot_times(cfg)]


def latest_slot(cfg: Config, now: datetime) -> datetime | None:
    """The most recent slot at or before `now` (looks back one day)."""
    now = now.astimezone(tz(cfg))
    cands = [
        s for d in (now.date() - timedelta(days=1), now.date())
        for s in _slots_on(cfg, d) if s <= now
    ]
    return max(cands) if cands else None


def next_slots(cfg: Config, after: datetime, n: int) -> list[datetime]:
    """The next `n` slots strictly after `after`."""
    if n <= 0 or not slot_times(cfg):
        return []
    after = after.astimezone(tz(cfg))
    out: list[datetime] = []
    day = after.date()
    while len(out) < n:
        out += [s for s in _slots_on(cfg, day) if s > after][: n - len(out)]
        day += timedelta(days=1)
    return out


# ------------------------------------------------------------------------- state
def _state_path(cfg: Config) -> Path:
    return cfg.path("logs") / "schedule_state.json"


def _read_state(cfg: Config) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(cfg: Config, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _last_served(cfg: Config) -> datetime | None:
    raw = _read_state(cfg).get("last_slot")
    return datetime.fromisoformat(raw) if raw else None


@contextlib.contextmanager
def _lock(cfg: Config):
    """Cross-process lock so overlapping timer ticks (or a tick racing the review
    server) can't both claim the same slot. No-op where fcntl is unavailable."""
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    p = cfg.path("logs") / "schedule.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ------------------------------------------------------------------------- queue
def _load(side: Path) -> dict | None:
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write(side: Path, md: dict) -> None:
    side.write_text(json.dumps(md, indent=2, ensure_ascii=False), encoding="utf-8")


def _attempts(md: dict) -> dict:
    return (md.get("schedule") or {}).get("attempts") or {}


def _remaining(cfg: Config, md: dict) -> list[str]:
    """Target platforms this render still has to go out on, with attempts left."""
    done = done_platforms(md)
    cap = int(settings(cfg)["max_attempts"])
    att = _attempts(md)
    return [p for p in available_platforms(cfg) if p not in done and att.get(p, 0) < cap]


def _order_key(item: tuple[Path, dict]):
    side, md = item
    order = (md.get("schedule") or {}).get("order")
    return (order is None, order if order is not None else 0, side.stem)


def queue(cfg: Config) -> list[Path]:
    """Approved renders not yet live on any platform, in posting order. Renders
    that exhausted every platform's attempts drop out (they stay approved, with
    the errors in their sidecar)."""
    items = []
    for side in cfg.path("output").glob("*.json"):
        md = _load(side)
        if not md or md.get("review", {}).get("state") != "approved":
            continue
        if done_platforms(md) or not _remaining(cfg, md):
            continue
        if not side.with_suffix(".mp4").is_file():
            continue
        items.append((side, md))
    return [s for s, _ in sorted(items, key=_order_key)]


def partial(cfg: Config) -> list[tuple[Path, list[str]]]:
    """Renders live on >=1 platform but still owed to others (with attempts left)."""
    out = []
    for side in sorted(cfg.path("output").glob("*.json")):
        md = _load(side)
        if not md or md.get("review", {}).get("state") != "approved":
            continue
        if done_platforms(md) and (rest := _remaining(cfg, md)):
            out.append((side, rest))
    return out


def move(cfg: Config, stem: str, to: str) -> list[str]:
    """Reorder the queue: `to` is top / up / down / bottom. Rewrites every queued
    sidecar's schedule.order to 0..n-1 and returns the new order."""
    order = [s.stem for s in queue(cfg)]
    if stem not in order:
        raise ValueError(f"{stem} is not in the posting queue")
    i = order.index(stem)
    order.pop(i)
    j = {"top": 0, "up": max(0, i - 1), "down": i + 1, "bottom": len(order)}[to]
    order.insert(min(j, len(order)), stem)
    out = cfg.path("output")
    for n, s in enumerate(order):
        side = out / f"{s}.json"
        md = _load(side)
        if md is None:
            continue
        md.setdefault("schedule", {})["order"] = n
        _write(side, md)
    return order


def _record_attempts(side: Path, report: dict) -> None:
    md = _load(side)
    if md is None:
        return
    sch = md.setdefault("schedule", {})
    att = sch.setdefault("attempts", {})
    errs = sch.setdefault("errors", {})
    for r in report.get("results", []):
        if r["ok"] and r["status"] in DONE_STATUSES:
            errs.pop(r["platform"], None)
        elif not r["ok"]:
            att[r["platform"]] = att.get(r["platform"], 0) + 1
            errs[r["platform"]] = r.get("error")
    _write(side, md)


# ---------------------------------------------------------------------- publish
def _post(cfg: Config, side: Path, platforms: list[str], dry_run: bool) -> dict:
    rep = publish_sidecar(cfg, side, platforms=platforms, dry_run=dry_run)
    if not dry_run:
        _record_attempts(side, rep)
    return rep


def publish_next(cfg: Config, *, count: int | None = None, dry_run: bool = False) -> dict:
    """Retry partially-published renders, then post the next `count` (default
    per_slot) from the queue. Returns {"retries": [...], "posted": [...]}."""
    count = int(count if count is not None else settings(cfg)["per_slot"])
    retries = [_post(cfg, side, rest, dry_run) for side, rest in partial(cfg)]
    posted = []
    for side in queue(cfg)[:count]:
        md = _load(side) or {}
        posted.append(_post(cfg, side, _remaining(cfg, md), dry_run))
    return {"retries": retries, "posted": posted}


def publish_due(cfg: Config, *, now: datetime | None = None, dry_run: bool = False) -> dict:
    """One scheduler tick. Posts if a slot has passed since the last served one.
    Returns {"due": bool, "slot": iso|None, "reason": str, ...publish_next result}."""
    s = settings(cfg)
    now = (now or datetime.now(tz(cfg))).astimezone(tz(cfg))
    if not s["enabled"]:
        return {"due": False, "slot": None, "reason": "schedule disabled"}

    with _lock(cfg):
        slot = latest_slot(cfg, now)
        last = _last_served(cfg)
        if slot is None or (last is not None and last >= slot):
            nxt = next_slots(cfg, now, 1)
            return {"due": False, "slot": None,
                    "reason": f"next slot {nxt[0].isoformat() if nxt else '?'}"}

        if not dry_run:
            # claim the slot before posting: a crash mid-upload must not make
            # every following tick retry the whole slot
            _write_state(cfg, {**_read_state(cfg), "last_slot": slot.isoformat()})

        late = now - slot
        if late > timedelta(minutes=int(s["grace_minutes"])):
            log.warning("schedule: slot %s missed by %s, skipping", slot, late)
            if not dry_run:
                ledger.record(cfg.path("logs"), "schedule", slot=slot.isoformat(),
                              ok=False, error=f"missed by {late}")
            return {"due": False, "slot": slot.isoformat(), "reason": "missed (past grace)"}

        res = publish_next(cfg, dry_run=dry_run)
        if not res["posted"]:
            log.warning("schedule: slot %s — queue empty, nothing to post", slot)
        if not dry_run:
            ledger.record(
                cfg.path("logs"), "schedule", slot=slot.isoformat(),
                posted=[r["id"] for r in res["posted"]],
                retried=[r["id"] for r in res["retries"]],
                ok=bool(res["posted"]) and all(not r.get("error") for r in res["posted"]),
                error=None if res["posted"] else "queue empty",
            )
        return {"due": True, "slot": slot.isoformat(),
                "reason": "posted" if res["posted"] else "queue empty", **res}


# ----------------------------------------------------------------------- status
def upcoming_slots(cfg: Config, n: int, now: datetime | None = None) -> list[datetime]:
    """The next `n` slots that will actually fire — including a just-passed slot
    that hasn't been served yet and is still within grace."""
    s = settings(cfg)
    now = (now or datetime.now(tz(cfg))).astimezone(tz(cfg))
    slots: list[datetime] = []
    cur = latest_slot(cfg, now)
    last = _last_served(cfg)
    if (cur and (last is None or last < cur)
            and now - cur <= timedelta(minutes=int(s["grace_minutes"]))):
        slots.append(cur)
    return slots + next_slots(cfg, now, n - len(slots))


def status(cfg: Config, now: datetime | None = None) -> dict:
    """What the review UI / `schedule` command show: next slot, the queue with
    each render's slot, and whether the queue is running low."""
    s = settings(cfg)
    per = max(1, int(s["per_slot"]))
    q = queue(cfg)
    used = math.ceil(len(q) / per)
    slots = upcoming_slots(cfg, used + 1, now)
    plan = {side.stem: slots[i // per] for i, side in enumerate(q)} if slots else {}
    ppd = posts_per_day(cfg)
    days = len(q) / ppd if ppd else 0.0
    return {
        "enabled": bool(s["enabled"]),
        "timezone": str(tz(cfg)),
        "slots": [t.strftime("%H:%M") for t in slot_times(cfg)],
        "posts_per_day": ppd,
        "next_slot": slots[0] if slots else None,
        "queue": [side.stem for side in q],
        "plan": plan,
        "queue_days": days,
        "runs_out": slots[used] if slots else None,   # first slot with nothing queued
        "low": days < float(s["low_queue_days"]),
        "partial": [(side.stem, rest) for side, rest in partial(cfg)],
    }


def fmt_slot(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "—"
    now = (now or datetime.now(dt.tzinfo)).astimezone(dt.tzinfo)
    delta = (dt.date() - now.date()).days
    day = {0: "today", 1: "tomorrow"}.get(delta, dt.strftime("%a %d %b"))
    return f"{day} {dt.strftime('%H:%M')}"
