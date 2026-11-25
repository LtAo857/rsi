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
    diagnose = sub.add_parser('diagnose', help='离线诊断已有任务报告，不调用模型、不写搜索历史')
    diagnose.add_argument('report', help='evaluation.json 或 trial tasks.json')
    sub.add_parser('report', help='只读汇总状态目录的轮次、诊断与用量，不修改状态')
    compare = sub.add_parser('compare', help='只读比较两个状态目录的评测、轮次与用量')
    compare.add_argument('left', help='基准状态目录')
    compare.add_argument('right', help='对比状态目录')
    ablate = sub.add_parser('ablate', help='在独立副本中逐机制消融，仅生成诊断报告')
    ablate.add_argument('specs', help='name/path/before/after 对象组成的 JSON 数组')
    ablate.add_argument('--seeds', type=int, default=2)
    ablate.add_argument('--confirm-seeds', type=int, default=0, help='达到初评门槛后，用新输入再次配对确认；默认不运行')
    ablate.add_argument('--seed', type=int, default=982451653)
    ablate.add_argument('--timeout', type=int, default=120)
    for name in ('max-calls','max-tokens','max-call-tokens','max-prompt-chars','max-output-bytes'):
        ablate.add_argument('--'+name, type=int)
    ablate.add_argument('--max-cost', type=float)
    initialize = sub.add_parser("init", help="创建初始版本")
    run = sub.add_parser("run", help="自动运行自我改进循环")
    run.add_argument("--rounds", type=int, default=2)
    run.add_argument("--timeout", type=int, default=900)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--coder", help="编码命令，提示词通过 stdin 传入，不经 shell")
    mode.add_argument("--coder-backend", choices=["codex", "deepseek"], help="内置编码后端")
    run.add_argument("--coder-model", default="", help="编码模型名，Codex 留空使用本机配置")
    mode.add_argument("--demo", action="store_true", help="确定性离线演示，不调用模型")
    run.add_argument('--trials', type=int, default=2, help='每阶段配对评测次数')
    run.add_argument('--benchmark', choices=['basic', 'advanced', 'datadesk'],
                     help='评测套件；datadesk 使用 CSV/SQL 多轮任务模型，选择后锁定')
    run.add_argument('--datadesk-size', choices=['compact', 'full', 'pagination', 'pagination_extended'], help='compact/full 数据分析；pagination / pagination_extended 为 CSV-only 多页读取，选择后锁定')
    run.add_argument('--noise-floor', type=float, default=.01, help='最小有效分数提升')
    run.add_argument('--regression-tolerance', type=float, default=0.0, help='可选配对置信下界容忍，默认逐 trial 不退步')
    run.add_argument('--cost-band', type=float, default=.05, help='接受等能力降本版本所需的最小 token 节省比例')
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
    run.add_argument('--max-call-tokens', type=int, help='单次调用已报告 token 阈值；并非服务端总费用硬限额')
    run.add_argument('--max-prompt-chars', type=int, help='启动前限制提示词字符数')
    run.add_argument('--max-output-bytes', type=int, help='限制单次子进程输出字节数')
    run.add_argument('--provider-output-tokens', type=int, help='DeepSeek 服务端输出 token 上限')
    run.add_argument('--deepseek-thinking', choices=['enabled','disabled'], help='显式控制 DeepSeek 思考模式')
    solve = sub.add_parser("solve", help="使用当前已接受版本执行任务")
    solve.add_argument("task", help="包含 observation 和 operation 的 JSON")
    solve.add_argument('--max-calls', type=int)
    solve.add_argument('--max-tokens', type=int)
    solve.add_argument('--max-cost', type=float)
    evaluate = sub.add_parser('evaluate', help='运行当前版本的任务评测，不参与搜索')
    evaluate.add_argument('--benchmark', choices=['basic', 'advanced', 'datadesk'])
    evaluate.add_argument('--datadesk-size', choices=['compact', 'full', 'pagination', 'pagination_extended'])
    evaluate.add_argument('--split', choices=['search', 'heldout'], default='search')
    evaluate.add_argument('--seed', type=int, default=982451653)
    evaluate.add_argument('--timeout', type=int, default=120)
    for name in ('max-calls', 'max-tokens', 'max-call-tokens', 'max-prompt-chars', 'max-output-bytes'):
        evaluate.add_argument('--' + name, type=int)
    evaluate.add_argument('--max-cost', type=float)
    for command_parser in (run, solve, evaluate):
        command_parser.add_argument('--codex-isolated', action='store_true', help='Codex 忽略用户配置（保留登录），排除外部 MCP 启动干扰')
        command_parser.add_argument('--task-backend', choices=['deepseek', 'codex'], help='任务模型后端，省略使用状态配置或离线求解器')
        command_parser.add_argument('--task-model', default='', help='任务模型名')
        command_parser.add_argument('--base-url', help='DeepSeek/OpenAI 兼容接口基础 URL')
        command_parser.add_argument('--api-key-env', help='存放 API 密钥的环境变量名')
        command_parser.add_argument('--task-output-tokens', type=int, help='DeepSeek 每个任务动作的服务端输出 token 上限')
        command_parser.add_argument('--task-thinking', choices=['enabled', 'disabled'], help='DeepSeek 任务模型思考模式')
    for command_parser in (run,evaluate):
        command_parser.add_argument('--analysis-backend', choices=['deepseek','codex'], help='可选独立失败分析模型；假设不作为评分真值')
        command_parser.add_argument('--analysis-model', default='')
    sub.add_parser("status", help="查看版本与改进历史")
    sub.add_parser("heldout", help="最终留出集验证，不参与版本选择")
    validate = sub.add_parser('validate', help='在多个新种子上比较初始与当前版本，不参与搜索')
    validate.add_argument('--seeds', type=int, default=3)
    policy = sub.add_parser('validate-policy', help='在未参与搜索的固定修复项目上比较策略，会调用编码后端四次')
    policy_mode = policy.add_mutually_exclusive_group(required=True)
    policy_mode.add_argument('--coder', help='编码命令，提示词通过 stdin 传入')
    policy_mode.add_argument('--coder-backend', choices=['codex', 'deepseek'])
    policy.add_argument('--coder-model', default='')
    policy.add_argument('--timeout', type=int, default=300)
    policy.add_argument('--base-url')
    policy.add_argument('--api-key-env')
    policy.add_argument('--codex-isolated', action='store_true')
    policy.add_argument('--provider-output-tokens', type=int)
    policy.add_argument('--deepseek-thinking', choices=['enabled', 'disabled'])
    for name in ('max-calls', 'max-tokens', 'max-call-tokens', 'max-prompt-chars', 'max-output-bytes'):
        policy.add_argument('--' + name, type=int)
    policy.add_argument('--max-cost', type=float)
    sub.add_parser("stop", help="当前轮完成后停止")
    sub.add_parser("resume", help="清除停止标记")
    for command_parser in (run,solve,evaluate,policy,ablate,initialize):
        command_parser.add_argument('--host-sandbox', choices=['none','macos','linux-bwrap'], help='候选 Python 宿主隔离（macos / linux-bwrap），配置随状态锁定；仅允许固定 DeepSeek 网络适配器')
    args = parser.parse_args()
    if args.action == 'diagnose':
        from .diagnostics import diagnose_evaluation
        try:
            report = json.loads(Path(args.report).read_text())
            result = diagnose_evaluation(report.get('evaluation', report))
        except (OSError, ValueError, AttributeError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action == 'report':
        from .reporting import summarize_state
        try:
            result = summarize_state(args.state)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action == 'compare':
        from .reporting import compare_states
        try:
            result = compare_states(args.left, args.right)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action in ('evaluate', 'ablate') and not 1 <= args.timeout <= 7200:
        parser.error('timeout 必须为 1–7200 秒')
    command = None
    if args.action in ('run', 'validate-policy'):
        if not 1 <= getattr(args, 'rounds', 1) <= 100 or not 1 <= args.timeout <= 7200:
            parser.error("rounds 必须为 1–100，timeout 必须为 1–7200 秒")
        if getattr(args, 'demo', False):
            if args.task_backend or args.reviewer_backend or args.reviewer:
                parser.error('--demo must remain offline; use --coder-backend for model experiments')
            command = [sys.executable, '-B', str(SOURCE / 'examples' / 'demo_coder.py')]
        elif args.coder_backend:
            command = backend_command(args.coder_backend, 'coder', args.coder_model,
                                      args.base_url, args.api_key_env, args.timeout, codex_isolated=args.codex_isolated,
                                      max_output_tokens=args.provider_output_tokens,
                                      thinking=args.deepseek_thinking)
        else:
            command = shlex.split(args.coder)
    settings = {}
    if args.action == 'run':
        settings = {name: getattr(args, name) for name in (
            'trials', 'noise_floor', 'edit_budget', 'min_edit_budget', 'decay',
            'stagnation', 'prune_every', 'max_calls', 'max_tokens', 'max_cost', 'worktrees', 'cache_baseline',
            'regression_tolerance', 'cost_band', 'max_call_tokens', 'max_prompt_chars', 'max_output_bytes')}
        settings['reviewer'] = (backend_command(args.reviewer_backend, 'reviewer', args.reviewer_model,
                                                args.base_url, args.api_key_env, args.timeout, codex_isolated=args.codex_isolated,
                                                max_output_tokens=args.provider_output_tokens if args.reviewer_backend == 'deepseek' else None,
                                                thinking=args.deepseek_thinking if args.reviewer_backend == 'deepseek' else None)
                                if args.reviewer_backend else
                                shlex.split(args.reviewer) if args.reviewer else None)
    if args.action == 'solve':
        settings = {name: getattr(args, name) for name in ('max_calls', 'max_tokens', 'max_cost')}
    if args.action in ('validate-policy', 'evaluate', 'ablate'):
        settings = {name: getattr(args, name) for name in (
            'max_calls', 'max_tokens', 'max_cost', 'max_call_tokens', 'max_prompt_chars', 'max_output_bytes')}
    task_command = None
    if getattr(args,'analysis_backend',None):
        settings['semantic_reviewer'] = backend_command(args.analysis_backend,'task',args.analysis_model,
            args.base_url,args.api_key_env,getattr(args,'timeout',300),codex_isolated=args.codex_isolated,
            max_output_tokens=2048 if args.analysis_backend=='deepseek' else None,
            thinking='disabled' if args.analysis_backend=='deepseek' else None)
    if getattr(args, 'task_backend', None):
        task_command = backend_command(args.task_backend, 'task', args.task_model,
                                       args.base_url, args.api_key_env, getattr(args, 'timeout', 300), codex_isolated=args.codex_isolated,
                                       max_output_tokens=args.task_output_tokens, thinking=args.task_thinking)
    try:
        engine = Engine(args.state, command, getattr(args, "timeout", 300), task_command=task_command,
                        benchmark=getattr(args, 'benchmark', None),
                        datadesk_size=getattr(args, 'datadesk_size', None),
                        host_sandbox=getattr(args,'host_sandbox',None), **settings)
    except ValueError as error:
        parser.error(str(error))
    if args.action == "run":
        if args.demo and engine.task_command:
            parser.error('--demo cannot reuse a state configured with a task model; choose a new state')
        if engine.benchmark == 'datadesk' and (args.demo or not engine.task_command):
            parser.error('datadesk requires --task-backend or a saved task model; --demo is not a DataDesk model')
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
        if args.action == 'validate':
            result = engine.validate_versions(args.seeds)
        elif args.action == 'validate-policy':
            result = engine.validate_policy()
        elif args.action == 'ablate':
            result = engine.ablate(json.loads(Path(args.specs).read_text()), args.seeds, args.seed,args.confirm_seeds)
        elif args.action == 'evaluate':
            result = {'active':state['active'], 'evaluation':engine.score(
                engine.version(state['active']), args.seed, heldout=args.split == 'heldout')}
            from .engine import atomic_json
            atomic_json(engine.root / 'evaluation.json', result)
        elif args.action == "heldout":
            # Heldout results are not added to the proposal history.
            result = {"active": state["active"], "evaluation": engine.score(
                engine.version(state["active"]), seed=982451653, heldout=True)}
            from .engine import atomic_json
            atomic_json(engine.root / "heldout.json", result)
        else:
            result = {**state, "history": engine.recent_history()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
