import numpy as np
from affine import Affine
from shapely.geometry import box
from wp_core.land_potential import history_pass, samples, terrain_class


def test_elevation_direction_and_overlap():
    assert terrain_class(110, 120, 90, 100)[0] == "mechanical_candidate"
    assert terrain_class(70, 80, 90, 100)[0] == "gravity_candidate"
    assert terrain_class(90, 110, 95, 100)[0] == "review"
    assert terrain_class(100, 110, 90, 100)[0] == "review"
    assert terrain_class(np.nan, 110, 90, 100)[0] == "review"


def test_recent_activity_and_missing_never_become_unused():
    assert history_pass([3, 3, 1, 3, 3])
    assert not history_pass([3, 3, 3, 3, 0])
    assert not history_pass([3, 3, 3, 2, 3])
    assert not history_pass([0, 0, 0, 3, 3])
    assert not history_pass([1, 1, 3, 3, 3])


def test_exact_grid_intersections_and_outside_coverage():
    t = Affine(10, 0, 0, 0, -10, 20)
    r, c, w = samples(box(9, 9, 11, 11), t, (2, 2))
    assert len(w) == 4 and np.isclose(w.sum(), 4)
    r, c, w = samples(box(-10, 0, 10, 10), t, (2, 2))
    assert np.isclose(w.sum(), 100)
    assert not len(samples(box(40, 40, 50, 50), t, (2, 2))[2])
