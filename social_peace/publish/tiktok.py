"""TikTok publisher via the Content Posting API (FILE_UPLOAD).

STATUS: skeleton — the full init -> upload -> poll flow is written but has not been
run against the real API.

SETUP (before the first run):
  1. https://developers.tiktok.com -> create an app. Add the **Content Posting API**
     product. Request scopes `video.publish` and `video.upload`.
  2. While the app is **unaudited**, `video.publish` only works for the app's own
     registered test users and every post is forced to `SELF_ONLY` (private). App
     review lifts that. Keep `metadata.platforms.tiktok.privacy` at `SELF_ONLY`
     until you've been audited.
  3. Do the OAuth user-authorization flow once to get a user access token + refresh
     token for the account you post to, and put them in .env:
       TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
       TIKTOK_ACCESS_TOKEN, TIKTOK_REFRESH_TOKEN
     (This module refreshes the access token with the refresh token when it 401s;
     it does not implement the initial browser consent — that's a one-time manual
     step, e.g. via the TikTok docs' OAuth playground.)

DOCS: https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

platform = "tiktok"
API = "https://open.tiktokapis.com"
_TOKEN_FILE = Path(os.environ.get("TIKTOK_TOKEN_FILE", "secrets/tiktok_token.json"))
_POLL_TIMEOUT_S = 300


def _access_token() -> str:
    if _TOKEN_FILE.is_file():
        tok = json.loads(_TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
        if tok:
            return tok
    tok = os.environ.get("TIKTOK_ACCESS_TOKEN")
    if not tok:
        raise RuntimeError(
            "no TikTok access token — set TIKTOK_ACCESS_TOKEN in .env "
            f"or cache one at {_TOKEN_FILE} (see module docstring)"
        )
    return tok


def _refresh_access_token() -> str | None:
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN")
    if not (key and secret and refresh):
        return None
    resp = requests.post(
        f"{API}/v2/oauth/token/",
        data={
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    tok = data.get("access_token")
    if tok:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    return tok


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}


def _init_upload(token: str, tt: dict, size: int) -> dict:
    body = {
        "post_info": {
            "title": (tt.get("caption") or "")[:2200],
            "privacy_level": tt.get("privacy", "SELF_ONLY"),
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,          # single chunk: whole file (fine for <= 64 MB)
            "total_chunk_count": 1,
        },
    }
    resp = requests.post(
        f"{API}/v2/post/publish/video/init/", headers=_headers(token),
        data=json.dumps(body), timeout=60,
    )
    if resp.status_code == 401:
        raise PermissionError("tiktok token expired")
    resp.raise_for_status()
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"tiktok init error: {data['error']}")
    return data["data"]  # { publish_id, upload_url }


def _upload(upload_url: str, video_path: Path, size: int) -> None:
    with video_path.open("rb") as fh:
        resp = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            data=fh,
            timeout=600,
        )
    resp.raise_for_status()


def _poll(token: str, publish_id: str) -> dict:
    deadline = time.time() + _POLL_TIMEOUT_S
    while time.time() < deadline:
        resp = requests.post(
            f"{API}/v2/post/publish/status/fetch/", headers=_headers(token),
            data=json.dumps({"publish_id": publish_id}), timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            return data
        if status in ("FAILED", "PUBLISH_FAILED"):
            raise RuntimeError(f"tiktok publish failed: {data}")
        time.sleep(5)
    raise TimeoutError(f"tiktok publish still processing after {_POLL_TIMEOUT_S}s")


def publish(video_path: Path, metadata: dict) -> PublishResult:
    tt = metadata.get("platforms", {}).get("tiktok")
    if not tt:
        return PublishResult(platform, ok=False, status="error", error="no tiktok block in sidecar")

    size = video_path.stat().st_size
    try:
        token = _access_token()
        try:
            init = _init_upload(token, tt, size)
        except PermissionError:
            token = _refresh_access_token()
            if not token:
                return PublishResult(platform, ok=False, status="error",
                                     error="tiktok token expired and refresh not configured")
            init = _init_upload(token, tt, size)

        _upload(init["upload_url"], video_path, size)
        done = _poll(token, init["publish_id"])
        post_id = done.get("publicaly_available_post_id") or done.get("publicly_available_post_id")
        post_id = post_id[0] if isinstance(post_id, list) and post_id else post_id
        url = f"https://www.tiktok.com/@me/video/{post_id}" if post_id else None
        return PublishResult(
            platform, ok=True, status="uploaded",
            url=url, remote_id=str(post_id or init["publish_id"]),
        )
    except RuntimeError as exc:
        log.error("tiktok: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except requests.HTTPError as exc:
        log.error("tiktok HTTP error: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("tiktok upload failed")
        return PublishResult(platform, ok=False, status="error", error=str(exc))
