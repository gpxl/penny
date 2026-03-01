"""Control Center popover view controller for Nae Nae.

Builds the entire UI programmatically — no NIB/XIB required.
Layout via NSStackView. Live updates via updateWithData_().
"""

from __future__ import annotations

import subprocess
from typing import Any

import objc
from AppKit import (
    NSButton,
    NSColor,
    NSFont,
    NSLayoutConstraint,
    NSScrollView,
    NSStackView,
    NSTextField,
    NSTextView,
    NSView,
    NSViewController,
)
from Foundation import NSEdgeInsets, NSMakeRect, NSObject

from .ui_components import ProgressBarView, make_label

# Popover width (fixed). Height is dynamic.
_WIDTH: float = 380.0
_PADDING: float = 16.0
_BAR_HEIGHT: float = 8.0
_SECTION_SPACING: float = 10.0
_TASK_LIMIT = 8  # max ready tasks shown


def _make_separator() -> NSView:
    from AppKit import NSBox
    sep = NSBox.alloc().initWithFrame_(((0, 0), (_WIDTH - _PADDING * 2, 1)))
    sep.setBoxType_(2)   # NSBoxSeparator — renders as a native hairline separator
    return sep


def _make_button(title: str, target: Any, action: str, small: bool = True) -> NSButton:
    btn = NSButton.buttonWithTitle_target_action_(title, target, action)
    if small:
        btn.setControlSize_(1)   # NSControlSizeSmall
        btn.setFont_(NSFont.systemFontOfSize_(11.0))
    btn.setBezelStyle_(4)        # NSBezelStyleRounded
    return btn


class ControlCenterViewController(NSViewController):
    """Popover view controller built entirely in code."""

    def init(self) -> "ControlCenterViewController":
        self = objc.super(ControlCenterViewController, self).init()
        if self is None:
            return self
        # Strong refs to all live elements so PyObjC doesn't GC them
        self._bar_all: ProgressBarView | None = None
        self._bar_sonnet: ProgressBarView | None = None
        self._bar_session: ProgressBarView | None = None
        self._lbl_all_pct: NSTextField | None = None
        self._lbl_sonnet_pct: NSTextField | None = None
        self._lbl_session_pct: NSTextField | None = None
        self._lbl_weekly_reset: NSTextField | None = None
        self._lbl_session_reset: NSTextField | None = None
        self._tasks_stack: NSStackView | None = None
        self._agents_stack: NSStackView | None = None
        self._task_views: list[Any] = []
        self._agent_views: list[Any] = []
        self._expanded_task_id: str | None = None
        self._app: Any = None      # set by app.py after construction
        self._config: dict = {}
        self._state: dict = {}
        self._all_ready_tasks: list = []
        self._agents_running: list = []
        return self

    def loadView(self) -> None:
        """Build the full popover UI."""
        outer = NSView.alloc().initWithFrame_(((0, 0), (_WIDTH, 500)))
        self.setView_(outer)
        self._build(outer)

    # ── Public update API ──────────────────────────────────────────────────

    def updateWithData_(self, data: dict) -> None:
        """Refresh UI from fresh data dict. Must be called on the main thread."""
        pred = data.get("prediction")
        state = data.get("state", {})
        ready_tasks = data.get("ready_tasks", [])
        agents_running = state.get("agents_running", [])

        self._state = state
        self._all_ready_tasks = ready_tasks
        self._agents_running = agents_running

        if pred:
            self._bar_all.setPct(pred.pct_all)
            self._bar_sonnet.setPct(pred.pct_sonnet)
            self._bar_session.setPct(pred.session_pct_all)
            self._lbl_all_pct.setStringValue_(f"{pred.pct_all:.0f}%")
            self._lbl_sonnet_pct.setStringValue_(f"{pred.pct_sonnet:.0f}%")
            self._lbl_session_pct.setStringValue_(f"{pred.session_pct_all:.0f}%")
            self._lbl_weekly_reset.setStringValue_(
                f"Resets {pred.reset_label}" if pred.reset_label else "—"
            )
            self._lbl_session_reset.setStringValue_(
                f"Resets {pred.session_reset_label}" if pred.session_reset_label else "—"
            )

        self._rebuild_tasks_section(ready_tasks, agents_running)
        self._rebuild_agents_section(agents_running)
        self._relayout()

    # ── Layout helpers ─────────────────────────────────────────────────────

    @objc.python_method
    def _build(self, outer: NSView) -> None:
        """Construct all subviews inside `outer`."""
        stack = NSStackView.alloc().initWithFrame_(((0, 0), (_WIDTH, 500)))
        stack.setOrientation_(1)      # NSUserInterfaceLayoutOrientationVertical
        stack.setAlignment_(9)        # NSLayoutAttributeLeading
        stack.setSpacing_(_SECTION_SPACING)
        stack.setEdgeInsets_(NSEdgeInsets(_PADDING, _PADDING, _PADDING, _PADDING))
        stack.setDistribution_(0)     # NSStackViewDistributionFill
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self._root_stack = stack
        outer.addSubview_(stack)

        # Pin stack to outer edges
        for attr, val in [
            (NSLayoutConstraint.constraintWithItem_attribute_relatedBy_toItem_attribute_multiplier_constant_(
                stack, 1, 0, outer, 1, 1.0, 0.0
            ), None),  # leading
            (NSLayoutConstraint.constraintWithItem_attribute_relatedBy_toItem_attribute_multiplier_constant_(
                stack, 2, 0, outer, 2, 1.0, 0.0
            ), None),  # trailing
            (NSLayoutConstraint.constraintWithItem_attribute_relatedBy_toItem_attribute_multiplier_constant_(
                stack, 3, 0, outer, 3, 1.0, 0.0
            ), None),  # top
        ]:
            attr.setActive_(True)
        # Bottom constraint — flexible so popover height adjusts
        bottom_c = NSLayoutConstraint.constraintWithItem_attribute_relatedBy_toItem_attribute_multiplier_constant_(
            stack, 4, 1, outer, 4, 1.0, 0.0
        )
        bottom_c.setPriority_(750)
        bottom_c.setActive_(True)

        self._populate_stack(stack)

    @objc.python_method
    def _populate_stack(self, stack: NSStackView) -> None:
        """Add all sections to the root stack."""
        # ── Header ──────────────────────────────────────────────────────────
        header = self._make_header_row()
        stack.addArrangedSubview_(header)
        stack.addArrangedSubview_(_make_separator())

        # ── Weekly Usage ────────────────────────────────────────────────────
        stack.addArrangedSubview_(make_label("Weekly Usage", size=11.0, secondary=True))

        self._bar_all, self._lbl_all_pct = self._add_bar_row(
            stack, "All models", 0.0
        )
        self._bar_sonnet, self._lbl_sonnet_pct = self._add_bar_row(
            stack, "Sonnet", 0.0
        )

        self._lbl_weekly_reset = make_label("—", size=11.0, secondary=True)
        stack.addArrangedSubview_(self._lbl_weekly_reset)
        stack.addArrangedSubview_(_make_separator())

        # ── Session ─────────────────────────────────────────────────────────
        stack.addArrangedSubview_(make_label("Session", size=11.0, secondary=True))
        self._bar_session, self._lbl_session_pct = self._add_bar_row(
            stack, "This session", 0.0
        )
        self._lbl_session_reset = make_label("—", size=11.0, secondary=True)
        stack.addArrangedSubview_(self._lbl_session_reset)
        stack.addArrangedSubview_(_make_separator())

        # ── Ready Tasks ──────────────────────────────────────────────────────
        tasks_header = self._make_tasks_header_row()
        stack.addArrangedSubview_(tasks_header)

        self._tasks_stack = NSStackView.alloc().init()
        self._tasks_stack.setOrientation_(1)
        self._tasks_stack.setAlignment_(9)
        self._tasks_stack.setSpacing_(4.0)
        self._tasks_stack.setDistribution_(0)
        self._tasks_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        stack.addArrangedSubview_(self._tasks_stack)
        stack.addArrangedSubview_(_make_separator())

        # ── Running Agents ───────────────────────────────────────────────────
        stack.addArrangedSubview_(make_label("Running Agents", size=11.0, secondary=True))

        self._agents_stack = NSStackView.alloc().init()
        self._agents_stack.setOrientation_(1)
        self._agents_stack.setAlignment_(9)
        self._agents_stack.setSpacing_(4.0)
        self._agents_stack.setDistribution_(0)
        self._agents_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        stack.addArrangedSubview_(self._agents_stack)
        stack.addArrangedSubview_(_make_separator())

        # ── Footer ───────────────────────────────────────────────────────────
        footer = self._make_footer_row()
        stack.addArrangedSubview_(footer)

    @objc.python_method
    def _make_header_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)   # horizontal
        row.setSpacing_(8.0)
        row.setDistribution_(2)  # NSStackViewDistributionEqualSpacing

        title = make_label("● Nae Nae", size=15.0, bold=True)
        refresh_btn = _make_button("↻ Refresh", self, "refreshNow:")
        row.addArrangedSubview_(title)
        row.addArrangedSubview_(refresh_btn)
        # Keep refs
        self._header_title = title
        self._refresh_btn = refresh_btn
        return row

    @objc.python_method
    def _make_tasks_header_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(2)

        self._tasks_header_lbl = make_label("Ready Tasks", size=11.0, secondary=True)
        new_btn = _make_button("+ New", self, "newTask:")
        row.addArrangedSubview_(self._tasks_header_lbl)
        row.addArrangedSubview_(new_btn)
        self._new_task_btn = new_btn
        return row

    @objc.python_method
    def _make_footer_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(0)

        report_btn = _make_button("View Report", self, "viewReport:")
        prefs_btn = _make_button("Preferences", self, "openPrefs:")
        quit_btn = _make_button("Quit", self, "quitApp:")

        for btn in (report_btn, prefs_btn, quit_btn):
            row.addArrangedSubview_(btn)

        # Keep refs
        self._footer_btns = [report_btn, prefs_btn, quit_btn]
        return row

    @objc.python_method
    def _add_bar_row(self, stack: NSStackView, label: str,
                     initial_pct: float) -> tuple[ProgressBarView, NSTextField]:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(0)

        lbl = make_label(label, size=12.0)
        lbl.setContentHuggingPriority_forOrientation_(251, 0)

        bar = ProgressBarView.alloc().initWithFrame_(((0, 0), (140.0, _BAR_HEIGHT)))
        bar.setPct(initial_pct)
        bar.setTranslatesAutoresizingMaskIntoConstraints_(False)
        bar.widthAnchor().constraintEqualToConstant_(140.0).setActive_(True)
        bar.heightAnchor().constraintEqualToConstant_(_BAR_HEIGHT).setActive_(True)

        pct_lbl = make_label(f"{initial_pct:.0f}%", size=12.0)
        pct_lbl.setAlignment_(1)  # NSTextAlignmentRight
        pct_lbl.setContentHuggingPriority_forOrientation_(251, 0)

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(bar)
        row.addArrangedSubview_(pct_lbl)
        stack.addArrangedSubview_(row)
        return bar, pct_lbl

    # ── Tasks section ──────────────────────────────────────────────────────

    @objc.python_method
    def _rebuild_tasks_section(self, tasks: list, agents_running: list) -> None:
        """Rebuild the ready-tasks stack with inline action buttons."""
        # Clear existing
        for v in self._task_views:
            v.removeFromSuperview()
        self._task_views = []

        running_ids = {a.get("task_id") for a in agents_running}
        shown = tasks[:_TASK_LIMIT]

        if not shown:
            placeholder = make_label("No ready tasks", size=12.0, secondary=True)
            self._tasks_stack.addArrangedSubview_(placeholder)
            self._task_views.append(placeholder)
            self._tasks_header_lbl.setStringValue_("Ready Tasks")
            return

        self._tasks_header_lbl.setStringValue_(f"Ready Tasks ({len(tasks)})")

        for task in shown:
            is_expanded = (task.task_id == self._expanded_task_id)
            row_view = self._make_task_row(task, is_expanded, running_ids)
            self._tasks_stack.addArrangedSubview_(row_view)
            self._task_views.append(row_view)

    @objc.python_method
    def _make_task_row(self, task: Any, expanded: bool, running_ids: set) -> NSView:
        """Build one task row (collapsed or expanded)."""
        container = NSStackView.alloc().init()
        container.setOrientation_(1)
        container.setAlignment_(9)
        container.setSpacing_(4.0)

        is_running = task.task_id in running_ids

        # Header line: [priority] id · title
        title_row = NSStackView.alloc().init()
        title_row.setOrientation_(0)
        title_row.setSpacing_(6.0)

        pri_badge = make_label(f"[{task.priority}]", size=11.0, secondary=True)
        id_lbl = make_label(f"{task.task_id}", size=11.0, bold=True)
        title_lbl = make_label(task.title[:50], size=12.0)
        title_lbl.setLineBreakMode_(4)   # NSLineBreakByTruncatingTail
        title_lbl.setMaximumNumberOfLines_(1)
        # Allow title to be compressed so the row never overflows the popover width
        title_lbl.setContentCompressionResistancePriority_forOrientation_(249, 0)

        title_row.addArrangedSubview_(pri_badge)
        title_row.addArrangedSubview_(id_lbl)
        title_row.addArrangedSubview_(title_lbl)

        # Expand/collapse toggle button
        toggle_title = "▼" if expanded else "▶"
        toggle_btn = _make_button(toggle_title, self, "_toggleTask:")
        toggle_btn.setTag_(id(task))    # hack: we store task_id in title accessible via repr
        # Store task_id in the button accessible title for the callback
        toggle_btn.setTitle_(f"{'▼' if expanded else '▶'} {task.task_id}")
        title_row.addArrangedSubview_(toggle_btn)

        container.addArrangedSubview_(title_row)

        if is_running:
            running_lbl = make_label("⚙ Running…", size=11.0, secondary=True)
            container.addArrangedSubview_(running_lbl)
        elif expanded:
            # Description area (truncated)
            desc = getattr(task, "_cached_desc", task.title)
            desc_lbl = make_label(desc[:200], size=11.0, secondary=True)
            desc_lbl.setMaximumNumberOfLines_(4)
            desc_lbl.setLineBreakMode_(0)   # NSLineBreakByWordWrapping
            container.addArrangedSubview_(desc_lbl)

            # Action buttons row 1
            actions1 = NSStackView.alloc().init()
            actions1.setOrientation_(0)
            actions1.setSpacing_(6.0)

            run_btn = _make_button("▶ Run", self, "_runTask:")
            run_btn.setTitle_(f"▶ Run {task.task_id}")
            defer1h = _make_button("↓+1h", self, "_deferTask1h:")
            defer1h.setTitle_(f"↓+1h {task.task_id}")
            defer4h = _make_button("↓+4h", self, "_deferTask4h:")
            defer4h.setTitle_(f"↓+4h {task.task_id}")
            defer_tmrw = _make_button("↓Tomorrow", self, "_deferTaskTomorrow:")
            defer_tmrw.setTitle_(f"↓Tomorrow {task.task_id}")

            for btn in (run_btn, defer1h, defer4h, defer_tmrw):
                actions1.addArrangedSubview_(btn)
            container.addArrangedSubview_(actions1)

            # Action buttons row 2
            actions2 = NSStackView.alloc().init()
            actions2.setOrientation_(0)
            actions2.setSpacing_(6.0)

            claim_btn = _make_button("⊕ Claim", self, "_claimTask:")
            claim_btn.setTitle_(f"⊕ Claim {task.task_id}")
            close_btn = _make_button("✕ Close", self, "_closeTask:")
            close_btn.setTitle_(f"✕ Close {task.task_id}")

            for btn in (claim_btn, close_btn):
                actions2.addArrangedSubview_(btn)
            container.addArrangedSubview_(actions2)

        return container

    # ── Agents section ─────────────────────────────────────────────────────

    @objc.python_method
    def _rebuild_agents_section(self, agents: list) -> None:
        for v in self._agent_views:
            v.removeFromSuperview()
        self._agent_views = []

        if not agents:
            placeholder = make_label("No agents running", size=12.0, secondary=True)
            self._agents_stack.addArrangedSubview_(placeholder)
            self._agent_views.append(placeholder)
            return

        for agent in agents:
            row = self._make_agent_row(agent)
            self._agents_stack.addArrangedSubview_(row)
            self._agent_views.append(row)

    @objc.python_method
    def _make_agent_row(self, agent: dict) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)

        task_id = agent.get("task_id", "?")
        title = agent.get("title", "")[:40]
        lbl = make_label(f"⚙ {task_id} · {title}", size=12.0)
        lbl.setContentCompressionResistancePriority_forOrientation_(250, 0)

        log_btn = _make_button("📋 Log", self, "_openAgentLog:")
        log_btn.setTitle_(f"📋 Log {agent.get('log', '')}")
        stop_btn = _make_button("■ Stop", self, "_stopAgent:")
        stop_btn.setTitle_(f"■ Stop {agent.get('pid', 0)}")

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(log_btn)
        row.addArrangedSubview_(stop_btn)
        return row

    # ── Layout pass ────────────────────────────────────────────────────────

    @objc.python_method
    def _relayout(self) -> None:
        """Resize the popover to fit the current content.

        Two layout passes ensure the stack has resolved its intrinsic sizes
        before we ask for fittingSize, preventing the popover from resizing
        to a stale height and then snapping again on the next event loop tick.
        """
        if self.view() is None:
            return
        # First pass: resolve pending layout so fittingSize is accurate
        self.view().layoutSubtreeIfNeeded()
        # Second pass: accommodate any layout-triggered changes
        self.view().layoutSubtreeIfNeeded()
        size = self._root_stack.fittingSize()
        new_height = max(200.0, size.height)
        self.view().setFrame_(((0, 0), (_WIDTH, new_height)))
        # Resize the popover (if we are presented inside one)
        if self._app and hasattr(self._app, "_popover"):
            self._app._popover.setContentSize_((_WIDTH, new_height))

    # ── Button action selectors ────────────────────────────────────────────

    def refreshNow_(self, sender: Any) -> None:
        if self._app:
            self._app.refreshNow_(sender)

    def newTask_(self, sender: Any) -> None:
        if self._app:
            self._app._newTaskSheet_(sender)

    def viewReport_(self, sender: Any) -> None:
        if self._app:
            self._app.viewReport_(sender)

    def openPrefs_(self, sender: Any) -> None:
        if self._app:
            self._app.openPrefs_(sender)

    def quitApp_(self, sender: Any) -> None:
        if self._app:
            self._app.quitApp_(sender)

    def _toggleTask_(self, sender: Any) -> None:
        """Toggle task expansion. Task ID is encoded in button title after prefix."""
        title = sender.title() or ""
        # Format: "▼ TASK_ID" or "▶ TASK_ID"
        parts = title.split(" ", 1)
        if len(parts) < 2:
            return
        task_id = parts[1].strip()
        if self._expanded_task_id == task_id:
            self._expanded_task_id = None
        else:
            self._expanded_task_id = task_id
            # Pre-fetch description so it's available for the rebuild
            task = next((t for t in self._all_ready_tasks if t.task_id == task_id), None)
            if task and not getattr(task, "_cached_desc", None):
                from .tasks import get_task_description
                task._cached_desc = get_task_description(task)
        self._rebuild_tasks_section(self._all_ready_tasks, self._agents_running)
        self._relayout()

    @objc.python_method
    def _task_id_from_btn(self, sender: Any, prefix: str) -> str | None:
        """Extract task_id from a button whose title is 'PREFIX TASK_ID'."""
        title = (sender.title() or "").strip()
        if title.startswith(prefix):
            return title[len(prefix):].strip() or None
        # Fallback: last token
        parts = title.rsplit(" ", 1)
        return parts[-1] if parts else None

    def _runTask_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "▶ Run ")
        task = next((t for t in self._all_ready_tasks if t.task_id == task_id), None)
        if task and self._app:
            self._app.spawnTask_(task)

    def _deferTask1h_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "↓+1h ")
        if task_id and self._app:
            self._app.runBdAction_((["defer", task_id, "--until", "+1h"],
                                    self._task_project_path(task_id)))

    def _deferTask4h_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "↓+4h ")
        if task_id and self._app:
            self._app.runBdAction_((["defer", task_id, "--until", "+4h"],
                                    self._task_project_path(task_id)))

    def _deferTaskTomorrow_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "↓Tomorrow ")
        if task_id and self._app:
            self._app.runBdAction_((["defer", task_id, "--until", "tomorrow"],
                                    self._task_project_path(task_id)))

    def _claimTask_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "⊕ Claim ")
        if task_id and self._app:
            self._app.runBdAction_((["update", task_id, "--claim"],
                                    self._task_project_path(task_id)))

    def _closeTask_(self, sender: Any) -> None:
        task_id = self._task_id_from_btn(sender, "✕ Close ")
        if task_id and self._app:
            self._app.runBdAction_((["close", task_id],
                                    self._task_project_path(task_id)))

    @objc.python_method
    def _task_project_path(self, task_id: str) -> str:
        task = next((t for t in self._all_ready_tasks if t.task_id == task_id), None)
        return task.project_path if task else ""

    def _openAgentLog_(self, sender: Any) -> None:
        title = (sender.title() or "").strip()
        log_path = title.removeprefix("📋 Log ").strip()
        if log_path:
            subprocess.run(["open", log_path], check=False)

    def _stopAgent_(self, sender: Any) -> None:
        title = (sender.title() or "").strip()
        try:
            pid = int(title.removeprefix("■ Stop ").strip())
        except (ValueError, AttributeError):
            return
        if self._app:
            self._app.stopAgent_(pid)
