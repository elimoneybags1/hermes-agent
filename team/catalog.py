"""The free-model catalog, as Team reads it.

``integrations/portkey/catalog.json`` is the only source of which models
exist anywhere in the system (PLAN.md Phase 2). The gateway session owns the
file and the generator; this module is how Team validates a model before it
ever reaches ``kanban create --model``, which would otherwise bypass the
profile pin. It fails closed: anything not resolvable here is refused.

A caller names a model by *spec*: ``tier-<t>`` (LiteLLM picks the model) or
``<provider>:<id>`` (one catalog model, served by that provider first).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

FREE = frozenset({"yes", "allowance"})
PROVIDER_NAME = "litellm"


class GateError(ValueError):
    """A model spec the catalog does not allow. Nothing is dispatched."""


class ModelChoice(NamedTuple):
    """What ``team run`` passes to kanban: the Hermes provider name and the model."""

    spec: str
    provider: str
    model: str
    why: str

    def as_dict(self) -> dict:
        return self._asdict()


def default_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "integrations"
        / "portkey"
        / "catalog.json"
    )


def load(path: Path | str | None = None) -> dict:
    """Read the catalog. A missing or unreadable catalog is a GateError."""
    path = Path(path) if path is not None else default_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise GateError(f"catalog unreadable at {path}: {e}") from e
    for key in ("tiers", "models", "providers"):
        if key not in data:
            raise GateError(f"catalog at {path} has no {key!r}")
    return data


def _served_by(model: dict) -> list[dict]:
    entries = model.get("served_by")
    if entries is None and model.get("provider"):
        # Older shape: one provider per model.
        entries = [
            {
                "provider": model["provider"],
                "free": model.get("free"),
                "why": model.get("why", ""),
            }
        ]
    return list(entries or [])


def is_free(
    catalog: dict, model_id: str, provider: str | None = None
) -> tuple[bool, str]:
    """(allowed, reason) for a model id, optionally for one provider of it."""
    blocked = catalog.get("blocked_providers", {})
    for model in catalog["models"]:
        if model.get("id") != model_id:
            continue
        if model.get("unavailable"):
            return False, f"{model_id} is marked unavailable: {model['unavailable']}"
        served = [
            s
            for s in _served_by(model)
            if provider is None or s.get("provider") == provider
        ]
        if not served:
            return False, f"{model_id} is not served by {provider!r}"
        for s in served:
            p = s.get("provider")
            if p in blocked:
                return False, f"provider {p!r} is blocked: {blocked[p]}"
            if p not in catalog["providers"]:
                return False, f"provider {p!r} is not in the catalog"
            if s.get("free") not in FREE:
                return False, f"{model_id} on {p!r} is not free ({s.get('free')!r})"
        return True, served[0].get("why", "free")
    return False, f"{model_id} is not in the catalog"


def resolve(spec: str, catalog: dict | None = None) -> ModelChoice:
    """Turn a spec into the provider name and model LiteLLM serves, or raise GateError."""
    catalog = catalog if catalog is not None else load()
    spec = (spec or "").strip()
    if not spec:
        raise GateError("empty model spec")
    if spec.startswith("tier-"):
        tier = spec[len("tier-") :]
        if tier not in catalog["tiers"]:
            raise GateError(
                f"unknown tier {tier!r}; tiers are {list(catalog['tiers'])}"
            )
        members = [
            m
            for m in catalog["models"]
            if m.get("tier") == tier and not m.get("unavailable")
        ]
        if not members:
            raise GateError(f"tier {tier!r} has no available models")
        return ModelChoice(
            spec,
            PROVIDER_NAME,
            spec,
            f"tier {tier}: {len(members)} models, LiteLLM picks",
        )
    if ":" not in spec:
        raise GateError(f"model spec must be tier-<t> or <provider>:<id>, got {spec!r}")
    provider, model_id = spec.split(":", 1)
    ok, why = is_free(catalog, model_id, provider)
    if not ok:
        raise GateError(why)
    return ModelChoice(spec, PROVIDER_NAME, model_id, f"{provider}: {why}")


def tiers(catalog: dict | None = None) -> list[str]:
    catalog = catalog if catalog is not None else load()
    return list(catalog["tiers"])
