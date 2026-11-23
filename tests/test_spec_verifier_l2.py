"""Offline unit tests for the L2 B-rep channel (src/spec_verifier_l2.py).

The comparison logic is pure, so it is tested here with hand-built measurement
dicts — no cadquery / OCP install is required. measure_b_rep's success path is
covered with a fake cadquery injected into sys.modules.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spec_manager import (  # noqa: E402
    AGENT_ASSUMED, ANSWER_CONFIRMED, USER_STATED, _entry, normalize_spec,
)
from src.spec_verifier_l2 import (  # noqa: E402
    _match_box_dims, _spec_box_dims, _spec_radii,
    measure_b_rep, verify_b_rep_against_spec,
)


def box_spec(length, width, height, source=USER_STATED):
    return {
        "unit": "mm",
        "features": [{
            "id": "plate_1", "type": "plate", "operation": "add",
            "parameters": {
                "length": _entry(length, source, "mm"),
                "width": _entry(width, source, "mm"),
                "thickness": _entry(height, source, "mm"),
            },
            "placement": [],
        }],
        "constraints": [],
    }


def meas(bbox=(60, 40, 10), radii=(), volume=24000.0,
         n_solids=1, n_faces=6, n_edges=12):
    return {
        "available": True,
        "bbox": {"x": bbox[0], "y": bbox[1], "z": bbox[2]},
        "volume": volume,
        "n_solids": n_solids,
        "n_faces": n_faces,
        "n_edges": n_edges,
        "cylinder_radii": sorted(radii),
        "error": None,
    }


class TestMatchBoxDims(unittest.TestCase):

    def test_exact(self):
        self.assertTrue(_match_box_dims([60, 40, 10], [60, 40, 10], 0.05, 0.01))

    def test_permutation_invariant(self):
        self.assertTrue(_match_box_dims([60, 40, 10], [10, 60, 40], 0.05, 0.01))

    def test_mismatch(self):
        self.assertFalse(_match_box_dims([60, 40, 10], [60, 40, 12], 0.05, 0.01))


class TestSpecExtraction(unittest.TestCase):

    def test_box_dims(self):
        dims = _spec_box_dims(box_spec(60, 40, 10))
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["length"], 60)
        self.assertEqual(dims[0]["height"], 10)

    def test_radii_halve_diameter(self):
        spec = normalize_spec({
            "features": [{
                "id": "h", "type": "hole",
                "parameters": {"diameter": _entry(10, USER_STATED, "mm")},
            }],
        })
        radii = _spec_radii(spec)
        self.assertEqual(len(radii), 1)
        self.assertEqual(radii[0]["radius"], 5.0)


class TestVerifyBRepAgainstSpec(unittest.TestCase):

    def test_clean_box_passes(self):
        r = verify_b_rep_against_spec(box_spec(60, 40, 10), meas())
        self.assertTrue(r["passed"], msg=str(r["issues"]))

    def test_box_dim_mismatch_blocking(self):
        r = verify_b_rep_against_spec(
            box_spec(60, 40, 10), meas(bbox=(60, 40, 12)))
        self.assertFalse(r["passed"])
        codes = {i["code"] for i in r["issues"]}
        self.assertIn("B_REP_DIM_MISMATCH", codes)

    def test_radius_missing_blocking(self):
        spec = normalize_spec({
            "unit": "mm",
            "features": [{
                "id": "hole_1", "type": "hole", "operation": "subtract",
                "parameters": {"radius": _entry(5, ANSWER_CONFIRMED, "mm"),
                               "depth": _entry("through", USER_STATED)},
                "placement": [],
            }],
            "constraints": [],
        })
        r = verify_b_rep_against_spec(spec, meas(bbox=(60, 40, 10), radii=()))
        codes = {i["code"] for i in r["issues"]}
        self.assertIn("B_REP_RADIUS_MISSING", codes)
        self.assertFalse(r["passed"])

    def test_radius_match_passes(self):
        spec = normalize_spec({
            "unit": "mm",
            "features": [{
                "id": "hole_1", "type": "hole", "operation": "subtract",
                "parameters": {"radius": _entry(5, ANSWER_CONFIRMED, "mm")},
                "placement": [],
            }],
            "constraints": [],
        })
        r = verify_b_rep_against_spec(spec, meas(radii=[5.0]))
        self.assertTrue(r["passed"], msg=str(r["issues"]))

    def test_assumed_radius_not_blocking(self):
        # unconfirmed defaults are V3's job, not a geometric failure in L2
        spec = normalize_spec({
            "features": [{
                "id": "boss_1", "type": "boss",
                "parameters": {"radius": _entry(4, AGENT_ASSUMED, "mm")},
            }],
        })
        r = verify_b_rep_against_spec(spec, meas(radii=()))
        self.assertTrue(r["passed"], msg=str(r["issues"]))

    def test_unavailable_measurements(self):
        r = verify_b_rep_against_spec(
            box_spec(60, 40, 10),
            {"available": False, "error": "cadquery unavailable: boom"})
        self.assertIsNone(r["passed"])
        self.assertEqual(r["issues"][0]["code"], "B_REP_UNAVAILABLE")


class _FakeFace:
    def __init__(self, geom_type, radius=None):
        self._t, self._r = geom_type, radius

    def geomType(self):
        return self._t

    def radius(self):
        return self._r


class _FakeShape:
    def __init__(self, x, y, z, radii):
        self._x, self._y, self._z = x, y, z
        self._radii = radii

    def BoundingBox(self):
        box = types.SimpleNamespace(xlen=self._x, ylen=self._y, zlen=self._z)
        return box

    def Volume(self):
        return self._x * self._y * self._z

    def Solids(self):
        return [object()]

    def Faces(self):
        return [_FakeFace("CYLINDER", r) for r in self._radii]

    def Edges(self):
        return [object() for _ in range(12)]


class _FakeCQ:
    @staticmethod
    def Workplane(plane):
        return _FakeWorkplane()


class _FakeWorkplane:
    def box(self, x, y, z):
        return _FakeResult(_FakeShape(x, y, z, []))

    def circle(self, r):
        return self

    def extrude(self, h):
        return _FakeResult(_FakeShape(0, 0, 0, []))


class _FakeResult:
    def __init__(self, shape):
        self._shape = shape

    def val(self):
        return self._shape


class TestMeasureBRep(unittest.TestCase):

    def test_measure_with_fake_cadquery(self):
        sys.modules["cadquery"] = _FakeCQ
        try:
            m = measure_b_rep(
                "result = cq.Workplane('XY').box(60, 40, 10)")
        finally:
            sys.modules.pop("cadquery", None)
        self.assertTrue(m["available"], msg=m.get("error"))
        self.assertAlmostEqual(m["bbox"]["x"], 60)
        self.assertAlmostEqual(m["volume"], 60 * 40 * 10)
        self.assertEqual(m["n_solids"], 1)
        self.assertEqual(m["n_edges"], 12)

    def test_missing_result_var_is_graceful(self):
        sys.modules["cadquery"] = _FakeCQ
        try:
            m = measure_b_rep("x = 1 + 1")
        finally:
            sys.modules.pop("cadquery", None)
        self.assertFalse(m["available"])
        self.assertIn("result", m["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
