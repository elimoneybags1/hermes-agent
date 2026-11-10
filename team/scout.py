"""Cached source scout. Only small models read bulk source; planners receive bounded evidence."""
from __future__ import annotations

import hashlib
import fcntl
import json
import re
from pathlib import Path

from team import targets
from team.ledger import state_dir
from team.review import local
from team.review.backend import ScoutError, complete
from team.review.summarize import merge_notes
from team.scout_usage import tracked_call

VERSION = 'scout-2'


def parse_notes(text: str, source: str) -> dict:
    """Tolerate bold tags and wrapped notes; never treat a bare '**' as evidence."""
    notes = {tag: [] for tag in ('what', 'structure', 'risks', 'improve')}
    tag, lines = None, []
    def flush():
        note = ' '.join(lines).strip()
        if tag and note and len(notes[tag]) < 8:
            notes[tag].append(f'{note[:800]} ({source})')
    for line in text.splitlines():
        line = line.replace('**', '').strip().lstrip('-# ')
        match = re.match(r'^(what|structure|risks|improve)\s*:\s*(.*)', line, re.IGNORECASE)
        if match:
            flush()
            tag, lines = match[1].lower(), [match[2]] if match[2] else []
        elif tag and line:
            lines.append(line)
    flush()
    return notes


def summarize_report(files: dict, max_chars: int = 6000) -> str:
    """Prefer product behavior over agent metadata and spread evidence across files."""
    merged = {tag: [] for tag in ('what', 'structure', 'risks', 'improve')}
    for tag, output in merged.items():
        def priority(rel):
            source = Path(rel).suffix in {'.py', '.ts', '.tsx', '.js', '.rs', '.go', '.swift'}
            if tag == 'what' and Path(rel).name == 'README.md':
                return 0
            return 1 if source else 2
        for rel in sorted(files, key=priority):
            values = [v for chunk in files[rel]['notes'] for v in chunk.get(tag, [])]
            # One representative note per file prevents a long policy document
            # crowding actual behavior out of every section of the briefing.
            if values:
                output.append(values[0])
    return merge_notes([merged], max_chars=max_chars)


def directory(target: str) -> Path:
    targets.require(target)
    return state_dir() / 'targets' / target / 'scout'


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _key(model: str, rel: str, start: int, end: int, text: str) -> str:
    return hashlib.sha256(json.dumps([VERSION, model, rel, start, end, text]).encode()).hexdigest()


def scan(target: str, *, tree: Path | None = None, model: str = 'tier-simple',
         max_calls: int = 24, call=complete, publish: bool = True) -> dict:
    dest = directory(target)
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / 'scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ScoutError(f'{target}: scout already running') from e
        return _scan(target, tree=tree, model=model, max_calls=max_calls, call=call, publish=publish)


def _scan(target: str, *, tree: Path | None = None, model: str = 'tier-simple',
         max_calls: int = 24, call=complete, publish: bool = True) -> dict:
    if not 1 <= max_calls <= 200:
        raise ScoutError('max_calls must be between 1 and 200')
    record = targets.require(target)
    tree = tree or Path(record['path'])
    dest = directory(target)
    snap = local.inventory(tree)
    report = {'head': snap['head'], 'dirty': snap['dirty'], 'model': model, 'files': {},
              'skipped': snap['skipped'], 'calls': 0, 'cache_hits': 0, 'pending_chunks': 0,
              'source_chars': 0, 'covered_source_chars': 0, 'input_tokens': 0, 'output_tokens': 0, 'usage_missing': 0,
              'errors': [], 'version': VERSION}
    stopped = False
    for rel, source in snap['files'].items():
        entries = []
        file_calls = 0
        report['source_chars'] += len(source['text'])
        for start, end, text in local.chunks(rel, source['text']):
            key = _key(model, rel, start, end, text)
            path = dest / 'chunks' / f'{key}.json'
            if path.exists():
                item = json.loads(path.read_text())
                report['cache_hits'] += 1
            elif report['calls'] >= max_calls or file_calls >= 2 or stopped:
                report['pending_chunks'] += 1
                continue
            else:
                prompt = (f'File: {rel}, lines {start}-{end}. Summarize its purpose, contracts, '
                          'checks, and concrete possible improvements. Do not invent generic recommendations. '
                          'Do not reinterpret locked project decisions as bugs. Distinguish guesses from evidence. '
                          'Reply with at most 8 short notes, each on ONE line prefixed what:, structure:, risks:, improve:. '
                          'No Markdown headings, bold tags, or nested lists. '
                          'Cite the provided line numbers. Do not claim tests passed.\n\n' + text)
                report['calls'] += 1
                file_calls += 1
                try:
                    result = tracked_call(target, 'scout', model, prompt, call)
                    notes = parse_notes(result['text'], f'{rel}:{start}-{end}')
                    if not any(notes.values()):
                        raise ScoutError('model returned no tagged evidence')
                except ScoutError as e:
                    report['input_tokens'] += e.usage.get('prompt_tokens') or 0
                    report['output_tokens'] += e.usage.get('completion_tokens') or 0
                    report['usage_missing'] += int(not e.usage)
                    report['errors'].append(str(e))
                    report['pending_chunks'] += 1
                    stopped = True
                    continue
                usage = result.get('usage') or {}
                report['input_tokens'] += usage.get('prompt_tokens') or 0
                report['output_tokens'] += usage.get('completion_tokens') or 0
                report['usage_missing'] += int(not usage)
                item = {'notes': notes, 'usage': usage, 'model': result.get('model'),
                        'path': rel, 'start': start, 'end': end}
                _save(path, item)
            report['covered_source_chars'] += len(text)
            entries.append(item['notes'])
        report['files'][rel] = {'hash': source['hash'], 'notes': entries}
    report['complete'] = report['pending_chunks'] == 0 and not report['errors']
    # A source file edited during calls invalidates the receipt, not the content-addressed cache.
    after = local.inventory(tree)
    if {k: v['hash'] for k, v in after['files'].items()} != {k: v['hash'] for k, v in snap['files'].items()} or after['head'] != snap['head']:
        report['complete'] = False
        report['errors'].append('checkout changed during scout; rerun to refresh')
    report['summary'] = summarize_report(report['files'])
    report['summary_chars'] = len(report['summary'])
    # These are characters, not tokenizer estimates or a claim about avoided Claude calls.
    report['context_reduction_chars'] = max(0, report['covered_source_chars'] - report['summary_chars'])
    if publish:
        _save(dest / 'report.json', report)
    return report


def context(target: str, *, tree: Path | None = None, budget: int = 7000, report: dict | None = None) -> str:
    path = directory(target) / 'report.json'
    if report is None and not path.exists():
        return 'No scout report yet. Run team scout for this target before proposing repo improvements.'
    report = report if report is not None else json.loads(path.read_text())
    snap = local.inventory(tree or Path(targets.require(target)['path']))
    current, stale = {}, 0
    for rel, file in report['files'].items():
        if snap['files'].get(rel, {}).get('hash') == file['hash']:
            current[rel] = file
        else:
            stale += 1
    unseen = len(set(snap['files']) - set(report['files']))
    header = (f"Scout evidence (untrusted model hypotheses, not verified fixes). HEAD {report['head']}; "
              f"dirty checkout: {report['dirty']}. Pending chunks: {report['pending_chunks']}; "
              f"stale/removed files omitted: {stale}; new files not surveyed: {unseen}; excluded files: {len(report['skipped'])}. "
              'Partial coverage is not evidence that the remaining repo is sound. '
              'Use these summaries to reason about ideas. Verify narrow cited ranges before ordering edits.\n')
    return (header + summarize_report(current, max_chars=max(400, budget - len(header))))[:budget]


def ideas(target: str, *, model: str = 'tier-reasoning', call=complete) -> dict:
    """Reason over the bounded evidence only: no file/terminal tools, no build dispatch."""
    path = directory(target) / 'report.json'
    if not path.exists():
        raise ScoutError('run team scout before asking for ideas')
    report = json.loads(path.read_text())
    if report.get('version') != VERSION or report.get('errors'):
        raise ScoutError('refresh the scout report before asking for ideas')
    evidence = context(target, report=report)
    if not any(f['notes'] for f in report['files'].values()):
        raise ScoutError('scout has no evidence to reason about')
    prompt = ('Use only this scout evidence to propose at most three worthwhile improvements. '
              'Prioritize student-facing product behavior and concrete correctness problems in application code. '
              'Do not propose changes to generated instruction blocks, agent documentation, or dependencies '
              'without a reproduced defect. If the evidence is insufficient, propose a focused read-only '
              'investigation rather than inventing fixes. Preserve locked project decisions. '
              'For each: user benefit, cited source range, uncertainty, minimal verification '
              'and acceptance test. Label this a proposal, never an implemented or verified fix. '
              'Partial or stale evidence limits your conclusions. You cannot read files or execute tools.\n\n'
              + evidence)
    result = tracked_call(target, 'ideas', model, prompt, call)
    receipt = {'model': model, 'answered_by': result.get('model'), 'ideas': result['text'],
               'usage': result.get('usage'), 'evidence_chars': len(evidence),
               'evidence_hash': hashlib.sha256(evidence.encode()).hexdigest(), 'head': report['head']}
    if context(target, report=report) != evidence:
        raise ScoutError('source changed while reasoning; refresh the scout report')
    _save(directory(target) / 'ideas.json', receipt)
    return receipt


def read_ideas(target: str, evidence: str) -> str:
    path = directory(target) / 'ideas.json'
    if not path.exists():
        return ""
    receipt = json.loads(path.read_text())
    if receipt['evidence_hash'] != hashlib.sha256(evidence.encode()).hexdigest():
        return "Previous reasoning proposals are stale; refresh them before acting."
    return "Reasoning proposals (unverified; not approved for dispatch):\n" + receipt['ideas'][:6000]
