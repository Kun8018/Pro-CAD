# ProCAD 的发布物、训练方式与评估方案

> 内容整理自对仓库脚本（`generate_misleading.py`、`to_sft.py`、`pipeline.py`、
> `src/evaluation.py`、`analyze_results.py`、`training/`）的代码核查。
> 相关讨论见 [THIRD_AGENT_IDEA.md](THIRD_AGENT_IDEA.md)。

---

## 1. 三个发布物：2 个模型 checkpoint + 1 个数据集

| 发布物 | 类型 | 本质 |
|---|---|---|
| `BBexist/ProCAD-coder` | 模型 | Qwen2.5-7B-Instruct 全量 SFT 后的 CadQuery 代码生成模型 |
| `BBexist/ProCAD-clarifier` | 模型 | 同一 7B 底座，用多轮澄清对话 SFT 的歧义检测/提问模型 |
| `BBexist/ProCAD` | 数据集 | 4 个 JSON：`improved.json`（约 1 万干净样本）、`train_val_test.json`（UID 划分）、`selected_misleading_samples_train/test`（误导样本） |

注意：`pipeline.py` 默认跑的 Clarifier 是 **gpt-4o-mini API**（`CLARIFY_AGENT_MODEL`
默认值），发布的 7B clarifier 是自训练的本地替代。复现实验时要留意对照口径
（API clarifier vs. SFT clarifier）。

---

## 2. 两个 agent 分别是怎么做出来的

### 2.1 数据生产线（核心工作量）

链路：**DeepCAD/Text2CAD → 干净样本 → 合成污染 → 过滤 → SFT**。

1. **底座数据**：Text2CAD 的专家级文本 prompt + DeepCAD 的 CAD 序列 / CadQuery
   GT 代码。`batch_label.py` 用 LLM 把 GT 代码重写成自然语言干净描述
   （`improved.json` 的 `final_modified_prompt` / `final_generated_code`）。
2. **干净样本门槛**：每条样本先让 coder 生成代码、渲染 mesh、与 GT STL 算
   Chamfer，只保留 **CD < 0.0002** 的样本（`generate_misleading.py` 的
   `DEFAULT_CD_THRESHOLD`）。先保证"干净 prompt → 好几何"成立，后续污染实验
   才有意义。
3. **合成误导样本**（`generate_misleading.py`，gpt-4.1 + 两类 few-shot 库）：
   - **underspec**：删除/弱化关键参数，K=1,2,3 表示删除参数的个数；
   - **conflict**（`direct_conflict_same_feature_two_values`）：同一特征给两个
     不相容的值（如半径先说 52 后说 50）。
   - 每条污染样本同时记录"标准答案"：`questions_to_ask`（该问什么）、
     `answer_to_questions`、`what_changed`、`ambiguity_scan`。
4. **污染有效性过滤**：污染后 prompt 生成几何的 CD 必须显著变差才入选
   （否则该样本没有"危害"，测不出澄清价值）；再过 `check_leakage.py` 用 LLM
   检查 prompt 是否泄漏代码/答案，泄漏的丢弃。

关键设计：**测试集每条样本自带三件套真值——正确描述、被污染描述、"该问哪些
问题"的标准答案**，使全部自动评估成为可能。

### 2.2 Coder 训练

配置：`training/configs/qwen2-5_full_sft.yaml`

- 底座 Qwen2.5-7B-Instruct，**全参数 SFT**（DeepSpeed ZeRO-3），bf16；
- lr 1e-5 cosine，2 epochs，cutoff 4096，有效 batch 8
  （per_device 4 × grad_accum 2）；
- 训练对为单轮：`"Generate CadQuery Python code for ... " + final_modified_prompt`
  → `final_generated_code`；
- **Coder 完全不知道澄清的存在**，只是在干净专家描述上训练的文本→代码模型，
  保证论文主张的分工纯净性。

### 2.3 Clarifier 训练

配置：`training/configs/qwen2-5_full_sft_misleading.yaml`，转换脚本 `to_sft.py`

- 同底座，1 epoch，cutoff 8192（多轮对话更长），sharegpt 格式，
  `mask_history: false`（所有 assistant 轮都参与训练）；
- **K>0 污染样本**拼成完整多轮对话：
  1. system（`ASK_AGENT_SYSTEM_PROMPT`，规定 JSON 输出格式）
  2. user：污染 prompt
  3. assistant：`{"is_misleading": true, "questions": [...]}`
  4. user：标准答案（`answer_to_questions`）
  5. assistant：`{"is_misleading": false, "standardized_prompt": <原始正确描述>}`
- **K=0 干净样本**只训短格式：prompt →
  `{"is_misleading": false, "standardized_prompt": prompt}`，防止模型变成
  "见什么都要问"。

Clarifier 学到两个监督动作：**检测 + 提问**（第 3 轮）和**吸收答案还原正确
描述**（第 5 轮）；训练中的"用户回答"就是合成数据的标准答案字段。

---

## 3. 评估方式（三层指标）

### 3.1 几何层：Chamfer Distance（主指标）

实现：`pipeline.py` 的 `evaluate_code` + `src/evaluation.py` 的 `chamfer_distance`

1. CadQuery 代码执行导出 mesh；
2. `normalize_mesh` 归一化（消除尺度/位置影响）；
3. 表面采样 **8192 个点**；GT 为 DeepCAD 提供的 STL，同样处理；
4. 双向 Chamfer：cKDTree 最近邻，`(mean(d1²) + mean(d2²)) / 2`；
5. 另算同一 mesh 两次采样的 baseline CD，作为采样噪声下限；
6. 分档：CD < 0.0001 excellent，< 0.0005 good，< 0.001 acceptable，否则 poor；
7. 同时报**成功率**（代码可执行并产出有效 mesh 的比例）与 CD 的均值/中位数。

### 3.2 问题质量层：LLM judge 的 precision / recall / F1

实现：`analyze_results.py` + `JUDGE_QUESTION_QUALITY_*` prompt（gpt-5-mini 裁判）

- 输入：ground-truth 问题清单（造数据时的 `questions_to_ask`）vs Clarifier
  实际提出的问题；
- judge 将每个实际问题判为 **Matched**（意图对应同一个缺失几何量）或
  **Hallucinated**；
- `calculate_question_score` 计算：
  - recall = 该问的问到了几个（低 → 模型"懒"）；
  - precision = 问的中有多少不相关（低 → "啰嗦/瞎问"）；
  - F1 为主指标。

### 3.3 歧义消解层：三分制 judge

实现：`JUDGE_AMBIGUITY_RESOLUTION_*` prompt。judge 拿到**原始正确 prompt +
`what_changed` + 澄清后的 standardized_prompt**，打三档分：

- **1.0 Fully Resolved**：所有缺失/冲突值都还原且数值正确（允许数学等价），
  无残留冲突；
- **0.5 Partially Resolved**：只还原部分，或修了但数值猜错；
- **0.0 Unresolved/Failed**：仍有歧义、引入新冲突、或幻觉关键细节。

这是不跑 Coder 也能评的"文本层最终效果"。

---

## 4. 如何证明比单 agent 好：同一样本的三条件对照

证据链是**样本内三重 Chamfer 对比**（pipeline 对每个样本落三个字段）：

| 条件 | 含义 | 结果字段 |
|---|---|---|
| **Original CD** | 干净 prompt 直接喂 Coder | 性能上界 |
| **Misleading CD** | 污染 prompt 直接喂 Coder（**即单 agent baseline**） | 证明危害 |
| **Clarified CD** | 污染 prompt → Clarifier 提问 → 用户模拟器回答 → standardized_prompt → 同一个 Coder | 证明恢复 |

论证逻辑：

1. ② 相对 ① 显著变差（合成污染阶段已用 CD 门槛保证）；
2. ③ 相对 ② 显著回升、趋近 ① → 澄清有用；
3. 因 Coder 相同、样本相同，唯一变量是"澄清前/后"，对照干净；
4. 再按 underspec K=1/2/3 与 conflict 分组，检验**污染越重、澄清增益越大**
   的剂量效应。

辅助证据：问题 F1（问得准不准）与消解分 0.5/1.0 占比。

---

## 5. 该评估方案的软肋（后续工作可切入的点）

1. **用户回答是 oracle**：`ANSWER_QUESTIONS_PROMPT_TEMPLATE` 直接把 original
   prompt（ground truth）塞给 User Simulator 开卷答题。真实用户会答错、答不出、
   多轮后不耐烦；novice/confused persona 只用于造数据侧，主实验的回答是理想的。
   "噪声答案下增益剩多少"没有测。
2. **只有一轮澄清**：K=3 样本一轮理论上问不完，但流程不允许第二轮追问。
3. **测试信号依赖 GT mesh**：Chamfer 在部署时不存在，系统在真实使用中无法
   判断自己生成得对不对——这正是需要 test-time verifier 的动机缺口。
4. **单 agent baseline 偏弱**：比较对象是"无澄清的同一 Coder"，而不是
   "让单 agent self-refine / 自问自答"。后续 Spec Manager 工作需要补更强的
   baseline（给单 agent 相同的额外推理/提问预算），即 fixed-round-budget 对照。
5. **judge 指标缺人工校验**：F1 与消解分均由 gpt-5-mini 单方裁定，没有报告
   人工一致性数据。

一句话总结：ProCAD 的方法论贡献主要在**数据合成 + 三条件评估协议**，两个模型
本身都是标准 SFT；它证明的是"先澄清再画"在 oracle 用户与离线 GT 评估下成立，
而**非理想用户、多轮澄清、部署时无 GT 的验证**是留下的三个洞——Spec Manager
（结构化溯源）+ spec-grounded Verifier（不依赖 GT 的静态核对）补的正是后两个。

---

## 6. Spec-Verifier 的验证对象：code ↔ spec，不是 code ↔ GT

新增的 spec-grounded Verifier（`src/spec_verifier.py`，`verify_code_against_spec`）
只有两个输入：

- **spec**：Spec Manager 从"用户输入 + Q&A 回答"构建的结构化规约；
- **code**：Coder 生成的 CadQuery 代码（AST 静态解析，**不执行**）。

它全程不接触 GT mesh/STL、original prompt（ground-truth 正确描述）和渲染图；
可选 LLM 语义通道同样只拿 spec + code（"it has no access to the original
prompt"）。三类裁决：

- **V1 `CONFIRMED_VALUE_MISSING`（blocking）**：spec 中 user-stated /
  answer-confirmed 的数值在代码中找不到；
- **V2 `UNSUPPORTED_LITERAL`（warning）**：代码尺寸类调用中的数字在 spec 中
  无出处（处理了 radius/diameter 对偶与结构常数 0/1/90/180…）；
- **V3 `ASSUMED_FIELD_USED`**：agent-assumed 的值被用进代码 → 不判错，
  列入 `assumed_to_reclarify`，经 `build_reclarify_request` 回灌 Clarifier。

即它是**内部一致性检查**（需求符合性），不是**正确性检查**（对 GT）。

一个具体例子：spec 为 plate 100×40×10（user-stated/answer-confirmed）+
hole diameter 5（answer-confirmed）+ hole depth 8（agent-assumed），代码是
`.box(100,40,10).hole(6).chamfer(3)`：V1 抓到 5 缺失（blocking），V2 抓到
6 和 3 无 spec 出处（warning）。

**与原评估/视觉自检的区别**：

| 方法 | 信号 | GT 依赖 | 部署可用 |
|---|---|---|---|
| Chamfer（原系统 Step 4 后） | 零件与标准答案 mesh 像不像 | 要 GT mesh | 否 |
| CADCodeVerify 式视觉自检 | VLM 看自己的渲染图主观判断 | 无客观参照 | 是但弱 |
| spec-verifier | 代码数字与用户确认值逐字段核对 | 不需要 | **是** |

注意实验环境下有"间接 GT"：Q&A 由拿 original prompt 的 User Simulator 回答，
所以 confirmed 值恰好等于 GT——但 verifier 只读 spec；真实用户答错时它验证的
是"代码是否忠于错误但被确认的需求"。

**明确的能力边界**（数字全对 ≠ 几何正确，V1/V2/V3 均漏检）：

1. 值对、操作错（`.hole(5)` 变成 `.circle(5).extrude(10)`，孔变凸台）；
2. 值对、位置错（孔打错面、坐标偏移）；
3. 值对、方向错（+Z 拉伸写成 -Z）；
4. 变量传值（`r=2.5; .hole(r*2)` AST 收不到常量，当前会 V1 误报）；
5. 同数值张冠李戴（plate 厚度 10 与孔深 10 混用）。

非数字事实（1/2/3/5）由可选 LLM 语义通道补，可靠性低于确定性数值通道。

---

## 7. 评估自证问题：spec 符合率不能作为最终指标

**核心担忧**：用"spec 符合率"评估一个按 spec 修正代码的闭环，指标几乎必然
上升——重生成的目标就是让代码出现 spec 的数字，再用同一检查器验收，属于
自证循环（setting the metric）；极端情况下 Coder 可以"应试"（把 spec 数字
塞进无用变量），检查全过但几何错误。

因此：

- **spec 符合率只作过程指标（process metric）**，可放附录；
- **最终 claim 必须落在外部 GT 指标上**：Chamfer（均值/中位数 + 分档比例）、
  执行成功率、视觉 judge，沿用 ProCAD 原评估协议。
- 闭环的真正命题是：**"忠于规约"这个部署时可测的代理信号，是否与 GT 几何
  正确相关、且干预后能降低 CD。** 这是可证伪的实验，不是注定涨点的工程。

三种可能结果都有意义：

1. probe 先做相关性——dropped/carryover 样本的 CD 是否显著更差（motivation）；
2. 闭环干预后 CD 是否下降（method 的主证据）；
3. "spec 过但 CD 差"的样本比例必须报告——它界定方法边界（操作/位置/方向错误）。

另一个反向情形：当用户回答含噪声/错误时（answer-confirmed 值本身错），
代码越"忠实"CD 可能越差。相比 ProCAD 的 oracle 用户设定，**在噪声答案下
验证溯源 + 闭环是否仍 net positive** 是更强的实验。

### 建议的实验设计（堵住"自证"质疑）

1. **主表只认外部指标**（Chamfer / 成功率 / 视觉 judge），spec 符合率仅作分析；
2. **同预算对照**：
   - 单 agent baseline；
   - ProCAD 一轮澄清；
   - Spec Manager 无闭环 + 固定 1 轮额外提问（`--spec-extra-rounds 1`，
     排除"多问一轮就涨点"的解释）；
   - 完整 verifier 闭环；
   - 关键看 ④ 是否在**相同或更少提问轮次**下赢 ③；
3. **先相关性、后干预**：probe 证明缺陷率可观且缺陷样本 CD 更差；闭环实验
   证明修掉缺陷降低 CD，两张证据分开；
4. **报告四象限**：spec-fail & CD 差 / spec-fail 但 CD 好 / spec-pass 但
   CD 差 / 两者都好 的样本数，主动暴露第三象限；
5. **防"应试"**：V1 目前是全数字池匹配（偏宽松），可收紧为数字必须出现在
   尺寸类调用的参数位置，魔数/无用变量无法通过。

### 闭环接线时的路由区分（实现注意）

`build_reclarify_request` 当前把 V1 与 V3 拼成同一文本回灌，但二者性质不同，
接线时应分流：

- **V3（assumed）→ 回 Step 3.5 再澄清**：信息确实缺失，提问能增加信息；
- **V1（confirmed 值缺失）→ 不应再问用户**：信息已存在于 spec，是 Coder
  没听话，应带字段级诊断直接重跑 Step 4（grounded re-generation）；
- **V2（unsupported literal，warning）→ 同样进重生成**，指出无出处的值；
- 循环最多一圈，落盘每样本消耗的提问轮次与 `verification_round_1/2`。

---

## 8. 完整 spec–geometry verifier 下 Chamfer 还有意义吗？

结论：**有意义，但二者衡量不同的事，且在论文叙事中主次应当反过来。**

- Chamfer / Point Cloud distance：生成几何与**参考 GT 网格**的整体形状
  相似度——离线指标，需要 GT，用于论文中证明代理信号有效、跨方法可比；
- spec–geometry verifier：生成几何是否满足**用户确认过的需求**（尺寸、
  孔数、位置、特征）——部署时可测，是 claim 的未来价值所在。

论文叙事：部署指标是 spec/geometry 满足率；Chamfer 是离线验证"这个代理
指标与 GT 几何正确性相关"的标尺（见第 7 节）。即使有完整 verifier，Chamfer
仍不可完全替代，因为：

1. spec 只覆盖"说出来的需求"，没说的错误（unknown unknowns：多余加强筋、
   错误整体拓扑）spec 检查永远发现不了，整体相似度仍有兜底价值；
2. Chamfer 是与既有工作可比、跨方法统一排序的指标。

### 8.1 Chamfer 对欠规格样本不公平（重要）

用户只说"一个带孔的支架"，澄清后选定了合理但与数据集 GT 不同的尺寸：
spec 满足率满分，Chamfer 却因偏离 GT 而变差。ProCAD 原设定下 User Simulator
永远还原 GT 值，该问题不显现；但一旦做**噪声/错误答案 persona**实验，
确认值会系统性偏离 GT，CD 当唯一指标会得出错误结论。因此必须按歧义类型
分层报告：

- **conflict + 完整 spec 样本**：确认值 = GT，Chamfer 是公平主指标；
- **underspec + 合法偏离 GT 的确认**：主指标用 spec 满足率、关键参数误差，
  Chamfer 仅作参考；
- 可同时报两个 CD：对数据集 GT 的 CD（传统可比）与对"按确认参数重新生成
  的参考几何"的 CD（CadQuery 环境可行，是本研究独有的实验杠杆）。

### 8.2 与 CADCodeVerify（ICLR 2025）的核实对比

参考：Alrashedy et al., *Generating CAD Code with Vision-Language Models
for 3D Designs*, ICLR 2025；仓库 <https://github.com/Kamel773/CAD_Code_Generation>。

**仓库核实（2026-11-20 本地核查）**：仓库**只发布 CADPrompt 数据集，没有
验证循环代码**。方法细节应以 OpenReview 论文为准，引用时勿把 repo 当实现来源。

CADPrompt：200 条样本（simple/complex 分层），每条 7 个文件：
`Ground_Truth.stl/.obj/.json`（DeepCAD 来源网格 + CAD 命令序列）、专家手写
`Python_Code.py`（CadQuery）、以及**两个版本的 prompt**：

- 欠规格版，如 `"write a python code using CADQuery to create an extruded
  sketch of a circle."`
- 带测量版，如 `"...radius of 0.75 units and extrusion 0.20923 units high."`

数据集本身内置 underspecified / specified 对照，可直接用于分层评估。

论文方法（据论文正文，非 repo 代码）：

- 推理时**不靠 GT 网格**：VLM 根据描述生成一组二元验证问题，再看生成模型
  的多视图（约四视图）图像回答 Yes / No / Unclear；对 No/Unclear 生成
  ameliorative feedback 迭代修代码；编译失败另有代码修复循环；
- 离线评测仍用 GT 网格：Point Cloud distance、Hausdorff distance 与 IoU
  （注：网传说法中的 "IoGT" 应为 IoU，引用前以论文 metrics 节为准）；
- 论文自述点云距离/Hausdorff 的盲区：桌腿与桌面间有小缝时整体距离仍可能
  很低——整体距离指标对细微结构差异不敏感。

**本研究与它的根本差异**：

1. CADCodeVerify 的验证问题和答案都由同一个 VLM 产生（问题端、答案端均可
   幻觉），本质是无客观锚点的视觉自我一致性检查；本研究锚定在**带来源溯源
   的结构化 spec** 上，每个 confirmed 值有用户原文证据，是客观的需求符合性
   检查。
2. CadQuery 真实执行产出的是 **B-rep 实体**，可用 OCCT 内核做确定性测量
   （圆柱面半径、拉伸深度、孔数、面/边计数、包围盒、体积），比"看图问答"
   精度与可信度高一个量级；CADCodeVerify 只有渲染图，本研究有实体。

### 8.3 建议的三层 verifier（吃掉 AST 层的漏检边界）

| 层 | 手段 | 成本 | 能抓 / 抓不住 |
|---|---|---|---|
| L1 AST 数字核对（已实现） | 静态解析，不执行 | 最低 | 抓值缺失/臆造/assumed；抓不住操作、位置、方向错与变量传值 |
| L2 B-rep 参数实测（建议新增） | 执行 CadQuery，用 OCCT 查拓扑/面/实体参数 vs spec | 中（CPU） | 抓"数字对但操作错"（孔变凸台：subtract vs add 反映在面数/体积）、方向、特征计数、实测半径/深度 |
| L3 VLM 视觉通道（可选） | 多视图渲染问答 | 最高（主观） | 处理前两层覆盖不到的语义/外观问题；定位同 CADCodeVerify，但问题应源自 spec 字段而非自由生成 |

L2 还提供比 L1 更硬的相关性证据：若"实测几何参数 vs spec 的偏差"能预测
Chamfer 变差，则 spec-grounded 代理指标在离线实验中站稳，部署时又不需要
GT——这是相对 CADCodeVerify 的核心增量。
