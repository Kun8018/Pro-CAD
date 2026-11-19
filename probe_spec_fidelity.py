"""
Spec-fidelity probe for the ProCAD Clarifier -> Coder hand-off.

Motivation
----------
In pipeline.py the ONLY artefact exchanged between the two trained agents is the
Clarifier's `standardized_prompt` (Step 3). The Coder never sees the original
prompt or the Q&A. This probe measures how often information is corrupted at
that hand-off:

  A. DROPPED       fact in original prompt / Q&A answers, missing in standardized prompt
  B. UNSUPPORTED   fact in standardized prompt with no value in any source (invention)
  C. CARRYOVER     wrong value from the misleading prompt survives clarification

Two independent channels:
  1. Numeric / deterministic  — regex slot extraction + unit normalization (free,
     reproducible, no API). Strongest on dimensions and feature counts.
  2. LLM semantic judge       — full fact-level audit using the prompt in
     config/spec_fidelity_audit.py (--with-llm). Use it to cross-check channel 1.

It also tests whether defects hurt generation quality: mean Chamfer distance of
defective vs clean samples.

Usage
-----
    # deterministic channel only, over a finished pipeline run
    python probe_spec_fidelity.py --results_dir ./clarification_results

    # add the LLM judge
    python probe_spec_fidelity.py \
        --results_dir ./clarification_results \
        --with-llm --judge-model gpt-5-mini-2025-08-07 \
        --output_dir ./probe_results

Input: clarification_batch_*.json / all_results_parallel_*.json produced by
pipeline.py, or individual clarification_result_*.json files.
"""

import argparse
import glob
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def extract_standardized_prompt(result: Dict) -> Optional[str]:
    """Get standardized prompt; fall back to parsing the raw Step 3 response."""
    sp = result.get("_standardized_prompt")
    if sp and isinstance(sp, str) and sp.strip():
        return sp.strip()

    raw = result.get("step3_corrected_description")
    if not raw or not isinstance(raw, str):
        return None

    cleaned = raw.strip()
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    # Find the object that actually contains standardized_prompt
    m = re.search(
        r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*"standardized_prompt"[^}]*\}',
        cleaned, re.DOTALL,
    )
    candidate = m.group(0) if m else cleaned
    try:
        parsed = json.loads(candidate)
        value = parsed.get("standardized_prompt")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (json.JSONDecodeError, AttributeError):
        pass
    return None  # do not fall back to raw text: it would poison the comparison


def load_results(results_dir: str) -> List[Dict]:
    """Load per-sample results from any pipeline output file in the directory."""
    patterns = [
        "clarification_batch_*.json",
        "all_results_parallel_*.json",
        "clarification_result_*.json",
    ]
    seen_keys = set()
    out: List[Dict] = []

    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(results_dir, pat)))

    for path in sorted(set(files)):
        with open(path, "r") as f:
            data = json.load(f)

        if "results" in data and isinstance(data["results"], list):
            candidates = data["results"]
        elif isinstance(data, dict) and "uid" in data:
            candidates = [data]
        else:
            continue

        for r in candidates:
            key = (r.get("uid"), r.get("k"), r.get("config_name"))
            if key in seen_keys:  # batch + aggregate file may overlap
                continue
            seen_keys.add(key)
            out.append(r)

    return out


# ---------------------------------------------------------------------------
# Channel 1: deterministic numeric slot extraction
# ---------------------------------------------------------------------------

UNIT_TO_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0,
    "millimetres": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
}

# canonical slot -> surface synonyms
SLOT_SYNONYMS = {
    "radius": ["radius", "radii", "r"],
    "diameter": ["diameter", "dia", "d"],
    "length": ["length", "long"],
    "width": ["width", "wide", "breadth"],
    "height": ["height", "tall"],
    "thickness": ["thickness", "thick"],
    "depth": ["depth", "deep"],
    "distance": ["distance", "spacing", "offset", "gap", "apart", "separation"],
    "angle": ["angle"],
    "hole_count": ["holes", "hole"],
    "slot_count": ["slots", "slot"],
    "fillet_radius": ["fillet"],
    "chamfer": ["chamfer"],
    "counterbore_depth": ["counterbore"],
}

_NAME_TO_SLOT = {}
for slot, names in SLOT_SYNONYMS.items():
    for n in names:
        _NAME_TO_SLOT[n] = slot

COUNT_SLOTS = {"hole_count", "slot_count"}

# number + optional unit, keyword within a small window either before or after
_NUM = r"(?P<num>\d+(?:\.\d+)?)"
_UNIT = r"(?P<unit>mm|millimeters?|millimetres?|cm|m|inches?|in|\")"
_KEYWORDS = sorted(_NAME_TO_SLOT.keys(), key=len, reverse=True)
_KW_ALT = "|".join(re.escape(k) for k in _KEYWORDS)

_NUMERIC_PATTERNS = [
    # "radius of 5 mm", "a thickness: 10mm", "length is 12.5 mm"
    re.compile(
        rf"\b(?P<kw>{_KW_ALT})\b\s*(?:of|is|:|=|of approx(?:imately)?)?\s*"
        rf"(?:approx(?:imately)?\s*)?{_NUM}\s*{_UNIT}?",
        re.IGNORECASE,
    ),
    # "5 mm radius", "10mm thick(ness)"
    re.compile(
        rf"{_NUM}\s*{_UNIT}?\s*(?P<kw>{_KW_ALT})\b",
        re.IGNORECASE,
    ),
    # bare count: "4 holes"
    re.compile(rf"\b{_NUM}\s*(?P<kw>holes?|slots?)\b", re.IGNORECASE),
]

_STOP = set("a an the and or with of to is are be has have its their this that "
            "for on at in by from plus minus than then".split())


def _context_keywords(text: str, span: Tuple[int, int], window: int = 60) -> List[str]:
    """Collect nearby content words to disambiguate which feature a slot belongs to."""
    lo, hi = span
    around = text[max(0, lo - window): min(len(text), hi + window)].lower()
    words = re.findall(r"[a-z]+", around)
    feature_words = [
        w for w in words
        if w not in _STOP and w not in _NAME_TO_SLOT and len(w) > 2
    ]
    # keep the closest few non-generic words
    generic = {"part", "model", "shape", "object", "piece", "design", "approximately"}
    return [w for w in feature_words if w not in generic][:4]


def extract_slots(text: str) -> List[Dict[str, Any]]:
    """Extract normalized (slot, value-in-mm-or-count, context, raw) facts."""
    if not text:
        return []

    # tight patterns first
    hits: List[Tuple[Tuple[int, int], str, float, str]] = []
    seen_spans: List[Tuple[int, int]] = []
    for pat in _NUMERIC_PATTERNS:
        for m in pat.finditer(text):
            if m.span() in seen_spans:
                continue
            seen_spans.append(m.span())
            hits.append((m.span(), m.group("kw").lower(), float(m.group("num")),
                         (m.groupdict().get("unit") or "").strip('"').lower()))

    # loose fallback: keyword with a number within ~40 chars after it
    num_re = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mm|millimeters?|cm|m|inches?|in|\")?")
    for m in re.finditer(rf"\b(?:{_KW_ALT})\b", text):
        slot = _NAME_TO_SLOT.get(m.group(0).lower())
        # counts need adjacency ('4 holes'); a loose window would mistake
        # 'a hole of radius 5' for 5 holes
        if slot is None or slot in COUNT_SLOTS:
            continue
        window = text[m.end(): m.end() + 40]
        nm = num_re.search(window)
        if not nm:
            continue
        span = (m.start(), m.end() + nm.end())
        if any(not (span[1] <= a or span[0] >= b) for a, b in seen_spans):
            continue  # tight pattern already owns this region
        seen_spans.append(span)
        hits.append((span, m.group(0).lower(), float(nm.group("num")),
                     (nm.group("unit") or "").strip('"').lower()))

    facts = []
    for span, kw, value, unit in hits:
        slot = _NAME_TO_SLOT.get(kw) or _NAME_TO_SLOT.get(kw.rstrip("s"))
        if slot is None:
            continue
        if slot not in COUNT_SLOTS and unit:
            value *= UNIT_TO_MM.get(unit, 1.0)
        facts.append({
            "slot": slot,
            "value": value,
            "context": _context_keywords(text, span),
            "raw": text[max(0, span[0] - 25): min(len(text), span[1] + 25)].strip(),
        })
    return facts


def _same_feature(a: Dict, b: Dict) -> bool:
    """Two slot mentions refer to the same feature if contexts overlap."""
    if a["slot"] != b["slot"]:
        return False
    ca, cb = set(a["context"]), set(b["context"])
    if not ca or not cb:
        return True  # no disambiguation possible; match at slot level
    return bool(ca & cb)


def _close(a: float, b: float, slot: str, tol: float = 0.06) -> bool:
    if slot in COUNT_SLOTS:
        return a == b
    return abs(a - b) <= max(tol, 0.02 * max(abs(a), abs(b)))


def _dedup_facts(facts: List[Dict]) -> List[Dict]:
    """Collapse repeated mentions of the same (slot, value) fact."""
    unique = {}
    for f in facts:
        key = (f["slot"], round(f["value"], 4))
        if key not in unique:
            unique[key] = dict(f)
        else:  # merge context so feature matching uses all mentions
            prev = unique[key]
            prev["context"] = list(dict.fromkeys(prev["context"] + f["context"]))
    return list(unique.values())


def audit_numeric(result: Dict, standardized: str) -> Dict:
    """Deterministic audit. Ground truth = original prompt + Q&A answers."""
    src = extract_slots(result.get("original_prompt") or "")
    src += extract_slots(result.get("step2_answers") or "")
    # same fact may be stated in both the original prompt and an answer
    src = _dedup_facts(src)
    misleading_facts = extract_slots(result.get("misleading_prompt") or "")
    std = extract_slots(standardized)

    dropped, carryover, unsupported = [], [], []

    # every ground-truth fact must survive in the standardized prompt
    for f in src:
        match = next((g for g in std if _same_feature(f, g)), None)
        if match is None:
            dropped.append(f)
        elif not _close(f["value"], match["value"], f["slot"]):
            # value changed: did it inherit the misleading value?
            bad = next(
                (h for h in misleading_facts
                 if _same_feature(f, h) and _close(h["value"], match["value"], h["slot"])
                 and not _close(h["value"], f["value"], h["slot"])),
                None,
            )
            (carryover if bad else unsupported).append(
                {"source": f, "standardized": match, **({"misleading": bad} if bad else {})}
            )

    # facts invented by the standardized prompt (no supporting value anywhere)
    for g in std:
        supported = any(_same_feature(g, f) and _close(g["value"], f["value"], g["slot"])
                        for f in src)
        if not supported:
            # slot-level presence with a *different* value was already counted above
            already = any(
                d.get("standardized") is g for d in carryover + unsupported
            )
            if not already:
                unsupported.append({"standardized": g})

    n_src = len(src)
    return {
        "channel": "numeric",
        "facts_in_source": n_src,
        "facts_in_standardized": len(std),
        "n_dropped": len(dropped),
        "n_carryover": len(carryover),
        "n_unsupported": len(unsupported),
        "dropped": dropped,
        "carryover": carryover,
        "unsupported": unsupported,
    }


# ---------------------------------------------------------------------------
# Channel 2: LLM semantic judge
# ---------------------------------------------------------------------------

def build_qa_text(result: Dict) -> str:
    questions = (result.get("step1_analysis") or {}).get("questions") \
        or result.get("ground_truth_questions") or []
    answers = result.get("step2_answers") or result.get("ground_truth_answers") or ""
    if isinstance(answers, list):
        answers = "\n".join(str(a) for a in answers)
    q = "\n".join(f"Q{i+1}: {x}" for i, x in enumerate(questions)) or "(no questions)"
    return f"{q}\nA: {answers}".strip()


def audit_llm(results: List[Dict], standardized_map: Dict[int, str],
              model_name: str, batch_size: int) -> List[Optional[Dict]]:
    from src.inference import LLM
    from config.spec_fidelity_audit import (
        SPEC_FIDELITY_JUDGE_SYSTEM_PROMPT,
        SPEC_FIDELITY_JUDGE_USER_TEMPLATE,
    )

    llm = LLM(model_name=model_name)
    idxs = [i for i in range(len(results)) if i in standardized_map]

    messages_list = []
    for i in idxs:
        r = results[i]
        user = SPEC_FIDELITY_JUDGE_USER_TEMPLATE.format(
            original_prompt=r.get("original_prompt") or "",
            misleading_prompt=r.get("misleading_prompt") or "(same as original)",
            qa_text=build_qa_text(r),
            standardized_prompt=standardized_map[i],
        )
        messages_list.append([
            {"role": "system", "content": SPEC_FIDELITY_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ])

    responses = llm.batch_inference(
        messages_list, batch_size=batch_size, max_new_tokens=2048, temperature=0
    )

    out: Dict[int, Optional[Dict]] = {}
    for i, resp in zip(idxs, responses):
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if not m:
            out[i] = None
            continue
        try:
            parsed = json.loads(m.group(0))
            parsed["channel"] = "llm"
            parsed["n_dropped"] = len(parsed.get("dropped", []))
            parsed["n_carryover"] = len(parsed.get("carryover", []))
            parsed["n_unsupported"] = len(parsed.get("unsupported", []))
            out[i] = parsed
        except json.JSONDecodeError:
            out[i] = None
    return [out.get(i) for i in range(len(results))]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _cd(result: Dict) -> Optional[float]:
    # rows produced by this probe store cd directly; raw pipeline results nest it
    if isinstance(result.get("chamfer_distance"), (int, float)):
        return result["chamfer_distance"]
    ev = result.get("evaluation") or {}
    cd = ev.get("chamfer_distance")
    return cd if isinstance(cd, (int, float)) else None


def summarize(rows: List[Dict], defect_key: str) -> Dict:
    """Aggregate defect rates, grouped by config_name (underspec vs conflict...)."""
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        groups[row["config_name"] or "unknown"].append(row)
    groups["ALL"] = rows

    summary = {}
    for name, items in sorted(groups.items()):
        eligible = [r for r in items if r.get("audit")]
        n = len(eligible)
        if n == 0:
            continue
        with_defect = [r for r in eligible if r["audit"].get(defect_key, 0) > 0]
        total_facts = sum(r["audit"].get("facts_in_source", 0) for r in eligible)
        total_defects = sum(r["audit"].get(defect_key, 0) for r in eligible)

        # quality split: does the defect track worse Chamfer distance?
        cd_bad = [_cd(r) for r in with_defect if _cd(r) is not None]
        cd_clean = [_cd(r) for r in eligible
                    if r["audit"].get(defect_key, 0) == 0 and _cd(r) is not None]

        summary[name] = {
            "n_samples": n,
            "samples_with_defect": len(with_defect),
            "sample_defect_rate": round(len(with_defect) / n, 4),
            "total_source_facts": total_facts,
            "total_defects": total_defects,
            "fact_defect_rate": round(total_defects / total_facts, 4) if total_facts else None,
            "mean_cd_defective": round(float(np.mean(cd_bad)), 6) if cd_bad else None,
            "mean_cd_clean": round(float(np.mean(cd_clean)), 6) if cd_clean else None,
        }
    return summary


def main():
    ap = argparse.ArgumentParser(description="Probe Clarifier->Coder hand-off fidelity")
    ap.add_argument("--results_dir", default="./clarification_results")
    ap.add_argument("--output_dir", default="./probe_results")
    ap.add_argument("--with-llm", action="store_true",
                    help="Also run the LLM semantic judge")
    ap.add_argument("--judge-model", default="gpt-5-mini-2025-08-07")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--include-skipped", action="store_true",
                    help="Include samples with no standardized prompt in counts")
    args = ap.parse_args()

    results = load_results(args.results_dir)
    if not results:
        raise SystemExit(f"No pipeline result files found under {args.results_dir}")
    print(f"Loaded {len(results)} samples from {args.results_dir}")

    rows = []
    standardized_map: Dict[int, str] = {}
    for i, r in enumerate(results):
        sp = extract_standardized_prompt(r)
        if sp is None:
            if args.include_skipped:
                rows.append({"result": r, "audit": None, "config_name": r.get("config_name")})
            continue
        standardized_map[i] = sp
        audit = audit_numeric(r, sp)
        rows.append({
            "uid": r.get("uid"),
            "k": r.get("k"),
            "config_name": r.get("config_name"),
            "chamfer_distance": _cd(r),
            "standardized_prompt": sp,
            "audit": audit,
        })

    eligible = [row for row in rows if row.get("audit")]
    print(f"Audited samples (with standardized prompt): {len(eligible)}")

    llm_audits: List[Optional[Dict]] = [None] * len(results)
    if args.with_llm and standardized_map:
        print(f"Running LLM judge: {args.judge_model}")
        llm_audits = audit_llm(results, standardized_map,
                               args.judge_model, args.batch_size)
        for row, la in zip(rows, llm_audits):
            if la is not None:
                row["audit_llm"] = la

    os.makedirs(args.output_dir, exist_ok=True)

    report = {
        "results_dir": args.results_dir,
        "n_loaded": len(results),
        "n_audited": len(eligible),
        "numeric": {
            "dropped": summarize(rows, "n_dropped"),
            "unsupported": summarize(rows, "n_unsupported"),
            "carryover": summarize(rows, "n_carryover"),
        },
    }
    if args.with_llm:
        llm_rows = [{**r, "audit": r.get("audit_llm")} for r in rows if r.get("audit_llm")]
        report["llm"] = {
            "n_judged": len(llm_rows),
            "dropped": summarize(llm_rows, "n_dropped"),
            "unsupported": summarize(llm_rows, "n_unsupported"),
            "carryover": summarize(llm_rows, "n_carryover"),
        }

    def _print_block(title: str, groups: Dict):
        print(f"\n--- {title} ---")
        print(f"{'group':<28}{'n':>6}{'%samples':>10}{'%facts':>10}"
              f"{'CD bad':>12}{'CD clean':>12}")
        for name, s in groups.items():
            print(f"{name:<28}{s['n_samples']:>6}"
                  f"{s['sample_defect_rate']:>10.2%}"
                  f"{(s['fact_defect_rate'] if s['fact_defect_rate'] is not None else float('nan')):>10.2%}"
                  f"{str(s['mean_cd_defective']):>12}{str(s['mean_cd_clean']):>12}")

    print("\n" + "=" * 80)
    print("NUMERIC CHANNEL")
    for defect, groups in report["numeric"].items():
        _print_block(defect.upper(), groups)
    if args.with_llm:
        print("\n" + "=" * 80)
        print("LLM CHANNEL")
        for defect, groups in report["llm"].items():
            if defect == "n_judged":
                continue
            _print_block(defect.upper(), groups)

    detail_path = os.path.join(args.output_dir, "spec_fidelity_details.json")
    with open(detail_path, "w") as f:
        json.dump(rows, f, indent=2)
    summary_path = os.path.join(args.output_dir, "spec_fidelity_summary.json")
    with open(summary_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nDetails: {detail_path}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
