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
