"""Pipelines and gates: the stages a goal goes through, and what code decides at each.

A goal's ``type`` picks a pipeline, an ordered list of stages. A stage names
a role (a worker profile, or None for a stage Team runs itself) and a gate,
the code in ``team.run`` that decides whether the stage's result stands. A
role may only name a gate registered here (``roles.load`` refuses others),
and a stage whose role is not active in the roster is skipped, so the roster
can grow a stage at a time (test-write, review, security) without the engine
changing. Nothing here is edited by an agent: a new gate is a Team code
change.

Landing. ``LANDING[type]`` is ``auto`` (land when the last gate passes) or
``ask`` (Eli answers a decision first). A ``feature`` is ``auto`` because its
approval happened before building: Eli approved the plan in his inbox, or
typed the goal himself. Risk only goes up: whatever the type, a diff that
touches a risky path (dependencies, lockfiles, migrations, CI, auth, billing,
secrets) or is oversized makes the goal ``ask``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    name: str  # also the value written to parts.stage
    role: str | None  # the worker profile, or None when Team runs the stage itself
    gate: str  # a key of GATES


# gate name -> what it checks, in one line (shown in the roster and the UI)
GATES: dict[str, str] = {
    "check": (
        "only owned files changed, work committed, base merged, the diff against base is not "
        "empty, the target's check and the part's acceptance command pass"
    ),
    "verify": (
        "Team runs the target's check, the part's acceptance command and the target's e2e "
        "command in the worktree; no worker, no model"
    ),
    "tests_only": (
        "only test files changed, committed; the acceptance command (the new tests) fails on "
        "the branch, so the tests are real and not yet satisfied"
    ),
    "review": "a verdict, veto only: every finding cites a line of the diff; 'fix' sends the part back",
    "security": "read-only scan of the diff; the verdict always goes to Eli",
    "onboard": "the check it wrote passes at HEAD and fails on a seeded break",
}

_TEST_WRITE = Stage("test_write", "test-write", "tests_only")
_IMPLEMENT = Stage("implement", "implement", "check")
_VERIFY = Stage("test_verify", None, "verify")
_REVIEW = Stage("review", "review", "review")
_SECURITY = Stage("security", "security", "security")

PIPELINES: dict[str, tuple[Stage, ...]] = {
    "bugfix": (_TEST_WRITE, _IMPLEMENT, _VERIFY, _REVIEW),
    "feature": (_TEST_WRITE, _IMPLEMENT, _VERIFY, _REVIEW),
    "dependencies": (_IMPLEMENT, _VERIFY, _REVIEW),
    "security": (_IMPLEMENT, _VERIFY, _SECURITY, _REVIEW),
    "onboarding": (Stage("spec", "onboard", "onboard"), _REVIEW),
}

LANDING: dict[str, str] = {
    "bugfix": "auto",
    "feature": "auto",
    "dependencies": "ask",
    "security": "ask",
    "onboarding": "ask",
}

TYPES = tuple(PIPELINES)
DEFAULT_TYPE = "bugfix"
STAGE_BY_NAME: dict[str, Stage] = {s.name: s for p in PIPELINES.values() for s in p}
# A gate's detail starting with this sends the goal back to the builder stage instead of
# retrying the current one: the reviewer said what is wrong, and the builder must fix it.
SEND_BACK = "REVIEW: fix"


class GateError(ValueError):
    """A type, gate or stage that does not exist."""


def stages_for(kind: str, active_roles: set[str] | frozenset[str]) -> list[Stage]:
    """The pipeline for ``kind`` with every stage whose role is not active left out.

    Always at least one stage: a pipeline reduced to nothing (no builder active) is an error.
    """
    if kind not in PIPELINES:
        raise GateError(f"unknown goal type {kind!r}; one of {TYPES}")
    stages = [s for s in PIPELINES[kind] if s.role is None or s.role in active_roles]
    builders = {s.role for s in PIPELINES[kind] if s.role and s.gate in ("check", "onboard")}
    if not any(s.role and s.gate in ("check", "onboard") for s in stages):
        raise GateError(f"no active role can build a {kind!r} goal; the roster needs one of {sorted(builders)}")
    return stages


def worker_stages(kind: str, active_roles: set[str] | frozenset[str]) -> list[Stage]:
    return [s for s in stages_for(kind, active_roles) if s.role]


def stages_named(names: list[str]) -> list[Stage]:
    """The stages a goal was created with (stored on the goal, so a roster change never
    reinterprets a running goal's position)."""
    try:
        return [STAGE_BY_NAME[n] for n in names]
    except KeyError as e:
        raise GateError(f"unknown stage {e.args[0]!r}") from None


# --- risk ---------------------------------------------------------------------------

# Paths whose change is always Eli's to land, whatever the goal's type.
DEFAULT_RISK_PATHS: tuple[str, ...] = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "pyproject.toml", "setup.py",
    "Cargo.lock", "Cargo.toml", "go.mod", "go.sum", "Gemfile.lock",
    ".github/workflows/*", ".gitlab-ci.yml", "Dockerfile", "docker-compose*.yml",
    "*/migrations/*", "prisma/schema.prisma", "*.sql",
    ".env*", "*.pem", "*.key", "*.p12",
)
# Path segments that name money or identity: lib/auth.ts, app/api/billing/route.ts, stripe.py.
_RISK_WORDS = re.compile(r"(^|[/_.\-])(auth|authn|authz|oauth|billing|payments?|stripe|checkout|secrets?|credentials?)([/_.\-]|$)",
                         re.IGNORECASE)

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e)(/|$)|(^|/)test_[^/]*\.py$|[._-](test|spec)\.[a-z]+$|_test\.(go|rs|ts|js)$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def risky_paths(changed: list[str], extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The changed paths that match a risk pattern (the defaults plus the target's own)."""
    patterns = tuple(DEFAULT_RISK_PATHS) + tuple(extra or ())
    out = []
    for path in changed:
        base = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p) for p in patterns) or _RISK_WORDS.search(path):
            out.append(path)
    return out


def risk_of(
    kind: str, changed: list[str], lines_changed: int, *, extra_paths: list[str] | tuple[str, ...] = (),
    max_lines: int,
) -> tuple[str, str]:
    """('auto' | 'ask', why). Never lower than the type's own landing policy."""
    if kind not in LANDING:
        raise GateError(f"unknown goal type {kind!r}; one of {TYPES}")
    if LANDING[kind] == "ask":
        return "ask", f"a {kind} goal always waits for Eli"
    risky = risky_paths(changed, extra_paths)
    if risky:
        return "ask", "risky paths changed: " + ", ".join(risky[:6]) + (" …" if len(risky) > 6 else "")
    if lines_changed > max_lines:
        return "ask", f"{lines_changed} lines changed, over the {max_lines} that land unattended"
    return "auto", f"a {kind} goal on ordinary files, {lines_changed} lines"
