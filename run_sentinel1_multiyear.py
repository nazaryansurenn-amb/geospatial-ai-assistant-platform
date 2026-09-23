"""Extend the existing private pilot to 2021–2025, retaining versioned snapshots."""
import os
for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
import argparse
from wp_core.sentinel1_multiyear import main

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["prepare", "collect", "extract", "analyze", "verify"])
    main(p.parse_args().action)
