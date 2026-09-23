from __future__ import annotations

import csv
import html
import json
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.windows import Window, from_bounds

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PRODUCT_ROOT / "data" / "analysis" / "parcel_eo"
SERIES_ROOT = DATA_ROOT / "v5_full_halo_2021_2025"
PARCELS_PATH = DATA_ROOT / "current_halo_v1" / "current_parcels.parquet"
SAMPLE_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_validation_sample.json"
)
SCENE_MANIFEST_PATH = SERIES_ROOT / "lower_hrazdan_parcel_eo_scene_manifest_2025.json"
OUTPUT_ROOT = PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_visual_review"
ASSET_ROOT = OUTPUT_ROOT / "assets"

TARGET_DATES = (date(2025, 5, 15), date(2025, 7, 15), date(2025, 9, 10))
GROUP_LABELS = {
    "annual_single_cycle": "Annual: single cycle",
    "annual_two_or_variable": "Annual: recurring two cycles or variable",
    "perennial": "Perennial profile",
    "mixed_profile": "Mixed seasonal profile",
}


def select_scenes() -> list[dict[str, object]]:
    payload = json.loads(SCENE_MANIFEST_PATH.read_text(encoding="utf-8"))
    candidates = []
    for scene in payload["selected_scenes"]:
        scene_date = datetime.fromisoformat(scene["datetime"].replace("Z", "+00:00")).date()
        if float(scene["cloud_cover_percent"]) > 10:
            continue
        if float(scene["nodata_pixel_percent"]) > 1:
            continue
        candidates.append({**scene, "date": scene_date})
    selected = []
    used = set()
    for target in TARGET_DATES:
        scene = min(
            (item for item in candidates if item["scene_id"] not in used),
            key=lambda item: (
                abs((item["date"] - target).days),
                float(item["cloud_cover_percent"]),
            ),
        )
        selected.append(scene)
        used.add(scene["scene_id"])
    return selected


def read_rgb_scene(
    scene: dict[str, object], sample_parcels: gpd.GeoDataFrame
) -> dict[str, object]:
    assets = scene["assets"]
    with rasterio.open(assets["red"]["href"]) as reference:
        projected = sample_parcels.to_crs(reference.crs)
        minx, miny, maxx, maxy = projected.total_bounds
        source_window = from_bounds(minx - 80, miny - 80, maxx + 80, maxy + 80, reference.transform)
        source_window = source_window.round_offsets().round_lengths()
        source_window = source_window.intersection(
            Window(0, 0, reference.width, reference.height)
        )
        transform = reference.window_transform(source_window)

    bands = []
    for color in ("red", "green", "blue"):
        with rasterio.open(assets[color]["href"]) as source:
            bands.append(source.read(1, window=source_window))
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    rgb = np.clip(rgb / 3000.0, 0.0, 1.0)
    rgb = np.power(rgb, 0.82)
    return {
        "date": scene["date"],
        "scene_id": scene["scene_id"],
        "cloud_cover_percent": float(scene["cloud_cover_percent"]),
        "crs": projected.crs,
        "transform": transform,
        "rgb": (rgb * 255).astype(np.uint8),
    }


def crop_rgb(scene: dict[str, object], geometry) -> tuple[np.ndarray, list[float]]:
    minx, miny, maxx, maxy = geometry.bounds
    span = max(maxx - minx, maxy - miny, 80.0)
    padding = max(30.0, span * 0.25)
    window = from_bounds(
        minx - padding,
        miny - padding,
        maxx + padding,
        maxy + padding,
        scene["transform"],
    ).round_offsets().round_lengths()
    rgb = scene["rgb"]
    row_start = max(0, int(window.row_off))
    col_start = max(0, int(window.col_off))
    row_stop = min(rgb.shape[0], row_start + max(1, int(window.height)))
    col_stop = min(rgb.shape[1], col_start + max(1, int(window.width)))
    crop = rgb[row_start:row_stop, col_start:col_stop]
    x0, y1 = scene["transform"] * (col_start, row_start)
    x1, y0 = scene["transform"] * (col_stop, row_stop)
    return crop, [x0, x1, y0, y1]


def save_review_figure(
    sample: dict[str, object], geometry_by_crs: dict[str, object], scenes: list[dict[str, object]]
) -> str:
    width, height = 1100, 620
    canvas = Image.new("RGB", (width, height), "#f8faf9")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    bold_font = font
    draw.text(
        (24, 16),
        f"{sample['cadastre_code']} | {GROUP_LABELS[sample['review_group']]}",
        fill="#17251f",
        font=bold_font,
    )
    draw.text(
        (24, 36),
        f"{sample['stage']} | {sample['area_ha']:.4f} ha",
        fill="#516159",
        font=font,
    )

    panel_width = 344
    panel_height = 250
    panel_top = 66
    for index, scene in enumerate(scenes):
        geometry = geometry_by_crs[str(scene["crs"])]
        crop, extent = crop_rgb(scene, geometry)
        left = 24 + index * 356
        image_top = panel_top + 34
        rgb_image = Image.fromarray(crop).resize(
            (panel_width, panel_height - 34), Image.Resampling.BILINEAR
        )
        canvas.paste(rgb_image, (left, image_top))
        draw.rectangle(
            (left, image_top, left + panel_width, panel_top + panel_height),
            outline="#7d8a84",
            width=1,
        )
        draw.text(
            (left, panel_top),
            f"Copernicus RGB {scene['date'].isoformat()}",
            fill="#17251f",
            font=font,
        )
        draw.text(
            (left, panel_top + 16),
            f"scene cloud {scene['cloud_cover_percent']:.1f}%",
            fill="#66736d",
            font=font,
        )
        x0, x1, y0, y1 = extent

        def to_pixel(x_value: float, y_value: float) -> tuple[float, float]:
            x_pixel = left + ((x_value - x0) / max(x1 - x0, 1)) * panel_width
            y_pixel = image_top + ((y1 - y_value) / max(y1 - y0, 1)) * (panel_height - 34)
            return x_pixel, y_pixel

        polygons = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
        for polygon in polygons:
            draw.line(
                [to_pixel(x_value, y_value) for x_value, y_value in polygon.exterior.coords],
                fill="#ff3b70",
                width=3,
                joint="curve",
            )

    chart_left, chart_top = 72, 360
    chart_width, chart_height = 980, 205
    draw.rectangle(
        (chart_left, chart_top, chart_left + chart_width, chart_top + chart_height),
        fill="#ffffff",
        outline="#8f9c96",
    )
    for day in (100, 150, 200, 250):
        x_value = chart_left + (day - 85) / 200 * chart_width
        draw.line((x_value, chart_top, x_value, chart_top + chart_height), fill="#e0e6e3")
        draw.text((x_value - 9, chart_top + chart_height + 7), str(day), fill="#64736c", font=font)
    for ndvi_value in (0.0, 0.3, 0.6, 0.9):
        y_value = chart_top + (0.95 - ndvi_value) / 1.15 * chart_height
        draw.line((chart_left, y_value, chart_left + chart_width, y_value), fill="#e0e6e3")
        draw.text((32, y_value - 5), f"{ndvi_value:.1f}", fill="#64736c", font=font)

    year_colors = {
        2021: "#157f5b",
        2022: "#e39b24",
        2023: "#3f7fb5",
        2024: "#b14f7f",
        2025: "#5b5f66",
    }
    series = sample.get("series", [])
    for year in range(2021, 2026):
        points = [
            item
            for item in series
            if item["date"].startswith(str(year)) and item["ndvi"] is not None
        ]
        chart_points = []
        for item in points:
            day = datetime.fromisoformat(item["date"]).timetuple().tm_yday
            x_value = chart_left + (day - 85) / 200 * chart_width
            y_value = chart_top + (0.95 - float(item["ndvi"])) / 1.15 * chart_height
            chart_points.append((x_value, y_value))
        if len(chart_points) > 1:
            draw.line(chart_points, fill=year_colors[year], width=2, joint="curve")
        for x_value, y_value in chart_points:
            draw.ellipse(
                (x_value - 2, y_value - 2, x_value + 2, y_value + 2),
                fill=year_colors[year],
            )
        legend_x = 700 + (year - 2021) * 74
        draw.line((legend_x, 340, legend_x + 20, 340), fill=year_colors[year], width=3)
        draw.text((legend_x + 24, 334), str(year), fill="#39473f", font=font)
    draw.text((chart_left, 334), "Parcel mean NDVI by completed season", fill="#17251f", font=font)
    draw.text((500, 590), "Day of year", fill="#64736c", font=font)
    draw.text((8, 455), "NDVI", fill="#64736c", font=font)

    filename = f"{sample['review_group']}__{sample['cadastre_code']}.webp"
    output_path = ASSET_ROOT / filename
    canvas.save(output_path, format="WEBP", quality=82, method=4)
    return f"assets/{filename}"


def build() -> dict[str, object]:
    sample_payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    samples = sample_payload["records"]
    sample_codes = {item["cadastre_code"] for item in samples}
    parcels = gpd.read_parquet(PARCELS_PATH)
    code_column = "cadastre_code" if "cadastre_code" in parcels else "cadastral_code"
    parcels[code_column] = parcels[code_column].astype(str)
    parcels = parcels[parcels[code_column].isin(sample_codes)].copy()
    if len(parcels) != 120:
        raise ValueError(f"Expected 120 review parcels, found {len(parcels)}")
    parcel_lookup = parcels.set_index(code_column).geometry.to_dict()

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    selected_scene_records = select_scenes()
    scenes = [read_rgb_scene(scene, parcels) for scene in selected_scene_records]

    transformed_by_crs = {
        str(scene["crs"]): {
            code: gpd.GeoSeries([geometry], crs=parcels.crs)
            .to_crs(scene["crs"])
            .iloc[0]
            for code, geometry in parcel_lookup.items()
        }
        for scene in scenes
    }
    review_records = []
    for sample in samples:
        geometry_by_crs = {
            crs: transformed[sample["cadastre_code"]]
            for crs, transformed in transformed_by_crs.items()
        }
        image_path = save_review_figure(sample, geometry_by_crs, scenes)
        review_records.append({**sample, "review_image": image_path})

    with (OUTPUT_ROOT / "review_sample.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("review_group", "cadastre_code", "stage", "area_ha", "crop_type", "annual_cycle"),
        )
        writer.writeheader()
        writer.writerows(
            {key: record.get(key) for key in writer.fieldnames} for record in review_records
        )

    sections = []
    for group_name in GROUP_LABELS:
        cards = []
        for record in (item for item in review_records if item["review_group"] == group_name):
            cards.append(
                "<article><h3>"
                + html.escape(record["cadastre_code"])
                + "</h3><p>"
                + html.escape(f"{record['stage']} | {record['area_ha']:.4f} ha")
                + "</p><img loading='lazy' src='"
                + html.escape(record["review_image"])
                + "' alt='Copernicus RGB and seasonal NDVI review'></article>"
            )
        sections.append(
            f"<section><h2>{html.escape(GROUP_LABELS[group_name])} ({len(cards)})</h2>"
            + "".join(cards)
            + "</section>"
        )
    selected_dates = ", ".join(scene["date"].isoformat() for scene in scenes)
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Land use type v2 visual review</title><style>
body{{font-family:Arial,sans-serif;margin:24px;background:#f3f6f4;color:#17251f}}
header{{max-width:1100px;margin:auto}}section{{max-width:1100px;margin:32px auto}}
article{{margin:16px 0;padding:12px;background:#fff;border:1px solid #cfd8d3;border-radius:6px}}
h2{{border-bottom:2px solid #276a52;padding-bottom:8px}}h3,p{{margin:4px 0 8px}}
img{{display:block;width:100%;height:auto}}
</style></head><body><header><h1>Land use type v2 owner review</h1>
<p>120 cadastral parcels: 30 in each review group. RGB dates: {html.escape(selected_dates)}.</p>
<p>Draft evidence only. No crop species are inferred. The 2026 season is excluded.</p></header>
{''.join(sections)}</body></html>"""
    (OUTPUT_ROOT / "index.html").write_text(document, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "status": "owner_review_only",
        "sample_count": len(review_records),
        "group_counts": sample_payload["group_counts"],
        "rgb_scenes": [
            {
                "date": scene["date"].isoformat(),
                "scene_id": scene["scene_id"],
                "cloud_cover_percent": scene["cloud_cover_percent"],
            }
            for scene in scenes
        ],
        "includes_raw_eo_series": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
    }
    (OUTPUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
