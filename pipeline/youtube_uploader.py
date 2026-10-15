"""YouTube Data API v3 upload with OAuth (installed-app / desktop flow)."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from agent.errors import AuthRequired
from config import Config, in_container
from pipeline.models import VideoBrief

log = logging.getLogger(__name__)

# upload  -> insert videos
# readonly -> verify uploads, read statistics, run content research
# analytics -> retention / watch-time data for the weekly report
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
ProgressCallback = Callable[[int, str], None]


def _browser_auth_possible() -> bool:
    """Whether the installed-app consent flow can finish where we are.

    `InstalledAppFlow.run_local_server` opens a browser and then waits for a
    redirect to http://localhost:<random port>. Inside a container there is no
    browser, the redirect would hit the container's own loopback (no port is
    published), so the flow hangs and the token never gets saved — which is
    exactly why Docker used to ask for authentication on every start. Without
    an attached terminal there is nobody to read the URL either.
    """
    if in_container():
        return False
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


class YouTubeUploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------- auth
    def missing_scopes(self) -> list[str]:
        """Scopes the stored token does not have (a refresh can never add scopes)."""
        path = self.cfg.youtube_token_file
        if not path.exists():
            return list(SCOPES)
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return list(SCOPES)
        granted = set(info.get("scopes") or [])
        return [scope for scope in SCOPES if scope not in granted]

    def credentials(self, allow_interactive: bool = True) -> Credentials:
        """Load the saved token, refreshing it when expired; re-consent when scopes changed.

        The saved credentials are always reused: a still-valid token is returned
        as-is and an expired one is refreshed with its refresh token (and written
        back) — no browser, no consent page. Interactive consent is only started
        when it can actually finish: never from a background path
        (`allow_interactive=False` — a worker thread has nobody to click the
        browser), never inside a container and never without an attached
        terminal. In those cases we raise AuthRequired explaining WHY
        reauthorization is needed and where to run it.
        """
        token_path: Path = self.cfg.youtube_token_file
        creds: Credentials | None = None
        reason = ""

        if not token_path.exists():
            reason = f"No saved YouTube token at {token_path}."
        elif self.missing_scopes():
            missing = ", ".join(s.rsplit("/", 1)[-1] for s in self.missing_scopes())
            reason = f"The saved token is missing scopes: {missing}."
        else:
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except Exception as exc:  # noqa: BLE001 - corrupt token file
                reason = f"The saved token file is unreadable ({exc})."
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds, token_path)
                log.info("Refreshed YouTube OAuth token")
            except Exception as exc:  # token revoked -> re-consent
                log.warning("Token refresh failed (%s); re-running consent flow", exc)
                reason = (
                    f"The saved token was rejected while refreshing ({exc}) — it was "
                    "revoked, or the OAuth app is still in \"Testing\" (those refresh "
                    "tokens expire after 7 days)."
                )
                creds = None
        elif creds and creds.expired:
            reason = "The saved token expired and carries no refresh token."
            creds = None

        if not creds or not creds.valid:
            if not self.cfg.youtube_client_secret.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secret: {self.cfg.youtube_client_secret}"
                )
            if not allow_interactive or not _browser_auth_possible():
                raise AuthRequired(self._auth_required_message(reason))
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.cfg.youtube_client_secret), SCOPES
            )
            try:
                creds = flow.run_local_server(
                    port=0,
                    prompt="consent",  # forces a refresh_token even on repeat consent
                    open_browser=True,
                    authorization_prompt_message=(
                        "Open this URL in your browser to authorize YouTube access:\n{url}"
                    ),
                )
            except TypeError:  # older google-auth-oauthlib without `prompt`
                creds = flow.run_local_server(port=0, open_browser=True)
            self._save_token(creds, token_path)
            log.info("YouTube consent completed; token saved with %d scopes", len(SCOPES))

        return creds

    # --------------------------------------------------------- auth messaging
    def _reauth_where(self) -> str:
        """Clause telling the user where the consent flow can actually run."""
        if in_container():
            return " on the HOST machine (outside Docker), then restart the container"
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return " from a terminal with a browser"
        return ""

    def _auth_required_message(self, reason: str) -> str:
        parts = ["YouTube authorization required."]
        if reason:
            parts.append(reason)
        parts.append(
            f"Run: python scripts/youtube_auth.py --force{self._reauth_where()}."
        )
        if in_container():
            parts.append(
                f"The token file ({self.cfg.youtube_token_file}) is shared with the "
                "container through the ./data bind mount, so the freshly granted "
                "token is picked up automatically — a browser can never open inside "
                "the container, which is why it must not ask again here."
            )
        return " ".join(parts)

    def credentials_background(self) -> Credentials:
        """Non-interactive variant for background workers — never opens a browser."""
        return self.credentials(allow_interactive=False)

    def preflight(self) -> dict:
        """Startup health check for the stored token. Never prompts, never raises.

        Returns a dict the caller can print/log so the user learns about an
        auth problem at startup instead of discovering it at publish time.
        """
        token_path = self.cfg.youtube_token_file
        report = {
            "ok": False,
            "token_exists": token_path.exists(),
            "missing_scopes": self.missing_scopes(),
            "has_refresh_token": False,
            "refresh_ok": None,
            "hint": "",
        }

        if not report["token_exists"]:
            report["hint"] = (
                "No YouTube token yet. Run: python scripts/youtube_auth.py"
                f"{self._reauth_where()}."
            )
            return report

        if report["missing_scopes"]:
            report["hint"] = (
                "Token is missing scopes "
                f"({', '.join(s.rsplit('/', 1)[-1] for s in report['missing_scopes'])}). "
                "Run once: python scripts/youtube_auth.py --force"
                f"{self._reauth_where()}."
            )
            return report

        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001 - corrupt token file
            report["hint"] = (
                f"Token file unreadable ({exc}). Re-run with --force"
                f"{self._reauth_where()}."
            )
            return report

        report["has_refresh_token"] = bool(creds.refresh_token)

        # Proactively refresh when expired so the first publish of the day
        # doesn't stall, and so we learn NOW if the token was revoked.
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds, token_path)
                report["refresh_ok"] = True
            except Exception as exc:  # noqa: BLE001
                report["refresh_ok"] = False
                report["hint"] = (
                    f"Refresh token rejected ({exc}). It was likely revoked or the "
                    "OAuth app is still in \"Testing\" (those expire after 7 days) — "
                    "set the consent screen to \"In production\", then run: "
                    f"python scripts/youtube_auth.py --force{self._reauth_where()}."
                )
                return report

        if not report["has_refresh_token"]:
            report["hint"] = (
                "Token has no refresh token, so it will expire within the hour. "
                "Re-run: python scripts/youtube_auth.py --force"
                f"{self._reauth_where()}."
            )
            return report

        report["ok"] = True
        return report

    @staticmethod
    def _save_token(creds: Credentials, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(creds.to_json(), encoding="utf-8")

    # ----------------------------------------------------------------- upload
    def upload(
        self,
        video_path: Path,
        brief: VideoBrief,
        privacy: str,
        progress: ProgressCallback | None = None,
        made_for_kids: bool = False,
        retries: int = 2,
    ) -> str:
        """Upload a Short and return its video ID. `progress(percent, message)` is called during transfer."""
        if privacy not in {"private", "unlisted", "public"}:
            privacy = "private"

        body = {
            "snippet": {
                "title": brief.title[:100],
                "description": brief.description[:4900],
                "tags": brief.tags,
                "categoryId": brief.category_id,
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": made_for_kids,
                "notifySubscribers": True,
            },
        }
        # YouTube only accepts ISO-639-1 codes here ("hinglish" or "en-US" would 400).
        lang = (brief.language or "").strip()
        if re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2})?", lang):
            body["snippet"]["defaultLanguage"] = lang
            body["snippet"]["defaultAudioLanguage"] = lang

        youtube = build("youtube", "v3",
                        credentials=self.credentials(allow_interactive=False),
                        cache_discovery=False)
        media = MediaFileUpload(
            str(video_path),
            chunksize=8 * 1024 * 1024,
            resumable=True,
            mimetype="video/*",
        )
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        last_reported = -1
        attempt = 0
        while True:
            try:
                status, response = request.next_chunk()
            except HttpError as exc:
                if exc.resp.status in (500, 502, 503, 504) and attempt < retries:
                    attempt += 1
                    log.warning("YouTube returned %s, retrying chunk (%d/%d)",
                                exc.resp.status, attempt, retries)
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(_friendly_http_error(exc)) from exc

            if status and progress:
                percent = int(status.progress() * 100)
                if percent != last_reported:
                    last_reported = percent
                    progress(percent, f"Uploading to YouTube... {percent}%")

            if response:
                video_id = response.get("id")
                if not video_id:
                    raise RuntimeError("YouTube accepted the request but returned no video ID.")
                if progress:
                    progress(100, "Upload complete")
                log.info("Uploaded video id=%s privacy=%s", video_id, privacy)
                return video_id

        # unreachable


def _friendly_http_error(exc: HttpError) -> str:
    reason = ""
    try:
        reason = exc.error_details[0].get("reason", "") if exc.error_details else ""
    except Exception:
        pass
    if exc.resp.status == 401:
        return "YouTube authorization expired. Delete data/tokens/youtube_token.json and re-run auth."
    if exc.resp.status == 403 and "quota" in reason.lower():
        return (
            "YouTube daily upload quota exhausted (default is ~10,000 units = ~6 uploads/day). "
            "Try again tomorrow or request a quota increase in Google Cloud Console."
        )
    if exc.resp.status == 403 and "restrictions" in reason.lower():
        return (
            "YouTube rejected this upload: the API project or your channel has a restriction. "
            "Common fix: run scripts/youtube_auth.py with --verify to complete app verification, "
            "and make sure YouTube Data API v3 is enabled for the project."
        )
    return f"YouTube upload failed ({exc.resp.status} {reason or exc.reason})."
