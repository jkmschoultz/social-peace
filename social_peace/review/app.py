"""Flask app backing `social-peace review`.

The per-video JSON sidecar in output/ is the only store. Bind to localhost; there
is no auth. Approve/Reject writes review.state (and any caption edits) back into
the sidecar via pipeline.metadata.set_review, and appends a `review` ledger event.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from social_peace import ledger
from social_peace.config import Config
from social_peace.fetch.common import remove_manifest_entry, update_manifest_entry
from social_peace.pipeline.metadata import REVIEW_STATES, set_review
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media
from social_peace.pipeline.variant import CHANGEABLE, REJECT_REASONS
from social_peace.publish import schedule
from social_peace.publish.runner import (
    DONE_STATUSES,
    available_platforms,
    is_published,
    publish_all_approved,
    publish_sidecar,
)

log = logging.getLogger(__name__)

_STEM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FILTERS = ("pending", "approved", "published", "rejected", "all")
_TABS = ("pending", "approved", "published", "rejected")

# in-memory registry for slow ops (generate / variant / fetch / publish). Shared
# across every page + browser tab in this server process, so a status survives
# navigating around. Finished jobs linger _JOB_TTL seconds for late page loads.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 90.0


def _prune_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        for jid in [
            j for j, v in _JOBS.items()
            if v.get("finished_at") and now - v["finished_at"] > _JOB_TTL
        ]:
            _JOBS.pop(jid, None)


def _start_job(kind: str, work, label: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "kind": kind, "label": label or kind,
            "state": "running", "started_at": time.time(),
        }

    def _waiting(msg):
        with _JOBS_LOCK:
            _JOBS[job_id]["waiting"] = msg

    def _run():
        from social_peace.pipeline.throttle import set_status_hook
        set_status_hook(_waiting)       # render_slot reports "queued" / "waiting: CPU 96% busy"
        try:
            res = work()
            upd = {"state": "done", "result": res}
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s (%s) failed", job_id, kind)
            upd = {"state": "error", "error": str(exc)}
        upd["finished_at"] = time.time()
        upd["waiting"] = None
        with _JOBS_LOCK:
            _JOBS[job_id].update(upd)

    threading.Thread(target=_run, daemon=True).start()
    return job_id


# Browsers open ~6 connections per site. A <video> asks for "bytes=0-", and
# answering that with the whole file (tens of MB) pins a connection open while
# the browser has stopped reading — a grid of tiles then exhausts the pool, and
# further videos and the page's own API calls hang (black tiles, dead buttons).
# Cap each range response; browsers just ask for the next piece as they play.
_MEDIA_CHUNK = 2 * 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def _send_media(folder: Path, name: str):
    m = _RANGE_RE.match(request.headers.get("Range", "").strip())
    if m:
        start, end = int(m.group(1)), m.group(2)
        if not end or int(end) - start + 1 > _MEDIA_CHUNK:
            request.environ["HTTP_RANGE"] = f"bytes={start}-{start + _MEDIA_CHUNK - 1}"
    return send_from_directory(folder, name, conditional=True)


_THUMB_LOCKS: dict[str, threading.Lock] = {}


def _thumbnail(output_dir: Path, stem: str) -> Path | None:
    """<stem>.thumb.jpg — one small frame for the tile's poster, made on first
    request (and again if the mp4 is newer)."""
    mp4, jpg = output_dir / f"{stem}.mp4", output_dir / f"{stem}.thumb.jpg"
    if not mp4.is_file():
        return None
    with _JOBS_LOCK:
        lock = _THUMB_LOCKS.setdefault(stem, threading.Lock())
    with lock:
        if jpg.is_file() and jpg.stat().st_mtime >= mp4.stat().st_mtime:
            return jpg
        import subprocess
        from social_peace.pipeline.ffmpeg_utils import _resolve_bin
        tmp = jpg.with_suffix(".tmp.jpg")
        proc = subprocess.run(
            [_resolve_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "1", "-i", str(mp4), "-frames:v", "1",
             "-vf", "scale=360:-2", "-q:v", "5", str(tmp)],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(jpg)
        return jpg


def _safe_stem(stem: str) -> str:
    if not _STEM_RE.match(stem or "") or "/" in stem or ".." in stem:
        abort(404)
    return stem


_DUR_CACHE: dict[str, tuple[float, float]] = {}


def _duration(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
        hit = _DUR_CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        from social_peace.pipeline.ffmpeg_utils import ffprobe_duration
        d = ffprobe_duration(path)
        _DUR_CACHE[str(path)] = (mtime, d)
        return d
    except Exception:  # noqa: BLE001
        return None


def _fmt_dur(sec: float | None) -> str | None:
    if not sec:
        return None
    sec = round(sec)
    return f"{sec}s" if sec < 60 else f"{sec // 60}:{sec % 60:02d}"


def _warm_durations(paths: list[Path]) -> None:
    """Probe uncached durations in parallel so the assets page renders fast."""
    cold = [p for p in paths if str(p) not in _DUR_CACHE
            or _DUR_CACHE[str(p)][0] != p.stat().st_mtime]
    if not cold:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(cold))) as ex:
        list(ex.map(_duration, cold))


_ASSET_SORTS = ("favourite", "name", "size", "duration", "newest")


def _sort_assets(rows: list[dict], sort: str) -> list[dict]:
    if sort == "name":
        return sorted(rows, key=lambda r: (r["label"] or r["name"]).lower())
    if sort == "size":
        return sorted(rows, key=lambda r: -r["size"])
    if sort == "duration":
        return sorted(rows, key=lambda r: -(r["dur_s"] or 0))
    if sort == "newest":
        return sorted(rows, key=lambda r: -r["mtime"])
    return sorted(rows, key=lambda r: (not r["favourite"], (r["label"] or r["name"]).lower()))


def _asset_rows(
    cfg: Config, kind: str, usage: dict[str, set], filt: str = "all", sort: str = "favourite"
) -> list[dict]:
    folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
    exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
    man = cfg.manifest.get(kind) or {}
    all_paths = _list_media(folder, exts)
    _warm_durations(all_paths)
    rows = []
    for p in all_paths:
        states = usage.get(p.name, set())
        m = man.get(p.name) or {}
        fav = bool(m.get("favourite"))
        if not _asset_matches(filt, states, fav):
            continue
        st = p.stat()
        rows.append({
            "name": p.name,
            "label": m.get("label") or None,
            "favourite": fav,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "dur_s": _duration(p),
            "duration": _fmt_dur(_duration(p)),
            "source": m.get("source"),
            "license": m.get("license"),
            "url": m.get("url"),
            "tags": m.get("tags") or [],
            "used": bool(states),
            "states": sorted(states),
        })
    return _sort_assets(rows, sort)


def _load_all(cfg: Config, output_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for side in sorted(output_dir.glob("*.json"), reverse=True):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unreadable sidecar %s: %s", side.name, exc)
            continue
        md["_has_video"] = side.with_suffix(".mp4").is_file()
        md.setdefault("review", {"state": "pending", "decided_utc": None, "note": ""})
        md.setdefault("status", {})
        md.setdefault("published", {})
        # re-resolve source links from the current manifest — sidecars freeze these
        # at build time, so a render made before a manifest URL was added would
        # otherwise show "(no link)" forever.
        frozen = md.get("source_urls") or {}
        md["source_urls"] = {
            kind: {
                n: cfg.source_for(kind, n) or (frozen.get(kind) or {}).get(n)
                for n in md.get("sources", {}).get(kind, [])
            }
            for kind in ("video", "audio")
        }
        md["_state"] = _effective_state(md)
        plats = md.setdefault("platforms", {})
        plats.setdefault("youtube", {"title": "", "description": ""})
        plats.setdefault("tiktok", {"caption": ""})
        plats.setdefault("instagram", {"caption": ""})
        rows.append(md)
    return rows


def _effective_state(md: dict) -> str:
    """review.state, except an approved render that has gone live on >=1 platform
    reports as 'published' so it moves to its own tab."""
    state = md.get("review", {}).get("state", "pending")
    if state == "approved" and is_published(md):
        return "published"
    return state


def _filtered(rows: list[dict], filt: str) -> list[dict]:
    if filt == "all":
        return rows
    return [r for r in rows if _effective_state(r) == filt]


_SORTS = ("queue", "newest", "oldest", "seed", "template", "status")
_STATE_ORDER = {"pending": 0, "approved": 1, "published": 2, "rejected": 3}


def _sorted(rows: list[dict], sort: str, queue: list[str] | None = None) -> list[dict]:
    if sort == "queue":
        pos = {stem: i for i, stem in enumerate(queue or [])}
        return sorted(rows, key=lambda r: (pos.get(r["id"], len(pos)), r["id"]))
    if sort == "oldest":
        return sorted(rows, key=lambda r: r["id"])
    if sort == "seed":
        return sorted(rows, key=lambda r: int(r.get("seed") or 0))
    if sort == "template":
        return sorted(rows, key=lambda r: (r.get("template") or "", r["id"]))
    if sort == "status":
        return sorted(rows, key=lambda r: (_STATE_ORDER.get(r.get("_state"), 9), r["id"]))
    return sorted(rows, key=lambda r: r["id"], reverse=True)  # newest


def _asset_usage(cfg: Config) -> dict[str, dict[str, set]]:
    """{'video': {filename: {effective states of renders using it}}, 'audio': {...}}"""
    out: dict[str, dict[str, set]] = {"video": {}, "audio": {}}
    for side in cfg.path("output").glob("*.json"):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        state = _effective_state(md)
        src = md.get("sources", {})
        for n in src.get("video", []):
            out["video"].setdefault(n, set()).add(state)
        for n in src.get("audio", []):
            out["audio"].setdefault(n, set()).add(state)
    return out


_ASSET_FILTERS = (
    "all", "video-only", "audio-only", "captions-only", "favourite", "unused", "used",
    "pending", "approved", "published", "rejected",
)


def _caption_sections(cfg: Config) -> list[dict]:
    """One block per caption/overlay template list — its lines tagged 'base'
    (from config.yaml, kept) or 'bank' (from caption_bank.yaml, removable here)."""
    from social_peace.config import _BANK_MAP
    from social_peace.pipeline.captions import _config_list, read_caption_bank

    bank = read_caption_bank(cfg)
    out = []
    for key in _BANK_MAP:
        bank_lines = set(bank.get(key, []))
        rows = [{"text": ln, "origin": "bank" if ln in bank_lines else "base"}
                for ln in _config_list(cfg, key)]
        out.append({
            "key": key,
            "title": key.replace("_", " "),
            "rows": rows,
            "n_bank": sum(1 for r in rows if r["origin"] == "bank"),
        })
    return out


def _asset_matches(filt: str, states: set, favourite: bool = False) -> bool:
    if filt == "all":
        return True
    if filt == "favourite":
        return favourite
    if filt == "unused":
        return not states
    if filt == "used":
        return bool(states)
    return filt in states


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    output_dir = cfg.path("output")
    log_dir = cfg.path("logs")

    @app.get("/")
    def index():
        filt = request.args.get("filter", "pending")
        if filt not in _FILTERS:
            filt = "pending"
        # the approved tab is the posting queue — show it in posting order
        sort = request.args.get("sort", "queue" if filt == "approved" else "newest")
        if sort not in _SORTS:
            sort = "newest"
        rows = _load_all(cfg, output_dir)
        counts = {s: len(_filtered(rows, s)) for s in _TABS}
        counts["all"] = len(rows)
        sched = schedule.status(cfg)
        return render_template(
            "index.html",
            videos=_sorted(_filtered(rows, filt), sort, sched["queue"]),
            sched=sched,
            partial=dict(sched["partial"]),
            fmt_slot=schedule.fmt_slot,
            filt=filt,
            sort=sort,
            sorts=list(_SORTS),
            counts=counts,
            platforms=available_platforms(cfg),
            done_statuses=list(DONE_STATUSES),
            reject_reasons={k: {"button": v[1], "note": v[2]} for k, v in REJECT_REASONS.items()},
        )

    @app.get("/assets")
    def assets():
        filt = request.args.get("filter", "all")
        if filt not in _ASSET_FILTERS:
            filt = "all"
        sort = request.args.get("sort", "favourite")
        if sort not in _ASSET_SORTS:
            sort = "favourite"
        only = {"video-only": "video", "audio-only": "audio"}.get(filt)
        caps_only = filt == "captions-only"
        match = "all" if (only or caps_only) else filt
        usage = _asset_usage(cfg)
        totals = {
            "video": len(_list_media(cfg.path("video_assets"), VIDEO_EXTS)),
            "audio": len(_list_media(cfg.path("audio_assets"), AUDIO_EXTS)),
        }
        sections = [] if caps_only else [
            (k, _asset_rows(cfg, k, usage[k], match, sort), totals[k])
            for k in ("video", "audio")
            if only in (None, k)
        ]
        return render_template(
            "assets.html",
            sections=sections,
            caption_sections=_caption_sections(cfg) if filt in ("all", "captions-only") else [],
            filt=filt,
            filters=list(_ASSET_FILTERS),
            sort=sort,
            sorts=list(_ASSET_SORTS),
            embed=bool(request.args.get("embed")),
        )

    @app.get("/asset-media/<kind>/<name>")
    def asset_media(kind: str, name: str):
        if kind not in ("video", "audio"):
            abort(404)
        name = _safe_stem(name)
        folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
        if not (folder / name).is_file():
            abort(404)
        return _send_media(folder, name)

    @app.post("/api/asset/<kind>/<name>/update")
    def asset_update(kind: str, name: str):
        if kind not in ("video", "audio"):
            return jsonify(error="kind must be 'video' or 'audio'"), 400
        name = _safe_stem(name)
        folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
        if not (folder / name).is_file():
            abort(404)
        body = request.get_json(silent=True) or {}
        updates: dict = {}
        if "label" in body:
            lbl = (body["label"] or "").strip()
            updates["label"] = lbl or None
        if "favourite" in body:
            updates["favourite"] = bool(body["favourite"]) or None
        if not updates:
            return jsonify(error="nothing to update (send label and/or favourite)"), 400
        man_path = cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml")
        entry = update_manifest_entry(man_path, kind, name, updates)
        cfg.manifest.setdefault(kind, {})[name] = entry  # keep in-process view fresh
        return jsonify(ok=True, label=entry.get("label"), favourite=bool(entry.get("favourite")))

    @app.post("/api/asset/<kind>/<name>/delete")
    def asset_delete(kind: str, name: str):
        if kind not in ("video", "audio"):
            return jsonify(error="kind must be 'video' or 'audio'"), 400
        name = _safe_stem(name)
        folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
        f = folder / name
        if not f.is_file():
            abort(404)
        f.unlink()
        remove_manifest_entry(
            cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml"), kind, name
        )
        _DUR_CACHE.pop(str(f), None)
        exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
        return jsonify(ok=True, pool_size=len(_list_media(folder, exts)))

    @app.post("/api/caption/<key>/<action>")
    def caption_line(key: str, action: str):
        from social_peace.pipeline.captions import (
            _BANK_KEYS,
            add_caption_bank_line,
            remove_caption_bank_line,
        )
        if key not in _BANK_KEYS:
            return jsonify(error=f"unknown caption list {key!r}"), 404
        if action not in ("add", "delete"):
            abort(404)
        text = (request.get_json(silent=True) or {}).get("text", "")
        if not str(text).strip():
            return jsonify(error="send a non-empty 'text'"), 400
        try:
            if action == "add":
                return jsonify(add_caption_bank_line(cfg, key, str(text)))
            res = remove_caption_bank_line(cfg, key, str(text))
            return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 400)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/api/videos")
    def api_videos():
        filt = request.args.get("filter", "pending")
        if filt not in _FILTERS:
            filt = "pending"
        return jsonify(_filtered(_load_all(cfg, output_dir), filt))

    @app.get("/media/<stem>.mp4")
    def media(stem: str):
        stem = _safe_stem(stem)
        if not (output_dir / f"{stem}.json").is_file():
            abort(404)
        return _send_media(output_dir, f"{stem}.mp4")

    @app.get("/thumb/<stem>.jpg")
    def thumb(stem: str):
        stem = _safe_stem(stem)
        if not (output_dir / f"{stem}.json").is_file():
            abort(404)
        jpg = _thumbnail(output_dir, stem)
        if jpg is None:
            abort(404)
        return send_from_directory(output_dir, jpg.name, max_age=3600)

    @app.post("/api/decision/<stem>")
    def decision(stem: str):
        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        body = request.get_json(silent=True) or {}
        state = body.get("state")
        if state not in REVIEW_STATES:
            return jsonify(error=f"state must be one of {REVIEW_STATES}"), 400
        md = set_review(
            sidecar,
            state,
            note=str(body.get("note", "")),
            captions=body.get("captions") or None,
        )
        ledger.record(
            log_dir, "review",
            video_id=stem, state=state, note=str(body.get("note", "")), ok=True,
        )
        return jsonify(ok=True, review=md["review"])

    def _queue_variant(stem: str, md: dict, changes: list[str], prefer: str | None = None,
                       reasons: list[str] | None = None) -> str:
        """Render a variant of `md` with `changes` re-rolled, as a background job
        (it waits its turn / for a quiet PC in pipeline.throttle)."""
        from social_peace.pipeline.variant import make_variant

        def work():
            res = make_variant(cfg, md, changes, prefer=prefer)
            ledger.record(
                log_dir, "build", video_id=res["id"], template=res["template"],
                seed=res["seed"], duration=res.get("duration"),
                sources=res.get("sources"), variant_of=stem, variant_change="+".join(changes),
                variant_prefer=prefer, reasons=reasons or None, ok=True,
            )
            return {"id": res["id"], "change": "+".join(changes), "variant_of": stem}

        what = {"video": "clips", "audio": "audio", "text": "text"}
        label = f"re-rolling {stem}: new {' + '.join(what[c] for c in changes)}"
        if prefer and set(changes) & {"video", "audio"}:
            label += f" matching “{prefer}”"
        return _start_job("variant", work, label)

    def _reasons(body: dict) -> list[str] | None:
        """Ticked reasons from a request body (None if any are unknown)."""
        raw = body.get("reasons")
        if raw is None and body.get("reason"):
            raw = [body["reason"]]
        reasons = list(dict.fromkeys(raw or []))
        return reasons if all(r in REJECT_REASONS for r in reasons) else None

    @app.post("/api/reject/<stem>")
    def reject_reason(stem: str):
        """Reject for the ticked reason(s), then queue one re-roll with every
        ticked part changed (see variant.reject_for), steered by the optional
        `prefer` wish like /api/variant."""
        from social_peace.pipeline.variant import changes_for, reject_for

        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        body = request.get_json(silent=True) or {}
        reasons = _reasons(body)
        if not reasons:
            return jsonify(error=f"tick one or more of {list(REJECT_REASONS)}"), 400
        md = reject_for(cfg, sidecar, reasons, note=str(body.get("note", "")))
        ledger.record(log_dir, "review", video_id=stem, state="rejected",
                      reasons=reasons, note=md["review"]["note"], ok=True)
        prefer = (body.get("prefer") or "").strip() or None
        job = _queue_variant(stem, md, changes_for(reasons), prefer, reasons)
        return jsonify(job_id=job, review=md["review"])

    @app.post("/api/captions/<stem>")
    def api_captions(stem: str):
        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        from social_peace.pipeline.metadata import regenerate_captions
        return jsonify(regenerate_captions(cfg, sidecar))

    @app.post("/api/publish/<stem>")
    def publish_video(stem: str):
        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        platform = (request.get_json(silent=True) or {}).get("platform")
        if platform and platform not in available_platforms(cfg):
            return jsonify(error=f"{platform!r} is not in project.target_platforms"), 400
        plats = [platform] if platform else None
        label = f"publishing {stem} → {platform or ', '.join(available_platforms(cfg))}"

        def work():
            return publish_sidecar(cfg, sidecar, platforms=plats)

        return jsonify(job_id=_start_job("publish", work, label))

    @app.get("/api/tiktok/login")
    def tiktok_login_start():
        from social_peace.publish import tiktok
        try:
            return jsonify(url=tiktok.start_login())
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/tiktok/login")
    def tiktok_login_finish():
        from social_peace.publish import tiktok
        pasted = (request.get_json(silent=True) or {}).get("redirect") or ""
        try:
            data = tiktok.finish_login(pasted)
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, scope=data.get("scope"))

    @app.post("/api/publish-approved")
    def publish_approved_all():
        def work():
            return {"reports": publish_all_approved(cfg)}

        return jsonify(job_id=_start_job("publish", work, "publishing all approved"))

    @app.post("/api/queue/<stem>/move")
    def queue_move(stem: str):
        stem = _safe_stem(stem)
        to = (request.get_json(silent=True) or {}).get("to")
        if to not in ("top", "up", "down", "bottom"):
            return jsonify(error="to must be top / up / down / bottom"), 400
        try:
            order = schedule.move(cfg, stem, to)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, queue=order)

    @app.get("/api/schedule")
    def api_schedule():
        st = schedule.status(cfg)
        return jsonify(
            enabled=st["enabled"], timezone=st["timezone"], slots=st["slots"],
            posts_per_day=st["posts_per_day"], queue=st["queue"], low=st["low"],
            queue_days=round(st["queue_days"], 2),
            next_slot=st["next_slot"].isoformat() if st["next_slot"] else None,
            runs_out=st["runs_out"].isoformat() if st["runs_out"] else None,
            plan={k: v.isoformat() for k, v in st["plan"].items()},
            partial=st["partial"],
        )

    @app.post("/api/generate")
    def api_generate():
        body = request.get_json(silent=True) or {}
        # auto: exactly what the nightly timer runs — batch sized to keep the
        # posting queue stocked (may be 0), library topped up first if short
        auto = bool(body.get("auto"))
        count = None if auto else int(
            body.get("count") or cfg.raw.get("pipeline", {}).get("batch_size", 4))
        # tops up clips / beds only when the library is short of unused ones
        do_fetch = bool(body.get("fetch", True))

        def work():
            from social_peace.pipeline.auto import run_pipeline
            s = run_pipeline(cfg, batch=count, do_fetch=do_fetch, dry_run=False)
            return {"built": s.get("built", []), "count": len(s.get("built", [])),
                    "fetched": s.get("fetched", 0), "plan": s.get("plan")}

        label = "filling the posting queue" if auto else f"generating {count} new render(s)"
        return jsonify(job_id=_start_job("generate", work, label))

    @app.post("/api/fetch/<kind>")
    def api_fetch(kind: str):
        if kind not in ("video", "audio"):
            return jsonify(error="kind must be 'video' or 'audio'"), 400
        query = ((request.get_json(silent=True) or {}).get("query") or "").strip() or None
        label = f"fetching {kind}" + (f" “{query}”" if query else "")

        def work():
            from social_peace.pipeline.auto import fetch_audio_library, fetch_video_library
            fn = fetch_video_library if kind == "video" else fetch_audio_library
            return fn(cfg, query=query)

        return jsonify(job_id=_start_job("fetch-" + kind, work, label))

    @app.post("/api/captions-bank")
    def api_captions_bank():
        def work():
            from social_peace.pipeline.captions import append_caption_bank, expand_caption_bank
            add = expand_caption_bank(cfg)
            if "error" in add:
                raise RuntimeError(add["error"])
            return append_caption_bank(cfg, add)

        return jsonify(job_id=_start_job("fetch-captions", work, "fetching caption ideas"))

    @app.post("/api/prune-rejected")
    def api_prune_rejected():
        from social_peace.pipeline.prune import prune_rejected
        body = request.get_json(silent=True) or {}
        drop_all = bool(body.get("all"))
        days = None if drop_all else int(body.get("days", 30))
        removed = prune_rejected(cfg, older_than_days=days, drop_all=drop_all)
        return jsonify(removed=removed, count=len(removed))

    @app.post("/api/variant/<stem>")
    def api_variant(stem: str):
        """Re-roll without rejecting. Body: `reasons` (ticked boxes — recorded,
        and they pick what to change) and/or `change` (one or more of
        video / audio / text), plus an optional `prefer` wish."""
        from social_peace.pipeline.variant import changes_for, note_reasons

        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        body = request.get_json(silent=True) or {}
        reasons = _reasons(body)
        if reasons is None:
            return jsonify(error=f"reasons must be from {list(REJECT_REASONS)}"), 400
        change = body.get("change") or []
        change = [change] if isinstance(change, str) else list(change)
        if any(c not in CHANGEABLE for c in change):
            return jsonify(error=f"change must be from {list(CHANGEABLE)}"), 400
        changes = list(dict.fromkeys(change + (changes_for(reasons) if reasons else [])))
        if not changes:
            return jsonify(error="tick what to change (or send change)"), 400
        prefer = (body.get("prefer") or "").strip() or None
        original = json.loads(sidecar.read_text(encoding="utf-8"))
        note_reasons(cfg, original, reasons)
        return jsonify(job_id=_queue_variant(stem, original, changes, prefer, reasons))

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            abort(404)
        return jsonify(job)

    @app.get("/api/jobs")
    def api_jobs():
        """Running jobs + any that finished within the last _JOB_TTL seconds, so a
        status the user started elsewhere still shows after navigating."""
        _prune_jobs()
        with _JOBS_LOCK:
            jobs = sorted(_JOBS.values(), key=lambda j: j.get("started_at", 0), reverse=True)
        return jsonify(jobs=jobs)

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8756) -> None:
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no per-request access lines
    app = create_app(cfg)
    try:
        from social_peace.pipeline.prune import prune_rejected
        days = int((cfg.raw.get("retention") or {}).get("rejected_days", 30))
        gone = prune_rejected(cfg, older_than_days=days)
        if gone:
            log.info("startup: pruned %d rejected render(s) older than %d days", len(gone), days)
    except Exception:  # noqa: BLE001
        log.exception("startup prune failed")
    log.info("review UI on http://%s:%d", host, port)
    # threaded so a slow upload (resumable YouTube / IG container poll) doesn't
    # freeze the rest of the UI.
    app.run(host=host, port=port, debug=False, threaded=True)
