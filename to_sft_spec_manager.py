#!/usr/bin/env python3
"""
Build SFT data for a Qwen Spec Manager (teacher distillation + canonical labels).

Design (see training/SPEC_MANAGER_TRAINING.md):

* A teacher LLM (default gpt-4o-mini) performs the three perception skills
  (extract spec, incorporate Q&A, propose questions).
* TRAINING TARGETS ARE NOT THE TEACHER'S RAW OUTPUTS. Every target passes
  through the deterministic kernel in src/spec_manager.py
  (normalize_spec / merge_specs / template_questions), so the student only
  sees specs that satisfy provenance ranking, stable ids and the resolutions
  log. This is formal denoising of teacher labels.
* Samples are filtered with deterministic checks (answers landed, conflict
  wrong values preserved, no teacher over-helpfulness, questions grounded).

Output: sharegpt-style JSON for LLaMA-Factory, two records per misleading
sample (extract+update trajectory; questions), one record per clean sample.

Usage:
    python to_sft_spec_manager.py \
        --train_json dataset/selected_misleading_samples_train \
        --improved_json dataset/improved.json \
        --output training/data/spec_manager_sft.json

Requires an API-backed teacher (config/config.yaml + OPENAI_API_KEY).
Progress is written incrementally; re-running skips finished uids.
"""

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.inference import LLM
from src.spec_manager import (
    SpecManager, empty_spec, normalize_spec, merge_specs,
    unresolved_fields, template_questions, iter_features, _as_mm,
)
from config.spec_manager import (
    SPEC_MANAGER_SYSTEM_PROMPT, QUESTIONS_SYSTEM_PROMPT,
)
from to_sft import parse_questions, parse_answers

DEFAULT_TEACHER_MODEL = "gpt-4o-mini-2024-07-18"
NUM_TOL_MM = 0.05  # absolute floor; also 2% relative


# ---------------------------------------------------------------------------
# Deterministic quality filters (pure functions; offline-testable)
# ---------------------------------------------------------------------------

def numeric_index(spec: Dict[str, Any]) -> Dict[str, List[float]]:
    """param name -> numeric mm values across all (non-obsolete) features."""
    out: Dict[str, List[float]] = defaultdict(list)
    for feat in iter_features(spec):
        for name, entry in feat.get("parameters", {}).items():
            v = _as_mm(entry)
            if v is not None:
                out[name].append(v)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(NUM_TOL_MM, 0.02 * max(abs(a), abs(b)))


def _radius_equivalents(index: Dict[str, List[float]]) -> List[float]:
    """All radius/diameter values normalized to radius equivalents."""
    pool = list(index.get("radius", []))
    pool += [v / 2.0 for v in index.get("diameter", [])]
    return pool


def covers_all_values(reference: Dict[str, List[float]],
                      candidate: Dict[str, List[float]]) -> Tuple[bool, List[str]]:
    """Every numeric parameter value in reference appears in candidate
    (mm-normalized). radius/diameter are compared with 2x duality; other
    params match by name. Returns (ok, missing_descriptions)."""
    missing = []
    cand_radial = _radius_equivalents(candidate)
    for param, values in reference.items():
        if param in ("radius", "diameter"):
            pool = list(cand_radial)
            targets = [v if param == "radius" else v / 2.0 for v in values]
            label_param = param
            for v, t in zip(values, targets):
                hit = next((i for i, w in enumerate(pool) if _close(t, w)), None)
                if hit is None:
                    missing.append(f"{label_param}={v:g}mm")
                else:
                    pool.pop(hit)
            continue
        pool = list(candidate.get(param, []))
        for v in values:  # consume: duplicated values need duplicated matches
            hit = next((i for i, w in enumerate(pool) if _close(v, w)), None)
            if hit is None:
                missing.append(f"{param}={v:g}mm")
            else:
                pool.pop(hit)
    return (not missing), missing


def preserved_wrong_value(spec_orig_idx: Dict[str, List[float]],
                          spec1_idx: Dict[str, List[float]]) -> bool:
    """For conflict samples: at least one ground-truth numeric value must be
    ABSENT in the spec extracted from the misleading prompt — i.e. the teacher
    extracted what was written, not what it thinks the part should be."""
    ok, _ = covers_all_values(spec_orig_idx, spec1_idx)
    return not ok


def update_took_effect(spec1: Dict[str, Any], spec2: Dict[str, Any]) -> bool:
    """The Q&A round must change something and leave an answer-confirmed tag."""
    if spec2.get("resolutions"):
        return True
    n1 = sum(len(f.get("parameters", {})) for f in iter_features(spec1))
    n2 = sum(len(f.get("parameters", {})) for f in iter_features(spec2))
    if n2 > n1:
        return True
    return any(
        p.get("source") == "answer-confirmed"
        for f in iter_features(spec2) for p in f.get("parameters", {}).values()
    )


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_extract_update_record(sm: SpecManager, spec1: Dict[str, Any],
                                spec2: Dict[str, Any], misleading_prompt: str,
                                qa_text: str, unresolved1: List[Dict[str, str]]
                                ) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": SPEC_MANAGER_SYSTEM_PROMPT},
        {"role": "user", "content": sm.extract_messages(misleading_prompt)[1]["content"]},
        {"role": "assistant", "content": _json(spec1)},
        {"role": "user", "content": sm.update_messages(
            spec1, qa_text, unresolved1)[1]["content"]},
        {"role": "assistant", "content": _json(spec2)},
    ]


def build_questions_record(sm: SpecManager, spec1: Dict[str, Any],
                           unresolved1: List[Dict[str, str]],
                           questions: List[str]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": QUESTIONS_SYSTEM_PROMPT},
        {"role": "user", "content": sm.questions_messages(
            spec1, unresolved1, max_questions=5)[1]["content"]},
        {"role": "assistant", "content": _json({"questions": questions})},
    ]


def build_clean_record(sm: SpecManager, prompt: str,
                       spec: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": SPEC_MANAGER_SYSTEM_PROMPT},
        {"role": "user", "content": sm.extract_messages(prompt)[1]["content"]},
        {"role": "assistant", "content": _json(spec)},
    ]


def qa_text_from_sample(sample: Dict[str, Any]) -> Tuple[str, List[str]]:
    questions = parse_questions(sample.get("questions_to_ask", ""))
    answers = parse_answers(sample.get("answer_to_questions", ""))
    q_block = "\n".join(f"Q{i+1}: {q}" for i, q in enumerate(questions))
    return f"{q_block}\nA: {answers}".strip(), questions


# ---------------------------------------------------------------------------
# Loading / resume
# ---------------------------------------------------------------------------

def load_samples(path: str) -> List[Dict[str, Any]]:
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("samples", data if isinstance(data, list) else [])


def load_done_uids(path: str) -> set:
    if not os.path.exists(path):
        return set()
    done = set()
    with open(path, "r") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("uid"):
                    done.add((rec["uid"], rec.get("kind")))
            except json.JSONDecodeError:
                continue
    return done


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate(samples: List[Dict[str, Any]], clean_samples: List[Dict[str, Any]],
             teacher_model: str, out_path: str, batch_size: int,
             clean_ratio: float, max_questions_tol: int = 1) -> Dict[str, int]:
    llm = LLM(model_name=teacher_model)
    sm = SpecManager(model_name=teacher_model, llm=llm)
    done = load_done_uids(out_path)
    stats = Counter(written=0, skipped_done=0, filtered=0, clean_written=0)
    reject_reasons = Counter()

    misleading = [s for s in samples if int(s.get("k", 0) or 0) > 0]
    random.shuffle(misleading)
    clean_budget = int(len(misleading) * clean_ratio)
    clean_pool = [s for s in clean_samples
                  if s.get("final_modified_prompt") and s.get("final_generated_code")]
    random.shuffle(clean_pool)

    out_f = open(out_path, "a")

    def flush_record(rec: Dict[str, Any]):
        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_f.flush()

    def batch_call(msgs_list: List[List[Dict[str, str]]], tokens: int = 2048):
        return llm.batch_inference(
            msgs_list, batch_size=batch_size,
            max_new_tokens=tokens, temperature=0)

    # ---- misleading samples ------------------------------------------------
    todo = [s for s in misleading
            if (s.get("uid"), "misleading") not in done]
    stats["skipped_done"] += len(misleading) - len(todo)
    print(f"Misleading: {len(todo)} to process, "
          f"{len(misleading) - len(todo)} already done")

    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        prompts_mis = [s.get("misleading_description") or s.get("misleading_prompt")
                       or s.get("original_prompt") for s in batch]
        prompts_orig = [s.get("original_prompt") for s in batch]

        r_mis = batch_call([sm.extract_messages(p) for p in prompts_mis])
        r_orig = batch_call([sm.extract_messages(p) for p in prompts_orig])

        # prepare only parseable samples for the update call
        prepared = []
        for s, rm, ro in zip(batch, r_mis, r_orig):
            spec1 = sm.parse_spec(rm)
            spec_orig = sm.parse_spec(ro)
            if spec1 is None or spec_orig is None:
                reject_reasons["unparseable"] += 1
                continue
            qa_text, gt_questions = qa_text_from_sample(s)
            unresolved1 = unresolved_fields(spec1)
            prepared.append((s, spec1, spec_orig, qa_text, gt_questions, unresolved1))

        if prepared:
            r_upd = batch_call([
                sm.update_messages(spec1, qa_text, u)
                for _, spec1, _, qa_text, u in prepared
            ])
            q_msgs = [sm.questions_messages(spec1, u, max_questions=5)
                      for _, spec1, _, _, _, u in prepared if u]
            r_q = batch_call(q_msgs, tokens=512) if q_msgs else []

        qi = 0
        for pi, (s, spec1, spec_orig, qa_text, gt_questions, unresolved1) in enumerate(prepared):
            uid = s.get("uid")
            upd = sm.parse_spec(r_upd[pi])
            if upd is None:
                reject_reasons["unparseable_update"] += 1
                stats["filtered"] += 1
                continue
            spec2 = merge_specs(spec1, upd)

            # --- deterministic filters ---
            idx_orig, idx1, idx2 = (numeric_index(x)
                                    for x in (spec_orig, spec1, spec2))
            is_conflict = "conflict" in (s.get("config_name") or "")

            ok, missing = covers_all_values(idx_orig, idx2)
            if not ok:
                reject_reasons["answers_not_landed"] += 1
                stats["filtered"] += 1
                continue
            if is_conflict and not preserved_wrong_value(idx_orig, idx1):
                reject_reasons["teacher_corrected_conflict"] += 1
                stats["filtered"] += 1
                continue
            if not update_took_effect(spec1, spec2):
                reject_reasons["update_no_effect"] += 1
                stats["filtered"] += 1
                continue

            # trajectory record (E1 + E2)
            flush_record({
                "uid": uid, "kind": "misleading", "skill": "extract_update",
                "config_name": s.get("config_name"), "k": s.get("k"),
                "messages": build_extract_update_record(
                    sm, spec1, spec2,
                    s.get("misleading_description") or s.get("misleading_prompt"),
                    qa_text, unresolved1),
            })
            stats["written"] += 1

            # questions record (E3), only when the spec has unresolved fields
            if unresolved1:
                qs = sm.parse_questions(r_q[qi]) if qi < len(r_q) else []
                qi += 1
                if not qs:
                    qs = template_questions(unresolved1, max_questions=5)
                if len(qs) > len(unresolved1) + max_questions_tol:
                    qs = qs[:len(unresolved1) + max_questions_tol]
                flush_record({
                    "uid": uid, "kind": "misleading", "skill": "questions",
                    "config_name": s.get("config_name"), "k": s.get("k"),
                    "messages": build_questions_record(
                        sm, spec1, unresolved1, qs),
                })
                stats["written"] += 1

        print(f"  {min(start + len(batch), len(todo))}/{len(todo)} | "
              f"written={stats['written']} filtered={stats['filtered']} "
              f"reasons={dict(reject_reasons)}")

    # ---- clean samples (teach "do not over-ask") ---------------------------
    clean_todo = [s for s in clean_pool[:clean_budget]
                  if (s.get("uid"), "clean") not in done]
    print(f"Clean: {len(clean_todo)} to process (budget {clean_budget})")
    for start in range(0, len(clean_todo), batch_size):
        batch = clean_todo[start:start + batch_size]
        prompts = [s["final_modified_prompt"] for s in batch]
        responses = batch_call([sm.extract_messages(p) for p in prompts])
        for s, resp in zip(batch, responses):
            spec = sm.parse_spec(resp)
            if spec is None or unresolved_fields(spec):
                # clean prompt must yield a fully-specified spec, else drop
                reject_reasons["clean_not_complete"] += 1
                stats["filtered"] += 1
                continue
            flush_record({
                "uid": s.get("uid"), "kind": "clean", "skill": "extract",
                "messages": build_clean_record(sm, s["final_modified_prompt"], spec),
            })
            stats["clean_written"] += 1

    out_f.close()
    stats["reject_reasons"] = dict(reject_reasons)
    return dict(stats)


def main():
    ap = argparse.ArgumentParser(description="Build Spec Manager SFT data")
    ap.add_argument("--train_json", default="./dataset/selected_misleading_samples_train")
    ap.add_argument("--improved_json", default="./dataset/improved.json")
    ap.add_argument("--output", default="./training/data/spec_manager_sft.jsonl")
    ap.add_argument("--teacher_model", default=DEFAULT_TEACHER_MODEL)
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--clean_ratio", type=float, default=1.0,
                    help="max clean records as a fraction of misleading count")
    ap.add_argument("--max_samples", type=int, default=None,
                    help="debug cap on misleading samples")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    samples = load_samples(args.train_json)
    clean_samples = load_samples(args.improved_json)
    if args.max_samples:
        samples = samples[:args.max_samples]
    print(f"Loaded {len(samples)} misleading / {len(clean_samples)} clean source samples")

    stats = generate(samples, clean_samples, args.teacher_model,
                     args.output, args.batch_size, args.clean_ratio)
    print("\nDone:", json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"JSONL written to {args.output}")
    print("Convert to a JSON array for LLaMA-Factory if needed "
          "(jq -s . file.jsonl > file.json), or point dataset_info at the jsonl.")


if __name__ == "__main__":
    main()
