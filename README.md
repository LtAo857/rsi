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
| 失败诊断 | 规则诊断、固定分页题算术复核，以及可选独立模型假设 |
| 机制消融 | 单独关闭指定机制，配对评测，可选新种子确认 |
| 宿主隔离 | 可选 macOS 沙箱，限制候选 Python 的文件访问、网络和环境 |

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
| `hard` | 原 5 类 + 城市净收入 / 最高利润商品 / 复购客户数，共 8 题 | 全部 10 类各 1 题，共 10 题 |
| `pagination`，专用环境 | 活跃记录数量、数量合计，共 2 题 | 同两类问题的新数据，共 2 题 |
| `pagination_extended`，扩展分页环境 | 含单列及多列过滤（status / region / priority）的计数与求和，共 6 题 | 同六类问题的新数据，共 6 题 |

规模随状态锁定。固定生成器按种子生成每题独立的商店数据，评分答案不传入模型提示词。配对测试使用相同种子，确认阶段使用新种子。当前覆盖 7 类合成问题，通过它们不等于具备任意数据分析能力。

`pagination` 是单独的 CSV-only 分页能力测试：每题 61–79 条记录、每页最多 20 条，通常需
读取 4 页。提供 list_files、read_csv 和已读数据 aggregate，禁用 SQL、schema 和 read_file，模拟只能
通过分页接口访问数据的环境。它不改变 compact/full 的工具或成绩，需使用新状态目录。
两类题分别要求统计活跃记录数、合计活跃记录 qty；后续页保证对答案有贡献。
留出使用固定偏移种子生成新数据，仍是同类问题，不表示陌生任务泛化。

`aggregate` 只能使用当前任务已通过 read_csv 观察到的记录，且必须先读完全部页面；
支持按字段精确过滤后的 count / sum，求和使用 Decimal。它不读取未观察页面，也不返回隐藏答案。
这是固定工具的开发改动，不是 Agent 自主进化的成绩；与旧版分页工具比较时应新建状态并分开记录。

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

分页专用评测及提示消融（以下为真实模型命令，会产生费用）：

```bash
python3 -m kernel --state .rsi-pages evaluate \
  --benchmark datadesk --datadesk-size pagination \
  --task-backend deepseek --task-model YOUR_TASK_MODEL \
  --task-thinking disabled --task-output-tokens 1024 \
  --seed 101 --max-calls 25 --max-tokens 30000
python3 -m kernel --state .rsi-pages ablate examples/ablation-pagination.json \
  --seeds 2 --confirm-seeds 2 --max-calls 170 --max-tokens 180000
```

先检查轨迹是否实际出现 next_offset 及完整续页，再解读消融结果。真实分页实验已执行：
旧工具下 0/2，完整读取后仍出现算术错误；加入已读数据汇总后，两个搜索种子和一个留出种子共 6/6。
随后消融与进化基线仍出现失败，尚不能认定模型稳定或分页提示可以删除。

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

改进证据汇总全部 baseline trial 的失败，后续 trial 使用独立失败 ID。较长工具观察在提示中
明确标记省略的行数或字符数，轨迹下标保持原样；完整观察保留在逐 trial 审计文件中。

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
python3 -m kernel --state .rsi-datadesk versions
python3 -m kernel --state .rsi-datadesk diff
python3 -m kernel --state .rsi-datadesk rollback <64-hex-version-id>
python3 -m kernel --state .rsi-datadesk report
python3 -m kernel compare .rsi-state-a .rsi-state-b
python3 -m kernel --state .rsi-datadesk heldout
python3 -m kernel --state .rsi-datadesk validate --seeds 5
python3 -m kernel --state .rsi-datadesk stop
python3 -m kernel --state .rsi-datadesk resume
```

`heldout` 评测当前版本，DataDesk 包含两类未参与搜索的问题；`validate` 在多个新种子上比较初始与当前版本的同类任务表现。它们不反馈给搜索，配置了任务模型时会产生调用。需要预算的当前版本评测可用 `evaluate --split heldout --max-calls ...`。

`validate-policy --coder-backend deepseek --coder-model YOUR_CODER_MODEL` 单独比较初始和当前策略在 CSV 解析、区间合并两个修复项目上的表现，共四次编码调用，沿用累计账本；结果在 `policy-validation.json` 和独立审计目录。这是固定项目、单次采样诊断，不能证明普遍的策略泛化能力。

`stop` 请求当前轮完成后停止；`resume` 清除标记，再次执行 run 才会继续。

已有报告可离线回放诊断，无需调用模型，也不创建实验状态：

```bash
python3 -m kernel diagnose .rsi-datadesk/evaluation.json > diagnosis.json
```

诊断逐条标明轨迹下标，识别 SQL / 工具错误、无效动作、查询截断、未读或跳过的分页、
无工具观察直接作答、未查 `schema` 即报 SQL 错误（`sql_without_schema`）、对文本数值列未 `CAST` 直接排序（`untyped_sql_numeric_risk`）、
分页环境读完全部页面却未调用 `aggregate` 提交错误答案（`manual_pagination_aggregation`），以及步数、上下文或重复动作终止。诊断不修改评分：已恢复的错误保留记录，
仍可判答对；分页、截断与风险规则属于风险提示，不能直接认定为错误答案的原因。
新的 DataDesk 评测自动保存逐题 diagnostics 和汇总，失败轨迹连同诊断进入下一轮改进证据。
轨迹下标从 0 开始；无特定动作依据的任务级诊断使用空下标列表。

固定分页题（`pagination` 2 类及 `pagination_extended` 6 类）在全部记录已观察、记录无冲突时，可从轨迹独立重算并标记
`verified_arithmetic_mismatch`；不读取评分答案，不扩展为任意 SQL 或开放任务的因果证明。

`run` / `evaluate` 可显式添加 `--analysis-backend deepseek --analysis-model YOUR_ANALYSIS_MODEL`，
对失败任务额外调用独立分析模型。结果须引用存在的动作下标，SQL 假设还须引用实际 SQL 动作，
但内容仍标记为 `unverified_hypothesis`，不能改变评分；格式无效时不采纳假设。
这些调用计入相同预算，默认关闭，每次命令需显式启用。本次真实分析也出现错误计算和无依据的 SQL 推断，
因此模型解释不能作为真值。

### 逐机制消融

`ablate` 在独立副本中，每次只关闭一个显式指定的 Agent 机制。每个变体重新评测原版本，
按相同种子配对，并交替先后顺序，记录得分、逐题退步及已报告 token；不自动删除或发布版本，
不把报告写入搜索历史。它只测试任务能力，不评测 evolution 策略的修复能力。

```bash
# 复用已有任务后端；有模型配置时会产生调用，预算按状态目录累计
python3 -m kernel --state .rsi-datadesk ablate examples/ablation-prompts.json \
  --seeds 2 --confirm-seeds 2 --max-calls 300 --max-tokens 300000
```

示例分别关闭分页提醒、先查表结构提醒；两个变体均从同一原版本独立产生，不累计删减。
规格是 name/path/before/after 对象组成的 JSON 数组，最多 5 项；只允许现有 agent/ 源码文件，
before 必须精确匹配一次，after 必须更短。所有变体先过语法和健康检查，再开始外部调用。
进化后的源码若不再匹配，需按当前版本重新指定规格，工具不会猜测替换。

每个变体可运行 1–5 个种子，默认 2 个。报告保存在 ablations/ 独立目录，完整报告另存
ablation.json；中断保留部分报告和累计用量，上一份完整报告不会被覆盖。
“未观察到退步”不等于机制无用：机制名和关闭范围由人指定，小样本、执行顺序和随机模型
仍会影响结果，token 也只是调用方报告的成本代理。实际删减仍需独立确认和正常验收。

`--confirm-seeds 2`（可选 2–5，默认 0）仅在初评至少两个种子、未观察到逐题退步，且满足
配对收益或 token 降本门槛后运行新的种子。初评与确认均通过才标记 `confirmed_task_benefit`；
它仅表示这组任务下的证据，报告始终不授权自动删除，不能证明机制普遍无用。
在剪枝轮中，若状态目录存在与当前活跃版本匹配且已通过确认的 `ablation.json`，内核会将其作为只读 `ablation_hints` 附入证据（仍标记 `deletion_authorized: false`，候选仍须通过完整配对与复评验收）。

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

### 可选 macOS 宿主沙箱

在支持 `/usr/bin/sandbox-exec` 的 macOS 上，新状态可显式启用：

```bash
python3 -m kernel --state .rsi-sandbox init --host-sandbox macos
# DataDesk 可在第一次 evaluate 命令上添加 --host-sandbox macos，配合固定 DeepSeek 后端
```

配置随状态锁定，已有状态不能切换。启动探测失败或平台不支持时拒绝运行，不自动降级。
默认仍是 `none`，只提供原有进程与接口隔离。

启用后，候选 Agent / 策略 Python 和修复练习执行程序由 Seatbelt 限制：源码只读、
只允许指定目录和运行时的读取、仅候选 worker 的临时目录可写、禁止网络，清除 API 密钥等继承环境；
对子进程继承限制，并设置 CPU、文件大小、文件描述符和输出/时间上限。
真实测试覆盖越界读写、源码写入、网络、环境、子进程及适配器 PYTHONPATH 注入。

固定内核、Python 运行时及 DeepSeek 网络适配器属于信任边界，适配器在沙箱外进行网络调用，
通过结构化补丁返回修改，不执行生成的代码；适配器使用 Python 隔离导入。
该模式拒绝任意自定义命令及 Codex CLI，不能把有网络和宿主权限的编码工具算作已隔离。
这不是虚拟机或通用跨平台沙箱，也没有可移植的硬内存配额。

## 功能与验证状态

每次修改同步更新本表及测试记录；部分实现保留未勾选，并注明缺口。

- [x] 执行代码与进化策略共同版本化，新策略接管下一轮。
- [x] 候选隔离、范围和策略接口检查、固定评分、可选独立模型审查。
- [x] 配对评测、新输入确认、源码快照、自动/手动回退（`versions` / `rollback`）和失败归档。
- [x] 编辑预算、停滞探索、基础剪枝、token 成本选择、单轮多候选复用基线（`--candidates`）与运行限制。
- [x] DeepSeek / Codex 任务、编码、审查接口、可选瞬态 HTTP 重试（`--http-retries`）及真实单次连通性验证。
- [x] CSV / SQL、多轮工具执行、DataDesk 任务（`compact` / `full` / `hard` 规模），已通过离线链路测试。
- [x] 新输入对比与不参与搜索的独立修复诊断。
- [x] DataDesk compact 真实模型链路：搜索基线 5/5、独立留出 7/7；一轮候选通过审查和初评，因降本复评不稳定被拒绝，未发布新版本。
- [x] DataDesk full 真实评测：两个搜索种子共 30/30、一个留出种子 20/20；仅代表本批固定合成任务。
- [ ] DataDesk 有效升级后的策略接管与更多任务/种子上的稳定性：本批三个进化候选均被拒绝，无新发布。
- [ ] 持续多轮能力增长：真实实验已有一次升级并接管下一轮，尚无持续增长证据。
- [x] 失败证据链路：全部 trial 失败汇总、轨迹下标、明确省略标记、完整审计与离线回放。
- [x] 固定分页题算术复核与可选独立模型分析接口；模型假设不参与评分。
- [ ] 通用语义归因：固定题算术已可复核；SQL 语义与开放任务根因尚无可靠的通用验证，真实分析模型也会判断错误。
- [x] 显式逐机制消融工具：隔离副本、配对任务、多种子、用量与退步报告，已通过离线测试。
- [x] CSV-only 分页任务（`pagination` 2 题与 `pagination_extended` 6 题）：限制单页大小、禁止 SQL 绕行、独立状态与留出数据，已通过离线测试。
- [x] 分页环境真实评测与已读数据汇总工具；完整续页后汇总，不绕过数据观察。
- [x] 消融的新种子确认流程与剪枝轮只读 `ablation_hints` 衔接：初评通过后再确认，报告不直接授权删除。
- [x] 只读状态汇总（`report`）、跨状态对比（`compare`）与版本源码差异（`diff`）命令，已通过离线测试。
- [ ] 精确识别无用机制：新分页消融出现一次逐题退步，未进入确认，保留机制；普遍冗余仍无证明，自动剪枝仍使用净删行代理。
- [x] 可选 macOS 宿主沙箱与真实拒绝测试；默认关闭，仅支持固定 DeepSeek 网络适配器。
- [ ] 跨平台宿主沙箱与硬内存隔离：已新增 `--host-sandbox linux-bwrap` 命令构建、fail-closed 探针与 Linux `RLIMIT_AS` 限制（已完成离线单元测试），尚未在真实 Linux 宿主上完成端到端拒绝验证。

已有真实模型实验在 advanced 套件上将任务分数从 33.3% 提高到 100%；下一轮使用新策略，因收益不足拒绝候选。五个新种子共 60 题中，40 题改善、0 题退步。结果属于固定任务范围，不能推断 DataDesk 或开放任务上的效果。

验证记录：

- [真实模型改进实验](docs/experiments/2026-11-05-live-improvement.md)
- [策略接管与独立修复诊断](docs/experiments/2026-11-05-policy-validation.md)
- [CSV / SQL 多轮链路验证](docs/experiments/2026-11-05-datadesk.md)
- [DataDesk 真实模型与进化实验](docs/experiments/2026-11-06-datadesk-live.md)
- [轨迹诊断与离线回放](docs/experiments/2026-11-06-diagnostics.md)
- [逐机制消融工具验证](docs/experiments/2026-11-06-ablation.md)
- [提示机制真实模型消融](docs/experiments/2026-11-06-ablation-live.md)
- [分页任务开发与测试](docs/experiments/2026-11-06-pagination.md)
- [本批功能补齐、宿主隔离及真实评测](docs/experiments/2026-11-06-completion.md)

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
# 或按测试模块并发运行（保持完整子进程边界）
python3 tests/run_parallel.py
```

测试使用离线脚本和本地模拟服务验证生命周期、模型适配、工具执行、预算、回退和评分。脚本提供的修改和答案用于验证机制，不代表自主学习或真实模型效果。

最近一次全量验证：2026-11-25，144 项离线测试全部通过；真实模型实验与离线测试分别记录。
