# Hermes runtime, Team policy, Claude subscription inference

Plain `team` and Claude worker escalation now instantiate the **actual forked
Hermes `AIAgent`**, not Team's earlier custom conversation loop.

```text
User → native Hermes boss + Team role policy → Team queue
                                                ↓
                                      Team run / grading / landing
                                                ↓
                                       native Hermes workers

Hermes prompt + history + tool schemas → localhost adapter → Claude CLI
              ↑                                ↓
              └──── native tool execution ← assistant/tool requests
```

The complete source is the submodule `vendor/hermes-agent`, forked at
https://github.com/elimoneybags1/hermes-agent. Initialize a fresh clone with
`git submodule update --init vendor/hermes-agent`. Install its dependencies
following its README. Team currently prefers the fork's `.venv` or `venv`,
and otherwise uses the installed Hermes dependency interpreter; imports still
come from the fork. Installed Hermes source is not modified.

## What Hermes owns

`run_agent.AIAgent` builds the prompt, discovers tools and skills, runs the
model/tool loop, maintains conversation history, compresses context, loads and
writes memory, searches past sessions and performs configured background review.
The full upstream `SOUL.md` and `skills/` tree seed each native subscription role.
Optional upstream integrations remain available in source; they still need their
own dependencies and credentials, and role toolsets control exposure.

## What Team owns

- `team/hermes_runtime.py`: persistent role profiles and process launch.
- `team/hermes_native.py`: native embedding, role tools and Team instruction overlays.
- `team/subscription_gateway.py`: authenticated loopback chat-completions adapter.
- `team/claude_transport.py`: subscription authentication and isolated CLI invocation.
- `roles/*/role.toml`, role `SOUL.md`, role skills and `plugin/agents/boss.md`:
  Team responsibilities, capability choices, handoffs and grading contracts.
- Existing Team queue, worktree, decision, grader and landing gates.

The old `team/harness.py` and `claude_run.legacy_run` remain compatibility code;
plain `team` and default Claude escalation do not use that loop.

## Subscription boundary

The CLI remains installed and authenticated using `claude auth login`. Before
launch, Team verifies `claude.ai` authentication with the first-party provider.
Claude CLI is invoked with tools, slash commands, MCP, Chrome, project settings
and persistence disabled, and with the Hermes prompt supplied explicitly.
Hermes executes tools. Claude CLI's own inference and structured-output
machinery still exists: this is not a direct subscription HTTP API.

Native subscription profiles route main and configured auxiliary inference
through the local adapter, with no configured paid fallback and no inherited
provider API credentials. Subscription limits or CLI failures surface as errors.
The adapter currently accepts text only and buffers responses; it does not
provide native vision or genuine token streaming. The whole upstream runtime
is imported, but this transport is not equivalent to every provider API feature.

## Use and state

```sh
team
team -- --prompt "Show the current queue"
team -- --resume <Hermes-session-UUID>
team -- --model opus --max-turns 20 --timeout 600
```

`/quit`, `/exit`, or EOF ends an interactive boss. Session IDs belong to Hermes;
old custom Team harness IDs cannot be resumed here.

Subscription profiles live at `$TEAM_STATE/hermes/profiles/<role>` (default
state root: `~/state/team`). Each holds `config.yaml`, `SOUL.md`, the full skills
library, `memories/` and native `state.db`. Session history is stored by Hermes;
its optional working-directory trajectory export is disabled to avoid writing
transcripts into target repositories.

Free worker profiles remain at `~/.hermes/profiles/team-<role>`. Team's normal
kanban subprocesses resolve the vendored Hermes CLI. Profile sync now seeds
missing upstream skills and enables memory, session search and compression,
while retaining learned skills and existing role identity.

Worker background review is enabled; boss background review is disabled.
Native worker review gets bounded completion time before process shutdown.
Learned memory/skills persist per role, separately for free and subscription
profiles. They are not automatically copied across profiles or promoted into
shared git policy. Shared changes still need Team's outcome/replay evaluation.
Persistence permits improvement; it does not prove an improvement occurred.
