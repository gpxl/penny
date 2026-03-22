"""Loadout plugin — automated skill management via the loadout CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..plugin import PennyPlugin
from ..preflight import PreflightIssue
from ..tasks import Task

# Manifest files checked for code project detection (subset of what loadout tracks)
_CODE_PROJECT_MARKERS = {
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "CMakeLists.txt",
    "setup.py",
    "setup.cfg",
    "Gemfile",
    "composer.json",
    "build.gradle",
    "pom.xml",
    "mix.exs",
    "deno.json",
}

_LOADOUT_AGENT_PROMPT = """\
You are a background agent managing Claude Code skills for the project at {project_path}.

Task {task_id}: {task_title}

Your goal is to ensure this project has the right Claude Code skills installed
via the loadout CLI tool.

Steps:
1. Run: loadout scan --json {project_path}
2. Parse the JSON output. Focus on recommendations with tier "essential" or "recommended".
3. For each recommended skill, install it:
   loadout install <source> -s <name> -y
4. Verify with: loadout status {project_path}
5. If the project already has good coverage (no essential/recommended skills missing),
   report that no changes were needed.

Rules:
- Only install skills with auditRisk "safe", "low", or "medium"
- Skip skills where compatible is false
- Do NOT install "optional" tier skills automatically
- Do NOT remove existing skills
- Report what you installed and why in your final output

{task_description}
"""

_STATUS_SUBPROCESS_TIMEOUT = 15  # seconds

# Common locations for pnpm/npm global binaries and node version managers.
# Checked when shutil.which() fails (e.g., launchd's minimal PATH).
_EXTRA_BIN_DIRS = [
    Path.home() / "Library" / "pnpm",  # pnpm global bin (macOS)
    Path.home() / ".local" / "share" / "pnpm",  # pnpm global bin (Linux)
    Path.home() / ".npm-global" / "bin",  # npm global prefix
    Path("/usr/local/bin"),
]

def _find_node_dirs() -> list[Path]:
    """Discover directories that may contain the ``node`` binary.

    Checks nvm, fnm, volta, and homebrew install locations so the plugin
    can build a subprocess PATH that lets loadout's shim find ``node``.
    """
    dirs: list[Path] = []
    # nvm
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        dirs.extend(sorted(nvm_root.glob("v*/bin"), reverse=True))
    # fnm
    fnm_root = Path.home() / ".local" / "share" / "fnm" / "node-versions"
    if fnm_root.is_dir():
        dirs.extend(sorted(fnm_root.glob("v*/installation/bin"), reverse=True))
    # volta
    dirs.append(Path.home() / ".volta" / "bin")
    # homebrew
    dirs.append(Path("/opt/homebrew/bin"))
    dirs.append(Path("/usr/local/bin"))
    return dirs


def _find_loadout() -> str | None:
    """Locate the loadout binary, checking common install locations beyond PATH."""
    found = shutil.which("loadout")
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        candidate = d / "loadout"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _build_subprocess_env() -> dict[str, str]:
    """Build an environment for loadout subprocesses.

    loadout is a Node.js CLI — its shim calls ``exec node ...``.
    Under launchd, ``node`` may not be in PATH (nvm, fnm, volta installs).
    We augment PATH with known node binary directories so the shim can find it.
    """
    env = dict(os.environ)
    current_path = env.get("PATH", "")
    extra: list[str] = []
    for d in _find_node_dirs():
        ds = str(d)
        if d.is_dir() and ds not in current_path:
            extra.append(ds)
    if extra:
        env["PATH"] = ":".join(extra) + ":" + current_path
    return env


def _query_loadout_status(project_path: str) -> dict[str, Any] | None:
    """Call ``loadout status --json <path>`` and return parsed JSON, or None on failure."""
    loadout = _find_loadout()
    if loadout is None:
        return None
    try:
        result = subprocess.run(
            [loadout, "status", "--json", project_path],
            capture_output=True,
            text=True,
            timeout=_STATUS_SUBPROCESS_TIMEOUT,
            env=_build_subprocess_env(),
        )
        if result.returncode != 0:
            print(f"[penny:loadout] status exited {result.returncode}: {result.stderr.strip()}", flush=True)
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"[penny:loadout] status timed out for {project_path}", flush=True)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[penny:loadout] status error: {exc}", flush=True)
        return None


def _needs_scan(cached: dict[str, Any], config: dict[str, Any]) -> bool:
    """Determine if a project needs a skill scan based on cached loadout status."""
    scan = cached.get("status", {}).get("scan", {})

    # Trigger 1: loadout reports stale (manifest checksums changed)
    if scan.get("stale") is True:
        return True

    # Trigger 2: never scanned or scan too old
    last_scan_at = scan.get("lastScanAt")
    if last_scan_at is None:
        return True

    interval_days = config.get("scan_interval_days", 14)
    try:
        last_dt = datetime.fromisoformat(last_scan_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - last_dt
        if age.days >= interval_days:
            return True
    except (ValueError, TypeError):
        return True

    return False


class Plugin(PennyPlugin):
    """Automated skill management via the loadout CLI."""

    def __init__(self) -> None:
        self._app: Any = None
        self._projects: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "loadout"

    @property
    def description(self) -> str:
        return "Automated skill management via loadout CLI"

    def is_available(self) -> bool:
        return _find_loadout() is not None

    def config_schema(self) -> dict[str, Any]:
        return {
            "scan_interval_days": 14,
            "auto_install_tiers": ["essential"],
            "exclude_projects": [],
        }

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        if _find_loadout() is None:
            return [
                PreflightIssue(
                    severity="warning",
                    message="loadout CLI not found in PATH",
                    fix_hint="Install loadout: curl -fsSL https://raw.githubusercontent.com/gpxl/loadout/main/install.sh | bash",
                )
            ]
        return []

    def on_activate(self, app: Any) -> None:
        self._app = app
        # Populate initial skill state for all projects.
        # Re-query projects whose cached status is empty (previous query failed).
        cache = self._get_project_cache()
        projects = self._get_projects()
        for proj in projects:
            path = proj.get("path", "")
            if not path:
                continue
            cached = cache.get(path)
            if cached is None or not cached.get("status"):
                self._refresh_project(path)

    def on_first_activated(self, app: Any) -> None:
        print("[penny:loadout] Loadout plugin activated — skill management enabled", flush=True)

    def on_deactivate(self) -> None:
        self._app = None

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        config = self._plugin_config()
        exclude = set(config.get("exclude_projects", []))
        cache = self._get_project_cache()
        tasks: list[Task] = []

        for proj in projects:
            path = proj.get("path", "")
            if not path or path in exclude:
                continue

            # Lazy initialization: query loadout if not yet cached
            if path not in cache:
                self._refresh_project(path)

            cached = cache.get(path, {})
            if cached.get("scan_in_progress"):
                continue

            if _needs_scan(cached, config):
                proj_name = proj.get("name", path.rsplit("/", 1)[-1])
                tasks.append(
                    Task(
                        task_id=f"loadout-scan-{proj_name}",
                        title=f"Scan and update skills for {proj_name}",
                        priority="P3",
                        project_path=path,
                        project_name=proj_name,
                        metadata={"plugin": "loadout", "project_path": path},
                    )
                )

        return tasks

    def get_task_description(self, task: Task) -> str | None:
        if task.metadata.get("plugin") != "loadout":
            return None
        path = task.metadata.get("project_path", task.project_path)
        cache = self._get_project_cache()
        cached = cache.get(path, {})
        status = cached.get("status", {})
        skills = status.get("skills", [])
        scan = status.get("scan", {})

        lines = [f"Project: {path}"]
        lines.append(f"Currently installed: {len(skills)} skill(s)")
        if scan.get("stale"):
            lines.append("Status: STALE — dependency manifests changed since last scan")
        elif scan.get("lastScanAt") is None:
            lines.append("Status: NEVER SCANNED")
        else:
            lines.append(f"Last scan: {scan['lastScanAt']}")
        return "\n".join(lines)

    def get_agent_prompt_template(self) -> str | None:
        return _LOADOUT_AGENT_PROMPT

    def on_agent_spawned(self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        if task.metadata.get("plugin") != "loadout":
            return
        path = task.metadata.get("project_path", task.project_path)
        projects = plugin_state.setdefault("projects", {})
        proj = projects.setdefault(path, {})
        proj["scan_in_progress"] = True

    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        # Refresh status for any project that had a scan in progress
        projects = plugin_state.get("projects", {})
        for path, proj in projects.items():
            if proj.get("scan_in_progress"):
                proj["scan_in_progress"] = False
                status = _query_loadout_status(path)
                if status is not None:
                    proj["status"] = status

    def dashboard_card_html(self, state: dict[str, Any], config: dict[str, Any]) -> str | None:
        cache = state.get("plugin_state", {}).get("loadout", {}).get("projects", {})
        if not cache:
            return "<p>No projects tracked yet.</p>"

        rows: list[str] = []
        for path, data in cache.items():
            proj_name = path.rsplit("/", 1)[-1]
            status = data.get("status", {})
            skills = status.get("skills", [])
            scan = status.get("scan", {})

            skill_count = len(skills)
            last_scan = scan.get("lastScanAt", "never")
            stale = scan.get("stale")

            if last_scan == "never" or last_scan is None:
                badge = '<span style="color:#e74c3c">never</span>'
            elif stale:
                badge = '<span style="color:#f39c12">stale</span>'
            else:
                badge = '<span style="color:#2ecc71">fresh</span>'

            rows.append(
                f"<tr><td>{proj_name}</td><td>{skill_count}</td>"
                f"<td>{last_scan if last_scan and last_scan != 'never' else '—'}</td>"
                f"<td>{badge}</td></tr>"
            )

        return (
            "<h3>Skill Coverage</h3>"
            "<table><thead><tr><th>Project</th><th>Skills</th>"
            "<th>Last Scan</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    def dashboard_api_handler(
        self, method: str, path_suffix: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if path_suffix == "status" and method == "GET":
            cache = self._get_project_cache()
            return {"projects": cache}
        return None

    def cli_commands(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "loadout-status",
                "description": "Show skill coverage for all monitored projects",
                "api_path": "status",
                "method": "GET",
            },
        ]

    # ── Private helpers ──────────────────────────────────────────────────

    def _plugin_config(self) -> dict[str, Any]:
        """Get merged plugin config (defaults + user overrides)."""
        defaults = self.config_schema()
        if self._app is None:
            return defaults
        try:
            cfg = self._app.config.get("plugins", {}).get("loadout", {})
            if isinstance(cfg, bool):
                return defaults
            merged = dict(defaults)
            merged.update(cfg)
            return merged
        except (AttributeError, TypeError):
            return defaults

    def _get_projects(self) -> list[dict[str, Any]]:
        """Get project list from app config."""
        if self._app is None:
            return []
        try:
            return self._app.config.get("projects", [])
        except (AttributeError, TypeError):
            return []

    def _get_project_cache(self) -> dict[str, Any]:
        """Get the mutable project cache from app state."""
        if self._app is None:
            return {}
        try:
            state = self._app.state
            ps = state.setdefault("plugin_state", {})
            loadout = ps.setdefault("loadout", {})
            return loadout.setdefault("projects", {})
        except (AttributeError, TypeError):
            return {}

    def _refresh_project(self, path: str) -> None:
        """Query loadout and cache the result for a project."""
        cache = self._get_project_cache()
        status = _query_loadout_status(path)
        if status is not None:
            cache[path] = {"status": status, "scan_in_progress": False}
        else:
            # Still record the project so we don't re-query every cycle
            cache.setdefault(path, {"status": {}, "scan_in_progress": False})
