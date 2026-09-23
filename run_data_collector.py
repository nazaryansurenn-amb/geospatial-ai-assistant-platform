"""Standalone entry point, independent of map applications and the parent repo."""
from pathlib import Path
from wp_core.data_collector.runner import main

if __name__ == "__main__":
    main(Path(__file__).resolve().parent)
