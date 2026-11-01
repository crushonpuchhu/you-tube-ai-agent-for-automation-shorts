"""Regression tests for the manual /autopilot_run path.

The scheduled path called run_autonomous_cycle(deps) correctly, but the manual
path passed `manual` positionally and died with:
    TypeError: run_autonomous_cycle() takes 1 positional argument but 2 were given
so every /autopilot_run failed after the acknowledgement message.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from features.autonomous_pipeline import run_autonomous_cycle
from features.telegram_autonomous_hooks import _run_manual_cycle


class _DisabledDeps:
    """Minimal deps: cycle returns immediately because the flag is off."""

    aconf = SimpleNamespace(enabled=False, bootstrap_niche="fitness")
    store = SimpleNamespace(is_paused=lambda: False)


class ManualCycleSignatureTests(unittest.TestCase):
    def test_manual_is_keyword_only(self):
        result = run_autonomous_cycle(_DisabledDeps(), manual=True)
        self.assertEqual(result.status, "disabled")

    def test_positional_manual_is_rejected(self):
        with self.assertRaises(TypeError):
            run_autonomous_cycle(_DisabledDeps(), True)  # type: ignore[call-arg]

    def test_run_manual_cycle_does_not_raise(self):
        # Regression: this used to raise TypeError inside the worker thread.
        asyncio.run(_run_manual_cycle(None, _DisabledDeps()))


if __name__ == "__main__":
    unittest.main()
