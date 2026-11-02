"""A deliberately small starting agent; useful for testing the full lifecycle.

Tasks supply JSON observations, sometimes wrapped in model-style markdown.
The initial parser deliberately handles only plain JSON.
"""

import json


def solve(task):
    observation = json.loads(task["observation"])
    values = observation["values"]
    operation = task["operation"]
    if operation == "sum":
        return sum(values)
    if operation == "mean":
        return sum(values) / len(values)
    if operation == "count":
        return len(values)
    raise ValueError("unsupported operation")
