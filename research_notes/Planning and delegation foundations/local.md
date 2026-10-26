# Team and Hermes planning and delegation audit

## What actually replaced Claude Code, and what did that remove?

### Takeaway
Team still executes the Claude Code CLI binary for subscription inference. It replaces the normal coding-agent workflow with a tool-free structured-response transport, while the actual agent loop now belongs to the full vendored Hermes AIAgent. Missing boss capabilities are therefore a mixture of deliberately removed Claude Code behavior and Hermes features that Team has not exposed.

### Cited Findings
- Claude invocation is `claude -p`, with `--safe-mode`, empty settings sources, strict empty MCP config, `--tools ""`, disabled slash commands/Chrome, no session persistence, an explicit system prompt, JSON schema, and max-turns 3. Subscription auth is verified as claude.ai/firstParty and provider override credentials are scrubbed. This is not a direct model API replacement and not ordinary Claude Code operation. [Transport](/Users/fullmoney/work/Team/team/claude_transport.py:24)
- The adapter sends Hermes's system/developer messages, conversation history and tool schemas; translates assistant requests into native tool calls; refuses requests for unexposed tools. It is currently text-only. [Adapter](/Users/fullmoney/work/Team/team/subscription_gateway.py:34)
- The native process imports actual fork `run_agent.AIAgent` and SessionDB, supplies Team overlays, enables native role tools, and calls `run_conversation`. The thin interactive wrapper recognizes only /quit and /exit, not native Hermes slash-command dispatch. [Native entry](/Users/fullmoney/work/Team/team/hermes_native.py:69)
- Boss receives custom Team tools plus skills, memory, session_search, todo, clarify. It does not receive delegation, file/terminal/web tools or generic write access. [Boss toolsets](/Users/fullmoney/work/Team/team/hermes_native.py:32)
- Boss repository read/list executor is rooted at Team; path resolution rejects reads outside that root. Consequently it cannot independently inspect an attached target's source merely because that target is listed. [File scope](/Users/fullmoney/work/Team/team/harness_tools.py:43)
- Native clarify is present but Team's callback prints only `question` and ignores the supplied `choices`. The upstream tool supports structured choices and multiple questions; current Team UI exposes less of it. [Team callback](/Users/fullmoney/work/Team/team/hermes_native.py:97); [native callback contract](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/clarify_tool.py:112)
- Subscription profile construction specifies local custom endpoint, no configured fallback providers, explicit auxiliary routing, and clears inherited provider environment. Boss coding_context is off. This is configuration evidence, not a proof that every optional future integration will respect that boundary. [Profile and environment](/Users/fullmoney/work/Team/team/hermes_runtime.py:51)

### Inferences
- Claude's underlying reasoning ability was not removed, but normal Claude Code exploration, planning, delegation tools, settings/skills loading, and session lifecycle are not available inside this invocation. A model cannot create a usable subagent without runtime tools to do so.
- Re-enabling native Claude Code tools inside the transport would create two competing agent/tool loops and unclear authority. A deliberately bounded external specialist is a different design and can be evaluated separately.

### Gaps
- This audit did not execute optional delegation through the subscription gateway, so its concurrency, cancellation and billing behavior are not verified live. No running fleet/process claims are made.

## What planning and subagent capabilities already exist in Hermes?

### Takeaway
Hermes already supplies focused delegated children, native todo tracking, clarification, a plan authoring prompt and Kanban decomposition. Team should adapt those primitives first. However, Hermes's `/plan` is instruction-based, and ordinary delegated children inherit broad tools: neither is automatically a host-enforced read-only explorer.

### Cited Findings
- `delegate_task` spawns isolated child AIAgent conversations, their own task IDs/terminal sessions, focused goal/context prompts, and summarizes results into the parent context. It supports individual and parallel batches; top-level dispatch is asynchronous and child orchestrators can synthesize their children. [Delegate module](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:1)
- Delegation has active child listing, steering and interruption; bounded concurrency/depth and optional worktree isolation controls exist. These features exist in upstream code, not in the current Team boss surface. [Control functions](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:323); [configuration readers](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:903)
- Children normally inherit parent toolsets. Child builder accepts a narrowed toolset intersected with parent capability, but public creation paths currently pass `toolsets=None`. Blocking removes delegation (unless permitted by depth), clarification, shared memory writes, messaging/cron and Kanban; it does not blanket-deny file writes or shell tools. Mixed toolset handling exists, but is not a read-only capability policy. [Blocked names](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:50); [blocked toolsets](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:1363); [child inheritance](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:1803); [public dispatch](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:4199)
- Delegate child provider/model/base URL/key can inherit from parent or be overridden by trusted delegation configuration. Thus reuse of the local subscription gateway is structurally possible; overrides must remain constrained to the user's approved subscription/free routes. [Credential inheritance](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/delegate_tool.py:1927)
- Hermes `/plan` uses `build_plan_prompt`. It instructs the live agent to inspect read-only, avoid implementation, and save a Markdown plan, using the existing toolset. Its own module explicitly says there is no engine or model-tool footprint. It does NOT install a read-only sandbox or remove mutation tools. [Plan rules](/Users/fullmoney/work/Team/vendor/hermes-agent/agent/plan_prompt.py:1); [CLI handler](/Users/fullmoney/work/Team/vendor/hermes-agent/hermes_cli/cli_commands_mixin.py:2245)
- Native plan craft includes goal, assumptions, approach, exact files/tasks, verification commands, risk/tradeoff/open questions, and clarification when genuinely underspecified. Default storage is workspace `.hermes/plans/`, but an explicit runtime-provided target path is supported. [Plan craft](/Users/fullmoney/work/Team/vendor/hermes-agent/agent/plan_prompt.py:29)
- Native todo list has nested tasks and pending/in_progress/completed/cancelled states; guidance requires verification before marking complete. It is already exposed to the boss. [Todo schema](/Users/fullmoney/work/Team/vendor/hermes-agent/tools/todo_tool.py:380); [boss tools](/Users/fullmoney/work/Team/team/hermes_native.py:52)
- Native Kanban decomposer returns structured child task graph with title/body/assignee and parent indices, picks real profiles, supports parallel independent tasks and can choose no fanout. This machinery exists upstream, while Team currently copies its policy into a role skill rather than exposing its complete workflow. [Decomposer](/Users/fullmoney/work/Team/vendor/hermes-agent/hermes_cli/kanban_decompose.py:52); [Team skill](/Users/fullmoney/work/Team/roles/boss/skills/kanban-orchestration/SKILL.md:8)

### Inferences
- Best near-term reuse: native Hermes plan craft + native todo/clarify + constrained delegation + existing Kanban. No need to invent another agent engine.
- Do NOT simply append `delegation` to the boss toolset and call it a read-only explorer. Add host-controlled child capabilities (target read/search and selected evidence tools only), explicit depth/concurrency/budgets, and route inheritance checks. Test custom toolset expansion so children cannot inherit Team task creation, mutation or shared skill writes.
- Team can reuse `build_plan_prompt` but should pair it with a plan-only tool surface and a narrow plan-save tool rooted in Team state, since Team's convention is not to place its state into target repositories. General `write_file` is unnecessary for this role.
- A plan-critic helper can use the same constrained delegation mechanism with a different brief: inspect unstated assumptions, incompatible interfaces, missing acceptance criteria and weak verification before tasks are queued.

### Gaps
- No live execution test of these proposed combinations was performed. Some upstream tool descriptions retain older role-language while code now derives delegation capability from depth; recommend reading executable code/config rather than reproducing the older wording.

## Which gaps matter most for this user's planning and goal setting?

### Takeaway
The key deficiency is not lack of another smart model. It is that the boss cannot fully research target code, lacks transient research helpers and an explicit persistent plan/readiness contract, and has a deliberately limited task-graph adapter. Retain one accountable boss and separate exploratory subagents from durable implementation workers.

### Cited Findings
- Boss instructions explicitly say no model-spawning tool and restrict read/list to Team itself. They reserve scope/spending/product decisions for Eli and describe no automatic role pipeline. [Boss policy](/Users/fullmoney/work/Team/plugin/agents/boss.md:25)
- Task creation only accepts discovered active profiles; parent Team goals must already have passed and landed. Pending dependency graphs are rejected before creating a worktree. [Creation gate](/Users/fullmoney/work/Team/team/hermes_boss.py:104)
- Team's own orchestration skill states the pending graph limitation and warns not to promise automatic spec→test→implement→review merely because roles exist. [Current contract](/Users/fullmoney/work/Team/roles/boss/skills/kanban-orchestration/SKILL.md:80)
- `team run` dispatches Kanban tasks then processes active goals; grades work by commits/clean tracked worktree/current-main merge/check command and lands passing branches. This is durable work coordination, unlike a short research helper. [Dispatch](/Users/fullmoney/work/Team/team/run.py:281); [grader](/Users/fullmoney/work/Team/team/run.py:674)
- Current start_goal immediately creates a goal/worktree/free attempt. It has no persistent planning-readiness stage. [Goal creation](/Users/fullmoney/work/Team/team/run.py:725)
- Decisions answer() persists an answer and appends decisions.md but does not itself resume blocked tasks. [Answer behavior](/Users/fullmoney/work/Team/team/decisions.py:72)

### Inferences
Recommended order, explicitly proposals rather than implemented changes:
1. Give boss host-scoped read/search access to the selected attached target and improve clarification rendering to show choices. This makes its plans informed rather than dependent on task descriptions.
2. Add an explicit plan workflow using Hermes plan prompt and a persistent Team-state artifact: goal, scope/non-goals, evidence, decisions, acceptance criteria, task ownership, dependencies, verification, unresolved questions. Readiness gate should block dispatch only for consequential unresolved decisions or missing executable acceptance, not impose ritual approval on every tiny task.
3. Add narrowly scoped Hermes exploration and plan-critique children. They return evidence/recommendations to the boss; they do not create Kanban implementation tasks or mutate the project. Keep recursion flat initially and bound total task/model effort.
4. Preserve Kanban/worktrees/ledger/grading for durable implementation workers. Integrate pending dependency graphs and specialist stages as separate orchestration work, not by pretending transient delegates replace that machinery.
5. Feed plans, helper findings, clarified decisions and rejected plan alternatives into Optimizer evidence so planning improvements can be evaluated alongside execution outcomes.
6. Optional experiment later: a separately named Claude Code or Codex planning specialist with explicit input/output and isolated scope. Benchmark whether it improves plan quality versus the native Hermes approach before adding another harness. Do not re-enable native tools in the subscription serialization transport itself, switch to a paid SDK/API implicitly, or move execution to cloud.

### Gaps
- We have not measured planning quality before/after any recommendation. The presence of Hermes primitives does not establish parity with Claude Code/Codex user experience or quality.
- No automatic readiness contract, critic helper, complete pending DAG flow or automatic specialist chain is implemented by this research. This audit only wrote notes.
