"""Public projection of the owner-approved consolidation review, never diagnostics."""
import json

PROPERTIES = {"group_number", "stage", "priority", "parcel_count", "area_ha"}


def project(blocks, members):
    if not blocks.block_id.is_unique or not members.cadastre_code.is_unique:
        raise ValueError("Duplicate group or cadastral identity")
    features, lookup = [], {}
    for number, row in enumerate(blocks.sort_values("block_id").to_crs(4326).itertuples(), 1):
        if row.stage not in {"stage_1", "stage_2"}:
            raise ValueError("Unresolved stage")
        selected = members[members.block_id.eq(row.block_id)]
        if len(selected) != row.parcel_count or (selected.area_official_m2 > 10000).any() or row.area_ha < 3:
            raise ValueError("Unapproved candidate membership")
        props = {"group_number": number, "stage": row.stage, "priority": bool(row.strip_priority),
                 "parcel_count": int(row.parcel_count), "area_ha": float(row.area_ha)}
        features.append({"type": "Feature", "id": number, "properties": props, "geometry": row.geometry.__geo_interface__})
        for code in selected.cadastre_code:
            lookup[code] = dict(props)
    collection = {"type": "FeatureCollection", "features": features}
    summaries = {}
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        rows = [f["properties"] for f in features if scope == "lower_hrazdan" or f["properties"]["stage"] == scope]
        summaries[scope] = {}
        for key, subset in (("all", rows), ("priority", [r for r in rows if r["priority"]]), ("other", [r for r in rows if not r["priority"]])):
            summaries[scope][key] = {"groups": len(subset), "parcels": sum(r["parcel_count"] for r in subset), "area_ha": sum(r["area_ha"] for r in subset)}
    summary = {"period": [2021, 2025], "status": "preliminary_candidates", "summaries": summaries}
    json.dumps((collection, summary, lookup), allow_nan=False)
    return collection, summary, lookup
