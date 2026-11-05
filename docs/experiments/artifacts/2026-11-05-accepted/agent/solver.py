"""Small task agent: parses JSON observations (plain or wrapped in markdown
fences, possibly prefixed with prose like "Tool result:") and computes the
requested aggregate operation.
"""

import json
import re


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text):
    """Return parsed JSON from text that may be plain, fenced, or prefixed by prose."""
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    fence = _FENCE.search(stripped)
    if fence:
        return json.loads(fence.group(1).strip())
    # Fallback: find the first balanced JSON object/array in the text.
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stripped):
        if ch in "[{":
            try:
                obj, _end = decoder.raw_decode(stripped[idx:])
                return obj
            except ValueError:
                continue
    return json.loads(stripped)


def parse_observation(task):
    return _extract_json(task["observation"])


def build_messages(task):
    return {'system': 'Compute the requested operation. Return only a JSON number.',
            'prompt': json.dumps(task, ensure_ascii=False)}


def _compute(observation, operation):
    if operation in ("sum", "mean", "count"):
        values = observation["values"]
        if operation == "sum":
            return sum(values)
        if operation == "mean":
            return sum(values) / len(values)
        return len(values)
    if operation == "filtered_sum":
        rows = observation["rows"]
        threshold = observation["threshold"]
        return sum(row["value"] for row in rows if row["value"] > threshold)
    if operation == "weighted_mean":
        rows = observation["rows"]
        total_weight = sum(row["weight"] for row in rows)
        return sum(row["value"] * row["weight"] for row in rows) / total_weight
    if operation == "group_max_total":
        rows = observation["rows"]
        totals = {}
        for row in rows:
            totals[row["group"]] = totals.get(row["group"], 0) + row["value"]
        return max(totals.values())
    raise ValueError("unsupported operation: %r" % (operation,))


def solve(task, llm=None):
    if llm is not None:
        messages = build_messages(task)
        return json.loads(llm(messages['system'], messages['prompt']))
    observation = parse_observation(task)
    return _compute(observation, task["operation"])
