#!/usr/bin/env python
"""Check the metric policy and the source registry heliosd would load: the
repository defaults merged with the overlays in HELIOS_HOME (default
~/Helios), each validated on its own, then against each other (design B15):
every device a policy list names exists in the registry, a HealthKit copy key
comes with its device's sync paths, a derived metric has a daily parent in
its own unit, a history-only device heads no metric that is not optional.
The daemon refuses to start on the same problems; this says so beforehand.

    server/.venv/bin/python server/tools/check_policy.py [--home DIR]

Exit 0 with one summary line when the files agree, 1 with one line per
problem when they do not. Read only. Prints paths and keys, never a value
from the store and never a setting from helios.toml (it is not read).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", help="the folder holding metric_policy.yaml and source_registry.yaml "
                                   "(default: $HELIOS_HOME, else ~/Helios)")
    args = ap.parse_args(argv)
    if args.home:
        os.environ["HELIOS_HOME"] = str(Path(args.home).expanduser())
    from heliosd import config
    from heliosd.trust.policy import MetricPolicy
    from heliosd.trust.registry import SourceRegistry
    from heliosd.trust.schema import PolicyError, registry_problems

    print(f"policy home {config.helios_home()} (overlays: {', '.join(config.active_overlays()) or 'none'})")
    header = None
    try:
        policy, registry = MetricPolicy(), SourceRegistry()
        problems = registry_problems(policy, registry)
    except PolicyError as e:                    # a file is invalid on its own
        header, problems = str(e).splitlines()[0], list(e.problems)
    if problems:
        if header:
            print(header)
        for p in problems:
            print(f"PROBLEM {p}")
        print(f"{len(problems)} problem(s): heliosd would refuse to start")
        return 1
    print(f"OK: {len(policy.metrics)} metrics and {len(registry.devices)} devices agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
