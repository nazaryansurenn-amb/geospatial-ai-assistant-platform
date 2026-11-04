"""Payload review (canal_ops_sim_v1): the real-data sample is clean and approval is owner-only."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_payload_review as pr  # noqa: E402
from wp_core import canal_ops_world as cw  # noqa: E402


@pytest.mark.skipif(not cw.available(), reason="network/demand outputs not built")
def test_sample_has_no_restricted_content():
    s = pr.sample()
    assert all(v == 0 for v in s["restricted_hits"].values())
    assert pr.DOC.exists() and "What never goes to the model" in pr.DOC.read_text(encoding="utf-8")


def test_approve_refuses_without_owner_flag_or_terminal():
    py = str(ROOT / ".venv/Scripts/python.exe")
    r1 = subprocess.run([py, "-B", "scripts/canal_ops_payload_review.py", "approve"], cwd=ROOT, capture_output=True,
                        text=True, stdin=subprocess.DEVNULL)
    assert r1.returncode != 0 and "owner only" in (r1.stderr + r1.stdout)
    r2 = subprocess.run([py, "-B", "scripts/canal_ops_payload_review.py", "approve", "--i-am-the-owner"], cwd=ROOT,
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r2.returncode != 0 and "terminal" in (r2.stderr + r2.stdout)
    assert not pr.APPROVAL.exists() or "owner (typed confirmation)" in pr.APPROVAL.read_text(encoding="utf-8")
