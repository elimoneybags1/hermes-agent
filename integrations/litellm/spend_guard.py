"""Stop routing to a provider once its monthly allowance is spent down to the stop.

catalog.json gives a provider an "allowance" when it is paid per token out of an
included credit (today: anthropic, the Claude Max plan's API credit). Before
LiteLLM picks a deployment, this hook removes every deployment whose Portkey
config reaches a provider that integrations/portkey/spend.py reports at or over
its stop_at_usd. With none left, LiteLLM fails the group over to the tier's
fallbacks, so tier-claude falls back to a free tier instead of spending more.

The second, hard stop is outside Team: the Console organization holding the key
has no card and no auto-reload, so an exhausted credit fails rather than bills.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

_spec = importlib.util.spec_from_file_location(
    "team_spend", Path(__file__).resolve().parent.parent / "portkey" / "spend.py")
spend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spend)
log = logging.getLogger("team.spend_guard")


class SpendGuard(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.ledger = spend.Ledger()

    async def async_filter_deployments(self, model, healthy_deployments, messages,
                                       request_kwargs=None, parent_otel_span=None):
        blocked = self.ledger.blocked_configs()
        if not blocked:
            return healthy_deployments
        out = [d for d in healthy_deployments
               if ((d.get("litellm_params") or {}).get("extra_headers") or {}).get("x-portkey-config") not in blocked]
        if len(out) != len(healthy_deployments):
            log.warning("spend_guard: %s: %d deployment(s) withheld, allowance at its stop",
                        model, len(healthy_deployments) - len(out))
        return out


guard = SpendGuard()
