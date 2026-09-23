"""Map export consistency and public-route regression tests."""
import json
from pathlib import Path
import unittest

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform

from serve_map import DATA, ROUTES


class MapTests(unittest.TestCase):
    def test_safe_allowlist(self):
        for route in ["/output/result_v2.json", "/data/samples", "/.env", "/analysis.py", "/../analysis.py"]:
            self.assertNotIn(route, ROUTES)
        self.assertTrue(all(p.is_file() for p, _ in ROUTES.values()))

    def test_areas_and_geometry(self):
        summary = json.loads((DATA / "summary.json").read_text())
        project = Transformer.from_crs(4326, 32638, always_xy=True).transform
        boundary = transform(project, shape(json.loads((DATA / "boundary.geojson").read_text())["geometry"]))
        geometries = {}
        for layer in summary["layers"]:
            data = json.loads((DATA / (layer["id"] + ".geojson")).read_text())
            self.assertEqual(set(data["features"][0]["properties"]), {"layer"})
            exported = shape(data["features"][0]["geometry"])
            self.assertTrue(exported.is_valid)
            projected = transform(project, exported)
            # Inverse projection can move a touching vertex by sub-micron roundoff.
            # Validate the actual exported geometry, then bound any measurement repair.
            geom = make_valid(projected)
            self.assertLess(abs(geom.area - projected.area), .0001)
            self.assertAlmostEqual(geom.area / 10000, layer["area_ha"], delta=.00051)
            self.assertLess(geom.difference(boundary).area, .05)
            geometries[layer["id"]] = geom
        self.assertLess(geometries["irrigation"].difference(geometries["active"]).area, .05)
        self.assertLess(geometries["active"].difference(geometries["vegetation"]).area, .05)

    def test_summary_is_sanitized(self):
        summary = json.loads((DATA / "summary.json").read_text())
        self.assertEqual(summary["observations"], 13)
        self.assertEqual(summary["end"], "2026-09-02")
        self.assertFalse(summary["confirmed_irrigation"])
        self.assertNotIn("threshold", json.dumps(summary).lower())
        self.assertNotIn("ndvi", json.dumps(summary).lower())
        self.assertEqual(len(summary["layers"]), 5)


if __name__ == "__main__":
    unittest.main()
