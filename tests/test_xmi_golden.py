"""The XMI export stays byte for byte what it was before ea-apply reused its builder (plan 2026-11-05, section 11).

The golden files were written by the code before that change. Regenerate them only for an intended change of the
export, with ARCHCREATOR_UPDATE_GOLDEN=1, and review the diff.
"""

import os
from pathlib import Path

import pytest

from archcreator.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
CASES = [  # (fixture, module, context) covering structure, context modules, macros and signals
    ("fx01", "alpha", "all"),
    ("fx01", "beta", None),
    ("fx02", "alpha", "all"),
    ("fx02", "beta", "all"),
]


@pytest.mark.parametrize("fixture,module,context", CASES)
def test_xmi_equals_golden(tmp_path, fixture, module, context):
    repo = FIXTURES / fixture
    args = ["analyze", "--repo", str(repo), "--module", module, "--profile", str(repo / "profile.yaml"),
            "--out", str(tmp_path), "--xmi"]
    if context:
        args += ["--context", context]
    assert main(args) in (0, 1)
    produced = (tmp_path / "model.xmi").read_bytes()
    golden = GOLDEN / f"{fixture}_{module}.xmi"
    if os.environ.get("ARCHCREATOR_UPDATE_GOLDEN") == "1":
        golden.write_bytes(produced)
    assert produced == golden.read_bytes()
