# Provenance: what this repository is built from

This repository is a fork of Nous Research's Hermes Agent at its root, with
other projects forked in as subtrees and Team layered on top. Upstream files
are never edited in place. Every exception is a marked `[team]` commit listed
here with file and line.

## Upstreams

| Project | Where | How it came in | Pinned | Update |
|---|---|---|---|---|
| NousResearch/hermes-agent | repo root | `git merge --allow-unrelated-histories upstream/main` on 2026-10-27, merge commit `8ee29e6d` | `e5ea5fd9` (upstream main, 2026-10-26) | `git fetch upstream main && git merge upstream/main` |
| NousResearch/hermes-agent-self-evolution | `self-evolution/` | `git subtree add --squash`, 2026-10-27 | `0a929e3a` (main) | `git subtree pull --prefix=self-evolution <url> main --squash` |
| Portkey-AI/gateway | `portkey/` | `git subtree add` (full history), by the gateway session | `v1.15.2` | `git subtree pull --prefix=portkey <url> <tag>` |
| BerriAI/litellm | `litellm/` | `git subtree add --squash`, by the gateway session | `v1.106.4` | `git subtree pull --prefix=litellm <url> <tag> --squash` |

Remotes: `upstream` = `https://github.com/NousResearch/hermes-agent.git`. No
`origin` is set; pushing is only ever on Eli's say-so, and the intended target
is his GitHub fork `elimoneybags1/hermes-agent`.

The repository before the fresh start is tag `before-fresh-start`
(`25b8aca`). `git checkout before-fresh-start` restores the whole old tree.

## What Team owns

`team/`, `plugins/team/`, `integrations/`, `tests/team/`, `PROVENANCE.md`,
`CLAUDE.md`, `.work.json`, `IDEAS.md`, `FRESH-START.md`, `PLAN.md`,
`HANDOFF-GATEWAY.md`, a `# Team` block appended to `.gitignore`, and three
lines in `pyproject.toml` (the `team` script, the `team` package include, and
its package-data).

## Marked patches to upstream files

None yet. Each entry: date, directory, file, lines, one-line reason, commit.

## Facts re-checked against the merged Hermes (e5ea5fd9)

Filled in as Phase 1 step 8 of `PLAN.md` runs: packages include list,
`plugins.enabled` gating, `VALID_HOOKS`, `kanban create --model/--provider`,
`platform_toolsets`, dashboard plugin discovery and manifest keys,
`extra_headers` and `${VAR}` expansion.
