"""Read guard for plan v6 owner decision 2: no SQL archive use at all, and nothing read from outside the product.

While a ReadGuard is active it records every file a build opens, through Python's open (audit hook) and through the
native readers that bypass it (pandas/pyarrow parquet, geopandas/pyogrio, numpy), and refuses:
  - any path that looks like the SQL archive or an archive-derived layer (ARCHIVE);
  - any path in this product's parent project (never read the parent);
  - any data path outside the product folder (Python's own install and the temp folder are allowed);
  - importing an SQL Server driver.
The recorded list goes into the build manifest, so a test can prove what a build read.
"""
from __future__ import annotations

import os
import re
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The archive's own table and layer names stay out of the code: set ARCHIVE_NAMES to a regex of them.
ARCHIVE = re.compile("|".join(filter(None, [os.environ.get("ARCHIVE_NAMES", ""), r"sql_?archive|sqlcmd|\.(bak|mdf|ldf|dwg)$"])), re.I)
SQL_MODULES = {"pyodbc", "pymssql", "_mssql", "sqlalchemy", "adodbapi", "turbodbc", "pytds"}
_ACTIVE: list["ReadGuard"] = []
_HOOKED = False
_LOCAL = threading.local()                  # re-entrancy flag: work done inside the hook never re-enters it


class ArchiveReadError(RuntimeError):
    pass


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


_ROOT_N = _norm(ROOT)
_PARENT_N = _norm(ROOT.parent)
_ALLOWED_OUTSIDE = sorted({_norm(p) for p in [sys.prefix, sys.base_prefix, sys.exec_prefix, tempfile.gettempdir(),
                                              *(sysconfig.get_paths().get(k) for k in ("stdlib", "platstdlib", "purelib", "platlib"))]
                           if p})


def _under(p, base):
    return p == base or p.startswith(base.rstrip("\\/") + os.sep)


def problem(path) -> str | None:
    """Why reading this path is refused, or None when it is allowed. Pure string work (safe inside an audit hook)."""
    p = _norm(path)
    if ARCHIVE.search(p):
        return "SQL archive or archive-derived file"
    if _under(p, _ROOT_N):
        return None
    if _under(p, _PARENT_N):
        return "parent project (outside the product)"
    if any(_under(p, a) for a in _ALLOWED_OUTSIDE):
        return None
    return "outside the product folder"


def _hook(event, args):
    if not _ACTIVE or getattr(_LOCAL, "busy", False):
        return
    if event not in ("open", "import"):
        return
    _LOCAL.busy = True
    try:
        if event == "open":
            path = args[0]
            if isinstance(path, (str, Path)):
                _ACTIVE[-1].record(path, "open")
        else:
            name = str(args[0]).split(".")[0]
            if name in SQL_MODULES:
                _ACTIVE[-1].refuse(name, "SQL Server driver import")
    finally:
        _LOCAL.busy = False


def _wrap(mod, name, how):
    f = getattr(mod, name)
    if getattr(f, "_read_guard", False):
        return None

    def g(*a, **k):
        src = a[0] if a else k.get("path", k.get("filename", k.get("file", k.get("path_or_buffer"))))
        if _ACTIVE and isinstance(src, (str, Path)):
            _ACTIVE[-1].record(src, how)
        return f(*a, **k)
    g._read_guard = True
    setattr(mod, name, g)
    return f


class ReadGuard:
    """with ReadGuard() as rg: ...build... ; rg.files -> sorted product-relative paths read."""

    def __init__(self):
        self.reads: dict[str, set] = {}
        self.violations: list[dict] = []
        self._orig = []

    def record(self, path, how):
        why = problem(path)
        p = _norm(path)
        inside = _under(p, _ROOT_N)
        key = os.path.relpath(p, _ROOT_N).replace(os.sep, "/") if inside else p
        if why:
            self.refuse(key, why)
        elif inside:
            self.reads.setdefault(key, set()).add(how)

    def refuse(self, what, why):
        self.violations.append({"path": str(what), "reason": why})
        raise ArchiveReadError(f"read refused ({why}): {what}")

    @property
    def files(self):
        """Product files read, excluding Python sources and caches."""
        return sorted(k for k in self.reads if not k.endswith((".py", ".pyc")) and "__pycache__" not in k)

    def __enter__(self):
        global _HOOKED
        if not _HOOKED:
            sys.addaudithook(_hook)
            _HOOKED = True
        import numpy
        import pandas
        targets = [(pandas, "read_parquet", "parquet"), (numpy, "load", "numpy")]
        try:
            import geopandas
            targets += [(geopandas, "read_file", "vector"), (geopandas, "read_parquet", "geoparquet")]
        except ImportError:
            pass
        try:
            import pyogrio
            targets += [(pyogrio, "read_dataframe", "vector"), (pyogrio, "read_info", "vector")]
        except ImportError:
            pass
        for mod, name, how in targets:
            orig = _wrap(mod, name, how)
            if orig is not None:
                self._orig.append((mod, name, orig))
        for name in SQL_MODULES:
            if name in sys.modules:
                self.refuse(name, "SQL Server driver already imported")
        _ACTIVE.append(self)
        return self

    def __exit__(self, *exc):
        _ACTIVE.remove(self)
        for mod, name, orig in self._orig:
            setattr(mod, name, orig)
        self._orig = []
        return False

    def report(self):
        return {"files_read": self.files, "violations": self.violations,
                "rule": "plan v6 decision 2: no SQL archive read; inputs only from inside the product"}
