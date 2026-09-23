"""Render private close-ups without changing a map server or sealed results."""
from pathlib import Path

import geopandas as gpd
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
VERSION = "consolidation_active_20260906_v1"


def main():
    source = ROOT / "data/analysis/land_consolidation" / VERSION
    destination = ROOT / "server_data/review" / VERSION / "candidate_details.png"
    if destination.exists():
        print(destination)
        return
    blocks = gpd.read_parquet(source / "blocks.parquet").sort_values("area_ha", ascending=False)
    members = pd.read_parquet(source / "members.parquet")
    basis = gpd.read_parquet(ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet").to_crs(32638)
    width, height = 1600, 1400
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    small = ImageFont.load_default(size=20)
    draw.text((35, 20), "Potential consolidation blocks | private review", font=font, fill="#18312a")
    draw.text((35, 62), "Original cadastral parcels retained. Similar historical EO profiles, not confirmed feasibility.", font=small, fill="#364743")
    for index, block in enumerate(blocks.head(4).itertuples()):
        left, top = 35 + (index % 2) * 795, 120 + (index // 2) * 625
        plot_left, plot_top, plot_width, plot_height = left + 5, top + 95, 720, 460
        x0, y0, x1, y1 = block.geometry.bounds
        pad = max(x1 - x0, y1 - y0) * .12
        bounds = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        x0, y0, x1, y1 = bounds
        scale = min(plot_width / (x1 - x0), plot_height / (y1 - y0))
        def coords(ring):
            return [(plot_left + (x - x0) * scale, plot_top + plot_height - (y - y0) * scale) for x, y in ring.coords]
        def paint(geom, fill, outline):
            polygons = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
            for polygon in polygons:
                if polygon.geom_type != "Polygon":
                    continue
                draw.polygon(coords(polygon.exterior), fill=fill, outline=outline, width=2)
                for ring in polygon.interiors:
                    draw.polygon(coords(ring), fill="white", outline=outline)
        from shapely.geometry import box
        nearby = basis.iloc[basis.sindex.query(box(*bounds), predicate="intersects")]
        for geometry in nearby.geometry:
            clipped = geometry.intersection(box(*bounds))
            if not clipped.is_empty:
                paint(clipped, "#edf0f1", "#c5cccf")
        selected = members[members.block_id.eq(block.block_id)].sort_values("cadastre_code")
        colors = ["#70bec2", "#efbd63"]
        for n, row in enumerate(selected.itertuples()):
            geometry = basis.loc[basis.internal_parcel_id.eq(row.internal_parcel_id), "geometry"].iloc[0]
            paint(geometry, colors[n % 2], "#1f4b42")
            point = geometry.representative_point()
            x, y = plot_left + (point.x - x0) * scale, plot_top + plot_height - (point.y - y0) * scale
            draw.text((x, y), str(n + 1), font=font, fill="#172725", anchor="mm")
        draw.text((left, top), f"Block {index + 1}: {block.area_ha:.2f} ha | Stage II", font=font, fill="#172725")
        draw.text((left, top + 38), "Supporting seasons: " + block.support_years.replace(",", ", "), font=small, fill="#364743")
        labels = "  |  ".join(f"{n + 1}: {r.cadastre_code}" for n, r in enumerate(selected.itertuples()))
        draw.text((left, top + 73), labels, font=small, fill="#364743")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    print(destination)


if __name__ == "__main__":
    main()
