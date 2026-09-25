"""TikTok publisher via the Content Posting API (Direct Post, FILE_UPLOAD).

SETUP (before the first run):
  1. https://developers.tiktok.com -> create an app. Add the **Login Kit** and
     **Content Posting API** products; turn on "Direct Post" and request the
     `video.publish` scope (`user.info.basic` comes with Login Kit).
  2. Login Kit -> Redirect URI: register docs/callback.html (served by GitHub Pages)
     and put the same URL in TIKTOK_REDIRECT_URI. After consent that page shows the
     address it was opened with (`?code=...&state=...`); paste it back into the
     review UI (or `social-peace tiktok-login`) to finish the login.
  3. Sandbox: add the TikTok account you post to as a Target User. While the app
     is **unaudited**, every post is forced to `SELF_ONLY` and that account must be
     set to **private** in the TikTok app. Keep `metadata.tiktok.privacy` at
     `SELF_ONLY` until the app passes review.
  4. Fill TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI in .env and
     run `social-peace tiktok-login`. Tokens are cached at TIKTOK_TOKEN_FILE: the
     access token lasts 24 h and is refreshed automatically. When there is no usable
     refresh token (missing, a year old, or revoked) publish returns
     status "needs-login", and the review UI offers the login right there.

LIMITS: roughly 15 posts a day per account and 6 init calls a minute per token.

DOCS: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

platform = "tiktok"
API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish"
_TOKEN_FILE = Path(os.environ.get("TIKTOK_TOKEN_FILE", "secrets/tiktok_token.json"))
_POLL_TIMEOUT_S = 600
_REFRESH_MARGIN_S = 900  # > _POLL_TIMEOUT_S, so the token can't lapse mid-post
_LOGIN_TTL_S = 900  # how long a started login's state stays valid
_pending_logins: dict[str, float] = {}  # state -> started at
_pending_lock = threading.Lock()

# Upload chunks: 5-64 MB each, the last one absorbs the remainder (up to 128 MB).
# A file of 64 MB or less goes up in one piece.
_SINGLE_CHUNK_MAX = 64 * 1024 * 1024
_CHUNK = 10 * 1024 * 1024


class _TokenExpired(Exception):
    pass


class _NeedsLogin(RuntimeError):
    """No usable token or refresh token — only a browser login fixes it."""


# ------------------------------------------------------------------------- tokens
def _load_token() -> dict:
    if _TOKEN_FILE.is_file():
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    # Bootstrap from .env (tokens made elsewhere, e.g. the TikTok OAuth playground).
    tok = os.environ.get("TIKTOK_ACCESS_TOKEN")
    if not tok:
        raise _NeedsLogin("no TikTok access token")
    return {"access_token": tok, "refresh_token": os.environ.get("TIKTOK_REFRESH_TOKEN")}


def _save_token(data: dict) -> dict:
    now = time.time()
    data = dict(data)
    if "expires_in" in data:
        data["expires_at"] = now + int(data.pop("expires_in"))
    if "refresh_expires_in" in data:
        data["refresh_expires_at"] = now + int(data.pop("refresh_expires_in"))
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        _TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return data


def _client_creds() -> tuple[str, str]:
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (key and secret):
        raise RuntimeError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set in .env")
    return key, secret


def _oauth_token(form: dict) -> dict:
    resp = requests.post(
        f"{API}/v2/oauth/token/", data=form, timeout=30,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    data = resp.json() if resp.content else {}
    if data.get("error") == "invalid_grant":  # refresh token revoked or expired
        raise _NeedsLogin(f"tiktok token rejected: {data.get('error_description', 'invalid_grant')}")
    if not resp.ok or "access_token" not in data:
        raise RuntimeError(
            f"tiktok token request failed: {data.get('error') or resp.status_code} "
            f"{data.get('error_description', '')}".strip()
        )
    return data


def _refresh(token: dict) -> dict:
    refresh = token.get("refresh_token")
    if not refresh:
        raise _NeedsLogin("tiktok access token expired and no refresh token")
    if token.get("refresh_expires_at") and token["refresh_expires_at"] < time.time():
        raise _NeedsLogin("tiktok refresh token expired")
    key, secret = _client_creds()
    log.info("tiktok: refreshing access token")
    data = _oauth_token({
        "client_key": key, "client_secret": secret,
        "grant_type": "refresh_token", "refresh_token": refresh,
    })
    return _save_token(data)


def _valid_token() -> dict:
    token = _load_token()
    exp = token.get("expires_at")
    if exp and exp - _REFRESH_MARGIN_S < time.time():
        token = _refresh(token)
    return token


# -------------------------------------------------------------------------- login
def auth_url(state: str) -> str:
    key, _ = _client_creds()
    return AUTH_URL + "?" + urlencode({
        "client_key": key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "state": state,
    })


def _redirect_uri() -> str:
    uri = os.environ.get("TIKTOK_REDIRECT_URI")
    if not uri:
        raise RuntimeError("TIKTOK_REDIRECT_URI not set — register one under Login Kit (see module docstring)")
    return uri


def parse_redirect(pasted: str, state: str) -> str:
    """Pull the auth code out of the redirect URL (or accept a bare code)."""
    pasted = pasted.strip()
    qs = parse_qs(urlparse(pasted).query)
    if "code" not in qs and "error" not in qs:
        if "://" in pasted or "?" in pasted:
            raise RuntimeError("tiktok consent: no code in the redirect URL")
        return pasted
    if "error" in qs:
        raise RuntimeError(f"tiktok consent failed: {qs['error'][0]} {qs.get('error_description', [''])[0]}")
    if qs.get("state", [state])[0] != state:
        raise RuntimeError("tiktok consent: state mismatch — paste the URL from this login attempt")
    return qs["code"][0]


def start_login() -> str:
    """Begin a browser login: returns TikTok's consent URL. Finish with finish_login()."""
    _client_creds()
    state = _secrets.token_urlsafe(16)
    now = time.time()
    with _pending_lock:
        for st, t in list(_pending_logins.items()):
            if now - t > _LOGIN_TTL_S:
                del _pending_logins[st]
        _pending_logins[state] = now
    return auth_url(state)


def finish_login(pasted: str) -> dict:
    """Exchange the code in the pasted callback URL. Saves and returns the token response."""
    key, secret = _client_creds()
    state = (parse_qs(urlparse(pasted.strip()).query).get("state") or [""])[0]
    with _pending_lock:
        started = _pending_logins.get(state)
        if started is None or time.time() - started > _LOGIN_TTL_S:
            raise RuntimeError("tiktok login: paste the full address from the latest login attempt")
        code = parse_redirect(pasted, state)
        del _pending_logins[state]
    data = _oauth_token({
        "client_key": key, "client_secret": secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": _redirect_uri(),
    })
    granted = set((data.get("scope") or "").split(","))
    if "video.publish" not in granted:
        log.warning("tiktok: video.publish not granted (got %s) — posting will fail", data.get("scope"))
    return _save_token(data)


def login() -> dict:
    """Terminal login: open the consent page, read the pasted callback URL."""
    url = start_login()
    print(f"Log in with the TikTok account to post to (opening in your browser):\n\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return finish_login(input("Paste the full address of the page you land on: "))


# ---------------------------------------------------------------------------- api
def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}


def _api(access_token: str, path: str, body: dict | None = None) -> dict:
    resp = requests.post(f"{API}{path}", headers=_headers(access_token), data=json.dumps(body or {}), timeout=60)
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"tiktok {path}: non-JSON response ({resp.status_code})")
    err = payload.get("error") or {}
    code = err.get("code", "ok")
    if resp.status_code == 401 or code == "access_token_invalid":
        raise _TokenExpired(code)
    if code != "ok":
        raise RuntimeError(f"tiktok {path}: {code} — {err.get('message', '')} (log_id {err.get('log_id', '?')})")
    return payload.get("data") or {}


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules."""
    if size <= _SINGLE_CHUNK_MAX:
        return size, 1
    return _CHUNK, size // _CHUNK


def _upload(upload_url: str, video_path: Path, size: int, chunk_size: int, count: int) -> None:
    with video_path.open("rb") as fh:
        for i in range(count):
            start = i * chunk_size
            end = size - 1 if i == count - 1 else start + chunk_size - 1
            fh.seek(start)
            blob = fh.read(end - start + 1)
            resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(blob)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                data=blob,
                timeout=600,
            )
            resp.raise_for_status()
            if count > 1:
                log.info("tiktok upload chunk %d/%d", i + 1, count)


def _poll(access_token: str, publish_id: str) -> dict | None:
    """Final status data, or None if TikTok is still processing at the deadline."""
    deadline = time.time() + _POLL_TIMEOUT_S
    while time.time() < deadline:
        try:
            data = _api(access_token, "/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        except _TokenExpired:
            return None  # already uploaded; never let the caller retry and re-post
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            return data
        if status == "FAILED":
            raise RuntimeError(f"tiktok publish failed: {data.get('fail_reason') or data}")
        time.sleep(5)
    return None


def _post(access_token: str, video_path: Path, tt: dict, duration: float | None) -> PublishResult:
    creator = _api(access_token, "/v2/post/publish/creator_info/query/")
    username = creator.get("creator_username")
    privacy = tt.get("privacy", "SELF_ONLY")
    options = creator.get("privacy_level_options") or []
    if options and privacy not in options:
        raise RuntimeError(f"privacy {privacy!r} not allowed for @{username} (allowed: {options})")
    max_s = creator.get("max_video_post_duration_sec")
    if max_s and duration and duration > max_s:
        raise RuntimeError(f"video is {duration:.0f}s, @{username} may post at most {max_s}s")

    size = video_path.stat().st_size
    chunk_size, count = chunk_plan(size)
    init = _api(access_token, "/v2/post/publish/video/init/", {
        "post_info": {
            "title": (tt.get("caption") or "")[:2200],
            "privacy_level": privacy,
            "disable_comment": bool(creator.get("comment_disabled")),
            "disable_duet": bool(creator.get("duet_disabled")),
            "disable_stitch": bool(creator.get("stitch_disabled")),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": count,
        },
    })
    publish_id = init["publish_id"]
    _upload(init["upload_url"], video_path, size, chunk_size, count)

    done = _poll(access_token, publish_id)
    profile = f"https://www.tiktok.com/@{username}" if username else None
    if done is None:
        # Uploaded and accepted; reporting success (with the publish_id) so the
        # queue doesn't retry and double-post while TikTok finishes processing.
        log.warning("tiktok: %s still processing after %ss — check the profile", publish_id, _POLL_TIMEOUT_S)
        return PublishResult(platform, ok=True, status="uploaded", url=profile, remote_id=publish_id)

    # Only public posts get a post id back; private ones link to the profile.
    post_id = done.get("publicaly_available_post_id") or done.get("publicly_available_post_id")
    post_id = post_id[0] if isinstance(post_id, list) and post_id else post_id
    url = f"{profile}/video/{post_id}" if post_id and profile else profile
    return PublishResult(platform, ok=True, status="uploaded", url=url, remote_id=str(post_id or publish_id))


def publish(video_path: Path, metadata: dict) -> PublishResult:
    tt = metadata.get("platforms", {}).get("tiktok")
    if not tt:
        return PublishResult(platform, ok=False, status="error", error="no tiktok block in sidecar")

    duration = metadata.get("duration_seconds")
    try:
        token = _valid_token()
        try:
            return _post(token["access_token"], video_path, tt, duration)
        except _TokenExpired:
            # Raised only by creator_info / init (before anything is uploaded);
            # _poll swallows it, so this retry can't double-post.
            token = _refresh(token)
            return _post(token["access_token"], video_path, tt, duration)
    except _NeedsLogin as exc:
        log.warning("tiktok: %s — log in again", exc)
        return PublishResult(platform, ok=False, status="needs-login",
                             error=f"{exc} — log in to TikTok again")
    except _TokenExpired as exc:
        return PublishResult(platform, ok=False, status="needs-login",
                             error=f"tiktok token rejected right after refresh ({exc}) — log in to TikTok again")
    except RuntimeError as exc:
        log.error("tiktok: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except requests.HTTPError as exc:
        log.error("tiktok HTTP error: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("tiktok upload failed")
        return PublishResult(platform, ok=False, status="error", error=str(exc))
