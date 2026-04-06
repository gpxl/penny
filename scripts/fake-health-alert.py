#!/usr/bin/env python3
"""Inject fake health alerts into Penny's state for visual testing.

Usage:
    python scripts/fake-health-alert.py          # inject red + yellow alerts
    python scripts/fake-health-alert.py --clear   # remove all alerts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Resolve state file the same way Penny does
import os

penny_home = os.environ.get("PENNY_HOME")
state_path = Path(penny_home) / "state.json" if penny_home else Path.home() / ".penny" / "state.json"

if not state_path.exists():
    print(f"State file not found: {state_path}")
    print("Is Penny running?")
    sys.exit(1)

with state_path.open() as f:
    state = json.load(f)

if "--clear" in sys.argv:
    state.pop("health_alerts", None)
    tmp = state_path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f)
    tmp.replace(state_path)
    print("Cleared health_alerts from state.")
    sys.exit(0)

# Inject realistic-looking alerts with new budget-aware format
state["health_alerts"] = [
    {
        "project": "Weekly Budget",
        "cwd": "",
        "health": "yellow",
        "reasons": [
            "Projected to use 92% of weekly budget by reset "
            "(1.8d remaining). Currently at 71%.",
        ],
    },
    {
        "project": "temper-trap",
        "cwd": "/Users/gerlando/projects/temper-trap",
        "health": "red",
        "reasons": [
            "High error rate: 37 of 60 tool calls failed (62%)",
            "Burn rate 54k tok/h this session is 4.9x the project's "
            "active-hour average (11k tok/h). May indicate a runaway process.",
        ],
    },
    {
        "project": "side-project",
        "cwd": "/Users/gerlando/projects/side-project",
        "health": "yellow",
        "reasons": [
            "Elevated error rate: 12 of 48 tool calls failed (25%)",
        ],
    },
]

tmp = state_path.with_suffix(".tmp")
with tmp.open("w") as f:
    json.dump(state, f)
tmp.replace(state_path)

print("Injected fake health alerts into Penny state:")
for a in state["health_alerts"]:
    icon = "RED" if a["health"] == "red" else "YLW"
    print(f"  [{icon}] {a['project']}: {', '.join(a['reasons'])}")
print()
print("Menu bar should update within ~30 seconds (next health check).")
print("Dashboard should update on next poll (30s).")
print()
print("To clear: python scripts/fake-health-alert.py --clear")
