"""Run the independent Sentinel-1 pilot using the product's own environment."""
import argparse
import os
for name in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"]:
    os.environ[name] = "1"
from wp_core.sentinel1_pilot import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "collect", "analyze", "verify"])
    main(parser.parse_args().action)
