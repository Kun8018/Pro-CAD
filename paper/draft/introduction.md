# 1. Introduction（草稿 v0.1）

> 写作约定：`[Cx]` 对应 PLAN.md 的 claim；`[RESULT: …]` 为待填实验数字；
> `[@key]` 为参考文献占位。语言先写英文（投稿语言），关键段落附中文意图说明。

---

Text-to-CAD systems turn natural-language descriptions into executable parametric
programs—typically CadQuery or construction-sequence code that is rendered into
boundary-representation (B-rep) geometry [@text2cad; @llm4cad; @deepcad]. The
promise is a conversational interface to mechanical design; the reality is that
users' descriptions are almost always incomplete, occasionally self-contradictory,
and rarely stated at the level of precision a parametric program requires.
Recent work addresses this with *proactive clarification*: an agent interrogates
the prompt before a second agent writes code [@procad]. ProCAD demonstrates that
a single round of questions, answered by an oracle user simulator, can recover
much of the geometry lost to underspecified or conflicting prompts.

Yet the two agents in this pipeline never share a structured state. Their **only
handoff is a single piece of free text**—a "standardized prompt" rewritten by the
clarifier. Whatever the question-answer exchange drops or invents is invisible to
the coder; whatever the coder ignores or improvises is invisible to everyone.
There is no execution-time feedback path: generated code is evaluated only
offline, against ground-truth meshes that do not exist when the system is
actually deployed.

That this is not merely an aesthetic concern is suggested by two recent findings
from complementary angles. In verification-driven code generation, vision-
language models that inspect renders of their own CAD output can catch some
geometric errors iteratively [@cadcodeverify]—but their validation questions and
answers are both produced by the same model looking at an image, with no
grounded reference to what the user actually confirmed. On the human side,
Treellis [@treellis] shows that exposing the *structure* of a generated model
lets users edit locally instead of regenerating the whole object—yet only
**32 of 50** generated models were in an editable state on the first pass
(43 after adding exemplars and structural constraints). Exposing structure
helps; the structure itself remains ungrounded and unverified.

**We argue that the missing object in this pipeline is a provenance-tracked
specification of design intent**, sitting between the user and the coder:
a structured feature graph in which every parameter records *where it came from*
—stated by the user, confirmed in a clarification answer, silently assumed by
the system, or derived from a constraint—and against which the generated program
can be checked **without any ground-truth geometry**.

Concretely, we introduce a **Spec Manager** agent and a deterministic
**spec-grounded verifier**, and close the loop between them:

1. **Structured, provenance-tagged specification.** Instead of a rewritten
   prompt, the Spec Manager maintains a feature/parameter/constraint graph.
   Clarification answers override earlier statements under an explicit
   provenance ranking; every override is logged. Parameters the model fills in
   without user support are marked as assumptions rather than silently inserted.
   A deterministic kernel—not the LLM—owns merging, conflict resolution, and
   the detection of unresolved fields, so these invariants are testable without
   an API call. [C2]

2. **Ground-truth-free verification at deployment time.** The verifier aligns
   generated CadQuery code back to the *specification the user confirmed*, not
   to a reference mesh. A static layer parses the program without executing it
   and flags confirmed values missing from code, dimensional literals with no
   source in the spec, and assumed values realized in code; a dynamic layer
   measures the executed B-rep directly (radii, depths, feature counts,
   topology) to catch the failure class numeric matching cannot—correct values
   with wrong operations, such as a hole that became a boss. [C4]

3. **A closed loop with differential routing and a question budget.** A missing
   *assumption* is routed back to the user as a targeted question; a confirmed
   value the coder dropped is routed forward with a field-level diagnostic for
   grounded re-generation—the user is never re-asked what they already said.
   Triggered only on demand, the loop improves geometry under the **same or
   smaller question budget** than an unconditionally extra clarification round.
   [C5]

We further show the behavior is reproducible without a proprietary model: the
Spec Manager is distilled into Qwen2.5-7B with training targets canonicalized by
the deterministic kernel, so the student cannot learn malformed schemas from the
teacher [C7].

In a controlled re-creation of the ProCAD benchmark—underspecification and
direct-conflict prompts with severity K = 1..3—we find that free-text handoffs
lose or invent information in [RESULT: C1 rate]% of clarified samples, that such
defects correlate strongly with degraded Chamfer distance [RESULT: correlation],
and that the provenance spec reduces them to [RESULT: C2 rate]% while improving
mean Chamfer distance from [RESULT: E1 CD] to [RESULT: E3 CD] at
[RESULT: rounds] average questions. Crucially, verifier verdicts predict
ground-truth geometric failure with [RESULT: precision/recall], establishing the
deployment-time signal as a valid proxy; and under imperfect (noisy or wrong)
user answers—conditions the oracle-simulator evaluation cannot examine—our
pipeline degrades more gracefully [RESULT: C6].

The central conceptual move is a separation the current literature conflates:
*specification fidelity* (does the program realize what the user confirmed?)
from *geometric similarity* (does the output resemble a reference mesh?).
Similarity needs ground truth and is available only in evaluation; fidelity can
be measured every time the system runs. Treellis gives humans an editable
geometric tree after generation; we give the agents a grounded, traceable
intent graph before generation and the means to check it afterward, making the
two views—an editable tree for people, a verified specification for agents—
natural complements rather than competitors.

---

### 本段的论证链（给作者的备忘，定稿删除）

1. 领域背景：text-to-CAD 是生成可执行参数化程序；用户输入天然不精确。
2. 已有进展（ProCAD）：主动澄清有效 → 不否认前人，站在其肩上。
3. 转折：两 agent 唯一交接是自由文本；无结构化状态、无部署时验证。
4. 两个外部动机：CADCodeVerify（验证无锚点）+ Treellis（结构暴露有用但
   64% 首过可编辑 → 结构本身不可靠）。HCI 数字只用 32/50 这一条技术相关
   数据；完成率/SUS 不进正文或仅在 related work 一句。
5. 论点 + 三个技术构件（与 claim 表一一对应）。
6. 开源模型可复现（挡"靠 gpt-4o-mini"的质疑）。
7. 结果段全部占位，按 C1–C7 顺序预告。
8. 概念贡献：fidelity vs similarity 的区分；与 Treellis 的互补定位。

### 待确认

- 标题候选：*Grounded Before Drawn: Provenance-Tracked Specifications and
  Ground-Truth-Free Verification for Text-to-CAD Agents*
- 是否把 Treellis 放 Introduction 还是只放 Related Work：当前选择放一句
  在 Intro（64% 缺口证据很有力），用户实验数字不放。
- C6（噪声用户）是否进主结果或放补充：若工期紧先做附录，但建议进主表——
  这是相对 ProCAD oracle 设定最独特的实验。
