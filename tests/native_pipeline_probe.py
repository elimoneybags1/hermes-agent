"""Subprocess probe of the pinned Hermes runtime; no production state or profiles."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/hermes-agent'))
sys.path.insert(0, str(ROOT))


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    from run_agent import AIAgent
    from hermes_state import SessionDB
    db = SessionDB()
    agent = AIAgent(provider='custom', api_mode='chat_completions',
                    model=cfg['model'], base_url=os.environ['OPENAI_BASE_URL'],
                    api_key=os.environ['OPENAI_API_KEY'], enabled_toolsets=['file'],
                    max_iterations=6, max_tokens=2048, run_budget_seconds=150,
                    platform='cli', quiet_mode=True, skip_background_review=True,
                    session_db=db, save_trajectories=False)
    try:
        response = agent.run_conversation(cfg['prompt'])
        out = {'completed': response.get('completed'), 'error': response.get('error'),
               'final': response.get('final_response'), 'session_id': agent.session_id,
               'api_calls': response.get('api_calls'), 'tools': sorted(agent.valid_tool_names)}
        if cfg.get('compress'):
            history = response['messages']
            # Force native manual compaction on a reproducible history without
            # paying a model to manufacture filler. Latest task remains real.
            filler = []
            for n in range(8):
                filler.extend([{'role':'user','content':f'Completed historical discussion {n}. ' + ('Archived detail. ' * 150)},
                               {'role':'assistant','content':'Historical discussion resolved. ' * 80}])
            history = filler + history
            before = len(json.dumps(history))
            old_tools = set(agent.valid_tool_names)
            history, _ = agent._compress_context(history, agent._cached_system_prompt or '', force=True)
            out['compression'] = {'before_chars':before, 'after_chars':len(json.dumps(history)),
                                  'tools_lost':sorted(old_tools - set(agent.valid_tool_names))}
            response = agent.run_conversation('Read widget.py again with the file tool and confirm the implemented behavior. Do not edit it.',
                                              conversation_history=history)
            out['continuation'] = {'completed':response.get('completed'), 'error':response.get('error'),
                                   'final':response.get('final_response'), 'api_calls':response.get('api_calls'),
                                   'tool_results':sum(m.get('role')=='tool' for m in response.get('messages',[]))}
        Path(cfg['result']).write_text(json.dumps(out, indent=2))
    finally:
        agent.close()
        db.close()


if __name__ == '__main__':
    main()
