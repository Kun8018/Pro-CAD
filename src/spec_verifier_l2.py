"""
L2: dynamic B-rep measurement channel of the spec-grounded verifier.

The L1/AST channel (spec_verifier.py) checks numeric literals WITHOUT executing
the program, so it cannot catch "correct values, wrong operations" — a hole that
became a boss, a cut applied to the wrong face, a reversed extrusion. L2 executes
the CadQuery program and measures the resulting boundary representation directly,
then compares those measurements back to the specification fields.

Measured quantities (see measure_b_rep):
  - bounding box extents (x/y/z, mm)
  - volume (mm^3)
  - face / edge / solid counts (topology)
  - radii of cylindrical faces (mm) — holes, bosses, cylinders

The comparison (verify_b_rep_against_spec) is PURE and unit-testable: it takes a
measurements dict and a spec and emits per-field issues. The cadquery dependency
is imported lazily inside measure_b_rep so the rest of the repo (and its tests)
run on machines without the CAD stack; a missing cadquery yields a graceful
"B_REP_UNAVAILABLE" result rather than an import error.
"""

from itertools import permutations
from typing import Any, Dict, List, Optional, Tuple

from src.spec_manager import (
    AGENT_ASSUMED, _as_mm, iter_features, normalize_spec,
)

# Feature types whose (length, width, height/thickness) define the bounding box.
_BOX_TYPES = {"box", "plate", "block"}

# Feature types that carry a radius/diameter parameter realized as a cylinder.
_RADIAL_TYPES = {"cylinder", "hole", "boss", "sphere", "disc", "slot"}

# Default measurement tolerances (mm). Relative + absolute, like L1.
_DEFAULT_TOL_MM = 0.05
_DEFAULT_TOL_REL = 0.01


def _close(a: float, b: float, tol_mm: float, tol_rel: float) -> bool:
    return abs(a - b) <= max(tol_mm, tol_rel * max(abs(a), abs(b)))


# ---------------------------------------------------------------------------
# Measurement (cadquery-dependent)
# ---------------------------------------------------------------------------

def measure_b_rep(code: str) -> Dict[str, Any]:
    """Execute CadQuery code and measure its B-rep.

    The code must bind a CadQuery result to a variable named ``result`` (or
    ``r``, matching mesh_utils.cadquery_to_mesh). Returns a measurements dict,
    or ``{"available": False, "error": ...}`` when the CAD stack is missing or
    execution fails. Never raises.
    """
    try:
        import cadquery as cq
    except Exception as e:  # pragma: no cover - environment dependent
        return {"available": False,
                "error": f"cadquery unavailable: {e}"}

    ns = {"cq": cq}
    try:
        exec(code, ns)
    except Exception as e:
        return {"available": False, "error": f"code execution failed: {e}"}

    shape = None
    for name in ("result", "r"):
        obj = ns.get(name)
        if obj is None:
            continue
        try:
            shape = obj.val()
            break
        except Exception:
            continue
    if shape is None:
        return {"available": False,
                "error": "code must bind 'result' or 'r' to a CadQuery object"}

    try:
        bb = shape.BoundingBox()
        radii: List[float] = []
        for face in shape.Faces():
            try:
                if face.geomType() == "CYLINDER":
                    radii.append(float(face.radius()))
            except Exception:
                continue
        return {
            "available": True,
            "bbox": {"x": float(bb.xlen), "y": float(bb.ylen),
                     "z": float(bb.zlen)},
            "volume": float(shape.Volume()),
            "n_solids": len(shape.Solids()),
            "n_faces": len(shape.Faces()),
            "n_edges": len(shape.Edges()),
            "cylinder_radii": sorted(radii),
            "error": None,
        }
    except Exception as e:
        return {"available": False, "error": f"measurement failed: {e}"}


# ---------------------------------------------------------------------------
# Pure comparison (testable without cadquery)
# ---------------------------------------------------------------------------

def _spec_box_dims(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """(length, width, height/thickness) in mm for every box-like feature."""
    out = []
    for f in iter_features(spec):
        if f["type"] not in _BOX_TYPES:
            continue
        dims: Dict[str, Optional[float]] = {}
        for name in ("length", "width"):
            if name in f.get("parameters", {}):
                dims[name] = _as_mm(f["parameters"][name])
        for alt in ("height", "thickness"):
            if alt in f.get("parameters", {}):
                dims["height"] = _as_mm(f["parameters"][alt])
                break
        if all(v is not None for v in (dims.get("length"), dims.get("width"),
                                       dims.get("height"))):
            out.append({
                "feature_id": f["id"], "feature_type": f["type"],
                "length": dims["length"], "width": dims["width"],
                "height": dims["height"],
            })
    return out


def _spec_radii(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """(radius in mm) for every radial feature; diameter is halved."""
    out = []
    for f in iter_features(spec):
        if f["type"] not in _RADIAL_TYPES:
            continue
        params = f.get("parameters", {})
        entry = params.get("radius")
        if entry is not None:
            r = _as_mm(entry)
        elif params.get("diameter") is not None:
            d = _as_mm(params["diameter"])
            r = d / 2.0 if d is not None else None
        else:
            r = None
        if r is not None:
            out.append({
                "feature_id": f["id"], "feature_type": f["type"],
                "radius": r,
                "source": (entry or params.get("diameter", {})).get("source"),
            })
    return out


def verify_b_rep_against_spec(
    spec: Dict[str, Any],
    measurements: Dict[str, Any],
    tol_mm: float = _DEFAULT_TOL_MM,
    tol_rel: float = _DEFAULT_TOL_REL,
) -> Dict[str, Any]:
    """Compare measured B-rep quantities against the spec.

    Returns {"passed", "issues", "channel": "l2", "measurements"}.
    ``passed`` is None when no measurement was available (degraded run).
    """
    spec = normalize_spec(spec)
    base = {
        "channel": "l2",
        "measurements": measurements,
    }
    if not measurements.get("available"):
        return {**base, "passed": None, "issues": [{
            "code": "B_REP_UNAVAILABLE",
            "severity": "info",
            "detail": measurements.get("error", "no B-rep measurement"),
        }]}

    issues: List[Dict[str, Any]] = []

    # L2a: bounding-box dimensions must match box-like features.
    bbox = measurements.get("bbox") or {}
    meas_xyz = [bbox.get("x"), bbox.get("y"), bbox.get("z")]
    if all(v is not None for v in meas_xyz):
        for box in _spec_box_dims(spec):
            spec_xyz = [box["length"], box["width"], box["height"]]
            if not _match_box_dims(spec_xyz, meas_xyz, tol_mm, tol_rel):
                issues.append({
                    "code": "B_REP_DIM_MISMATCH",
                    "severity": "blocking",
                    "feature_id": box["feature_id"],
                    "feature_type": box["feature_type"],
                    "expected_mm": spec_xyz,
                    "measured_mm": meas_xyz,
                    "detail": (f"{box['feature_id']} expects box dims "
                               f"{spec_xyz} mm but the B-rep measures "
                               f"{meas_xyz} mm."),
                })

    # L2b: every confirmed radial value must have a matching measured radius.
    measured_radii = measurements.get("cylinder_radii", []) or []
    for rad in _spec_radii(spec):
        if rad["source"] == AGENT_ASSUMED:
            continue  # unconfirmed defaults are L1/V3's concern, not L2
        match = next((m for m in measured_radii
                      if _close(rad["radius"], m, tol_mm, tol_rel)), None)
        if match is None:
            issues.append({
                "code": "B_REP_RADIUS_MISSING",
                "severity": "blocking",
                "feature_id": rad["feature_id"],
                "feature_type": rad["feature_type"],
                "expected_mm": rad["radius"],
                "measured_radii": measured_radii,
                "detail": (f"{rad['feature_id']} expects a radius of "
                           f"{rad['radius']} mm but no cylindrical face of that "
                           f"radius was measured (got {measured_radii})."),
            })

    blocking = [i for i in issues if i["severity"] == "blocking"]
    return {**base, "passed": not blocking, "issues": issues}


def _match_box_dims(spec_xyz: List[float], meas_xyz: List[float],
                    tol_mm: float, tol_rel: float) -> bool:
    """Permutation-invariant match of three box dims (axes may be permuted)."""
    if len(spec_xyz) != 3 or len(meas_xyz) != 3:
        return False
    for perm in permutations(spec_xyz):
        if all(_close(s, m, tol_mm, tol_rel) for s, m in zip(perm, meas_xyz)):
            return True
    return False
