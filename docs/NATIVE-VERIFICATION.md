# Native Hermes and Team verification — 2026-10-26

All four requested verification stages ran. Integration faults were in Team's
configuration, adapter/testing and lifecycle glue; importing Hermes did not validate
those boundaries. Upstream owns the agent loop, prompt construction, tools, skills,
memory, sessions and compression. Team owns scoped contracts, role policy, the
subscription adapter, grading and landing. Upstream source remains unmodified at
`48c0c3a873bc5adaf20c632b5b7630a4fac000b4`.

## 1. Native baseline

`tests/run_native_comparison.py` uses the actual upstream AIAgent and file tools in
an isolated repository. The task changes `value.upper()` to
`value.strip().upper()` while leaving tests unchanged.

| Probe | Outcome | Reported input tokens |
|---|---|---:|
| Direct native baseline | Passed | 14,680 |
| Direct native forced compression and continuation | Passed | 44,566 |
| Subscription adapter forced compression and continuation | Passed | 88,545 |

Evidence: `reports/native-direct-baseline.json`,
`reports/native-direct-compression.json`, and
`reports/native-subscription-compression-fixed-stream.json`.
Compression probes deliberately include artificial history; these are not ordinary
small-task costs. Different models/cache behavior prevent a fair savings ratio.

## 2. Subscription adapter

The adapter passed actual native tool use, compression and subsequent tool use.
The compressed history shrank from 46,478 to 38,252 characters without losing tools.
The first compression probe used an endpoint that mishandled streaming; its failure
is retained in `reports/native-subscription-compression.json`.

Shared bounds now admit at most 12 model requests per user turn, reject serialized
requests over 200,000 bytes, and stop new calls after 120,000 reported input tokens.
The token threshold is not a hard total cap: concurrent/in-flight responses can
overshoot it. Native auxiliary calls and helpers share the budget. JSONL traces
record sizes, reported usage/cache fields, durations and errors without prompt text.
CLI aggregate usage versus native context-size accounting still needs investigation.
The historical missing-tool report is not conclusively explained by these probes.

## 3. Prefer native capabilities

- Scoped search delegates matching/redaction to Hermes after Team admits exact
  files through its scope and budget checks.
- Removed duplicated Kanban guidance already inserted by the native runtime.
- Corrected `agent.coding_context` and selected native focus mode for the boss.
- Enabled native deferred skill/session/todo tools; the upstream skill library
  remains available through discovery instead of automatic full catalog loading.
- Preserved explicit profile tuning and separated worker profile roots from the
  ephemeral subscription adapter environment, including Kanban/session commands.

Team-specific scope/provenance checks remain because unrestricted upstream file
access does not implement those policies.

## 4. Incremental Team orchestration

`tests/run_team_pipeline.py` exercises saved contract → actual Kanban dispatch →
native worker → test/commit → review state → Team grading → local landing.
Both scripted-model and live free-model runs passed with unchanged tests. Scripted
means only the model responses are deterministic; Git, runtime, dispatcher and
grader are real. The live run consumed 52,838 input and 712 output tokens in six
native API calls. Evidence: `reports/team-native-scripted-end-to-end.json` and
`reports/team-native-end-to-end.json`. The first live attempt failed its verification
window and is retained as `reports/team-native-first-attempt.json`.

Worker briefs now name the workspace explicitly. Landing handles native Kanban
having already removed the completed worktree instead of failing duplicate cleanup.

The separate subscription boss probe saved a ready scoped contract and gave a final
response without dispatch (`reports/boss-planning-baseline.json`). Earlier failed
planning probes are retained. The successful probe consumed 171,564 input tokens:
112,239 cached and 59,325 other input. This is still high for the fixture.
System prompt size fell from 46,067 to 32,372 characters (29.7%); tool schemas fell
from 15,566 to 12,248 characters (21.3%). These measure payload size, not demonstrated
total-task token savings. Planning and execution were tested separately; this is
not a single continuous dashboard-to-merge live conversation.

## Validation, rollout and remaining work

Full Team suite: **706 passed**. Vendor source is clean. No commit or push performed.
The dashboard on port 7342 and idle boss were restarted with a fresh native session;
UI transcript and old native history were retained. Active worker/model-switch tests
were left running, so runner-side changes take effect on its next restart.

Remaining: reduce boss round trips and context cost using measured traces; resolve
aggregate-usage/context accounting; fail closed on missing post-run session evidence;
implement general decision-aware queue barriers; validate stronger worker read/tool
limits and real specialist stage handoffs. This is a verified bounded pipeline,
not an unattended production-readiness claim.
