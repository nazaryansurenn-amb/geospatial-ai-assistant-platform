"""Prepare an offline, owner-only 25-parcel EO review from the pinned baseline.

No network calls, public assets, training labels, or classifier modifications.
RGB uses the downloaded native 10 m bands, their recorded scale/offset and a
fixed display stretch. Display enlargement never adds spatial information.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import html
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
import rasterio
from rasterio.windows import Window, from_bounds

from release_tools import local_path, sha256
from run_observation_analysis import write_json, write_table
from wp_core.data_collector.storage import json_hash
from wp_core.observation_rules import CORE, FRACTIONS, INDICES, Policy, season_signals

ROOT = Path(__file__).resolve().parent
BASELINE = "data/analysis/observation_rules/observation_rules_20260906_v1"
COLORS = {"ndvi": "#168148", "evi2": "#956db5", "ndmi": "#197cb6", "bsi": "#cb623d"}
TYPE_NAMES = {0: "coverage/support limit", 1: "annual candidate", 2: "perennial candidate",
              3: "low crop signal", 4: "ambiguous"}


def choose25(sample):
    chosen = []
    for group, count, start_stage in (("boundary", 10, "stage_1"), ("single", 5, "stage_1"),
                                       ("double", 5, "stage_2"), ("perennial", 5, "stage_1")):
        data = sample.loc[sample.review_group.eq(group)].copy()
        data["selection_order"] = data.internal_parcel_id.map(lambda v: hashlib.sha256(v.encode()).hexdigest())
        if group == "boundary":
            # Include a true support limit, low signal and changing/stable profiles.
            data["reason_stratum"] = np.select([data.assessable_years.eq(0), data.annual_years.gt(0) & data.perennial_years.gt(0)],
                                                ["support_limit", "changing_type"], default=data.history_signal)
        else:
            data["reason_stratum"] = data.area_group
        data = data.sort_values("selection_order")
        data["round"] = data.groupby(["activity_stage", "reason_stratum"]).cumcount()
        by_stage = {stage: data.loc[data.activity_stage.eq(stage)].sort_values(["round", "reason_stratum", "selection_order"])
                    for stage in (start_stage, "stage_2" if start_stage == "stage_1" else "stage_1")}
        local = []
        for n in range(count):
            for stage, rows in by_stage.items():
                if n < len(rows) and len(local) < count:
                    local.append(rows.iloc[n])
        if len(local) != count:
            raise ValueError("Review group shortage; no substituted labels allowed")
        chosen.extend(local)
    result = pd.DataFrame(chosen).drop(columns=["selection_order", "reason_stratum", "round"]).reset_index(drop=True)
    if len(result) != 25 or not result.internal_parcel_id.is_unique or result.accepted.any() or result.training_eligible.any():
        raise ValueError("Invalid review selection")
    return result


def load_observations(root, manifest, selected):
    ids = selected.internal_parcel_id.tolist()
    columns = ["internal_parcel_id", "cadastre_code", "observation_date", "scene_id", "geometry_version", "data_version",
               "ndvi_count", "ndvi_p10", "ndvi_p90", *FRACTIONS,
               *[f"{name}_{stat}" for name in INDICES for stat in ("mean", "valid_fraction")]]
    geometry_hash = manifest["input_pins"]["data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"]
    blocks = []
    for number, job in enumerate(manifest["eo_scenes"]):
        path = local_path(root, job["output"])
        if sha256(path) != job["sha256"]:
            raise ValueError("Input EO changed")
        data = pd.read_parquet(path, columns=columns, filters=[("internal_parcel_id", "in", ids)])
        if len(data) != len(ids) or set(data.internal_parcel_id) != set(ids) or not data.internal_parcel_id.is_unique:
            raise ValueError("Missing/duplicate review parcel")
        if not data.geometry_version.eq(geometry_hash).all() or not data.observation_date.eq(job["date"]).all():
            raise ValueError("Input date/geometry mismatch")
        if not data.data_version.eq(manifest["config"]["input_version"]).all() or not data.scene_id.eq(job["scene_id"]).all():
            raise ValueError("Input data version mismatch")
        blocks.append(data)
        if number % 150 == 149:
            print(f"Read {number+1}/{len(manifest['eo_scenes'])} scenes for 25 parcels", flush=True)
    raw = pd.concat(blocks, ignore_index=True)
    raw["core_support"] = raw[[name + "_valid_fraction" for name in CORE]].min(axis=1)
    finite = np.isfinite(raw[[k + "_mean" for k in CORE]].to_numpy()).all(axis=1)
    raw["selection_score"] = np.where(finite, raw.core_support, 0)
    daily = raw.sort_values(["internal_parcel_id", "observation_date", "selection_score", "scene_id"],
                             ascending=[True, True, False, True]).drop_duplicates(["internal_parcel_id", "observation_date"]).copy()
    daily["year"] = pd.to_datetime(daily.observation_date).dt.year
    daily["day"] = pd.to_datetime(daily.observation_date).dt.dayofyear
    return raw, daily


def diagnostic_tables(daily, selected, baseline, policy):
    ids = selected.internal_parcel_id.tolist()
    rows, sensitivity = [], []
    variants = {"green_ndvi_040": {"green_ndvi": .40}, "green_evi2_025": {"green_evi2": .25},
                "green_fraction_030": {"green_fraction": .30}, "bare_fraction_025": {"reset_bare_fraction": .25},
                "reset_ndvi_035": {"reset_ndvi": .35}, "growth_duration_015": {"minimum_growth_days": 15},
                "moisture_drop_000": {"minimum_ndmi_drop": .0}, "pixel_count_001": {"minimum_pixels_10m": 1}}
    for year in range(2021, 2026):
        data = daily.loc[daily.year.eq(year)]
        days = np.sort(data.day.unique())
        def matrix(col):
            return data.pivot(index="internal_parcel_id", columns="day", values=col).reindex(index=ids, columns=days).to_numpy(dtype="float32")
        values = {k: matrix(k + "_mean" if k in INDICES else k) for k in (*INDICES, *FRACTIONS)}
        supports = {k: matrix(k + "_valid_fraction") for k in INDICES}
        pixels = matrix("ndvi_count")
        signals, seasonal = season_signals(days, values, supports, pixels, year, policy)
        original = pd.read_parquet(baseline / "seasons" / f"{year}.parquet").set_index("internal_parcel_id").loc[ids]
        for key in ("type_code", "cycle_code", "usable_dates", "complete_cycles", "quality_reason", "type_reason"):
            if not np.array_equal(original[key].to_numpy(), signals[key]):
                raise ValueError("Review replay differs from the pinned classifier: " + key)
        frame = pd.DataFrame({"internal_parcel_id": ids, "year": year, **signals})
        frame["max_native_pixels"] = pixels.max(axis=1).astype(int)
        frame["ndvi_high_dates"] = (seasonal & (values["ndvi"] >= policy.green_ndvi)).sum(axis=1)
        frame["ndvi_low_dates"] = (seasonal & (values["ndvi"] <= policy.reset_ndvi)).sum(axis=1)
        frame["bare_fraction_gate_dates"] = (seasonal & (values["bare_fraction"] >= policy.reset_bare_fraction)).sum(axis=1)
        frame["positive_bsi_dates"] = (seasonal & (values["bsi"] > 0)).sum(axis=1)
        rows.append(frame)
        for name, changes in variants.items():
            probe, _ = season_signals(days, values, supports, pixels, year, replace(policy, **changes))
            for i, identifier in enumerate(ids):
                if (probe["type_code"][i], probe["cycle_code"][i]) != (signals["type_code"][i], signals["cycle_code"][i]):
                    sensitivity.append({"internal_parcel_id": identifier, "year": year, "probe": name,
                                        "baseline_type": int(signals["type_code"][i]), "probe_type": int(probe["type_code"][i]),
                                        "baseline_cycle": int(signals["cycle_code"][i]), "probe_cycle": int(probe["cycle_code"][i]),
                                        "interpretation": "sensitivity_only_not_a_correction"})
    return pd.concat(rows, ignore_index=True), pd.DataFrame(sensitivity)


def choose_dates(frame):
    selected = []
    # Local parcel support, not whole-scene cloud percentage, determines availability.
    for lo, hi, target in ((60, 105, 90), (106, 150, 135), (151, 195, 180),
                            (196, 240, 220), (241, 285, 265), (286, 335, 310)):
        candidates = frame.loc[frame.day.between(lo, hi) & frame.ndvi_valid_fraction.ge(.80)
                               & frame.ndvi_mean.notna()].copy()
        if candidates.empty:
            selected.append(None)
            continue
        candidates["distance"] = (candidates.day - target).abs()
        selected.append(candidates.sort_values(["distance", "ndvi_valid_fraction", "scene_id"],
                                               ascending=[True, False, True]).iloc[0])
    return selected


def crop_rgb(root, scene, bounds_key, geometry, checked):
    cache = local_path(root, f"data/cache/copernicus/{bounds_key}/{scene['collection']}/{scene['id']}")
    arrays, valid, transform, native_shape = [], None, None, None
    used = []
    for color in ("red", "green", "blue"):
        path = cache / f"{color}.tif"
        side = path.with_suffix(".json")
        if not path.exists() or not side.exists():
            raise FileNotFoundError("Required cached RGB unavailable; no automatic download")
        if str(path) not in checked:
            record = json.loads(side.read_text())
            if sha256(path) != record["sha256"]:
                raise ValueError("Cached RGB checksum mismatch")
            checked[str(path)] = record["sha256"]
        used.append({"path": path.relative_to(root).as_posix(), "sha256": checked[str(path)]})
        with rasterio.open(path) as src:
            if src.crs.to_epsg() != 32638 or not np.allclose(src.res, (10, 10)):
                raise ValueError("Unexpected RGB CRS/resolution")
            xmin, ymin, xmax, ymax = geometry.bounds
            cx, cy = (xmin+xmax)/2, (ymin+ymax)/2
            span = max(xmax-xmin, ymax-ymin, 90) * 1.35
            win = from_bounds(cx-span/2, cy-span/2, cx+span/2, cy+span/2, src.transform)
            col, row = int(np.floor(win.col_off)), int(np.floor(win.row_off))
            win = Window(col, row, int(np.ceil(win.col_off+win.width))-col, int(np.ceil(win.row_off+win.height))-row)
            raw = src.read(1, window=win, boundless=True, masked=True)
            current_transform = src.window_transform(win)
            if transform is not None and (current_transform != transform or raw.shape != native_shape):
                raise ValueError("RGB bands are not co-registered")
            transform, native_shape = current_transform, raw.shape
            asset = scene["assets"][color]
            tags = src.tags()
            if not np.isclose(float(tags["scale"]), asset["scale"]) or not np.isclose(float(tags["offset"]), asset["offset"]):
                raise ValueError("RGB radiometry metadata differs")
            band = raw.astype("float32").filled(np.nan) * asset["scale"] + asset["offset"]
            mask = np.isfinite(band)
            valid = mask if valid is None else valid & mask
            arrays.append(band)
    rgb = np.stack(arrays, axis=-1)
    rgb = np.power(np.clip(np.nan_to_num(rgb) / .30, 0, 1), .82)
    rgb = (rgb * 255).astype("uint8")
    rgb[~valid] = [195, 195, 195]
    return rgb, transform, used


def draw_graph(draw, frame, box, small):
    x, y, width, height = box
    top, bottom = -.4, 1.0
    def point(day, value):
        return x + day/366*width, y + (bottom-value)/(bottom-top)*height
    for value in (0, .5, 1):
        yy = point(1, value)[1]
        draw.line((x, yy, x+width, yy), fill="#dce2e2")
        draw.text((x+3, yy+1), str(value), font=small, fill="#687474")
    for day, month in ((1, "Jan"), (91, "Apr"), (182, "Jul"), (274, "Oct")):
        draw.text((x+day/366*width, y+height+3), month, font=small, fill="#687474")
    for name, color in COLORS.items():
        previous = None
        for obs in frame.sort_values("day").itertuples():
            value, support = getattr(obs, name+"_mean"), getattr(obs, name+"_valid_fraction")
            if not np.isfinite(value) or support < .6 or not top <= value <= bottom:
                previous = None
                continue
            xy = point(obs.day, value)
            if previous is not None and obs.day - previous[0] <= 35:
                draw.line((*previous[1], *xy), fill=color, width=2)
            draw.ellipse((xy[0]-1, xy[1]-1, xy[0]+1, xy[1]+1), fill=color)
            previous = (obs.day, xy)


def render_sheet(root, output, row, geometry, daily, diagnostics, scenes, bounds_key, checked):
    canvas = Image.new("RGB", (1440, 1630), "white")
    draw = ImageDraw.Draw(canvas)
    font, small = ImageFont.load_default(size=18), ImageFont.load_default(size=13)
    draw.text((20, 15), f"{row.cadastre_code} | {row.review_group} | {row.activity_stage} | {row.area_official_m2/10000:.4f} ha", fill="#18352d", font=font)
    draw.text((20, 44), "UNVERIFIED | Copernicus native RGB 10 m | white: unchanged cadastral outline | fixed display stretch", fill="#53615d", font=small)
    rect = list(geometry.minimum_rotated_rectangle.exterior.coords)
    sides = [np.hypot(rect[i+1][0]-rect[i][0], rect[i+1][1]-rect[i][1]) for i in range(4)]
    width = min(sides)
    draw.text((20, 65), f"Minimum rectangle width: {width:.1f} m | geometry area / 10 m pixel: {geometry.area/100:.2f} | enlargement is not extra detail", fill="#53615d", font=small)
    for i, (name, color) in enumerate(COLORS.items()):
        draw.text((1015+(i%2)*170, 88+(i//2)*19), name.upper(), fill=color, font=small)
    records = []
    identifier = row.internal_parcel_id
    for year in range(2021, 2026):
        y = 130 + (year-2021)*295
        frame = daily.loc[daily.internal_parcel_id.eq(identifier) & daily.year.eq(year)]
        d = diagnostics.loc[diagnostics.internal_parcel_id.eq(identifier) & diagnostics.year.eq(year)].iloc[0]
        draw.text((20, y), f"{year} | {TYPE_NAMES[int(d.type_code)]} | cycle {int(d.cycle_code) or '-'} | usable dates {d.usable_dates} | gap {d.maximum_gap_days} d", fill="#203e33", font=font)
        for i, observed in enumerate(choose_dates(frame)):
            x, image_y = 20+i*162, y+47
            if observed is None:
                draw.rectangle((x, image_y, x+150, image_y+150), fill="#ededed")
                draw.text((x+7, image_y+60), "No suitable date", fill="#53615d", font=small)
                records.append({"parcel": identifier, "year": year, "window": i+1, "status": "no_suitable_date"})
                continue
            scene = scenes[observed.scene_id]
            rgb, transform, used = crop_rgb(root, scene, bounds_key, geometry, checked)
            chip = output / "chips" / f"{row.cadastre_code}_{observed.observation_date}.png"
            chip.parent.mkdir(exist_ok=True)
            Image.fromarray(rgb).save(chip)
            height, width_px = rgb.shape[:2]
            view = Image.fromarray(rgb).resize((150, 150), Image.Resampling.NEAREST)
            canvas.paste(view, (x, image_y))
            def pt(point):
                col, rr = (~transform) * point
                return x+col/width_px*150, image_y+rr/height*150
            for polygon in geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]:
                for ring in (polygon.exterior, *polygon.interiors):
                    coords = [pt(c) for c in ring.coords]
                    draw.line(coords, fill="#102a22", width=3)
                    draw.line(coords, fill="white", width=1)
            draw.text((x, y+28), observed.observation_date, fill="#203e33", font=small)
            draw.text((x, image_y+157), f"valid area {observed.ndvi_valid_fraction:.0%}", fill="#53615d", font=small)
            records.append({"parcel": identifier, "year": year, "window": i+1, "date": observed.observation_date,
                            "scene_id": observed.scene_id, "native_shape": [height, width_px],
                            "chip": chip.relative_to(output).as_posix(), "rgb_inputs": used,
                            "status": "available", "pixel_std": round(float(rgb.std()), 3)})
        draw_graph(draw, frame, (1015, y+48, 405, 150), small)
        draw.text((20, y+228), f"Reason: {d.type_reason}; green {d.green_dates}, soil resets {d.soil_reset_dates}, complete cycles {d.complete_cycles}, max pixels {d.max_native_pixels}", fill="#35453d", font=small)
        draw.text((20, y+251), f"Coverage: {d.quality_reason or 'passed'} | regrowth flag {bool(d.possible_regrowth)} | persistent canopy {bool(d.persistent_canopy)}", fill="#53615d", font=small)
    path = output / "sheets" / f"{row.cadastre_code}.png"
    path.parent.mkdir(exist_ok=True)
    canvas.save(path)
    return records, {"internal_parcel_id": identifier, "cadastre_code": row.cadastre_code,
                     "minimum_rectangle_width_m": float(min(sides)), "geometry_pixel_equivalents_10m": float(geometry.area/100),
                     "sheet": path.relative_to(output).as_posix(), "review_status": "prepared_not_visually_verified"}


def run(root, slug):
    if not slug.replace("_", "").isalnum():
        raise ValueError("Invalid review version")
    baseline = local_path(root, BASELINE)
    output = local_path(root, f"server_data/review/observation_rules_20260906_v1/{slug}")
    if (output / "prepared.json").exists():
        raise FileExistsError("Review version exists; do not overwrite prior evidence")
    manifest = json.loads((baseline / "manifest.json").read_text())
    for relative, digest in manifest["code_sha256"].items():
        if sha256(local_path(root, relative)) != digest:
            raise ValueError("Baseline implementation changed")
    progress = json.loads((baseline / "progress.json").read_text())
    for relative, digest in progress["files"].items():
        if sha256(local_path(baseline, relative)) != digest:
            raise ValueError("Baseline output changed")
    source = local_path(root, "data/observations/" + manifest["config"]["input_version"])
    source_spec = json.loads((source / "specification.json").read_text())
    for relative, digest in manifest["input_pins"].items():
        if sha256(local_path(root, relative)) != digest:
            raise ValueError("Pinned input changed")
    selected = choose25(pd.read_parquet(baseline / "review_sample.parquet"))
    geometry = gpd.read_parquet(local_path(root, source_spec["config"]["basis"])).set_index("internal_parcel_id").loc[selected.internal_parcel_id].to_crs(32638)
    raw, daily = load_observations(root, manifest, selected)
    diagnostics, sensitivity = diagnostic_tables(daily, selected, baseline, Policy(**manifest["policy"]))
    scenes = {s["id"]: s for s in json.loads((source / "scene_manifest.json").read_text())["scenes"]}
    bounds_key = json_hash(source_spec["scope"]["bounds_wgs84"])[:16]
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("selection", selected), ("raw_sample", raw), ("daily_sample", daily),
                       ("diagnostics", diagnostics), ("sensitivity", sensitivity)):
        write_table(output / f"{name}.parquet", data)
    selected.to_csv(output / "selection.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(output / "diagnostics.csv", index=False, encoding="utf-8-sig")
    sensitivity.to_csv(output / "sensitivity.csv", index=False, encoding="utf-8-sig")
    checked, evidence, sheets = {}, [], []
    for i, row in enumerate(selected.itertuples()):
        frames, sheet = render_sheet(root, output, row, geometry.loc[row.internal_parcel_id].geometry,
                                     daily, diagnostics, scenes, bounds_key, checked)
        evidence.extend(frames)
        sheets.append(sheet)
        print(f"Prepared {i+1}/25: {row.cadastre_code} ({row.review_group})", flush=True)
    write_json(output / "evidence.json", evidence)
    cards = []
    for sheet, row in zip(sheets, selected.itertuples()):
        cards.append(f'<tr><td>{html.escape(row.review_group)}</td><td><a href="{sheet["sheet"]}">{row.cadastre_code}</a></td>'
                     f'<td>{row.activity_stage}</td><td>{row.area_official_m2/10000:.4f}</td><td>{sheet["minimum_rectangle_width_m"]:.1f}</td></tr>')
    document = ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>EO: 25 control parcels</title><style>body{font:16px system-ui;max-width:1050px;margin:24px auto;padding:0 16px;color:#20382e}'
                'table{width:100%;border-collapse:collapse}th,td{padding:9px;text-align:left;border-bottom:1px solid #ddd}a{color:#126697}h1{font-size:24px}</style>'
                '<h1>EO: 25 control parcels, 2021-2025</h1><p>Internal draft. Native 10 m RGB, dated observations. No accepted or training labels.</p>'
                '<table><thead><tr><th>Group</th><th>Cadastre</th><th>Stage</th><th>ha</th><th>Width, m</th></tr></thead><tbody>'
                + ''.join(cards) + '</tbody></table></html>')
    (output / "index.html").write_text(document, encoding="utf-8")
    report = {"review_version": slug, "baseline": BASELINE, "baseline_manifest_sha256": sha256(baseline / "manifest.json"),
              "script_sha256": sha256(Path(__file__)), "selection": selected.groupby(["review_group", "activity_stage"]).size().reset_index(name="count").to_dict(orient="records"),
              "parcel_count": len(selected), "parcel_years": len(diagnostics), "raw_rows": len(raw), "distinct_parcel_dates": len(daily),
              "sheets": sheets, "available_rgb": sum(e["status"] == "available" for e in evidence),
              "missing_rgb_windows": sum(e["status"] != "available" for e in evidence), "replay_matches_baseline": True,
              "sensitivity_is_validation": False, "verified_training_labels": 0, "classification_modified": False,
              "map_modified": False, "external_downloads": 0}
    write_json(output / "prepared.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("sheets", "selection")}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="control25_v1")
    args = parser.parse_args()
    run(ROOT, args.version)
