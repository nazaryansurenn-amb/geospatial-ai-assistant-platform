from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageFilter


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PRODUCT_ROOT / "public" / "data" / "cadastre_overview.png"
DEFAULT_OUTPUT = PRODUCT_ROOT / "public" / "data" / "cadastre_overview_low.png"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGBA")
    alpha = source.getchannel("A").filter(ImageFilter.MaxFilter(15))
    overview = Image.new("RGBA", source.size, (255, 255, 255, 0))
    overview.putalpha(alpha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    overview.save(args.output, optimize=True)
    print(f"Prepared low-zoom cadastral overview: {overview.size[0]}x{overview.size[1]}")


if __name__ == "__main__":
    main()
