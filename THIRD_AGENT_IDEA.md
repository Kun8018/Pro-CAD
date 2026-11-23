# 第三个 Agent 的研究设想：Spec Manager + Spec-grounded Verifier

> 交接文档。目标：在 ProCAD（Clarifier + Coder 两 agent）之上论证并实现第三个 agent。
> 前两节是对现有系统的分析，第三节是创新点评估，第四节是确定的研究路线，
> 第五节是已完成的探针工作，第六节是下一步 TODO。

---

## 1. Pro-CAD 的两个 agent 实际怎么协作

虽然论文口径是 "two-agent"，但 `pipeline.py` 里实际有 **三个 LLM 角色**，是严格线性、无回环的流水线：

| 阶段 | 角色 | 模型 | 做什么 |
|---|---|---|---|
| Step 1 | **Clarifier** (`AskAgent`, `src/ask_agent.py`) | gpt-4o-mini / SFT Qwen2.5-7B | 判断 prompt 是否 underspec/conflict，输出 JSON（`is_misleading`, `issues`, `questions`）；不模糊就直接跳过 |
| Step 2 | **User Simulator**（`src/user.py`，persona: expert/intermediate/novice/confused） | gpt-5-mini | 拿着原始 prompt 作为"答案依据"回答澄清问题 |
| Step 3 | **Clarifier 再次调用**（复用 Step 1 的多轮上下文 + answer） | 同 Step 1 | 输出 `standardized_prompt`——一段改写后的完整描述 |
| Step 4 | **Coder**（SFT Qwen） | ProCAD-coder | **只看** `standardized_prompt`，生成 CadQuery 代码 |

之后是离线评估：CadQuery 执行 → mesh → 与 GT STL 算 Chamfer distance（`pipeline.py` 的 `evaluate_code`）。

### 关键架构特征（也是薄弱点）

1. **唯一的 agent 间交接物是一段自然语言 `standardized_prompt`。**
   Clarifier 是单点信息瓶颈——它在 Q&A 中丢掉或臆造的任何信息，Coder 永远不知道，也无处申诉。
2. **没有任何执行后反馈闭环。** 代码报错、Chamfer 很差，都不会回流给任何 agent。
   `config/prompt_verification.py` 里虽有 "description ↔ code 一致性校验"，但那只用于**造数据阶段**（过滤误导样本），推理时不进 pipeline。
3. **澄清只有一轮**，且验证信号（Chamfer）依赖 GT mesh，真实部署时不存在。
   → 加 test-time verifier 时**不能靠 Chamfer**，只能靠渲染图视觉验证或代码静态/规约一致性检查。

---

## 2. 加第三个 agent 的创新性评估

### 方案 A：Verifier agent —— 单独做偏弱

近邻工作：
- **CADCodeVerify（ICLR 2025）**：「生成 → 验证问题 → 反馈 → 修正代码」的 VLM 自纠错闭环，VLM 看自己渲染的多视图给自己挑几何错误。
- **cadrille（ICLR 2026）**：多模态 CAD 重建 + 在线 RL 验证。

如果只是"Coder 生成后加个 agent 渲染看图、挑错打回"，审稿人会直接归为 CADCodeVerify 变体，创新性不足。

但有两个 Pro-CAD 架构特有的差异化位置：

- **跨阶段一致性 verifier（推荐切入点）**：不做几何验证，做**信息保真度验证**——核对
  `original prompt → Q&A → standardized_prompt → code` 这条链，定位信息在哪一跳丢失或被臆造（unsupported assumption）。CADCodeVerify 没有澄清阶段，这个位置它占不住，且正好打 Pro-CAD 自己最大的结构性弱点。
- **verifier 产出回灌 Clarifier 而非 Coder**：发现残留歧义时触发第二轮提问，形成 clarify ↔ verify 环；而不是 verify → coder 的自纠错（后者就是 CADCodeVerify）。

**实验设计注意**：baseline Clarifier 只要多问一轮问题就能拿到更多信息。必须证明 verifier 闭环的增益超过"单纯多问一轮"本身（需要一个 fixed-round-budget 对照）。

### 方案 B：Context / Spec Manager agent —— 更新，但要防"工程性"质疑

text-to-CAD 领域基本没人做，比单纯 verifier 新。核心是把扁平的 `standardized_prompt` 升级成**结构化设计状态**：feature / 参数 / 约束的 JSON（类似 B-rep 语义的设计意图图），并维护**字段溯源**：

- `user-stated`：原始 prompt 明确给出
- `answer-confirmed`：Q&A 中确认
- `agent-assumed`：agent 填补的默认值（显式暴露）
- `inferred`：由约束推导

职责：
- **给 Clarifier**：保证每个问题针对未确认字段，避免冗余提问；
- **给 Coder**：喂结构化 spec 而非自由文本，臆造参数显式标记为 `assumed`；
- **给编辑/多轮场景**（参考目录中的 CAD-Editor）：局部改参数时做影响传播，而不是重生成整段代码。

**风险**：审稿人可能说"这就是 scratchpad / memory module，不是 agent"。
要成立必须：(1) 有可测量的独立增益（Chamfer、信息丢失率、提问轮次）；(2) 消融掉"换种结构化 prompt 也能实现"的解释（对照：同样的 JSON schema，但无溯源、无主动行为）。

---

## 3. 确定的研究路线：Spec Manager 为核心，Verifier 作为消费者

三 agent 故事：

```
Clarifier（主动澄清）
   →  Spec Manager（维护带来源的结构化设计规约）
   →  Coder（按规约生成 CadQuery）
   →  Verifier（把代码/渲染结果对齐回规约字段，而非仅做视觉自检）
冲突 → 回灌 Clarifier 触发再澄清（clarify ↔ verify 环）
```

相对两篇近邻都站得住：

- **对 Pro-CAD**：解决"单段自然语言交接、无闭环"的结构缺陷；
- **对 CADCodeVerify**：验证针对**带溯源的规约**做跨阶段一致性检查，闭环经过澄清环节，而不是 VLM 看自己的渲染图自问自答。

---

## 4. Motivation 探针：交接处的信息丢失率（已完成实现）

在动任何模型之前，先用数据回答：**Clarifier → Coder 的 `standardized_prompt` 交接处，信息丢失/臆造/错误值存活的比例有多大？**

如果比例可观（尤其在 conflict 类样本上），整个研究的 motivation 就有数据支撑。

### 缺陷定义（ground truth = original prompt + Q&A answers）

| 缺陷类 | 定义 | 对应论证 |
|---|---|---|
| **DROPPED** | 真值中的几何事实（尺寸/特征数/特征类型）在 standardized prompt 中消失 | 信息瓶颈：Coder 被迫猜 |
| **UNSUPPORTED** | standardized prompt 出现、但任何来源都不支持的值 | Clarifier 臆造 |
| **CARRYOVER** | misleading prompt 的错误值在澄清后原样存活 | 澄清失败，直接打 Pro-CAD 的核心 claim |

### 实现物

- `probe_spec_fidelity.py`：探针主脚本。
  - **数值通道**（默认、纯 CPU、可复现）：正则抽取 dimension slot（radius/length/width/height/thickness/depth/distance/angle/chamfer + 孔/槽计数），mm/cm/m/inch 单位归一化，按局部上下文对齐同一特征；输出按 `config_name`（underspec vs conflict）分组的样本缺陷率、事实缺陷率，以及缺陷样本 vs 干净样本的平均 Chamfer distance。
  - **LLM 语义通道**（`--with-llm`）：事实级审计（`config/spec_fidelity_audit.py` 中的 judge prompt），捕获数值通道漏掉的非数值事实（特征类型、方向、布尔操作），用于交叉验证数值通道的可信度。
  - 输出 `probe_results/spec_fidelity_summary.json`（分组比率 + CD 对比）和 `spec_fidelity_details.json`（每条缺陷的原文片段，便于人工抽查误报）。
- `pipeline.py` 新增 `--skip-codegen`：只跑 Step 1–3（detect → Q&A → corrected description），**不加载本地 Coder、不需要 GPU、不需要 GT mesh**；cadquery/trimesh 改为 `evaluate_code` 内懒加载，无 CAD 栈的机器也能跑文本阶段。
- 已用合成 fixture 验证：carryover（5→8mm 错误值存活）、dropped（thickness/chamfer 丢失）、unsupported（凭空 fillet 3mm）、`2 cm = 20 mm` 单位换算判净、"radius should be 5 mm" 松散措辞、original/answer 重复事实不双算——分类均正确。

### 真实数据运行步骤（在有 API key 的机器上）

```bash
# 1. 只下数据集 JSON（文本，几百 MB 内），不需要 DeepCAD mesh
hf download BBexist/ProCAD --repo-type dataset --local-dir ./dataset

# 2. 配置 config/config.yaml + OPENAI_API_KEY

# 3. 文本-only pipeline（笔记本 CPU 即可）
python pipeline.py \
  --json_path dataset/selected_misleading_samples_test \
  --output_dir ./clarification_results_textonly \
  --skip-codegen

# 4. 探针（数值通道）
python probe_spec_fidelity.py \
  --results_dir ./clarification_results_textonly

# 5. LLM 通道交叉验证
python probe_spec_fidelity.py \
  --results_dir ./clarification_results_textonly --with-llm
```

阶段二（台式机 3080 12GB）再跑完整 pipeline 拿 Chamfer 相关性。
注意：Qwen2.5-7B fp16 约 15GB，12GB 装不下，需要给 `src/inference.py` 加 4-bit 加载（bitsandbytes，权重 ~5GB，batch_size 降到 1–2）或换 vLLM + 量化版——**这是待办**。

---

## 5. 下一步 TODO（给接手的 agent）

1. 在台式机上跑第 4 节的真实数据探针，拿到 underspec / conflict 分组的 dropped / unsupported / carryover 比率与 LLM 通道交叉验证；人工标注 ~100 条 details 给数值通道报 precision。
2. ~~若缺陷率可观，开始设计 Spec Manager~~ **（最小实现已完成，2026-11-19）**：
   - ✅ 结构化 spec schema（feature 图：参数、约束、四级溯源 `user-stated / answer-confirmed / agent-assumed / inferred`，单位归一化 mm）；
   - ✅ LLM 抽取/更新（`config/spec_manager.py` prompts），但**溯源合并是确定性代码**（rank：answer-confirmed > user-stated > inferred > agent-assumed），LLM 输出经 `merge_specs` 防御性对账，所有覆写记入 `spec['resolutions']`；
   - ✅ Clarifier 额外轮次只由"未确认字段"驱动（`unresolved_fields` → questions，LLM 失败时有确定性模板兜底）；
   - ✅ 消融：`--spec-no-provenance`（同一 JSON schema 喂 Coder，但剥掉溯源、关闭主动提问轮次）；固定轮次预算用 `--spec-extra-rounds N` 控制（对照 baseline 需跑 N=0 vs N=1）。
3. ~~Verifier 实现为 spec-grounded~~ **（确定性通道已完成，2026-11-19）**：
   - ✅ `src/spec_verifier.py`：不执行代码、纯 AST 静态核对，三类裁决 V1 `CONFIRMED_VALUE_MISSING`（blocking）、V2 `UNSUPPORTED_LITERAL`（warning，已处理 radius/diameter 对偶与结构常数）、V3 `ASSUMED_FIELD_USED` → `build_reclarify_request` 回灌 Clarifier 而非改代码；
   - ✅ LLM 语义通道 prompt（`config/spec_verification.py`，查特征类型/布尔操作/placement 等非数值事实），接口 `verify_with_llm`；
   - ✅ 已接入 pipeline Step 5：`--use-spec-manager` 时每条结果存 `step5_verification`；
   - ⬜ **未完成**：verifier 裁决触发的自动再澄清→再生成闭环（当前只产出 `reclarify_request`，需在 process_batch 里加最多一轮循环）；渲染图视觉通道。
4. 评估指标（待真实数据跑）：Chamfer（baseline / spec+provenance / spec 无溯源 三组）、事实丢失率（探针直接对 `step3_spec` / Coder hand-off 文本复用）、达到收敛的平均提问轮次、单轮预算下的成功率。
5. 确定性逻辑测试已就位（无需 API/GPU）：`python tests/test_spec_manager.py`（24 tests）、`python tests/test_spec_verifier.py`（10 tests）。

### 已实现产物清单（2026-11-19）

| 文件 | 内容 |
|---|---|
| `config/spec_manager.py` | Spec Manager 三个 prompt（抽取/更新/提问）与 schema、溯源规则 |
| `src/spec_manager.py` | Spec 规范化、溯源秩合并、冲突检测、未决字段检测、Coder hand-off 渲染（含无溯源消融）、SpecManager LLM 封装 |
| `config/spec_verification.py` | Verifier LLM 语义裁决 prompt |
| `src/spec_verifier.py` | CadQuery AST 字面量抽取、V1/V2/V3 确定性核对、回灌 Clarifier 请求构造、LLM 通道接口 |
| `pipeline.py` | `--use-spec-manager / --spec-no-provenance / --spec-extra-rounds / --spec-manager-model`；Step 3.5（抽取→合并→主动提问轮）与 Step 5（spec-grounded 验证）；Clarifier 原 standardized_prompt 保留在 `step3_clarifier_standardized_prompt` 供 A/B |
| `tests/` | 34 个离线单测，覆盖 carryover/dropped/unsupported fixture、cm→mm、id 漂移、radius/diameter 对偶、误发明 fillet、语法错误等 |

## 讨论

multi agent
