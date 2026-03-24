"""Penny — native macOS first-run onboarding wizard.

Uses NSAlert and NSOpenPanel so the experience matches standard macOS conventions
(Apple Human Interface Guidelines) rather than showing raw error messages.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

try:
    from AppKit import (
        NSAlert,
        NSAlertFirstButtonReturn,
        NSAlertSecondButtonReturn,
        NSAlertThirdButtonReturn,  # noqa: F401
        NSApp,
        NSOpenPanel,
    )
    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False

_DEFAULT_SCOPED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(git:*)",
]


def _get_app_icon():
    """Load the Penny icon for use in dialogs. Cached after first call."""
    if not _HAS_APPKIT:
        return None
    if not hasattr(_get_app_icon, "_cached"):
        from .app import _load_app_icon
        _get_app_icon._cached = _load_app_icon()
    return _get_app_icon._cached


def _set_alert_icon(alert) -> None:
    """Set the Penny icon on an NSAlert if available."""
    icon = _get_app_icon()
    if icon:
        alert.setIcon_(icon)


def needs_onboarding(config: dict[str, Any]) -> bool:
    """Return True if first-run setup is required (no real projects configured)."""
    projects = config.get("projects", [])
    if not projects:
        return True
    return any("PLACEHOLDER" in str(p.get("path", "")) for p in projects)


def _bring_to_front() -> None:
    """Activate the app so dialogs receive keyboard focus."""
    if _HAS_APPKIT:
        NSApp.activateIgnoringOtherApps_(True)


def _pick_directory() -> Path | None:
    """Show a native directory picker. Returns the chosen Path or None."""
    panel = NSOpenPanel.openPanel()
    panel.setTitle_("Select Project Folder")
    panel.setMessage_("Choose your project folder.")
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setPrompt_("Select Folder")
    _bring_to_front()
    if panel.runModal() == 1:   # NSModalResponseOK
        url = panel.URL()
        if url:
            return Path(url.path())
    return None


def _ask_agent_permissions(extra_tools: list[str] | None = None) -> tuple[str, list[str]]:
    """Show an NSAlert asking the user to choose an agent_permissions mode.

    Returns (mode, allowed_tools) where mode is 'off' | 'scoped' | 'full'
    and allowed_tools is a list (empty unless mode is 'scoped').

    Defaults to 'off' (safest) if AppKit is unavailable.
    extra_tools are appended to the scoped allowed list when mode is 'scoped'.
    """
    if not _HAS_APPKIT:
        return "off", []

    alert = NSAlert.alloc().init()
    _set_alert_icon(alert)
    alert.setMessageText_("Agent Permission Mode")
    alert.setInformativeText_(
        "Choose how much access Claude agents have when working on your projects.\n\n"
        "\u2022 Off \u2014 Monitoring only. Penny tracks capacity but never spawns agents.\n\n"
        "\u2022 Scoped \u2014 Agents may only use a limited set of tools (read, edit, git). "
        "No arbitrary shell commands.\n\n"
        "\u2022 Full \u2014 Agents run with --dangerously-skip-permissions. They can read, "
        "write, delete files, run any shell command, commit code, and open pull requests "
        "without confirmation. Use only if you trust the tasks fully."
    )
    # First button = default (blue, Enter key). Off is the safest default.
    alert.addButtonWithTitle_("Off \u2014 Monitoring Only")
    alert.addButtonWithTitle_("Scoped \u2014 Limited Tools")
    alert.addButtonWithTitle_("Full \u2014 No Restrictions")
    _bring_to_front()
    response = alert.runModal()

    if response == NSAlertSecondButtonReturn:
        scoped = list(_DEFAULT_SCOPED_TOOLS)
        if extra_tools:
            scoped.extend(extra_tools)
        return "scoped", scoped
    if response == NSAlertThirdButtonReturn:
        return "full", []
    return "off", []


def check_full_permissions_consent(config: dict[str, Any], state: dict[str, Any]) -> bool:
    """Return True (and record consent) if the user confirms full-permission mode.

    Shows a one-time confirmation when agent_permissions transitions to 'full'
    without prior consent recorded in state.json.

    Returns True if the user consented or consent was already given.
    Returns False if the user declined (caller should revert mode).
    """
    mode = config.get("work", {}).get("agent_permissions", "full")
    if mode != "full":
        return True

    consent = state.get("agent_permissions_consent", {})
    if consent.get("mode") == "full" and consent.get("given"):
        return True   # already consented

    if not _HAS_APPKIT:
        return True   # non-GUI context — assume ok

    from datetime import datetime, timezone

    alert = NSAlert.alloc().init()
    _set_alert_icon(alert)
    alert.setMessageText_("Enable Full Agent Permissions?")
    alert.setInformativeText_(
        "agent_permissions is set to \u201cfull\u201d in config.yaml.\n\n"
        "This means Claude agents will run with --dangerously-skip-permissions and "
        "can read, write, delete files, run shell commands, commit code, and open "
        "pull requests \u2014 all without asking for confirmation.\n\n"
        "Only proceed if you have reviewed the pending tasks and trust them fully."
    )
    alert.addButtonWithTitle_("Understood \u2014 Enable Full Mode")
    alert.addButtonWithTitle_("Cancel \u2014 Keep Off")
    _bring_to_front()
    if alert.runModal() == NSAlertFirstButtonReturn:
        state["agent_permissions_consent"] = {
            "given": True,
            "mode": "full",
            "date": datetime.now(timezone.utc).isoformat(),
        }
        return True

    # User declined — record this so the dialog doesn't reappear on every restart.
    # The caller reverts agent_permissions to "off" in config.yaml.
    state["agent_permissions_consent"] = {
        "given": False,
        "mode": "full",
        "date": datetime.now(timezone.utc).isoformat(),
    }
    return False


def run_onboarding(
    config_path: Path,
    config: dict[str, Any],
    plugin_manager: Any = None,
) -> dict[str, Any] | None:
    """Show the native onboarding wizard.

    Walks the user through adding project folders via system dialogs.
    Writes the updated config to *config_path* on completion.

    Returns the updated config dict, or None if the user chose "Set Up Later".
    """
    if not _HAS_APPKIT:
        return None

    # ── Welcome ───────────────────────────────────────────────────────────
    welcome = NSAlert.alloc().init()
    _set_alert_icon(welcome)
    welcome.setMessageText_("Welcome to Penny")
    welcome.setInformativeText_(
        "Penny monitors your Claude Pro or Max usage and helps you stay within "
        "your weekly token budget.\n\n"
        "No setup needed \u2014 Penny reads your Claude Code stats automatically."
    )
    welcome.addButtonWithTitle_("Add Project Folder\u2026")   # default (blue)
    welcome.addButtonWithTitle_("Set Up Later")
    _bring_to_front()
    if welcome.runModal() != NSAlertFirstButtonReturn:
        return None     # user deferred — caller should record this in state

    # ── Collect project folders ───────────────────────────────────────────
    collected: list[dict[str, Any]] = []

    while True:
        path = _pick_directory()
        if path is None:
            break

        if plugin_manager is not None:
            plugin_manager.setup_projects([path])

        collected.append({"path": str(path), "priority": len(collected) + 1})

        # Offer to add another
        another = NSAlert.alloc().init()
        _set_alert_icon(another)
        another.setMessageText_(f"\u201c{path.name}\u201d Added")
        another.setInformativeText_("Would you like to add another project folder?")
        another.addButtonWithTitle_("Add Another Folder\u2026")   # default
        another.addButtonWithTitle_("Done")
        _bring_to_front()
        if another.runModal() != NSAlertFirstButtonReturn:
            break

    if not collected:
        return None

    # ── Agent permission mode ─────────────────────────────────────────────
    extra = plugin_manager.get_default_allowed_tools() if plugin_manager is not None else []
    perm_mode, allowed_tools = _ask_agent_permissions(extra_tools=extra)

    # ── Write config ──────────────────────────────────────────────────────
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config_with_comments(config_path, collected, config, perm_mode, allowed_tools)

    updated = dict(config)
    updated["projects"] = collected
    updated.setdefault("work", {})["agent_permissions"] = perm_mode
    if perm_mode == "scoped":
        updated["work"]["allowed_tools"] = allowed_tools
    return updated


def _write_config_with_comments(
    config_path: Path,
    projects: list[dict[str, Any]],
    existing_config: dict[str, Any],
    agent_permissions: str = "off",
    allowed_tools: list[str] | None = None,
) -> None:
    """Write config preserving template comments.

    Finds config.yaml.template, replaces only the ``projects:`` block, and
    writes the result — so all inline docs and checklist comments survive.
    Falls back to yaml.dump if the template cannot be found.
    """
    # Build the projects YAML block
    project_lines = ["projects:"]
    for proj in projects:
        project_lines.append(f"  - path: {proj['path']}")
        project_lines.append(f"    priority: {proj['priority']}")
    projects_yaml = "\n".join(project_lines)

    # Search for the template in known locations
    template_candidates = [
        config_path.parent / "config.yaml.template",
        Path(__file__).parent.parent / "config.yaml.template",
    ]
    template_text: str | None = None
    for candidate in template_candidates:
        if candidate.exists():
            template_text = candidate.read_text(encoding="utf-8")
            break

    if template_text is not None:
        # Replace the ``projects:`` block — handles both active and commented-out forms
        new_text = re.sub(
            r"(?m)^projects:.*?(?=^\w|\Z)",
            projects_yaml + "\n\n",
            template_text,
            count=1,
            flags=re.DOTALL,
        )
        if new_text == template_text:
            # Template has projects commented out — replace the comment block
            new_text = re.sub(
                r"(?m)^# projects:.*?(?=^[^#\n]|\Z)",
                projects_yaml + "\n\n",
                template_text,
                count=1,
                flags=re.DOTALL,
            )
        # Update agent_permissions in the work: section
        new_text = re.sub(
            r'(agent_permissions:\s*)"[^"]*"',
            f'\\1"{agent_permissions}"',
            new_text,
        )
        if agent_permissions == "scoped" and allowed_tools:
            # Uncomment the allowed_tools block if it's present as comments
            new_text = re.sub(
                r"  # allowed_tools:.*?(?=\n\w|\Z)",
                "  allowed_tools:\n" + "\n".join(f"    - {t}" for t in allowed_tools) + "\n",
                new_text,
                flags=re.DOTALL,
            )
        config_path.write_text(new_text, encoding="utf-8")
    else:
        # Fallback: yaml.dump (loses comments but stays correct)
        updated = dict(existing_config)
        updated["projects"] = projects
        updated.setdefault("work", {})["agent_permissions"] = agent_permissions
        if agent_permissions == "scoped" and allowed_tools:
            updated["work"]["allowed_tools"] = allowed_tools
        with config_path.open("w") as fh:
            yaml.dump(updated, fh, default_flow_style=False, allow_unicode=True)
