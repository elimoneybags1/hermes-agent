# Provenance: what this repository is built from

This repository is a fork of Nous Research's Hermes Agent at its root, with
other projects forked in as subtrees and Team layered on top. Upstream files
are never edited in place. Every exception is a marked `[team]` commit listed
here with file and line.

## Upstreams

| Project | Where | How it came in | Pinned | Update |
|---|---|---|---|---|
| NousResearch/hermes-agent | repo root | `git merge --allow-unrelated-histories upstream/main` on 2026-10-27, merge commit `8ee29e6d` | `e5ea5fd9` (upstream main, 2026-10-26) | `git fetch upstream main && git merge upstream/main` |
| NousResearch/hermes-agent-self-evolution | `self-evolution/` | `git subtree add --squash`, 2026-10-27 | `0a929e3a` (main) | `git subtree pull --prefix=self-evolution self-evolution main --squash` |
| Portkey-AI/gateway | `portkey/` | `git subtree add` (full history) on 2026-10-27, commit `f2cfba42` | `v1.15.2` = `ca771294` (2026-01-12) | `git subtree pull --prefix=portkey portkey <tag>` (the two `[team]` patches below merge forward) |
| BerriAI/litellm | `litellm/` | `git subtree add --squash` on 2026-10-27, commit `0c3f0dc4` (squash `68eb04f3`); fetched through a depth-1 clone because GitHub served the tag's full-history pack at ~50 KB/s, so `.git/shallow` lists `ea7ca16d` (the squash has no parent link to it; harmless). 13 files then renormalized to LF by the root `.gitattributes`, commit `51200db6` | `v1.106.4` = `ea7ca16d` (2026-10-25) | `git subtree pull --prefix=litellm litellm <tag> --squash` |

## Remotes and GitHub forks (set 2026-11-01)

Every upstream has a fork under Eli's GitHub account, and each fork is a named
remote here. Pushing to `origin` is only ever on Eli's say-so.

| Remote | Points at | Fork of | Role |
|---|---|---|---|
| `origin` | `elimoneybags1/hermes-agent` | NousResearch/hermes-agent | this repository, Team included; `main` is pushed here |
| `upstream` | `NousResearch/hermes-agent` | | Hermes itself; `git fetch upstream main && git merge upstream/main` |
| `portkey` | `elimoneybags1/gateway` | Portkey-AI/gateway | source for `portkey/` |
| `litellm` | `elimoneybags1/litellm` | BerriAI/litellm | source for `litellm/` |
| `self-evolution` | `elimoneybags1/hermes-agent-self-evolution` | NousResearch/hermes-agent-self-evolution | source for `self-evolution/` |

Taking an upstream change into a subtree goes through the fork, so the fork is
always the record of what was taken:

```
gh repo sync elimoneybags1/litellm --source BerriAI/litellm       # fork main = upstream main
git subtree pull --prefix=litellm litellm <tag-or-main> --squash  # into this repo
```

Same with `gateway`/`portkey` (no `--squash`, full history) and
`hermes-agent-self-evolution`/`self-evolution` (`--squash`). Sending the
`[team]` patches of a subtree back to its fork is
`git subtree push --prefix=portkey portkey team`, which splits this repo's
history for that prefix and pushes it as the fork's `team` branch. Then update
the Pinned column above.

`elimoneybags1/gateway` branch `team` was first pushed 2026-11-01 at
`0c6548e3`: the four `[team]` Portkey commits (12 files) on top of v1.15.2.
A Portkey built from that branch has config-by-name, the request log and the
tool round-trip fixes without this repository. LiteLLM carries no Team patch;
its connection to Portkey is the `config.yaml` that `build.py` generates from
the catalog, so `elimoneybags1/litellm` has no `team` branch. The Rust
connection layer, `integrations/gateway-rs/`, needs the catalog, LiteLLM's
`usage.db` and Portkey's `requests.jsonl`, so it lives here and not in either
fork.

The repository before the fresh start is tag `before-fresh-start`
(`25b8aca`). `git checkout before-fresh-start` restores the whole old tree.

## What Team owns

`team/`, `plugins/team/`, `integrations/`, `tests/team/`, `PROVENANCE.md`,
`CLAUDE.md`, `.work.json`, `IDEAS.md`, `FRESH-START.md`, `PLAN.md`,
`HANDOFF-GATEWAY.md`, a `# Team` block appended to `.gitignore`, and three
lines in `pyproject.toml` (the `team` script, the `team` package include, and
its package-data).

## Marked patches to upstream files

Each entry: date, directory, file, lines, one-line reason, commit. Verify the
lines with `git -C . log --oneline -- <file>` and `grep -n '\[team\]' <file>`.

| Date | Dir | File | Lines | Why | Commit |
|---|---|---|---|---|---|
| 2026-10-27 | root | `hermes_cli/kanban_db_dispatch.py::_worker_argv` | worker argv | launch with `-P` so a task worktree cannot replace the installed Hermes tree; scoped to the worker, not inherited by owner/PM children; regression in `tests/hermes_cli/test_kanban_worker_import_root.py` | `[team] Pin kanban worker imports to the installed checkout` |
| 2026-10-30 | root | `hermes_cli/kanban_db.py` (schema `toolsets TEXT`, `Task.toolsets`, `_normalize_task_toolsets`, `create_task(toolsets=)`), `hermes_cli/kanban_db_connect.py` (`_LATER_TASK_COLUMNS`), `hermes_cli/kanban_parser.py` (`--toolset`), `hermes_cli/kanban.py` (create passes it), `gateway/session_kanban.py::build_kanban_policy` | grep `[team]` | per-task toolsets that only narrow the assignee profile's (refused with `toolsets_not_in_profile` otherwise; upstream's `_resolve_toolsets` sets the list before checking it, so it cannot refuse a widening); lets Team's lead give each free worker only the tools its part needs; tests in `tests/team/test_hermes_task_toolsets.py` | `[team] Kanban: per-task toolsets that narrow the profile` |
| 2026-10-27 | `portkey/` | `src/index.ts` | 38, 103-104 | import and `app.use('/v1/*', configByName)` before the first `/v1` route, so `x-portkey-config: <name>` is resolved from `$PORTKEY_CONFIG_DIR/<name>.json` before the validator and handlers see it | `a87204e7` |
| 2026-10-27 | `portkey/` | `src/start-server.ts` | 19-21, 152, 155 | `--host=` argument passed to `serve({hostname})`; without it `@hono/node-server` listens on every interface | `a87204e7` |
| 2026-10-27 | `portkey/` | `src/team/configByName.ts`, `src/team/configByName.test.ts` | new | the resolver and its 7 jest tests | `a87204e7` |
| 2026-10-29 | `portkey/` | `src/team/configByName.ts`, `src/team/configByName.test.ts` | Team file | fail closed: a `/v1` request with no config name, or an inline JSON config, is a 400; 9 jest tests | `[team] Portkey: refuse requests without a config name` |
| 2026-10-27 | `portkey/` | `src/middlewares/log/index.ts` | 3, 97-98 | import and one call after `next()`: append a JSON line per `/v1` request to `$PORTKEY_REQUEST_LOG` | `02cd4031` |
| 2026-10-27 | `portkey/` | `src/team/requestLog.ts`, `src/team/requestLog.test.ts` | new | the line builder and its 3 jest tests | `02cd4031` |
| 2026-11-01 | `portkey/` | `src/providers/google/chatComplete.ts` | 185, 248-249, 256-258, 663, 721, 774-775, 851 | Gemini tool round trip: a `tool` message is a `user` turn, not the dead `function` role; the function response carries the tool's real name looked up from the assistant's `tool_calls` by id; `thoughtSignature` comes back out as `extra_content.google.thought_signature` on the tool call and is sent back in; `completion_tokens` includes `thoughtsTokenCount` (both transforms) so the ledger's output count is the billed one | `[team] Portkey: Gemini and Cohere tool round trip` |
| 2026-11-01 | `portkey/` | `src/providers/google-vertex-ai/chatComplete.ts` | 565, 667-668 | `completion_tokens` includes `thoughtsTokenCount`, same as above | same |
| 2026-11-01 | `portkey/` | `src/providers/cohere/chatComplete.ts` | 39-40 | pass the whole message through, so `tool_calls` and `tool_call_id` survive instead of only `content` | same |
| 2026-11-01 | `portkey/` | `src/types/requestBody.ts` | 273 | `ToolCall.extra_content.google.thought_signature` | same |
| 2026-11-01 | `portkey/` | `src/providers/toolRoundTrip.test.ts` | new | 4 jest tests for the three provider changes | same |

Run the patch tests with
`npx --prefix portkey jest --config portkey/jest.config.js --rootDir portkey src/team src/providers/toolRoundTrip`
(`npm --prefix portkey run test:gateway` runs `jest src/`, where 8 upstream
suites already fail at v1.15.2 for want of API keys and of ESM support in
ts-jest; the two `src/team` suites pass).

## Facts re-checked against the merged Hermes (e5ea5fd9), 2026-10-27

| Fact | Where now |
|---|---|
| Install and run from a checkout go through upstream's package manager: `scripts/run-in-hermes-env CMD` (or `source ./activate`), which runs `setup-hermes.sh --runtime-only`, provisions a pinned Python 3.14, uv, node and tools into `$HERMES_RUNTIME_DIR`, and builds the dependency venv under `$HERMES_HOME/installs/<hash>/environments/<hash>/venv`. The command is `python3 ./hermes`; `PYTHONPATH` is the checkout plus that venv. Raw pip/uv into that environment is forbidden by `pm/AGENTS.md`. | `scripts/_activation.sh`, `setup-hermes.sh:161`, `pm/AGENTS.md` |
| `requires-python = ">=3.11,<3.15"` in `pyproject.toml:15`; `CONTRIBUTING.md:118` says PM provides 3.14 and it did (`python-3.14.9`). Node `>=26` (`package.json` engines). | `pyproject.toml`, `package.json` |
| Core `dependencies` no longer carry `openai` for Python < 3.14; PM prepares extras on demand. `[all]` and the `dev` dependency group exist; `dev` is a group, not an extra. | `pyproject.toml:34-44, 506, 539-553` |
| Packages are an explicit include list; `team` and `team.*` appended (`[team]`). | `pyproject.toml:672-699` |
| A `standalone` plugin (the default kind) is opt-in via `plugins.enabled`, bundled or not; kinds are `standalone, backend, exclusive, platform, model-provider`. | `hermes_cli/plugins_manifest.py:27, 359-363` |
| Hook names (`VALID_HOOKS`): `pre_tool_call`, `post_tool_call`, `transform_*`, `pre_verify`, `pre_api_request`, `post_api_request`, `api_request_error`, and the rest. `llm_request` middleware. | `hermes_cli/plugins.py:110-119`, `hermes_cli/middleware.py:21` |
| `kanban create --model` / `--provider` (dest `model_override`, `provider_override`) still bypass the profile pin. | `hermes_cli/kanban_parser.py:199-203` |
| `platform_toolsets` per profile. | `cli-config.yaml.example:1408-1417` |
| Dashboard plugin discovery moved to `hermes_cli/web_server_dashboard.py` (`_discover_dashboard_plugins`). | `hermes_cli/web_server_dashboard.py:604` |
| `extra_headers` per custom provider, `model.extra_headers` / `model.default_headers` merge, `${VAR}` and `${env:VAR}` expansion. | `hermes_cli/config.py:593-598, 880-887, 1443-1462` |
| `compression.threshold` is a fraction of `context_length` (default 0.50), not a token count; `agent.max_turns` default 500. | `cli-config.yaml.example:654-700, 1190-1195` |
