"""Self-edit policy. Prefers fixes for shared parse/operation causes and keeps
prompts focused on the diagnosed root causes.
"""

import json


def _text_of(failure):
    trace = failure.get('trace') or {}
    return json.dumps(trace, ensure_ascii=False)


def _root_cause(failure):
    """Classify a failure by its traceback text into a coarse root cause."""
    text = _text_of(failure)
    if 'JSONDecodeError' in text:
        return 'observation_parsing'
    if 'KeyError' in text:
        return 'observation_schema'
    if 'unsupported operation' in text:
        return 'unsupported_operation'
    if 'ZeroDivisionError' in text or 'divide' in text:
        return 'empty_aggregate'
    return 'other'


def analyze(evidence):
    failures = evidence.get('failures', [])
    search = evidence.get('search', {}) or {}
    behavioral = [row for row in failures if row.get('kind') != 'maintenance']

    if search.get('prune'):
        selected = [row for row in failures if row.get('kind') == 'maintenance'][:1]
    else:
        selected = behavioral[:3]
    if not selected:
        selected = failures[:1]

    causes = sorted({_root_cause(row) for row in selected})
    target = search.get('target')
    if target is None:
        target = 'evolution' if selected and selected[0].get('kind') == 'repair_failure' else 'agent'
    hypothesis = 'Fix the shared root cause(s) %s across selected failures; preserve successful behavior.' % (causes,)
    return {'failure_ids': [row['id'] for row in selected], 'target': target,
            'hypothesis': hypothesis}


def build_prompt(evidence, goal):
    return (
        "Improve this Python system using the failure evidence. "
        "First analyze the supplied failure evidence and previous edits. "
        "Explain each change with a failure ID, cause, expected effect and validation. "
        "Fix general causes, never hardcode task answers. "
        "Edit the allowed source files directly.\n"
        + goal + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False)
    )
