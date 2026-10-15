"""One-time YouTube OAuth setup (also used to upgrade scopes).

Run:      python scripts/youtube_auth.py
Re-auth:  python scripts/youtube_auth.py --force   (after changing scopes or if revoked)

Saves the refresh token so the bot can upload, verify publications, read
statistics and fetch YouTube Analytics without logging in again.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from agent.errors import AuthRequired  # noqa: E402
from pipeline.youtube_uploader import SCOPES, YouTubeUploader  # noqa: E402


def main() -> None:
    cfg = Config.load()

    if not cfg.youtube_client_secret.exists():
        raise SystemExit(
            f"Missing {cfg.youtube_client_secret}\n"
            "Download it from Google Cloud Console -> APIs & Services -> Credentials -> "
            "OAuth client ID -> Desktop app."
        )

    token = cfg.youtube_token_file
    if token.exists() and "--force" not in sys.argv:
        uploader = YouTubeUploader(cfg)
        missing = uploader.missing_scopes()
        if not missing:
            print(f"Token already exists and has all {len(SCOPES)} scopes: {token}")
            print("Delete that file or run with --force to re-authorize.")
            return
        print("Existing token is missing scopes:")
        for scope in missing:
            print(f"  - {scope}")
        print("Re-running the consent flow…\n")

    # --force swaps the token only when the NEW consent succeeds: keep a backup
    # so a failed attempt (Ctrl-C, rejected consent, container/headless machine)
    # never leaves the agent without working credentials.
    backup: str | None = None
    if "--force" in sys.argv and token.exists():
        backup = token.read_text(encoding="utf-8")
        token.unlink()
        print("Existing token removed; running a fresh consent flow…")

    uploader = YouTubeUploader(cfg)
    try:
        creds = uploader.credentials(allow_interactive=True)  # opens the browser
    except BaseException as exc:
        if backup is not None:
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(backup, encoding="utf-8")
            print("⚠ Consent did not finish — the previous token was restored.")
        if isinstance(exc, AuthRequired):
            # Container / headless machine: the browser flow cannot finish here,
            # so say exactly why and where to authorize instead of hanging on a
            # URL nobody can open.
            raise SystemExit(f"\n🔐 {exc}") from None
        raise
    remaining = YouTubeUploader(cfg).missing_scopes()
    print("\n✅ YouTube authorization saved.")
    print(f"   token file : {cfg.youtube_token_file}")
    print(f"   scopes     : {', '.join((creds.scopes or SCOPES))}")
    if remaining:
        print(f"   ⚠ still missing: {', '.join(remaining)}")
    else:
        print("   ✅ all required scopes granted (upload, verify, analytics)")
    print("You can now run:  python main.py")


if __name__ == "__main__":
    main()
