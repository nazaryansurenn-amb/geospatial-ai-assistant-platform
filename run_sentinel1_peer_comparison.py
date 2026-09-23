"""Prepare or run the private, weather-gated 96-parcel comparison once."""
import argparse
import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"

from wp_core.sentinel1_peer_comparison import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "status", "run-if-ready", "verify"))
    main(parser.parse_args().action)
