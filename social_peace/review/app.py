"""Flask app backing `social-peace review`.

The per-video JSON sidecar in output/ is the only store. Bind to localhost; there
is no auth. Approve/Reject writes review.state (and any caption edits) back into
the sidecar via pipeline.metadata.set_review, and appends a `review` ledger event.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from social_peace import ledger
from social_peace.config import Config
from social_peace.pipeline.metadata import REVIEW_STATES, set_review

log = logging.getLogger(__name__)

_STEM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FILTERS = ("pending", "approved", "rejected", "all")


def _safe_stem(stem: str) -> str:
    if not _STEM_RE.match(stem or "") or "/" in stem or ".." in stem:
        abort(404)
    return stem


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
        md.setdefault("source_urls", {"video": {}, "audio": {}})
        plats = md.setdefault("platforms", {})
        plats.setdefault("youtube", {"title": "", "description": ""})
        plats.setdefault("tiktok", {"caption": ""})
        plats.setdefault("instagram", {"caption": ""})
        rows.append(md)
    return rows


def _filtered(rows: list[dict], filt: str) -> list[dict]:
    if filt == "all":
        return rows
    return [r for r in rows if r.get("review", {}).get("state") == filt]


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    output_dir = cfg.path("output")
    log_dir = cfg.path("logs")

    @app.get("/")
    def index():
        filt = request.args.get("filter", "pending")
        if filt not in _FILTERS:
            filt = "pending"
        rows = _load_all(output_dir)
        counts = {s: len(_filtered(rows, s)) for s in ("pending", "approved", "rejected")}
        counts["all"] = len(rows)
        return render_template(
            "index.html", videos=_filtered(rows, filt), filt=filt, counts=counts
        )

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

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8756) -> None:
    app = create_app(cfg)
    log.info("review UI on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
