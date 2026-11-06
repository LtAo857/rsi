# RSI

RSI 是一个能够执行任务、修改自身代码，并通过独立测试决定是否保留修改的软件自我改进系统。

`agent/` 负责完成任务，`evolution/` 负责分析失败和指导下一次修改。两部分共同版本化：新版本通过验收后，接管后续任务及下一轮改进。固定内核负责调度、评分、版本发布和回退。

当前用于研究 Agent 的任务能力、改进策略和调用成本如何随着代码修改而变化。改进发生在软件和策略层，底层模型权重保持固定。

## 核心功能

| 功能 | 当前行为 |
|---|---|
| 任务执行 | 离线数据处理，或 DeepSeek / Codex 任务模型 |
| CSV / SQL 数据分析 | 文件与表结构查看、CSV 分页读取、只读 SQLite 查询 |
| 多轮工具执行 | 根据工具结果继续查询、修正错误，再提交答案 |
| 自我改进 | 根据失败证据修改任务代码，也能修改下一轮使用的进化策略 |
| 候选验收 | 检查范围、预算、失败归因和策略接口；可选独立模型审查 |
| 配对评测与复评 | 比较新旧版本的任务能力和代码修复能力，再用新输入确认 |
| 成本选择 | 同等能力下，允许保留已报告 token 消耗明显下降的版本 |
| 版本管理 | 按源码哈希保存版本，通过后启用；坏版本回退，失败修改归档 |
| 运行控制 | 调用预算、超时、输入输出限制、停止与恢复 |
| 独立验证 | 比较初始与当前版本，留出结果不参与版本选择 |

## 运行链路

```text
当前版本执行任务
    ↓
收集失败证据、修改历史和调用用量
    ↓
当前进化策略选择问题与改进方向
    ↓
编码后端修改独立候选副本
    ↓
范围、提案、语法、健康与策略接口检查
    ↓
可选独立模型审查
    ↓
新旧版本配对评测 → 新输入复评
    ↓
通过：保存并启用新版本 / 未通过：保留旧版本
    ↓
由已启用策略继续下一轮
```

普通轮要求两项能力不退步，且至少一项有足够提升，或者满足明确的 token 降本条件。周期性剪枝轮允许在能力不退步时接受净删行修改。重复 trial 和噪声门槛用于过滤波动，小样本规则仍不能提供严格的统计保证。

## 快速体验

需要 Python ≥ 3.10，运行与测试只依赖标准库。在项目根目录执行；请确认 `python3` 指向可用的 Python，使用 Conda 时也可替换为环境中的 `python`。

```bash
python3 -m kernel --state .rsi-demo run --demo --rounds 3
python3 -m kernel --state .rsi-demo status
python3 -m kernel --state .rsi-demo solve \
  '{"operation":"sum","observation":"{\"values\":[100,-7,9]}"}'
```

离线演示执行预先编写的修改：修复 JSON 代码块解析，再调整改进提示词，随后运行新策略。它验证版本发布和策略接管，不能作为模型自主发现改进的证据。模型实验应使用新的状态目录。

## 任务套件

| 套件 | 任务 | 执行方式 |
|---|---|---|
| `basic`，默认 | 求和、平均值、计数；普通与代码块 JSON | 离线求解器，或单次任务模型调用 |
| `advanced` | 增加条件求和、加权平均、分组聚合 | 离线求解器，或单次任务模型调用 |
| `datadesk` | CSV / JSON 分析、多表查询和聚合 | 任务模型驱动的多轮工具执行 |

套件在初始化时锁定，同一状态目录不能切换。DataDesk 必须配置任务模型。

DataDesk 搜索集包含 5 类问题：客户城市查询、订单状态计数、月度收入、分类取消订单数、最高收入客户。留出集增加客户等级平均购买数量、分类利润两类问题，它们不进入搜索评测。

| DataDesk 规模 | 搜索集 | 留出集 |
|---|---|---|
| `compact`，默认 | 每类 1 题，共 5 题 | 每类 1 题，共 7 题 |
| `full` | 每类 3 题，共 15 题 | 前 5 类各 2 题、新增 2 类各 5 题，共 20 题 |

规模随状态锁定。固定生成器按种子生成每题独立的商店数据，评分答案不传入模型提示词。配对测试使用相同种子，确认阶段使用新种子。当前覆盖 7 类合成问题，通过它们不等于具备任意数据分析能力。

### CSV / SQL 多轮执行

链路为：问题 → Agent 选择工具 → 内核执行 → 返回结果或错误 → Agent 继续决策 → 最终答案。

| 工具 | 作用 |
|---|---|
| `list_files` | 列出当前数据目录的文件 |
| `read_file` | 按字符偏移分段读取文本 |
| `read_csv` | 正确解析引号与换行，按记录分页，标明下一页 |
| `schema` | 查看自动导入的 SQLite 表与字段 |
| `sql` | 执行只读查询，支持关联、过滤、分组和聚合 |

CSV 和 JSON 对象数组按文件名导入内存 SQLite，列类型为 TEXT，数值排序等场景应显式 CAST。SQL 禁止写入、ATTACH 和 PRAGMA，限制函数、查询工作量及结果大小；最多返回 100 行并标明截断。CSV 每页最多 100 行。大结果应缩小分页或使用聚合。

工具错误会反馈给 Agent，允许修正。每个任务最多 10 个动作（含最终答案），还有上下文上限和重复动作检查。工具和评分固定，Agent 可以改进提示、动作解析及决策方式。

### 先运行一次数据分析评测

以下命令会调用真实模型并产生费用。模型名替换为账户可用名称；密钥放在环境变量 `DEEPSEEK_API_KEY`，不要写入源码或命令参数。

```bash
python3 -m kernel --state .rsi-datadesk evaluate \
  --benchmark datadesk --datadesk-size compact \
  --task-backend deepseek --task-model YOUR_TASK_MODEL \
  --task-thinking disabled --task-output-tokens 1024 \
  --split search --seed 101 --timeout 120 \
  --max-calls 50 --max-tokens 30000

# 复用任务配置，评测留出集；预算按状态目录累计
python3 -m kernel --state .rsi-datadesk evaluate \
  --split heldout --seed 202 --max-calls 120 --max-tokens 70000
```

使用 `--datadesk-size full` 时请初始化另一个状态目录，并按更大的任务量配置预算。`evaluate` 保存 `evaluation.json`，不写入搜索历史；该文件每次覆盖，需保留多个报告时请另行保存。

## 自动改进

下面复用已有任务配置，另选编码和审查模型：

```bash
python3 -m kernel --state .rsi-datadesk run --rounds 1 \
  --coder-backend deepseek --coder-model YOUR_CODER_MODEL \
  --reviewer-backend deepseek --reviewer-model YOUR_REVIEW_MODEL \
  --deepseek-thinking disabled --provider-output-tokens 4096 \
  --trials 2 --worktrees --timeout 120 \
  --max-calls 300 --max-tokens 200000 --max-call-tokens 24000
```

也可选择 `--coder-backend codex` 或 `--reviewer-backend codex`，使用本机已登录的 Codex CLI。`--coder '命令 参数'` 与 `--reviewer '命令 参数'` 支持自定义外部工具，通过 stdin 接收提示，命令不经 shell。`--codex-isolated` 可选忽略用户配置，默认保留本机配置，不修改全局设置。

DataDesk 一题可能产生多次模型调用。默认 2 次 trial、初评和复评全部执行时，compact 一轮最多有 400 次任务动作调用，full 最多 1200 次；另有最多 9 次编码和修复探针调用，可选审查 1 次。示例预算可能提前终止，应先做小规模评测。

任务模型命令、套件和规模保存在 `runtime.json`，后续运行复用。更换任务配置应新建状态目录；预算与搜索参数每次显式传入。`--task-output-tokens` / `--task-thinking` 控制 DeepSeek 任务动作，`--provider-output-tokens` / `--deepseek-thinking` 控制 DeepSeek 编码与审查。`--base-url` 和 `--api-key-env` 可覆盖地址与密钥环境变量名。

### 策略与修改边界

`evolution/policy.py` 用 `analyze(evidence)` 选择失败 ID、目标组件及假设，再用 `build_prompt(evidence, goal)` 指导编码。缺少 analyze 的旧版本仍兼容。候选需通过健康检查和本轮真实证据的接口检查；格式错误、引用不存在失败项的策略会被拒绝。

编码工具随修改生成 `.rsi-proposal.json`，含 `analysis` 和 `changes`。每项 changes 必须包含 `files`、`failure_id`、`cause`、`expected_effect`、`validation`，引用策略选中的失败 ID，并覆盖全部修改文件。内核还执行退火编辑预算、停滞探索与定期剪枝要求。结构和引用检查不能证明语义因果关系正确。

| 路径 | 职责 | 候选可修改 |
|---|---|---|
| `agent/` | 任务逻辑、模型提示、动作解析 | 是 |
| `evolution/` | 失败分析、改进提示与策略 | 是 |
| `kernel/` | 工具执行、评分、验收、发布与回退 | 否 |
| `tests/` | 接口、工具和生命周期测试 | 否 |

系统还让策略指导编码工具修复独立练习项目，衡量改进能力。basic 使用求和练习，advanced 和 datadesk 轮换求和、JSON 恢复与双文件修复。每个 trial 使用一个项目，单轮结果不代表整个修复集成绩。

## 验证与运行控制

```bash
python3 -m kernel --state .rsi-datadesk status
python3 -m kernel --state .rsi-datadesk heldout
python3 -m kernel --state .rsi-datadesk validate --seeds 5
python3 -m kernel --state .rsi-datadesk stop
python3 -m kernel --state .rsi-datadesk resume
```

`heldout` 评测当前版本，DataDesk 包含两类未参与搜索的问题；`validate` 在多个新种子上比较初始与当前版本的同类任务表现。它们不反馈给搜索，配置了任务模型时会产生调用。需要预算的当前版本评测可用 `evaluate --split heldout --max-calls ...`。

`validate-policy --coder-backend deepseek --coder-model YOUR_CODER_MODEL` 单独比较初始和当前策略在 CSV 解析、区间合并两个修复项目上的表现，共四次编码调用，沿用累计账本；结果在 `policy-validation.json` 和独立审计目录。这是固定项目、单次采样诊断，不能证明普遍的策略泛化能力。

`stop` 请求当前轮完成后停止；`resume` 清除标记，再次执行 run 才会继续。

| 参数 | 约束 |
|---|---|
| `--max-calls` | 状态目录累计外部调用数 |
| `--max-tokens` / `--max-cost` | 累计已报告 token / 美元，缺少报告时阻断 |
| `--max-call-tokens` | 单次已报告 token，收到超限用量后终止 |
| `--max-prompt-chars` / `--max-output-bytes` | 启动前提示字符数 / 子进程输出量 |
| `--timeout` | 外部调用超时 |

适配器报告原生 token，用量未知时不会伪造零消耗；美元账单通常未知，应优先用调用数和 token 控制实验。失败调用也保留用量。本地阈值不能撤销服务端已发生的费用，输出 token 限制也不是总费用硬上限。

可选 `--cache-baseline` 缓存已完成的初评基线，候选与确认评测仍重跑。严格独立实验应关闭缓存，远端模型和外部工具变化无法完整指纹化。

## 版本与记录

状态目录的 `versions/<sha256>/` 保存已发布源码，`state.json` 指向当前版本。开发目录种子源码不会被自动覆盖；手动修改种子后应新建状态。单写锁和原子替换避免并发写入和不完整切换。

每轮证据、轨迹、提案、diff、审查和分数在 `rounds/`；模型日志在 `model-calls/`，用量在 `usage.json`。失败候选清理前进行有大小上限的归档，跳过项显式记录。默认使用临时副本，可选 `--worktrees` 使用独立 Git worktree。

当前隔离依赖子进程、工作目录和接口检查，尚无宿主级安全沙箱。路径限制和只读 SQL 只约束工具接口，不能阻止候选 Python 或外部编码工具通过宿主权限访问其他资源。

## 功能与验证状态

每次修改同步更新本表及测试记录；部分实现保留未勾选，并注明缺口。

- [x] 执行代码与进化策略共同版本化，新策略接管下一轮。
- [x] 候选隔离、范围和策略接口检查、固定评分、可选独立模型审查。
- [x] 配对评测、新输入确认、源码快照、回退和失败归档。
- [x] 编辑预算、停滞探索、基础剪枝、token 成本选择与运行限制。
- [x] DeepSeek / Codex 任务、编码、审查接口及真实单次连通性验证。
- [x] CSV / SQL、多轮工具执行、7 类 DataDesk 任务和 compact / full 规模，已通过离线链路测试。
- [x] 新输入对比与不参与搜索的独立修复诊断。
- [x] DataDesk compact 真实模型链路：搜索基线 5/5、独立留出 7/7；一轮候选通过审查和初评，因降本复评不稳定被拒绝，未发布新版本。
- [ ] DataDesk full 真实模型评测、多种子稳定性和有效升级后的策略接管。
- [ ] 持续多轮能力增长：真实实验已有一次升级并接管下一轮，尚无持续增长证据。
- [ ] 语义归因与完整轨迹诊断：目前为结构检查、事件规则和超时检测。
- [ ] 精确识别无用机制：当前剪枝用净删行代理，尚无逐机制消融。
- [ ] 宿主级安全沙箱。

已有真实模型实验在 advanced 套件上将任务分数从 33.3% 提高到 100%；下一轮使用新策略，因收益不足拒绝候选。五个新种子共 60 题中，40 题改善、0 题退步。结果属于固定任务范围，不能推断 DataDesk 或开放任务上的效果。

验证记录：

- [真实模型改进实验](docs/experiments/2026-11-05-live-improvement.md)
- [策略接管与独立修复诊断](docs/experiments/2026-11-05-policy-validation.md)
- [CSV / SQL 多轮链路验证](docs/experiments/2026-11-05-datadesk.md)
- [DataDesk 真实模型与进化实验](docs/experiments/2026-11-06-datadesk-live.md)

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用离线脚本和本地模拟服务验证生命周期、模型适配、工具执行、预算、回退和评分。脚本提供的修改和答案用于验证机制，不代表自主学习或真实模型效果。

最近一次全量验证：2026-11-06，96 项测试全部通过；真实模型实验与离线测试分别记录。
