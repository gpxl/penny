"""Beads integration plugin for Penny.

Provides task discovery, agent prompts, preflight checks, and UI sections
for projects using the Beads issue tracker (bd CLI).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import objc
from AppKit import (
    NSAttributedString,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableAttributedString,
    NSScrollView,
    NSStackView,
    NSTextView,
)
from Foundation import NSObject

from ..plugin import PennyPlugin, UISection
from ..preflight import PreflightIssue
from ..tasks import Task
from ..ui_components import make_button, make_label

# ── Layout constants (must match popover_vc._WIDTH/_PADDING) ───────────────

_WIDTH: float = 380.0
_PADDING: float = 16.0
_PAGE_SIZE: int = 5
_TASK_LIMIT: int = 20

# ── Markdown helpers (copied from popover_vc, used for task descriptions) ──

_SECTION_HDR_RE = re.compile(r"^[A-Z][A-Z_\s]{3,}$")
_INLINE_MARKUP_RE = re.compile(r"\*\*(.+?)\*\*|`(.+?)`")


def _extract_description(bd_show_output: str) -> str:
    """Strip bd show header lines; return from first ALL-CAPS section header onward."""
    lines = bd_show_output.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and _SECTION_HDR_RE.match(stripped):
            return "\n".join(lines[i:]).strip()
    return bd_show_output.strip()


def _markdown_to_attrstr(text: str) -> Any:
    """Convert **bold**, `code`, ALL-CAPS headers, and bullets to NSMutableAttributedString."""
    body_font = NSFont.systemFontOfSize_(12.0)
    bold_font = NSFont.boldSystemFontOfSize_(12.0)
    code_font = NSFont.userFixedPitchFontOfSize_(11.0)
    hdr_font = NSFont.boldSystemFontOfSize_(11.0)
    _body_color = NSColor.labelColor()
    dim_color = NSColor.secondaryLabelColor()

    result = NSMutableAttributedString.alloc().init()

    def _append(txt: str, font: Any, color: Any = None) -> None:
        attrs: dict = {NSFontAttributeName: font}
        if color is not None:
            attrs[NSForegroundColorAttributeName] = color
        result.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(txt, attrs)
        )

    def _append_inline(line: str, nl: str) -> None:
        last = 0
        for m in _INLINE_MARKUP_RE.finditer(line):
            if m.start() > last:
                _append(line[last:m.start()], body_font)
            if m.group(1) is not None:   # **bold**
                _append(m.group(1), bold_font)
            else:                         # `code`
                _append(m.group(2), code_font)
            last = m.end()
        if last < len(line):
            _append(line[last:], body_font)
        if nl:
            _append(nl, body_font)

    for raw_line in text.splitlines(True):
        line = raw_line.rstrip("\n\r")
        nl = "\n" if len(raw_line) > len(line) else ""
        stripped = line.strip()

        if stripped and _SECTION_HDR_RE.match(stripped):
            _append(line + nl, hdr_font, dim_color)
            continue

        bullet_m = re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if bullet_m:
            _append(bullet_m.group(1) + "• ", body_font)
            _append_inline(bullet_m.group(2), nl)
            continue

        _append_inline(line, nl)

    return result


def _make_desc_scroll_view(text: str) -> Any:
    """Return a width-constrained NSScrollView containing a markdown-rendered NSTextView."""
    inner_w = _WIDTH - _PADDING * 2

    tv = NSTextView.alloc().initWithFrame_(((0, 0), (inner_w, 400.0)))
    tv.textStorage().setAttributedString_(_markdown_to_attrstr(text))
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setDrawsBackground_(False)
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.textContainer().setWidthTracksTextView_(True)
    tv.textContainer().setContainerSize_((inner_w, 1e7))

    lm = tv.layoutManager()
    lm.ensureLayoutForTextContainer_(tv.textContainer())
    used = lm.usedRectForTextContainer_(tv.textContainer())
    content_h = max(min(used.size.height + 8.0, 160.0), 40.0)

    sv = NSScrollView.alloc().initWithFrame_(((0, 0), (inner_w, content_h)))
    sv.setDocumentView_(tv)
    sv.setHasVerticalScroller_(True)
    sv.setHasHorizontalScroller_(False)
    sv.setAutohidesScrollers_(True)
    sv.setDrawsBackground_(False)
    sv.setTranslatesAutoresizingMaskIntoConstraints_(False)
    sv.widthAnchor().constraintEqualToConstant_(inner_w).setActive_(True)
    sv.heightAnchor().constraintEqualToConstant_(content_h).setActive_(True)
    return sv


def _make_inner_stack() -> Any:
    """Return a plain vertical NSStackView for a paginated section."""
    inner = NSStackView.alloc().init()
    inner.setOrientation_(1)
    inner.setAlignment_(5)   # NSLayoutAttributeLeading
    inner.setSpacing_(4.0)
    inner.setDistribution_(0)
    inner.setTranslatesAutoresizingMaskIntoConstraints_(False)
    inner.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)
    return inner


def _update_pagination_nav(
    nav_row: Any,
    prev_btn: Any,
    next_btn: Any,
    page_lbl: Any,
    page: int,
    total_pages: int,
) -> None:
    """Refresh pagination nav controls for a section."""
    if nav_row is None:
        return
    if total_pages <= 1:
        nav_row.setHidden_(True)
        return
    nav_row.setHidden_(False)
    page_lbl.setStringValue_(f"{page + 1} / {total_pages}")
    prev_btn.setEnabled_(page > 0)
    next_btn.setEnabled_(page < total_pages - 1)


AGENT_PROMPT_TEMPLATE = """\
You are a background agent working on the project at {project_path}.

Task {task_id}: {task_title}
Priority: {priority}

Full task description:
{task_description}

Instructions (follow exactly):
1. Run: bd prime  (understand full project context)
2. Run: bd update {task_id} --status=in_progress
3. Create a git branch for this task: git checkout -b agent/{task_id}
4. Implement the solution following project conventions (TDD: write tests first, then implement)
5. Run all project tests and fix any failures
6. Run lint and fix all warnings (code is not complete until lint passes)
7. Stage and commit with a descriptive message: git add <files> && git commit -m "..."
8. Push the branch: git push -u origin agent/{task_id}
9. Open a pull request: gh pr create --title "<task title>" --body "<summary of changes>"
10. Run: bd close {task_id}
11. Run: bd sync --flush-only

Work autonomously. Do not ask for confirmation. Complete the full task end-to-end.
"""


def _resolve_bd() -> str:
    """Return the absolute path to the bd binary.

    When Penny is launched as a GUI app (e.g. via Spotlight or Finder) macOS
    provides a minimal PATH that typically omits ~/.local/bin and
    /opt/homebrew/bin.  shutil.which() searches the current process PATH, so
    we fall back to the canonical install locations used by the bd installer.
    """
    found = shutil.which("bd")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "bd",
        Path("/opt/homebrew/bin/bd"),
        Path("/usr/local/bin/bd"),
    ):
        if candidate.exists():
            return str(candidate)
    return "bd"  # will raise FileNotFoundError at call time, caught below


_BD_BIN: str = _resolve_bd()
print(f"[beads] bd binary resolved to {_BD_BIN!r}", flush=True)


def _run_bd(args: list[str], cwd: str) -> str:
    """Run a bd command in a given directory and return stdout."""
    try:
        env = os.environ.copy()
        env["BEADS_DIR"] = str(Path(cwd) / ".beads")
        result = subprocess.run(
            [_BD_BIN] + args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 and result.stderr:
            print(
                f"[beads] bd {args} in {cwd!r} (rc={result.returncode}): {result.stderr.strip()}",
                flush=True,
            )
        return result.stdout
    except FileNotFoundError:
        print(f"[beads] bd binary not found (tried {_BD_BIN!r})", flush=True)
        return ""
    except subprocess.TimeoutExpired:
        return ""


def _parse_bd_ready(output: str, project_path: str) -> list[Task]:
    """Parse `bd ready` output into Task objects."""
    tasks = []
    project_name = Path(project_path).name

    pattern = re.compile(
        r"\d+\.\s+\[.*?\]\s+\[.*?\]\s+([\w-]+):\s+(.+)"
    )
    priority_pattern = re.compile(r"\[\S*\s*(P\d)\]")

    for line in output.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        task_id = m.group(1).strip()
        title = m.group(2).strip()

        pm = priority_pattern.search(line)
        priority = pm.group(1) if pm else "P3"

        tasks.append(
            Task(
                task_id=task_id,
                title=title,
                priority=priority,
                project_path=project_path,
                project_name=project_name,
                raw_line=line.strip(),
            )
        )
    return tasks


def _parse_bd_list(output: str, project_path: str) -> list[Task]:
    """Parse `bd list --status=closed` output into Task objects.

    Format: ✓ <id> [P<n>] [<type>] - <title>
    """
    tasks = []
    project_name = Path(project_path).name
    pattern = re.compile(r"✓\s+([\w-]+)\s+\[(P\d)\]\s+\[.*?\]\s+-\s+(.+)")
    for line in output.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        tasks.append(
            Task(
                task_id=m.group(1).strip(),
                title=m.group(3).strip(),
                priority=m.group(2),
                project_path=project_path,
                project_name=project_name,
                raw_line=line.strip(),
            )
        )
    return tasks


# ── Beads UI Controller ────────────────────────────────────────────────────

class BeadsUIController(NSObject):
    """NSObject target for Beads UI button actions.

    Holds all task/agent/completed section state and UI rebuild methods.
    Button actions on beads-contributed UI views target this object.
    """

    def init(self) -> BeadsUIController:
        self = objc.super(BeadsUIController, self).init()
        if self is None:
            return self

        self._plugin: Any = None
        self._app: Any = None

        # Tasks section state
        self._tasks_stack: Any = None
        self._task_views: list = []
        self._tasks_page: int = 0
        self._tasks_total_pages: int = 1
        self._tasks_nav_row: Any = None
        self._tasks_prev_btn: Any = None
        self._tasks_next_btn: Any = None
        self._tasks_page_lbl: Any = None
        self._tasks_header_lbl: Any = None
        self._expanded_task_id: str | None = None
        self._latest_tasks: list = []
        self._latest_agents: list = []
        self._latest_completed: list = []
        self._pending_task_ids: set = set()

        # Agents section state
        self._agents_stack: Any = None
        self._agent_views: list = []
        self._agents_header_lbl: Any = None
        self._agents_page: int = 0
        self._agents_total_pages: int = 1
        self._agents_nav_row: Any = None
        self._agents_prev_btn: Any = None
        self._agents_next_btn: Any = None
        self._agents_page_lbl: Any = None

        # Completed section state
        self._completed_stack: Any = None
        self._completed_views: list = []
        self._completed_header_row: Any = None
        self._completed_outer: Any = None
        self._completed_nav_row: Any = None
        self._completed_prev_btn: Any = None
        self._completed_next_btn: Any = None
        self._completed_page_lbl: Any = None
        self._completed_page: int = 0
        self._completed_total_pages: int = 1

        return self

    # ── ObjC action selectors — immediate UI rebuilds ──────────────────────

    def _toggleTask_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if not task_id:
            return
        if self._expanded_task_id == task_id:
            self._expanded_task_id = None
        else:
            self._expanded_task_id = task_id
            task = next((t for t in self._latest_tasks if t.task_id == task_id), None)
            if task and not getattr(task, "_cached_desc", None):
                mgr = getattr(self._app, "_plugin_mgr", None)
                if mgr:
                    task._cached_desc = mgr.get_task_description(task)
                else:
                    task._cached_desc = task.title
        self._rebuild_tasks_section(self._latest_tasks, self._latest_agents)
        self._relayout()

    def _tasksPrev_(self, sender: Any) -> None:
        self._tasks_page = max(0, self._tasks_page - 1)
        self._rebuild_tasks_section(self._latest_tasks, self._latest_agents)
        self._relayout()

    def _tasksNext_(self, sender: Any) -> None:
        self._tasks_page = min(self._tasks_total_pages - 1, self._tasks_page + 1)
        self._rebuild_tasks_section(self._latest_tasks, self._latest_agents)
        self._relayout()

    def _agentsPrev_(self, sender: Any) -> None:
        self._agents_page = max(0, self._agents_page - 1)
        self._rebuild_agents_section(self._latest_agents)
        self._relayout()

    def _agentsNext_(self, sender: Any) -> None:
        self._agents_page = min(self._agents_total_pages - 1, self._agents_page + 1)
        self._rebuild_agents_section(self._latest_agents)
        self._relayout()

    def _completedPrev_(self, sender: Any) -> None:
        self._completed_page = max(0, self._completed_page - 1)
        self._rebuild_completed_section(self._latest_completed)
        self._relayout()

    def _completedNext_(self, sender: Any) -> None:
        self._completed_page = min(self._completed_total_pages - 1, self._completed_page + 1)
        self._rebuild_completed_section(self._latest_completed)
        self._relayout()

    # ── ObjC action selectors — data-mutating, forwarded through app ───────

    def _runTask_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        task = next((t for t in self._latest_tasks if t.task_id == task_id), None)
        if task and self._app:
            # Mark as pending — row stays visible showing "Launching…" until
            # the next data refresh confirms the agent is running.
            self._pending_task_ids.add(task_id)
            self._rebuild_tasks_section(self._latest_tasks, self._latest_agents)
            self._relayout()
            self._app.spawnTask_(task)

    def _stopAgent_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if not task_id:
            return
        sender.setTitle_("Stopping\u2026")
        sender.setEnabled_(False)
        if self._app:
            self._app.stopAgentByTaskId_(task_id)

    def _controlAgent_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if not task_id or not self._app:
            return
        agent = next(
            (a for a in self._latest_agents if a.get("task_id") == task_id), None
        )
        if agent is None:
            return
        session = agent.get("session", "")
        if not session:
            return
        tmux_bin = agent.get("tmux_bin") or "/opt/homebrew/bin/tmux"
        if subprocess.run(
            [tmux_bin, "has-session", "-t", session], capture_output=True
        ).returncode == 0:
            attach_cmd = shlex.join([tmux_bin, "attach-session", "-t", session])
        else:
            attach_cmd = shlex.join(["screen", "-x", session])
        script_content = f"#!/bin/sh\n{attach_cmd}\n"
        fd, tmp_path = tempfile.mkstemp(suffix=".command")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script_content)
            os.chmod(tmp_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
            subprocess.Popen(["open", tmp_path], start_new_session=True)
        except Exception as exc:
            print(f"[penny] BeadsUIController _controlAgent_ failed: {exc}", flush=True)

    def _dismissCompleted_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id and self._app:
            self._app.dismissCompleted_(task_id)

    def _clearAllCompleted_(self, sender: Any) -> None:
        if self._app:
            self._app.clearAllCompleted_(None)

    # ── Internal helpers ───────────────────────────────────────────────────

    @objc.python_method
    def _relayout(self) -> None:
        if self._app and hasattr(self._app, "_vc") and self._app._vc:
            self._app._vc._relayout()

    # ── View rebuild methods ───────────────────────────────────────────────

    @objc.python_method
    def _rebuild_tasks_section(self, tasks: list, agents_running: list) -> None:
        if self._tasks_stack is None:
            return
        for v in self._task_views:
            v.removeFromSuperview()
        self._task_views = []

        running_ids = {a.get("task_id") for a in agents_running}
        # Confirmed running → no longer pending
        self._pending_task_ids -= running_ids
        shown = [t for t in tasks if t.task_id not in running_ids][:_TASK_LIMIT]

        if not shown:
            placeholder = make_label("No ready tasks", size=12.0, secondary=True)
            placeholder.setAlignment_(2)  # NSTextAlignmentCenter
            self._tasks_stack.addArrangedSubview_(placeholder)
            self._task_views.append(placeholder)
            if self._tasks_header_lbl:
                self._tasks_header_lbl.setStringValue_("BEADS TASKS")
            if self._tasks_nav_row:
                self._tasks_nav_row.setHidden_(True)
            return

        ready_count = len(shown)
        if self._tasks_header_lbl:
            self._tasks_header_lbl.setStringValue_(f"BEADS TASKS ({ready_count} READY)")

        total_pages = max(1, (len(shown) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._tasks_total_pages = total_pages
        self._tasks_page = min(self._tasks_page, total_pages - 1)

        start = self._tasks_page * _PAGE_SIZE
        page_tasks = shown[start:start + _PAGE_SIZE]

        if self._expanded_task_id and not any(
            t.task_id == self._expanded_task_id for t in page_tasks
        ):
            self._expanded_task_id = None

        for task in page_tasks:
            is_expanded = (task.task_id == self._expanded_task_id)
            row_view = self._make_task_row(task, is_expanded, running_ids)
            self._tasks_stack.addArrangedSubview_(row_view)
            self._task_views.append(row_view)

        _update_pagination_nav(
            self._tasks_nav_row,
            self._tasks_prev_btn,
            self._tasks_next_btn,
            self._tasks_page_lbl,
            self._tasks_page,
            total_pages,
        )

    @objc.python_method
    def _rebuild_agents_section(self, agents: list) -> None:
        if self._agents_stack is None:
            return
        for v in self._agent_views:
            v.removeFromSuperview()
        self._agent_views = []

        hidden = not agents
        if self._agents_header_lbl:
            self._agents_header_lbl.setHidden_(hidden)
        self._agents_stack.setHidden_(hidden)

        if hidden:
            if self._agents_nav_row:
                self._agents_nav_row.setHidden_(True)
            return

        total_pages = max(1, (len(agents) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._agents_total_pages = total_pages
        self._agents_page = min(self._agents_page, total_pages - 1)

        start = self._agents_page * _PAGE_SIZE
        page_agents = agents[start:start + _PAGE_SIZE]

        for agent in page_agents:
            row = self._make_agent_row(agent)
            self._agents_stack.addArrangedSubview_(row)
            self._agent_views.append(row)

        _update_pagination_nav(
            self._agents_nav_row,
            self._agents_prev_btn,
            self._agents_next_btn,
            self._agents_page_lbl,
            self._agents_page,
            total_pages,
        )

    @objc.python_method
    def _rebuild_completed_section(self, completed: list) -> None:
        if self._completed_stack is None:
            return
        for v in self._completed_views:
            v.removeFromSuperview()
        self._completed_views = []

        hidden = not completed
        if self._completed_header_row:
            self._completed_header_row.setHidden_(hidden)
        if self._completed_stack:
            self._completed_stack.setHidden_(hidden)
        # Hide/show the entire outer view so _insert_plugin_sections
        # separators collapse properly when there's nothing to show.
        if self._completed_outer:
            self._completed_outer.setHidden_(hidden)

        if hidden:
            if self._completed_nav_row:
                self._completed_nav_row.setHidden_(True)
            return

        items = list(reversed(completed[-20:]))   # newest first

        total_pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._completed_total_pages = total_pages
        self._completed_page = min(self._completed_page, total_pages - 1)

        start = self._completed_page * _PAGE_SIZE
        page_items = items[start:start + _PAGE_SIZE]

        for agent in page_items:
            row = self._make_completed_row(agent)
            self._completed_stack.addArrangedSubview_(row)
            self._completed_views.append(row)

        _update_pagination_nav(
            self._completed_nav_row,
            self._completed_prev_btn,
            self._completed_next_btn,
            self._completed_page_lbl,
            self._completed_page,
            total_pages,
        )

    @objc.python_method
    def _make_task_row(self, task: Any, expanded: bool, running_ids: set) -> Any:
        """Build one task row (collapsed or expanded).

        Layout:
          Row 1 (horizontal): ● dot + "id · project  Px" (stretchy) + ▶ Run button
          Row 2: Full title in bold (clickable to expand)
          Expanded: description scroll view
        """
        inner_w = _WIDTH - _PADDING * 2
        container = NSStackView.alloc().init()
        container.setOrientation_(1)
        container.setAlignment_(5)  # NSLayoutAttributeLeading
        container.setSpacing_(3.0)
        container.setTranslatesAutoresizingMaskIntoConstraints_(False)
        container.widthAnchor().constraintEqualToConstant_(inner_w).setActive_(True)

        is_running = task.task_id in running_ids
        is_pending = task.task_id in self._pending_task_ids

        # ── Row 1: "● id · project  Px" (stretchy) + Run button ──────────
        meta_row = NSStackView.alloc().init()
        meta_row.setOrientation_(0)
        meta_row.setSpacing_(4.0)
        meta_row.setDistribution_(0)  # NSStackViewDistributionFill
        meta_row.setTranslatesAutoresizingMaskIntoConstraints_(False)
        meta_row.widthAnchor().constraintEqualToConstant_(inner_w).setActive_(True)

        dot = make_label("\u25cf", size=10.0)  # ●
        dot.setTextColor_(
            NSColor.systemOrangeColor() if (is_running or is_pending) else NSColor.systemGreenColor()
        )

        project = getattr(task, "project_name", "") or ""
        meta_text = f"{task.task_id} \u00b7 {project}" if project else task.task_id
        if not is_running and not is_pending:
            meta_text += f"  {task.priority}"

        meta_lbl = make_label(meta_text, size=11.0, secondary=True)
        # Low hugging priority so meta_lbl stretches, pushing Run button to the right
        meta_lbl.setContentHuggingPriority_forOrientation_(249, 0)

        meta_row.addArrangedSubview_(dot)
        meta_row.addArrangedSubview_(meta_lbl)

        # Run button always visible on the right (absent only when already running)
        if not is_running:
            if is_pending:
                run_btn = make_button("Launching\u2026", self, "_runTask:")
                run_btn.setEnabled_(False)
            else:
                run_btn = make_button("\u25b6 Run", self, "_runTask:")
            run_btn.setRepresentedObject_(task.task_id)
            meta_row.addArrangedSubview_(run_btn)

        container.addArrangedSubview_(meta_row)

        # ── Row 2: title (clickable to expand) ────────────────────────────
        title_btn = NSButton.buttonWithTitle_target_action_(
            task.title, self, "_toggleTask:"
        )
        title_btn.setRepresentedObject_(task.task_id)
        title_btn.setBordered_(False)
        title_btn.setBezelStyle_(0)
        title_btn.setFont_(NSFont.boldSystemFontOfSize_(13.0))
        title_btn.setAlignment_(0)   # NSTextAlignmentLeft
        title_btn.setLineBreakMode_(3)  # NSLineBreakByTruncatingTail
        title_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title_btn.widthAnchor().constraintEqualToConstant_(inner_w).setActive_(True)
        container.addArrangedSubview_(title_btn)

        # ── Expanded description ──────────────────────────────────────────
        if expanded and not is_running:
            desc = getattr(task, "_cached_desc", task.title)
            desc_view = _make_desc_scroll_view(_extract_description(desc))
            container.addArrangedSubview_(desc_view)

        return container

    @objc.python_method
    def _make_agent_row(self, agent: dict) -> Any:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)

        task_id = agent.get("task_id", "?")
        title = agent.get("title", "")[:40]
        lbl = make_label(f"\u2699 {task_id} \u00b7 {title}", size=12.0)
        lbl.setContentCompressionResistancePriority_forOrientation_(250, 0)

        log_btn = make_button("Control", self, "_controlAgent:")
        log_btn.setRepresentedObject_(agent.get("task_id", ""))
        stop_btn = make_button("\u25a0 Stop", self, "_stopAgent:")
        stop_btn.setRepresentedObject_(agent.get("task_id", ""))

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(log_btn)
        row.addArrangedSubview_(stop_btn)
        return row

    @objc.python_method
    def _make_completed_row(self, agent: dict) -> Any:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(6.0)
        row.setDistribution_(0)

        task_id = agent.get("task_id", "?")
        title = agent.get("title", "")[:38]
        project = agent.get("project", "")
        status = agent.get("status", "completed")
        icon = "\u2753" if status == "unknown" else "\u2713"
        lbl = make_label(f"{icon} {task_id} \u00b7 {title} ({project})", size=12.0)
        if status == "unknown":
            lbl.setTextColor_(NSColor.secondaryLabelColor())
        lbl.setContentCompressionResistancePriority_forOrientation_(249, 0)

        dismiss_btn = make_button("\u2715", self, "_dismissCompleted:")
        dismiss_btn.setRepresentedObject_(task_id)

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(dismiss_btn)
        return row


# ── Plugin ─────────────────────────────────────────────────────────────────

class Plugin(PennyPlugin):
    """Beads issue tracker integration."""

    def __init__(self) -> None:
        self._app: Any = None
        self._ui_ctrl: Any = None

    @property
    def name(self) -> str:
        return "beads"

    @property
    def description(self) -> str:
        return "Task discovery and agent prompts via Beads (bd CLI)"

    def is_available(self) -> bool:
        return shutil.which("bd") is not None

    def on_activate(self, app: Any) -> None:
        self._app = app

    def on_first_activated(self, app: Any) -> None:
        """Notify the user that beads was detected and task management is now active."""
        try:
            from ..spawner import send_notification
            send_notification(
                "Penny",
                "Beads detected \u2014 task management activated. Run \u2018bd ready\u2019 to see ready tasks.",
            )
        except Exception:
            pass

    def on_deactivate(self) -> None:
        self._ui_ctrl = None
        self._app = None

    def preflight_checks(self, config: dict[str, Any]) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []

        if shutil.which("bd") is None:
            issues.append(PreflightIssue(
                severity="error",
                message="`bd` (beads) CLI not found in PATH.",
                fix_hint="Install it: brew install beads  (or: npm install -g @beads/bd)\n"
                         "Then re-run install.sh so launchd picks up the new PATH.",
            ))

        for entry in config.get("projects", []):
            path_str: str = entry.get("path", "")
            if "PLACEHOLDER" in path_str:
                continue
            project_path = Path(path_str).expanduser()
            if not project_path.exists():
                continue
            beads_dir = project_path / ".beads"
            if not beads_dir.exists():
                issues.append(PreflightIssue(
                    severity="warning",
                    message=f"No .beads/ directory in {project_path}.",
                    fix_hint=f"Run `bd init` inside {project_path} to initialise beads.",
                ))

        return issues

    def get_tasks(self, projects: list[dict[str, Any]]) -> list[Task]:
        all_tasks: list[Task] = []

        for project in projects:
            path = str(Path(project["path"]).expanduser())
            if not Path(path).exists():
                print(f"[beads] get_tasks: path not found: {path}", flush=True)
                continue
            if not (Path(path) / ".beads").exists():
                continue
            output = _run_bd(["ready"], path)
            tasks = _parse_bd_ready(output, path)
            for t in tasks:
                t.metadata["project_priority"] = project.get("priority", 99)
            all_tasks.extend(tasks)

        priority_order = {"P1": 1, "P2": 2, "P3": 3}
        all_tasks.sort(
            key=lambda t: (
                t.metadata.get("project_priority", 99),
                priority_order.get(t.priority, 99),
            )
        )
        return all_tasks

    def on_agent_spawned(self, task: Task, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        plugin_state.setdefault("spawned_task_ids", []).append(task.task_id)

    def on_agent_completed(self, record: dict[str, Any], plugin_state: dict[str, Any]) -> None:
        pass

    def filter_tasks(
        self,
        tasks: list[Task],
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[Task]:
        work_cfg = config.get("work", {})
        max_agents = work_cfg.get("max_agents_per_run", 2)
        priority_levels = work_cfg.get("task_priority_levels", ["P1", "P2", "P3"])

        beads_state = state.get("plugin_state", {}).get("beads", {})
        spawned_ids = set(beads_state.get("spawned_task_ids", []))
        running_ids = {
            a["task_id"]
            for a in state.get("agents_running", [])
        }
        skip_ids = spawned_ids | running_ids

        filtered = [
            t for t in tasks
            if t.task_id not in skip_ids and t.priority in priority_levels
        ]
        return filtered[:max_agents]

    def get_completed_tasks(
        self, projects: list[dict[str, Any]], plugin_state: dict[str, Any]
    ) -> list[Task]:
        seen_ids = set(plugin_state.get("seen_closed_ids", []))
        initialized = set(plugin_state.get("initialized_projects", []))
        new_tasks: list[Task] = []
        for project in projects:
            path = str(Path(project["path"]).expanduser())
            if not Path(path).exists():
                continue
            if not (Path(path) / ".beads").exists():
                continue
            output = _run_bd(["list", "--status=closed"], path)
            tasks = _parse_bd_list(output, path)
            if path not in initialized:
                # First encounter with this project: silently seed all existing
                # closed tasks so we don't spam notifications for old work.
                for task in tasks:
                    seen_ids.add(task.task_id)
                initialized.add(path)
            else:
                for task in tasks:
                    if task.task_id not in seen_ids:
                        new_tasks.append(task)
                        seen_ids.add(task.task_id)
        plugin_state["seen_closed_ids"] = list(seen_ids)
        plugin_state["initialized_projects"] = list(initialized)
        return new_tasks

    def get_task_description(self, task: Task) -> str | None:
        output = _run_bd(["show", task.task_id], task.project_path)
        return output if output else None

    def get_agent_prompt_template(self) -> str | None:
        return AGENT_PROMPT_TEMPLATE

    def handle_action(self, action: str, payload: Any) -> bool:
        """Handle bd CLI actions dispatched from the UI."""
        if action != "bd_command":
            return False
        args, cwd = payload
        str_args = [str(a) for a in args]
        str_cwd = str(cwd) if cwd else ""
        if not str_cwd:
            return False
        try:
            r = subprocess.run(
                ["bd"] + str_args,
                cwd=str_cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                print(f"[penny] bd {str_args} failed (rc={r.returncode}): {r.stderr.strip()}", flush=True)
        except Exception as exc:
            print(f"[penny] bd {str_args} exception: {exc}", flush=True)
        return True

    def cli_commands(self) -> list[dict[str, Any]]:
        return [
            {"name": "tasks",         "description": "List ready beads tasks",       "api_path": "/api/state", "method": "GET"},
            {"name": "agents",        "description": "List running Claude agents",    "api_path": "/api/state", "method": "GET"},
            {"name": "run",           "description": "Spawn a Claude agent for a task", "api_path": "/api/run",  "method": "POST", "arg": "task-id"},
            {"name": "stop-agent",    "description": "Stop a running agent",          "api_path": "/api/stop-agent", "method": "POST", "arg": "task-id"},
            {"name": "dismiss",       "description": "Dismiss a completed task",      "api_path": "/api/dismiss",    "method": "POST", "arg": "task-id"},
            {"name": "clear-completed", "description": "Clear all completed tasks",   "api_path": "/api/clear-completed", "method": "POST"},
        ]

    def config_schema(self) -> dict[str, Any]:
        return {
            "enabled": {
                "type": "string",
                "default": "auto",
                "description": "Enable beads plugin: true, false, or auto (detect)",
            },
        }

    # ── UISection contributions ────────────────────────────────────────────

    def ui_sections(self) -> list[UISection]:
        if self._ui_ctrl is None:
            self._ui_ctrl = BeadsUIController.alloc().init()
            self._ui_ctrl._plugin = self
            self._ui_ctrl._app = self._app
        return [
            UISection(
                name="beads_tasks",
                sort_order=10,
                build_view=self._build_tasks_view,
                rebuild=self._rebuild_tasks_view,
            ),
            UISection(
                name="beads_completed",
                sort_order=20,
                build_view=self._build_completed_view,
                rebuild=self._rebuild_completed_view,
            ),
        ]

    @objc.python_method
    def _build_tasks_view(self) -> Any:
        """Build the outer NSStackView for tasks + agents."""
        ctrl = self._ui_ctrl
        outer = NSStackView.alloc().init()
        outer.setOrientation_(1)
        outer.setAlignment_(5)   # NSLayoutAttributeLeading
        outer.setSpacing_(4.0)
        outer.setTranslatesAutoresizingMaskIntoConstraints_(False)
        outer.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)

        # Tasks header label (left-aligned)
        ctrl._tasks_header_lbl = make_label("BEADS TASKS", size=11.0, secondary=True)
        outer.addArrangedSubview_(ctrl._tasks_header_lbl)

        # Tasks inner stack
        ctrl._tasks_stack = _make_inner_stack()
        outer.addArrangedSubview_(ctrl._tasks_stack)

        # Tasks pagination nav
        tasks_nav = NSStackView.alloc().init()
        tasks_nav.setOrientation_(0)
        tasks_nav.setSpacing_(8.0)
        tasks_nav.setDistribution_(2)
        ctrl._tasks_prev_btn = make_button("\u25c0", ctrl, "_tasksPrev:")
        ctrl._tasks_page_lbl = make_label("1 / 1", size=11.0, secondary=True)
        ctrl._tasks_page_lbl.setAlignment_(1)
        ctrl._tasks_next_btn = make_button("\u25b6", ctrl, "_tasksNext:")
        tasks_nav.addArrangedSubview_(ctrl._tasks_prev_btn)
        tasks_nav.addArrangedSubview_(ctrl._tasks_page_lbl)
        tasks_nav.addArrangedSubview_(ctrl._tasks_next_btn)
        tasks_nav.setHidden_(True)
        ctrl._tasks_nav_row = tasks_nav
        outer.addArrangedSubview_(tasks_nav)

        # Agents header (hidden when no agents running)
        ctrl._agents_header_lbl = make_label("Running Agents", size=11.0, secondary=True)
        ctrl._agents_header_lbl.setHidden_(True)
        outer.addArrangedSubview_(ctrl._agents_header_lbl)

        # Agents inner stack
        ctrl._agents_stack = _make_inner_stack()
        ctrl._agents_stack.setHidden_(True)
        outer.addArrangedSubview_(ctrl._agents_stack)

        # Agents pagination nav
        agents_nav = NSStackView.alloc().init()
        agents_nav.setOrientation_(0)
        agents_nav.setSpacing_(8.0)
        agents_nav.setDistribution_(2)
        ctrl._agents_prev_btn = make_button("\u25c0", ctrl, "_agentsPrev:")
        ctrl._agents_page_lbl = make_label("1 / 1", size=11.0, secondary=True)
        ctrl._agents_page_lbl.setAlignment_(1)
        ctrl._agents_next_btn = make_button("\u25b6", ctrl, "_agentsNext:")
        agents_nav.addArrangedSubview_(ctrl._agents_prev_btn)
        agents_nav.addArrangedSubview_(ctrl._agents_page_lbl)
        agents_nav.addArrangedSubview_(ctrl._agents_next_btn)
        agents_nav.setHidden_(True)
        ctrl._agents_nav_row = agents_nav
        outer.addArrangedSubview_(agents_nav)

        return outer

    @objc.python_method
    def _build_completed_view(self) -> Any:
        """Build the outer NSStackView for the recently completed section."""
        ctrl = self._ui_ctrl
        outer = NSStackView.alloc().init()
        outer.setOrientation_(1)
        outer.setAlignment_(5)   # NSLayoutAttributeLeading
        outer.setSpacing_(4.0)
        outer.setTranslatesAutoresizingMaskIntoConstraints_(False)
        outer.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)

        # Header row: "Recently Completed" + "Clear All" button
        header_row = NSStackView.alloc().init()
        header_row.setOrientation_(0)
        header_row.setSpacing_(8.0)
        header_row.setDistribution_(2)
        comp_lbl = make_label("Recently Completed", size=11.0, secondary=True)
        clear_btn = make_button("Clear All", ctrl, "_clearAllCompleted:")
        header_row.addArrangedSubview_(comp_lbl)
        header_row.addArrangedSubview_(clear_btn)
        header_row.setHidden_(True)
        ctrl._completed_header_row = header_row
        outer.addArrangedSubview_(header_row)

        # Completed inner stack
        ctrl._completed_stack = _make_inner_stack()
        ctrl._completed_stack.setHidden_(True)
        outer.addArrangedSubview_(ctrl._completed_stack)

        # Completed pagination nav
        comp_nav = NSStackView.alloc().init()
        comp_nav.setOrientation_(0)
        comp_nav.setSpacing_(8.0)
        comp_nav.setDistribution_(2)
        ctrl._completed_prev_btn = make_button("\u25c0", ctrl, "_completedPrev:")
        ctrl._completed_page_lbl = make_label("1 / 1", size=11.0, secondary=True)
        ctrl._completed_page_lbl.setAlignment_(1)
        ctrl._completed_next_btn = make_button("\u25b6", ctrl, "_completedNext:")
        comp_nav.addArrangedSubview_(ctrl._completed_prev_btn)
        comp_nav.addArrangedSubview_(ctrl._completed_page_lbl)
        comp_nav.addArrangedSubview_(ctrl._completed_next_btn)
        comp_nav.setHidden_(True)
        ctrl._completed_nav_row = comp_nav
        outer.addArrangedSubview_(comp_nav)

        # Start hidden; shown by _rebuild_completed_section when there are items
        outer.setHidden_(True)
        ctrl._completed_outer = outer

        return outer

    @objc.python_method
    def _rebuild_tasks_view(self, data: dict) -> None:
        ctrl = self._ui_ctrl
        if ctrl is None:
            return
        ready_tasks = data.get("ready_tasks", [])
        agents = data.get("state", {}).get("agents_running", [])
        completed = data.get("state", {}).get("recently_completed", [])
        ctrl._latest_tasks = ready_tasks
        ctrl._latest_agents = agents
        ctrl._latest_completed = completed
        # Fresh data is authoritative — clear all pending states.
        # Tasks now confirmed running will disappear naturally (filtered by running_ids);
        # failed spawns will reappear as normal ready tasks.
        ctrl._pending_task_ids.clear()
        ctrl._rebuild_tasks_section(ready_tasks, agents)
        ctrl._rebuild_agents_section(agents)

    @objc.python_method
    def _rebuild_completed_view(self, data: dict) -> None:
        ctrl = self._ui_ctrl
        if ctrl is None:
            return
        completed = data.get("state", {}).get("recently_completed", [])
        ctrl._latest_completed = completed
        ctrl._rebuild_completed_section(completed)
