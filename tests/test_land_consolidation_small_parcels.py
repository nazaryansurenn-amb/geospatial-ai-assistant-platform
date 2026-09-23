import numpy as np
import pandas as pd

from run_land_consolidation_small_parcels import small_parcels
from wp_core.land_consolidation import Policy, connected_complete_link


def test_size_limit_uses_exact_official_area_and_includes_boundary():
    values = pd.Series([5000., 4999.99, 5000.01, 0., -1., np.nan, np.inf])
    assert small_parcels(values, 5000.).tolist() == [True, True, False, False, False, False, False]


def test_size_gate_precedes_grouping_and_cannot_bridge_through_large_parcel():
    original = pd.DataFrame({"id": ["a", "large", "b"], "area": [5000., 5100., 5000.]})
    chosen = original[small_parcels(original.area, 5000.)]
    remap = {old: new for new, old in enumerate(chosen.index)}
    edges = [(0, 1, 20.), (1, 2, 20.)]
    filtered = [(remap[a], remap[b], length) for a, b, length in edges if a in remap and b in remap]
    groups = connected_complete_link(chosen.id.tolist(), filtered, lambda a, b: 31, Policy())
    assert all(len(members) == 1 for members, mask in groups)


def test_at_least_ten_half_hectare_members_needed_for_five_hectares():
    for count in (9, 10):
        ids = [f"p{i:02d}" for i in range(count)]
        edges = [(i, i + 1, 20.) for i in range(count - 1)]
        groups = connected_complete_link(ids, edges, lambda a, b: 31, Policy())
        area_ha = len(groups[0][0]) * .5
        assert (area_ha >= 5.) == (count == 10)
