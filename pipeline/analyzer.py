"""Video analysis with Google Gemini: watch the clip, produce YouTube metadata."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path

from google import genai
from google.genai import types

from config import Config
from pipeline.models import VideoBrief

log = logging.getLogger(__name__)

# Transient upstream failures worth retrying: Gemini overload (503), rate
# limits (429) and flaky 5xx gateways. These are temporary by definition.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BASE_DELAY_S = 3.0


def _is_transient(exc: BaseException) -> bool:
    """True for errors a retry can actually fix (Gemini busy / rate limited)."""
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if code in _RETRYABLE_CODES:
        return True
    # Fall back to message sniffing when the SDK doesn't expose a code.
    text = str(exc).lower()
    return any(tok in text for tok in
               ("503", "unavailable", "overloaded", "high demand",
                "429", "rate limit", "quota", "resource_exhausted", "502", "504"))


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter so parallel workers don't sync up."""
    return _BASE_DELAY_S * (2 ** attempt) + random.uniform(0, 1.5)

# Valid YouTube video category IDs (US). The model picks the best fit.
CATEGORIES = {
    "1": "Music",
    "2": "Autos & Vehicles",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}

SYSTEM_PROMPT = """\
You are a YouTube Shorts growth strategist and viral content analyst.
You will receive a short vertical video. Watch it AND listen to its audio, then write
the complete metadata package that gives the upload the best chance of going viral.

Return ONLY a raw JSON object (no markdown fences, no commentary) with exactly these keys:
{
  "title": "click-worthy title, max 90 characters, no hashtags, no clickbait lies",
  "description": "full YouTube description",
  "hashtags": ["#example", "..."],
  "tags": ["keyword", "..."],
  "category_id": "one of the allowed category ids",
  "language": "BCP-47-ish code of the spoken language, e.g. en, hi, hinglish",
  "hook_summary": "one sentence describing what happens in the video"
}

Rules for each field:
- title: front-load the strongest keyword, create curiosity or state a clear payoff.
  No "#shorts" in the title, no ALL CAPS words except acronyms, no emojis at the start.
- description:
    The description is ONLY about the video's content — what happens in it and
    what the viewer gets. Never mention scheduling, publishing times, upload
    dates or "stay tuned for tomorrow": timing is handled by the scheduler
    separately and must not leak into this text.
    Line 1: a 1-2 sentence hook (this shows above the fold).
    Middle: what the viewer will get, key moments, context, and a call to action
            (like, comment, follow for more).
    End: a hashtag block of 3-8 of the most relevant hashtags, one per line or inline.
    Keep the whole description under 900 characters.
- hashtags: 10-15 total, mix broad viral tags (#fyp #viral #trending #shorts) with
  niche tags that actually match this video. Always include "#shorts". No duplicates.
- tags: 20-30 search phrases people actually type, ordered most->least important.
  Total joined length must stay under 450 characters (YouTube limit is 500).
- category_id: exactly one of: __CATEGORIES__.
- Write title/description in the same language as the audio (Hinglish stays Hinglish).
- Never invent facts that are not visible or audible in the video.
"""


class VideoAnalyzer:
    """Uploads a clip to Gemini and turns it into a VideoBrief."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = genai.Client(api_key=cfg.gemini_api_key)
        self.model = cfg.gemini_model

    # ---------------------------------------------------------------- async API
    async def analyze(self, video_path: Path, hint: str = "") -> VideoBrief:
        """Upload + analyze. Runs blocking SDK calls in a worker thread."""
        file_ref = await asyncio.to_thread(self._upload, video_path)
        file_ref = await asyncio.to_thread(self._wait_until_active, file_ref)

        prompt = SYSTEM_PROMPT.replace(
        "__CATEGORIES__", ", ".join(f"{k}={v}" for k, v in CATEGORIES.items())
    )
        if hint and hint.strip():
            prompt += (
                "\nAdditional direction from the creator (treat as priority context):\n"
                f'"{hint.strip()}"\n'
            )
        prompt += "\nAnalyze the video now and return the JSON object."

        text = await asyncio.to_thread(self._generate_with_retry, file_ref, prompt)
        log.info("Gemini raw response (%d chars): %s", len(text), text[:400])
        return self._parse(text)

    def _generate_with_retry(self, file_ref, prompt: str) -> str:
        """Call Gemini, retrying while it is overloaded.

        The video is already uploaded at this point, so a retry only re-sends
        the prompt — cheap, and it turns a hard 503 failure into a wait.
        """
        last_exc: BaseException | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[file_ref, prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.9,
                        max_output_tokens=4096,
                        response_mime_type="application/json",
                    ),
                )
                return (response.text or "").strip()
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_transient(exc) or attempt == _MAX_ATTEMPTS - 1:
                    raise
                last_exc = exc
                delay = _retry_delay(attempt)
                log.warning(
                    "Gemini busy (%s) — retry %d/%d in %.1fs",
                    str(exc)[:120], attempt + 1, _MAX_ATTEMPTS - 1, delay,
                )
                time.sleep(delay)
        raise last_exc if last_exc else RuntimeError("Gemini analysis failed.")

    # ------------------------------------------------------------- SDK plumbing
    def _upload(self, path: Path):
        log.info("Uploading %s (%.1f MB) to Gemini", path.name, path.stat().st_size / 1e6)
        return self.client.files.upload(file=str(path))

    def _wait_until_active(self, file_ref, timeout_s: int = 600):
        import time

        deadline = time.time() + timeout_s
        while getattr(file_ref, "state", None) and file_ref.state.name == "PROCESSING":
            if time.time() > deadline:
                raise TimeoutError("Gemini took too long processing the video.")
            time.sleep(2)
            file_ref = self.client.files.get(name=file_ref.name)
        if getattr(file_ref, "state", None) and file_ref.state.name == "FAILED":
            raise RuntimeError("Gemini could not process this video file.")
        return file_ref

    # ----------------------------------------------------------------- parsing
    def _parse(self, text: str) -> VideoBrief:
        data = _json_loads_best_effort(text)

        hashtags = _clean_hashtags(data.get("hashtags") or [])
        if "#shorts" not in hashtags:
            hashtags.append("#shorts")

        tags = _clean_tags(data.get("tags") or [])

        title = str(data.get("title") or "").strip() or "My YouTube Short"
        if len(title) > 100:
            title = title[:97].rstrip() + "..."

        category = str(data.get("category_id") or self.cfg.youtube_default_category_id).strip()
        if category not in CATEGORIES:
            category = self.cfg.youtube_default_category_id

        description = str(data.get("description") or "").strip()
        # Make sure #shorts is always present in the description too.
        if "#shorts" not in description.lower():
            description = f"{description}\n\n#shorts".strip()

        return VideoBrief(
            title=title,
            description=description,
            hashtags=hashtags,
            tags=tags,
            category_id=category,
            language=str(data.get("language") or "en"),
            hook_summary=str(data.get("hook_summary") or "").strip(),
        )


# --------------------------------------------------------------------- helpers
def _json_loads_best_effort(text: str) -> dict:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Strip ```json fences if the model added them anyway.
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError("Gemini did not return valid JSON metadata for this video.")


def _clean_hashtags(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        word = str(item).strip().lstrip("#").replace(" ", "")
        if not word or len(word) > 40:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(f"#{word}")
        if len(result) >= 15:
            break
    return result


def _clean_tags(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    budget = 440  # leave headroom under YouTube's 500-char tag limit
    for item in items:
        tag = str(item).strip().strip("#")
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        added = len(tag) + 1
        if budget - added < 0:
            break
        seen.add(key)
        result.append(tag)
        budget -= added
        if len(result) >= 30:
            break
    return result
