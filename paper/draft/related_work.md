# 2. Related Work（草稿 v0.1）

> 引用键 `[@…]` 为待建 BibTeX 条目；带 `[VERIFY]` 标注的主张需在通读原文后
> 再收紧措辞。中文备忘在文末，定稿删除。

## 2.1 Text-to-CAD: from command sequences to executable programs

Learning-based CAD generation was first formulated as sequence modeling:
DeepCAD represents models as sketch-and-extrude command sequences and trains a
generative network over them [@deepcad], and Text2CAD pairs such sequences with
natural-language descriptions at multiple levels of expertise [@text2cad]. The
shift to large language models recast CAD generation as *program synthesis*:
LLM4CAD emits a Python-based domain-specific language [@llm4cad], Text-to-CAD
aligns parametric data with language via a vision-language model
[@texttocad_vl], and CAD-GPT augments multimodal LLMs with spatial-reasoning
prompts to synthesize construction sequences [@cadgpt]. More recent systems
generate directly executable CadQuery programs and execute them to obtain
B-rep geometry [@procad; @cadrille].

Two trends in this line are directly relevant to ours. First, **structured
intermediate tokens help**: FlexCAD achieves controllable generation and
partial editing through hierarchy-aware masking of structured CAD text
[@flexcad], and Pointer-CAD unifies B-rep and command sequences with explicit
pointer-based edge/face references [@pointercad]—structure in the representation
buys controllability that raw free text cannot. Second, **visual and geometric
feedback during training helps the generator**: CADFusion alternates between
learning a visual-feedback model and fine-tuning the LLM on that feedback
[@cadfusion], while FutureCAD grounds generated programs onto text-described
B-rep primitives and uses the grounding signal as an RL reward [@futurecad].
Crucially, both use feedback as a *training-time* signal over the generator; at
inference time the user's specification is still consumed once, as free text,
with no record of which values were stated, assumed, or corrected.

Executable Python programs rather than opaque token vocabularies are now the
preferred target representation as well: CADRecode reverse-engineers point
clouds directly into CadQuery scripts with a small LLM and a million-program
synthetic corpus [@cadrecode], and CADFS collects a million-scale corpus of
*real* FeatureScript programs with complete modeling histories, executes them
through a CAD kernel to obtain B-reps, and evaluates on Chamfer, normal
consistency, and edge Chamfer distance (CD/NC/ECD) [@cadfs]; CAD-Llama adapts
open-weight LLMs to the same program-synthesis setting [@cadllama]. We adopt
this executable-program view—and CADFS's multi-metric evaluation, including
edge-aware distances that expose fine structural defects—but generate forward
from language while tracking where every required value came from.

Multimodal unified representations such as CAD-MLLM [@cadmllm] and
general-purpose CAD agents that combine tools with VLMs [@caddesigner;
@cadassistant] broaden the range of solvable tasks, but do not address the
provenance and verification of the design requirements themselves—the gap we
target.

## 2.2 Proactive clarification of user intent

Rather than forcing a single prompt to carry every requirement, conversational
and proactive agents ask before acting. This idea appears in task-oriented
dialogue and information-seeking agents more broadly [@clarify_general], and in
CAD it is instantiated by ProCAD [@procad]: a Clarifier detects underspecified
or conflicting prompts and asks targeted questions before a separately trained
Coder produces CadQuery. Misleading prompts—parameters removed or duplicated
with contradictory values—are synthesized at controlled severity, and an oracle
user simulator answers from the ground-truth description. We build directly on
this problem formulation and reuse its benchmark protocol.

Our analysis, however, identifies a structural weakness: the two agents exchange
a single rewritten description with no schema, no per-parameter provenance, and
no post-generation check. Information lost in that rewrite is unrecoverable
downstream, and a wrong value carried through clarification is undetectable
without the ground-truth mesh the benchmark happens to provide. We replace the
free-text hand-off with a provenance-tracked specification, make the answering
user explicitly imperfect in our experiments, and add a verification loop the
original pipeline lacks.

## 2.3 Structured and editable intermediate representations

Exposing intermediate structure has a long history in procedural and
constructive-solid-modeling systems, where CSG trees and feature histories make
geometry inspectable and locally editable [@csg; @featurescript; @openscad].
The recent generative literature returns to this idea: CAD-Editor locates a
target span in CAD text and re-infills only that span, avoiding full
re-generation for edits [@cadeditor]; FlexCAD's hierarchy masking similarly
preserves non-target parts [@flexcad].

Most directly related is Treellis [@treellis], which has an LLM emit an assembly
modeling language (AML)—a CSG-style tree of primitives, boolean and repetition
nodes with editable parameters—and presents the tree, viewport, and parameter
panel as linked, editable views; a scoped edit sends only the selected subtree
to the model. In a 24-participant study, structured editing largely eliminated
the "regenerate everything and watch unrelated parts change" loop that plain
prompting produces. We take this as strong motivation that users and agents
alike need an intermediate object between language and geometry.

But Treellis's own measurements expose the other half of the problem: only 32
of 50 generated trees were editable on the first pass (43 with exemplars and
structural constraints), because nothing verifies that the tree faithfully
captures the intent or that its parameters are complete. Their tree is a
*geometric* structure revealed after generation for a human to repair; our
specification is an *intent* structure built before generation, with every
value attributed to a source, and checked by the agent itself without a human
in the loop. The two are complementary downstream: a provenance-tagged
specification is a natural authoring layer for an editable assembly tree.

## 2.4 Verifying and repairing generated programs

Self-verification of LLM outputs ranges from consistency sampling
[@selfcheckgpt] and self-refinement [@selfrefine] to execution-based repair,
where compiler/runtime feedback drives another generation attempt; in CAD,
cadrille uses online RL with execution feedback [@cadrille]. The closest
system, CADCodeVerify [@cadcodeverify], prompts a VLM to (i) generate binary
validation questions from the description, (ii) answer them by looking at
multi-view renders of its own generated object, and (iii) rewrite code from
the resulting feedback, with a separate repair loop for compilation failures.

This verifies geometry *visually and self-referentially*: the questions, the
answers, and the judged render all come from the same model, with no grounded
reference, and holistic image questions are insensitive to local defects (a
small gap between two solids barely moves image-level judgments—an acknowledged
failure of holistic visual checking). SEEK-CAD applies the same render-and-
critique loop with chain-of-thought reasoning on open-weight models [@seekcad],
and ReCAD improves the generator with reinforcement learning using visual
feedback as reward [@recad]; both inherit the unanchored, holistic nature of
the visual signal.

A second family validates programs against geometry through the CAD kernel
itself, but in the *reverse-engineering* setting where a target mesh is
available: CADFit casts mesh-to-CAD recovery as volumetric-IoU-guided
optimization over executable programs, pruning operations whose marginal IoU
contribution is non-positive and nearly eliminating invalid programs
[@cadfit]; StepCAD conditions an LLM policy on the current intermediate
geometry and performs test-time tree search with local program edits under an
IoU objective [@stepcad]. Kernel-executed validation and per-step geometry
conditioning are close in spirit to our dynamic B-rep checks, but their
reference signal is the *target mesh being reverse-engineered*—ground truth
that exists offline in reconstruction and never exists when generating from a
user's description.

Our verifier is anchored instead to the explicit set of user-confirmed
parameters and is performed in two deterministic channels—static alignment of
program literals to specification fields, and direct measurement of the
executed B-rep (radii, depths, feature counts, topology)—with a vision channel
reserved for semantics the measurements cannot cover. Failures are routed by
cause: an unconfirmed assumption triggers another clarification question,
while a confirmed value the coder dropped triggers a diagnostic-grounded
regeneration rather than another question. This is closer in spirit to
specification- and property-based program verification [@propertytesting]
than to visual self-critique, and it requires no ground-truth mesh at
inference—the reference geometry that offline Chamfer evaluation depends on
[@chamfer_evals].

## 2.5 Positioning

Table: positioning against the axes that matter (also in PLAN.md §3).

| System | Intent clarification | Hand-off representation | Deployment-time verification | Closed loop | Ground-truth needed at inference |
|---|---|---|---|---|---|
| Text2CAD / DeepCAD | – | command sequence | – | – | – (generation only) |
| ProCAD | one round, oracle user | free text | – | – | no |
| CADFusion / FutureCAD | – | structured text / programs + B-rep grounding | training-time signal | training loop | no |
| CADCodeVerify | – | code + renders | visual self-Q&A (unanchored) | verify→rewrite | no |
| CAD-Editor / FlexCAD | – | editable structured text | – (partial editing) | – | – |
| CADCodeVerify / SEEK-CAD | – | code + renders | visual self-Q&A or CoT critique (unanchored) | verify→rewrite | no |
| CADFit / StepCAD (mesh→CAD) | – | executable programs | kernel/IoU vs. **target mesh** | search/optimize | **yes (reverse-engineering input)** |
| CADFS | – | FeatureScript programs | executable eval (CD/NC/ECD, offline) | – | – (generation only) |
| Treellis | – | geometric assembly tree (for humans) | – | human edits | no |
| **Ours** | **on-demand, multi-round, noisy-user-aware** | **provenance-tracked intent spec** | **AST + B-rep aligned to the spec (no GT)** | **clarify / regenerate by failure type** | **no** |

---

## 写作备忘（定稿删除）

- **引用待建**（建议 bib key → 对象）：
  deepcad(Wu ICCV'21), text2cad(Khan NeurIPS'24), llm4cad, texttocad_vl,
  cadgpt(AAAI'25), cadmllm, caddesigner(arXiv:2508.01031), cadassistant,
  cadrille(ICLR'26), cadfusion(ICML'25, arXiv:2501.19054), futurecad(ICML'26
  Spotlight, arXiv:2603.11831), pointercad(arXiv:2603.04337), flexcad(ICLR'25,
  arXiv:2411.05823), cadeditor(ICML'25, arXiv:2502.03997), procad(arXiv:2602.03045),
  cadrecode(ICLR'25, filaPro/cad-recode), cadfs(CVPR'26, FeatureScript 百万程序,
  CD/NC/ECD), cadllama(CVPR'25), seekcad(ICLR'26, arXiv:2505.17702),
  recad(AAAI'26, RL+VLM), cadfit(ICML'26, arXiv:2605.01171, IoU 优化逆向),
  stepcad(NeurIPS'26, policy+tree search, ARCADE-1.5M),
  cadcodeverify(ICLR'25), treellis(UIST'26, DOI 10.1145/3830398.3830694),
  csg(经典 CSG 综述/原始文献，[VERIFY] 选引), featurescript, openscad,
  clarify_general([VERIFY] 选一篇主动澄清代表作), selfcheckgpt, selfrefine,
  propertytesting, chamfer_evals。
- CADFit/StepCAD 是逆向工程（mesh→program，天然有 target mesh 做 IoU），
  引用时必须显式说明任务方向差异，避免被误读为同一设定的竞品。
- SEEK-CAD/ReCAD 与 CADCodeVerify 同属视觉反馈族，一句带过即可，篇幅给差异。
- CADFS 的 ECD（edge Chamfer distance）对 CADCodeVerify 承认的"小缝盲区"
  敏感，评估章节采纳（PLAN.md 已更新）。
- 对 ProCAD 要公平：它是本文的直接基座，措辞是"扩展并修复结构缺陷"，不是
  否定；benchmark 与 User Simulator 明确复用。
- Treellis：正文只用 32/50、43/50 与定性"反复重生成"观察；24 人完成率/
  SUS 数字一律不引（本文不做人因论证）。
- CADFusion/FutureCAD 的"反馈"严格限定为训练时机制，避免被审稿人指出它们
  "也有验证"；FutureCAD 的 BRepGround RL reward 措辞需读原文 [VERIFY]。
- 2.1 较长，定稿时可压缩 CAD-MLLM/agent 一句带过的部分，把篇幅留给
  2.3/2.4 的差异化。
