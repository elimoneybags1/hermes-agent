# Pipeline readiness — 2026-10-26

This is an audit snapshot, not a live health indicator. The dashboard must read
process, board, session and ledger evidence for current state.

## What is built

Boss: native Hermes with bounded planning and read-only helper agents. Its
persisted task contracts provide assigned files, relevant inspection roots,
acceptance criteria, dependencies and a check command. Optimizer: advisory,
with evidence-backed recommendations; it does not apply its own proposals.
Implement: active worker. Spec, test and review: configured but planned.
`hands_to` does not implement an automatic specialist pipeline.

## Controls added in this audit

- Team-managed tasks set native `max_retries=1`. In Hermes this means one
  failure before blocking, not one extra retry. Team owns subsequent attempts.
- Planned roles and missing dedicated profiles cannot silently run as a
  generic worker. Installed profiles must have a positive turn cap no greater
  than the manifest before a new attempt can be queued.
- Profile sync writes `agent.max_turns`. Installed caps were migrated to
  implement 40, spec 30, test 30 and review 25. Their runtime budgets are 900s.
- Claude escalation relies on the authoritative post-merge grading check;
  the transport wrapper no longer repeats that same check first. Workers may
  still run checks while developing.
- Scoped assignments resolve file ownership from their saved contract at
  grading. Changes outside exact owned paths, including both sides of a
  rename, fail before landing. Manual goals without a plan keep legacy grading.
- Task packets support validated `recommended_toolsets` and `required_skills`.
  Skill references must exist in that role's or the upstream skills library;
  toolset recommendations must be available to the role. These are loading
  guidance, not a per-task capability sandbox. No skill library content is
  copied into the packet. Existing mandatory role procedures still apply.

Prefer one packet per coherent change: exact requested behavior, relevant
interfaces, owned files, needed procedures and proof of completion. Split only
when outputs are independently useful; tiny tasks can duplicate startup context.
Do not ask a worker to read the runtime source to understand its tool schemas.

## Verification and rollout limits

Verification: **706 tests passed**. Native direct inference, subscription tools and
compression, deterministic orchestration, live free-worker dispatch through local
landing, and subscription boss planning all passed isolated probes. See
[NATIVE-VERIFICATION.md](NATIVE-VERIFICATION.md) for evidence and measured usage.
These are bounded fixture checks, not proof of unattended reliability.

The dashboard on port 7342 and its idle boss were restarted on 2026-10-26.
The boss has a fresh native session; its UI transcript and previous native history
were retained. The active worker runner/model-switch tests were not interrupted.
Runner-side Python fixes require its next restart; queued tasks retain old settings.

## Still needed before claiming unattended team readiness

- Decision-aware dispatch/resume: an open Team decision is not currently a
  general queue-dispatch barrier.
- Reliable model/session evidence: the legacy missing-session path currently
  accepts unknown model evidence. Pre-dispatch model allowlisting exists, but
  this post-run audit gap remains.
- Per-task tool-schema filtering and stronger worker read limits. Boss reads
  are bounded; worker context selection is still largely instruction-guided.
- Validate subscription transport aggregate usage versus Hermes context-size
  accounting before claiming context-compression efficiency or token savings.
- Activate specialist stages only with real grading and handoff behavior.

No measured token reduction is claimed. Installed, active, running, verified
and production-ready should be separate dashboard states.

## Boss token incident follow-up

The earlier tab-layout session recorded 1,311,159 input tokens across 18 API
calls without dispatch. The follow-up replaced custom search internals with native
Hermes search, removed duplicated Kanban guidance, corrected the native coding
context setting and enabled native deferred skill tools. Shared per-turn inference
bounds and content-free request/usage diagnostics now expose and limit repeated
calls. The reported-token threshold is checked after responses and can overshoot.

The final boss fixture successfully saved a ready plan, but used 171,564 input
tokens, including 112,239 cached input tokens. Prompt size decreased; total task
cost optimization is not established. The original reported missing-tool incident
has not been conclusively reproduced or assigned a root cause. Controlled native
compression retained tools and continued successfully through the adapter.
