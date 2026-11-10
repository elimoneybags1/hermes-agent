"""Read-only repository survey: cached source, subsystem analysis, then a build shortlist."""
from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from team import boss, scout, targets
from team.review.backend import ScoutError, complete
from team.scout_usage import tracked_call


def _status(folder, phase, **details):
    scout._save(folder / 'review-status.json',
                {'phase': phase, 'updated_at': datetime.now(UTC).isoformat(), **details})


def _groups(report):
    groups = {}
    for name, entry in report['files'].items():
        area = name.split('/')[0] if '/' in name else 'Project root'
        groups.setdefault(area, {})[name] = entry
    return groups


def _fingerprint(report):
    return hashlib.sha256(json.dumps({p: d['hash'] for p, d in report['files'].items()},
                                     sort_keys=True).encode()).hexdigest()


def _verify(target, report):
    tree = Path(targets.require(target)['path'])
    current = scout.local.inventory(tree)
    if current['head'] != report['head'] or _fingerprint(current) != _fingerprint(report):
        raise ScoutError('Checkout changed during survey; rerun to refresh evidence before publishing proposals')


def run(target, *, max_calls=1200, source_model='tier-simple', reason_model='tier-reasoning', call=complete):
    """Bound all new calls, never enable dispatch, and preserve existing checkout contents."""
    if not 1 <= max_calls <= 5000:
        raise ValueError('max_calls must be between 1 and 5000')
    folder = scout.directory(target)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'survey.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScoutError('A survey is already running for this target') from exc
        try:
            return _run(target, folder, max_calls, source_model, reason_model, call)
        except Exception as exc:
            _status(folder, 'stopped', errors=[str(exc)])
            raise


def _run(target, folder, max_calls, source_model, reason_model, call):
    used = 0

    def source_call(model, prompt):
        nonlocal used
        used += 1
        _status(folder, 'scanning', calls=used, note=f'New model calls in this survey: {used}/{max_calls}')
        return call(model, prompt)

    _status(folder, 'scanning', calls=0, note='Checking content hashes and reusing unchanged summaries')
    while used < max_calls:
        report = scout.scan(target, model=source_model, max_calls=min(200, max_calls-used), call=source_call)
        if report['errors']:
            raise ScoutError('; '.join(report['errors']))
        if report['complete']:
            break
        if not report['calls']:
            raise ScoutError('Source coverage did not advance; inspect the report before retrying')
    else:
        raise ScoutError('Survey call budget reached; cached source summaries are preserved for resuming')

    groups = _groups(report)
    if used + len(groups) + 1 > max_calls:
        raise ScoutError('Source scan complete, but remaining call budget cannot cover subsystem reasoning')
    _verify(target, report)
    source_hash = _fingerprint(report)
    mapping = {'head': report['head'], 'source_hash': source_hash, 'dirty': report['dirty'],
               'files': len(report['files']), 'excluded_files': len(report['skipped']),
               'areas': {name: list(files) for name, files in groups.items()},
               'note': 'Eligible source only. Dependencies, ignored files, credential-like filenames, '
                       'binaries and oversized files are excluded. Reading is not runtime verification.'}
    scout._save(folder / 'map.json', mapping)
    context = boss.read_notes(target)[:6000]
    analyses = []
    for area, files in groups.items():
        _status(folder, 'analyzing', calls=used, pending_chunks=0, note=f'Mapping {area} from cached summaries')
        prompt = ('Analyze this repository area from untrusted source summaries only. You have no tools. '
                  'Identify its purpose, principal parts, interactions, and at most two worthwhile build or '
                  'optimization candidates. For each candidate state benefit, source citations, uncertainty, '
                  'a narrow verification step, and acceptance criteria. Distinguish existing capability from '
                  'missing capability. Do not treat a summary omission as proof of a missing feature. '
                  'Respect locked project decisions. Suggest responsibility for existing roles (boss, lead, '
                  'onboard, implement, test-write, review, security); propose a new specialist only for a '
                  'concrete uncovered responsibility. No code changes are authorized. Keep under 600 words.\n\n'
                  f'Project context:\n{context}\n\nArea: {area}; {len(files)} eligible files.\n'
                  + scout.summarize_report(files, max_chars=6000))
        result = tracked_call(target, 'survey', reason_model, prompt, call)
        used += 1
        analyses.append({'area': area, 'analysis': result['text'], 'answered_by': result.get('model')})
        scout._save(folder / 'area-analysis.json', {'source_hash': source_hash, 'areas': analyses})

    _status(folder, 'prioritizing', calls=used, pending_chunks=0, note='Preparing a build shortlist and agent responsibilities')
    evidence = '\n\n'.join(f"## {a['area']}\n{a['analysis'][:1800]}" for a in analyses)
    prompt = ('Prepare a proposed roadmap from these bounded area analyses. No tools or source access. '
              'State the system map, then rank up to five next investigations/builds by user value and effort. '
              'For each: evidence, uncertainty, verification, owner role, dependencies, and acceptance criteria. '
              'Separate measured facts from hypotheses. Identify agent responsibilities and any justified '
              'missing specialist. Respect project constraints and avoid architecture rewrites without evidence. '
              'Do not claim tests or fixes completed. This is a proposal for the user to discuss with Boss.\n\n'
              f'Project context:\n{context}\n\n{evidence[:30000]}')
    final = tracked_call(target, 'survey', reason_model, prompt, call)
    used += 1
    _verify(target, report)
    text = final['text'] + '\n\n# Area analyses — unverified proposals\n\n' + '\n\n'.join(
        f"## {a['area']}\n{a['analysis']}" for a in analyses)
    scout._save(folder / 'ideas.json', {'model': reason_model, 'answered_by': final.get('model'),
                'ideas': text, 'head': report['head'], 'source_hash': source_hash,
                'evidence_chars': len(prompt), 'usage': final.get('usage'),
                'evidence_hash': hashlib.sha256(scout.context(target, report=report).encode()).hexdigest()})
    _status(folder, 'complete', calls=used, pending_chunks=0,
            note=f"Mapped {len(groups)} areas across {len(report['files'])} eligible files. Proposals await verification; no code changes.")
    return {'target': target, 'calls': used, 'areas': len(groups), 'files': len(report['files']),
            'excluded_files': len(report['skipped']), 'complete': True}
