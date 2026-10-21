"""YouTube Data API v3 upload with OAuth (installed-app / desktop flow).

Authentication design
---------------------
The access token Google hands out expires after ~1 hour, so the agent never
stores or trusts it for long: every authenticated operation goes through
`credentials()`, which loads the saved user token from
``data/tokens/youtube_token.json`` and refreshes it — using the long-lived
*refresh token* — when it has expired or is about to (configurable leeway).
A refreshed token is written back atomically, so it survives restarts and AWS
reboots (the file lives on the ./data bind mount).

When Google *rejects the refresh token itself* (``invalid_grant: Token has
been expired or revoked``) no code can recover automatically — the user must
re-consent. That is reported as :class:`AuthRequired` with a precise manual
recovery recipe and a Telegram notification (deduplicated in time so one
problem never spams the chat). Transient network failures while refreshing
are retried and then surfaced as ordinary errors — they are NOT treated as
authentication problems, so unrelated agent functions keep running.

Most ``invalid_grant`` cases in practice come from the OAuth consent screen
being in *Testing* status: Google expires those refresh tokens after 7 days.
Publishing the app to *In production* is the durable fix.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

try:  # httpx is already a dependency (python-telegram-bot / google-genai)
    import httpx
    _NETWORK_ERRORS: tuple[type[BaseException], ...] = (
        OSError, TimeoutError, ConnectionError, httpx.HTTPError,
    )
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = (OSError, TimeoutError, ConnectionError)

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

#: Refresh the access token this many seconds BEFORE Google says it expires.
#: Proactive refresh means a long upload never dies half-way on a stale token.
DEFAULT_LEEWAY_SECONDS = 10 * 60

#: Transient (network) refresh failures are retried this many times before the
#: operation gives up — a flaky connection must not be mistaken for a dead
#: refresh token.
_REFRESH_RETRIES = 3
_REFRESH_BACKOFF_S = 2.0

#: Google error responses that mean "this refresh token will never work again".
#: Anything else (timeouts, resets, 5xx) is treated as transient.
_PERMANENT_MARKERS = (
    "invalid_grant", "invalid_client", "unauthorized_client",
    "invalid_scope", "deleted_client", "token has been expired or revoked",
)

#: Do not repeat the same "auto refreshed" / "recovered" Telegram notice more
#: often than this, even across many background tasks.
_NOTIFY_WINDOW_S = 6 * 3600


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


def _is_permanent_rejection(text: str) -> bool:
    """True when Google said the refresh token itself is unusable."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PERMANENT_MARKERS)


def _redact(text: str, limit: int = 300) -> str:
    """Log/notify-safe exception text (no token material can appear in Google
    error strings, but be defensive anyway and cap the length)."""
    cleaned = re.sub(r"(ya29\.[A-Za-z0-9._-]+|1//[^?\s]+)", "[redacted]", text or "")
    cleaned = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", cleaned)
    return cleaned.strip()[:limit]


class YouTubeUploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.RLock()
        # Set while Google is rejecting our refresh token; cleared on recovery.
        self._auth_degraded = False
        # True when a token refresh happened during the current operation —
        # the Publisher surfaces this in its success message.
        self._refreshed_in_flight = False
        self._last_refresh_at: datetime | None = None
        # Injected by bot.app.build_application() so this module never imports
        # Telegram/DB code itself (keeps scripts/youtube_auth.py lightweight).
        self._notify: Callable[[str], None] | None = None
        self._claim: Callable[[str, float], bool] | None = None

    @property
    def refresh_leeway_seconds(self) -> int:
        return max(0, int(self.cfg.youtube_leeway_minutes * 60))

    # ------------------------------------------------------- notification glue
    def bind_notifications(self, notifier=None, claim=None) -> None:
        """Wire Telegram notifications + time-windowed deduplication.

        `claim(key, window_seconds) -> bool` returns True when a notification
        with that key may be sent now (first use, or last use older than the
        window). Both are optional: without them the agent only logs.
        """
        if notifier is not None:
            self._notify = notifier.send
        if claim is not None:
            self._claim = claim

    def _send_auth_notification(self, key: str, message: str) -> None:
        """Send a notification unless the same problem was already reported
        inside the dedupe window."""
        if self._notify is None:
            return
        claimed = True
        if self._claim is not None:
            try:
                claimed = bool(self._claim(key, _NOTIFY_WINDOW_S))
            except Exception:  # noqa: BLE001 - dedupe must never block reporting
                claimed = True
        if claimed:
            self._notify(message)

    def consume_refresh_flag(self) -> bool:
        """True when the token was refreshed during the last operation."""
        flag = self._refreshed_in_flight
        self._refreshed_in_flight = False
        return flag

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

    @staticmethod
    def _needs_refresh(creds: Credentials, leeway_seconds: int) -> bool:
        """True when the token is expired or expires within the leeway.

        A missing expiry (older token formats) counts as "refresh when the
        API rejects it" — googleapiclient does that automatically — so we only
        force a refresh when we actually know time is up.
        """
        if not creds.valid:
            return True
        if creds.expiry is None:
            return False
        expiry = creds.expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return (expiry - datetime.now(timezone.utc)).total_seconds() < leeway_seconds

    def credentials(self, allow_interactive: bool = True) -> Credentials:
        """Load the saved token, refreshing it when expired; re-consent when scopes changed.

        The saved credentials are always reused: a still-valid token is
        returned as-is and an expired one is refreshed with its refresh token
        (and written back) — no browser, no consent page. Interactive consent
        is only started when it can actually finish: never from a background
        path (`allow_interactive=False` — a worker thread has nobody to click
        the browser), never inside a container and never without an attached
        terminal. In those cases we raise AuthRequired explaining WHY
        reauthorization is needed and where to run it.

        Failure classification (important for the agent's recovery behaviour):
        * Google rejects the refresh token (invalid_grant/…)  -> AuthRequired
        * the network fails while refreshing                  -> RuntimeError
          (retryable; the agent must not declare auth broken on a timeout)
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
                reason = f"The saved token file is unreadable ({_redact(str(exc))})."
                creds = None

        if creds is not None and self._needs_refresh(creds, self.refresh_leeway_seconds):
            if creds.refresh_token:
                # Raises AuthRequired (rejection) or RuntimeError (network).
                self._refresh(creds, token_path)
            else:
                reason = (
                    "The saved token expired and carries no refresh token — the "
                    "consent screen likely skipped it (Testing-mode apps that were "
                    "never used in 7 days can lose it)."
                )
                creds = None

        if not creds or not creds.valid:
            if not self.cfg.youtube_client_secret.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secret: {self.cfg.youtube_client_secret}"
                )
            if not allow_interactive or not _browser_auth_possible():
                raise AuthRequired(self._auth_required_message(reason))
            creds = self._run_consent_flow()
            self._mark_auth_recovered(force=True)

        self._install_persistent_refresh(creds)
        return creds

    def _refresh(self, creds: Credentials, token_path: Path) -> None:
        """Refresh with retries; persist on success; classify on failure.

        Transient network errors are retried with backoff. A permanent
        rejection raises AuthRequired with the manual recovery recipe and
        notifies Telegram (time-deduplicated).
        """
        was_expired = bool(creds.expired)
        last_error: Exception | None = None

        for attempt in range(_REFRESH_RETRIES):
            try:
                with self._lock:
                    creds.refresh(Request())
                    self._save_token(creds, token_path)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                text = _redact(str(exc))
                if _is_permanent_rejection(text):
                    self._auth_degraded = True
                    log.error("YouTube refresh token rejected by Google: %s", text)
                    self._send_auth_notification(
                        "auth:youtube:refresh", self._rejection_message(exc)
                    )
                    raise AuthRequired(self._auth_required_message(
                        f"The saved token was rejected while refreshing ({text})."
                    )) from exc
                last_error = exc
                if attempt < _REFRESH_RETRIES - 1:
                    delay = _REFRESH_BACKOFF_S * (2 ** attempt)
                    log.warning(
                        "YouTube token refresh attempt %d/%d failed (%s) — retrying in %.0fs",
                        attempt + 1, _REFRESH_RETRIES, text, delay,
                    )
                    time.sleep(delay)

        if last_error is not None:
            # Transient: the connection, not the credentials, is broken. Do NOT
            # declare auth dead — the caller/scheduler retries safely.
            log.warning("YouTube token refresh failed on network: %s", _redact(str(last_error)))
            raise RuntimeError(
                f"Could not reach Google to refresh the YouTube token "
                f"({_redact(str(last_error), 160)}). This is a network problem, not an "
                "authentication problem — the agent will retry."
            ) from last_error

        self._last_refresh_at = datetime.now(timezone.utc)
        self._refreshed_in_flight = True
        log.info(
            "Refreshed YouTube OAuth token (%s, saved to %s)",
            "it had expired" if was_expired else "proactively before expiry",
            token_path.name,
        )
        if was_expired:
            # The token ran out mid-idle; tell the owner it renewed itself and
            # work continues. Time-deduplicated so analytics sync loops stay
            # quiet after the first report.
            self._send_auth_notification(
                "auth:youtube:auto-refreshed",
                "🔁 <b>YouTube token refreshed automatically</b>\n\n"
                "The access token had expired; the agent renewed it with the saved "
                "refresh token and is continuing its work. No action needed.",
            )
        if self._auth_degraded:
            self._mark_auth_recovered()

    def _mark_auth_recovered(self, force: bool = False) -> None:
        """Clear the degraded flag and announce the recovery once."""
        if not self._auth_degraded and not force:
            return
        self._auth_degraded = False
        self._send_auth_notification(
            "auth:youtube:recovered",
            "✅ <b>YouTube authentication recovered</b>\n\n"
            "Google is accepting the saved token again — uploads, analytics and "
            "scheduled Shorts continue automatically.",
        )

    def _run_consent_flow(self) -> Credentials:
        """Interactive browser consent (host machine with a terminal only)."""
        token_path = self.cfg.youtube_token_file
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

    def _install_persistent_refresh(self, creds: Credentials) -> None:
        """Persist the token after refreshes triggered inside googleapiclient.

        During a long upload the API client refreshes an expired access token
        itself; without this hook the new token would live only in memory and
        the next restart would need an extra refresh round-trip.
        """
        try:
            original_refresh = creds.refresh

            def _refresh_and_save(request):
                original_refresh(request)
                with self._lock:
                    self._save_token(creds, self.cfg.youtube_token_file)
                self._refreshed_in_flight = True
                log.info("Refreshed YouTube OAuth token (mid-request) and persisted it")

            creds.refresh = _refresh_and_save  # type: ignore[method-assign]
        except Exception:  # noqa: BLE001 - best effort; the token still works
            pass

    # --------------------------------------------------------- auth messaging
    def _reauth_where(self) -> str:
        """Clause telling the user where the consent flow can actually run."""
        if in_container():
            return " on the HOST machine (outside Docker), then restart the container"
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return " from a terminal with a browser"
        return ""

    def _console_project_hint(self) -> str:
        """Google Cloud console link for THIS OAuth project (from the client id)."""
        try:
            info = json.loads(
                self.cfg.youtube_token_file.read_text(encoding="utf-8")
            )
            client_id = info.get("client_id") or ""
        except Exception:  # noqa: BLE001
            client_id = ""
        if not client_id and self.cfg.youtube_client_secret.exists():
            try:
                secret = json.loads(
                    self.cfg.youtube_client_secret.read_text(encoding="utf-8")
                )
                block = secret.get("installed") or secret.get("web") or {}
                client_id = block.get("client_id") or ""
            except Exception:  # noqa: BLE001
                pass
        project = client_id.split("-")[0] if client_id.isdigit() or (
            client_id and client_id.split("-")[0].isdigit()
        ) else ""
        if not project:
            return "https://console.cloud.google.com/apis/credentials"
        return f"https://console.cloud.google.com/apis/credentials?project={project}"

    def _rejection_message(self, exc: BaseException) -> str:
        """Telegram notice for a genuinely dead refresh token (HTML, no secrets)."""
        detail = html.escape(_redact(str(exc), 200))
        return (
            "🔐 <b>YouTube authentication needs your action</b>\n\n"
            "What happened: Google rejected the saved refresh token "
            f"(<code>{detail}</code>).\n"
            "Most common cause: the OAuth app's consent screen is still in "
            "<b>Testing</b> — Google expires those refresh tokens after 7 days. "
            "It can also mean the token was revoked (password change, or "
            "<i>Settings → Security → Third-party access</i>).\n\n"
            "<b>Fix (one time, ~2 minutes):</b>\n"
            f"1. Open <a href=\"{html.escape(self._console_project_hint())}\">"
            "Google Cloud → OAuth consent screen</a> and set "
            "<b>Publishing status → In production</b>.\n"
            "2. Make sure <b>YouTube Data API v3</b> is enabled in the project.\n"
            f"3. On the HOST machine run:<code> python scripts/youtube_auth.py "
            f"--force</code>{html.escape(self._reauth_where())}.\n\n"
            "Until then publishing/analytics pause and retry automatically; "
            "Telegram, research and everything else keep working."
        )

    def _auth_required_message(self, reason: str) -> str:
        parts = ["YouTube authorization required."]
        if reason:
            parts.append(_redact(reason))
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
        if _is_permanent_rejection(reason):
            parts.append(
                "Hint: if the OAuth consent screen is in \"Testing\", set it to "
                "\"In production\" first — testing-mode refresh tokens die after "
                "7 days and this will keep recurring otherwise."
            )
        return " ".join(parts)

    def credentials_background(self) -> Credentials:
        """Non-interactive variant for background workers — never opens a browser."""
        return self.credentials(allow_interactive=False)

    def background_refresh(self) -> dict:
        """Proactive health check used by the scheduler every ~45 minutes.

        Keeps the access token fresh even when no Short is published for days,
        spreads the token lifetime over many small refreshes, and — most
        importantly — discovers a revoked token within 45 minutes instead of
        at publish time. Never raises.
        """
        try:
            creds = self.credentials(allow_interactive=False)
            expiry = creds.expiry.isoformat() if creds.expiry else None
            return {"ok": True, "auth": False, "expiry": expiry}
        except AuthRequired as exc:
            return {"ok": False, "auth": True, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - network / unexpected
            return {"ok": False, "auth": False, "error": _redact(str(exc), 400)}

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
                f"Token file unreadable ({_redact(str(exc))}). Re-run with --force"
                f"{self._reauth_where()}."
            )
            return report

        report["has_refresh_token"] = bool(creds.refresh_token)

        # Proactively refresh when expired (or nearly) so the first publish of
        # the day doesn't stall, and so we learn NOW if the token was revoked.
        if self._needs_refresh(creds, self.refresh_leeway_seconds) and creds.refresh_token:
            try:
                with self._lock:
                    creds.refresh(Request())
                    self._save_token(creds, token_path)
                report["refresh_ok"] = True
            except Exception as exc:  # noqa: BLE001
                text = _redact(str(exc))
                if _is_permanent_rejection(text):
                    self._auth_degraded = True
                    report["refresh_ok"] = False
                    report["hint"] = (
                        f"Refresh token rejected ({text}). It was revoked or the "
                        "OAuth app is still in \"Testing\" (those expire after 7 "
                        "days) — set the consent screen to \"In production\", then "
                        f"run: python scripts/youtube_auth.py --force{self._reauth_where()}."
                    )
                    return report
                # Transient network problem: the token may still be fine.
                report["refresh_ok"] = False
                report["hint"] = (
                    f"Could not reach Google to refresh the token ({text[:120]}). "
                    "This looks like a network issue, not an auth problem — the "
                    "agent will keep retrying in the background."
                )

        if not report["has_refresh_token"]:
            report["hint"] = (
                "Token has no refresh token, so it will expire within the hour. "
                "Re-run: python scripts/youtube_auth.py --force"
                f"{self._reauth_where()}."
            )
            return report

        report["ok"] = report["hint"] == ""
        return report

    @staticmethod
    def _save_token(creds: Credentials, path: Path) -> None:
        """Atomically persist credentials with owner-only permissions.

        Write-to-temp + rename so a crash mid-write can never truncate the
        token the whole agent depends on; chmod 600 because the file carries
        the OAuth refresh token.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(creds.to_json(), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:  # read-only mount etc: keep working in memory
            log.warning("Could not persist the YouTube token to %s: %s", path, _redact(str(exc)))

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
                if exc.resp.status == 401:
                    # googleapiclient already tried one silent refresh; a
                    # second 401 means the credentials are genuinely rejected.
                    raise AuthRequired(
                        "YouTube rejected the access token during the upload (401). "
                        f"{self._auth_required_message('The token is no longer accepted.')}"
                    ) from exc
                if exc.resp.status in (500, 502, 503, 504) and attempt < retries:
                    attempt += 1
                    log.warning("YouTube returned %s, retrying chunk (%d/%d)",
                                exc.resp.status, attempt, retries)
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(_friendly_http_error(exc)) from exc
            except _NETWORK_ERRORS as exc:
                # httpx.ReadError / resets mid-transfer: the resumable upload
                # keeps its offset, so re-asking for the next chunk resumes
                # where it stopped instead of losing the whole upload.
                if attempt < retries:
                    attempt += 1
                    log.warning("Network error during upload (%s), retrying chunk (%d/%d)",
                                exc, attempt, retries)
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"Network error during the YouTube upload: {exc}. "
                    "Check the server's internet connection and try again."
                ) from exc

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
