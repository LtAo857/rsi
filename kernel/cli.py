"""User-facing commands for the trusted supervisor."""

import argparse
import json
from pathlib import Path
import shlex
import sys

from .engine import Engine, SOURCE
from .backends import backend_command


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
    mode.add_argument("--coder-backend", choices=["codex", "deepseek"], help="内置编码后端")
    run.add_argument("--coder-model", default="", help="编码模型名，Codex 留空使用本机配置")
    mode.add_argument("--demo", action="store_true", help="确定性离线演示，不调用模型")
    run.add_argument('--trials', type=int, default=2, help='每阶段配对评测次数')
    run.add_argument('--benchmark', choices=['basic', 'advanced'],
                     help='评测套件；advanced 增加条件聚合、分组与多类代码修复，选择后锁定')
    run.add_argument('--noise-floor', type=float, default=.01, help='最小有效分数提升')
    run.add_argument('--edit-budget', type=int, default=200, help='初始新增加删除行数上限')
    run.add_argument('--min-edit-budget', type=int, default=20)
    run.add_argument('--decay', type=float, default=.9)
    run.add_argument('--stagnation', type=int, default=2)
    run.add_argument('--prune-every', type=int, default=5)
    run.add_argument('--max-calls', type=int, help='状态目录累计外部调用上限，含审查')
    run.add_argument('--max-tokens', type=int, help='累计 token 上限，需要 usage JSONL')
    run.add_argument('--max-cost', type=float, help='累计美元上限，需要 usage JSONL')
    review_mode = run.add_mutually_exclusive_group()
    review_mode.add_argument('--reviewer-backend', choices=['codex', 'deepseek'])
    run.add_argument('--reviewer-model', default='')
    review_mode.add_argument('--reviewer', help='独立审查命令，stdin 接收 JSON，stdout 输出批准结论')
    run.add_argument('--worktrees', action='store_true', help='为每个候选建立独立 Git worktree')
    run.add_argument('--cache-baseline', action='store_true', help='复用同版本、同配置、同 trial 的已完成初评基线；确认评测不复用')
    solve = sub.add_parser("solve", help="使用当前已接受版本执行任务")
    solve.add_argument("task", help="包含 observation 和 operation 的 JSON")
    solve.add_argument('--max-calls', type=int)
    solve.add_argument('--max-tokens', type=int)
    solve.add_argument('--max-cost', type=float)
    for command_parser in (run, solve):
        command_parser.add_argument('--codex-isolated', action='store_true', help='Codex 忽略用户配置（保留登录），排除外部 MCP 启动干扰')
        command_parser.add_argument('--task-backend', choices=['deepseek', 'codex'], help='任务模型后端，省略使用状态配置或离线求解器')
        command_parser.add_argument('--task-model', default='', help='任务模型名')
        command_parser.add_argument('--base-url', help='DeepSeek/OpenAI 兼容接口基础 URL')
        command_parser.add_argument('--api-key-env', help='存放 API 密钥的环境变量名')
    sub.add_parser("status", help="查看版本与改进历史")
    sub.add_parser("heldout", help="最终留出集验证，不参与版本选择")
    sub.add_parser("stop", help="当前轮完成后停止")
    sub.add_parser("resume", help="清除停止标记")
    args = parser.parse_args()
    command = None
    if args.action == "run":
        if not 1 <= args.rounds <= 100 or not 1 <= args.timeout <= 7200:
            parser.error("rounds 必须为 1–100，timeout 必须为 1–7200 秒")
        if args.demo:
            if args.task_backend or args.reviewer_backend or args.reviewer:
                parser.error('--demo must remain offline; use --coder-backend for model experiments')
            command = [sys.executable, '-B', str(SOURCE / 'examples' / 'demo_coder.py')]
        elif args.coder_backend:
            command = backend_command(args.coder_backend, 'coder', args.coder_model,
                                      args.base_url, args.api_key_env, args.timeout, codex_isolated=args.codex_isolated)
        else:
            command = shlex.split(args.coder)
    settings = {}
    if args.action == 'run':
        settings = {name: getattr(args, name) for name in (
            'trials', 'noise_floor', 'edit_budget', 'min_edit_budget', 'decay',
            'stagnation', 'prune_every', 'max_calls', 'max_tokens', 'max_cost', 'worktrees', 'cache_baseline')}
        settings['reviewer'] = (backend_command(args.reviewer_backend, 'reviewer', args.reviewer_model,
                                                args.base_url, args.api_key_env, args.timeout, codex_isolated=args.codex_isolated)
                                if args.reviewer_backend else
                                shlex.split(args.reviewer) if args.reviewer else None)
    if args.action == 'solve':
        settings = {name: getattr(args, name) for name in ('max_calls', 'max_tokens', 'max_cost')}
    task_command = None
    if getattr(args, 'task_backend', None):
        task_command = backend_command(args.task_backend, 'task', args.task_model,
                                       args.base_url, args.api_key_env, getattr(args, 'timeout', 300), codex_isolated=args.codex_isolated)
    try:
        engine = Engine(args.state, command, getattr(args, "timeout", 300), task_command=task_command,
                        benchmark=getattr(args, 'benchmark', None), **settings)
    except ValueError as error:
        parser.error(str(error))
    if args.action == "run":
        if args.demo and engine.task_command:
            parser.error('--demo cannot reuse a state configured with a task model; choose a new state')
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
