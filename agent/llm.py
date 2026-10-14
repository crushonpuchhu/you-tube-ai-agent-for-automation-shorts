"""Central Gemini helper for text/JSON tasks used by the agent modules."""

from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai import types

from config import Config
from pipeline.analyzer import _json_loads_best_effort

log = logging.getLogger(__name__)


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
    ) -> dict:
        """Blocking JSON completion (run it in a worker thread)."""
        contents = ([system + "\n\n", prompt] if system else [prompt])
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

    async def json_async(self, prompt: str, **kwargs) -> dict:
        return await asyncio.to_thread(self.json, prompt, **kwargs)
