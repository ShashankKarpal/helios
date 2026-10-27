"""Test environment: point HELIOS_HOME at the synthetic fixture overlay before
any heliosd module loads config, so the suite sees the same device lineup on a
fresh clone as on the owner's Mac, and never reads the owner's real overlay."""

from __future__ import annotations

import os
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["HELIOS_HOME"] = str(FIXTURES)
# Never let a developer's real config leak into the suite either.
os.environ.pop("HELIOS_CONFIG", None)


import pytest  # noqa: E402


@pytest.fixture
def brief_gate_open(monkeypatch):
    """The publish gate (rank 3 fix, 2026-10-27) records no brief before 07:30
    in the reporting zone, and from 07:30 only once Whoop's night has settled:
    pin the gate's clock at 10:00 on the real reporting day (past the 09:30
    fallback), so a test that opens Today and reads the record runs the same
    at any hour. The day itself and the brief's own clock stay real."""
    from datetime import datetime

    from heliosd import main

    def ten(zone):
        return datetime.now(zone).replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(main, "_now_local", ten)
