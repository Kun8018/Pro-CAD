# Spec Manager 的千问微调方案

目标：把当前基于 prompt 的 gpt-4o-mini Spec Manager（`src/spec_manager.py`）
替换/对照为开源 Qwen SFT 模型，回答两个审稿问题：(1) 能力来自 schema/确定性
内核的设计，还是来自 gpt-4o-mini？(2) 本地小模型能否承担结构化规格管理？

底座与现有两个模型保持一致：**Qwen/Qwen2.5-7B-Instruct**。

---

## 1. 训练对象：Spec Manager 的三个 LLM 技能

确定性逻辑（normalize / merge / unresolved 检测 / render / 冲突裁决）**不进
训练**，它们是推理时的“法律”，教师和学生输出都受其约束。需要 SFT 的只有
三处 LLM 调用：

| 技能 | 输入 | 目标输出 | 对应方法 |
|---|---|---|---|
| E1 抽取 | 单段描述（可能含错误/缺失） | 完整 spec JSON，所见即所标 | `extract_messages` |
| E2 更新 | 当前 spec + Q&A 文本 + unresolved | 更新后的完整 spec JSON | `update_messages` |
| E3 提问 | spec + unresolved 字段 | `{"questions": [...]}`，每问对应一个 unresolved 字段 | `questions_messages` |

## 2. 核心原则：标签由确定性内核 canonicalize，不用教师原文

教师（gpt-4o-mini / gpt-4.1）负责“读懂文本”，但训练标签这样构造：

1. E1 标签 = `normalize_spec(teacher_extract(misleading))`
   —— 非法 source 被降级、裸值被补 entry、id 补齐；
2. **E2 标签 = `merge_specs(spec1, normalize_spec(teacher_update(...)))`**
   —— 不是教师的原始 JSON，而是把教师更新合并进 E1 状态后的确定性结果。
   由此教给学生的输出天然满足溯源排序、resolutions 日志、id 稳定、
   不丢特征等全部内核不变量；
3. E3 标签：教师问题通过字段覆盖校验，失败时回退到
   `template_questions(unresolved)`（保证每问必有结构依据）；
4. 所有 assistant 目标都是可 `json.loads` 的 JSON（无 markdown 围栏）。

这等价于用确定性内核给教师蒸馏做了一层“形式化去噪”，学生不可能学到非法
schema（而单纯蒸馏 gpt-4o-mini 会把它的格式错误也学过去）。

## 3. 数据构造（`to_sft_spec_manager.py`）

输入：`dataset/selected_misleading_samples_train`（含 original / misleading /
questions_to_ask / answer_to_questions / what_changed）与
`dataset/improved.json`（干净样本）。

### 3.1 每条误导样本的生产流程

```text
misleading_description ──teacher E1──► spec1 ──normalize──► 标签1
original_prompt       ──teacher E1──► spec_orig（仅用于过滤）
questions_to_ask + answer_to_questions ──teacher E2──► upd
spec2 = merge_specs(spec1, normalize(upd))             ◄── 标签2
unresolved_fields(spec1) ──teacher E3（失败用模板）──► 问题标签
```

### 3.2 确定性质量过滤（不通过则丢弃，对应 ProCAD 的 CD<0.0002 门槛思路）

- **解析过滤**：spec1 / upd 必须可解析；
- **答案落实过滤（最重要）**：spec2 必须覆盖 spec_orig 的全部确认数值
  （按参数名 + mm 容差匹配，复用 `_as_mm`），否则说明更新没把正确答案
  合进去；
- **防教师作弊（conflict 类）**：spec1 必须保留误导值——spec_orig 中至少
  一个数值在 spec1 中找不到（教师若“心善地”提前纠错，样本作废）；
- **更新生效过滤**：spec2 相对 spec1 至少有一条 resolution 或新增参数，
  且至少一个 `answer-confirmed` 字段，否则这轮 Q&A 没学到东西；
- **提问落地过滤**：unresolved 非空时才允许产生问题标签，问题数不超过
  unresolved 字段数（+1 容差）。

### 3.3 样本配比

- conflict K=1/2/3、underspec K=1/2/3：全部保留，按 config_name 均衡；
- 干净样本（k=0，取自 improved.json）：只做 E1，且 unresolved 必须为空、
  问题标签为 `[]` —— **专门教模型不要见什么都问**；
- 建议干净样本上限 ≈ 误导样本数（`--clean-ratio 1`），防止干净样本淹没
  纠错能力；
- 每条误导样本产出两条 sharegpt 记录：(E1+E2 多轮) 与 (E3 单轮)；
- 规模参考：误导样本数千条 × 2 + 干净样本数千条 ≈ 1–2 万条会话；
  教师每样本 3 次调用（mis 抽取 / orig 抽取 / 更新），gpt-4o-mini 成本为
  几美元量级；脚本支持断点续跑（jsonl 逐条落盘）。

## 4. 训练配置

两份配置都在 `training/configs/`：

### 4.1 多卡全参（论文主配方）：`qwen2-5_full_sft_spec_manager.yaml`

对齐 clarifier 配方：Qwen2.5-7B-Instruct，full + DeepSpeed ZeRO-3，bf16，
lr 1e-5 cosine warmup 0.1，2 epochs，sharegpt，cutoff_len 8192（spec JSON +
多轮 Q&A 较长），per_device 2 × grad_accum 4，`train_on_prompt: false`、
`mask_history: false`（E1 与 E2 两轮 assistant 都参与损失）。

### 4.2 单机 12GB（3080）：`qwen2-5_qlora_spec_manager.yaml`

QLoRA 复现配方：`finetuning_type: lora` + `quantization_bit: 4`，
lora_rank 32 / alpha 64，target all linear，gradient_checkpointing，
per_device 1 × grad_accum 16，lr 1e-4，cutoff 4096，2 epochs，bf16。
峰值显存约 10–12GB。LoRA 产物作为“开源可复现”结果，论文主数字仍以全参为准。

### 4.3 上线方式

无需改代码：`LLM` 对非 API 模型名自动走 HF transformers 加载：

```bash
python pipeline.py --use-spec-manager \
  --spec-manager-model saves/qwen2_5-7b/sft_spec_manager/full
```

## 5. 评估协议（训得好不好怎么判）

分层验证，先离线后端到端：

1. **格式层**：spec JSON 合法率（可 parse 且过 normalize）、非法 source 率；
2. **内核一致性**：学生 E2 输出经 merge 后与教师 canonical 标签的字段级
   一致率（参数值 / provenance / 不丢特征率）；
3. **信息保真**：直接复用 `probe_spec_fidelity.py`——在同一批
   pipeline 结果上比较 dropped / unsupported / carryover 三组率，
   四个系统横评：ProCAD Clarifier（原版）/ gpt-4o-mini Spec Manager /
   SFT-7B Spec Manager / 无溯源消融（`--spec-no-provenance`）；
4. **提问质量**：E3 问题对 GT 问题的 F1（复用
   `src/evaluation.py:calculate_question_score`），并报告平均提问数；
5. **端到端**：台式机全量 pipeline 的 Chamfer、执行成功率（需 coder+mesh）；
6. **成本表**：参数量、每条样本 token/延迟、是否需要 API。

主对比必须在**相同提问轮次预算**下进行（`--spec-extra-rounds` 对齐），
否则“多问就涨点”无法排除。

## 6. 实施步骤清单

1. 台式机/任意带 API key 的机器：`hf download BBexist/ProCAD` 取 train JSON；
2. `python to_sft_spec_manager.py --train_json dataset/selected_misleading_samples_train \
      --improved_json dataset/improved.json --output training/data/spec_manager_sft.json`
   （先 `--max-samples 20` 抽检标签质量）；
3. 合并 `training/dataset_info.json` 的 `spec_manager_sft` 条目到
   LLaMA-Factory；
4. 多卡跑 full 配置；3080 跑 QLoRA 配置；
5. 按第 5 节顺序评估，先过格式/一致性，再跑 probe，最后端到端 CD。
