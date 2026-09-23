"""Private review sheet of the six largest below-threshold joint EO groups."""
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/analysis/land_consolidation/consolidation_joint_eo_20260906_v1"
OUT = ROOT / "server_data/review/consolidation_joint_eo_20260906_v1/largest_groups.png"


def execute():
    groups = pd.read_parquet(SOURCE / "groups.parquet").sort_values(["area_ha", "block_id"], ascending=[False, True]).head(6)
    config = json.loads((SOURCE / "manifest.json").read_text())["source_config"]
    basis = gpd.read_parquet(ROOT / config["basis"]).to_crs(32638).set_index("internal_parcel_id")
    image = Image.new("RGB", (1500, 1410), "#ffffff")
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=29)
    label = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=18)
    draw.text((35, 20), "Shared-pixel consolidation screening: largest groups", font=title, fill="#192e2a")
    draw.text((35, 62), "Private review | each parcel <=0.5 ha | all six groups are BELOW the 5 ha minimum", font=label, fill="#a23232")
    colors = ["#168c79", "#3f78ae", "#b2752e", "#965a88", "#568249", "#bd5555"]
    for n, group in enumerate(groups.itertuples()):
        x0, y0 = 35 + (n % 2) * 735, 115 + (n // 2) * 410
        selected = basis.loc[json.loads(group.member_ids)]
        minx, miny, maxx, maxy = selected.total_bounds
        pad = max(maxx - minx, maxy - miny) * .10
        minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        scale = min(680 / (maxx - minx), 290 / (maxy - miny))
        dx = x0 + (690 - (maxx - minx) * scale) / 2
        dy = y0 + 85 + (290 - (maxy - miny) * scale) / 2
        def xy(ring):
            return [(dx + (x - minx) * scale, dy + (maxy - y) * scale) for x, y in ring.coords]
        def paint(geometry, fill, outline):
            clipped = geometry.intersection(box(minx, miny, maxx, maxy))
            polygons = [clipped] if clipped.geom_type == "Polygon" else getattr(clipped, "geoms", [])
            for polygon in polygons:
                if polygon.geom_type != "Polygon" or polygon.is_empty:
                    continue
                draw.polygon(xy(polygon.exterior), fill=fill, outline=outline)
                for ring in polygon.interiors:
                    draw.polygon(xy(ring), fill="white", outline=outline)
        draw.text((x0, y0), f"{n + 1}. {group.area_ha:.3f} ha | {group.parcel_count} parcels", font=label, fill="#192e2a")
        years = [str(y) for bit, y in enumerate(range(2021, 2026)) if group.support_year_mask & (1 << bit)]
        draw.text((x0, y0 + 33), "Common matching years: " + ", ".join(years), font=small, fill="#52635d")
        nearby = basis.iloc[basis.sindex.query(box(minx, miny, maxx, maxy), predicate="intersects")]
        for geom in nearby.geometry:
            paint(geom, "#edf0ef", "#bbc5c1")
        for geom in selected.geometry:
            paint(geom, colors[n], "#ffffff")
        draw.line((x0, y0 + 393, x0 + 690, y0 + 393), fill="#dce2df", width=1)
    draw.text((35, 1360), "Original cadastral outlines. Joint EO evidence is not independent confirmation for each parcel.", font=small, fill="#52635d")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(OUT)


if __name__ == "__main__":
    execute()
