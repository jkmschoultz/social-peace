"""Instagram Reels publisher via the Instagram Graph API.

STATUS: skeleton — the container -> poll -> publish flow is written but has not
been run against the real API.

IMPORTANT — Instagram will not accept a file upload. It fetches the video from a
**public HTTPS URL** you provide. A local render in output/ is not reachable, so
you must host it somewhere public first. Set:
    INSTAGRAM_PUBLIC_BASE_URL   e.g. https://media.example.com/social-peace
and make sure `<INSTAGRAM_PUBLIC_BASE_URL>/<video filename>` serves the mp4.

SETUP (before the first run):
  1. Convert the target Instagram account to a **Business or Creator** account and
     link it to a Facebook Page.
  2. https://developers.facebook.com -> create an app (type "Business"). Add the
     "Instagram Graph API" product. Get a long-lived User (or Page) access token
     with `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`.
     Production posting to accounts you don't own needs App Review for
     `instagram_content_publish`; your own account works in dev mode.
  3. Find the IG user id: GET /me/accounts -> page id -> GET /{page-id}?fields=
     instagram_business_account. Put values in .env:
       INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_PUBLIC_BASE_URL
     optional: INSTAGRAM_API_VERSION (default v21.0)

DOCS: https://developers.facebook.com/docs/instagram-api/guides/content-publishing
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

platform = "instagram"
_GRAPH = "https://graph.facebook.com"
_POLL_TIMEOUT_S = 300


def _cfg() -> tuple[str, str, str, str]:
    user_id = os.environ.get("INSTAGRAM_USER_ID")
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    base_url = os.environ.get("INSTAGRAM_PUBLIC_BASE_URL")
    version = os.environ.get("INSTAGRAM_API_VERSION", "v21.0")
    missing = [
        n for n, v in [
            ("INSTAGRAM_USER_ID", user_id),
            ("INSTAGRAM_ACCESS_TOKEN", token),
            ("INSTAGRAM_PUBLIC_BASE_URL", base_url),
        ] if not v
    ]
    if missing:
        raise RuntimeError(f"instagram not configured — set {', '.join(missing)} in .env")
    return user_id, token, base_url.rstrip("/"), version


def _create_container(base: str, user_id: str, token: str, video_url: str, caption: str) -> str:
    resp = requests.post(
        f"{base}/{user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2200],
            "access_token": token,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _wait_ready(base: str, container_id: str, token: str) -> None:
    deadline = time.time() + _POLL_TIMEOUT_S
    while time.time() < deadline:
        resp = requests.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        code = resp.json().get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"instagram container failed: {resp.json()}")
        time.sleep(5)
    raise TimeoutError(f"instagram container not ready after {_POLL_TIMEOUT_S}s")


def _publish_container(base: str, user_id: str, token: str, container_id: str) -> str:
    resp = requests.post(
        f"{base}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _permalink(base: str, media_id: str, token: str) -> str | None:
    try:
        resp = requests.get(
            f"{base}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("permalink")
    except Exception:  # noqa: BLE001
        return None


def publish(video_path: Path, metadata: dict) -> PublishResult:
    ig = metadata.get("platforms", {}).get("instagram")
    if not ig:
        return PublishResult(platform, ok=False, status="error", error="no instagram block in sidecar")

    try:
        user_id, token, base_url, version = _cfg()
    except RuntimeError as exc:
        return PublishResult(platform, ok=False, status="error", error=str(exc))

    base = f"{_GRAPH}/{version}"
    video_url = f"{base_url}/{video_path.name}"
    caption = ig.get("caption") or ""

    try:
        container_id = _create_container(base, user_id, token, video_url, caption)
        _wait_ready(base, container_id, token)
        media_id = _publish_container(base, user_id, token, container_id)
        return PublishResult(
            platform, ok=True, status="uploaded",
            url=_permalink(base, media_id, token), remote_id=media_id,
        )
    except (RuntimeError, TimeoutError) as exc:
        log.error("instagram: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else ""
        log.error("instagram HTTP error: %s %s", exc, body)
        return PublishResult(platform, ok=False, status="error", error=f"{exc} {body}")
    except Exception as exc:  # noqa: BLE001
        log.exception("instagram publish failed")
        return PublishResult(platform, ok=False, status="error", error=str(exc))
