"""Evidence-linked trajectory rules, not semantic root-cause verification."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, DecimalException


def verify_pagination_arithmetic(trace):
    """Verify only the two fixed CSV task contracts using fully observed rows."""
    tid = trace.get('task',{}).get('id')
    if tid not in ('p-active_sum','p-active_count') or trace.get('ok') is not False:
        return None
    observed, end, steps = {}, None, []
    for index, entry in enumerate(trace.get('history',[])):
        action, result = entry.get('action',{}), entry.get('result',{})
        if not isinstance(action,dict) or action.get('tool')!='read_csv':
            continue
        args = action.get('args',{})
        if not isinstance(args,dict):
            return None
        if args.get('path')!='records.csv' or not isinstance(result.get('rows'),list):
            continue
        offset = args.get('offset',0)
        if type(offset) is not int or not 0<=offset<=10000:
            return None
        steps.append(index)
        for i,row in enumerate(result['rows']):
            if offset+i in observed and observed[offset+i] != row:
                return None
            observed[offset+i] = row
        if result.get('next_offset') is None:
            end = offset+len(result['rows'])
    if end is None or end>10000 or set(observed)!=set(range(end)):
        return None
    try:
        rows = [row for row in observed.values() if row['status']=='active']
        expected = sum((Decimal(row['qty']) for row in rows),Decimal(0)) if tid=='p-active_sum' else Decimal(len(rows))
        actual = Decimal(str(trace.get('answer','')).strip())
        if not expected.is_finite() or not actual.is_finite() or actual==expected:
            return None
    except (DecimalException,ValueError,KeyError,TypeError):
        return None
    return {'kind':'verified_arithmetic_mismatch','steps':steps,'certainty':'verified_for_fixed_task',
            'detail':'All CSV rows were observed; recomputing the fixed active-record operation disagrees with the submitted number. No hidden answer was read.'}


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
    verified = verify_pagination_arithmetic(trace)
    if verified:
        findings.append(verified)
    return {'method':'trajectory_rules_v1', 'findings':findings,
            'outcome':'correct' if trace.get('ok') is True else 'incorrect' if trace.get('ok') is False else 'ungraded',
            'scope':'rules and risks; fixed pagination arithmetic may be verified from complete observations; other semantic causes are unverified'}


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


def compact_evidence(evaluation):
    """Keep step indexes and explicit omissions; full data stays in audit files."""
    result = deepcopy(evaluation)
    for trace in result.get('traces',[]):
        for entry in trace.get('history',[]):
            observation = entry.get('result',{})
            if isinstance(observation.get('rows'),list) and len(observation['rows'])>2:
                observation['evidence_rows_omitted'] = len(observation['rows'])-2
                observation['rows'] = observation['rows'][:2]
            if isinstance(observation.get('text'),str) and len(observation['text'])>1000:
                observation['evidence_chars_omitted'] = len(observation['text'])-1000
                observation['text'] = observation['text'][:1000]
    return result


def task_failure_evidence(evaluations):
    failures = []
    for trial, evaluation in enumerate(evaluations):
        for index, trace in enumerate(compact_evidence(evaluation)['traces']):
            if not trace['ok']:
                failures.append({'id':f'task-{index}' if trial==0 else f'trial-{trial}-task-{index}',
                                 'kind':'task_failure','trial':trial,'trace':trace})
    return failures
