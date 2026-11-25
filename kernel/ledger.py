"""Budget accounting and usage ledger enforcement for external model/coder calls."""

import json
from .store import atomic_json


EMPTY_USAGE = {
    "calls": 0,
    "tokens": 0,
    "cost_usd": 0.0,
    "unknown_tokens": 0,
    "unknown_cost": 0,
}


def read_usage(usage_path):
    if usage_path.exists():
        return json.loads(usage_path.read_text())
    return dict(EMPTY_USAGE)


def reserve_call(usage_path, settings):
    """Check configured budgets and reserve an invocation before process launch."""
    usage = read_usage(usage_path)
    for limit, key in ((settings.max_calls, "calls"),
                       (settings.max_tokens, "tokens"),
                       (settings.max_cost, "cost_usd")):
        if limit is not None and usage[key] >= limit:
            raise RuntimeError("budget exhausted: " + key)
    if ((settings.max_tokens is not None and usage["unknown_tokens"]) or
            (settings.max_cost is not None and usage["unknown_cost"])):
        raise RuntimeError("usage unknown; cannot enforce configured budget")
    # Reserve before launching: a crash or timeout cannot erase a paid invocation.
    usage["calls"] += 1
    usage["unknown_tokens"] += 1
    usage["unknown_cost"] += 1
    atomic_json(usage_path, usage)
    return usage


def settle_call(usage_path, usage, trace):
    """Reconcile reported tokens/cost from a completed trace into the ledger."""
    for key, missing in (("tokens", "unknown_tokens"), ("cost_usd", "unknown_cost")):
        if trace[key] is not None:
            usage[key] += trace[key]
            usage[missing] -= 1
    atomic_json(usage_path, usage)
    return usage


def check_post_call_limits(usage, trace, settings):
    """Enforce configured post-call token and cost ceilings."""
    for limit, key in ((settings.max_tokens, "tokens"), (settings.max_cost, "cost_usd")):
        if limit is not None and trace[key] is None:
            raise RuntimeError("usage missing for configured budget: " + key)
        if limit is not None and usage[key] > limit:
            raise RuntimeError("budget exceeded: " + key)
    if settings.max_call_tokens is not None and trace['tokens'] is not None and trace['tokens'] > settings.max_call_tokens:
        raise RuntimeError('reported call token limit exceeded')
    if settings.max_call_tokens is not None and trace['tokens'] is None:
        raise RuntimeError('usage missing for configured call token limit')
