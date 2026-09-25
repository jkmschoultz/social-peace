"""What was used when — so selection can rotate through overlay lines, and send
an overused line / clip / bed "to the back of the queue" when a render is
rejected for it.

A render "touches" its overlay line, clips and beds at its created_utc (rejected
renders don't count — they never went out). Demoting an item stamps it with
now, and it stays *behind* until every other item in its pool has been touched
since: a real back-of-the-queue, rather than a permanent ban.

"Doesn't fit" rejections record the audio <-> clips pairing so selection stops
putting that bed on those clips. Stored in logs/demoted.json.
"""
from __future__ import annotations

import json
import random
import threading
from datetime import datetime, timezone
from pathlib import Path

from social_peace.config import Config

KINDS = ("text", "video", "audio")
_LOCK = threading.Lock()


def _store_path(cfg: Config) -> Path:
    return cfg.path("logs") / "demoted.json"


def _read(cfg: Config) -> dict:
    try:
        data = json.loads(_store_path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, KeyError):
        data = {}
    for k in KINDS:
        data.setdefault(k, {})
    data.setdefault("mismatch", [])
    return data


def _write(cfg: Config, data: dict) -> None:
    p = _store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def demote(cfg: Config, kind: str, names: list[str]) -> None:
    """Send `names` (overlay lines / clip / bed filenames) to the back of their queue."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    with _LOCK:
        data = _read(cfg)
        for n in names:
            data[kind][n] = _now()
        _write(cfg, data)


def record_mismatch(cfg: Config, audio: list[str], video: list[str]) -> None:
    """Remember that these beds don't suit these clips."""
    with _LOCK:
        data = _read(cfg)
        for a in audio:
            data["mismatch"].append({"audio": a, "video": list(video), "utc": _now()})
        _write(cfg, data)


class Recency:
    """A snapshot of touches + demotions, loaded once per selection."""

    def __init__(self, touches: dict, demoted: dict, mismatch: list):
        self.touches = touches        # kind -> {name: last iso ts}
        self.demoted = demoted        # kind -> {name: demote iso ts}
        self.mismatch = mismatch

    @classmethod
    def load(cls, cfg: Config) -> "Recency":
        touches: dict = {k: {} for k in KINDS}
        try:
            out, store = cfg.path("output"), _read(cfg)
        except KeyError:              # a test config without output/logs paths
            return cls(touches, {k: {} for k in KINDS}, [])
        for side in out.glob("*.json") if out.is_dir() else []:
            try:
                md = json.loads(side.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if md.get("review", {}).get("state") == "rejected":
                continue
            ts = md.get("created_utc") or ""
            src = md.get("sources", {})
            names = {"text": [md.get("overlay_text")], "video": src.get("video", []),
                     "audio": src.get("audio", [])}
            for k, ns in names.items():
                for n in ns:
                    if n and ts > touches[k].get(n, ""):
                        touches[k][n] = ts
        demoted = {k: store[k] for k in KINDS}
        for k in KINDS:
            for n, ts in demoted[k].items():
                if ts > touches[k].get(n, ""):
                    touches[k][n] = ts
        return cls(touches, demoted, store["mismatch"])

    def behind(self, kind: str, pool: list[str]) -> set[str]:
        """Demoted items still waiting: some other pool item hasn't been touched
        since the demotion."""
        out = set()
        t = self.touches[kind]
        for name, ts in self.demoted[kind].items():
            if name in pool and any(t.get(o, "") < ts for o in pool if o != name):
                out.add(name)
        return out

    def pick_text(self, rng: random.Random, pool: list[str]) -> str:
        """Least-recently-used overlay line: a random never-used line if any,
        else one from the oldest quarter by last use. Demoted lines are recent
        by construction, so they wait at the back."""
        t = self.touches["text"]
        fresh = [s for s in pool if s not in t]
        if fresh:
            return rng.choice(fresh)
        by_age = sorted(pool, key=lambda s: t[s])
        return rng.choice(by_age[: max(1, len(by_age) // 4)])

    def mismatched_clips(self, audio: list[str]) -> set[str]:
        return {v for m in self.mismatch if m["audio"] in audio for v in m["video"]}

    def mismatched_audio(self, video: list[str]) -> set[str]:
        vs = set(video)
        return {m["audio"] for m in self.mismatch if vs & set(m["video"])}
