"""Nae Nae — pre-flight validation checks."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreflightIssue:
    severity: str   # "error" | "warning"
    message: str
    fix_hint: str


def run_preflight(config: dict[str, Any]) -> list[PreflightIssue]:
    """Run all pre-flight checks and return a list of issues (may be empty)."""
    issues: list[PreflightIssue] = []

    # ── CLI tool checks ────────────────────────────────────────────────────

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        issues.append(PreflightIssue(
            severity="error",
            message="`claude` CLI not found in PATH.",
            fix_hint="Install it: npm install -g @anthropic-ai/claude-code\n"
                     "Then re-run install.sh so launchd picks up the new PATH.",
        ))
    else:
        # Sanity-check that the binary actually runs
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                issues.append(PreflightIssue(
                    severity="warning",
                    message="`claude --version` exited with a non-zero code.",
                    fix_hint="Run `claude --version` in a terminal to diagnose.",
                ))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            issues.append(PreflightIssue(
                severity="warning",
                message="`claude` found but failed to execute.",
                fix_hint="Run `claude --version` in a terminal to diagnose.",
            ))


    bd_bin = shutil.which("bd")
    if bd_bin is None:
        issues.append(PreflightIssue(
            severity="error",
            message="`bd` (beads) CLI not found in PATH.",
            fix_hint="Install it: npm install -g beads-cli  (or follow beads docs)\n"
                     "Then re-run install.sh so launchd picks up the new PATH.",
        ))

    # ── Stats cache check ──────────────────────────────────────────────────

    stats_path_str: str = config.get("stats_cache_path", "~/.claude/stats-cache.json")
    stats_path = Path(stats_path_str).expanduser()
    if not stats_path.exists():
        issues.append(PreflightIssue(
            severity="warning",
            message=f"Stats cache not found at {stats_path}.",
            fix_hint="Use Claude Code in a terminal session to generate token usage data.",
        ))

    projects_dir = Path("~/.claude/projects").expanduser()
    if not any(projects_dir.rglob("*.jsonl")):
        issues.append(PreflightIssue(
            severity="warning",
            message="No Claude session files found in ~/.claude/projects/.",
            fix_hint="Start a Claude Code session to generate usage history.",
        ))

    # ── Config checks ──────────────────────────────────────────────────────

    projects: list[dict[str, Any]] = config.get("projects", [])
    if not projects:
        issues.append(PreflightIssue(
            severity="error",
            message="No projects configured.",
            fix_hint="Edit config.yaml and add at least one project path.",
        ))
    else:
        for entry in projects:
            path_str: str = entry.get("path", "")
            if "PLACEHOLDER" in path_str:
                issues.append(PreflightIssue(
                    severity="error",
                    message=f"Project path still contains placeholder: {path_str!r}",
                    fix_hint="Edit config.yaml and replace PLACEHOLDER_PROJECT_PATH "
                             "with your actual project directory.",
                ))
                continue

            project_path = Path(path_str).expanduser()
            if not project_path.exists():
                issues.append(PreflightIssue(
                    severity="warning",
                    message=f"Project path does not exist: {project_path}",
                    fix_hint="Check the path in config.yaml or create the directory.",
                ))
                continue

            beads_dir = project_path / ".beads"
            if not beads_dir.exists():
                issues.append(PreflightIssue(
                    severity="warning",
                    message=f"No .beads/ directory in {project_path}.",
                    fix_hint=f"Run `bd init` inside {project_path} to initialise beads.",
                ))

    return issues


def has_errors(issues: list[PreflightIssue]) -> bool:
    """Return True if any issue has severity 'error'."""
    return any(i.severity == "error" for i in issues)


def format_issues_for_alert(issues: list[PreflightIssue]) -> str:
    """Format issues for a rumps.alert() dialog body."""
    lines: list[str] = []
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if errors:
        lines.append("ERRORS (must fix before agents can run):")
        for i in errors:
            lines.append(f"  • {i.message}")
            lines.append(f"    → {i.fix_hint}")
        lines.append("")

    if warnings:
        lines.append("WARNINGS:")
        for i in warnings:
            lines.append(f"  • {i.message}")
            lines.append(f"    → {i.fix_hint}")

    return "\n".join(lines)


def format_issues_for_cli(issues: list[PreflightIssue]) -> str:
    """Format issues for terminal output (naenae doctor)."""
    if not issues:
        return "✅ All checks passed."

    lines: list[str] = []
    for issue in issues:
        icon = "❌" if issue.severity == "error" else "⚠️ "
        lines.append(f"{icon}  {issue.message}")
        lines.append(f"    Fix: {issue.fix_hint}")
    return "\n".join(lines)
