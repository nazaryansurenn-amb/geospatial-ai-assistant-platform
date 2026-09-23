"""Private cadastral preview of the largest long-strip priority candidates."""
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/analysis/land_consolidation/consolidation_strip_priority_20260906_v1"
OUT = ROOT / "server_data/review/consolidation_strip_priority_20260906_v1/priority_groups.png"


def execute():
    groups = gpd.read_parquet(SOURCE / "priority_blocks.parquet").sort_values(["area_ha", "block_id"], ascending=[False, True]).head(6)
    config = json.loads((SOURCE / "manifest.json").read_text())["source_config"]
    basis = gpd.read_parquet(ROOT / config["basis"]).to_crs(32638).set_index("internal_parcel_id")
    register = pd.read_parquet(SOURCE / "register.parquet").set_index("internal_parcel_id")
    image = Image.new("RGB", (1500, 1510), "#ffffff")
    draw = ImageDraw.Draw(image)
    title, label, small = (ImageFont.load_default(size=s) for s in (29, 22, 18))
    draw.text((35, 20), "Consolidation screening: long-strip priority", font=title, fill="#192e2a")
    draw.text((35, 62), "Private review | member <=1 ha | connected group >=3 ha | six largest priority groups", font=label, fill="#52635d")
    draw.rectangle((35, 101, 51, 117), fill="#168c79")
    draw.text((61, 98), "Long/narrow member", font=small, fill="#192e2a")
    draw.rectangle((330, 101, 346, 117), fill="#deb34c")
    draw.text((356, 98), "Other qualifying member", font=small, fill="#192e2a")
    draw.rectangle((665, 101, 681, 117), fill="#edf0ef", outline="#bbc5c1")
    draw.text((691, 98), "Surrounding cadastre", font=small, fill="#192e2a")
    for n, group in enumerate(groups.itertuples()):
        x0, y0 = 35 + n % 2 * 735, 155 + n // 2 * 420
        selected = basis.loc[json.loads(group.member_ids)]
        minx, miny, maxx, maxy = selected.total_bounds
        pad = max(maxx - minx, maxy - miny) * .10
        minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        scale = min(680 / (maxx - minx), 275 / (maxy - miny))
        dx = x0 + (690 - (maxx - minx) * scale) / 2
        dy = y0 + 110 + (275 - (maxy - miny) * scale) / 2
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
        stage = group.stage.replace("stage_1", "I").replace("stage_2", "II")
        draw.text((x0, y0 + 32), f"{group.strip_parcel_count} strips | {group.strip_area_fraction:.0%} of group area | stage {stage}", font=small, fill="#52635d")
        draw.text((x0, y0 + 60), "Common matching years: " + group.support_years.replace(",", ", "), font=small, fill="#52635d")
        nearby = basis.iloc[basis.sindex.query(box(minx, miny, maxx, maxy), predicate="intersects")]
        for geom in nearby.geometry:
            paint(geom, "#edf0ef", "#bbc5c1")
        for pid, row in selected.iterrows():
            paint(row.geometry, "#168c79" if register.loc[pid].long_narrow else "#deb34c", "#ffffff")
        draw.line((x0, y0 + 403, x0 + 690, y0 + 403), fill="#dce2df", width=1)
    draw.text((35, 1440), "Original cadastral outlines. Priority is a screening choice, not ownership or feasibility confirmation.", font=small, fill="#52635d")
    draw.text((35, 1468), "Shared EO pixels are permitted: matching signals are not independent parcel-level confirmation.", font=small, fill="#52635d")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(OUT)


if __name__ == "__main__":
    execute()
