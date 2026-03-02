"""Control Center popover view controller for Nae Nae.

Builds the entire UI programmatically — no NIB/XIB required.
Layout via NSStackView. Live updates via updateWithData_().
"""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

import objc
from AppKit import (
    NSAttributedString,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSLayoutConstraint,
    NSMutableAttributedString,
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
_TASK_LIMIT = 20          # max ready tasks fetched
_PAGE_SIZE: int = 5       # items shown per page in paginated sections

# ── Markdown helpers ───────────────────────────────────────────────────────────

_SECTION_HDR_RE = re.compile(r"^[A-Z][A-Z_\s]{3,}$")
_INLINE_MARKUP_RE = re.compile(r"\*\*(.+?)\*\*|`(.+?)`")


def _extract_description(bd_show_output: str) -> str:
    """Strip the bd show header lines; return from first ALL-CAPS section header onward."""
    lines = bd_show_output.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and _SECTION_HDR_RE.match(stripped):
            return "\n".join(lines[i:]).strip()
    return bd_show_output.strip()


def _markdown_to_attrstr(text: str) -> Any:
    """Convert **bold**, `code`, ALL-CAPS section headers, and bullet lists to NSMutableAttributedString."""
    body_font = NSFont.systemFontOfSize_(12.0)
    bold_font = NSFont.boldSystemFontOfSize_(12.0)
    code_font = NSFont.userFixedPitchFontOfSize_(11.0)
    hdr_font = NSFont.boldSystemFontOfSize_(11.0)
    body_color = NSColor.labelColor()
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


def _make_desc_scroll_view(text: str) -> NSScrollView:
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
    sv.setDrawsBackground_(False)   # transparent — matches popover material
    sv.setTranslatesAutoresizingMaskIntoConstraints_(False)
    sv.widthAnchor().constraintEqualToConstant_(inner_w).setActive_(True)
    sv.heightAnchor().constraintEqualToConstant_(content_h).setActive_(True)
    return sv


def _make_separator() -> NSView:
    from AppKit import NSBox
    sep = NSBox.alloc().initWithFrame_(((0, 0), (_WIDTH - _PADDING * 2, 1)))
    sep.setBoxType_(2)   # NSBoxSeparator
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
        # Progress bars and labels
        self._bar_all: ProgressBarView | None = None
        self._bar_sonnet: ProgressBarView | None = None
        self._bar_session: ProgressBarView | None = None
        self._lbl_all_pct: NSTextField | None = None
        self._lbl_sonnet_pct: NSTextField | None = None
        self._lbl_session_pct: NSTextField | None = None
        self._lbl_weekly_reset: NSTextField | None = None
        self._lbl_session_reset: NSTextField | None = None
        # Inner stacks for paginated sections
        self._tasks_stack: NSStackView | None = None
        self._agents_stack: NSStackView | None = None
        self._completed_stack: NSStackView | None = None
        # Running Agents section chrome (hidden when empty)
        self._agents_header_lbl: Any = None
        self._agents_sep: Any = None
        # Pagination nav rows
        self._tasks_nav_row: Any = None
        self._agents_nav_row: Any = None
        self._completed_nav_row: Any = None
        # Pagination buttons & labels — tasks
        self._tasks_prev_btn: Any = None
        self._tasks_next_btn: Any = None
        self._tasks_page_lbl: Any = None
        # Pagination buttons & labels — agents
        self._agents_prev_btn: Any = None
        self._agents_next_btn: Any = None
        self._agents_page_lbl: Any = None
        # Pagination buttons & labels — completed
        self._completed_prev_btn: Any = None
        self._completed_next_btn: Any = None
        self._completed_page_lbl: Any = None
        # Pagination state
        self._tasks_page: int = 0
        self._agents_page: int = 0
        self._completed_page: int = 0
        self._tasks_total_pages: int = 1
        self._agents_total_pages: int = 1
        self._completed_total_pages: int = 1
        # Item view lists (for cleanup on rebuild)
        self._task_views: list[Any] = []
        self._agent_views: list[Any] = []
        self._completed_views: list[Any] = []
        # Completed section chrome
        self._completed_header_row: Any = None
        self._completed_sep: Any = None
        # UI state
        self._expanded_task_id: str | None = None
        self._last_refresh_at: datetime | None = None
        self._last_refresh_lbl: NSTextField | None = None
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
        recently_completed = state.get("recently_completed", [])
        fetched_at = data.get("fetched_at")
        if fetched_at is not None:
            self._last_refresh_at = fetched_at
        self._update_last_refresh_label()

        self._state = state
        self._all_ready_tasks = ready_tasks
        self._agents_running = agents_running

        # Guard: views not yet created (loadView not called yet)
        if self._bar_all is None:
            return

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
        self._rebuild_completed_section(recently_completed)
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
    def _make_inner_stack(self) -> NSStackView:
        """Return a plain vertical NSStackView for a paginated section."""
        inner = NSStackView.alloc().init()
        inner.setOrientation_(1)
        inner.setAlignment_(9)
        inner.setSpacing_(4.0)
        inner.setDistribution_(0)
        inner.setTranslatesAutoresizingMaskIntoConstraints_(False)
        inner.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)
        return inner

    @objc.python_method
    def _make_pagination_nav(self, section: str) -> NSView:
        """Return a nav row with ◀ Page X/N ▶ controls for the given section."""
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(2)  # NSStackViewDistributionEqualSpacing

        cap = section.capitalize()
        prev_btn = _make_button("◀", self, f"_prev{cap}Page:")
        page_lbl = make_label("1 / 1", size=11.0, secondary=True)
        page_lbl.setAlignment_(1)  # NSTextAlignmentCenter
        next_btn = _make_button("▶", self, f"_next{cap}Page:")

        row.addArrangedSubview_(prev_btn)
        row.addArrangedSubview_(page_lbl)
        row.addArrangedSubview_(next_btn)

        if section == "tasks":
            self._tasks_prev_btn = prev_btn
            self._tasks_page_lbl = page_lbl
            self._tasks_next_btn = next_btn
        elif section == "agents":
            self._agents_prev_btn = prev_btn
            self._agents_page_lbl = page_lbl
            self._agents_next_btn = next_btn
        elif section == "completed":
            self._completed_prev_btn = prev_btn
            self._completed_page_lbl = page_lbl
            self._completed_next_btn = next_btn

        row.setHidden_(True)  # shown only when total_pages > 1
        return row

    @objc.python_method
    def _update_pagination_nav(
        self,
        nav_row: Any,
        prev_btn: Any,
        next_btn: Any,
        page_lbl: Any,
        page: int,
        total_pages: int,
    ) -> None:
        """Refresh pagination nav controls for a section."""
        if total_pages <= 1:
            nav_row.setHidden_(True)
            return
        nav_row.setHidden_(False)
        page_lbl.setStringValue_(f"{page + 1} / {total_pages}")
        prev_btn.setEnabled_(page > 0)
        next_btn.setEnabled_(page < total_pages - 1)

    @objc.python_method
    def _populate_stack(self, stack: NSStackView) -> None:
        """Add all sections to the root stack."""
        # ── Header ──────────────────────────────────────────────────────────
        header = self._make_header_row()
        stack.addArrangedSubview_(header)
        stack.addArrangedSubview_(_make_separator())

        # ── Session Budget ───────────────────────────────────────────────────
        stack.addArrangedSubview_(make_label("Session Budget", size=11.0, secondary=True))
        self._bar_session, self._lbl_session_pct = self._add_bar_row(
            stack, "This session", 0.0
        )
        self._lbl_session_reset = make_label("—", size=11.0, secondary=True)
        stack.addArrangedSubview_(self._lbl_session_reset)
        stack.addArrangedSubview_(_make_separator())

        # ── Weekly Budget ────────────────────────────────────────────────────
        stack.addArrangedSubview_(make_label("Weekly Budget", size=11.0, secondary=True))
        self._bar_all, self._lbl_all_pct = self._add_bar_row(stack, "All models", 0.0)
        self._bar_sonnet, self._lbl_sonnet_pct = self._add_bar_row(stack, "Sonnet", 0.0)
        self._lbl_weekly_reset = make_label("—", size=11.0, secondary=True)
        stack.addArrangedSubview_(self._lbl_weekly_reset)
        stack.addArrangedSubview_(_make_separator())

        # ── Ready Tasks ──────────────────────────────────────────────────────
        tasks_header = self._make_tasks_header_row()
        stack.addArrangedSubview_(tasks_header)

        self._tasks_stack = self._make_inner_stack()
        stack.addArrangedSubview_(self._tasks_stack)

        self._tasks_nav_row = self._make_pagination_nav("tasks")
        stack.addArrangedSubview_(self._tasks_nav_row)
        stack.addArrangedSubview_(_make_separator())

        # ── Running Agents (hidden when empty) ───────────────────────────────
        self._agents_header_lbl = make_label("Running Agents", size=11.0, secondary=True)
        self._agents_header_lbl.setHidden_(True)
        stack.addArrangedSubview_(self._agents_header_lbl)

        self._agents_stack = self._make_inner_stack()
        self._agents_stack.setHidden_(True)
        stack.addArrangedSubview_(self._agents_stack)

        self._agents_nav_row = self._make_pagination_nav("agents")
        stack.addArrangedSubview_(self._agents_nav_row)

        self._agents_sep = _make_separator()
        self._agents_sep.setHidden_(True)
        stack.addArrangedSubview_(self._agents_sep)

        # ── Recently Completed (hidden when empty) ───────────────────────────
        self._completed_header_row = self._make_completed_header_row()
        self._completed_stack = self._make_inner_stack()
        self._completed_nav_row = self._make_pagination_nav("completed")
        self._completed_sep = _make_separator()

        self._completed_header_row.setHidden_(True)
        self._completed_stack.setHidden_(True)
        self._completed_nav_row.setHidden_(True)
        self._completed_sep.setHidden_(True)

        stack.addArrangedSubview_(self._completed_header_row)
        stack.addArrangedSubview_(self._completed_stack)
        stack.addArrangedSubview_(self._completed_nav_row)
        stack.addArrangedSubview_(self._completed_sep)

        # ── Footer ───────────────────────────────────────────────────────────
        footer = self._make_footer_row()
        stack.addArrangedSubview_(footer)

    @objc.python_method
    def _make_header_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)   # horizontal
        row.setSpacing_(8.0)
        row.setDistribution_(3)  # NSStackViewDistributionEqualSpacing

        refresh_btn = _make_button("↻ Refresh", self, "refreshNow:")
        last_refresh_lbl = make_label("—", size=11.0, secondary=True)
        last_refresh_lbl.setAlignment_(2)  # NSTextAlignmentRight

        row.addArrangedSubview_(refresh_btn)
        row.addArrangedSubview_(last_refresh_lbl)
        self._refresh_btn = refresh_btn
        self._last_refresh_lbl = last_refresh_lbl
        return row

    @objc.python_method
    def _make_tasks_header_row(self) -> NSView:
        self._tasks_header_lbl = make_label("Ready Tasks", size=11.0, secondary=True)
        return self._tasks_header_lbl

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

        self._footer_btns = [report_btn, prefs_btn, quit_btn]
        return row

    def setRefreshing_(self, refreshing: bool) -> None:
        """Update refresh button to show/hide a loading indicator."""
        if self._refresh_btn is None:
            return
        if refreshing:
            self._refresh_btn.setTitle_("↻ Refreshing…")
            self._refresh_btn.setEnabled_(False)
        else:
            self._refresh_btn.setTitle_("↻ Refresh")
            self._refresh_btn.setEnabled_(True)

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
        """Rebuild the ready-tasks stack with pagination."""
        for v in self._task_views:
            v.removeFromSuperview()
        self._task_views = []

        running_ids = {a.get("task_id") for a in agents_running}
        shown = [t for t in tasks if t.task_id not in running_ids][:_TASK_LIMIT]

        if not shown:
            placeholder = make_label("No ready tasks", size=12.0, secondary=True)
            self._tasks_stack.addArrangedSubview_(placeholder)
            self._task_views.append(placeholder)
            self._tasks_header_lbl.setStringValue_("Ready Tasks")
            self._tasks_nav_row.setHidden_(True)
            return

        ready_count = len(shown)
        self._tasks_header_lbl.setStringValue_(f"Ready Tasks ({ready_count})")

        total_pages = max(1, (len(shown) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._tasks_total_pages = total_pages
        self._tasks_page = min(self._tasks_page, total_pages - 1)

        start = self._tasks_page * _PAGE_SIZE
        page_tasks = shown[start:start + _PAGE_SIZE]

        # Collapse expanded task if it's not on this page
        if self._expanded_task_id and not any(
            t.task_id == self._expanded_task_id for t in page_tasks
        ):
            self._expanded_task_id = None

        for task in page_tasks:
            is_expanded = (task.task_id == self._expanded_task_id)
            row_view = self._make_task_row(task, is_expanded, running_ids)
            self._tasks_stack.addArrangedSubview_(row_view)
            self._task_views.append(row_view)

        self._update_pagination_nav(
            self._tasks_nav_row,
            self._tasks_prev_btn,
            self._tasks_next_btn,
            self._tasks_page_lbl,
            self._tasks_page,
            total_pages,
        )

    @objc.python_method
    def _make_task_row(self, task: Any, expanded: bool, running_ids: set) -> NSView:
        """Build one task row (collapsed or expanded).

        Layout: [priority] [id] [title — clickable to expand] [▶ Run | ⚙ Running…]
        Expanded: description view shown below the title row (no separate action buttons).
        """
        container = NSStackView.alloc().init()
        container.setOrientation_(1)
        container.setAlignment_(9)
        container.setSpacing_(4.0)

        is_running = task.task_id in running_ids

        title_row = NSStackView.alloc().init()
        title_row.setOrientation_(0)
        title_row.setSpacing_(6.0)

        pri_badge = make_label(f"[{task.priority}]", size=11.0, secondary=True)
        id_lbl = make_label(f"{task.task_id}", size=11.0, bold=True)

        # Clickable title button — borderless, looks like a label
        title_btn = NSButton.buttonWithTitle_target_action_(
            task.title[:50], self, "_toggleTask:"
        )
        title_btn.setRepresentedObject_(task.task_id)
        title_btn.setBordered_(False)
        title_btn.setBezelStyle_(0)
        title_btn.setFont_(NSFont.systemFontOfSize_(12.0))
        title_btn.setAlignment_(0)   # NSTextAlignmentLeft
        title_btn.setLineBreakMode_(4)   # NSLineBreakByTruncatingTail
        title_btn.setContentCompressionResistancePriority_forOrientation_(249, 0)

        title_row.addArrangedSubview_(pri_badge)
        title_row.addArrangedSubview_(id_lbl)
        title_row.addArrangedSubview_(title_btn)

        if is_running:
            status_lbl = make_label("⚙ Running…", size=11.0, secondary=True)
            title_row.addArrangedSubview_(status_lbl)
        else:
            run_btn = _make_button("▶ Run", self, "_runTask:")
            run_btn.setRepresentedObject_(task.task_id)
            title_row.addArrangedSubview_(run_btn)

        container.addArrangedSubview_(title_row)

        if expanded and not is_running:
            desc = getattr(task, "_cached_desc", task.title)
            desc_view = _make_desc_scroll_view(_extract_description(desc))
            container.addArrangedSubview_(desc_view)

        return container

    # ── Agents section ─────────────────────────────────────────────────────

    @objc.python_method
    def _rebuild_agents_section(self, agents: list) -> None:
        for v in self._agent_views:
            v.removeFromSuperview()
        self._agent_views = []

        hidden = not agents
        self._agents_header_lbl.setHidden_(hidden)
        self._agents_stack.setHidden_(hidden)
        self._agents_sep.setHidden_(hidden)

        if hidden:
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

        self._update_pagination_nav(
            self._agents_nav_row,
            self._agents_prev_btn,
            self._agents_next_btn,
            self._agents_page_lbl,
            self._agents_page,
            total_pages,
        )

    @objc.python_method
    def _make_agent_row(self, agent: dict) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)

        task_id = agent.get("task_id", "?")
        title = agent.get("title", "")[:40]
        lbl = make_label(f"⚙ {task_id} · {title}", size=12.0)
        lbl.setContentCompressionResistancePriority_forOrientation_(250, 0)

        log_btn = _make_button("Control", self, "_controlAgent:")
        log_btn.setRepresentedObject_(agent.get("session", ""))
        stop_btn = _make_button("■ Stop", self, "_stopAgent:")
        stop_btn.setRepresentedObject_(agent.get("task_id", ""))

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(log_btn)
        row.addArrangedSubview_(stop_btn)
        return row

    # ── Header helpers ─────────────────────────────────────────────────────

    @objc.python_method
    def _update_last_refresh_label(self) -> None:
        if self._last_refresh_lbl is None:
            return
        if self._last_refresh_at is None:
            self._last_refresh_lbl.setStringValue_("—")
            return
        delta = int((datetime.now(timezone.utc) - self._last_refresh_at).total_seconds())
        if delta < 60:
            text = "just now"
        elif delta < 3600:
            text = f"{delta // 60} min ago"
        else:
            hrs = delta // 3600
            mins = (delta % 3600) // 60
            text = f"{hrs}h {mins}m ago"
        self._last_refresh_lbl.setStringValue_(f"last refresh {text}")

    # ── Completed section ──────────────────────────────────────────────────

    @objc.python_method
    def _make_completed_header_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(2)  # NSStackViewDistributionEqualSpacing
        lbl = make_label("Recently Completed", size=11.0, secondary=True)
        clear_btn = _make_button("Clear All", self, "_clearAllCompleted:")
        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(clear_btn)
        return row

    @objc.python_method
    def _rebuild_completed_section(self, completed: list) -> None:
        for v in self._completed_views:
            v.removeFromSuperview()
        self._completed_views = []

        hidden = not completed
        self._completed_header_row.setHidden_(hidden)
        self._completed_stack.setHidden_(hidden)
        self._completed_sep.setHidden_(hidden)

        if hidden:
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

        self._update_pagination_nav(
            self._completed_nav_row,
            self._completed_prev_btn,
            self._completed_next_btn,
            self._completed_page_lbl,
            self._completed_page,
            total_pages,
        )

    @objc.python_method
    def _make_completed_row(self, agent: dict) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(6.0)
        row.setDistribution_(0)

        task_id = agent.get("task_id", "?")
        title = agent.get("title", "")[:38]
        project = agent.get("project", "")
        lbl = make_label(f"\u2713 {task_id} \u00b7 {title} ({project})", size=12.0)
        lbl.setContentCompressionResistancePriority_forOrientation_(249, 0)

        dismiss_btn = _make_button("\u2715", self, "_dismissCompleted:")
        dismiss_btn.setRepresentedObject_(task_id)

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(dismiss_btn)
        return row

    def _dismissCompleted_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id and self._app:
            self._app.dismissCompleted_(task_id)

    def _clearAllCompleted_(self, sender: Any) -> None:
        if self._app:
            self._app.clearAllCompleted_(sender)

    # ── Layout pass ────────────────────────────────────────────────────────

    @objc.python_method
    def _relayout(self) -> None:
        """Resize the popover to fit the current content."""
        if self.view() is None:
            return
        self.view().layoutSubtreeIfNeeded()
        size = self._root_stack.fittingSize()
        new_height = max(200.0, size.height)
        self.view().setFrameSize_((_WIDTH, new_height))
        if self._app and hasattr(self._app, "_popover"):
            self._app._popover.setContentSize_((_WIDTH, new_height))

    # ── Pagination action selectors ─────────────────────────────────────────

    def _prevTasksPage_(self, sender: Any) -> None:
        if self._tasks_page > 0:
            self._tasks_page -= 1
            self._rebuild_tasks_section(self._all_ready_tasks, self._agents_running)
            self._relayout()

    def _nextTasksPage_(self, sender: Any) -> None:
        if self._tasks_page < self._tasks_total_pages - 1:
            self._tasks_page += 1
            self._rebuild_tasks_section(self._all_ready_tasks, self._agents_running)
            self._relayout()

    def _prevAgentsPage_(self, sender: Any) -> None:
        if self._agents_page > 0:
            self._agents_page -= 1
            self._rebuild_agents_section(self._agents_running)
            self._relayout()

    def _nextAgentsPage_(self, sender: Any) -> None:
        if self._agents_page < self._agents_total_pages - 1:
            self._agents_page += 1
            self._rebuild_agents_section(self._agents_running)
            self._relayout()

    def _prevCompletedPage_(self, sender: Any) -> None:
        if self._completed_page > 0:
            self._completed_page -= 1
            self._rebuild_completed_section(self._state.get("recently_completed", []))
            self._relayout()

    def _nextCompletedPage_(self, sender: Any) -> None:
        if self._completed_page < self._completed_total_pages - 1:
            self._completed_page += 1
            self._rebuild_completed_section(self._state.get("recently_completed", []))
            self._relayout()

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
        """Toggle task expansion. Task ID is stored in representedObject."""
        task_id = str(sender.representedObject() or "")
        if not task_id:
            return
        if self._expanded_task_id == task_id:
            self._expanded_task_id = None
        else:
            self._expanded_task_id = task_id
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
        parts = title.rsplit(" ", 1)
        return parts[-1] if parts else None

    def _runTask_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        task = next((t for t in self._all_ready_tasks if t.task_id == task_id), None)
        if task and self._app:
            self._app.spawnTask_(task)

    @objc.python_method
    def _collapse_and_act(self, task_id: str, args: list, project_path: str) -> None:
        """Collapse the expanded row immediately, then dispatch the bd action."""
        self._expanded_task_id = None
        self._rebuild_tasks_section(self._all_ready_tasks, self._agents_running)
        self._relayout()
        if self._app:
            self._app.runBdAction_((args, project_path))

    def _deferTask1h_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id:
            self._collapse_and_act(task_id, ["defer", task_id, "--until", "+1h"],
                                   self._task_project_path(task_id))

    def _deferTask4h_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id:
            self._collapse_and_act(task_id, ["defer", task_id, "--until", "+4h"],
                                   self._task_project_path(task_id))

    def _deferTaskTomorrow_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id:
            self._collapse_and_act(task_id, ["defer", task_id, "--until", "tomorrow"],
                                   self._task_project_path(task_id))

    def _claimTask_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id:
            self._collapse_and_act(task_id, ["update", task_id, "--claim"],
                                   self._task_project_path(task_id))

    def _closeTask_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if task_id:
            self._all_ready_tasks = [t for t in self._all_ready_tasks if t.task_id != task_id]
            self._collapse_and_act(task_id, ["close", task_id],
                                   self._task_project_path(task_id))

    @objc.python_method
    def _task_project_path(self, task_id: str) -> str:
        tid = str(task_id)
        task = next((t for t in self._all_ready_tasks if t.task_id == tid), None)
        return task.project_path if task else ""

    def _controlAgent_(self, sender: Any) -> None:
        session = str(sender.representedObject() or "")
        if not session:
            return
        script = (
            'tell application "Terminal" to activate\n'
            f'tell application "Terminal" to do script "screen -x {shlex.quote(session)}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)

    def _stopAgent_(self, sender: Any) -> None:
        task_id = str(sender.representedObject() or "")
        if not task_id:
            return
        sender.setTitle_("Stopping\u2026")
        sender.setEnabled_(False)
        if self._app:
            self._app.stopAgentByTaskId_(task_id)
