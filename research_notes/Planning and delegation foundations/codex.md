# Codex planning and delegation foundations

Research date: 2026-10-26. Official pages were retrieved, including their Markdown bodies; this is research, not an integration change. Current availability can vary by installed client/account. The ExecPlans recipe is explicitly archived; its planning pattern remains useful, but its model/version examples should not be copied as current defaults.

## Which Codex planning patterns are useful for Team?

### Takeaway

Separate agreeing on an approach from maintaining a durable completion contract. Give the boss a living plan and a precise goal, then let its delegated work supply evidence against that goal.

### Cited Findings

- Current Codex guidance recommends starting with `/plan` when an outcome is unclear, interviewing the user and resolving constraints, then activating the refined outcome with `/goal`. Goal mode retains the objective across turns and supports steering. A goal does not expand permissions. Parallel coding chats should use separate checkouts to avoid write collisions. — [Long-running work](https://learn.chatgpt.com/docs/long-running-work)
- The Goals cookbook defines six useful elements: outcome, verification surface, constraints, resource boundaries, iteration policy, and a blocked stop condition. Goals are an evidence-checked continuation mechanism rather than just a longer first prompt; the lifecycle includes pause, resume, clear, success, budget stop, and blockers. — [Using Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
- ExecPlans are a documented prompting convention, not a special model capability. The archived recipe describes self-contained living plans that can carry work across fresh contexts, with progress, discoveries, decision rationale, retrospective, observable acceptance, and recovery guidance. It recommends small verifiable milestones and feasibility prototypes where uncertainty is high. — [Using PLANS.md for multi-hour problem solving](https://developers.openai.com/cookbook/articles/codex_exec_plans)

### Inferences

- Highest priority: adapt these planning artifacts and lifecycle concepts into Hermes/Team; they do not require using Codex as the boss or importing another full harness.
- A Team goal should carry both the desired end state and what evidence proves it. Its plan should capture milestones, assumptions, decisions, dependencies and recovery. A task check passing must not silently substitute for the whole user's objective.
- Preserve the user's decision ownership. The archived ExecPlan template includes autonomous ambiguity resolution; Team should adapt that wording rather than import it unchanged, because Eli explicitly wants consequential ambiguities surfaced.
- Planning should allow exploratory read-only investigations before the boss commits to a work split. Plans can change as evidence arrives, while the goal and constraints remain explicit.
- Do not conflate a model-maintained checklist with the authoritative queue/ledger. Team's code should retain execution state; the plan describes intent and evidence.

### Gaps

- This research did not verify which Plan/Goal features the locally installed Codex build exposes. It does not establish that arbitrary Codex goal tools can be transplanted directly into Hermes.
- The archived ExecPlan recipe's reported long run is an example, not evidence that copying it improves Team's performance. Evaluate the adaptation against representative Team tasks.

## How does Codex delegate, and what should Team borrow?

### Takeaway

Use short-lived, bounded helper agents to investigate before and during planning, with summaries returned to the boss. Keep durable implementation work on Team's board.

### Cited Findings

- Codex local subagent workflows are available by default in current releases, with delegation requested by the user or applicable project/skill instructions. The runtime handles spawning, follow-up instructions, waiting and thread controls. Documentation recommends read-heavy exploration, tests, triage and summarization first; parallel code edits need more coordination. Summarized results keep noisy intermediate work out of the main context. — [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- Built-in agent types include general-purpose, worker, and explorer. Custom agent TOML files define a name, description and instructions and can set models/reasoning or disable supported capabilities. They inherit parent permissions/provider/MCP rather than establishing an independent sandbox. Model-only instructions to avoid edits are not a read-only security boundary. Concurrency controls limit active child threads. — [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

### Inferences

- Proposed helper roles: Explorer (map target code and dependencies), Plan Critic (test assumptions and missing decisions), Verification Designer (turn desired behavior into acceptance evidence). Their outputs should name findings, file references, uncertainties and next questions, not dump complete transcripts.
- Each delegated assignment should carry purpose, allowed scope/tools, expected return format, budget and stop criteria. The boss synthesizes competing findings rather than letting helpers decide user-facing scope independently.
- Separate ephemeral assistance from durable execution. An explorer answers a planning question; a Kanban worker owns a deliverable with retries, checks and landing. Both may use Hermes while maintaining different lifecycle contracts.
- Team should enforce helper capabilities in actual tool allowlists and filesystem boundaries; custom role prose is not an enforcement mechanism.
- Keep tight concurrency and make the boss able to steer/cancel helpers. More agents consume more inference and are not automatically better; avoid fan-out for tightly sequential work or shared writes.

### Gaps

- This research does not claim the full Codex delegation machinery is available merely by changing a model identifier or copying its agent TOML files into Hermes. Those are harness-managed tools and configuration semantics.
- No recommendation here assumes subagents automatically share their entire reasoning or synchronize all private memories.

## Should Team embed Codex, and how does billing affect that option?

### Takeaway

Embedding Codex is an optional way to obtain a complete specialist agent under Team's supervision. It is a different architectural choice from borrowing planning conventions, and the subscription boundary must remain explicit.

### Cited Findings

- Codex SDK is intended for programmatic local agent workflows. TypeScript supports starting, continuing and resuming threads. The Python SDK controls local app-server through JSON-RPC and ships a pinned runtime; it supports thread/turn sandbox presets including read-only. — [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- App-server exposes rich-client integration: authentication, persistent threads, history, approvals and streamed events. It has resume/fork/steer/interrupt surfaces, structured plan updates and user-question requests. The documentation calls app-server experimental and not supported for production workloads. It distinguishes local/open-source uses of app-server authentication from commercial/hosted services and recommends Sign in with ChatGPT migration for the latter integration needs. — [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- Codex has ChatGPT subscription sign-in and separately billed API-key sign-in. The authentication documentation recommends API keys for general programmatic CI workflows, while documenting advanced account-auth automation separately. Do not treat all SDK or API usage as covered by the user's subscription. — [Authentication](https://learn.chatgpt.com/docs/auth)
- The old `codex mcp-server` command and standalone binary have been removed. App-server is its own protocol, not an MCP drop-in; external MCP tool connections remain supported. — [Codex MCP server removal](https://learn.chatgpt.com/docs/mcp-server)
- OpenAI's Agents API is a managed Codex harness with sessions, orchestration, compaction and recovery; it charges model API rates and separate tool/container rates. This is not the local Codex subscription route. — [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview)

### Inferences

- Recommended order: (1) borrow the planning/goal contract and bounded-helper patterns within Hermes; (2) only if needed, pilot a read-only Codex planner/reviewer through a local SDK/app-server adapter; (3) keep Team's task IDs, budgets, verification and acceptance authoritative.
- A Codex specialist can submit a proposal to the Hermes boss without becoming the global orchestrator. Team should capture its session ID and evidence, constrain its tools, and ensure it cannot dispatch competing teams behind Team's accounting.
- For this user's local subscription-only preference, verify active auth and successful authorized local invocation before any pilot. Do not choose Agents API or silently inject an API key as a fallback.
- If embedding Codex, prefer SDK-provided thread lifecycle or app-server events to parsing an interactive terminal. Do not design a new integration around removed MCP hosting examples.

### Gaps

- No SDK/app-server was installed, launched, or authenticated during this research. Runtime compatibility, account entitlement and exact Python SDK support for every Plan/Goal/subagent setting require a local pilot.
- Official docs do not establish that switching the Team boss to Codex or adding a Codex planner will improve its outcomes. That is an evaluation question.
