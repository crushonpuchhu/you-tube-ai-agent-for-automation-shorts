"""Central Gemini helper for text/JSON tasks used by the agent modules."""

from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai import types

from config import Config
from pipeline.analyzer import _is_transient, _json_loads_best_effort, _retry_delay

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = genai.Client(api_key=cfg.gemini_api_key)
        self.model = cfg.gemini_model

    def json(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.6,
        max_tokens: int = 8192,
    ) -> dict | list:
        """Blocking JSON completion (run it in a worker thread).

        Retries on transient Gemini failures (503 high demand, 429 rate
        limits, 5xx gateways) with exponential backoff — /research, /report
        and /decide used to die on the first "model is experiencing high
        demand" answer.
        """
        import time

        contents = ([system + "\n\n", prompt] if system else [prompt])
        last_exc: BaseException | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        response_mime_type="application/json",
                    ),
                )
                text = (response.text or "").strip()
                log.debug("LLM response: %s", text[:400])
                return _json_loads_best_effort(text)
            except ValueError:
                raise  # malformed JSON is not something a retry can fix
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_transient(exc) or attempt == _MAX_ATTEMPTS - 1:
                    raise
                last_exc = exc
                delay = _retry_delay(attempt)
                log.warning(
                    "Gemini busy on %s (%s) — retry %d/%d in %.1fs",
                    self.model, str(exc)[:120], attempt + 1, _MAX_ATTEMPTS - 1, delay,
                )
                time.sleep(delay)
        raise last_exc if last_exc else RuntimeError("Gemini request failed.")

    async def json_async(self, prompt: str, **kwargs) -> dict | list:
        return await asyncio.to_thread(self.json, prompt, **kwargs)
