"""Read-only evidence collection and append-only proposals for the advisory agent."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from team import ledger


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def _clip(value, limit=6000):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + '\n[truncated; inspect source for more]'
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


class Evidence:
    """Each session has an evidence registry; proposals cite only observed records."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.observed = {}

    def record(self, key, data):
        item = {'id': key, 'collected_at': _now(), 'data': _clip(data)}
        self.observed[key] = item
        return item

    def collect(self, target=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be 1..200')
        result = {'target': target, 'limit': limit, 'jobs': [], 'goals': [], 'decisions': [],
                  'coverage': 'Latest matching jobs; aggregates are for this sample only. Board and transcripts require job inspection.',
                  'missing': []}
        path = ledger.db_path()
        if path.exists():
            with _db(path) as conn:
                where, args = (' WHERE target = ?', (target,)) if target else ('', ())
                for table, key in [('jobs', 'jobs'), ('goals', 'goals'), ('decisions', 'decisions')]:
                    try:
                        result[key] = _rows(conn, f'SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?', (*args, limit))
                    except sqlite3.OperationalError as exc:
                        result['missing'].append(f'{table}: {exc}')
        else:
            result['missing'].append('No Team ledger exists yet')
        groups = {}
        for job in result['jobs']:
            key = (job['target'], job['role'])
            stats = groups.setdefault(key, {'target': key[0], 'role': key[1], 'attempts': 0, 'finished': 0, 'passed': 0})
            stats['attempts'] += 1
            stats['finished'] += bool(job.get('ended'))
            stats['passed'] += job.get('outcome') == 'pass'
        result['sample_outcomes'] = list(groups.values())
        plans=[]
        for file in (ledger.state_dir()/'planning').glob('*.json'):
            plan=json.loads(file.read_text())
            if target and plan.get('target') != target:
                continue
            plans.append({**{k:plan.get(k) for k in ('id','target','objective','updated_at','revision','inspection_scope','bytes_read','task_goals')},
                          'source_id':'plan:'+file.stem,'helper_count':len(plan.get('helper_runs',[])),
                          'expansion_count':len(plan.get('expansions',[]))})
        result['plans']=sorted(plans,key=lambda p:p.get('updated_at') or '',reverse=True)[:20]
        result['sources'] = list(self.sources())
        return self.record('snapshot:' + uuid.uuid4().hex[:12], result)

    def sources(self):
        paths = {}
        candidates = list((self.root/'team').glob('*.py')) + list((self.root/'roles').rglob('*.md')) + list((self.root/'roles').glob('*/role.toml'))
        candidates += [self.root/'plugin/agents/boss.md', self.root/'orchestration.toml']
        candidates += [self.root/'vendor/hermes-agent'/name for name in (
            'run_agent.py', 'agent/prompt_builder.py', 'agent/context_compressor.py',
            'agent/background_review.py', 'agent/memory_manager.py',
            'tools/registry.py', 'hermes_cli/kanban_db.py')]
        candidates += list((self.root/'docs').glob('*.md'))
        for p in candidates:
            if p.is_file() and not p.is_symlink():
                paths['source:' + p.relative_to(self.root).as_posix()] = p
        # Include role-local learning, but never credentials/config files.
        upstream = self.root/'vendor/hermes-agent/skills'
        for role in (self.root/'roles').iterdir():
            if not role.is_dir():
                continue
            for lane, home in [('subscription', ledger.state_dir()/'hermes/profiles'/role.name),
                               ('free', Path.home()/'.hermes/profiles'/('team-'+role.name))]:
                candidates = [home/'SOUL.md', home/'memories/MEMORY.md', home/'memories/USER.md']
                for p in (home/'skills').rglob('SKILL.md'):
                    if not p.resolve().is_relative_to(home.resolve()):
                        continue
                    base = upstream/p.relative_to(home/'skills')
                    if not base.exists() or p.read_bytes() != base.read_bytes():
                        candidates.append(p)
                for p in candidates:
                    if p.is_file() and p.resolve().is_relative_to(home.resolve()):
                        paths[f'profile:{lane}:{role.name}:' + p.relative_to(home).as_posix()] = p
        for path in (ledger.state_dir()/'planning').glob('*.json'):
            if not path.is_symlink():
                paths['plan:'+path.stem]=path
        return paths

    def source(self, source_id, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be nonnegative')
        path = self.sources().get(source_id)
        if path is None:
            raise ValueError('Unknown evidence source; use optimizer_evidence for the catalog')
        text = path.read_text(errors='replace')
        data = {'source': source_id, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'offset': offset, 'text': text[offset:offset+6000],
                'next_offset': offset+6000 if len(text) > offset+6000 else None}
        return self.record(source_id + f'@{offset}', data)

    def job(self, job_id):
        with _db(ledger.db_path()) as conn:
            rows = _rows(conn, 'SELECT * FROM jobs WHERE id=?', (job_id,))
            if not rows:
                raise ValueError('Unknown job')
            job = rows[0]
            data = {'job': job, 'calls': _rows(conn, 'SELECT * FROM calls WHERE job_id=? ORDER BY id', (job_id,))}
            ref = job.get('task_ref') or ''
            if ref and not ref.startswith('claude:'):
                from team import kanban
                try:
                    data['board'] = kanban.show_full(job['target'], ref)
                except Exception as exc:
                    data['board_unavailable'] = str(exc)
            elif ref.startswith('claude:'):
                data['session_id'] = ref.removeprefix('claude:')
            return self.record(f'job:{job_id}', data)

    def session(self, role, lane, session_id, offset=0):
        if not (self.root/'roles'/role/'role.toml').is_file() or '/' in role or lane not in {'free','subscription'}:
            raise ValueError('Unknown role or lane')
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be nonnegative')
        home = (ledger.state_dir()/'hermes/profiles'/role if lane == 'subscription'
                else Path.home()/'.hermes/profiles'/('team-'+role))
        with _db(home/'state.db') as conn:
            # Read existing SQLite directly: no Hermes migration, inference or writes.
            columns = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
            fields = [f for f in ('id','role','content','tool_name','tool_calls','tool_call_id','timestamp') if f in columns]
            messages = _rows(conn, f"SELECT {','.join(fields)} FROM messages WHERE session_id=? ORDER BY id LIMIT 21 OFFSET ?", (session_id, offset))
            data = {'role': role, 'lane': lane, 'session_id': session_id, 'messages': messages[:20],
                    'next_offset': offset+20 if len(messages)>20 else None}
            return self.record(f'session:{lane}:{role}:{session_id}@{offset}', data)

    def recommend(self, **proposal):
        required = ('subject','scope','location','before','after','reason','validation','uncertainty')
        for key in required:
            if not isinstance(proposal.get(key), str) or not proposal[key].strip():
                raise ValueError(f'{key} must be explicit')
        refs = proposal.get('evidence_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(r,str) or r not in self.observed for r in refs):
            raise ValueError('Cite evidence IDs inspected in this session')
        parent = proposal.get('supersedes')
        if parent and parent not in {p['id'] for p in proposals()}:
            raise ValueError('Unknown superseded proposal')
        item = {**proposal, 'id': str(uuid.uuid4()), 'created_at': _now(), 'status': 'proposed',
                'evidence': [self.observed[r] for r in refs]}
        directory = ledger.state_dir()/'optimizer/proposals'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory/(item['id']+'.json')
        with open(path, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
            json.dump(item, out, indent=2)
        return {k: v for k,v in item.items() if k != 'evidence'}


def proposals():
    items = [json.loads(p.read_text()) for p in (ledger.state_dir()/'optimizer/proposals').glob('*.json')]
    items.sort(key=lambda p: (p['created_at'], p['id']))
    revisions = {}
    for item in items:
        if item.get('supersedes'):
            revisions[item['supersedes']] = item['id']
    for item in items:
        if item['id'] in revisions:
            item.update(status='superseded', superseded_by=revisions[item['id']])
    return items


def register(root):
    from tools.registry import registry
    from toolsets import create_custom_toolset
    evidence = Evidence(root)
    string = {'type':'string'}
    integer = {'type':'integer'}
    definitions = [
        ('optimizer_evidence', 'Compile recent jobs, outcomes, goals, decisions and current source/learning catalog.', evidence.collect,
         {'target':string,'limit':integer}, []),
        ('optimizer_source', 'Read a cataloged source or role-local learned instruction. Paginate with offset.', evidence.source,
         {'source_id':string,'offset':integer}, ['source_id']),
        ('optimizer_job', 'Inspect one job, its model calls and available native board history.', evidence.job,
         {'job_id':integer}, ['job_id']),
        ('optimizer_session', 'Read existing role session messages and tool results; no inference or database migration.', evidence.session,
         {'role':string,'lane':{'type':'string','enum':['free','subscription']},'session_id':string,'offset':integer}, ['role','lane','session_id']),
        ('optimizer_recommend', 'Save a concrete proposal with inspected evidence. Does not approve or apply anything.', evidence.recommend,
         {**{k:string for k in ('subject','scope','location','before','after','reason','validation','uncertainty','supersedes')},'evidence_ids':{'type':'array','items':string}},
         ['subject','scope','location','before','after','reason','validation','uncertainty','evidence_ids']),
        ('optimizer_proposals', 'Read previous recommendations; these are proposals, never implementation approvals.',
         lambda: [{k:v for k,v in p.items() if k!='evidence'} for p in proposals()], {}, []),
    ]
    for name, description, handler, properties, required in definitions:
        def dispatch(args, _handler=handler, **kwargs):
            try:
                return json.dumps(_handler(**args))
            except Exception as exc:
                return json.dumps({'error':str(exc)})
        registry.register(name=name, toolset='team_optimizer', schema={'name':name,'description':description,
            'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}, handler=dispatch)
    create_custom_toolset('team_optimizer', 'Evidence and advisory recommendations only', tools=[d[0] for d in definitions])
    return ['team_optimizer', 'clarify']


def main(root, argv):
    import argparse
    from team.hermes_runtime import launch
    parser = argparse.ArgumentParser(prog='team optimize')
    parser.add_argument('--prompt')
    parser.add_argument('--resume')
    parser.add_argument('--max-turns', type=int, default=40)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--list', action='store_true', help='Show saved recommendations without a model call')
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps([{k:v for k,v in p.items() if k!='evidence'} for p in proposals()], indent=2))
        return 0
    try:
        proc, _ = launch('optimizer', root=root, prompt=args.prompt, resume=args.resume,
                         initial_prompt='Compile the current Team evidence. Inspect the strongest candidate and present one concrete improvement with a short reason, or explain what evidence is missing.',
                         max_turns=args.max_turns, timeout_s=args.timeout)
        return proc.returncode
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Cannot start Team Optimizer: {exc}')
        return 1
