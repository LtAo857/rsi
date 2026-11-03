"""Scripted lifecycle fixture, NOT a model and NOT evidence of autonomous learning.

Two predetermined edits make it possible to exercise activation and handoff
without credentials, network calls or spending model quota.
"""

import json
from pathlib import Path
import sys


def edit(prompt):
    root = Path.cwd()
    solution = root / "solution.py"
    if solution.exists():
        # Demonstrate that the improved generation's prompt reaches the next coder.
        if "Read the tests before editing" in prompt:
            solution.write_text("def total(values):\n    return sum(values)\n")
        return
    solver = root / "agent" / "solver.py"
    source = solver.read_text()
    if 'text.startswith("```")' not in source:
        source = source.replace(
            '    observation = json.loads(task["observation"])',
            '    text = task["observation"].strip()\n'
            '    if text.startswith("```"):\n'
            '        text = text.split("\\n", 1)[1].rsplit("```", 1)[0].strip()\n'
            '    observation = json.loads(text)')
        solver.write_text(source)
    else:
        policy = root / "evolution" / "policy.py"
        source = policy.read_text()
        if "Read the tests before editing" not in source:
            source = source.replace(
                '"Improve this Python system using the failure evidence. "',
                '"Read the tests before editing. Run them after the patch. "\n'
                '        "Improve this Python system using the failure evidence. "')
            policy.write_text(source)


def main():
    prompt = sys.stdin.read()
    paths = [Path('agent/solver.py'), Path('evolution/policy.py')]
    before = {str(p): p.read_text() for p in paths if p.exists()}
    edit(prompt)
    if before:
        evidence = json.loads(prompt.rsplit('Evidence:\n', 1)[1])
        changes = []
        for name, source in before.items():
            if Path(name).read_text() != source:
                failure_id = ('task-1' if name.startswith('agent/') else 'policy-repair')
                changes.append({'files': [name], 'failure_id': failure_id,
                                'cause': 'Scripted fixture for the supplied failure',
                                'expected_effect': 'Exercise lifecycle improvement',
                                'validation': 'Run supervisor evaluations'})
        Path('.rsi-proposal.json').write_text(json.dumps({
            'analysis': 'Scripted fixture: inspect supplied failures before applying predetermined edits.',
            'changes': changes}))
    print(json.dumps({'type': 'usage', 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0}))


if __name__ == "__main__":
    main()
