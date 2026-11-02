# Team: from one builder to a working agent pipeline

Approved by Eli on 2026-11-02. This file is the brief for the session that
builds it. It is built directly in this repository, not through Team's own
pipeline; Team changes nothing in itself until the new pipeline has proved
itself on a disposable target.

## Context

Eli wants to do his project work through Team instead of Claude Code directly:
work routed by type to specialised agents, agents that improve after each
project and can propose new agents, and one clean visual UI, gateway included.

Audit findings on 2026-11-02:
- Only `implement` builds. The stages exist in the ledger, but no code moves a
  goal between them.
- Nothing has been built for a repo other than Team.
- 45.9% of chat calls failed (903 of 1,968). `tier-medium` falls off
  `north-mini-code` (462/470 OK) onto `laguna-xs-2.1` (30/177 OK). There were
  also 143 `__aiter__` stream crashes in LiteLLM.
- The replay loop is built but has never run. `self-evolution/` is unused.
  Three UIs overlap.

## Eli's decisions

- First real target: StudyApp (`$HOME/work/StudyApp`, remote `my-prof`).
- Repos without tests start with an onboarding goal that writes the check. Eli
  approves it once.
- Autonomy by type: bug fixes and small changes land automatically if they
  pass; features need plan approval; security, dependencies and money always
  ask.
- Claude plans. The goal is orders precise enough, with the right tools and
  skills, that small free models do the building.
- After each project, a review improves the agents and can propose a new one.
  Once adopted, the boss and lead see it in the roster.
- This build does not go through Team's pipeline.
- Updated 2026-11-02: build Team only. Team may read the existing StudyApp checkout, including dirty/untracked source. Do not edit, clean, stash or move its existing work.

## Defaults chosen in planning (change them when reviewing)

- "Land" means local `main`. Eli pushes. Workers get a hard block on
  `git push`.
- StudyApp stays in its existing checkout. Preserve dirty and untracked work in place.
- New agents are inbox cards that Eli approves, and they start on trial.
- Eli adopts evolution changes at first. Auto-adopting skills can come after a
  track record exists.
- A new agent may only use a gate already in the registry. A new gate is a Team
  code change.

## How this gets built

- Directly in this repo, committing as it goes, `team/bin/team-check` as the
  gate. Never push.
- Step 0, before any code: stop the `team run` loop and disable the `team`
  target. Finish or park goals 17, 21 and 22. Confirm in `ps` and the log that
  nothing is dispatching.
- Team stays off its own target until the end of Phase 3.
- The proving target is a disposable clone of Team under
  `$HOME/state/team/acceptance/`, attached as `team-sandbox`, so goals are
  real-sized and nothing can land on the real repo.
- Eli authorized access to the existing StudyApp checkout from Team on 2026-11-02.
  The runner scouts it read-only. Build-session edits remain confined to Team;
  a future approved worker goal uses an isolated worktree and the landing guards.

## Phases

| # | Build | Exit, measured |
|---|---|---|
| 0 | Pause the loop, run the spikes | Each spike has a recorded number |
| 1 | Gateway reliability | At most 5% of 30 consecutive attempts die on infrastructure, shown beside the raw non-200 rate |
| 2 | Pipeline engine and per-target loops | 10 `team-sandbox` goals go through pipelines; a no-op commit is refused |
| 3 | Roles, gates, reviewer, risk rules | Reviewer meets the spike bar; every active role maps to a registered gate; re-enable `team` |
| 4 | Target overlays, onboarding, StudyApp | Check reproduces in a worktree; a deliberate break fails it; Eli approves; 10 goals land |
| 5 | Retrospective, proposing agents, replay fixes | One real paired replay finishes and is decided |
| 6 | One UI | Old dashboards removed; 8792 shows agents, targets and a live gateway diagram |

Done: 20 consecutive StudyApp goals finish with no hands from Eli beyond
required approvals.

UI work can start beside Phase 2. The agent and pipeline views need that
schema, so they are finished after it.

### Spikes (Phase 0)

1. Worktree check. Provision with an APFS `cp -c` of `node_modules` plus
   `prisma generate`, using an env allowlist. Pass: `npm run check` is green
   and `test:browser` runs in two parallel worktrees without collisions. Needs
   StudyApp access.
2. Reasoning stripping. Replay failed requests with reasoning fields removed.
   If non-200s don't fall substantially, the cause is wrong: stop and
   re-diagnose.
3. Reviewer accuracy. 20 bad diffs (failed jobs plus seeded bugs) and 10 good
   ones, free model against Sonnet. Pass: no misses on auto-land work and
   under 20% false blocks. This picks the reviewer's model.
4. Free models on precise orders. 5 StudyApp-class orders. This tests the whole
   strategy. Show Eli the number before Phase 3. If it's poor, the money goes
   to better orders or Claude takes more of the building.

### Phase 1: gateway

- Strip replayed reasoning fields; fix the stream crash at
  `litellm/litellm/proxy/proxy_server.py:10192`.
- Reorder or drop models in `tier-medium` using `usage.db`. Models are catalog
  edits through `integrations/portkey/build.py`.
- Classify each failed attempt as infrastructure or work, so bad gateway days
  don't count against agents.
- Correct `CLAUDE.md` and `HANDOFF-GATEWAY.md` where they claim `spend_guard`
  is active or give wrong ports. No API key goes in until the guard is really
  built.
- Mark `gateway-rs` as idle. Don't rewrite it.

### Phase 2: pipeline engine

- Replace both `_land` calls (`team/run.py:386`, `:632`) with
  `_advance(goal)`: land, or move to the next stage on the same branch.
- New `team/gates.py` holds pipelines and gates in Python, not agent-editable.
- New acceptance gate: orders gain `type`, `done_when[]` and `acceptance_cmd`.
  Today any commit on a passing repo passes, which isn't safe for auto-land.
- Schema: `goals` gains `type`, `stage_idx`, `risk`, `checked_base_sha`;
  `parts.stage` adds `security` (rebuild the CHECK in `team/ledger.py:142`);
  `diagnoses` gains `cause_tag`; `role.toml` gains `gate`, `extends`;
  `target.json` gains `setup`, `e2e`, `check_env`, `risk_paths`.
- Per-target loops: one `team run --target X` per target plus one global
  process, a per-target lock, and a global file lock capping concurrent checks.
  No async checks: grading and landing together act as the merge queue.
- Check whether `worker.max_calls_per_goal=15` is too tight; goal 15's worker
  used 34 calls.
- Make model orders apply past attempt 1 where the code needs it
  (`team/run.py:520`).

### Pipelines

| Type | Stages | Landing |
|---|---|---|
| onboarding | onboard → review | Eli approves |
| bugfix / small | test_write → implement → verify → review | auto |
| feature | spec (plan approval) → test_write → implement → verify → review | auto after approval |
| security | spec → implement → verify → security → review | Eli |
| dependencies / money | same as security | Eli |

- Verify is code with no model: check, acceptance, `e2e`.
- Risk can only go up. A `risk_paths` hit (manifests, lockfiles, migrations,
  auth, billing, CI) or an oversized diff forces `ask`, whatever type the lead
  chose.

### Phase 3: roster

| Role | Gate, enforced by code |
|---|---|
| implement | Owned files, check, acceptance |
| test-write | Only test files changed; new tests fail at base; existing tests pass |
| review | Veto only; each `done_when` cites a diff line or logged exit code that code validates |
| security | Read-only; scanner diff clean; verdict always goes to Eli |
| onboard | Check passes at HEAD and fails on a seeded break |

- UI, backend and bugfix builders are `extends = "implement"` plus their own
  skills, tools and candidate models. They share a gate, so they aren't
  separate roles underneath, but they appear by name in the roster and ledger.
  Pool their rates until each has about 20 attempts.
- Enable `plugins/security-guidance`. Start skills from the bundled set:
  `test-driven-development`, `systematic-debugging`, `dogfood`,
  `simplify-code`.
- Update `team/board.py:120,434`, which hardcodes the columns, the lead's menu
  and the `write-work-order` skill.
- Rescue's tool list is hardcoded in `team/hermes_run.py:137`; give it role
  tools.
- A reviewer can only veto. Code gates still decide done (principle 3).

### Phase 4: overlays and StudyApp

- Overlays live at `$TEAM_STATE/targets/<t>/overlay/<role>/`, each a local
  git repo.
- `profiles.sync` builds `<role>--<target>` from base plus overlay, following
  `candidate_profile`. Overlays can add skills or narrow tools, models and
  budget, never widen them. `audit_profiles` must cover them.
- `lead.menu` takes a target. `jobs.role_version` becomes `base+overlay`.
- Attach StudyApp without enabling dispatch or moving its dirty work. Run the
  read-only scout first; reason over its summaries. Onboarding must validate a
  check in an isolated worktree before normal changes can be dispatched.

### Phase 5: self-improvement and new agents

- `record_closed` triggers code that gathers rates and `cause_tag` counts,
  excluding infrastructure failures.
- A narrow lead call then proposes at most one change per project. It's only
  allowed when the same cause shows in at least 3 goals across at least 2
  projects. Otherwise it records nothing.
- New `boss.propose_agent` and tool: role files, a registered gate, and the
  evidence. It arrives as an inbox card and starts on trial.
- Replay fixes in `team/replay.py`: role-scoped fixed set of 8, paired by
  item; sync candidate profiles for every role in the set; exclude
  infrastructure-dead runs; ties are dismissed (removes the fewer-tokens
  adoption in `verdict()`).
- Replay at this size is a regression guard and a signal, not proof. After
  adoption, watch the next 10 attempts and propose a revert if the rate falls
  below base.
- Cut for now: DSPy, `self-evolution/` and GEPA, until one replay-adopted
  change exists. Take Harness2's method only (several candidates compared on
  the same task). It can't run under free-only, so its repo isn't needed.

### Phase 6: UI

- `team/app` on 8792 becomes the one UI. Keep Hermes's kanban dashboard.
- Move verify, test, recent and log views from `integrations/dashboard`, whose
  functions `team/app/edit.py:187` already imports, then delete it and
  `plugins/team/dashboard`.
- Agents: the roster, each agent's rates beside base with counts, plus its
  gate, skills and history.
- Propose agent: a form that calls `boss.propose_agent`, never a direct file
  write.
- Work: a target selector, with columns derived from the pipelines.
- Gateway: vendor one `mermaid.min.js`, no framework or build step. Generate
  the route diagram server-side from `catalog.json` and live health, with
  plain controls for models and keys.
- Fix the Usage page if its `fill()` calls overwrite each other
  (`app.js:1078`).

## Added by Eli during the build (2026-11-02)

Folded into the phases above where they fit; the rest is the layer after this
build, on the same roster and gate mechanism.

- **Pre-built agents for specific coding work** (UI, backend, bug fixes, and
  more as repos need them): `team_propose_agent` plus `extends` is the
  mechanism; the first three are seeded as trial roles once the review stage
  has numbers (Phase 3 exit). Each is `implement` plus skills and models; a
  genuinely different gate is a Team code change.
- **A small-model scout when a repo is attached.** The onboarding worker and
  the lead's briefing should read a repo through a small model's summary, not
  by pulling hundreds of thousands of tokens into Claude. `team/review`
  (chunk → small-model summaries → `read_summary`) already does this for a
  cloned repo; Phase 4 wires it into `lead.briefing` and the onboarding brief
  as the first thing read, and records the tokens it saved next to the
  tokens it cost.
- **Model routing tuned from the ledger, not guessed.** `role.models.candidates`
  are tried in order today. Phase 5 adds `models.choose`: per role and target,
  candidates ranked by pass rate over work attempts (infra left out), with
  some exploration so a new free model gets real chances, every choice
  recorded with its reason; pinnable. LiteLLM keeps choosing the deployment
  inside a tier; Team chooses the tier or model per attempt.
- **Routines: runner-style watchers that are not GitHub's.** Per target, a
  recurring goal on an interval ("run the suite and fix what breaks" daily,
  "security scan the diff since last run", "dependency audit" weekly). The
  loop reads `$TEAM_STATE/targets/<t>/routines.toml` each tick and starts the
  goal when due, through the normal pipeline and landing policy. Phase 5.
- **Business agents** (email triage into a database and a morning summary,
  drafted replies, sales and marketing work): the layer after this build.
  They are roles with non-code gates (a draft is done when Eli approves it;
  a summary is done when every item cites its source), so the gate registry
  grows before any such role exists. Nothing here changes to make room for
  it; it is why gates are a registry and roles are files.

## Critical files

`team/run.py`, `team/grade.py`, `team/lead.py`, `team/roles.py`,
`team/profiles.py`, `team/replay.py`, `team/boss.py`, `team/ledger.py`,
`team/board.py`, `team/hermes_run.py`, new `team/gates.py`,
`plugins/team/boss_tools.py`, `team/app/`, `integrations/portkey/build.py`,
`litellm/team/`.

Ownership rule holds: any upstream or subtree edit is a marked `[team]` commit
listed in `PROVENANCE.md`.

## Verification

- `team/bin/team-check` passes at every commit; `team gate audit` is clean.
- Each phase exit is read from `team.db`, `usage.db` and `requests.jsonl`,
  rates beside base rates with counts.
- Negative tests per gate: a no-op commit refused, a test-write that touches
  source refused, a `risk_paths` hit forced to `ask`, a worker `git push`
  blocked, an overlay that widens tools refused.
- End to end in a browser at 8792: a bug fix on `team-sandbox` runs through
  every stage and lands, with the agents and gateway views matching the ledger.
- Evidence comes from processes and logs, not markdown.

## Progress

Appended by the build session, one dated line per phase exit with its number.

- 2026-11-02 Step 0: `team` target disabled, `team run` (pid 96240) stopped;
  goals 17, 21–32 parked open, nothing dispatches. Three kanban workers were
  still running and timed out on their own budgets.
- 2026-11-02 Spike 2 (reasoning fields): the dumped failed worker request
  replayed to `laguna-xs-2.1` and `ministral-14b` → 400 / `null` with the
  fields, 200 without. Cause confirmed. A second cause found on the way:
  LiteLLM returned `None` for an uncurable provider 422 (`null` body, or the
  500 `__aiter__` on stream); reproduced, patched (`[team]` commit
  `85445cffc0`), re-verified: HTTP 422 with the provider's message.
- 2026-11-02 Phase 1 interim: after the hook, the patch and the catalog
  reorder, 30 consecutive worker-shaped requests through `tier-medium` →
  0/30 non-200 (before: 903/1,968 = 45.9% non-200 that day). The exit
  criterion proper (≤5% of 30 consecutive *attempts* dying of
  infrastructure) is measured on `team-sandbox` goals in Phase 2; the ledger
  now records `jobs.failure_kind` for it. Backfill over the existing ledger:
  2 of 19 finished failures classified infra.
- 2026-11-02 Phase 2 engine landed (`5f52b1b62b`): types, stages, verify,
  acceptance, no-op refusal, risk-based landing, per-target loops and locks;
  362 tests. `team-sandbox` attached (a clone of Team under
  `$TEAM_STATE/acceptance/`, check through the main checkout's environment).
  First sandbox batch (10 goals): 3 of 5 graded attempts died because the
  lead wrote `pytest …` acceptance commands and `pytest` is not on the
  worktree's PATH; batch superseded, `test_runner` added to targets and the
  lead's prompt, bare test tools refused.
- 2026-11-02 Phase 3 landed (`707c5d0523`): review, test-write, security
  roles with gates; review active. Second sandbox batch: goal 42 landed
  through implement → verify → review, the reviewer's second attempt giving
  a grounded `land` verdict (its first died of the gateway). Of the batch's
  first 9 graded worker attempts, 4 passed, 0 failed on their work, 5 died
  of infrastructure (`custom:litellm didn't answer`): OpenRouter mid-stream
  errors on `north-mini-code` and a bad Cohere fallback. Since the 16:22Z
  LiteLLM restart the gateway's own failure rate is 23/960 = 2.4%
  (`usage.db`); the Cohere fallback is now off and a second LiteLLM stream
  bug patched (`served_output_texts`). Phase 1's exit on *attempts* is still
  to be read off the next 30 sandbox attempts.
- 2026-11-02 Phase 4 mechanics (`cf77e0b980`): per-target overlays and the
  onboarding role whose gate proves the check it proposes. StudyApp itself
  waits on Eli allowing the path. Phase 5 landed: per-role replay sets,
  the post-adoption watch, cause tags and the retrospective, agents the
  boss can propose (397 tests).
- 2026-11-02 Phase 6 landed (`33b959ae95`): one UI on 8792 with Agents,
  Gateway (Mermaid route diagram from the catalog and usage.db) and a target
  selector; integrations/dashboard and the Hermes tab plugin removed.
  Sandbox batch 2 at this point: 5 of 10 goals landed through implement →
  verify → review (42, 44, 45, 47, 48), 5 in review. 24 graded worker
  attempts: 15 passed, 2 failed on their work, 7 died of infrastructure
  (all before the 17:40Z fixes; since then 4 of 54 gateway calls failed).
  Reviewer so far: 5 grounded `land` verdicts, 2 prose answers refused and
  retried, 0 `fix`; the reviewer calibration spike (20 bad / 10 good
  diffs) is still to run and decides the reviewer's model.
- **Left when this session's usage limit stopped it:** ledger-ranked model
  choice (`team/models.py` into `Loop._choice`), routines, the reviewer
  spike, the small-model repo scout, seeding ui-build/backend-build/bugfix
  as trial agents, and Phase 4's StudyApp half. Access was subsequently
  authorized; see the continuation below for the read-only attachment. `team` target is still disabled; the pinned
  sandbox loop (`team run --target team-sandbox --no-boss --pin`) is
  still running and may be stopped with `pkill -f 'team run --target'`.


### 2026-11-02 continuation: preserve My Prof, build the runner

Eli clarified that this session changes Team only. The existing My Prof
checkout is registered as `myprof`, with summary-first planning enabled and
build dispatch disabled pending verified onboarding. No clone, cleanup,
stash, branch switch or direct app edits are authorized by this continuation.

The runner now has an existing-checkout scout, content-addressed incremental
summaries, stale-evidence filtering, source references, a free gateway backend,
and bounded summary input for boss/lead/onboarding. `team ideas` gives only
summaries to a stronger reasoning tier and saves proposals without dispatch.
Model ranking from graded work (excluding infra) and opt-in routines are wired
into the loop. The instructions above about preserving work replace the old
preservation-branch plan.

This does not claim the original acceptance campaign is complete: reviewer
calibration, the paired evolution replay, specialist-role adoption, and the
StudyApp onboarding/build campaign still need their own measured gates.
