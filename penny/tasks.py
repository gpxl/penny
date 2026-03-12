"""Core task dataclass used by Penny and its plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    task_id: str
    title: str
    priority: str
    project_path: str
    project_name: str
    raw_line: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
