"""
Offline unit tests for the spec-grounded Verifier (deterministic channel).

Run:
    python tests/test_spec_verifier.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spec_manager import (  # noqa: E402
    AGENT_ASSUMED, ANSWER_CONFIRMED, USER_STATED, _entry, normalize_spec,
)
from src.spec_verifier import (  # noqa: E402
    build_reclarify_request,
    extract_code_literals,
    verify_code_against_spec,
)


PLATE_SPEC = {
    "unit": "mm",
    "features": [
        {"id": "base_plate", "type": "plate", "operation": "add",
         "parameters": {
             "length": _entry(60, USER_STATED, "mm"),
             "width": _entry(40, USER_STATED, "mm"),
             "thickness": _entry(10, USER_STATED, "mm"),
         },
         "placement": []},
        {"id": "hole_1", "type": "hole", "operation": "subtract",
         "parameters": {
             "radius": _entry(5, ANSWER_CONFIRMED, "mm"),
             "depth": _entry("through", USER_STATED),
         },
         "placement": []},
    ],
    "constraints": [],
}

GOOD_CODE = """
import cadquery as cq
result = (cq.Workplane("XY").box(60, 40, 10)
          .faces(">Z").workplane()
          .pushPoints([(0, 0)])
          .hole(10))
"""

DROPPED_CODE = """
import cadquery as cq
result = cq.Workplane("XY").box(60, 40, 10)
"""

INVENTED_CODE = """
import cadquery as cq
result = (cq.Workplane("XY").box(60, 40, 10)
          .faces(">Z").workplane().hole(10)
          .edges("|Z").fillet(3.0))
"""

WRONG_VALUE_CODE = """
import cadquery as cq
result = (cq.Workplane("XY").box(60, 40, 12)
          .faces(">Z").workplane().hole(16))
"""

ASSUMED_SPEC = {
    "unit": "mm",
    "features": [
        {"id": "boss_1", "type": "boss", "operation": "add",
         "parameters": {
             "radius": _entry(4, AGENT_ASSUMED, "mm"),
             "height": _entry(8, USER_STATED, "mm"),
         },
         "placement": []},
    ],
    "constraints": [],
}

ASSUMED_CODE = """
import cadquery as cq
result = cq.Workplane("XY").circle(4).extrude(8)
"""


class TestLiteralExtraction(unittest.TestCase):

    def test_extracts_dimensional_literals(self):
        parsed = extract_code_literals(GOOD_CODE)
        self.assertIsNone(parsed["parse_error"])
        values = {v for v, _, _ in parsed["literals"]}
        self.assertTrue({60.0, 40.0, 10.0} <= values)

    def test_syntax_error_reported_not_raised(self):
        parsed = extract_code_literals("definitely not python (((")
        self.assertIsNotNone(parsed["parse_error"])

    def test_variable_binding_resolves_in_call_context(self):
        code = """
import cadquery as cq
L = 60
W = 40
T = 12
result = cq.Workplane("XY").box(L, W, T)
"""
        parsed = extract_code_literals(code)
        calls = {(v, c) for v, c, _ in parsed["literals"]}
        self.assertIn((60.0, "box"), calls)
        self.assertIn((40.0, "box"), calls)
        self.assertIn((12.0, "box"), calls)

    def test_reassigned_nonliteral_binding_is_not_resolved(self):
        code = """
import cadquery as cq
r = 5
r = some_computed_value()
result = cq.Workplane("XY").circle(r)
"""
        parsed = extract_code_literals(code)
        values = {v for v, _, _ in parsed["literals"]}
        # r is no longer a literal after reassignment, so 5 must not leak in
        self.assertNotIn(5.0, values)


class TestVerification(unittest.TestCase):

    def test_good_code_passes(self):
        result = verify_code_against_spec(PLATE_SPEC, GOOD_CODE)
        self.assertTrue(result["passed"], msg=str(result["issues"]))
        # hole diameter 10 is recognized as 2 * radius 5, not an invention
        unsupported = [i for i in result["issues"]
                       if i["code"] == "UNSUPPORTED_LITERAL"]
        self.assertEqual(unsupported, [])
        self.assertEqual(result["checked_fields"], 4)

    def test_dropped_confirmed_value_is_blocking(self):
        result = verify_code_against_spec(PLATE_SPEC, DROPPED_CODE)
        self.assertFalse(result["passed"])
        missing = {(i["feature_id"], i["param"])
                   for i in result["issues"] if i["code"] == "CONFIRMED_VALUE_MISSING"}
        self.assertIn(("hole_1", "radius"), missing)

    def test_wrong_values_flagged(self):
        result = verify_code_against_spec(PLATE_SPEC, WRONG_VALUE_CODE)
        missing = {i["param"] for i in result["issues"]
                   if i["code"] == "CONFIRMED_VALUE_MISSING"}
        # thickness 10 -> code 12; hole r5 -> diameter 16 (= r8), neither matches
        self.assertIn("thickness", missing)
        self.assertIn("radius", missing)

    def test_invented_fillet_is_unsupported_literal(self):
        result = verify_code_against_spec(PLATE_SPEC, INVENTED_CODE)
        invented = [i for i in result["issues"]
                    if i["code"] == "UNSUPPORTED_LITERAL" and i["literal_mm"] == 3.0]
        self.assertEqual(len(invented), 1)
        self.assertEqual(invented[0]["severity"], "warning")
        # warnings alone do not block
        self.assertTrue(result["passed"])

    def test_assumed_value_routed_back_to_clarifier(self):
        result = verify_code_against_spec(ASSUMED_SPEC, ASSUMED_CODE)
        fields = {(f["feature_id"], f["param"])
                  for f in result["assumed_to_reclarify"]}
        self.assertIn(("boss_1", "radius"), fields)

        request = build_reclarify_request(result)
        self.assertIsNotNone(request)
        self.assertIn("boss_1.radius", request)
        self.assertIn("unconfirmed default", request)

    def test_clean_result_needs_no_reclarification(self):
        result = verify_code_against_spec(PLATE_SPEC, GOOD_CODE)
        self.assertIsNone(build_reclarify_request(result))

    def test_syntax_error_is_blocking(self):
        result = verify_code_against_spec(PLATE_SPEC, "(((")
        self.assertFalse(result["passed"])
        self.assertEqual(result["issues"][0]["code"], "CODE_SYNTAX_ERROR")

    def test_unit_normalization_in_spec(self):
        # same part, radius expressed as 0.5 cm -> 5 mm; hole(10) must match
        spec = normalize_spec(PLATE_SPEC)
        spec["features"][1]["parameters"]["radius"]["value"] = 0.5
        spec["features"][1]["parameters"]["radius"]["unit"] = "cm"
        result = verify_code_against_spec(spec, GOOD_CODE)
        self.assertTrue(result["passed"], msg=str(result["issues"]))

    def test_wrong_value_passed_via_variable_is_flagged(self):
        code = """
import cadquery as cq
L = 60
W = 40
T = 12
result = cq.Workplane("XY").box(L, W, T)
"""
        result = verify_code_against_spec(PLATE_SPEC, code)
        missing = {i["param"] for i in result["issues"]
                   if i["code"] == "CONFIRMED_VALUE_MISSING"}
        self.assertIn("thickness", missing)
        unsupported = [i for i in result["issues"]
                       if i["code"] == "UNSUPPORTED_LITERAL"
                       and i["literal_mm"] == 12.0]
        self.assertEqual(len(unsupported), 1)

    def test_v2_duality_restricted_to_radial_calls(self):
        # spec plate length is 17 mm; code box length is 34 (= 2 * 17).
        # `box` is a linear call, so 34 must be UNSUPPORTED, not grounded by
        # the radius<->diameter duality (which only applies to radial calls).
        # (34 is chosen because 30 is a structural angle and would be skipped.)
        spec = {
            "unit": "mm",
            "features": [{
                "id": "plate_1", "type": "plate", "operation": "add",
                "parameters": {
                    "length": _entry(17, USER_STATED, "mm"),
                    "width": _entry(8, USER_STATED, "mm"),
                    "thickness": _entry(2, USER_STATED, "mm"),
                },
                "placement": [],
            }],
            "constraints": [],
        }
        code = "import cadquery as cq\nresult = cq.Workplane('XY').box(34, 8, 2)"
        result = verify_code_against_spec(spec, code)
        unsupported = [i for i in result["issues"]
                       if i["code"] == "UNSUPPORTED_LITERAL"
                       and i["literal_mm"] == 34.0]
        self.assertEqual(len(unsupported), 1)
        self.assertFalse(result["passed"])  # length 17 is also missing


if __name__ == "__main__":
    unittest.main(verbosity=2)
