"""Thin embedding entry point. The fork's AIAgent owns the complete agent lifecycle."""
from __future__ import annotations

import json
import os
import sys
import time
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORK = ROOT / 'vendor' / 'hermes-agent'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FORK))


def register_team_tools(root: Path, role: str, decision_file=None, planning_context=None):
    import model_tools
    from tools.registry import registry
    from tools import kanban_tools
    from toolsets import create_custom_toolset
    from team import hermes_boss
    from team.harness_tools import ToolExecutor, DESCRIPTIONS

    # Ensure builtins are discovered before applying explicit process-local overrides.
    model_tools.get_tool_definitions(enabled_toolsets=['skills'], quiet_mode=True)
    if role == 'optimizer':
        from team.optimizer import register
        return register(root)
    names = []
    if role == 'boss':
        executor = ToolExecutor(root, ['team'], root=root)
        for name in ['team']:
            props = {'argv': {'type': 'array', 'items': {'type': 'string'}}} if name == 'team' else {'path': {'type': 'string'}}
            registry.register(name=name, toolset='team_boss',
                              schema={'name': name, 'description': DESCRIPTIONS[name], 'parameters': {'type': 'object', 'properties': props, 'required': list(props)}},
                              handler=lambda args, _name=name, **kw: executor.execute(_name, args, 120), override=True)
            names.append(name)
        for name, schema in hermes_boss.schemas().items():
            if name == 'kanban_create':
                continue  # Native boss dispatches only through the saved-plan readiness gate.
            def dispatch(args, _name=name, **kw):
                try:
                    from team.planning import PLAN_ONLY
                    if PLAN_ONLY.get() and _name == 'kanban_comment':
                        raise ValueError('Planning-only turn cannot mutate the board')
                    hermes_boss.validate(_name, args)
                    if _name in hermes_boss.NATIVE:
                        return getattr(kanban_tools, '_handle_' + _name.removeprefix('kanban_'))(args)
                    return hermes_boss.execute(root, _name, args)
                except Exception as exc:
                    return json.dumps({'error': str(exc)})
            registry.register(name=name, toolset='team_boss', schema=schema, handler=dispatch, override=True)
            names.append(name)
        create_custom_toolset('team_boss', 'Team policies on native Hermes orchestration', tools=names)
        from team.planning import register as register_planning
        planner = register_planning(root)
        if planning_context is not None:
            planning_context['planner'] = planner
        return ['team_boss', 'team_planning', 'team_planning_read', 'skills', 'memory', 'session_search', 'todo', 'clarify']
    if decision_file:
        def decision(args, **kw):
            if not all(isinstance(args.get(k), str) and args[k].strip() for k in ('question', 'context', 'recommendation')) or not isinstance(args.get('options'), list):
                return json.dumps({'error': 'decision needs question, context, options, recommendation'})
            Path(decision_file).parent.mkdir(parents=True, exist_ok=True)
            Path(decision_file).write_text(json.dumps(args))
            return json.dumps({'recorded': True, 'instruction': 'Stop and report the decision to Team.'})
        registry.register(name='team_decision', toolset='team_worker', schema={
            'name': 'team_decision', 'description': 'Request a human decision through Team and stop; do not guess.',
            'parameters': {'type': 'object', 'properties': {**{k: {'type': 'string'} for k in ('question','context','recommendation')}, 'options': {'type':'array','items':{'type':'string'}}}, 'required':['question','context','options','recommendation']}}, handler=decision)
        create_custom_toolset('team_worker', 'Team worker handoff', tools=['team_decision'])
    config = tomllib.loads((root / 'roles' / role / 'role.toml').read_text())
    configured = [t for t in config.get('tools', []) if t not in {'read-only','orchestrator','kanban'}]
    return list(dict.fromkeys([*configured, 'skills', 'memory', 'session_search', 'todo', *(['team_worker'] if decision_file else [])]))


def ask_clarification(question, choices=None):
    print(str(question), flush=True)
    options = choices or []
    for index, choice in enumerate(options, 1):
        print(f'{index}. {choice}', flush=True)
    answer = input('> ')
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return str(options[int(answer)-1])
    return answer


def main() -> int:
    payload = json.loads(Path(sys.argv[1]).read_text())
    root, cwd, role = Path(payload['root']), Path(payload['cwd']), payload['role']
    from run_agent import AIAgent
    from hermes_state import SessionDB

    planning_context = {}
    enabled = register_team_tools(root, role, payload.get('decision_file'), planning_context)
    session_id = payload.get('resume') or str(uuid.uuid4())
    db = SessionDB()
    if payload.get('resume') and db.get_session(session_id) is None:
        raise ValueError('Unknown Hermes session for this role; legacy Team transcripts are not Hermes sessions')
    history = db.get_messages_as_conversation(session_id) if payload.get('resume') else []
    instruction = root / 'plugin/agents/boss.md' if role == 'boss' else root / 'roles' / role / 'SOUL.md'
    overlay = instruction.read_text() if instruction.exists() else ''
    # Hermes already injects KANBAN_GUIDANCE for agents with kanban_show.
    # Add only Team-specific policy; duplicating the native block wastes every call.
    if role not in {'boss', 'optimizer'}:
        overlay += '\nUse memory and skills to retain verified, role-specific lessons. Do not alter shared role policy or claim test success without running its check.'
    for skill in sorted((root / 'roles' / role / 'skills').glob('*/SKILL.md')):
        overlay += '\n\n' + skill.read_text()
    agent = AIAgent(provider='custom', api_mode='chat_completions', model=payload['model'],
                    base_url=os.environ['TEAM_INFERENCE_URL'], api_key=os.environ['TEAM_INFERENCE_TOKEN'],
                    enabled_toolsets=enabled, max_iterations=payload['max_turns'],
                    run_budget_seconds=payload['timeout_s'], platform='cli', quiet_mode=True,
                    session_id=session_id, session_db=db, save_trajectories=False,
                    skip_background_review=role in {'boss', 'optimizer'},
                    clarify_callback=ask_clarification if payload.get('prompt') is None else None)
    if planning_context.get('planner'):
        planning_context['planner'].parent_agent = agent
    # Full upstream prompt construction, memory, compression, tools and persistence stay in AIAgent.
    print(f'Hermes-backed Team {role} · {session_id}', flush=True)
    start = time.monotonic()
    final = None
    initial_prompt = payload.get('initial_prompt') if payload.get('prompt') is None else None
    try:
        while True:
            prompt = payload.get('prompt')
            if prompt is None and initial_prompt:
                prompt, initial_prompt = initial_prompt, None
            if prompt is None:
                try:
                    prompt = input('you> ')
                except EOFError:
                    break
            if prompt.strip() in {'/exit', '/quit'}:
                break
            if not prompt.strip():
                if payload.get('prompt') is not None:
                    break
                continue
            from team.planning import PLAN_ONLY, planning_prompt
            planning_turn = role == 'boss' and (prompt.strip() == '/plan' or prompt.lstrip().startswith('/plan '))
            if planning_turn:
                prompt = planning_prompt(prompt.strip()[5:].strip())
            # A user turn owns one shared inference allowance, including native
            # compression/title calls and delegated helpers. Retries cannot reset it.
            import urllib.request
            reset = urllib.request.Request(os.environ['TEAM_INFERENCE_URL'] + '/team/begin-turn',
                data=b'{}', headers={'Authorization': 'Bearer ' + os.environ['TEAM_INFERENCE_TOKEN'],
                                    'Content-Type': 'application/json'})
            with urllib.request.urlopen(reset, timeout=5) as result:
                result.read()
            token = PLAN_ONLY.set(planning_turn)
            try:
                response = agent.run_conversation(prompt, system_message=overlay, conversation_history=history)
            finally:
                PLAN_ONLY.reset(token)
            history = response.get('messages') or history
            text = response.get('final_response') or response.get('error') or ''
            print(f'{role}> {text}', flush=True)
            final = {'ok': bool(response.get('completed')) and not bool(response.get('error') or response.get('failed') or response.get('interrupted') or response.get('partial')),
                     'result': text, 'session_id': session_id, 'num_turns': response.get('api_calls'),
                     'duration_ms': int((time.monotonic()-start)*1000), 'structured': None,
                     'usage': {'input_tokens': response.get('input_tokens', 0), 'output_tokens': response.get('output_tokens', 0)}}
            if payload.get('prompt') is not None:
                break
        # Let an already-started native worker review finish within a bounded grace period.
        review = getattr(agent, '_background_review_run', None)
        if review is not None:
            review.request_done.wait(timeout=60)
    finally:
        if payload.get('result_file') and final is not None:
            Path(payload['result_file']).write_text(json.dumps(final))
        agent.close()
        db.close()
    return 0 if final is None or final['ok'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
