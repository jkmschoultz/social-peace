"""Instagram Reels publisher — supports both current Meta setups for the
"Manage messaging and content on Instagram" use case:

  * API setup with **Instagram Login** (graph.instagram.com) — no Facebook Page.
    Add the account under the app's "Generate access tokens", copy the token to
    INSTAGRAM_ACCESS_TOKEN. Nothing else needed — the IG id is read from /me.
  * API setup with **Facebook Login** (graph.facebook.com) — IG Business/Creator
    account linked to a Facebook Page. Token needs `instagram_basic`,
    `instagram_content_publish`, `pages_read_engagement`, `pages_show_list`,
    `business_management`. Also set INSTAGRAM_USER_ID to the numeric IG business
    account id (GET /{page-id}?fields=instagram_business_account).

Which one you're on is auto-detected from the token. `instagram_content_publish`
(advanced access) needs App Review to post to accounts you don't own; your own
account works while the app is in development.

Instagram will not accept a file upload; it fetches the video from a public HTTPS
URL. Set INSTAGRAM_PUBLIC_BASE_URL to a host you control, or leave it unset and
this module spins an ephemeral Cloudflare quick tunnel (`cloudflared` binary
required) over the render for the few seconds Instagram needs, then tears it down.

DOCS: https://developers.facebook.com/docs/instagram-platform/content-publishing
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import nullcontext
from pathlib import Path

import requests

from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

platform = "instagram"
_POLL_TIMEOUT_S = 300


def _resolve(token: str, user_id_hint: str, version: str) -> tuple[str, str]:
    """Probe both setups; return (api_base, ig_user_id). Raises RuntimeError with
    the Graph error(s) if neither works."""
    # Instagram Login — token is validated against graph.instagram.com/me
    r = requests.get(
        f"https://graph.instagram.com/{version}/me",
        params={"fields": "user_id,username", "access_token": token}, timeout=30,
    )
    if r.ok:
        j = r.json()
        return f"https://graph.instagram.com/{version}", str(j.get("user_id") or j.get("id"))
    ig_probe = f"instagram-login probe {r.status_code}: {r.text}"

    # Facebook Login — needs the numeric IG business account id
    if user_id_hint.isdigit():
        r2 = requests.get(
            f"https://graph.facebook.com/{version}/{user_id_hint}",
            params={"fields": "id", "access_token": token}, timeout=30,
        )
        if r2.ok:
            return f"https://graph.facebook.com/{version}", user_id_hint
        raise RuntimeError(f"instagram auth failed. {ig_probe}; "
                           f"facebook-login probe {r2.status_code}: {r2.text}")

    raise RuntimeError(
        f"instagram auth failed. {ig_probe}. If you set up 'API with Facebook "
        f"Login', also set INSTAGRAM_USER_ID to the numeric IG business account id."
    )


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


def _run(base: str, user_id: str, token: str, video_url: str, caption: str) -> PublishResult:
    container_id = _create_container(base, user_id, token, video_url, caption)
    _wait_ready(base, container_id, token)
    media_id = _publish_container(base, user_id, token, container_id)
    return PublishResult(
        platform, ok=True, status="uploaded",
        url=_permalink(base, media_id, token), remote_id=media_id,
    )


def publish(video_path: Path, metadata: dict) -> PublishResult:
    ig = metadata.get("platforms", {}).get("instagram")
    if not ig:
        return PublishResult(platform, ok=False, status="error", error="no instagram block in sidecar")

    token = (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return PublishResult(platform, ok=False, status="error",
                             error="instagram not configured — set INSTAGRAM_ACCESS_TOKEN in .env")

    version = os.environ.get("INSTAGRAM_API_VERSION", "v21.0")
    user_id_hint = (os.environ.get("INSTAGRAM_USER_ID") or "").strip()
    caption = ig.get("caption") or ""
    public_base = os.environ.get("INSTAGRAM_PUBLIC_BASE_URL")

    # auto-detect the setup + validate the token before spinning up a tunnel
    try:
        base, ig_id = _resolve(token, user_id_hint, version)
    except RuntimeError as exc:
        return PublishResult(platform, ok=False, status="error", error=str(exc))

    try:
        if public_base:
            ctx = nullcontext(f"{public_base.rstrip('/')}/{video_path.name}")
        else:
            from social_peace.publish._tunnel import public_file
            ctx = public_file(video_path)
        with ctx as video_url:
            return _run(base, ig_id, token, video_url, caption)
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
