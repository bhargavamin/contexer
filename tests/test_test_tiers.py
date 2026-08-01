"""Regression checks for the documented pytest tier interface."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("tier", ("fast", "integration", "accuracy", "harness"))
def test_pytest_registers_test_tier(tier):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"@pytest.mark.{tier}:" in result.stdout
