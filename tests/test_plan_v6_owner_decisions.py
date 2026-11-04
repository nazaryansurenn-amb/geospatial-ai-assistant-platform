"""Plan v6 owner decisions (checklist A1, A2): the 12 m3/s main canal with spill above it, and no SQL archive use at all.

A2 is proven three ways: the read guard refuses archive, parent-project and outside paths and SQL Server drivers; the
real builds' manifests list every file they read (all inside the product, none archive-like); and no builder or tool
source imports an SQL driver, names an archive layer or reads the parent project."""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import read_guard as rg  # noqa: E402
from wp_core.canal_inflow import MAIN_CAPACITY_M3S, cap_inflow, dekad_volume_m3  # noqa: E402

BUILDS = [ROOT / "data/analysis/canal_ops_sim_v1/network_v2", ROOT / "data/analysis/canal_ops_sim_v1/map"]
SOURCES = sorted([*ROOT.glob("scripts/canal_ops_*.py"), *ROOT.glob("wp_core/canal_*.py"), ROOT / "scripts/irrigation_assistant.py"])
SQL_IMPORT = re.compile(r"^\s*(import|from)\s+(pyodbc|pymssql|_mssql|sqlalchemy|adodbapi|turbodbc|pytds)\b", re.M)
ARCHIVE_TOKEN = re.compile(r"sqlcmd|sql_archive|\.(bak|mdf|ldf|dwg)['\"]", re.I)
PARENT_READ = re.compile(r"parents\[2\]|ROOT\.parent|\.\.[\\/]")


# ---------------------------------------------------------------- A1
def test_main_canal_capacity_is_12_m3s():
    assert MAIN_CAPACITY_M3S == 12.0


def test_owner_flow_below_capacity_passes_unchanged():
    r = cap_inflow(6)
    assert r["accepted_m3s"] == 6.0 and r["spill_m3s"] == 0.0 and not r["capped"]


def test_owner_flow_above_capacity_is_capped_and_the_excess_is_spill():
    r = cap_inflow(15.5)
    assert r["accepted_m3s"] == 12.0 and r["spill_m3s"] == pytest.approx(3.5) and r["capped"]
    assert "12 m3/s" in r["capacity_basis"]


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), "6", None, True])
def test_bad_flows_are_refused(bad):
    with pytest.raises(ValueError):
        cap_inflow(bad)


def test_volume_is_flow_times_seconds():
    assert dekad_volume_m3(6.0, 10) == 6.0 * 10 * 86400


def test_network_head_uses_the_same_capacity():
    p = ROOT / "data/analysis/canal_ops_sim_v1/network_v2/nodes.parquet"
    if not p.exists():
        pytest.skip("network v2 not built")
    nodes = pd.read_parquet(p).set_index("node")
    assert nodes.at["I", "capacity_lps"] == MAIN_CAPACITY_M3S * 1000
    assert (nodes.capacity_lps.dropna() <= MAIN_CAPACITY_M3S * 1000).all()


# ---------------------------------------------------------------- A2: the guard
def test_guard_refuses_archive_parent_and_outside_paths():
    assert rg.problem(ROOT / "data/source/lower_hrazdan.geojson") is None
    assert rg.problem(ROOT / "x" / "archive_2020.bak") == "SQL archive or archive-derived file"
    assert rg.problem(ROOT / "y" / "backup_2019.mdf") == "SQL archive or archive-derived file"
    assert rg.problem(ROOT.parent / "data" / "agent" / "x.json") == "parent project (outside the product)"
    assert rg.problem(Path("D:/elsewhere/table.csv")) == "outside the product folder"


def test_guard_records_reads_and_blocks_an_archive_open(tmp_path):
    with rg.ReadGuard() as g:
        pd.read_parquet(ROOT / "data/analysis/canal_ops_sim_v1/network/branches.parquet")
        (ROOT / "config/canal_ops_network_v2.json").read_text(encoding="utf-8")
        bad = ROOT / "data" / "sql_archive_layer.json"
        with pytest.raises(rg.ArchiveReadError):
            open(bad, encoding="utf-8")
    assert "data/analysis/canal_ops_sim_v1/network/branches.parquet" in g.files
    assert "config/canal_ops_network_v2.json" in g.files
    assert g.violations and g.violations[0]["reason"] == "SQL archive or archive-derived file"


def test_guard_blocks_sql_server_drivers():
    sys.modules.pop("pyodbc", None)
    with rg.ReadGuard():
        with pytest.raises(rg.ArchiveReadError):
            import pyodbc  # noqa: F401


def test_guard_is_inactive_outside_its_block():
    with rg.ReadGuard():
        pass
    (ROOT / "config/canal_ops_network_v2.json").read_text(encoding="utf-8")   # no guard: nothing recorded, no error


# ---------------------------------------------------------------- A2: what the real builds read
@pytest.mark.parametrize("out", BUILDS, ids=["network_v2", "map"])
def test_real_builds_read_only_product_files(out):
    if not (out / "manifest.json").exists():
        pytest.skip("not built")
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert man["read_guard_violations"] == []
    assert man["files_read"] and "SQL archive" in man["not_used"]
    for f in man["files_read"]:
        assert rg.problem(ROOT / f) is None, f
        assert (ROOT / f).exists() and not re.search(r"archive|sql_?server|\.bak$", f, re.I), f


# ---------------------------------------------------------------- A2: no tool or builder path touches it
@pytest.mark.parametrize("src", SOURCES, ids=[p.name for p in SOURCES])
def test_no_builder_or_tool_source_reaches_the_archive(src):
    text = src.read_text(encoding="utf-8")
    assert not SQL_IMPORT.search(text)
    lines = [ln for ln in text.splitlines() if not (src.name == "canal_ops_checker.py" and "re.compile(" in ln)]
    assert not ARCHIVE_TOKEN.search("\n".join(lines)), "archive name outside the Checker's blocking patterns"
    assert not PARENT_READ.search(text)
