"""Scoped planning on native Hermes; project reads and dispatch gates are host-owned."""
from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from team import ledger, targets

PLAN_ONLY = contextvars.ContextVar('team_planning_only', default=False)
HELPER_PLAN = contextvars.ContextVar('team_planning_helper_plan', default=None)
HELPER_SCOPE = contextvars.ContextVar('team_planning_helper_scope', default=None)
READ_TOOLS = ['planning_view', 'planning_locate', 'planning_read', 'planning_search']
MAX_BYTES = 250_000
MAX_FILES = 30
PAGE_BYTES = 12_000
BLOCKED = {'.git', '.ssh', '.aws', '.hermes', '.codex', '.Codex', '.claude', 'node_modules', '.venv', 'venv', '__pycache__'}


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonempty')
    return value.strip()


def strings(value, name, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f'{name} must be a list' + (' with entries' if nonempty else ''))
    return [text(v, name) for v in value]


def relative(value):
    value = text(value, 'path')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or str(p) == '.':
        raise ValueError('Use a specific relative path, never the project root or traversal')
    if any(part in BLOCKED or part.startswith('.env') for part in p.parts):
        raise ValueError('Credential, runtime, dependency and repository metadata paths are excluded')
    if p.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.db'}:
        raise ValueError('Secret and database files are not planning inputs')
    return p.as_posix()


def within(path, roots):
    return any(path == root or path.startswith(root.rstrip('/') + '/') for root in roots)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2)
        temporary = Path(f.name)
    os.replace(temporary, path)


class Planner:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.parent_agent = None
        self.instruction_cache = {}

    def path(self, plan_id):
        if str(uuid.UUID(plan_id)) != plan_id:
            raise ValueError('Invalid plan ID')
        restricted = HELPER_PLAN.get()
        if restricted and restricted != plan_id:
            raise ValueError('Helper may inspect only its assigned plan')
        return ledger.state_dir()/'planning'/f'{plan_id}.json'

    def load(self, plan_id):
        return json.loads(self.path(plan_id).read_text())

    @contextmanager
    def edit(self, plan_id):
        path = self.path(plan_id)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(path.read_text())
            yield data
            data['updated_at'] = now()
            write_json(path, data)

    def list_plans(self, target):
        self.writable()
        if not targets.NAME_RE.fullmatch(target) or target in {'.','..'} or targets.get(target) is None:
            raise ValueError('Unknown target')
        items=[]
        for path in (ledger.state_dir()/'planning').glob('*.json'):
            data=json.loads(path.read_text())
            if data.get('target')==target:
                items.append({k:data[k] for k in ('id','objective','updated_at','revision','inspection_scope','task_goals')})
        return sorted(items,key=lambda d:d['updated_at'],reverse=True)[:20]

    def target_root(self, data):
        target = targets.get(data['target'])
        if target is None or str(Path(target['path']).resolve()) != data['project_root']:
            raise ValueError('Attached target changed; create a new plan')
        return Path(data['project_root'])

    def resolve(self, data, value):
        rel = relative(value)
        root = self.target_root(data)
        path = (root/rel).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Path escapes attached project')
        cursor=root
        for part in Path(rel).parts:
            cursor=cursor/part
            if cursor.is_symlink():
                raise ValueError('Symlink paths are excluded from scoped inspection')
        return rel, path

    def writable(self):
        if HELPER_PLAN.get():
            raise ValueError('Planning helpers cannot mutate plans or expand scope')

    def start(self, target, objective):
        self.writable()
        if not targets.NAME_RE.fullmatch(target) or target in {'.','..'}:
            raise ValueError('Invalid target')
        record = targets.get(target)
        if record is None:
            raise ValueError('Attach this target before planning')
        root = Path(record['path']).resolve()
        entries = []
        for p in sorted(root.iterdir()):
            try:
                relative(p.name)
            except ValueError:
                continue
            if not p.is_symlink():
                entries.append(p.name + ('/' if p.is_dir() else ''))
        plan_id = str(uuid.uuid4())
        data = dict(id=plan_id, target=target, project_root=str(root), objective=text(objective,'objective'),
                    created_at=now(), updated_at=now(), inspection_scope=[], expansions=[], observed={},
                    bytes_read=0, files_read=[], contract=None, revision=0, task_goals={}, assessments=[], helper_runs=[])
        write_json(self.path(plan_id), data)
        # Only root instruction files, never all project content, are initial context.
        instructions = self.instructions(plan_id, '')
        map_path = ledger.state_dir()/'targets'/target/'project-map.json'
        return {'plan_id':plan_id,'objective':objective,'top_level_names':entries[:100],
                'names_truncated':len(entries)>100,'instructions':instructions,
                'project_map':json.loads(map_path.read_text()) if map_path.exists() else [],
                'budget':{'content_bytes':MAX_BYTES,'distinct_files':MAX_FILES},
                'next':'Locate relevant filenames, then choose a specific subsystem before searching or reading content.'}

    def instruction_paths(self, rel):
        paths = ['AGENTS.md','CLAUDE.md']
        parent = Path(rel).parent if rel else Path('.')
        for ancestor in reversed([parent, *parent.parents]):
            if str(ancestor) != '.':
                paths += [(ancestor/name).as_posix() for name in ('AGENTS.md','CLAUDE.md')]
        return list(dict.fromkeys(paths))

    def instructions(self, plan_id, rel):
        result = []
        for name in self.instruction_paths(rel):
            data = self.load(plan_id)
            _, path = self.resolve(data, name)
            if path.is_file():
                item = self._read(plan_id, name, 0, instruction=True)
                if item.get('cached'):
                    # Already supplied to this Planner's conversation. Keep the
                    # provenance without duplicating governing text on every page.
                    item = {k: item[k] for k in ('evidence_id', 'path', 'sha256')}
                    item['already_supplied'] = True
                result.append(item)
        return result

    def scope(self, plan_id, paths, reason, evidence):
        self.writable()
        paths = strings(paths,'paths')
        reason = text(reason,'reason')
        evidence = strings(evidence,'evidence',False)
        with self.edit(plan_id) as data:
            if data['inspection_scope'] and not evidence:
                raise ValueError('Expanding scope requires an observed file or locate/search evidence ID')
            if any(e not in data['observed'] for e in evidence):
                raise ValueError('Scope expansion must cite evidence inspected for this plan')
            for p in paths:
                _, path = self.resolve(data,p)
                if not path.exists():
                    raise ValueError(f'Inspection path does not exist: {p}')
            roots = list(dict.fromkeys(data['inspection_scope'] + [relative(p) for p in paths]))
            if len(roots)>12:
                raise ValueError('Too many inspection roots; split this goal into focused plans')
            data['inspection_scope'] = roots
            data['expansions'].append({'at':now(),'paths':paths,'reason':reason,'evidence':evidence})
        return {'inspection_scope':roots,'reason':reason}

    def locate(self, plan_id, pattern):
        data = self.load(plan_id)
        pattern = text(pattern,'filename pattern')
        if pattern in {'*','**','**/*'} or len(pattern)>200:
            raise ValueError('Use a meaningful filename pattern, not a whole-repository inventory')
        root = self.target_root(data)
        args = ['rg','--files','--hidden','-g',pattern]
        args += [arg for name in BLOCKED for arg in ('-g',f'!{name}/**')]
        proc = subprocess.run(args, cwd=root, capture_output=True,text=True,timeout=15)
        if proc.returncode not in (0,1):
            raise ValueError(proc.stderr.strip())
        matches=[]
        for p in proc.stdout.splitlines():
            try:
                rel, full = self.resolve(data,p)
                if not HELPER_PLAN.get() or within(rel,HELPER_SCOPE.get() or data['inspection_scope']):
                    matches.append(rel)
            except ValueError:
                continue
        key = 'locate:' + uuid.uuid4().hex[:12]
        result={'evidence_id':key,'pattern':pattern,'paths':matches[:80], 'truncated':len(matches)>80,
                'coverage':'Filename metadata only; no source content read.'}
        with self.edit(plan_id) as current:
            current['observed'][key] = result
        return result

    def _read(self, plan_id, path, offset=0, instruction=False):
        if type(offset) is not int or offset<0:
            raise ValueError('offset must be nonnegative bytes')
        with self.edit(plan_id) as data:
            rel, full = self.resolve(data,path)
            instruction_allowed = rel in self.instruction_paths(rel) and Path(rel).name in {'AGENTS.md','CLAUDE.md'} and (
                '/' not in rel or any(within(s,[str(Path(rel).parent)]) for s in (HELPER_SCOPE.get() or data['inspection_scope'])))
            if not within(rel,HELPER_SCOPE.get() or data['inspection_scope']) and not instruction_allowed:
                raise ValueError('Outside inspection scope; request expansion with a concrete reason and evidence')
            if not full.is_file():
                raise ValueError('Read requires a file')
            cache_key=(plan_id,rel,offset,full.stat().st_mtime_ns)
            if instruction and cache_key in self.instruction_cache:
                return {**self.instruction_cache[cache_key], 'cached':True}
            if rel not in data['files_read'] and len(data['files_read']) >= MAX_FILES:
                raise ValueError('Inspection file budget exhausted; split the goal before reading more')
            remaining = MAX_BYTES-data['bytes_read']
            if remaining <= 0:
                raise ValueError('Inspection content budget exhausted; split the goal before reading more')
            size=min(PAGE_BYTES,remaining)
            with full.open('rb') as f:
                f.seek(offset)
                raw=f.read(size)
            if b'\0' in raw:
                raise ValueError('Binary files are excluded')
            data['bytes_read'] += len(raw)
            if rel not in data['files_read']:
                data['files_read'].append(rel)
            stat=full.stat()
            key=f'file:{rel}@{offset}'
            evidence={'evidence_id':key,'path':rel,'offset':offset,'bytes':len(raw),
                      'sha256':hashlib.sha256(raw).hexdigest(),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                      'instruction':instruction}
            data['observed'][key]=evidence
            result = {**evidence,'text':raw.decode('utf-8',errors='replace'),
                    'next_offset':offset+len(raw) if offset+len(raw)<stat.st_size else None,
                    'remaining_bytes':MAX_BYTES-data['bytes_read']}
            if instruction: self.instruction_cache[cache_key]=result
            return result

    def read(self, plan_id, path, offset=0):
        # Load applicable instructions before supplying the file itself.
        instructions=[] if Path(path).name in {'AGENTS.md','CLAUDE.md'} else self.instructions(plan_id,path)
        return {'instructions':instructions, 'file':self._read(plan_id,path,offset)}

    def search(self, plan_id, query):
        query=text(query,'query')
        data=self.load(plan_id)
        if not data['inspection_scope']:
            raise ValueError('Select a subsystem first')
        root=self.target_root(data)
        args=['rg','--files','--hidden']
        args += [arg for name in BLOCKED for arg in ('-g',f'!{name}/**')]
        args += ['--',*(HELPER_SCOPE.get() or data['inspection_scope'])]
        proc=subprocess.run(args,cwd=root,capture_output=True,text=True,timeout=15)
        if proc.returncode not in (0,1):
            raise ValueError(proc.stderr.strip())
        scanned=[]; coverage=[]; admitted=[]; offsets={}
        names=proc.stdout.splitlines()
        if len(names)>80:
            coverage.append('Only the first 80 scoped files considered')
        with self.edit(plan_id) as current:
            for name in names[:80]:
                try:
                    rel, full=self.resolve(current,name)
                    if not within(rel, HELPER_SCOPE.get() or current['inspection_scope']):
                        raise ValueError('Outside inspection scope')
                    if not full.is_file():
                        continue
                    size=full.stat().st_size
                    if size>MAX_BYTES-current['bytes_read']:
                        coverage.append(f'{rel}: file exceeds remaining search byte budget; narrow the scope or read a page')
                        continue
                    if rel not in current['files_read'] and len(current['files_read'])>=MAX_FILES:
                        coverage.append('Inspection file budget exhausted')
                        break
                    raw=full.read_bytes()
                    if b'\0' in raw:
                        continue
                    current['bytes_read']+=len(raw)
                    if rel not in current['files_read']: current['files_read'].append(rel)
                    scanned.append(rel); admitted.append(str(full))
                    positions=[0]
                    for line in raw.splitlines(keepends=True): positions.append(positions[-1]+len(line))
                    offsets[str(full)]=(rel,positions)
                except ValueError as exc:
                    coverage.append(f'{name}: {exc}')
        from team.hermes_bridge import scoped_search
        try:
            native=scoped_search(admitted,query)
        except ImportError:
            # Administrative callers/tests use the same upstream implementation
            # in its interpreter; never a second search implementation.
            from team.hermes_runtime import python_executable, FORK
            response=subprocess.run([str(python_executable()),str(Path(__file__).with_name('hermes_bridge.py')),str(FORK)],
                input=json.dumps({'name':'scoped_search','arguments':{'files':admitted,'query':query}}),
                capture_output=True,text=True,timeout=30)
            if response.returncode:
                raise ValueError('Native Hermes search failed: '+response.stderr[-500:])
            native=json.loads(response.stdout)
        matches=[]
        for item in native['matches']:
            rel,positions=offsets[item['path']]
            line=max(1,item['line'])
            matches.append({**item,'path':rel,'offset':positions[min(line-1,len(positions)-1)]})
        coverage.extend(native['coverage_notes'])
        key='search:'+uuid.uuid4().hex[:12]
        for item in matches: item['evidence_id']=key
        result={'evidence_id':key,'query':query,'matches':matches,'files_scanned':scanned,
                'coverage_notes':coverage,'bounded':True,
                'note':'Search returns bounded excerpts with page offsets. Check coverage_notes before interpreting no matches.'}
        with self.edit(plan_id) as current:
            current['observed'][key]=result
        return result

    def update(self, plan_id, contract):
        self.writable()
        if not isinstance(contract,dict):
            raise ValueError('contract must be an object')
        # Store drafts freely; readiness is checked separately against current evidence.
        with self.edit(plan_id) as data:
            if data['task_goals']:
                raise ValueError('This plan has dispatched work; create a follow-up plan instead of rewriting its contract')
            data.setdefault('revisions',[]).append({'revision':data['revision'],'contract':data['contract'],'at':now()})
            data['contract']=contract
            data['revision']+=1
        return self.view(plan_id)

    def readiness(self, data, check_freshness=True):
        try:
            return self._readiness(data, check_freshness)
        except (TypeError, KeyError, ValueError) as exc:
            return [f'Invalid contract shape: {exc}']

    def _readiness(self, data, check_freshness):
        errors=[]
        c=data.get('contract') or {}
        for field in ('success','change_scope','verification_scope','exclusions','stop_conditions','tasks'):
            if field not in c or (field not in {'exclusions'} and not c[field]):
                errors.append(f'Missing {field}')
        if not isinstance(c.get('decisions'),list):
            errors.append('decisions must explicitly list resolved questions, or be []')
        elif any(not isinstance(d,dict) or not d.get('answer') for d in c['decisions']):
            errors.append('Unresolved decisions')
        success=c.get('success',[])
        if not isinstance(success,list) or any(not isinstance(s,dict) or not all(s.get(k) for k in ('id','behavior','verification')) for s in success):
            errors.append('Each success criterion needs id, behavior and verification')
            success=[]
        criteria={s['id'] for s in success}
        if len(criteria)!=len(success):
            errors.append('Duplicate success criterion IDs')
        change=c.get('change_scope',[])
        if not isinstance(change,list):
            errors.append('change_scope must be paths');change=[]
        for name in change:
            try:
                relative(name)
            except ValueError as exc:
                errors.append(str(exc))
        tasks=c.get('tasks',[])
        if not isinstance(tasks,list):
            errors.append('tasks must be a list');tasks=[]
        ids=set();ownership=[];covered=set()
        for task in tasks:
            if not isinstance(task,dict) or not all(task.get(k) for k in ('id','title','body','role','files','criteria','check')):
                errors.append('Each task needs id, title, body, role, files, criteria and check');continue
            if not isinstance(task['id'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',task['id']):
                errors.append('Task IDs must be simple stable identifiers')
            if task['id'] in ids: errors.append('Duplicate task ID')
            ids.add(task['id'])
            if not isinstance(task['files'],list) or not isinstance(task['criteria'],list):
                errors.append('Task files/criteria must be lists');continue
            for key in ('recommended_toolsets', 'required_skills'):
                if key in task:
                    strings(task[key], key, nonempty=False)
            for skill in task.get('required_skills', []):
                path = relative(skill)
                source = (self.root / path).resolve()
                libraries = [(self.root / 'roles' / task['role'] / 'skills').resolve(),
                             (self.root / 'vendor/hermes-agent/skills').resolve()]
                if source.name != 'SKILL.md' or not source.is_file() or not any(source.is_relative_to(lib) for lib in libraries):
                    errors.append(f'Required skill is not in the role or upstream library: {skill}')
            task_inspection=task.get('inspection_scope')
            if task_inspection is not None:
                if not isinstance(task_inspection,list) or not task_inspection or any(not within(relative(p),data['inspection_scope']) for p in task_inspection):
                    errors.append('Task inspection_scope must be a nonempty subset of plan inspection scope')
            covered.update(task['criteria'])
            if set(task['criteria'])-criteria: errors.append('Task references unknown success criteria')
            role=self.root/'roles'/task['role']/'role.toml'
            import tomllib
            if '/' in task['role'] or not role.is_file():
                errors.append('Unknown task role')
            else:
                manifest=tomllib.loads(role.read_text())
                available = set(manifest.get('tools', [])) | {'skills', 'memory', 'session_search', 'todo'}
                if set(task.get('recommended_toolsets', [])) - available:
                    errors.append('Recommended toolsets must be available to the assigned role')
                if manifest.get('status')!='active' or manifest.get('runs_on',{}).get('harness')!='hermes':
                    errors.append(f'Task role {task["role"]} is not an active implementation worker')
            for name in task['files']:
                try:
                    rel=relative(name)
                    if not within(rel,change): errors.append(f'{rel} outside change_scope')
                    _,full=self.resolve(data,rel)
                    if full.exists():
                        if not full.is_file():
                            errors.append(f'Owned paths must name exact files: {rel}')
                        if not within(rel,task_inspection or data['inspection_scope']):
                            errors.append(f'Existing owned file outside inspection scope: {rel}')
                        if not any(o.get('path')==rel and not o.get('instruction') for o in data['observed'].values()):
                            errors.append(f'Existing owned file has not been inspected: {rel}')
                    elif not within(rel,data['inspection_scope']):
                        refs=task.get('evidence',[])
                        if not task.get('scope_reason') or not isinstance(refs,list) or not refs or any(e not in data['observed'] or not data['observed'][e].get('path') for e in refs):
                            errors.append(f'New file outside inspection roots requires scope_reason and inspected file evidence: {rel}')
                    if any(within(rel,[old]) or within(old,[rel]) for old in ownership):
                        errors.append(f'Overlapping task ownership: {rel}')
                    ownership.append(rel)
                except ValueError as exc: errors.append(str(exc))
        for task in tasks:
            if isinstance(task,dict):
                deps=task.get('depends_on',[])
                if not isinstance(deps,list) or any(d not in ids or d==task.get('id') for d in deps):
                    errors.append('Invalid task dependency')
        graph={t['id']:t.get('depends_on',[]) for t in tasks if isinstance(t,dict) and t.get('id') in ids}
        def cycle(node,trail):
            return node in trail or any(cycle(d,trail|{node}) for d in graph.get(node,[]) if d in graph)
        if any(cycle(n,set()) for n in graph): errors.append('Cyclic task dependencies')
        if criteria-covered: errors.append('Some acceptance criteria have no assigned task')
        observations=[o for o in data['observed'].values() if o.get('path') and not o.get('instruction')]
        if not observations: errors.append('No relevant project source has been inspected')
        if check_freshness:
            for observation in [o for o in data['observed'].values() if o.get('path')]:
                try:
                    _,p=self.resolve(data,observation['path'])
                    stat=p.stat()
                    if stat.st_size!=observation['size'] or stat.st_mtime_ns!=observation['mtime_ns']:
                        errors.append(f'Inspected source changed: {observation["path"]}; reread it')
                except OSError:
                    errors.append(f'Inspected source missing: {observation["path"]}')
        return list(dict.fromkeys(errors))

    def view(self, plan_id):
        data=self.load(plan_id)
        result={k:v for k,v in data.items() if k not in {'observed','revisions'}}
        result['evidence']=list(data['observed'].values())
        if HELPER_PLAN.get():
            roots=HELPER_SCOPE.get() or data['inspection_scope']
            filtered=[]
            for item in result['evidence']:
                if item.get('path') and (within(item['path'],roots) or item.get('instruction')):
                    filtered.append(item)
                elif item.get('pattern'):
                    filtered.append({**item,'paths':[p for p in item['paths'] if within(p,roots)]})
                elif item.get('query'):
                    filtered.append({**item,'matches':[m for m in item['matches'] if within(m['path'],roots)],
                                     'files_scanned':[p for p in item['files_scanned'] if within(p,roots)],'coverage_notes':['Filtered to helper scope']})
            result['evidence']=filtered
            result['helper_runs']=[]
            result['inspection_scope']=list(roots)
        result['readiness_errors']=[] if HELPER_PLAN.get() else self.readiness(data)
        result['readiness_checked']=not bool(HELPER_PLAN.get())
        result['freshness_checked']=not bool(HELPER_PLAN.get())
        result['ready']=None if HELPER_PLAN.get() else not result['readiness_errors']
        result['linked_outcomes']={}
        if ledger.db_path().exists():
            import sqlite3
            conn=sqlite3.connect(ledger.db_path().resolve().as_uri()+'?mode=ro',uri=True)
            conn.row_factory=sqlite3.Row
            try:
                for task,goal_id in data['task_goals'].items():
                    row=conn.execute('SELECT * FROM goals WHERE id=?',(goal_id,)).fetchone()
                    if row: result['linked_outcomes'][task]=dict(row)
            finally: conn.close()
        result['completion']='needs whole-goal assessment; task passes alone do not close this plan'
        return result

    def remember(self, plan_id, subsystem, paths, summary, evidence):
        self.writable()
        data=self.load(plan_id)
        paths=strings(paths,'paths');evidence=strings(evidence,'evidence')
        if any(not within(relative(p),data['inspection_scope']) for p in paths):
            raise ValueError('Map entries must describe inspected subsystems')
        if any(e not in data['observed'] for e in evidence): raise ValueError('Unknown evidence')
        path=ledger.state_dir()/'targets'/data['target']/'project-map.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            entries=json.loads(path.read_text()) if path.exists() else []
            entries=[e for e in entries if e['subsystem']!=subsystem]
            entries.append({'subsystem':text(subsystem,'subsystem'),'paths':paths,'summary':text(summary,'summary')[:1500],
                            'plan_id':plan_id,'at':now(),'evidence':evidence,'status':'navigation hint; verify current source'})
            write_json(path,entries[-30:])
        return entries[-1]

    def helper(self, plan_id, kind, question, paths=None):
        self.writable()
        data=self.load(plan_id)
        if not data['inspection_scope']: raise ValueError('Set inspection scope before assigning a helper')
        if self.parent_agent is None: raise ValueError('Native boss context not bound')
        if kind not in {'explorer','critic','verifier'}: raise ValueError('Unknown helper kind')
        selected=strings(paths if paths is not None else data['inspection_scope'],'paths')
        if any(not within(relative(p),data['inspection_scope']) for p in selected):
            raise ValueError('Helper roots must be inside the plan inspection scope')
        from team.planning_delegate import run_helper
        prompt=(f'Plan {plan_id}. Objective: {data["objective"]}. Inspection roots: {selected}.\n'
                f'Your assignment ({kind}): {text(question,"question")}\n'
                'Use planning_view first. Inspect only this plan and its assigned roots. Return findings with '
                'file/evidence IDs, uncertainties, and any requested scope expansion with a reason. '
                'Do not modify files, plans, skills, tasks or goals. Do not equate a task pass with whole-goal success.')
        token=HELPER_PLAN.set(plan_id)
        scope_token=HELPER_SCOPE.set(tuple(selected))
        try:
            result=run_helper(self.parent_agent,prompt,kind,toolset='team_planning_read',allowed_tools=READ_TOOLS)
        finally:
            HELPER_SCOPE.reset(scope_token)
            HELPER_PLAN.reset(token)
        with self.edit(plan_id) as current:
            evidence_id='helper:'+uuid.uuid4().hex[:12]
            entry={'evidence_id':evidence_id,'kind':kind,'question':question,'at':now(),'result':result}
            current['helper_runs'].append(entry)
            current['observed'][evidence_id]=entry
        result={**result,'evidence_id':evidence_id}
        return result

    def dispatch(self, plan_id, task_id):
        self.writable()
        if PLAN_ONLY.get():
            raise ValueError('Planning-only turn cannot dispatch work')
        # Hold the plan lock across dispatch: duplicate tool retries reuse the recorded goal.
        with self.edit(plan_id) as data:
            if task_id in data['task_goals']:
                return {'goal_id':data['task_goals'][task_id],'already_dispatched':True}
            errors=self.readiness(data)
            if errors: raise ValueError('Plan not ready: ' + '; '.join(errors))
            task=next((t for t in data['contract']['tasks'] if t['id']==task_id),None)
            if task is None: raise ValueError('Unknown task')
            from team import hermes_boss
            parents=[]
            with ledger.connect() as conn:
                ledger.init(conn)
                for dep in task.get('depends_on',[]):
                    goal_id=data['task_goals'].get(dep)
                    row=ledger.get_goal(conn,goal_id) if goal_id else None
                    if not row or row['outcome']!='pass':
                        raise ValueError(f'Dependency {dep} must pass and land before dispatch')
                    parents.append(row['current_task'])
            contract=data['contract']
            slice_={k:contract.get(k) for k in ('success','verification_scope','exclusions','decisions','stop_conditions')}
            task_evidence=[data['observed'][e].get('path','') for e in task.get('evidence',[]) if e in data['observed']]
            task_inspection=task.get('inspection_scope') or [r for r in data['inspection_scope']
                if any(within(relative(f),[r]) for f in task['files']) or any(within(e,[r]) for e in task_evidence if e)]
            slice_['success']=[s for s in contract['success'] if s['id'] in task['criteria']]
            slice_.update(objective=data['objective'],plan_id=plan_id,revision=data['revision'],
                          inspection_scope=task_inspection,change_scope=task['files'],
                          task_id=task_id,assigned_criteria=task['criteria'],
                          recommended_toolsets=task.get('recommended_toolsets', []),
                          required_skills=task.get('required_skills', []))
            marker=f'Team plan task: {plan_id}:{task_id}'
            body=marker+'\n'+task['body']+'\n\nTeam scoped assignment:\n'+json.dumps(slice_,indent=2)+(
                '\nInvestigate the assigned area first. Change only owned paths. If a dependency requires '
                'more scope, stop and report the path and reason. Start with recommended_toolsets and load only '
                'the required_skills relevant to this task, using native skill discovery by name. Skill paths identify '
                'Team library sources, not paths inside the target repository. Do not read the runtime source or '
                'the entire skills library to learn your tools. These recommendations do not change tool permissions. '
                'Verification may be broader than inspection. '
                'Do not infer that this task completes the entire goal.')
            # Recover a prior dispatch if the process stopped after goal creation.
            with ledger.connect() as conn:
                existing=conn.execute('SELECT id, current_task FROM goals WHERE target=? AND instr(goal,?)>0 ORDER BY id DESC LIMIT 1',(data['target'],marker+'\n')).fetchone()
            if existing:
                data['task_goals'][task_id]=existing['id']
                return {'goal_id':existing['id'],'task_id':existing['current_task'],'already_dispatched':True}
            # Reuse existing creation, model, worktree, ledger and landing gates.
            result=json.loads(hermes_boss.execute(self.root,'kanban_create',dict(board=data['target'],
                title=task['title'],body=body,assignee='team-'+task['role'],parents=parents,check=task['check'])))
            data['task_goals'][task_id]=result['goal_id']
        return result

    def assess(self, plan_id, criteria, summary):
        self.writable()
        view=self.view(plan_id)
        expected={s['id'] for s in (view.get('contract') or {}).get('success',[])}
        if not expected or not isinstance(criteria,list) or {c.get('id') for c in criteria}!=expected or len(criteria)!=len(expected):
            raise ValueError('Assess every acceptance criterion exactly once')
        data=self.load(plan_id)
        goals={g['id']:g for g in view['linked_outcomes'].values()}
        for criterion in criteria:
            if criterion.get('status') not in {'met','unmet','unknown'}: raise ValueError('Invalid assessment status')
            text(criterion.get('reason'),'reason')
            if criterion['status']=='met':
                refs=strings(criterion.get('evidence_ids'),'evidence_ids')
                if any(r not in data['observed'] for r in refs): raise ValueError('Unknown assessment evidence')
                linked=criterion.get('goal_ids')
                if not isinstance(linked,list) or not linked or any(g not in goals or goals[g]['outcome']!='pass' for g in linked):
                    raise ValueError('Met criteria must cite linked goals that passed and landed')
        assessment={'at':now(),'summary':text(summary,'summary'),'criteria':criteria,
                    'type':'model assessment; code checks evidence references, not semantic truth'}
        with self.edit(plan_id) as current: current['assessments'].append(assessment)
        return assessment


def register(root):
    from tools.registry import registry
    from toolsets import create_custom_toolset
    planner=Planner(root)
    s={'type':'string'}; ss={'type':'array','items':s}; number={'type':'integer','minimum':0}
    pid={'plan_id':s}
    criterion={'type':'object','properties':{'id':s,'behavior':s,'verification':s},'required':['id','behavior','verification'],'additionalProperties':False}
    task={'type':'object','properties':{**{k:s for k in ('id','title','body','role','check')},'files':ss,'criteria':ss,'depends_on':ss,'inspection_scope':ss,'scope_reason':s,'evidence':ss,'recommended_toolsets':ss,'required_skills':ss},
          'required':['id','title','body','role','check','files','criteria'],'additionalProperties':False}
    contract={'type':'object','properties':{'success':{'type':'array','items':criterion},'change_scope':ss,'verification_scope':ss,
        'exclusions':ss,'stop_conditions':ss,'decisions':{'type':'array','items':{'type':'object','properties':{'question':s,'answer':s},'required':['question','answer']}},
        'tasks':{'type':'array','items':task}},'additionalProperties':False}
    definitions=[
        ('planning_start','Start a scoped goal for an attached target. Returns top-level filenames/instructions and cached navigation hints, never a full repository read.',planner.start,{'target':s,'objective':s},['target','objective']),
        ('planning_list','List recent saved plans for an attached target; resume an existing goal instead of restarting inspection.',planner.list_plans,{'target':s},['target']),
        ('planning_view','Read a saved goal, scope, contract, evidence, readiness issues and linked task outcomes.',planner.view,pid,['plan_id']),
        ('planning_locate','Find relevant filename metadata using a specific glob; content is not read. Helpers remain in their assigned scope.',planner.locate,{**pid,'pattern':s},['plan_id','pattern']),
        ('planning_scope','Select initial inspection roots or expand them with a reason and previously observed evidence. Whole-project root is forbidden.',planner.scope,{**pid,'paths':ss,'reason':s,'evidence':ss},['plan_id','paths','reason','evidence']),
        ('planning_read','Read a bounded page inside inspection scope plus applicable project instructions. Offset is bytes. Shared per-goal read budgets apply.',planner.read,{**pid,'path':s,'offset':number},['plan_id','path']),
        ('planning_search','Search text only in selected inspection roots, with bounded file/content coverage. Review coverage notes before concluding absence.',planner.search,{**pid,'query':s},['plan_id','query']),
        ('planning_update','Save a draft goal contract. Keep outcome, change/inspection/verification scopes, decisions, dependencies and acceptance checks explicit. Returns readiness errors.',planner.update,{**pid,'contract':contract},['plan_id','contract']),
        ('planning_remember','Save an evidence-backed subsystem navigation hint for later goals, not a repository dump. Future sessions must verify it.',planner.remember,{**pid,'subsystem':s,'paths':ss,'summary':s,'evidence':ss},['plan_id','subsystem','paths','summary','evidence']),
        ('planning_helper','Ask a bounded native Hermes explorer, plan critic or completion verifier. Read-only tools and current inspection scope only. Helpers cannot expand scope or create tasks.',planner.helper,{**pid,'kind':{'type':'string','enum':['explorer','critic','verifier']},'question':s,'paths':ss},['plan_id','kind','question']),
        ('planning_dispatch','Create one durable Kanban worker task only after the saved contract passes readiness checks. Dependencies must already be landed. Repeated calls reuse its goal.',planner.dispatch,{**pid,'task_id':s},['plan_id','task_id']),
        ('planning_assess','Record a whole-goal assessment separately from task checks. Met criteria require inspected evidence and linked passed goals. Does not implement, approve or merge anything.',planner.assess,
         {**pid,'summary':s,'criteria':{'type':'array','items':{'type':'object','properties':{'id':s,'status':{'type':'string','enum':['met','unmet','unknown']},'reason':s,'evidence_ids':ss,'goal_ids':{'type':'array','items':number}},'required':['id','status','reason','evidence_ids','goal_ids']}}},['plan_id','summary','criteria']),
    ]
    for name,description,handler,properties,required in definitions:
        def dispatch(args,_handler=handler,**kwargs):
            try: return json.dumps(_handler(**args))
            except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as exc: return json.dumps({'error':str(exc)})
        registry.register(name=name,toolset='team_planning_read' if name in READ_TOOLS else 'team_planning',
            schema={'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}},handler=dispatch)
    create_custom_toolset('team_planning_read','Scoped read-only planning evidence',tools=READ_TOOLS)
    create_custom_toolset('team_planning','Goal contracts and bounded planning helpers',tools=[d[0] for d in definitions if d[0] not in READ_TOOLS])
    return planner


def planning_prompt(task):
    from agent.plan_prompt import build_plan_prompt
    return build_plan_prompt(task) + """

Team integration overrides:
Use planning_start/view/locate/scope/read/search/update. Save the structured plan
only in Team state through planning_update; do not write .hermes/plans in the
project. No shell or project writes are available. Start with goal-specific
filename discovery and a subsystem, never read the whole repository. Keep goal,
change scope, inspection scope and verification scope distinct. Use bounded
helpers only for concrete independent questions; keep simple planning local.
Give implementers precise interfaces and acceptance evidence, not speculative
complete implementations or arbitrary tiny tasks. Do not dispatch during this
planning-only turn. Surface consequential unresolved choices to Eli.
"""


def owned_paths_for_goal(root, goal):
    """Resolve ownership from the saved contract, never the worker's prose.

    Legacy manually created goals have no plan marker and retain their grading.
    Scoped goals fail closed if their persisted contract cannot be resolved.
    """
    lines = [line for line in goal['goal'].splitlines() if line.startswith('Team plan task: ')]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError('ambiguous plan assignment')
    plan_id, task_id = lines[0].removeprefix('Team plan task: ').split(':', 1)
    data = Planner(root).load(plan_id)
    if data['target'] != goal['target'] or data['task_goals'].get(task_id) != goal['id']:
        raise ValueError('plan does not own this goal')
    task = next((t for t in data['contract']['tasks'] if t['id'] == task_id), None)
    if task is None or not task.get('files'):
        raise ValueError('missing task ownership')
    return {relative(path) for path in task['files']}
