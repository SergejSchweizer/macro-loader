from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_hook_installer_is_idempotent() -> None:
    subprocess.run(["bash", "scripts/install_hooks.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "scripts/install_hooks.sh"], cwd=ROOT, check=True)
    installed = ROOT / ".git/hooks/pre-commit"
    assert installed.exists()
    assert os.access(installed, os.X_OK)
    assert installed.read_text(encoding="utf-8") == (ROOT / ".githooks/pre-commit").read_text(
        encoding="utf-8"
    )


def test_required_gate_does_not_select_network_tests() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert '-m "not network"' in makefile
    assert '-m "integration and not network"' in makefile
