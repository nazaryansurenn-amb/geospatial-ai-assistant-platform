"""Launch one verified release. Existing ports are never replaced."""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from release_tools import load_runtime, verify_release

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", default="working", choices=["working", "use_type_review"])
    parser.add_argument("--port", type=int, default=8522)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only:
        print(json.dumps(verify_release(ROOT, args.release), indent=2))
        return
    if args.port in {8520, 8521}:
        parser.error("8520/8521 are preserved. Use a separate technical verification port.")
    sys.dont_write_bytecode = True
    module = load_runtime(ROOT, args.release)
    handler = partial(module.ProductRequestHandler, directory=str(module.DIST_ROOT))
    server = module.ProductServer(("127.0.0.1", args.port), handler)
    print(f"Verified {args.release}: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
