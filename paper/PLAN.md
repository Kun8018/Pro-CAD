# 论文计划（SIGGRAPH / ACM TOG 投稿）

> 状态：规划稿。所有数字用 `[RESULT: …]` 占位，实验未完成前不得替换为臆造值。
> 代码/实验事实以 `feat/spec-fidelity-probe` 分支为准；设计背景见仓库根目录
> `THIRD_AGENT_IDEA.md`、`JUDGE_AMBIGUITY_RESOLUTION.md`、`CONTRIBUTIONS_AND_WORKLOAD.md`。

## 1. 定位

- **Venue**：ACM SIGGRAPH / TOG（技术类）。主张必须是几何生成管线的技术贡献，
  交互/可用性只能作为动机，不能作为证据（本文不做用户研究）。
- **一句话**：We replace the single free-text hand-off in clarify-then-generate
  text-to-CAD pipelines with a provenance-tracked structured design specification
  maintained by a Spec Manager agent, and a deployment-time, ground-truth-free
  spec-grounded verification loop that routes failures back to clarification or
  grounded re-generation.
- **对 Treellis（UIST'26）的使用**：仅作 motivation + related work。其 32/50
  首过可编辑率（64%，加约束后 43/50）是"结构化输出本身不可靠、无验证"的缺口
  证据；用户实验数字（98.1% vs 85.2%、SUS）只一句带过，不作技术依据。

## 2. 核心 Claim 与所需证据（claim–evidence 表）

| # | Claim | 证据 | 实验/分析 | 状态 |
|---|---|---|---|---|
| C1 | 自由文本交接存在可观的信息丢失/臆造/错误值存活 | probe 三缺陷率（按 underspec K / conflict 分组） | `probe_spec_fidelity.py` 数值+LLM 双通道；人工抽检 precision | 脚本就绪，待数据 |
| C2 | 带溯源的结构化 spec 显著降低交接缺陷率 | 同批样本 spec 模式 vs ProCAD 文本模式的缺陷率对比 | pipeline `--use-spec-manager` vs 原版；probe 直接复用 | 待跑 |
| C3 | 降低缺陷率可改善几何质量 | Chamfer/成功率提升，且缺陷与高 CD 相关 | 四系统 × 三条件 CD；缺陷/CD 四象限相关分析 | 待台式机 |
| C4 | 无 GT 的 spec-grounded 验证能预测几何失败（代理信号有效性） | verifier 裁决 vs Chamfer 的混淆矩阵/相关性 | AST 层 + B-rep 层；precision/recall 相对 CD 阈值 | verifier AST 已实现；B-rep 层待做 |
| C5 | 闭环（按需再澄清/重生成）在**相同提问预算**下优于无差别多问一轮 | 固定轮次预算对照（见实验矩阵） | E3 vs E2 | 闭环未接线 |
| C6 | 对不完美用户（错误/部分回答）比原 oracle 设定更鲁棒 | persona/噪声回答下的 CD 与 spec 满足率 | 需扩展 User Simulator | 待做 |
| C7 | 开源 7B SFT 可承担 Spec Manager，能力来自设计而非封闭大模型 | gpt-4o-mini 教师 vs SFT Qwen2.5-7B 学生的指标对比 | `to_sft_spec_manager.py` + 两份训练配置 | 数据/训练待台式机 |

**自证防线**：spec 符合率只作过程指标；C3/C4 必须用外部 GT 指标（Chamfer/
Hausdorff/成功率）证明代理信号与几何正确相关。必须报告"spec 过但 CD 差"象限。

## 3. 与近邻工作的差异化表

| 工作 | 澄清 | 中间表示 | 验证信号 | 闭环 | GT 依赖 |
|---|---|---|---|---|---|
| Text2CAD / DeepCAD | 无 | CAD 命令序列（生成目标） | 无 | 无 | 训练数据 |
| ProCAD (ICML'26) | 有（一轮，oracle 用户） | **自由文本** standardized_prompt | 仅离线 Chamfer | 无 | GT mesh |
| CADCodeVerify (ICLR'25) | 无 | 代码 + 渲染图 | VLM 自问自答（无锚点视觉自检） | verify→改代码 | 推理时无；验证主观 |
| Treellis (UIST'26) | 无 | 几何装配树 AML（给人编辑） | 无（32/50 可编辑） | 人在环手动编辑 | 无 |
| CAD-Editor (ICML'25) | 无 | locate-then-infill 编辑 | 重建指标 | 编辑（无澄清） | GT |
| **Ours** | **主动、多轮、按需** | **带来源溯源的意图 spec** | **AST+B-rep 对 spec 客观核对** | **V3→再澄清 / V1,V2→带诊断重生成** | **推理时无；离线用 GT 仅做评测** |

## 4. 方法组成（与代码对应）

1. Spec Manager agent（`src/spec_manager.py`）：feature/参数/约束 schema，
   四级 provenance，确定性 provenance-ranked merge + resolutions 日志，
   unresolved 字段检测驱动提问，Coder hand-off 渲染（带/不带溯源 = 消融）。
2. Spec-grounded Verifier（`src/spec_verifier.py`）：
   - L1 AST 数字核对（已实现：V1 missing / V2 unsupported / V3 assumed）；
   - L2 **B-rep 参数实测（待实现）**：执行 CadQuery，用 OCCT 测半径/深度/
     特征计数/体积/包围盒，抓"值对操作错"（孔变凸台、方向错）；
   - L3 可选 VLM 语义通道（问题由 spec 字段产生，非自由生成）。
3. 闭环编排（待接线）：V3→Spec Manager 再提问；V1/V2→带字段诊断重生成；
   最多一圈；记录每样本提问轮次。
4. SFT Spec Manager（`to_sft_spec_manager.py`）：教师蒸馏 + 确定性内核
   canonicalize 标签；过滤含 r/d 对偶；全参 + QLoRA 配方。

## 5. 实验矩阵

系统（同 Coder、同测试集）：
- E0 单 Coder（misleading prompt 直出）
- E1 ProCAD 一轮澄清（oracle User Simulator）
- E2 Spec Manager 无闭环 + 固定 1 轮额外提问（预算对照）
- E3 完整闭环（verifier 按需触发）
- E4 E3 的无溯源消融（`--spec-no-provenance`）
- E5 SFT-7B Spec Manager 替代 gpt-4o-mini（C7）

数据维度：underspec K=1,2,3 × conflict K=1,2,3 × clean(k=0)；
用户条件：oracle / novice（近似值、省略）/ confused（错误值）。

指标：
- 几何：Chamfer（主，对齐 ProCAD 协议：8192 点、双向、归一化）、Hausdorff
  （补 CADCodeVerify 式细结构敏感性）、CADFS 协议的 **NC（normal consistency）
  与 ECD（edge Chamfer distance）**——ECD 对 CADCodeVerify 承认的"小缝/细结构
  盲区"敏感，能检验"值对但操作错"的样本；体积 IoU（CADFit/StepCAD 口径）作为
  辅助；执行成功率（含编译失败/几何失败分类，对齐 CADFS 的 _c/_r 三分类）；
- 代理：V1/V2/V3 触发率、spec 符合率（仅过程指标）、verifier 对 CD 失败的
  precision/recall；
- 交互成本：平均提问轮次/Token；
- 按歧义类型分层报告；underspec 合法偏离 GT 的情形补充"对确认参考几何"的 CD。

## 6. 章节结构（TOG 长文）

1. Introduction（草稿：`draft/introduction.md`）
2. Related Work：text-to-CAD；clarification/proactive agents；structured
   intermediate representations（Treellis、Feature Script、CAD-Editor）；
   verification of generated programs（CADCodeVerify、program alignment）
3. Problem Formulation：三条件设定、符号、为什么自由文本交接是瓶颈（C1 数据）
4. Method：4.1 Spec schema & provenance；4.2 Spec Manager（merge/unresolved/
   提问）；4.3 Coder 交接；4.4 三层 Verifier；4.5 闭环路由
5. Implementation & Training：教师蒸馏 + canonical labels + SFT
6. Evaluation：设置、主表（几何）、代理指标验证、预算对照、消融、鲁棒性、
   成本、局限性
7. Discussion：与可编辑结构树（Treellis 式 UI）的结合、B-rep 层边界、
   unknown-unknowns、合法多解下 Chamfer 的局限
8. Conclusion

## 7. 工程 TODO（投稿前必须完成）

- [ ] 台式机跑 `pipeline.py --skip-codegen` 全量 → probe 出 C1 数字
- [x] verifier 已知问题修复：V2 对偶限定 radial 上下文；变量传值解析
- [x] B-rep 实测层 L2（`src/spec_verifier_l2.py`，cadquery 惰性导入）+ 单测
      （比较逻辑离线可测；真实测量需台式机 cadquery/OCP）
- [x] probe LLM 通道 rows 对齐 bug 修复；`spec_no_provenance` 消融解耦
- [x] 闭环路由核心（`src/spec_loop.py` route/诊断/LoopBudget）+ 单测；
      pipeline Step5 已接 L1+L2 合并 + route + 诊断
- [ ] 闭环**重调用**接线：route=regenerate → 用诊断再调 coder；
      route=reclarify → 再走一轮 Q&A（C5 端到端）
- [ ] SFT 数据生成 → 全参训练 → C7 对照
- [ ] 噪声用户实验（C6，User Simulator 加 novice/confused persona）
- [ ] 人工金标准：~100 条缺陷标注 + verifier 裁决抽检
- [ ] 论文补图：目前仅 Fig.1 overview；缺生成/验证过程图（spec schema 示例、
      provenance merge 例、verifier L1/L2/L3 分层、闭环路由分流）
