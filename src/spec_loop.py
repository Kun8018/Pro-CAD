"""
Closed-loop routing: turn verifier verdicts into a single action.

The deterministic core of the loop in THIRD_AGENT_IDEA.md section 3 / the paper's
"routing by failure type". It decides, per sample, what to do after the
spec-grounded verifier (L1 AST + L2 B-rep) reports:

  - REGENERATE   a confirmed value was dropped/altered (V1 blocking) or a
                 measured B-rep disagrees with the spec (L2 blocking). The
                 information already exists and is confirmed, so re-asking the
                 user would return the same answer; instead the coder is re-run
                 with a field-level diagnostic.
  - RECLARIFY    an unconfirmed default (V3, agent-assumed) is realized in the
                 code. The information is genuinely missing from the spec, so
                 the Spec Manager asks the user before regenerating.
  - ACCEPT       nothing blocking and nothing to confirm.

Everything here is pure and unit-testable; the LLM-backed re-invocation (calling
the coder / asking the user again) lives in pipeline.py, which consumes these
decisions. A LoopBudget caps the number of interventions per sample so the loop
cannot run away and stays comparable to an unconditional extra round.
"""

from typing import Any, Dict, Optional

ACCEPT = "accept"
RECLARIFY = "reclarify"
REGENERATE = "regenerate"


def route(verification: Dict[str, Any]) -> str:
    """Map a verification result to its routing action.

    Matches the paper's "routing by failure type":
      - V1 CONFIRMED_VALUE_MISSING (blocking), V2 UNSUPPORTED_LITERAL (warning,
        but the user's info is already complete so re-asking returns the same
        answer) and any L2 geometric blocking issue -> REGENERATE.
      - V3 assumed-but-unconfirmed fields realized in code -> RECLARIFY.
      - otherwise -> ACCEPT.
    """
    issues = verification.get("issues", []) or []
    regenerate = any(
        i.get("severity") == "blocking" or i.get("code") == "UNSUPPORTED_LITERAL"
        for i in issues
    )
    if regenerate:
        return REGENERATE
    if verification.get("assumed_to_reclarify"):
        return RECLARIFY
    return ACCEPT


def build_regeneration_diagnostic(verification: Dict[str, Any],
                                  code: Optional[str] = None) -> str:
    """Field-level diagnostic for a regenerate-routed failure.

    The coder receives this instead of a bare "try again", so it can target the
    exact field that failed without a new user interaction. Covers V1 (missing
    confirmed value), V2 (invented literal) and L2 geometric issues.
    """
    issues = verification.get("issues", []) or []
    regenerate = [i for i in issues
                  if i.get("severity") == "blocking"
                  or i.get("code") == "UNSUPPORTED_LITERAL"]
    if not regenerate:
        return ""
    lines = ["The generated program does not realize the confirmed "
             "specification. Fix the following, keeping everything else "
             "unchanged:"]
    for i in regenerate:
        code_name = i.get("code")
        if code_name == "CONFIRMED_VALUE_MISSING":
            lines.append(
                f"- Add/restore {i.get('feature_id')}.{i.get('param')} = "
                f"{i.get('expected_mm')} mm.")
        elif code_name == "UNSUPPORTED_LITERAL":
            lines.append(
                f"- Remove/replace the dimension {i.get('literal_mm')} in "
                f"{i.get('call', 'the code')}: it is not supported by any "
                f"specification value.")
        else:
            lines.append(f"- {i.get('detail') or code_name}")
    return "\n".join(lines)


class LoopBudget:
    """Caps interventions (re-clarify or re-generate) per sample."""

    def __init__(self, max_rounds: int = 1):
        if max_rounds < 0:
            raise ValueError("max_rounds must be >= 0")
        self.max_rounds = max_rounds
        self._spent: Dict[Any, int] = {}

    def remaining(self, uid: Any) -> int:
        return max(0, self.max_rounds - self._spent.get(uid, 0))

    def can_act(self, uid: Any) -> bool:
        return self.remaining(uid) > 0

    def record(self, uid: Any) -> None:
        """Charge one intervention round to a sample."""
        if not self.can_act(uid):
            raise RuntimeError(
                f"loop budget exceeded for sample {uid!r} "
                f"(max {self.max_rounds} rounds)")
        self._spent[uid] = self._spent.get(uid, 0) + 1
