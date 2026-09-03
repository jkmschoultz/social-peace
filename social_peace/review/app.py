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
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from social_peace import ledger
from social_peace.config import Config
from social_peace.fetch.common import remove_manifest_entry
from social_peace.pipeline.metadata import REVIEW_STATES, set_review
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media
from social_peace.pipeline.variant import CHANGEABLE
from social_peace.publish.runner import (
    DONE_STATUSES,
    available_platforms,
    publish_all_approved,
    publish_sidecar,
)

log = logging.getLogger(__name__)

_STEM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FILTERS = ("pending", "approved", "published", "rejected", "all")
_TABS = ("pending", "approved", "published", "rejected")

# in-memory registry for slow render jobs (generate / variant). Per-process; the
# renders land in output/ regardless, so a lost job just means no status polling.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _start_job(kind: str, work) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"id": job_id, "kind": kind, "state": "running"}

    def _run():
        try:
            res = work()
            upd = {"state": "done", "result": res}
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s (%s) failed", job_id, kind)
            upd = {"state": "error", "error": str(exc)}
        with _JOBS_LOCK:
            _JOBS[job_id].update(upd)

    threading.Thread(target=_run, daemon=True).start()
    return job_id


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


def _asset_rows(cfg: Config, kind: str, usage: dict[str, set], filt: str = "all") -> list[dict]:
    folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
    exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
    man = cfg.manifest.get(kind) or {}
    rows = []
    for p in _list_media(folder, exts):
        states = usage.get(p.name, set())
        if not _asset_matches(filt, states):
            continue
        m = man.get(p.name) or {}
        rows.append({
            "name": p.name,
            "size": p.stat().st_size,
            "duration": _duration(p),
            "source": m.get("source"),
            "license": m.get("license"),
            "url": m.get("url"),
            "tags": m.get("tags") or [],
            "used": bool(states),
            "states": sorted(states),
        })
    return rows


def _load_all(output_dir: Path) -> list[dict]:
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
        md.setdefault("source_urls", {"video": {}, "audio": {}})
        md["_state"] = _effective_state(md)
        plats = md.setdefault("platforms", {})
        plats.setdefault("youtube", {"title": "", "description": ""})
        plats.setdefault("tiktok", {"caption": ""})
        plats.setdefault("instagram", {"caption": ""})
        rows.append(md)
    return rows


def _is_published(md: dict) -> bool:
    pub = md.get("published") or {}
    if any((i or {}).get("url") or (i or {}).get("remote_id") for i in pub.values()):
        return True
    return any(s in DONE_STATUSES for s in (md.get("status") or {}).values())


def _effective_state(md: dict) -> str:
    """review.state, except an approved render that has gone live on >=1 platform
    reports as 'published' so it moves to its own tab."""
    state = md.get("review", {}).get("state", "pending")
    if state == "approved" and _is_published(md):
        return "published"
    return state


def _filtered(rows: list[dict], filt: str) -> list[dict]:
    if filt == "all":
        return rows
    return [r for r in rows if _effective_state(r) == filt]


_SORTS = ("newest", "oldest", "seed", "template", "status")
_STATE_ORDER = {"pending": 0, "approved": 1, "published": 2, "rejected": 3}


def _sorted(rows: list[dict], sort: str) -> list[dict]:
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


_ASSET_FILTERS = ("all", "unused", "used", "pending", "approved", "published", "rejected")


def _asset_matches(filt: str, states: set) -> bool:
    if filt == "all":
        return True
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
        sort = request.args.get("sort", "newest")
        if sort not in _SORTS:
            sort = "newest"
        rows = _load_all(output_dir)
        counts = {s: len(_filtered(rows, s)) for s in _TABS}
        counts["all"] = len(rows)
        return render_template(
            "index.html",
            videos=_sorted(_filtered(rows, filt), sort),
            filt=filt,
            sort=sort,
            sorts=list(_SORTS),
            counts=counts,
            platforms=available_platforms(cfg),
            done_statuses=list(DONE_STATUSES),
        )

    @app.get("/assets")
    def assets():
        filt = request.args.get("filter", "all")
        if filt not in _ASSET_FILTERS:
            filt = "all"
        usage = _asset_usage(cfg)
        return render_template(
            "assets.html",
            video=_asset_rows(cfg, "video", usage["video"], filt),
            audio=_asset_rows(cfg, "audio", usage["audio"], filt),
            total_video=len(_list_media(cfg.path("video_assets"), VIDEO_EXTS)),
            total_audio=len(_list_media(cfg.path("audio_assets"), AUDIO_EXTS)),
            filt=filt,
            filters=list(_ASSET_FILTERS),
        )

    @app.get("/asset-media/<kind>/<name>")
    def asset_media(kind: str, name: str):
        if kind not in ("video", "audio"):
            abort(404)
        name = _safe_stem(name)
        folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
        if not (folder / name).is_file():
            abort(404)
        return send_from_directory(folder, name, conditional=True)

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

    @app.get("/api/videos")
    def api_videos():
        filt = request.args.get("filter", "pending")
        if filt not in _FILTERS:
            filt = "pending"
        return jsonify(_filtered(_load_all(output_dir), filt))

    @app.get("/media/<stem>.mp4")
    def media(stem: str):
        stem = _safe_stem(stem)
        if not (output_dir / f"{stem}.json").is_file():
            abort(404)
        return send_from_directory(output_dir, f"{stem}.mp4", conditional=True)

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

    @app.post("/api/publish/<stem>")
    def publish_video(stem: str):
        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        platform = (request.get_json(silent=True) or {}).get("platform")
        if platform:
            if platform not in available_platforms(cfg):
                return jsonify(error=f"{platform!r} is not in project.target_platforms"), 400
            return jsonify(publish_sidecar(cfg, sidecar, platforms=[platform]))
        return jsonify(publish_sidecar(cfg, sidecar))

    @app.post("/api/publish-approved")
    def publish_approved_all():
        return jsonify(reports=publish_all_approved(cfg))

    @app.post("/api/generate")
    def api_generate():
        body = request.get_json(silent=True) or {}
        count = int(body.get("count") or cfg.raw.get("pipeline", {}).get("batch_size", 4))
        do_fetch = bool(body.get("fetch", False))

        def work():
            from social_peace.pipeline.auto import run_pipeline
            s = run_pipeline(cfg, batch=count, do_fetch=do_fetch, dry_run=False)
            return {"built": s.get("built", []), "count": len(s.get("built", []))}

        return jsonify(job_id=_start_job("generate", work))

    @app.post("/api/fetch/<kind>")
    def api_fetch(kind: str):
        if kind not in ("video", "audio"):
            return jsonify(error="kind must be 'video' or 'audio'"), 400

        def work():
            from social_peace.pipeline.auto import fetch_audio_library, fetch_video_library
            fn = fetch_video_library if kind == "video" else fetch_audio_library
            return fn(cfg)

        return jsonify(job_id=_start_job("fetch-" + kind, work))

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
        stem = _safe_stem(stem)
        sidecar = output_dir / f"{stem}.json"
        if not sidecar.is_file():
            abort(404)
        change = (request.get_json(silent=True) or {}).get("change")
        if change not in CHANGEABLE:
            return jsonify(error=f"change must be one of {list(CHANGEABLE)}"), 400
        original = json.loads(sidecar.read_text(encoding="utf-8"))

        def work():
            from social_peace.pipeline.variant import make_variant
            res = make_variant(cfg, original, change)
            ledger.record(
                log_dir, "build", video_id=res["id"], template=res["template"],
                seed=res["seed"], duration=res.get("duration"),
                sources=res.get("sources"), variant_of=stem, variant_change=change, ok=True,
            )
            return {"id": res["id"], "change": change, "variant_of": stem}

        return jsonify(job_id=_start_job("variant", work))

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            abort(404)
        return jsonify(job)

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8756) -> None:
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
