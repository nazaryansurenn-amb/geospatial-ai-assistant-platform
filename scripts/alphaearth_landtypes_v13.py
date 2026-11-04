"""Land types v1.3 (owner 2026-11-01: "freeze current situation also"): v1.2 plus every owner label given so far on
the labelling page (answers/owner_unclassified_v12__owner.json). Owner labels win. Written once, read-only.

  alphaearth_landtypes_v13.py freeze [NAME]   -> releases/landtypes_<NAME, default v1_3>/

Labelling continues after the freeze; later labels go into a later version. "Can't tell" answers leave a parcel
UNCLASSIFIED. Owner labels from the labelling page are not a random sample: they never enter accuracy figures.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT  # noqa: E402

BASE = OUT / "releases/landtypes_v1_2"
LABELS = OUT / "answers/owner_unclassified_v12__owner.json"
MAIN = {"ORCHARD": "Perennial plantings", "VINEYARD": "Perennial plantings", "PERENNIAL": "Perennial plantings",
        "FIELD_CROP": "Field land", "NO_CROP": "Field land", "CROPLAND_UNCERTAIN": "Field land"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(name="v1_3"):
    target = OUT / "releases" / f"landtypes_{name}"
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    labels = json.loads(LABELS.read_text(encoding="utf-8"))          # one read = the snapshot frozen below
    d = pd.read_parquet(BASE / "parcels.parquet")
    d["type6_v12"] = d.type6
    lab = d.item_id.map(lambda i: labels.get(i, {}).get("type"))
    take = lab.notna() & (lab != "cannot_tell") & (d.type6 == "UNCLASSIFIED")
    d.loc[take, "type6"] = lab[take]
    d.loc[take, "owner_label"] = lab[take]
    d.loc[take, "source_v11"] = "owner label (labelling page)"
    pairs_file = OUT / "answers/owner_nci_pairs__owner.json"
    if name != "v1_3" and pairs_file.exists():       # v1.4+: the owner's sliver-pair labels (one answer for both slivers)
        pa = json.loads(pairs_file.read_text(encoding="utf-8"))
        items = json.loads((OUT / "answers/owner_nci_pairs_items.json").read_text(encoding="utf-8"))
        sliver_type = {s_: pa[it["id"]]["type"] for it in items if it["id"] in pa and pa[it["id"]]["type"] != "cannot_tell"
                       for s_ in it["slivers"]}
        sl = d.internal_parcel_id.map(sliver_type)
        take_sl = sl.notna() & (d.status == "fixed_nci")
        d.loc[take_sl, "type6"] = sl[take_sl]
        d.loc[take_sl, "owner_label"] = sl[take_sl]
        d.loc[take_sl, "source_v11"] = "owner label (sliver pair)"
        take = take | take_sl
    d["owner_label_page"] = take
    d["main_class"] = d.type6.map(MAIN).fillna(d.type6)
    target.mkdir(parents=True)
    d.to_parquet(target / "parcels.parquet", index=False)
    d.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    (target / "frozen").mkdir()
    snap = target / "frozen" / "owner_unclassified_v12__owner.json"
    snap.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(Path(__file__), target / "frozen" / Path(__file__).name)
    if name != "v1_3" and pairs_file.exists():
        for f in (pairs_file, OUT / "answers/owner_nci_pairs_items.json"):
            shutil.copyfile(f, target / "frozen" / f.name)
    counts = lambda col: {str(k): {"parcels": int(len(g)), "ha": round(g.area_ha.sum(), 1)}
                          for k, g in d.groupby(d[col].fillna("not classifiable"))}
    summary = {"version": name.replace("_", "."), "base": "landtypes_v1_2", "owner_labels_in_snapshot": len(labels),
               "owner_labels_applied": int(take.sum()),
               "cannot_tell": sum(1 for v in labels.values() if v.get("type") == "cannot_tell"),
               "guess_seen": sum(1 for v in labels.values() if v.get("guess_seen")),
               "types": counts("type6"), "main_classes": counts("main_class"),
               "note": "owner labels from the labelling page win; they are not a random sample and never enter accuracy "
                       "figures; v1.2 accuracy ranges still describe the AI-typed parcels"}
    (target / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (target / "manifest.json").write_text(json.dumps({
        "version": summary["version"], "created": datetime.now(timezone.utc).isoformat(),
        "label": "AI-interpreted from imagery + owner labels; not field-verified",
        "inputs": {"releases/landtypes_v1_2/parcels.parquet": sha(BASE / "parcels.parquet"),
                   "frozen/owner_unclassified_v12__owner.json": sha(snap)},
        "parcels_sha256": sha(target / "parcels.parquet")}, indent=1), encoding="utf-8")
    (target / "README.md").write_text(
        f"# Land types {summary['version']} (frozen {datetime.now(timezone.utc).date()})\n\n"
        f"v1.2 plus {int(take.sum())} owner labels from the labelling page (snapshot in frozen/). Owner labels win. "
        "Read-only; a new version goes to a new folder. Owner labels are not a random sample and never enter accuracy "
        "figures.\n", encoding="utf-8")
    for f in target.rglob("*"):
        if f.is_file():
            f.chmod(0o444)
    print(json.dumps({k: summary[k] for k in ("owner_labels_in_snapshot", "owner_labels_applied", "cannot_tell", "main_classes")}, indent=1))


if __name__ == "__main__":
    if sys.argv[1:2] == ["freeze"]:
        freeze(*sys.argv[2:3])
    else:
        sys.exit(__doc__)
