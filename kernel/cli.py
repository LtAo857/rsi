"""User-facing commands for the trusted supervisor."""

import argparse
import json
from pathlib import Path
import shlex
import sys

from .engine import Engine, SOURCE


def main():
    parser = argparse.ArgumentParser(description="RSI：执行与改进策略共同版本化")
    parser.add_argument("--state", default=str(SOURCE / ".rsi"), help="状态目录")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="创建初始版本")
    run = sub.add_parser("run", help="自动运行自我改进循环")
    run.add_argument("--rounds", type=int, default=2)
    run.add_argument("--timeout", type=int, default=900)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--coder", help="编码命令，提示词通过 stdin 传入，不经 shell")
    mode.add_argument("--demo", action="store_true", help="确定性离线演示，不调用模型")
    solve = sub.add_parser("solve", help="使用当前已接受版本执行任务")
    solve.add_argument("task", help="包含 observation 和 operation 的 JSON")
    sub.add_parser("status", help="查看版本与改进历史")
    sub.add_parser("heldout", help="最终留出集验证，不参与版本选择")
    sub.add_parser("stop", help="当前轮完成后停止")
    sub.add_parser("resume", help="清除停止标记")
    args = parser.parse_args()
    command = None
    if args.action == "run":
        if not 1 <= args.rounds <= 100 or not 1 <= args.timeout <= 7200:
            parser.error("rounds 必须为 1–100，timeout 必须为 1–7200 秒")
        command = ([sys.executable, "-B", str(SOURCE / "examples" / "demo_coder.py")]
                   if args.demo else shlex.split(args.coder))
    engine = Engine(args.state, command, getattr(args, "timeout", 300))
    if args.action == "run":
        engine.run(args.rounds)
        return
    if args.action == "solve":
        print(json.dumps(engine.solve(json.loads(args.task)), ensure_ascii=False))
        return
    # A stop request must not wait for the run's single-writer lock.
    if args.action in ("stop", "resume"):
        if args.action == "stop":
            (engine.root / "STOP").touch()
        else:
            (engine.root / "STOP").unlink(missing_ok=True)
        print(json.dumps({"stop_requested": args.action == "stop"}))
        return
    with engine.lock():
        engine.initialize()
        state = engine.recover(engine.state())
        if args.action == "heldout":
            # Heldout results are not added to the proposal history.
            result = {"active": state["active"], "evaluation": engine.score(
                engine.version(state["active"]), seed=982451653)}
            from .engine import atomic_json
            atomic_json(engine.root / "heldout.json", result)
        else:
            result = {**state, "history": engine.recent_history()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
