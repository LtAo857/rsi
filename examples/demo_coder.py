"""Scripted lifecycle fixture, NOT a model and NOT evidence of autonomous learning.

Two predetermined edits make it possible to exercise activation and handoff
without credentials, network calls or spending model quota.
"""

from pathlib import Path
import sys


def main():
    prompt = sys.stdin.read()
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


if __name__ == "__main__":
    main()
