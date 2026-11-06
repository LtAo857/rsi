"""Evidence-linked trajectory rules, not semantic root-cause verification."""

from collections import Counter
from copy import deepcopy


def diagnose_trace(trace):
    findings = []
    pending = {}
    history = trace.get('history', [])
    if (not isinstance(history, list) or any(not isinstance(row, dict) or
            not isinstance(row.get('result', {}), dict) for row in history)):
        raise ValueError('history must contain action/result objects')
    def add(kind, steps, detail, certainty='observed'):
        findings.append({'kind':kind, 'steps':steps, 'detail':detail, 'certainty':certainty})
    for index, row in enumerate(history):
        action, result = row.get('action'), row.get('result', {})
        valid = (isinstance(action, dict) and set(action) == {'tool', 'args'}
                 and isinstance(action['tool'], str) and isinstance(action['args'], dict))
        if not valid:
            add('invalid_action', [index], 'Action does not follow the tool protocol.')
            continue
        name, args = action['tool'], action['args']
        if 'error' in result:
            add('sql_error' if name == 'sql' else 'tool_error', [index], str(result['error'])[:500])
            continue
        if name in ('read_csv', 'read_file'):
            key = (name, str(args.get('path', '')))
            previous = pending.get(key)
            offset = args.get('offset', 0)
            gap = False if offset == 0 else (previous is None or previous[2] or offset != previous[1])
            if result.get('next_offset') is not None or gap:
                pending[key] = (index, result.get('next_offset'), gap)
            else:
                pending.pop(key, None)
        if name == 'sql' and result.get('truncated'):
            add('truncated_sql_result', [index], 'SQL returned only part of its rows.', 'risk')
    reason = trace.get('stop_reason')
    if reason in ('step_limit', 'context_limit', 'repeated_action'):
        add(reason, list(range(max(0, len(history)-3), len(history))), 'Runner stopped before a final answer.')
    if reason == 'answer' and not history:
        add('answer_without_tools', [], 'Answer submitted without a recorded data observation.', 'risk')
    if reason == 'answer' and pending:
        add('unread_pages', sorted(value[0] for value in pending.values()), 'Some reads have remaining or skipped pages; other observations may still suffice.', 'risk')
    if trace.get('error'):
        add('execution_error', [], str(trace['error'])[:500])
    if trace.get('ok') is False and reason == 'answer':
        add('wrong_answer', [], 'Fixed grader rejected the answer; arithmetic or SQL semantics are not inferred.')
    return {'method':'trajectory_rules_v1', 'findings':findings,
            'outcome':'correct' if trace.get('ok') is True else 'incorrect' if trace.get('ok') is False else 'ungraded',
            'scope':'observations and risks only; no verified semantic cause'}


def diagnose_evaluation(evaluation):
    if (not isinstance(evaluation, dict) or not isinstance(evaluation.get('traces'), list)
            or any(not isinstance(row, dict) for row in evaluation['traces'])):
        raise ValueError('expected evaluation with a traces list')
    result = deepcopy(evaluation)
    counts = Counter()
    for row in result['traces']:
        diagnostic = diagnose_trace(row)
        row['diagnostics'] = diagnostic
        counts.update(f['kind'] for f in diagnostic['findings'])
    result['diagnostic_summary'] = {'method':'trajectory_rules_v1', 'counts':dict(sorted(counts.items())),
                                    'tasks':len(result['traces'])}
    return result
