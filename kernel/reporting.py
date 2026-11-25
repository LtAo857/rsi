"""Read-only experiment reporting, state aggregation, and cross-state comparison."""

from collections import Counter
import json
from pathlib import Path


def write_markdown_report(root, state, history):
    lines = ["# RSI 运行报告", "", f"当前版本：`{state['active']}`",
             f"已完成轮数：{state['round']}", "",
             "| 轮次 | 使用的策略版本 | 基线（任务/修复） | 候选 | 接受 |",
             "|---|---|---|---|---|"]
    for record in history:
        if record.get("type") == "round":
            lines.append(f"| {record['round']} | {record['planner_version'][:12]} | "
                         f"{record['baseline']} | {record.get('candidate', '未通过')} | "
                         f"{record['accepted']} |")
    lines += ["", "完整历史见 history.jsonl；每轮的证据、源码、diff 与日志见 rounds/。",
              "离线 demo 使用预设补丁，分数仅验证生命周期，不证明模型自主改进能力。"]
    (Path(root) / "REPORT.md").write_text("\n".join(lines) + "\n")


def _load_json(path):
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def summarize_state(state_dir):
    """Read-only summary of an RSI state directory without creating or modifying files."""
    root = Path(state_dir).resolve()
    state = _load_json(root / "state.json")
    if state is None:
        raise ValueError(f"state.json not found in {root}")
    runtime = _load_json(root / "runtime.json") or {
        "benchmark": "basic",
        "datadesk_size": "compact",
        "host_sandbox": "none",
        "task_command": None,
    }
    usage = _load_json(root / "usage.json") or {
        "calls": 0, "tokens": 0, "cost_usd": 0.0,
        "unknown_tokens": 0, "unknown_cost": 0,
    }
    history_path = root / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text().splitlines() if line.strip()] if history_path.is_file() else []
    rounds = []
    rejected_reasons = Counter()
    diagnostic_counts = Counter()
    for row in history:
        if row.get("type") != "round":
            continue
        rounds.append({
            "round": row.get("round"),
            "planner_version": row.get("planner_version"),
            "baseline": row.get("baseline"),
            "candidate": row.get("candidate"),
            "confirmation": row.get("confirmation"),
            "accepted": bool(row.get("accepted")),
            "stage": row.get("stage"),
            "reason": row.get("reason"),
            "changed": row.get("changed", []),
        })
        if not row.get("accepted") and row.get("reason"):
            rejected_reasons[row["reason"]] += 1
        audit = Path(row["audit"]) if row.get("audit") else None
        if audit and audit.is_dir():
            for tasks_file in sorted(audit.glob("base-meta-*.tasks.json")):
                payload = _load_json(tasks_file)
                if isinstance(payload, dict) and isinstance(payload.get("diagnostic_summary"), dict):
                    diagnostic_counts.update(payload["diagnostic_summary"].get("counts", {}))
    evaluation = _load_json(root / "evaluation.json")
    eval_summary = None
    if isinstance(evaluation, dict) and isinstance(evaluation.get("evaluation"), dict):
        inner = evaluation["evaluation"]
        eval_summary = {
            "active": evaluation.get("active"),
            "score": inner.get("score"),
            "split": inner.get("split"),
            "seed": inner.get("seed"),
            "size": inner.get("size"),
            "tasks": len(inner.get("traces", [])),
            "diagnostic_summary": inner.get("diagnostic_summary"),
        }
    ablation = _load_json(root / "ablation.json")
    ablation_summary = None
    if isinstance(ablation, dict):
        ablation_summary = {
            "active": ablation.get("active"),
            "status": ablation.get("status"),
            "variants": [
                {
                    "name": v.get("name"),
                    "conclusion": v.get("conclusion"),
                    "observed_regressions": v.get("observed_regressions"),
                    "confirmed_task_benefit": v.get("confirmed_task_benefit"),
                    "deletion_authorized": v.get("deletion_authorized"),
                }
                for v in ablation.get("variants", []) if isinstance(v, dict)
            ],
        }
    return {
        "state_dir": str(root),
        "active": state.get("active"),
        "previous": state.get("previous"),
        "rounds_completed": state.get("round", 0),
        "accepted_rounds": sum(1 for r in rounds if r["accepted"]),
        "runtime": {
            "benchmark": runtime.get("benchmark", "basic"),
            "datadesk_size": runtime.get("datadesk_size", "compact"),
            "host_sandbox": runtime.get("host_sandbox", "none"),
            "has_task_command": bool(runtime.get("task_command")),
        },
        "usage": usage,
        "rounds": rounds,
        "rejected_reasons": dict(sorted(rejected_reasons.items())),
        "baseline_diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "latest_evaluation": eval_summary,
        "latest_ablation": ablation_summary,
    }


def compare_states(state_a, state_b):
    """Read-only comparison of two RSI state directories."""
    left, right = summarize_state(state_a), summarize_state(state_b)
    def score_of(summary):
        ev = summary.get("latest_evaluation")
        if ev and ev.get("score") is not None:
            return ev["score"]
        if summary["rounds"]:
            last = summary["rounds"][-1]
            return last["baseline"][0] if last.get("baseline") else None
        return None
    left_score, right_score = score_of(left), score_of(right)
    left_usage, right_usage = left["usage"], right["usage"]
    tokens_delta = (
        right_usage["tokens"] - left_usage["tokens"]
        if left_usage.get("unknown_tokens", 0) == 0 and right_usage.get("unknown_tokens", 0) == 0
        else None
    )
    return {
        "left": left,
        "right": right,
        "comparison": {
            "same_active_version": left["active"] == right["active"],
            "same_runtime": left["runtime"] == right["runtime"],
            "rounds_delta": right["rounds_completed"] - left["rounds_completed"],
            "accepted_rounds_delta": right["accepted_rounds"] - left["accepted_rounds"],
            "score_delta": (right_score - left_score) if (left_score is not None and right_score is not None) else None,
            "calls_delta": right_usage.get("calls", 0) - left_usage.get("calls", 0),
            "tokens_delta": tokens_delta,
        },
    }
