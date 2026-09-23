"""Read-only diagnosis of the frozen multi-year rainfall exclusions.

Writes a separate private review record, never modifies inputs or analysis rules.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/analysis/rapid_water_loss/sentinel1_peer_2021_2025_20260906_v1"
REVIEW = ROOT / "server_data/review/sentinel1_peer_2021_2025_20260906_v1"
KEY = "637b52d6ffa54404ec29"


def main():
    report = json.loads((OUT / "snapshots" / KEY / "report.json").read_text())
    cfg = json.loads((ROOT / "config/sentinel1_peer_comparison_20260906_v1.json").read_text())
    points = pd.read_parquet(ROOT / "data/analysis/rapid_water_loss/sentinel1_peer_comparison_20260906_v1/fields.parquet").set_index("internal_parcel_id")
    years, records, parcel_ids = [], [], set()
    for annual in report["years"]:
        folder = OUT / "years" / annual["key"]
        marker = json.loads((folder / "complete.json").read_text())
        weather = pd.concat([pd.read_parquet(ROOT / x["path"], columns=["valid_time", "latitude", "longitude", "tp"]) for x in marker["weather_sources"]], ignore_index=True)
        weather["valid_time"] = pd.to_datetime(weather.valid_time, utc=True)
        by_cell = {}
        for cell, data in weather.groupby(["latitude", "longitude"]):
            data = data.sort_values("valid_time").set_index("valid_time")
            assert data.index.is_unique
            # Retain signed values to identify the exact rejection reason.
            current = data.tp.astype("float64")
            previous = current.shift(1).where(data.index.to_series().diff().eq(pd.Timedelta(hours=1)))
            previous.loc[data.index.hour == 1] = 0.0
            by_cell[cell] = 1000 * (current - previous)
        raw = pd.read_parquet(folder / "radar_input.parquet").set_index(["internal_parcel_id", "source_item"])
        diag = pd.read_parquet(folder / "comparisons.parquet").drop_duplicates(["internal_parcel_id", "middle_item"])
        matched = diag.loc[diag.supported & diag.peer_count.ge(cfg["minimum_peers"])]
        year_records = []
        for row in matched.itertuples():
            point = points.loc[row.internal_parcel_id]
            series = by_cell[(point.latitude, point.longitude)]
            dates = [pd.Timestamp(raw.loc[(row.internal_parcel_id, item), "datetime"]) for item in (row.first_item, row.middle_item, row.last_item)]
            windows = []
            for name, start, end in (("before", dates[0], dates[1]), ("after", dates[1], dates[2]), ("recent_48h", dates[1]-pd.Timedelta(hours=48), dates[1])):
                index = pd.date_range(start.floor("h")+pd.Timedelta(hours=1), end.ceil("h"), freq="h")
                values = series.reindex(index).to_numpy()
                negatives = values[np.isfinite(values) & (values < 0)]
                missing = int((~np.isfinite(values)).sum())
                windows.append({"window": name, "hours": len(index), "negative_increments": len(negatives), "nonfinite_or_missing_increments": missing,
                                "minimum_signed_increment_mm": float(negatives.min()) if len(negatives) else None})
            record = {"year": annual["year"], "internal_parcel_id": row.internal_parcel_id, "middle_item": row.middle_item, "windows": windows,
                      "rejected_only_due_to_negative_increments": any(x["negative_increments"] for x in windows) and not any(x["nonfinite_or_missing_increments"] for x in windows)}
            assert not row.weather_complete
            year_records.append(record)
            parcel_ids.add(row.internal_parcel_id)
        records.extend(year_records)
        years.append({"year": annual["year"], "matched_triplets": len(matched), "matched_parcels": int(matched.internal_parcel_id.nunique()),
                      "triplets_rejected_only_due_to_negative_increments": sum(x["rejected_only_due_to_negative_increments"] for x in year_records),
                      "windows_with_missing_or_nonfinite_increment": sum(w["nonfinite_or_missing_increments"] > 0 for x in year_records for w in x["windows"]),
                      "minimum_signed_increment_mm_in_matched_windows": min((w["minimum_signed_increment_mm"] for x in year_records for w in x["windows"] if w["minimum_signed_increment_mm"] is not None), default=None)})
    result = {"snapshot_key": KEY, "diagnostic_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "years": years,
              "matched_triplets_total": len(records), "unique_parcels_matched_across_years": len(parcel_ids),
              "all_matched_triplets_rejected_only_for_negative_increments": bool(records) and all(x["rejected_only_due_to_negative_increments"] for x in records),
              "interpretation": "The frozen rule rejects these windows for tiny negative hourly precipitation differences. The arithmetic is verified; the physical/data-encoding cause is not established here. No correction, threshold relaxation or reclassification was performed.",
              "triplets": records}
    dest = REVIEW / ("rainfall_rejection_diagnosis_" + KEY + ".json")
    if dest.exists():
        assert json.loads(dest.read_text()) == result
    else:
        dest.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "triplets"}, indent=2))


if __name__ == "__main__":
    main()
