# Claude Code planning and delegation

## What did the transport change remove, and what remains?

### Takeaway
The current boss still invokes the Claude Code binary, but deliberately disables its normal agent surface. Claude's reasoning remains available; Claude Code's built-in exploration, planning, delegation, session lifecycle, and tool integration do not automatically come with a tool-free request. Hermes supplies its own versions of several of these facilities.

### Cited Findings
- Local transport invokes `claude -p` with `--tools ""`, safe mode, empty settings sources/MCP servers, disabled slash commands, no session persistence, a replacement system prompt, and structured JSON output. The native boss exposes Team tools plus Hermes skills, memory, session search, todo and clarification; no delegation toolset is returned. — [Transport](/Users/fullmoney/work/Team/team/claude_transport.py), [Native registration](/Users/fullmoney/work/Team/team/hermes_native.py)
- Ordinary `claude -p` is non-interactive Claude Code, not a separate raw model API. Unless stripped down, it loads the agent's tools and project/user customizations. The documented subprocess route supports structured output and continuation. — [Programmatic Claude Code](https://code.claude.com/docs/en/headless)
- Claude Code's Agent SDK embeds the Claude Code binary and its tools, agent loop, context management, permissions, sessions, hooks, subagents, MCP, and configuration. Using the SDK means using that runtime, not importing several MD files into a different runtime. Its overview directs developers to API-key authentication and says third-party offerings of claude.ai login/rate limits need prior approval. — [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- Claude Code itself supports claude.ai Pro/Max and organization subscriptions as well as Console/cloud billing. This does not establish interchangeable authentication for every SDK use case. — [Authentication](https://code.claude.com/docs/en/authentication)
- Current documentation describes separate included API credits for eligible Max/Team plans, covering API/SDK work but not Claude Code usage. These require a linked Console organization and API credentials; they do not turn SDK usage into the existing Claude Code subscription pool. Eligibility and account setup were not inspected. — [API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)

### Inferences
- Re-enabling the Agent tool inside the existing transport would introduce another tool loop underneath Hermes. That would obscure ownership of delegation, budgets and session evidence. Prefer an explicit separate worker adapter if retaining native Claude Code is desired.
- Markdown can supply procedures, but it cannot itself recreate runtime enforcement, process lifecycle, forked contexts, hooks, or checkpointing. No source establishes an importable package of Claude Code's internal Plan/Explore system prompts.

### Gaps
- We have not benchmarked native Claude Code against the Hermes-backed boss on identical planning tasks. Feature availability is not proof that one produces better plans.
- This research does not decide account-specific SDK subscription permissions or authorize switching to API billing.

## Which planning and goal-setting patterns are worth retaining?

### Takeaway
The highest-value features are structured exploration before commitment, focused clarification, a durable self-contained spec, and an independent check of the completion condition. These patterns can be implemented on Hermes without changing the model or replacing Kanban.

### Cited Findings
- Claude Code's Explore subagent is read-only code discovery; Plan is a read-only research helper used during planning. They run in separate contexts and return findings, preventing exploration from flooding the main conversation. Custom agents have their own instructions, capabilities and permissions. Current docs say Explore and Plan omit CLAUDE.md and the git-status snapshot at startup, so required constraints need explicit inclusion. — [Subagents](https://code.claude.com/docs/en/sub-agents)
- Official workflow guidance recommends plan mode before changes and using separate-context subagents for exploration. — [Common workflows](https://code.claude.com/docs/en/common-workflows)
- Anthropic recommends interviewing the user about hidden technical/UI/edge-case tradeoffs, producing a self-contained spec naming affected files/interfaces, exclusions and an end-to-end verification step, then executing from clean context. This is a procedure rather than a unique model capability. — [Best practices](https://code.claude.com/docs/en/best-practices#let-claude-interview-you)
- Plan mode is a permission policy as well as prompting. In the SDK, source edits are not auto-approved; they reach the approval callback, and main-session plan mode propagates to subagents. The documentation notes interactive-terminal exceptions when bypass permissions are available, so do not equate the mode name with an unconditional sandbox. — [SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions), [Permission modes](https://code.claude.com/docs/en/permission-modes)
- `/goal` adds a separate model evaluator after turns. It assesses a stated condition from conversation evidence, not independent file reads or commands. Good conditions specify a measurable end state, a proving check, and constraints. This provides persistence beyond a one-turn response, but a model verdict is not equivalent to independently executed tests. — [Goals](https://code.claude.com/docs/en/goal)
- `TaskCreated` hooks can reject task creation, and `TaskCompleted` hooks can prevent completion until checks pass. `Stop` and `SubagentStop` offer lifecycle points for checking work. — [Hooks](https://code.claude.com/docs/en/hooks)

### Inferences
Ranked recommendations for Team:
1. Add a boss-accessible, target-scoped read-only exploration helper. It should report relevant files/interfaces, constraints, uncertainties and evidence without modifying code or starting build tasks.
2. Make a plan artifact the contract: goal, non-goals, constraints, questions, acceptance checks, task ownership and dependencies. Enforce readiness in Team code before build dispatch. Preserve the user's preference for clarification only when materially needed; do not add a ritual approval for every trivial step.
3. Add a planning critique pass for substantial changes: another isolated helper searches for missing requirements, conflicting ownership and untestable completion criteria. Boss synthesizes; user owns product/scope tradeoffs.
4. Borrow the goal-evaluator pattern for whole-goal progress, but retain actual Team tests and deterministic landing gates. The evaluator should identify missing acceptance evidence rather than overrule failed checks.
5. Add lifecycle validation at task creation and handoff. Existing generic grading should grow role-specific checks instead of relying on instructions alone.

### Gaps
- Need decide whether helper calls use existing Hermes delegation facilities or a controlled native Claude Code worker; another researcher is auditing Hermes reuse.
- Neither Plan mode nor a Plan subagent automatically defines a durable product goal graph. Team still owns that contract.

## Should Team use native Claude Code teams, skills, or SDK integration?

### Takeaway
Use native Claude Code as an optional bounded worker when its full harness is valuable. Do not add a second peer orchestration authority underneath the boss merely to obtain parallel research. Borrow the mechanisms at the appropriate layer first.

### Cited Findings
- Agent teams coordinate independent Claude Code sessions using shared tasks and direct messages. They remain experimental and have resume/task-status/shutdown limitations. They suit independent parallel investigation, review and module work; tightly dependent or same-file work carries more coordination overhead. — [Agent teams](https://code.claude.com/docs/en/agent-teams)
- Important current nuance: a teammate spawned while the lead is already in plan mode can have its plan auto-approved when ready if its permission mode has not changed. This must not be represented as guaranteed lead or human review. Ordinary subagent delegation and durable team collaboration are different coordination models. — [Teammate planning](https://code.claude.com/docs/en/agent-teams#have-teammates-plan-before-implementing)
- Skills follow the open Agent Skills format, but Claude Code adds runtime-specific features such as forked execution, hooks and dynamic context injection. Copying SKILL.md prose can transfer a procedure; Claude-specific frontmatter and body behavior do not automatically operate elsewhere. — [Skills](https://code.claude.com/docs/en/skills#using-skill-frontmatter-outside-claude-code)

### Inferences
- Preferred architecture: one Team/Hermes boss; short-lived exploration/critique helpers for preparation; Kanban workers for durable deliverables; Team's code for readiness, grading and landing; optimizer for evidence-based recommendations.
- Optional native adapter: Team dispatches one bounded research/review/implementation job to a native Claude Code CLI session with explicit project scope, allowed capabilities, turn/time budget, no independent merging, and a structured report. Its internal subagents remain inside that job. Ledger records its child activity or clearly reports visibility limits. This retains useful native capabilities without letting them compete with Kanban ownership.
- Alternative full replacement with Agent SDK is technically supported, but changes runtime ownership and authentication/billing assumptions; it is not the first recommendation for this user's stated Hermes-first, subscription-oriented design.
- Treat native teams as an optional experiment, not a prerequisite. Do not copy inaccessible proprietary system prompts or imply the public repository is the entire Claude Code harness.

### Gaps
- No integration or configuration changes were made in this research. Native-adapter security, telemetry and subscription behavior need a separate review before implementation.
