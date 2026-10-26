# Scoped planning improvements for the other roles

This is a source review of the existing role instructions and execution path,
performed while the boss's scoped-planning integration is being built. The
role-specific recommendations below do not change those roles’ instructions or permissions.
The boss integration now delivers scoped assignment briefs and exposes saved
planning evidence to the Optimizer; those shared changes are noted below.
No model-quality improvement was measured by this review.

## Current roles

Implement and Optimizer are marked active. Spec, Test and Review have real
profiles, instructions and skills, but remain marked planned. The `hands_to`
fields describe the intended sequence; they do not implement automatic stage
transitions. An agent having a profile does not mean it is currently running.

| Role | Useful existing behavior | Concrete scoped-work improvement | Enforcement needed |
|---|---|---|---|
| Implement | Owns named files, reads its contract, runs checks, commits | Start from a plan slice containing owned files, relevant interfaces, investigation paths, explicit exclusions and required evidence. If another file must change, return the discovered dependency and proposed scope expansion. | Compare the diff with approved change scope before landing. File/shell tools currently permit more than the prose ownership rule. |
| Spec | Defines interfaces, behavior, task ownership and verification | Consume the boss's already gathered evidence. Investigate unresolved interfaces rather than repeating project exploration. Produce separate change, inspection and verification scopes for each task. | Validate the task contract, paths, ownership conflicts and unresolved decisions before dispatch. Existing task-list prose describes a gate not found in the current generic execution path. |
| Test | Separates test-writing from real verification; asks for evidence | Select targeted tests from acceptance criteria and affected dependency boundaries. Explain broader regression checks without reading unrelated project source. Keep real integration verification where it matters. | Stage-specific checks: test-writing only changes permitted tests and establishes a meaningful failing baseline; verification preserves evidence and cannot silently change production code. |
| Review | Compares the diff and evidence to the goal and contract | Review the changed area and its affected interfaces first. Expand only when a concrete dependency, invariant or security boundary warrants it. Report each unmet criterion with evidence and a bounded fix request. | Prefer read-only inspection tools; if test execution is allowed, distinguish it from broad shell/file mutation. A `land` verdict remains a recommendation, not merge authority. |
| Optimizer | Inspects evidence, cites retrieved records and proposes rather than applies | Add plan ID/revision, subsystem, inspection expansion reasons and acceptance failures to its evidence. Compare similar task classes instead of treating unrelated jobs as one cohort. | Read scoped plan records through an explicit evidence tool. Distinguish assignment failures from plan deficiencies; validate the sample and denominators before claiming improvement. |

Sources: [Implement role](../roles/implement/SOUL.md),
[Spec role](../roles/spec/SOUL.md), [Test role](../roles/test/SOUL.md),
[Review role](../roles/review/SOUL.md), [Optimizer role](../roles/optimizer/SOUL.md),
[generic grader](../team/run.py), and [optimizer evidence](../team/optimizer.py).

## Differences that matter before adopting these changes

**Instruction updates currently propagate differently.**
`team/profiles.py::_write_soul_md` retains an existing free-profile identity;
`_write_skills` fills missing files and preserves existing ones. This protects
learned content, but it means editing a versioned role file does not automatically
refresh an existing free worker. Native subscription workers explicitly receive
source role instructions and skills during launch in `team/hermes_native.py`.
A common scoped assignment should therefore travel in the task brief, or in a
managed versioned overlay separate from learned identity and skills. Do not solve
this by overwriting every worker's learned files.

**The boss now supplies a structured plan slice.**
`planning_dispatch` validates the saved contract and embeds its ID/revision,
assigned acceptance criteria, relevant inspection roots, exact owned files and
verification context in the task body. `team/run.py::_brief` carries that body
into both free and subscription attempts. Manual `team goal` entry still uses
the generic goal interface; specialist artifact/stage enforcement remains work
to do before activating the full multi-role pipeline.

**Prose restrictions are not host restrictions.**
The worker manifests grant terminal and file toolsets; the native wrapper enables
them. The generic grader verifies commits, tracked worktree cleanliness, merging
main and the configured check. It does not currently enforce each role's claimed
file restrictions or expected artifact shape. Stage-specific validators and diff
checks are required before treating those rules as guarantees. They should account
for separately authorized integration changes, including merge conflict resolution.

**Some role skills describe intended machinery as existing.**
Spec's `plan-a-goal` skill refers to `team talk`, which is not an available CLI
command in the inspected code. Its `task-list` skill says a gate rejects invalid
contracts, but that validator is not part of the inspected run path. These should
be reconciled with the real planning contract before activating that stage.

**Verification can be broader than reading.**
The Test and Implement roles' full-check requirements can remain useful even when
inspection is narrow. Running a regression suite does not require loading every
source file into the model. Do not weaken verification simply to reduce context.
Likewise, Review may need to inspect a shared dependency even when it is outside
the change scope; that calls for an explained inspection expansion, not permission
to edit it.

## Suggested next order

1. **Integrated for boss-created tasks:** pass the goal/plan revision and three
   scopes into each dispatched brief, narrowed to that task's relevant roots.
2. Add a pre-landing comparison between committed changes and approved file scope.
3. Before activating Spec/Test/Review as automatic stages, implement their distinct
   artifact, test-baseline and evidence gates, then remove stale skill claims.
4. **Planning records integrated:** Optimizer now lists recent plan metadata and
   reads saved plan records through its existing evidence tools. Task-class
   tagging and evaluation across comparable cohorts remain future work.
   Evaluate whether a planning change reduces avoidable rework against comparable
   baseline tasks; do not infer causality from aggregate pass counts alone.

This review recommends common contracts and narrow role-specific rules. It does
not recommend giving every worker its own planning swarm or asking each role to
rediscover the whole project.
