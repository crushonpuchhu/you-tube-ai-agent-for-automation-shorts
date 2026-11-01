"""Autonomous fitness/content workflow (feature-additive package).

Everything in this package is new and optional.  The rest of the agent keeps
working unchanged when every feature flag is left at its default (OFF).

Public entry points used by the existing application:

* :mod:`features.autonomous_config`      -- environment/feature configuration
* :mod:`features.autonomous_pipeline`    -- ``run_autonomous_cycle()``
* :mod:`features.autonomous_scheduler`   -- hooks used by ``agent.scheduler``
* :mod:`features.telegram_autonomous_hooks` -- ``/autopilot_*`` commands
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Version of the additive feature set (the JSON specification is v4.0).
__version__ = "4.0.0"
