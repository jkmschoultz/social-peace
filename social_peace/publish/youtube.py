"""YouTube publisher (Shorts = a normal video upload that is <=3 min and 9:16).

SETUP (do this before the first run):
  1. Google Cloud Console -> new project -> enable "YouTube Data API v3".
  2. OAuth consent screen: External, add yourself as a Test user. While the app is
     in "Testing", refresh tokens for the `youtube.upload` scope expire after 7 days
     and uploads are forced to `private`. Submitting for verification lifts both.
  3. Credentials -> Create OAuth client ID -> type "Desktop app" -> download JSON to
     the path in YOUTUBE_CLIENT_SECRET_FILE (default secrets/youtube_client_secret.json).
  4. `pip install -e .[youtube]`, then run any publish once to do the consent flow in
     a browser; the refresh token is cached at YOUTUBE_TOKEN_FILE.

QUOTA: default 10,000 units/day. A video insert costs ~1,600 units, so ~6 uploads/day
before you must request more. Plan cadence around that.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
platform = "youtube"

# YouTube rejects '<' and '>' anywhere in snippet.title / snippet.description
# (reason: invalidTitle / invalidDescription). Swap in look-alikes so "<3" etc.
# survive review-UI edits.
_ANGLE = str.maketrans({"<": "‹", ">": "›"})


def _clean(text: str) -> str:
    return (text or "").translate(_ANGLE)


def _load_credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "secrets/youtube_token.json"))
    client_file = Path(
        os.environ.get("YOUTUBE_CLIENT_SECRET_FILE", "secrets/youtube_client_secret.json")
    )

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Testing-mode consent screens revoke refresh tokens after 7 days
            # (invalid_grant) — fall through to a fresh consent flow.
            log.warning("youtube refresh token rejected (%s) — re-running consent flow", exc)
            creds = None
    if not (creds and creds.valid):
        if not client_file.exists():
            raise FileNotFoundError(
                f"{client_file} missing — download the OAuth client secret (see module docstring)"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _client():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_load_credentials(), cache_discovery=False)


def publish(video_path: Path, metadata: dict) -> PublishResult:
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        return PublishResult(
            platform, ok=False, status="error",
            error="google-api-python-client not installed. Run: pip install -e .[youtube]",
        )

    yt = metadata.get("platforms", {}).get("youtube")
    if not yt:
        return PublishResult(platform, ok=False, status="error", error="no youtube block in sidecar")

    body = {
        "snippet": {
            "title": _clean(yt["title"])[:100],
            "description": _clean(yt["description"])[:5000],
            "tags": yt.get("tags", []),
            "categoryId": yt.get("categoryId", "22"),
        },
        "status": {
            "privacyStatus": yt.get("privacyStatus", "public"),
            "selfDeclaredMadeForKids": bool(yt.get("madeForKids", False)),
        },
    }

    try:
        client = _client()
        media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        request = client.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info("youtube upload %d%%", int(status.progress() * 100))
        vid = response["id"]
        return PublishResult(
            platform, ok=True, status="uploaded",
            url=f"https://youtu.be/{vid}", remote_id=vid,
        )
    except HttpError as exc:
        log.error("youtube API error: %s", exc)
        return PublishResult(platform, ok=False, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("youtube upload failed")
        return PublishResult(platform, ok=False, status="error", error=str(exc))
