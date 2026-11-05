# RSI

一个把任务执行 Agent 与自我改进策略一起版本化的软件自我改进原型。

沿用 `agent-rsi` 的失败反馈、冻结评测、候选隔离、复评和保留最佳版本思路；
沿用 `agentrsi-demo` 中「执行逻辑与评测逻辑分离」的设计。关键新增点是：
`evolution/policy.py` 也允许修改，新版本通过验证后，由它产生下一轮的改进提示词。

## 快速体验

只需 Python ≥ 3.10，运行与测试没有第三方依赖。以下命令在本项目目录执行：

请确认 `python3` 指向可用的 Python ≥ 3.10；若使用 Conda，也可以用环境中的
`python` 替换下文的 `python3`。

```bash
python3 -m kernel run --demo --rounds 3
python3 -m kernel status
python3 -m kernel solve '{"operation":"sum","observation":"```json\n{\"values\":[100,-7,9]}\n```"}'
python3 -m kernel heldout
```

离线演示会发生两次预先编写的修改：第一轮修复 Agent 的 JSON 代码块解析；第二轮
改进策略加入「先读测试再修改」的要求；第三轮从新策略生成提示词，再因没有新修改而拒绝。
演示用于验证进程切换、策略接管、独立评分与版本保留，**不是模型自主发现改进的证据**。
开始真实实验时请使用独立状态目录，不要继续使用演示历史。

## 真实编码模型

本机已安装并登录 Codex 时，可以运行：

```bash
python3 -m kernel --state .rsi-live run --rounds 2 \
  --coder 'codex exec --skip-git-repo-check --ephemeral -s workspace-write -'
```

编码命令通过 stdin 接收当前版本生成的提示词，在仓库外的临时副本内修改文件。
命令按参数列表执行，不经过 shell；可替换成其他支持 stdin 和当前工作目录的编码工具。
模型调用使用工具自己的登录配置。默认任务执行 Agent 是确定性 Python 数据处理器；也可显式选择 DeepSeek/Codex 执行任务，
由外部编码模型生成改动。完整 DataDesk CSV/SQL、多轮工具调用尚未迁移。

默认每轮最多调用编码工具 9 次：两个阶段各进行 2 次基线与候选策略探针，加一次候选修改；
可选独立审查另加一次调用。默认每次超时 900 秒；`--timeout` 可调整。调用失败时保留当前版本，
基础设施错误允许重新运行，但尚未继承 agent-rsi 的细粒度 trial 缓存与退避重试。

## 运行机制

1. 固定内核读取当前版本，并使用新 Python 进程加载其执行逻辑和进化策略。
2. 测试执行能力；让该版本的策略指导编码工具修复独立练习项目，测试改进能力。
3. 当前策略读取失败记录和历史，为自身副本生成修改指令。
4. 副本只包含 `agent/` 和 `evolution/`，允许修改这两部分。
5. 固定评测器独立计算任务得分和代码修复探针得分。
6. 两项能力都不退步，且至少一项超过噪声门槛，才进入复评；定期剪枝轮允许以源码净减少替代分数提高。
7. 用新的任务输入复评，再次通过后发布源码哈希版本并原子更新当前版本指针。
8. 下一轮重新加载已发布版本，使用更新后的策略；健康检查失败则退回上一版本。

默认 basic 修复探针仍只有一个求和函数练习。可通过 `run --benchmark advanced` 显式选择
扩展套件：任务覆盖求和、平均数、计数、条件求和、加权平均与分组聚合；修复探针按 trial
轮换求和修复、工具 JSON 恢复和双文件协作修复。每次 trial 使用其中一个探针，不能把
单轮结果当作整个修复集的成绩。套件保存在 runtime.json，已有状态不能更换套件。
扩展套件仍是小规模确定性基准，不能证明广泛的改进能力。留出任务使用独立随机种子，仍属于同类任务；
`heldout` 显式运行，其结果不会写入提案历史，也不会参与选择。
随机模型下还需要重复 trial、统计噪声处理、多种修复任务和独立审查才能可靠评估能力增长。

## 修改边界

| 路径 | 作用 | 可自动修改 |
|---|---|---|
| `agent/` | 执行任务、处理输出 | 是 |
| `evolution/` | 形成下一轮改进策略 | 是 |
| `kernel/` | 评分、范围校验、版本发布与回退 | 否 |
| `tests/` | 系统生命周期测试 | 否 |
| `examples/demo_coder.py` | 离线预设修改器 | 否 |

使用源码快照而非候选 Git 分支发布版本：`.rsi/versions/<sha256>/` 包含当时的完整可变源码。
原始开发源码不会被自动覆盖，`solve` 总是加载状态指针选择的版本。你修改开发源码后，
应使用新的 `--state` 初始化实验。状态目录记录历史、每轮证据、编码日志、候选源码及报告。
进程锁保证单实例写入，状态写入采用原子替换；崩溃前未发布的轮次可以重试。

这里的隔离是工作目录与子进程隔离，**不是安全沙箱**。候选 Python 代码和外部编码工具
仍可能通过绝对路径访问宿主机。固定内核、评测文件与状态在逻辑上不可变，但宿主机权限
尚未强制实现；不应把不可信代码或对抗性模型放到有敏感数据的宿主环境中运行。
底层模型权重和验证内核不参与进化，编码执行器目前仍是外部工具。

## 控制与验证

```bash
python3 -m kernel stop       # 当前轮结束后停止
python3 -m kernel resume     # 清除标记，然后可再次 run
python3 -m unittest discover -s tests -v
```

测试覆盖两代真实源码发布、第三轮新策略接管、重启续跑、坏版本回退、越界与软链接拒绝、
任务崩溃计零分、拒绝退步与平分版本、单写锁、停止标记和编码命令失败。

当前交付的是可运行的自我改进软件骨架。真实模型能否持续提出有效修改，需要单独实验，
不能由离线演示推断。后续应扩充真实任务、独立模型审查、统计复评、调用成本约束和容器隔离。

## 正则化搜索与证据协议

### 可变策略、成本选择与调用限制（2026-11-05）

`evolution/policy.py` 现在可提供 `analyze(evidence)`，返回 `failure_ids`、`target` 和
`hypothesis`。当前版本的策略先选择失败项和改进方向，内核验证其引用与目标，随后生成
修改提案；提案必须引用策略选中的失败项，并修改目标组件。停滞时固定探索目标优先。
旧版本没有 analyze 时仍可运行。剪枝轮的初始策略会选择维护目标。
这是可执行的独立诊断阶段，尚不能证明语义归因正确，也没有独立 LLM 分析方。

普通轮现在可接受同等能力下明显更省 token 的版本。每个 trial 的任务执行与修复探针用量
保存在 `.cost.json`，两阶段都需通过成本比较；缺失、零用量或不充分的报告不能作为降本证据。
`--cost-band` 默认 0.05。`--regression-tolerance` 默认 0，保持逐 trial 不退步；显式设为
正数时，使用配对差值的正态近似下界控制允许退步，并仍要求能力提升或成本收益。
该小样本规则不是严格统计保证，成本也是工具报告的 token 代理，不是美元账单。

新增可选限制：

- `--max-prompt-chars`：启动前限制提示字符数。
- `--max-output-bytes`：运行中限制子进程 stdout/stderr 总字节。
- `--max-call-tokens`：检查调用已报告 token，收到超过阈值的用量事件时终止进程组；
  未报告用量则拒绝。若后端缓存事件到调用结束，检查也只能在结束时发生。
- `--provider-output-tokens`：向 DeepSeek 服务端传递输出上限；不约束输入费用与总账单。
- `--deepseek-thinking disabled`：显式关闭 DeepSeek 思考模式，不改变全局配置；默认不覆盖服务端设置。

DeepSeek 返回 `finish_reason=length` 时明确报告截断、保留 usage，不应用不完整补丁。
参数依据 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/) 及
[Chat Completions 协议](https://api-docs.deepseek.com/api/create-chat-completion/)。
超时或超限会清理本次子进程组，但服务端已发生的费用无法由本地进程撤销。

`python3 -m kernel --state EXPERIMENT validate --seeds 5` 比较初始与当前版本的新输入表现；
结果保存为 validation.json，不反馈给搜索。默认仍是原任务类型，不表示跨任务类型泛化，
也不衡量改进策略在陌生修复项目上的泛化。

新增 `validate-policy`，在不参与搜索评测的 CSV 解析、区间合并项目上，分别使用初始与当前
策略产生修复提示词，交给同一个编码后端执行。共四次调用，计入状态目录的累计预算；
`--max-calls` 等限制与 run 相同。结果存放在 `policy-validation.json` 和独立审计目录，
不写入搜索历史，不决定是否启用版本。例如（真实模型调用会产生费用）：

```bash
python3 -m kernel --state EXPERIMENT validate-policy \
  --coder-backend deepseek --coder-model deepseek-flash \
  --deepseek-thinking disabled --provider-output-tokens 4096 --timeout 120
```

这些是仓库内固定的诊断项目，不是外部盲测；每个版本每个项目只采样一次，不能据此证明
普遍的自我改进能力。本次只用离线脚本验证评分、预算及历史隔离，未执行新项目的真实模型实验。
候选策略现在还需通过基础健康检查和本轮真实证据的 analyze 接口检查，格式错误或引用
不存在失败项的策略会在启用前被拒绝。旧版缺少 analyze 仍保持兼容。
验证记录见 [策略接管与留出评测测试](docs/experiments/2026-11-05-policy-validation.md)。
本次新增 9 项测试，全量 84 项全部通过。

2026-11-05 真实模型实验完成了一次经审查与复评的发布，下一轮使用新策略继续运行。
任务得分从 33.3% 提升到 100%，五个新种子共 60 题中 40 题改善、0 题退步。
第二轮无足够收益被拒绝。详见 [真实实验与验证记录](docs/experiments/2026-11-05-live-improvement.md)。
已通过版本的源码快照随实验文档保存；开发目录仍保留初始 Agent，便于复现实验。
本批最终全量 75 项测试全部通过；真实模型实验、新输入验证与离线接口测试分别记录。

### 2026-11-05 评测与成本改进

`--benchmark advanced` 提供六类任务和三类轮换修复探针；默认保留 basic，以兼容已有实验
与离线 demo。新实验可显式选择 advanced，不会自动迁移旧状态。

`--cache-baseline` 可选择缓存已完成的初评基线 trial，用于中断续跑；键包含版本、套件、
输入种子、编码与任务命令、超时、Python、内核代码和环境摘要。环境值不写入缓存。
确认评测和候选评测始终重新执行。缓存不会改变账本或抹去历史用量；未显式固定的远端
模型行为、外部命令文件和服务端模型变化无法完整指纹化，严格独立实验应关闭该选项。

候选的全部 Python 文件先做语法检查，基本健康检查前移到独立模型审查之前。
初始策略产生的重复 Evidence 会被合并，固定规则仍由内核追加。

候选被拒绝时，在临时目录清理前归档 `rejected-source/`、`rejected-changes.patch` 和
`rejected-archive.json`。包括编码命令超限、超时、提案无效、审查否决及评测失败；
`result.json` 的 stage 标明失败阶段。归档仅保存可变目录和提案，不跟随软链接，
单文件最多 200KB、合计最多 2MB，跳过项和读取错误会显式列出，不能把不完整归档当作完整补丁。
归档没有通过审查的源码也不表示该候选有效。基线阶段尚未产生候选时仍只保留基线日志。

分批验证：第一批 47 项、第二批 52 项、第三批 56 项测试全部通过；basic 三轮和
advanced 一轮 CLI 组合检查通过。详见 [2026-11-05 测试记录](docs/experiments/2026-11-05-optimization-tests.md)。
这些是离线接口与生命周期验证，未进行真实付费模型改进实验，也未改变下方上游清单的能力边界。

现在每轮要求编码工具先分析证据，再随补丁生成 `.rsi-proposal.json`：

```json
{
  "analysis": "先解释失败证据、历史尝试和本次选择",
  "changes": [{
    "files": ["agent/solver.py"],
    "failure_id": "task-1",
    "cause": "从本轮 Evidence.failures 中选择实际失败 ID，解释原因",
    "expected_effect": "描述预期行为变化",
    "validation": "描述验证方式"
  }]
}
```

内核提取并保存清单，检查每个改动文件都有失败归因、引用的失败 ID 存在。
这是文件级证据覆盖检查，不能自动证明文字解释和代码的语义因果关系。
`complexity` 是固定维护目标，可用于提出精简补丁；剪枝仍必须通过行为评测。
旧实验中的策略会收到内核追加的同一协议，新实验策略也明确要求先分析证据。

- **历史反馈**：提案收到完整历史记录、已有补丁和 trace 诊断；不再仅取最近 8 条。
  提示词超过 100KB 时停止并提示使用独立实验状态，不会静默截断。报告仍只展示近期记录。
- **退火预算**：新增行数加删除行数默认不超过 `max(20, int(200 * .9 ** round))`。
  计算使用源码行差异；预算和探索限制由固定内核执行。
- **停滞探索**：连续 2 个候选未接受后，强制修改尝试次数最少的组件（`agent` 或
  `evolution`，同次数时先选 `agent`）。计数覆盖成功和失败的已记录修改，重启后保留。
- **评测前审查**：默认先检查归因覆盖、修改预算、探索与剪枝要求，再运行候选评测。
  `--reviewer '命令 参数'` 可增加独立审查工具，stdin 是包含证据、提案、diff 的 JSON，
  stdout 必须有且只有一条含 `approved` 布尔值及可选 `reason` 的 JSON 结论。
  审查在候选临时副本中执行，检测到修改源码则拒绝；仍不是宿主安全沙箱。
- **噪声处理**：初评和确认各默认做 2 次配对 trial，每次重跑任务及策略修复探针。
  每项指标在每个配对 trial 都不得退步，至少一项的平均提升必须严格大于
  `max(.01, 1.96 * 配对差值标准差 / sqrt(trials))`。这是小样本启发式，不能当作
  严格的统计显著性保证；固定修复练习仍只有一个，增加 trial 不会增加任务多样性。
- **剪枝**：每第 5 轮要求候选净删除源码行；两阶段配对评测都无退步时允许接受，
  即使任务分数不增加。行数减少是复杂度代理，不能证明某个机制普遍无用；删除文档也
  会影响该代理，应结合独立审查。普通轮仍要求能力提升，不接受平分候选。
- **worktree**：`--worktrees` 在仓库外建立仅含可变源码的临时 Git 仓库，每个候选
  使用一个 detached worktree，结束后清理。需本机 Git；不会将固定内核放入候选仓库。
  默认仍用临时源码副本，发布仍是哈希快照。

### trace 与成本适配

外部工具或包装器可向 stdout 输出 JSONL 事件：

```json
{"type":"tool_call","name":"read_file","arguments":{"path":"agent/solver.py"}}
{"type":"tool_error","message":"示例错误"}
{"type":"give_up","message":"示例放弃"}
{"type":"loop","message":"适配器检测到循环"}
{"type":"usage","input_tokens":1200,"output_tokens":300,"cost_usd":0.01}
```

同一次调用出现 3 个相同名称和参数的 `tool_call` 会被标记为重复调用；这只是循环启发式，
可能误判合理重试。显式 `tool_error`、`give_up`、`loop` 或重复调用会拒绝本次调用结果；在策略修复探针中
计零分，基线诊断进入本轮失败证据，供后续提案修复。
普通文本输出不能识别语义上的放弃或死循环，只能通过进程超时和退出码诊断。
内置 DeepSeek/Codex 后端已转换原生 token 用量；其他通用 `--coder` / `--reviewer`
命令仍需包装器适配。原生轨迹的语义诊断仍未完整适配。
每次调用应报告一条**本次调用累计** usage，若有多条则以最后有效值为准。
审查工具的 usage 也采用该协议。

`usage.json` 保存状态目录累计调用数、已报告 tokens/美元和未知用量次数。
`--max-calls`、`--max-tokens`、`--max-cost` 可设置上限；每次外部调用前先持久化调用
预留，失败或重启不会抹去消耗。配置 token/费用上限时，缺失对应报告会停止，已有未知
用量也会阻止后续调用；恢复需核对并补全账本，或使用新实验状态。
阈值只能阻止后续调用及超限候选激活，不能中止正在执行的调用产生的费用；真正的硬上限
需编码服务自身支持。报告是工具提供的数据，不是独立计费核验。每次 run 都需传入所需
预算和搜索参数，账本累计但这些配置不持久化。

默认一轮最多调用编码工具 **9 次**（`4 * trials + 1`），配置独立审查后再增加 1 次。
离线 demo 报告零 token、零费用，仍是预设补丁。示例：

```bash
python3 -m kernel --state .rsi-regularized run --demo --rounds 3 \
  --trials 2 --max-calls 30 --worktrees
```

所有每轮证据、提案、diff、审查结论、trial 分数与 `.trace.json` 诊断保存在 `rounds/`。
基线阶段的基础设施故障（超时、非零退出、预算问题）会保留日志并终止运行，当前轮号
不推进；显式行为 trace 失败计零分并反馈。候选修改阶段故障会记录拒绝结果。

## 上游机制落地清单

本清单以用户提供的机制描述为验收目标，不表示已经逐行核验或完整移植上游仓库。
**维护规则：每实现并验证一项，就更新本表的勾选、实现说明和测试；部分实现保持未勾选。**
修改内核后必须运行 `python -m unittest discover -s tests -v`；不得用脚本 demo 分数证明自主发现。

### google-research/rrsi

| 完成 | 机制 | 当前实现与剩余工作 | 实现位置 / 验证 |
|---|---|---|---|
| [x] | 退火编辑预算 | 每轮按 decay 衰减新增加删除行预算，保留最小预算，超限拒绝 | `kernel/search.py:SearchSettings.budget`、`review_proposal`；编辑超限测试 |
| [x] | 带完整编辑历史的提案方 | 传入所有已记录轮次、diff 与 trace；超出提示词上限则显式停止 | `kernel/engine.py:search_evidence`；超过 8 轮历史测试 |
| [x] | 停滞时强制探索未尝试组件 | 优先选择尝试最少的组件，未尝试组件计数为 0；所有组件都试过时选择最少者 | `search_evidence`、`review_proposal`；探索目标及强制执行测试 |
| [x] | 评估前先过审查方 | 默认固定规则审查；可选 `--reviewer` 独立外部审查，有否决权 | `kernel/engine.py:round`；审查否决测试 |
| [x] | 噪声带下限 | 重复配对 trial，提升需超过 noise_floor 和标准误差门槛，逐 trial 不得退步 | `kernel/search.py:confirmed_gain`；小提升、噪声、退步测试 |
| [x] | token 成本规则 | 累计预算及未知用量阻断；另在初评和确认比较每 trial 已报告 token，明显降本且能力不退步可接受；不等于美元账单或服务端总费用硬限额 | `Engine.code`、`accept_trials`；预算、未知用量与成本选择测试 |
| [ ] | 剪枝不再有用的机制 | **基础版**：周期性要求净删行，无退步时可接受；尚无逐机制消融实验，不能判断机制是否无用 | `review_proposal`、`confirmed_gain`；等分剪枝生命周期测试 |
| [ ] | 分析方先读证据 | **部分实现**：可变 analyze 先执行并选择失败与方向，固定层验证、提案需遵循；候选启用前检查其策略接口及本轮证据引用；尚无独立 LLM 分析方及语义阅读验证 | `evolution/policy.py`、worker analyze、`tests/test_policy_validation.py` |
| [x] | 每个候选一个 git worktree | 可选 `--worktrees`，临时仓库仅含可变源码，候选结束清理，仍以哈希快照发布 | `kernel/search.py:candidate_workspace`；worktree 生命周期测试 |

### kayba-ai/recursive-improve

| 完成 | 机制 | 当前实现与剩余工作 | 实现位置 / 验证 |
|---|---|---|---|
| [ ] | trace 失败检测器：死循环、放弃、工具报错 | **基础版**：支持显式 JSONL 事件、重复调用、进程超时和退出码；已读取 Codex 原生事件并适配用量；尚无完整原生轨迹诊断及语义放弃检测 | `kernel/search.py:inspect_trace`、`Engine.code`；事件诊断与超时测试 |
| [x] | 保留或回滚的棘轮 | 普通轮无退步且有有效提升；剪枝轮允许无退步且净删行；坏版本健康检查回滚 | `kernel/engine.py:round`、`recover`；原有生命周期及剪枝测试 |
| [x] | 直接用 Claude Code / Codex 当改代码的 Agent | `--coder` 通用接口及 `--coder-backend codex/deepseek` 内置后端；已适配原生 token 用量 | `kernel/cli.py`、`Engine.code`；离线命令进程测试，未做付费模型实验 |
| [ ] | 每个改动对应一个失败模式 | **基础版**：清单按文件关联真实 failure_id，检查全文件覆盖；尚无逐逻辑改动的语义因果核验 | `kernel/search.py:review_proposal`；缺失清单拒绝测试 |

本轮验证（2026-11-02）：使用 Python 3.12 执行
`python -m unittest discover -s tests -v`，27 项测试通过；独立临时状态的 CLI
`--demo --rounds 3 --trials 2 --max-calls 30 --worktrees` 验证两次接受、第三次拒绝，
累计 21 次脚本调用、零模型用量。该结果只验证机制与生命周期。

## DeepSeek 与 Codex 后端

从两个本地上游示例迁移的接入方式现在位于固定层 `kernel/backends.py`：
DeepSeek 使用标准库 `urllib` 调用 Chat Completions，Codex 使用本机已登录 CLI。
三个角色独立配置，可按上游的分工组合：DeepSeek 执行任务、Codex 修改代码、DeepSeek 审查。
也可以交换后端。没有配置任务模型时，继续使用离线确定性求解器。

```bash
# 密钥放在 shell 的 DEEPSEEK_API_KEY 中；不要写入命令参数、源码或状态文件。
# 模型名显式填写你账户可用的名称；Codex 模型留空则沿用本机配置。
python3 -m kernel --state .rsi-models run --rounds 1 \
  --coder-backend codex \
  --task-backend deepseek --task-model YOUR_TASK_MODEL \
  --reviewer-backend deepseek --reviewer-model YOUR_REVIEW_MODEL \
  --trials 2 --max-calls 150 --max-tokens 200000

# 单独验证任务执行（会调用模型；适合先做一次小规模连通性检查）
python3 -m kernel --state .rsi-model-smoke solve \
  '{"operation":"sum","observation":"{\"values\":[2,3]}"}' \
  --task-backend deepseek --task-model YOUR_TASK_MODEL --max-calls 1
```

`--base-url` 和 `--api-key-env` 可覆盖 DeepSeek 接口与密钥环境变量名。
兼容已有 `DATADESK_BASE_URL`、`DATADESK_API_KEY_ENV`、`DATADESK_MODEL_NAME`，
也支持优先级更高的 `RSI_BASE_URL`、`RSI_API_KEY_ENV`、`RSI_MODEL_NAME`。
`DATADESK_MODEL` 不会自动启用模型，以免离线运行意外产生费用。
DeepSeek 模型名没有硬编码默认值，必须由参数或上述环境变量提供。

- 任务后端命令及显式模型参数保存在 `runtime.json`，`solve`、`heldout`、后续 `run`
  自动复用；健康检查不调用模型。更换任务配置必须用新状态目录，避免混用评分基准。
  环境变量选择在构造命令时解析；若 Codex 模型名未指定，本机配置的变化仍可能改变模型，
  真实对比实验应显式填写 `--task-model`。预算和搜索参数仍需每次运行显式传入。
- 可变 Agent 提供 `build_messages(task) -> {system, prompt}` 和 `solve(task, llm=None)`。
  固定层生成一次模型响应，再让候选解析。当前每任务只支持一次无状态调用；还没有上游
  DataDesk 那种多轮工具 Agent。离线 `solve(task)` 接口仍可使用。
- DeepSeek 编码后端以结构化 JSON 返回完整文件内容及提案清单，再由固定层校验路径并写入；
  Codex 编码后端直接在候选目录修改。两者都要通过现有范围、提案和评测检查。
- API 限流、认证失败、超时、CLI 启动失败会作为模型基础设施错误传播，不计入任务零分。
  任务模型的响应无法被 Agent 正确解析则计为任务失败。候选阶段的模型故障保留旧版本。
- 所有任务模型、编码与审查调用共用 `usage.json`。DeepSeek 使用原生 prompt/completion
  tokens，Codex 使用 `turn.completed.usage`，不重复计算已包含的缓存 token。
  这两个接口均未提供可靠美元账单，因此费用保持未知；直接设置 `--max-cost` 会触发未知
  用量阻断，应先用 `--max-calls` / `--max-tokens` 控制调用量。
- 启用任务模型后，一轮不再只有 9 次调用。默认两次 trial、12 题和两阶段复评，最多执行
  96 次任务模型调用，再加 9 次编码及探针调用、可选审查一次。日志保存在 `model-calls/`。
  原生 token 报告缺失时保留未知状态，不伪造为零。
- `--demo` 拒绝显式模型后端和外部审查命令，也拒绝复用启用了任务模型的状态目录。

后端协议参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、
[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)。

后端接入进度：

- [x] DeepSeek：任务执行、结构化代码修改、审查，原生 token 统计。
- [x] Codex：任务执行、目录内代码修改、审查，原生 token 统计。
- [x] 本地模拟 HTTP/CLI 验证，包括限流传播、预算、配置恢复、路径越界和密钥不落盘。
- [x] 真实 DeepSeek 连通性：`deepseek-flash` 单任务返回正确答案，报告 104 tokens。
- [x] 真实 Codex 连通性：补齐系统代理桥接并开启 `--codex-isolated` 后，项目适配器单任务返回 5，耗时 26.64 秒，报告 22,957 tokens。
- [x] 第一轮真实模型提案尝试：基线双指标满分，候选因 token 阈值超限被拒绝，未发布新版本。
- [ ] 真实多轮改进与策略接管验证：部分完成；2026-11-05 一轮发布成功、下一轮使用新策略后因收益不足被拒绝。尚无持续多轮能力增长证据。
- [ ] 完整 DataDesk CSV/SQL、多轮工具执行与任务集迁移。

后端本轮验证（2026-11-02）：Python 3.12 下全部 38 项测试通过，其中 11 项验证模型后端，
均使用本地 HTTP 模拟服务或假 CLI，未消耗真实模型额度。本机 Codex 已安装并登录；
当前进程未设置 `DEEPSEEK_API_KEY` 或模型名环境变量。该记录描述接入时的环境；随后用户提供了密钥，单次连通性验证进度见上表。

真实连通性验证（2026-11-02）：DeepSeek Flash 单次调用返回 5，耗时 1.85 秒，
报告 104 tokens；美元账单未报告。Codex 适配器请求及独立 CLI 复测均超时，
不能将登录成功视为模型调用成功，也不能把未知用量记为实际零消耗。密钥仅在测试进程中使用。

### Codex 超时排查与隔离运行

本机排查发现，系统代理已启用，但 CLI 进程没有相应代理环境变量；直连 ChatGPT 接口
超时，经系统代理可以收到响应。适配器现在通过标准库发现系统代理，仅向 Codex
子进程传递 HTTP/HTTPS/ALL_PROXY 及适用的 NO_PROXY。已有显式代理设置（包括空值）
优先保留，父进程环境与全局配置不变。

可用 `--codex-isolated` 让本次 Codex 调用带上 `--ignore-user-config`，保留登录认证，
排除用户配置中外部 MCP 等启动项的影响。该选项同时适用于本次选择的任务、编码和审查
Codex 后端；默认关闭。隔离也会跳过用户配置中的模型、provider 和 profile，因此依赖
这些设置的用户应保留默认模式，或显式指定兼容的模型。任务命令中的隔离选项随状态保存。

```bash
python3 -m kernel --state .rsi-codex-smoke solve \
  '{"operation":"sum","observation":"{\"values\":[2,3]}"}' \
  --task-backend codex --codex-isolated --max-calls 1
```

Codex 的 `turn.completed` 事件与整个 CLI 退出并不总是同时发生。本次对照中，约 33 秒
收到结果、约 51 秒进程退出。适配器仍要求进程正常完成，不会把中断中的候选当作成功；
即使超时，也保留此前收到的原生事件和已报告 token，避免错误地丢弃用量。

Codex 修复验证（2026-11-02）：真实任务调用通过（26.64 秒，22,957 tokens），
未修改父进程环境及全局 Codex 配置。该结果验证连通性，不代表多轮自我改进实验通过；
早期的 90/45 秒超时记录保留为排查历史。
本次修复后，Python 3.12 下全部 42 项离线测试通过，覆盖系统代理传递、显式代理优先、
隔离参数转发和超时后保留已报告 token；`git diff --check` 通过。

## 第一轮真实模型提案实验

2026-11-03 已执行一轮真实模型实验，见[完整记录](docs/experiments/2026-11-03-live-round-1.md)。
两次基线都是任务 1.0 / 修复 1.0；27 次调用累计报告 513,539 tokens，最后一次 Codex
调用超过 400,000 的调用间阈值，候选被拒绝，未进入独立审查和候选复评，旧版本保留。
美元费用未知；这轮没有验证自主能力增长。后续需先处理基准满分和单次调用开销问题。
