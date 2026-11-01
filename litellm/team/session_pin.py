"""Keep each session (a Team job) on the model that last answered it.

LiteLLM's own session_affinity only pins when every deployment in a group is the
same underlying model (DeploymentAffinityCheck._get_stable_model_map_key_from_deployments
returns None for mixed groups), and each tier here mixes models, so jobs switched
model on nearly every call. This pins per (session, tier group) instead.

The pinned deployment is moved to the front of the order rather than returned
alone: if it is cooling down it is already absent, and if LiteLLM's later checks
drop it (prompt too large) or it failed earlier in this request, the normal order
still has the rest of the tier. After any success the pin moves to the model that
answered, so a job that failed over stays on its new model instead of bouncing back.
"""

from __future__ import annotations

import copy
import logging
import os
import time

from litellm.integrations.custom_logger import CustomLogger
from litellm.router_utils.pre_call_checks.deployment_affinity_check import DeploymentAffinityCheck

# policy.litellm.session_pin_ttl_s in catalog.json, exported by run.sh
TTL_S = int(os.environ.get("TEAM_SESSION_PIN_TTL_S") or 2 * 3600)
FRONT = -1  # below every configured order (they start at 1)
log = logging.getLogger("team.session_pin")


class SessionPin(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.pins: dict[tuple[str, str], tuple[str, float]] = {}

    def _get(self, session: str, group: str) -> str | None:
        hit = self.pins.get((session, group))
        if hit is None or hit[1] < time.time():
            return None
        return hit[0]

    async def async_filter_deployments(self, model, healthy_deployments, messages,
                                       request_kwargs=None, parent_otel_span=None):
        kwargs = request_kwargs or {}
        session = DeploymentAffinityCheck._get_session_id_from_request_kwargs(kwargs)
        pinned = self._get(session, model) if session else None
        if not pinned:
            return healthy_deployments
        ids = [str((d.get("model_info") or {}).get("id")) for d in healthy_deployments]
        # Say why a pin goes unused, so every model switch in usage.db can be explained.
        if kwargs.get("_target_order") is not None:  # LiteLLM is already failing over by order
            log.warning("session_pin: %s %s pinned %s not applied: failing over by order", session, model, pinned)
            return healthy_deployments
        if pinned not in ids:
            log.warning("session_pin: %s %s pinned %s not applied: not among healthy %s",
                        session, model, pinned, ids)
            return healthy_deployments
        out = []
        for d in healthy_deployments:
            if str((d.get("model_info") or {}).get("id")) == pinned:
                d = copy.copy(d)
                d["litellm_params"] = {**d.get("litellm_params", {}), "order": FRONT}
            out.append(d)
        return out

    @staticmethod
    def _call(kwargs) -> tuple[str | None, str | None, str | None]:
        slo = kwargs.get("standard_logging_object") or {}
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        # The same session id the filter reads; the logging payload's session_id can be empty.
        session = meta.get("session_id") or slo.get("session_id") or slo.get("trace_id")
        return session, slo.get("model_id"), meta.get("deployment_model_name")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        # The pinned model failed: unpin so this request's retry, and the next turn,
        # take the normal order within the tier rather than retrying it at the front.
        session, deployment, group = self._call(kwargs)
        if session and deployment and group and self._get(str(session), group) == str(deployment):
            self.pins.pop((str(session), group), None)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        session, deployment, group = self._call(kwargs)
        if session and deployment and group:
            self.pins[(str(session), group)] = (str(deployment), time.time() + TTL_S)
            if len(self.pins) > 5000:  # drop expired pins now and then
                now = time.time()
                self.pins = {k: v for k, v in self.pins.items() if v[1] >= now}


pin = SessionPin()
