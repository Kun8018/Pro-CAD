"""
Spec-grounded Verifier.

Unlike a geometric/visual self-check (CADCodeVerify-style), this verifier aligns
the generated CadQuery code back to the STRUCTURED SPEC produced by the Spec
Manager, field by field. It runs WITHOUT executing the code (pure AST analysis),
so it needs no cadquery install, no GPU and no GT mesh, and it provides the
test-time signal that Chamfer distance cannot (no GT exists at deployment).

Three deterministic checks:

  V1 CONFIRMED_VALUE_MISSING
        a value tagged user-stated / answer-confirmed in the spec has no
        matching numeric literal in the code -> the Coder dropped or guessed it.

  V2 UNSUPPORTED_LITERAL
        a dimensional numeric literal in the code matches no value in the spec
        and is not a structural constant (0/1, angles 90/180/360, segment counts,
        ...) -> a dimension the Coder invented.

  V3 ASSUMED_FIELD_USED
        the code implements a feature whose only spec value is agent-assumed.
        These are not failures by themselves; they are the set of fields that
        must be routed back to the Clarifier for another question round rather
        than silently accepted.

An optional LLM semantic channel (config/spec_verification.py) handles
non-numeric facts (feature types, boolean operations, placement) and is invoked
through verify_with_llm().
"""

import ast
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from src.spec_manager import (
    AGENT_ASSUMED, ANSWER_CONFIRMED, USER_STATED,
    _as_mm, iter_features, normalize_spec,
)

CONFIRMED_SOURCES = {USER_STATED, ANSWER_CONFIRMED}

# Numeric constants that are structural, not geometric dimensions.
# angles, segment counts, booleans-as-ints, small offsets, selector strings are
# handled separately (string selectors carry no number here).
_STRUCTURAL_NUMBERS = {0.0, 1.0, -1.0, 2.0, 90.0, 180.0, 270.0, 360.0, 45.0,
                       30.0, 60.0, 120.0, 15.0, 75.0}

# Call attribute names whose arguments are dimensional (mm) values.
_DIMENSIONAL_CALLS = {
    "box", "circle", "extrude", "rect", "sphere", "cylinder", "circle",
    "fillet", "chamfer", "moveTo", "lineTo", "spline", "close", "hLine",
    "vLine", "threePointArc", "sagittaArc", "tangentArc", "radiusArc",
    "offset", "workplane", "center", "hole", "cboreHole", "cskHole",
    "pushPoints", "polarArray", "polarLocations", "rarray", "rect",
    "split", "thickness", "shell", "wedge", "loft", "revolve",
}

# keyword names that are counts/angles/quality, not mm dimensions
_NONDIM_KW = {"angle", "count", "n", "combine", "clean", "tolerance",
              "forConstruction", "centered", "both", "direct", "type",
              "wires", "kind", "rule", "mode", "reverse", "weld", "filled"}


class _LiteralCollector(ast.NodeVisitor):
    """Collects numeric literals that flow into dimensional CadQuery calls."""

    def __init__(self):
        # (value, call_name, positional_index_or_keyword)
        self.literals: List[Tuple[float, str, str]] = []
        # every numeric literal anywhere (used as a weaker fallback check)
        self.all_literals: Set[float] = set()
        self._call_stack: List[str] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return
        self.all_literals.add(float(node.value))
        if self._call_stack:
            call_name, kw = self._call_stack[-1]
            if call_name in _DIMENSIONAL_CALLS and kw not in _NONDIM_KW:
                self.literals.append((float(node.value), call_name, kw))

    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node.func)
        # positional args
        self._call_stack.append((name, ""))
        for arg in node.args:
            self.visit(arg)
        self._call_stack.pop()
        # keyword args
        for kw in node.keywords:
            self._call_stack.append((name, kw.arg or ""))
            self.visit(kw.value)
            self._call_stack.pop()
        # also walk chained receivers (cq.workplane(...).circle(...))
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
            self.visit(node.func.value)
        elif isinstance(node.func, ast.Attribute):
            self.visit(node.func.value)

    @staticmethod
    def _call_name(func: ast.AST) -> str:
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return ""


def extract_code_literals(code: str) -> Dict[str, Any]:
    """Parse CadQuery source (without executing it) and pull dimensional
    numeric literals plus the call contexts they appear in."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"parse_error": str(e), "literals": [], "all_literals": set()}
    collector = _LiteralCollector()
    collector.visit(tree)
    return {
        "parse_error": None,
        "literals": collector.literals,
        "all_literals": collector.all_literals,
    }


def _numbers_close(a: float, b: float) -> bool:
    if a == b:
        return True
    return abs(a - b) <= max(1e-3, 0.005 * max(abs(a), abs(b)))


def _spec_numeric_values(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All numeric parameter values in the spec, with provenance."""
    out = []
    for f in iter_features(spec):
        for name, entry in f.get("parameters", {}).items():
            v = _as_mm(entry)
            if v is not None:
                out.append({
                    "feature_id": f["id"], "feature_type": f["type"],
                    "param": name, "value": v,
                    "source": entry.get("source"),
                    "unit": entry.get("unit") or spec.get("unit", "mm"),
                })
    return out


def verify_code_against_spec(spec: Dict[str, Any], code: str,
                             strict_all_literals: bool = False) -> Dict[str, Any]:
    """
    Deterministic spec-grounded verification of generated CadQuery code.

    Returns:
        {
          "passed": bool,                 # True if no blocking issue
          "issues": [ {code, severity, feature_id, param, ...} ],
          "assumed_to_reclarify": [...],  # V3 fields routed to the Clarifier
          "checked_fields": int,
        }
    """
    spec = normalize_spec(spec)
    parsed = extract_code_literals(code)
    if parsed["parse_error"]:
        return {
            "passed": False,
            "issues": [{
                "code": "CODE_SYNTAX_ERROR",
                "severity": "blocking",
                "detail": parsed["parse_error"],
            }],
            "assumed_to_reclarify": [],
            "checked_fields": 0,
        }

    dim_literals = [v for v, _, _ in parsed["literals"]]
    pool = dim_literals if dim_literals or not strict_all_literals \
        else sorted(parsed["all_literals"])
    # when no dimensional call was recognized at all, fall back to all literals
    if not dim_literals:
        pool = sorted(parsed["all_literals"])

    # literals inside radius/diameter-taking calls; duality is only allowed
    # against these (otherwise plate thickness 10 could ground hole radius 5).
    _RADIAL_CALLS = {"circle", "hole", "cboreHole", "cskHole", "cylinder",
                     "sphere", "ellipse", "revolve"}
    radial_pool = [v for v, call_name, _ in parsed["literals"]
                   if call_name in _RADIAL_CALLS]

    issues: List[Dict[str, Any]] = []
    assumed: List[Dict[str, Any]] = []
    numeric_fields = _spec_numeric_values(spec)

    # V1: every confirmed value must appear in the code.
    for field in numeric_fields:
        if field["source"] not in CONFIRMED_SOURCES:
            continue

        def _grounded(lit: float) -> bool:
            if _numbers_close(field["value"], lit):
                return True
            # radius <-> diameter duality (circle() takes radius; hole() takes
            # diameter), applied only to literals in radial call contexts
            if field["param"] in ("radius", "diameter") and radial_pool:
                return any(
                    _numbers_close(field["value"], rlit)
                    or _numbers_close(field["value"], rlit * 2.0)
                    or _numbers_close(field["value"], rlit / 2.0)
                    for rlit in radial_pool)
            return False

        if not any(_grounded(lit) for lit in pool):
            issues.append({
                "code": "CONFIRMED_VALUE_MISSING",
                "severity": "blocking",
                "feature_id": field["feature_id"],
                "feature_type": field["feature_type"],
                "param": field["param"],
                "expected_mm": field["value"],
                "detail": (f"{field['feature_id']}.{field['param']}="
                           f"{field['value']} mm is {field['source']} in the "
                           f"spec but no matching value appears in the code."),
            })
        if field["source"] == AGENT_ASSUMED:
            assumed.append(field)

    # V3: assumed fields actually realized in code
    for field in numeric_fields:
        if field["source"] != AGENT_ASSUMED:
            continue
        if any(_numbers_close(field["value"], lit) for lit in pool):
            assumed.append(field)

    # V2: dimensional code literals must be supported by some spec value
    spec_values = [f["value"] for f in numeric_fields]
    for value, call_name, where in parsed["literals"]:
        if value in _STRUCTURAL_NUMBERS or abs(value) < 1e-9:
            continue
        if any(_numbers_close(value, sv) for sv in spec_values):
            continue
        # radius/diameter duality: code circle() takes radius; a spec diameter D
        # corresponds to code value D/2, and vice versa
        dual = value * 2.0
        dual_inv = value / 2.0
        if any(_numbers_close(dual, sv) or _numbers_close(dual_inv, sv)
               for sv in spec_values):
            continue
        issues.append({
            "code": "UNSUPPORTED_LITERAL",
            "severity": "warning",
            "call": f".{call_name}({where})".replace("()", ""),
            "literal_mm": value,
            "detail": (f"Code uses {value} in .{call_name}() but no spec "
                       f"parameter supports that value."),
        })

    blocking = [i for i in issues if i["severity"] == "blocking"]
    # de-duplicate assumed fields
    seen = set()
    assumed_unique = []
    for a in assumed:
        key = (a["feature_id"], a["param"])
        if key not in seen:
            seen.add(key)
            assumed_unique.append(a)

    return {
        "passed": not blocking,
        "issues": issues,
        "assumed_to_reclarify": assumed_unique,
        "checked_fields": len([f for f in numeric_fields
                               if f["source"] in CONFIRMED_SOURCES]),
    }


def build_reclarify_request(verification: Dict[str, Any]) -> Optional[str]:
    """
    Route verification failures back to the Clarifier (NOT a code patch).

    Returns a question prompt covering blocking issues and unconfirmed assumed
    fields, or None when the code is fully grounded.
    """
    issues = verification.get("issues", [])
    missing = [i for i in issues if i["code"] == "CONFIRMED_VALUE_MISSING"]
    assumed = verification.get("assumed_to_reclarify", [])
    if not missing and not assumed:
        return None

    lines = ["The structured specification could not be grounded in the "
             "generated code. Please clarify the following before regenerating:"]
    for i, item in enumerate(missing, 1):
        lines.append(f"{i}. {item['detail']}")
    for j, item in enumerate(assumed, len(missing) + 1):
        lines.append(
            f"{j}. The value of {item['feature_id']}.{item['param']} "
            f"({item['value']} mm) is an unconfirmed default. "
            f"Please confirm it or give the intended value.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional LLM semantic channel (non-numeric facts)
# ---------------------------------------------------------------------------

def verify_with_llm(spec: Dict[str, Any], code: str, llm: Any,
                    model_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Semantic per-field verification: feature types, boolean operations,
    placement — facts the numeric channel cannot see. The judge must cite the
    spec field each finding relates to; it has no access to the original prompt.
    """
    import json
    from config.spec_verification import (
        SPEC_VERIFIER_SYSTEM_PROMPT,
        SPEC_VERIFIER_USER_TEMPLATE,
    )

    messages = [
        {"role": "system", "content": SPEC_VERIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": SPEC_VERIFIER_USER_TEMPLATE.format(
            spec_json=json.dumps(normalize_spec(spec), indent=2),
            code=code)},
    ]
    resp = llm.inference(messages, max_new_tokens=1024, temperature=0)
    m = re.search(r"\{.*\}", resp, re.DOTALL)
    if not m:
        return {"passed": None, "issues": [], "error": "unparseable judge response"}
    import json as _json
    parsed = _json.loads(m.group(0))
    parsed.setdefault("issues", [])
    parsed["passed"] = not any(
        i.get("severity") == "blocking" for i in parsed["issues"])
    parsed["channel"] = "llm"
    return parsed
