"""Fixed tests and independent scoring; never copied into editable candidates."""

import json
import random


def tasks(seed, count=12, suite="basic"):
    if suite not in ("basic", "advanced"):
        raise ValueError("unknown benchmark suite")
    rng = random.Random(seed)
    result = []
    for i in range(count):
        values = [rng.randint(-30, 80) for _ in range(rng.randint(2, 10))]
        operation = ("sum", "mean", "count")[i % 3]
        observation = json.dumps({"values": values})
        if i % 2:
            observation = "```json\n" + observation + "\n```"
        expected = {"sum": sum(values), "mean": sum(values) / len(values),
                    "count": len(values)}[operation]
        if suite == "advanced" and i % 6 >= 3:
            rows = [{"value": value, "weight": rng.randint(1, 5),
                     "group": rng.choice(["a", "b", "c"])} for value in values]
            operation = ("filtered_sum", "weighted_mean", "group_max_total")[i % 3]
            payload = {"rows": rows, "threshold": 10}
            if operation == "filtered_sum":
                expected = sum(r["value"] for r in rows if r["value"] > 10)
            elif operation == "weighted_mean":
                expected = sum(r["value"] * r["weight"] for r in rows) / sum(r["weight"] for r in rows)
            else:
                expected = max(sum(r["value"] for r in rows if r["group"] == group)
                               for group in {r["group"] for r in rows})
            observation = json.dumps(payload)
            if i % 2:
                observation = "Tool result:\n```json\n" + observation + "\n```"
        task = {"observation": observation, "operation": operation}
        if suite == 'advanced':
            instructions = {'sum':'Sum values.', 'mean':'Arithmetic average of values.', 'count':'Number of values.',
                            'filtered_sum':'Sum row.value only where row.value is strictly greater than threshold.',
                            'weighted_mean':'Sum row.value * row.weight divided by sum row.weight.',
                            'group_max_total':'Sum row.value within each group and return the largest group sum. Do not apply weights.'}
            task['instruction'] = instructions[operation]
        result.append((task, expected))
    return result


REPAIR_SOURCE = '''def total(values):
    return values[0]
'''

# These tests are visible to the repair worker. Additional cases are supervisor-only.
REPAIR_TESTS = '''import unittest
from solution import total

class TestTotal(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(total([]), 0)
    def test_multiple(self):
        self.assertEqual(total([2, 3]), 5)
'''


def repair_goal():
    return ("Repair solution.py: total(values) must sum all supplied numbers. "
            "Only solution.py may be changed. Existing tests must remain unchanged.")


def repair_case(suite="basic", index=0):
    """Supervisor-owned repair fixtures; paired trials use the same case index."""
    cases = [{"name": "sum", "failure": "total ignores later values",
              "files": {"solution.py": REPAIR_SOURCE, "test_solution.py": REPAIR_TESTS},
              "mutable": ["solution.py"], "goal": repair_goal(),
              "expression": "[total(v) for v in [[], [2,3], [-7,4,10], [0.5,1.25], [8]]]",
              "imports": "from solution import total", "expected": [0, 5, 7, 1.75, 8]}]
    if suite == "advanced":
        cases += [
            {"name": "json_recovery", "failure": "parse_values rejects fenced tool output",
             "files": {"solution.py": "import json\ndef parse_values(text):\n    return json.loads(text)['values']\n",
                       "test_solution.py": "from solution import parse_values\nassert parse_values('{\"values\":[1]}') == [1]\n"},
             "mutable": ["solution.py"],
             "goal": "Repair parse_values(text) in solution.py to read a JSON object, optionally wrapped in markdown fences or a Tool result: prefix. Return its values list. Preserve tests.",
             "imports": "from solution import parse_values",
             "expression": "[parse_values(v) for v in ['{\"values\":[]}', '```json\\n{\"values\":[2,3]}\\n```', 'Tool result:\\n```json\\n{\"values\":[-7]}\\n```']]",
             "expected": [[], [2, 3], [-7]]},
            {"name": "multi_file", "failure": "unique_active ignores status and normalized duplicate emails",
             "files": {"solution.py": "from helpers import normalize\ndef unique_active(rows):\n    return len(rows)\n",
                       "helpers.py": "def normalize(value):\n    return value\n",
                       "test_solution.py": "from solution import unique_active\nassert unique_active([]) == 0\n"},
             "mutable": ["solution.py", "helpers.py"],
             "goal": "Repair solution.py and helpers.py. unique_active(rows) counts distinct emails among rows with active == True, normalizing emails by stripping whitespace and lowercasing with helpers.normalize. Preserve test_solution.py.",
             "imports": "from solution import unique_active; from helpers import normalize",
             "expression": "[unique_active([]), unique_active([{'email':' A@B ', 'active':True}, {'email':'a@b','active':True}, {'email':'c@d','active':False}]), normalize(' X@Y ')]",
             "expected": [0, 1, "x@y"]}]
    return cases[index % len(cases)]


def heldout_repair_cases():
    """Diagnostic projects, never returned by repair_case or used for selection.

    The worker sees the contract and visible tests, but not expression/expected.
    These are fixed local fixtures, not a hidden external benchmark.
    """
    return [
        {'name': 'csv_quoting', 'failure': 'read_names splits quoted commas and retains blank rows',
         'files': {'solution.py': 'def read_names(text):\n    return [line.split(",")[0] for line in text.splitlines()[1:]]\n',
                   'test_solution.py': 'from solution import read_names\nassert read_names("name,age\\nAlice,20") == ["Alice"]\n'},
         'mutable': ['solution.py'],
         'goal': 'Repair solution.py read_names(text). Parse CSV using its first row as a header; return the name column in order. Support quoted commas, escaped quotes and embedded newlines. Skip empty rows. Empty input or header-only input returns []. Preserve test_solution.py.',
         'imports': 'from solution import read_names',
         'expression': '[read_names(v) for v in ' + repr(['', 'name,age\n', 'name,age\n"Doe, Jane",20\n\nBob,30',
                             'age,name\n10,"A""B"', 'name,age\n"Line\nBreak",20']) + ']',
         'expected': [[], [], ['Doe, Jane', 'Bob'], ['A"B'], ['Line\nBreak']]},
        {'name': 'interval_merge', 'failure': 'merge combines disjoint intervals and fails empty input',
         'files': {'solution.py': 'def merge(intervals):\n    return [[min(a for a,b in intervals), max(b for a,b in intervals)]]\n',
                   'test_solution.py': 'from solution import merge\nassert merge([[1,3],[2,4]]) == [[1,4]]\n'},
         'mutable': ['solution.py'],
         'goal': 'Repair solution.py merge(intervals). Return sorted closed intervals merging overlaps and touching endpoints, preserving disjoint intervals. Empty input returns []. Endpoints are numbers with start <= end. Do not mutate the input or its nested lists. Preserve test_solution.py.',
         'imports': 'from solution import merge',
         'setup': 'values = [[3,4],[1,3]]; before = [row[:] for row in values]; merged = merge(values)',
         'expression': '[merge([]), merge([[4,5],[1,2]]), merge([[5,5],[-3,0],[0,2]]), merge([[0.5,2],[1,3]]), values == before]',
         'expected': [[], [[1, 2], [4, 5]], [[-3, 2], [5, 5]], [[0.5, 3]], True]},
    ]


def answers_equal(actual, expected):
    """Numeric equivalence without treating booleans as numbers, even nested."""
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(answers_equal(a, b) for a, b in zip(actual, expected)))
    return ((type(actual) is type(expected) or
             type(actual) in (int, float) and type(expected) in (int, float))
            and actual == expected)
