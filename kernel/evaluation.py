"""Fixed tests and independent scoring; never copied into editable candidates."""

import json
import random


def tasks(seed, count=12):
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
        result.append(({"observation": observation, "operation": operation}, expected))
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
