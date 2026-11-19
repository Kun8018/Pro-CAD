"""
Spec Manager agent.

Replaces the Clarifier's single free-text `standardized_prompt` hand-off with a
structured design specification (feature graph) where every parameter carries a
provenance tag. See THIRD_AGENT_IDEA.md, section 3 (research route) and
config/spec_manager.py (schema + prompts).

Design split
------------
Deterministic operations (schema validation, provenance-ranked merge, conflict
detection, unresolved-field detection, Coder hand-off rendering) are pure
functions/methods with NO LLM dependency, so they are unit-testable offline.
LLM calls are confined to:
  - extract_spec      : text          -> spec JSON
  - incorporate_text  : spec + Q&A text -> updated spec (LLM), then the result
                        is defensively reconciled by the deterministic merge
  - propose_questions : spec + unresolved -> question list (template fallback)

The pipeline (pipeline.py) batches the *_messages / parse_* methods directly.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config.spec_manager import (
    SPEC_MANAGER_SYSTEM_PROMPT,
    EXTRACT_SPEC_USER_TEMPLATE,
    UPDATE_SPEC_USER_TEMPLATE,
    QUESTIONS_SYSTEM_PROMPT,
    QUESTIONS_USER_TEMPLATE,
)

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

USER_STATED = "user-stated"
ANSWER_CONFIRMED = "answer-confirmed"
AGENT_ASSUMED = "agent-assumed"
INFERRED = "inferred"

VALID_SOURCES = {USER_STATED, ANSWER_CONFIRMED, AGENT_ASSUMED, INFERRED}

# Higher rank wins in a merge. An answer is the ground-truth channel in the
# misleading-prompt setting, so it outranks the original (possibly wrong)
# user statement.
SOURCE_RANK = {
    ANSWER_CONFIRMED: 4,
    USER_STATED: 3,
    INFERRED: 2,
    AGENT_ASSUMED: 1,
}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Required parameters per feature type. Each tuple lists ALTERNATIVES
# (satisfying any one is enough, e.g. radius OR diameter).
REQUIRED_PARAMETERS: Dict[str, List[Tuple[str, ...]]] = {
    "box": [("length",), ("width",), ("height", "thickness")],
    "plate": [("length",), ("width",), ("height", "thickness")],
    "block": [("length",), ("width",), ("height", "thickness")],
    "cylinder": [("radius", "diameter"), ("height", "depth")],
    "disc": [("radius", "diameter"), ("height", "thickness")],
    "hole": [("radius", "diameter"), ("depth", "height", "through")],
    "slot": [("length",), ("width",), ("depth", "height", "through")],
    "sphere": [("radius", "diameter")],
    "boss": [("radius", "diameter"), ("height",)],
    "pocket": [("length",), ("width",), ("depth", "height")],
    "extrusion": [("height", "depth", "thickness")],
    "chamfer": [("distance",)],
    "fillet": [("radius",)],
}

_UNIT_TO_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0,
    "millimetres": 1.0, None: 1.0, "": 1.0,
    "cm": 10.0, "m": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4,
}


def _entry(value: Any, source: str = AGENT_ASSUMED, unit: Optional[str] = None,
           evidence: str = "") -> Dict[str, Any]:
    """Build a parameter/value entry."""
    return {"value": value, "unit": unit, "source": source, "evidence": evidence}


def empty_spec() -> Dict[str, Any]:
    return {"unit": "mm", "features": [], "constraints": [], "resolutions": []}


def normalize_spec(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a parsed LLM JSON object into the canonical schema shape."""
    if not isinstance(spec, dict):
        return empty_spec()
    out = {
        "unit": spec.get("unit", "mm"),
        "features": [],
        "constraints": [],
        "resolutions": spec.get("resolutions", []),
    }
    for feat in spec.get("features", []):
        if not isinstance(feat, dict):
            continue
        params: Dict[str, Any] = {}
        for name, p in feat.get("parameters", {}).items():
            if not isinstance(p, dict):
                # tolerate bare values: {"length": 60}
                p = _entry(p)
            src = p.get("source")
            if src not in VALID_SOURCES:
                src = AGENT_ASSUMED
            params[name] = {
                "value": p.get("value"),
                "unit": p.get("unit"),
                "source": src,
                "evidence": p.get("evidence", ""),
            }
        out["features"].append({
            "id": feat.get("id") or f"feature_{len(out['features']) + 1}",
            "type": (feat.get("type") or "other").lower(),
            "operation": feat.get("operation", "add"),
            "count": feat.get("count"),
            "parameters": params,
            "placement": feat.get("placement", []),
            "_obsolete": feat.get("_obsolete", False),
        })
    for con in spec.get("constraints", []):
        if isinstance(con, dict):
            out["constraints"].append(con)
    return out


# ---------------------------------------------------------------------------
# Deterministic analysis
# ---------------------------------------------------------------------------

def _as_mm(entry: Dict[str, Any]) -> Optional[float]:
    """Numeric value normalized to mm; None if non-numeric."""
    try:
        v = float(entry["value"])
    except (TypeError, ValueError, KeyError):
        return None
    return v * _UNIT_TO_MM.get((entry.get("unit") or "").strip('"').lower(), 1.0)


def values_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Compare two entries numerically (unit-aware) or by string value."""
    ma, mb = _as_mm(a), _as_mm(b)
    if ma is not None and mb is not None:
        tol = max(1e-6, 0.001 * max(abs(ma), abs(mb)))
        return abs(ma - mb) <= tol
    return str(a.get("value")).strip().lower() == str(b.get("value")).strip().lower()


def iter_features(spec: Dict[str, Any]):
    for f in spec.get("features", []):
        if not f.get("_obsolete"):
            yield f


def unresolved_fields(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Parameters missing or only agent-assumed, for feature types we know."""
    out: List[Dict[str, str]] = []
    for f in iter_features(spec):
        required = REQUIRED_PARAMETERS.get(f["type"])
        if not required:
            continue
        params = f.get("parameters", {})
        for alternatives in required:
            present = next(
                ((name, params[name]) for name in alternatives if name in params),
                None,
            )
            if present is None:
                out.append({
                    "feature_id": f["id"],
                    "feature_type": f["type"],
                    "param": alternatives[0],
                    "reason": "missing",
                })
            else:
                name, entry = present
                if entry.get("source") == AGENT_ASSUMED:
                    out.append({
                        "feature_id": f["id"],
                        "feature_type": f["type"],
                        "param": name,
                        "reason": "agent-assumed",
                        "assumed_value": entry.get("value"),
                        "unit": entry.get("unit") or spec.get("unit", "mm"),
                    })
    return out


def format_unresolved(unresolved: List[Dict[str, str]]) -> str:
    if not unresolved:
        return "(none — every required field is confirmed)"
    lines = []
    for u in unresolved:
        if u["reason"] == "missing":
            lines.append(f"- {u['feature_id']} ({u['feature_type']}): "
                         f"{u['param']} is missing")
        else:
            lines.append(
                f"- {u['feature_id']} ({u['feature_type']}): {u['param']} "
                f"is an unconfirmed default "
                f"({u.get('assumed_value')} {u.get('unit', '')})"
            )
    return "\n".join(lines)


def detect_conflicts(spec_a: Dict[str, Any], spec_b: Dict[str, Any]
                     ) -> List[Dict[str, Any]]:
    """Find same (feature, param) pairs with different values across two specs."""
    index_b = {}
    for f in spec_b.get("features", []):
        for name, entry in f.get("parameters", {}).items():
            index_b[(f["id"], name)] = entry
    conflicts = []
    for f in spec_a.get("features", []):
        for name, ea in f.get("parameters", {}).items():
            eb = index_b.get((f["id"], name))
            if eb is not None and not values_equal(ea, eb):
                conflicts.append({
                    "feature_id": f["id"], "param": name,
                    "value_a": ea.get("value"), "source_a": ea.get("source"),
                    "value_b": eb.get("value"), "source_b": eb.get("source"),
                })
    return conflicts


def merge_specs(base: Dict[str, Any], incoming: Dict[str, Any],
                append_resolutions: bool = True) -> Dict[str, Any]:
    """
    Deterministic provenance-ranked merge of `incoming` into `base`.

    - a higher-rank source overrides a lower-rank one (answer-confirmed >
      user-stated > inferred > agent-assumed);
    - equal rank with a changed value overrides too (fresh statement wins);
    - new features/parameters/constraints are added;
    - every override where the old value differed is logged in
      spec['resolutions'] — this is the machine-readable record the Verifier
      uses to localize where information changed.
    """
    out = normalize_spec(base)
    incoming = normalize_spec(incoming)
    resolutions = out.setdefault("resolutions", [])

    by_id = {f["id"]: f for f in out["features"]}
    # secondary alignment: same type, nth occurrence (ids may drift in LLM output)
    type_counters: Dict[str, int] = {}

    for fin in incoming["features"]:
        f = by_id.get(fin["id"])
        if f is None:
            # try positional match within the same type
            same_type = [x for x in out["features"] if x["type"] == fin["type"]]
            idx = type_counters.get(fin["type"], 0)
            type_counters[fin["type"]] = idx + 1
            if idx < len(same_type):
                f = same_type[idx]
        if f is None:
            out["features"].append(fin)
            by_id[fin["id"]] = fin
            continue

        if fin.get("_obsolete"):
            f["_obsolete"] = True
        f["placement"] = fin.get("placement") or f.get("placement", [])
        if fin.get("count") is not None:
            f["count"] = fin["count"]

        for name, new_entry in fin["parameters"].items():
            old_entry = f["parameters"].get(name)
            if old_entry is None:
                f["parameters"][name] = new_entry
                continue
            old_rank = SOURCE_RANK.get(old_entry.get("source"), 0)
            new_rank = SOURCE_RANK.get(new_entry.get("source"), 0)
            changed = not values_equal(old_entry, new_entry)
            if changed and new_rank >= old_rank:
                if append_resolutions:
                    resolutions.append({
                        "feature_id": f["id"], "param": name,
                        "old_value": old_entry.get("value"),
                        "old_source": old_entry.get("source"),
                        "new_value": new_entry.get("value"),
                        "new_source": new_entry.get("source"),
                        "evidence": new_entry.get("evidence", ""),
                    })
                f["parameters"][name] = new_entry
            elif not changed and new_rank > old_rank:
                # same value, stronger provenance — upgrade the tag
                f["parameters"][name] = new_entry

    # union of constraints by (subject, kind, target)
    existing = {
        (c.get("subject"), c.get("kind"), c.get("target")): c
        for c in out.get("constraints", [])
    }
    for c in incoming.get("constraints", []):
        key = (c.get("subject"), c.get("kind"), c.get("target"))
        existing[key] = c
    out["constraints"] = list(existing.values())
    return out


# ---------------------------------------------------------------------------
# Coder hand-off rendering
# ---------------------------------------------------------------------------

ASSUMED_MARKER = "[ASSUMED DEFAULT - NOT CONFIRMED BY USER]"


def render_for_coder(spec: Dict[str, Any], with_provenance: bool = True) -> str:
    """
    Produce the text handed to the Coder.

    With provenance: every value carries its source and assumed values are
    loudly marked, so the Coder (and downstream Verifier) can tell confirmed
    geometry from defaults.
    Without provenance (ablation): the same structured listing, bare values.
    """
    spec = normalize_spec(spec)
    lines = ["Design specification (structured):", f"Unit: {spec.get('unit', 'mm')}"]

    for f in iter_features(spec):
        op = f.get("operation", "add")
        count = f.get("count")
        header = f"- {f['id']}: {f['type']} ({op})"
        if count is not None:
            header += f", count={count}"
        lines.append(header)
        for name, p in f["parameters"].items():
            unit = p.get("unit") or ""
            value = f"{p['value']} {unit}".strip()
            if with_provenance:
                if p.get("source") == AGENT_ASSUMED:
                    lines.append(f"    {name} = {value} {ASSUMED_MARKER}")
                else:
                    lines.append(f"    {name} = {value} [{p.get('source')}]")
            else:
                lines.append(f"    {name} = {value}")
        for place in f.get("placement", []):
            desc = " ".join(str(place.get(k, "")) for k in ("kind", "target")).strip()
            if desc:
                tag = f" [{place.get('source')}]" if with_provenance and place.get("source") else ""
                lines.append(f"    placement: {desc}{tag}")

    for c in spec.get("constraints", []):
        val = c.get("value")
        val_txt = f" = {val.get('value')} {val.get('unit') or ''}".rstrip() if isinstance(val, dict) else ""
        line = (f"- constraint {c.get('id', '')}: {c.get('subject')} "
                f"{c.get('kind')} {c.get('target')}{val_txt}".rstrip())
        if with_provenance and c.get("source"):
            line += f" [{c['source']}]"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-facing agent
# ---------------------------------------------------------------------------

def _parse_json_object(response: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from an LLM response (tolerates fences/prose)."""
    if not response:
        return None
    text = response.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


class SpecManager:
    """LLM-backed wrapper. All deterministic behaviour lives in module
    functions above; this class only performs extraction/update/question calls
    and defensively reconciles their outputs."""

    DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"

    def __init__(self, model_name: Optional[str] = None,
                 llm: Optional[Any] = None,
                 config_path: str = "./config/config.yaml",
                 max_new_tokens: int = 2048):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.max_new_tokens = max_new_tokens
        self._llm = llm  # injectable for tests / reuse of pipeline LLM

    @property
    def llm(self):
        if self._llm is None:
            from src.inference import LLM
            self._llm = LLM(model_name=self.model_name)
        return self._llm

    # -- message builders (also used directly by the batched pipeline) ------

    def extract_messages(self, prompt: str,
                         stage_label: str = "USER DESCRIPTION") -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": SPEC_MANAGER_SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACT_SPEC_USER_TEMPLATE.format(
                stage_label=stage_label, prompt=prompt)},
        ]

    def update_messages(self, spec: Dict[str, Any], qa_text: str,
                        unresolved: Optional[List[Dict[str, str]]] = None
                        ) -> List[Dict[str, str]]:
        unresolved = unresolved if unresolved is not None else unresolved_fields(spec)
        return [
            {"role": "system", "content": SPEC_MANAGER_SYSTEM_PROMPT},
            {"role": "user", "content": UPDATE_SPEC_USER_TEMPLATE.format(
                spec_json=json.dumps(spec, indent=2),
                qa_text=qa_text,
                unresolved_text=format_unresolved(unresolved))},
        ]

    def questions_messages(self, spec: Dict[str, Any],
                           unresolved: List[Dict[str, str]],
                           max_questions: int = 5) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": QUESTIONS_SYSTEM_PROMPT},
            {"role": "user", "content": QUESTIONS_USER_TEMPLATE.format(
                spec_json=json.dumps(spec, indent=2),
                unresolved_text=format_unresolved(unresolved),
                max_questions=max_questions)},
        ]

    # -- parsers ------------------------------------------------------------

    @staticmethod
    def parse_spec(response: str) -> Optional[Dict[str, Any]]:
        obj = _parse_json_object(response)
        return normalize_spec(obj) if obj is not None else None

    @staticmethod
    def parse_questions(response: str) -> List[str]:
        obj = _parse_json_object(response)
        if obj and isinstance(obj.get("questions"), list):
            return [q.strip() for q in obj["questions"]
                    if isinstance(q, str) and q.strip()]
        return []

    # -- one-shot helpers (single sample; the pipeline batches these) -------

    def extract_spec(self, prompt: str,
                     stage_label: str = "USER DESCRIPTION") -> Dict[str, Any]:
        resp = self.llm.inference(
            self.extract_messages(prompt, stage_label),
            max_new_tokens=self.max_new_tokens, temperature=0)
        return self.parse_spec(resp) or empty_spec()

    def incorporate_qa(self, spec: Dict[str, Any], qa_text: str
                       ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Apply a round of Q&A. Returns (merged_spec, resolutions_this_round)."""
        before = len(spec.get("resolutions", []))
        resp = self.llm.inference(
            self.update_messages(spec, qa_text),
            max_new_tokens=self.max_new_tokens, temperature=0)
        updated = self.parse_spec(resp)
        if updated is None:
            return spec, []
        merged = merge_specs(spec, updated)
        return merged, merged.get("resolutions", [])[before:]

    def propose_questions(self, spec: Dict[str, Any],
                          max_questions: int = 5) -> List[str]:
        unresolved = unresolved_fields(spec)
        if not unresolved:
            return []
        try:
            resp = self.llm.inference(
                self.questions_messages(spec, unresolved, max_questions),
                max_new_tokens=512, temperature=0)
            questions = self.parse_questions(resp)
            if questions:
                return questions[:max_questions]
        except Exception:
            pass
        return template_questions(unresolved, max_questions)


def template_questions(unresolved: List[Dict[str, str]],
                       max_questions: int = 5) -> List[str]:
    """Deterministic fallback questions, one per unresolved field."""
    questions = []
    for u in unresolved[:max_questions]:
        fid, ftype, param = u["feature_id"], u["feature_type"], u["param"]
        if u["reason"] == "missing":
            questions.append(
                f"What is the {param} of {ftype} '{fid}'? "
                f"Please give a numeric value in millimetres (or specify the unit).")
        else:
            questions.append(
                f"The {param} of {ftype} '{fid}' was assumed to be "
                f"{u.get('assumed_value')} {u.get('unit', 'mm')}. "
                f"Is this correct? If not, please provide the intended value.")
    return questions
