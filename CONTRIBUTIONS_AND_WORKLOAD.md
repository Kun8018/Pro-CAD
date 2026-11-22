# ProCAD 后续研究方向与工作量评估

本文整理三条研究方向，并区分 ProCAD 论文/原始代码已有能力、尚未覆盖的部分、当前两个 commit 的进度，以及后续工作量。这里的 ProCAD 原始基线指本地仓库 `origin/main`；当前分支新增的 Spec Manager 和 probe 不算作 ProCAD 原论文已有贡献。

## 1. 面向生成的可追溯规格管理与闭环验证

### 研究目标

让用户描述、澄清问答、结构化规格和 CAD 程序之间的信息可以追踪；检查生成代码及其几何是否满足规格；发现不一致时触发针对性澄清并重新生成。

### ProCAD 已有与未覆盖

| 环节 | ProCAD 论文/原始代码已做到 | 尚未覆盖 | 预计后续工作 |
|---|---|---|---|
| 澄清与问答 | Clarifier 分析含糊/冲突描述、提出问题；收集回答后生成修订描述。论文说明 Clarifier 用 clarification trajectories 做 agentic SFT，Coder 也经过微调。 | 没有逐字段维护信息来源、确认状态和变更历史的设计状态。 | 定义 CAD 特征、参数、关系、约束、来源与确认状态的 schema；明确冲突和覆盖规则。 |
| Coder 交接 | Coder 根据澄清后的描述生成 CadQuery。 | 交接主要是自由文本；Coder 不知道参数是用户给定、回答确认还是模型假设。 | 将 schema 稳定地渲染/转换为 Coder 输入，并保留兼容原始描述的对照。 |
| CAD 代码执行 | 原始代码能执行 CadQuery、生成 mesh，并进行离线几何评估。 | 这是生成/评测流程，不等于基于规格的推理时验证；结果不会自动回流到 Clarifier。 | 复用现有执行工具，检查执行成功、实体有效、输出非空，并实现超时和错误处理。 |
| 规格一致性验证 | 有基于 ground truth 的几何指标（如 Chamfer）以及其他离线评估。 | 没有逐规格字段定位“漏了什么/多了什么/哪一环改错了”，Chamfer 也不能直接解释违反了哪条规格。 | 建立按字段和特征核对的检查，并用人工标注评估 verifier 的准确率。 |
| 反馈闭环 | 有一轮澄清问答与修订描述。 | 没有生成后验证→重新澄清→更新规格→重生成的运行时闭环。 | 实现反馈状态机、轮数上限、停止条件和日志；控制反复提问或循环生成。 |
| 实验评估 | 论文已有生成质量、澄清质量/交互成本等评测方向。 | 尚不能单独归因 provenance、结构化表示、执行验证、反馈闭环各自的贡献。 | 人工标注规格保真度；控制提问轮数和模型调用的因子化消融；同时报告几何质量、有效率、交互成本。 |

### 当前两个 commit 覆盖范围

已覆盖原型部分：

- `--skip-codegen` 和 `probe_spec_fidelity.py`：可先检查 Clarifier 输出的信息保真，不必每次都运行 Coder。
- `src/spec_manager.py`：结构化规格、来源标签、合并/冲突记录、未解决字段和 Coder hand-off。
- `pipeline.py`：在 Clarifier 后加入 Spec Manager，将规格渲染结果用于 Coder。
- `src/spec_verifier.py`：AST/数字静态核对，并生成 `reclarify_request` 文本。
- 增加了 Spec Manager 和 Verifier 的离线单元测试。

尚未完成：

- 当前 verifier 不执行 CadQuery，也不检查实际 B-rep/mesh 是否满足几何规格；静态数字匹配无法证明数字属于正确特征。
- `reclarify_request` 会保存到结果，但 pipeline 尚未实际调用 Clarifier、收集用户回答并重跑 Coder。
- LLM audit 结果与过滤后的 rows 存在潜在错位，需按样本 ID 对齐后再相信报告。
- `spec_no_provenance` 同时关闭 extra clarification rounds，相关消融混杂了 provenance 与额外问答，需要拆开。
- 需要人工标注的小规模金标准来校准 probe/verifier，并进行端到端实验。

### 粗略工作量

- Schema、来源追踪和冲突处理完善：**中等**（已有代码骨架；补边界条件、身份对齐和可靠性评估）。
- 执行级检查及几何有效性检查：**中到高**（可复用现有 CadQuery 执行工具，但要处理安全、超时、异常和几何有效判据）。
- 真正的重新澄清与重生成闭环：**中到高**（需设计状态管理、轮次控制、回答更新和停止策略）。
- 人工标注、probe 校准和消融实验：**高**（数据与实验设计通常是主要工作量，也是论文结论的关键证据）。

## 2. 面向 CAD 编辑的可追溯规格表示

### 研究目标

把可追溯规格从“生成一个 CAD”扩展到“修改已有 CAD”：明确用户要求改变什么、保留什么、变化依据是什么，并避免局部编辑意外破坏其他特征。

### ProCAD 已有与未覆盖

| 环节 | ProCAD 论文/原始代码已做到 | 尚未覆盖 | 预计后续工作 |
|---|---|---|---|
| 文本到 CAD 生成 | 根据描述和澄清结果生成 CadQuery。 | 主任务是从描述生成模型，不是对现有模型做局部编辑。 | 定义编辑请求、初始模型和目标结果的数据格式。 |
| 编辑状态追踪 | 澄清流程能保留 prompt、问题、回答和修订描述等过程信息。 | 没有将“变更前值→变更后值”、变更来源、受影响特征和保持项组织成编辑状态。 | 建立规格 diff：新增、删除、修改、保持；为变更建立来源和目标特征关联。 |
| 局部编辑与影响传播 | 无专门编辑闭环。 | 不知道如何定位要修改的特征、传播依赖，也没有保护非目标特征的机制。 | 设计特征定位、依赖分析、局部重生成/修改和非目标保持策略。 |
| 编辑验证 | 有生成代码执行和离线几何评估能力。 | 没有针对“只改指定部分、其他部分保持”的验证指标和评测集。 | 构建带目标变更/保持项标注的编辑集，评估目标完成率、非目标改动率、程序有效率和几何质量。 |

### 当前两个 commit 覆盖范围

当前 commit 的规格 schema、provenance、变更记录和字段合并可作为编辑方向的基础组件，但目前 pipeline 面向从文本生成 CAD。尚无编辑数据格式、已有模型的特征定位、局部修改/依赖传播或非目标保持评测，因此不能把当前实现算作已完成 CAD 编辑功能。

### 粗略工作量

- 定义编辑任务、输入/输出和编辑规格 diff：**中**。
- 构建或整理编辑数据与人工标注：**高**。
- 实现特征定位、依赖/影响传播和局部修改：**高**，取决于模型表示是否能稳定映射到代码/特征。
- 验证非目标结构保持并完成对照实验：**中到高**。

这条方向复用第一项的 provenance，但需要新的任务定义、数据和指标，属于独立的研究扩展。

## 3. 将可追溯表示扩展为 Feature Script

### 研究目标

把可追踪信息提升到 CAD 特征和构造步骤层级。Feature Script 应明确表达特征类型、参数、依赖、操作顺序、设计关系及其来源，并能支持生成、验证和编辑。

### ProCAD 已有与未覆盖

| 环节 | ProCAD 论文/原始代码已做到 | 尚未覆盖 | 预计后续工作 |
|---|---|---|---|
| CAD 程序生成 | Coder 生成 CadQuery 程序，程序可执行并导出几何。 | 没有独立、标准化的中间 Feature Script 作为规格与代码之间的显式表示。 | 定义 Feature Script 的语法/schema、语义、约束和版本规则。 |
| 特征及参数 | 输入描述和 CadQuery 代码隐含包含形状与建模操作信息。 | 没有保证每个特征都能映射到唯一 ID、参数、来源和依赖；代码文本结构不等于稳定特征图。 | 建立规格→Feature Script→CadQuery/几何的转换，处理 ID、依赖和转换失败。 |
| 验证 | 有代码执行和 GT 几何评估。 | 无 Feature Script 层级的静态/动态验证，无法逐特征报告规格偏差。 | 实现特征级不变量、依赖检查、参数约束和几何执行检查。 |
| 编辑与复用 | 无专门的 Feature Script 编辑能力。 | 无特征级局部修改接口及其对几何的影响分析。 | 用 Feature Script 表达变更和保持项，并评估局部编辑与整体重生成。 |
| 实验 | 有文本到 CAD 的生成评测。 | 没有证明 Feature Script 相比结构化 JSON、自由文本或直接代码生成更有价值的实验。 | 做表示消融，衡量可追踪性、编辑成功率、验证定位准确率和额外成本。 |

### 当前两个 commit 覆盖范围

当前 `step3_spec` 是结构化规格，包含特征、参数和来源信息；它可以看作 Feature Script 的前置表示或雏形，但目前还不能等同于 Feature Script。要成为可用的特征脚本，还需要明确操作语义、依赖关系、可执行/可转换语义，以及从特征到实际几何的稳定映射。

### 粗略工作量

- 设计 Feature Script schema 和语义：**中到高**。
- 建立规格/Feature Script/CadQuery/几何之间的转换：**高**。
- 特征级验证、错误定位和编辑支持：**高**。
- 构建评测集并证明该表示比普通结构化规格更有效：**高**。

这条方向范围最大，适合作为第一项和编辑方向验证后的后续研究；若要纳入同一项目，应先限定 Feature Script 的表达范围（例如常见 sketch/extrude/cut 特征），避免一开始追求通用 CAD 特征系统。

## 总体工作量与建议顺序

| 方向 | 现有基础 | 主要未完成 | 粗略工作量 |
|---|---|---|---|
| 生成的可追溯规格与闭环验证 | 当前两个 commit 已有原型和 pipeline 接入 | 修 probe/消融；提升 verifier；真正闭环；人工评测 | **中到高** |
| CAD 编辑中的可追溯规格 | 可复用 provenance schema 和变更日志 | 编辑任务/数据、特征定位、局部修改、保持性评测 | **高** |
| Feature Script | 可复用结构化规格的概念与部分字段 | 脚本语义、转换、执行/验证、编辑与独立评测 | **高** |

建议先完成第一项的可信实验，确认可追溯规格确实能改善信息保真或生成结果；再将其扩展到 CAD 编辑。Feature Script 可作为后续表示层研究，先限定支持的 CAD 操作范围。

## 核实依据与边界

- ProCAD 论文摘要：<https://arxiv.org/abs/2602.03045>。论文说明 Clarifier 通过 clarification trajectories 训练，Coder 基于整理后的 text-to-CadQuery 数据微调；框架包含主动澄清和代码生成。
- 本地原始基线：`origin/main`（当前分支的两个 commit 之前）。相关执行/评估逻辑见 `pipeline.py` 与 `src/mesh_utils.py`。
- 当前分支实现：`feat/spec-fidelity-probe` 上的 `a657926`、`c84456f`。
- 本文“工作量”是根据功能边界作的相对估算（中/高），不是论文或仓库给出的官方工期；具体日历时间取决于数据、GPU/API、标注规模和可复用的执行环境。

## 附：Agent 数量与定位的设计决策

讨论问题：Spec Manager 和 spec-grounded Verifier 是否应该各立一个 agent？
结论：**新增 1 个 agent，而不是 2 个**——Spec Manager 是唯一的新 agent；
Verifier 做成 Spec Manager 调用的确定性工具（tool），不立独立 agent。

### 先数系统中的“agent”

| 角色 | 是否 agent | 理由 |
|---|---|---|
| Clarifier | 是 | LLM 决策：检测歧义、生成问题 |
| Coder | 是 | SFT LLM，自主生成代码 |
| User Simulator | 否（评测夹具） | 只在实验中模拟用户，真实部署不存在，不应计入系统组件 |
| Spec Manager | 是（新增） | 维护状态、决定问什么、管理交接物 |
| Verifier | 否，做成 tool | 主通道（V1/V2/V3）是纯 AST/B-rep 计算，零 LLM、零自主决策 |

核心论据：Verifier 主通道是确定性函数（无 LLM 调用、无目标、无行动选择）。
把纯函数称为 agent，恰好坐实审稿人“multi-agent is just prompt engineering
with extra steps”的批评；反过来，“agent 使用形式化验证工具”是软件工程界
熟悉且受认可的叙事（类型检查器不是 agent，但使用类型检查器的程序员是）。

### 推荐架构：3 agent + 1 verifier 工具

```text
User
 │
Clarifier ──► Spec Manager（持有结构化状态的 agent）
                 │   ├─ tool: verify(spec, code)  ← AST / B-rep，确定性
                 │   └─ 依据 verify 的结构化裁决做路由
                 ▼
               Coder ── code ──┐
                 ▲             │
                 └─────────────┘
   V1/V2（confirmed 值缺失 / 无出处字面量）：带字段诊断 grounded 重生成（不问用户）
   V3（assumed 值被使用）：Spec Manager 向用户追问（信息真实缺失）
```

闭环的“智能”集中在 Spec Manager 一处：它读 verifier 的裁决，区分信息缺失
（V3，去问）与执行失误（V1/V2，打回 Coder）。这是控制器逻辑，归入 Spec
Manager 比再立一个“调度 agent”自然。

### 三种方案取舍

1. **加 1 个（Spec Manager + verify 工具，推荐）**
   - 叙事最干净：“单段自然语言交接 → 带来源的结构化规约 + 形式化符合性检查”；
   - 消融天然成立：去溯源（已有 `--spec-no-provenance`）、去验证工具、去闭环；
   - 成本最低：V1/V2 零 token；LLM 语义 judge 可作为工具内可选通道。
2. **加 2 个（另立独立 Verifier agent）**
   - 仅当 Verifier 需要自主判断（自选渲染视角、主动决定测量哪些 B-rep
     特征、综合多源证据）时才成立——此时它从“尺子”变为“质检员”；
   - 建议作为论文 extended/full 版本而非主方法；若实验显示独立 agent 化
     无额外增益，反而是更稳的结果。
3. **不新增（把 Spec Manager 并入 Clarifier）**
   - 逻辑上可行：`unresolved_fields` 已驱动提问，Spec Manager 包含 Clarifier
     全部职能；
   - 当前不建议：保留 Clarifier 可直接复用其 SFT checkpoint 与 ProCAD 全部
     baseline 对照，职责切分（Clarifier 管对话、Spec Manager 管状态）也更清晰；
   - 可写入 future work：最终形态是 Clarifier 被 Spec Manager 吸收，从
     3 agent 收敛回 2 agent。

### 判断准则

> 有状态、要做面向用户的决策（问什么、信哪个值、打回谁）→ agent；
> 无状态、输入确定输出确定、可写单测 → tool。

据此：merge/溯源是 Spec Manager 的状态逻辑（agent）；AST 核对、B-rep
测量是 tool；VLM 视觉判断若只对 spec 字段做 yes/no 裁决也是 tool，仅当它
需要主动规划检查项时才升级为 agent。

对外提法：**“ProCAD has two agents; we add one — a Spec Manager — equipped
with a deterministic spec-conformance verifier, closing the loop.”**
与 CADCodeVerify 的差异在于验证锚点（带来源的 spec vs. 无锚点视觉自问自答），
而不在于验证器是否被称作 agent。
