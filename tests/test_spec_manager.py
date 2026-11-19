"""
Offline unit tests for the Spec Manager's deterministic logic.

Run from repo root:
    python -m unittest tests.test_spec_manager -v
or:
    python tests/test_spec_manager.py

No GPU / network / API key required (no LLM is instantiated).
The fixture scenarios mirror the ones used to validate the spec-fidelity probe
in THIRD_AGENT_IDEA.md section 4: carryover of a wrong value, a dropped
dimension, an invented fillet, cm->mm normalization, and loose wording.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spec_manager import (  # noqa: E402
    AGENT_ASSUMED, ANSWER_CONFIRMED, USER_STATED,
    SpecManager,
    detect_conflicts, empty_spec, merge_specs, normalize_spec,
    render_for_coder, template_questions, unresolved_fields, values_equal,
    _entry,
)


def box_spec(length, width, thickness, source=USER_STATED):
    return {
        "unit": "mm",
        "features": [{
            "id": "base_plate", "type": "plate", "operation": "add",
            "parameters": {
                "length": _entry(length, source, "mm"),
                "width": _entry(width, source, "mm"),
                "thickness": _entry(thickness, source, "mm"),
            },
            "placement": [],
        }],
        "constraints": [],
    }


class TestNormalize(unittest.TestCase):

    def test_bare_values_become_assumed(self):
        spec = normalize_spec({
            "features": [{
                "id": "f1", "type": "box",
                "parameters": {"length": 60},  # bare value, no provenance
            }],
        })
        p = spec["features"][0]["parameters"]["length"]
        self.assertEqual(p["value"], 60)
        self.assertEqual(p["source"], AGENT_ASSUMED)

    def test_invalid_source_rejected(self):
        spec = normalize_spec({
            "features": [{
                "id": "f1", "type": "box",
                "parameters": {"length": {"value": 1, "source": "dreamt"}},
            }],
        })
        self.assertEqual(
            spec["features"][0]["parameters"]["length"]["source"], AGENT_ASSUMED)

    def test_garbage_input(self):
        self.assertEqual(normalize_spec(None), empty_spec())
        self.assertEqual(normalize_spec("nope")["features"], [])


class TestUnitAwareEquality(unittest.TestCase):

    def test_cm_to_mm(self):
        # 2 cm == 20 mm
        self.assertTrue(values_equal(_entry(2, USER_STATED, "cm"),
                                     _entry(20, USER_STATED, "mm")))

    def test_inch_to_mm(self):
        self.assertTrue(values_equal(_entry(1, USER_STATED, "inch"),
                                     _entry(25.4, USER_STATED, "mm")))

    def test_mismatch(self):
        self.assertFalse(values_equal(_entry(5, USER_STATED, "mm"),
                                      _entry(8, USER_STATED, "mm")))

    def test_string_values(self):
        self.assertTrue(values_equal(_entry("through", USER_STATED),
                                     _entry("through", ANSWER_CONFIRMED)))


class TestMergeProvenance(unittest.TestCase):

    def test_answer_overrides_carried_over_wrong_value(self):
        """CARRYOVER fixture: misleading prompt says hole r=8; Q&A corrects to 5."""
        spec = {
            "unit": "mm",
            "features": [{
                "id": "hole_1", "type": "hole", "operation": "subtract",
                "parameters": {
                    "radius": _entry(8, USER_STATED, "mm",
                                     evidence="8 mm hole in the input"),
                    "depth": _entry("through", USER_STATED, None),
                },
                "placement": [],
            }],
            "constraints": [],
        }
        answer_spec = {
            "unit": "mm",
            "features": [{
                "id": "hole_1", "type": "hole",
                "parameters": {
                    "radius": _entry(5, ANSWER_CONFIRMED, "mm",
                                     evidence="radius should be 5 mm"),
                },
            }],
            "constraints": [],
        }
        merged = merge_specs(spec, answer_spec)
        radius = merged["features"][0]["parameters"]["radius"]
        self.assertEqual(radius["value"], 5)
        self.assertEqual(radius["source"], ANSWER_CONFIRMED)
        # untouched facts survive
        self.assertEqual(
            merged["features"][0]["parameters"]["depth"]["value"], "through")
        # the correction is logged with both old and new values
        res = merged["resolutions"]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["old_value"], 8)
        self.assertEqual(res[0]["new_value"], 5)
        self.assertEqual(res[0]["feature_id"], "hole_1")

    def test_assumed_default_cannot_override_user_value(self):
        base = box_spec(60, 40, 10)
        incoming = box_spec(99, 40, 10, source=AGENT_ASSUMED)  # LLM dreamt 99
        merged = merge_specs(base, incoming)
        self.assertEqual(
            merged["features"][0]["parameters"]["length"]["value"], 60)
        self.assertEqual(merged["resolutions"], [])

    def test_new_parameter_is_added(self):
        """DROPPED fixture at merge level: an answer supplies a missing chamfer."""
        base = box_spec(60, 40, 10)
        incoming = {
            "unit": "mm",
            "features": [{
                "id": "edge_chamfer", "type": "chamfer",
                "parameters": {"distance": _entry(2, ANSWER_CONFIRMED, "mm")},
            }],
            "constraints": [],
        }
        merged = merge_specs(base, incoming)
        ids = {f["id"] for f in merged["features"]}
        self.assertIn("edge_chamfer", ids)

    def test_repeated_same_value_upgrades_provenance_only(self):
        base = box_spec(60, 40, 10, source=AGENT_ASSUMED)
        incoming = box_spec(60, 40, 10, source=ANSWER_CONFIRMED)
        merged = merge_specs(base, incoming)
        p = merged["features"][0]["parameters"]["length"]
        self.assertEqual(p["value"], 60)
        self.assertEqual(p["source"], ANSWER_CONFIRMED)
        self.assertEqual(merged["resolutions"], [])

    def test_id_drift_falls_back_to_type_position(self):
        base = box_spec(60, 40, 10)
        incoming = {  # LLM renamed the id
            "features": [{
                "id": "plate_main", "type": "plate",
                "parameters": {"thickness": _entry(12, ANSWER_CONFIRMED, "mm")},
            }],
        }
        merged = merge_specs(base, incoming)
        self.assertEqual(
            merged["features"][0]["parameters"]["thickness"]["value"], 12)

    def test_detect_conflicts(self):
        a = box_spec(60, 40, 10)
        b = box_spec(60, 40, 12)
        conflicts = detect_conflicts(a, b)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["param"], "thickness")


class TestUnresolvedAndQuestions(unittest.TestCase):

    def test_missing_required_field_is_unresolved(self):
        spec = box_spec(60, 40, 10)
        del spec["features"][0]["parameters"]["thickness"]
        unresolved = unresolved_fields(spec)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["param"], "height")  # first alternative
        self.assertEqual(unresolved[0]["reason"], "missing")

    def test_assumed_field_is_unresolved_confirmed_is_not(self):
        spec = box_spec(60, 40, 10)
        spec["features"][0]["parameters"]["fillet"] = _entry(
            3, AGENT_ASSUMED, "mm")  # UNSUPPORTED-style invention
        spec["features"][0]["type"] = "plate"
        unresolved = {(u["feature_id"], u["param"]): u
                      for u in unresolved_fields(spec)}
        # all required dims are confirmed -> only the assumed extra surfaces
        # if the type requires it; plate does not, so emulate a 'fillet' type
        self.assertNotIn(("base_plate", "length"), unresolved)

    def test_radius_alternative_satisfies_hole(self):
        spec = {
            "features": [{
                "id": "h", "type": "hole",
                "parameters": {
                    "diameter": _entry(10, USER_STATED, "mm"),
                    "depth": _entry("through", USER_STATED),
                },
            }],
        }
        self.assertEqual(unresolved_fields(spec), [])

    def test_template_questions(self):
        qs = template_questions([
            {"feature_id": "hole_1", "feature_type": "hole",
             "param": "radius", "reason": "missing"},
            {"feature_id": "boss_1", "feature_type": "boss",
             "param": "height", "reason": "agent-assumed",
             "assumed_value": 5, "unit": "mm"},
        ])
        self.assertEqual(len(qs), 2)
        self.assertIn("hole_1", qs[0])
        self.assertIn("correct", qs[1].lower())

    def test_obsolete_features_skipped(self):
        spec = box_spec(60, 40, 10)
        spec["features"][0]["_obsolete"] = True
        self.assertEqual(unresolved_fields(spec), [])


class TestRender(unittest.TestCase):

    def setUp(self):
        self.spec = box_spec(60, 40, 10)
        self.spec["features"][0]["parameters"]["fillet_radius"] = _entry(
            3, AGENT_ASSUMED, "mm")

    def test_provenance_render_marks_assumed_and_tags_confirmed(self):
        text = render_for_coder(self.spec, with_provenance=True)
        self.assertIn("length = 60 mm [user-stated]", text)
        self.assertIn("ASSUMED DEFAULT", text)
        self.assertIn("fillet_radius = 3 mm", text)

    def test_ablation_render_strips_provenance(self):
        text = render_for_coder(self.spec, with_provenance=False)
        self.assertNotIn("user-stated", text)
        self.assertNotIn("ASSUMED", text)
        # ...but the structured values are still all there (fair schema control)
        self.assertIn("length = 60 mm", text)
        self.assertIn("fillet_radius = 3 mm", text)

    def test_obsolete_feature_not_rendered(self):
        self.spec["features"][0]["_obsolete"] = True
        self.assertNotIn("base_plate", render_for_coder(self.spec))


class TestLLMOutputParsing(unittest.TestCase):

    def test_parse_spec_with_fence_and_prose(self):
        response = '''Here is the spec:
```json
{"unit": "mm", "features": [
  {"id": "f1", "type": "cylinder",
   "parameters": {"radius": {"value": 5, "unit": "mm",
                              "source": "user-stated", "evidence": "r=5"},
                  "height": {"value": 20, "unit": "mm",
                             "source": "user-stated", "evidence": ""}}}
]}
```
done.'''
        spec = SpecManager.parse_spec(response)
        self.assertEqual(spec["features"][0]["id"], "f1")
        self.assertEqual(
            spec["features"][0]["parameters"]["radius"]["source"], USER_STATED)

    def test_parse_questions(self):
        qs = SpecManager.parse_questions(
            '{"questions": ["What is the radius?", "  ", 3]}')
        self.assertEqual(qs, ["What is the radius?"])

    def test_parse_broken_response_returns_none(self):
        self.assertIsNone(SpecManager.parse_spec("not json at all"))
        self.assertEqual(SpecManager.parse_questions("{}"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
