"""Starting self-edit policy. Candidates may improve this module itself."""

import json


def analyze(evidence):
    """Mutable diagnosis and prioritization; the supervisor validates references."""
    failures = evidence.get('failures', [])
    behavioral = [row for row in failures if row.get('kind') != 'maintenance']
    selected = behavioral[:3] or failures[:1]
    if evidence.get('search', {}).get('prune'):
        selected = [row for row in failures if row.get('kind') == 'maintenance'][:1] or selected
    target = evidence.get('search', {}).get('target')
    if target is None:
        target = 'evolution' if selected and selected[0].get('kind') == 'repair_failure' else 'agent'
    return {'failure_ids': [row['id'] for row in selected], 'target': target,
            'hypothesis': 'Fix a shared cause in the selected failures; preserve successful behavior.'}


def build_prompt(evidence, goal):
    return (
        "Improve this Python system using the failure evidence. "
        "First analyze the supplied failure evidence and previous edits. "
        "Explain each change with a failure ID, cause, expected effect and validation. "
        "Fix general causes, never hardcode task answers. "
        "Edit the allowed source files directly.\n"
        + goal + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False)
    )
