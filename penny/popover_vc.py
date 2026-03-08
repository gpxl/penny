"""Control Center popover view controller for Penny.

Builds the entire UI programmatically — no NIB/XIB required.
Layout via NSStackView. Live updates via updateWithData_().
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import objc
from AppKit import (
    NSButton,
    NSColor,
    NSFont,
    NSLayoutConstraint,
    NSStackView,
    NSTextField,
    NSView,
    NSViewController,
)
from Foundation import NSEdgeInsets

from .analysis import format_reset_label
from .ui_components import ProgressBarView, make_button, make_label

# Popover width (fixed). Height is dynamic.
_WIDTH: float = 380.0
_PADDING: float = 16.0
_BAR_HEIGHT: float = 8.0
_SECTION_SPACING: float = 10.0


def _make_separator() -> NSView:
    from AppKit import NSBox
    sep = NSBox.alloc().initWithFrame_(((0, 0), (_WIDTH - _PADDING * 2, 1)))
    sep.setBoxType_(2)   # NSBoxSeparator
    return sep


class ControlCenterViewController(NSViewController):
    """Popover view controller built entirely in code."""

    def init(self) -> ControlCenterViewController:
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
        self._lbl_outage_warning: NSTextField | None = None
        # UI state
        self._refresh_btn: Any = None
        self._keep_alive_btn: Any = None
        self._login_btn: Any = None
        self._last_refresh_at: datetime | None = None
        self._last_refresh_lbl: NSTextField | None = None
        self._app: Any = None      # set by app.py after construction
        self._config: dict = {}
        self._state: dict = {}
        # Plugin UI sections
        self._plugin_section_views: list[Any] = []
        self._plugin_sections: list[Any] = []  # UISection instances
        # Plugins management section
        self._plugins_section_stack: Any = None
        self._plugin_row_views: list[Any] = []
        return self

    def loadView(self) -> None:
        """Build the full popover UI."""
        outer = NSView.alloc().initWithFrame_(((0, 0), (_WIDTH, 500)))
        self.setView_(outer)
        self._build(outer)
        self._insert_plugin_sections()

    # ── Public update API ──────────────────────────────────────────────────

    def updateWithData_(self, data: dict) -> None:
        """Refresh UI from fresh data dict. Must be called on the main thread."""
        pred = data.get("prediction")
        state = data.get("state", {})
        fetched_at = data.get("fetched_at")
        if fetched_at is not None:
            self._last_refresh_at = fetched_at
        self._update_last_refresh_label()

        self._state = state

        # Guard: views not yet created (loadView not called yet)
        if self._bar_all is None:
            return

        if self._keep_alive_btn is not None and self._app is not None:
            svc = getattr(self._app, "config", {}).get("service", {})
            self._keep_alive_btn.setState_(1 if svc.get("keep_alive", True) else 0)
            self._login_btn.setState_(1 if svc.get("launch_at_login", True) else 0)

        if pred:
            self._bar_all.setPct(pred.pct_all)
            self._bar_sonnet.setPct(pred.pct_sonnet)
            self._bar_session.setPct(pred.session_pct_all)
            self._lbl_all_pct.setStringValue_(f"{pred.pct_all:.0f}%")
            self._lbl_sonnet_pct.setStringValue_(f"{pred.pct_sonnet:.0f}%")
            self._lbl_session_pct.setStringValue_(f"{pred.session_pct_all:.0f}%")
            self._lbl_weekly_reset.setStringValue_(
                f"Resets at {format_reset_label(pred.reset_label)}" if pred.reset_label else "—"
            )
            self._lbl_session_reset.setStringValue_(
                f"Resets at {format_reset_label(pred.session_reset_label)}" if pred.session_reset_label else "—"
            )
            if self._lbl_outage_warning is not None:
                if pred.outage:
                    self._lbl_outage_warning.setStringValue_(
                        "\u26a0\ufe0f Claude API outage \u2014 usage data may be stale"
                    )
                    self._lbl_outage_warning.setHidden_(False)
                elif pred.live_unavailable and pred.budget_all is None:
                    self._lbl_outage_warning.setStringValue_(
                        "Calibrating \u2014 need 1\u20132 weeks of usage history for budget estimates"
                    )
                    self._lbl_outage_warning.setHidden_(False)
                elif pred.live_unavailable:
                    self._lbl_outage_warning.setStringValue_(
                        "Live stats unavailable \u2014 showing JSONL estimates"
                    )
                    self._lbl_outage_warning.setHidden_(False)
                else:
                    self._lbl_outage_warning.setHidden_(True)

        # Refresh plugin sections
        for section in self._plugin_sections:
            try:
                section.rebuild(data)
            except Exception as exc:
                print(f"[penny] plugin section rebuild error: {exc}", flush=True)

        # Refresh plugins management checkboxes
        self._rebuild_plugins_section()

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
        for attr, _val in [
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

        # ── Outage warning (hidden by default) ───────────────────────────────
        self._lbl_outage_warning = make_label("", size=11.0)
        self._lbl_outage_warning.setTextColor_(NSColor.systemOrangeColor())
        self._lbl_outage_warning.setHidden_(True)
        stack.addArrangedSubview_(self._lbl_outage_warning)

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

        # ── Plugin section insertion point ─────────────────────────────────────
        # Plugin sections (e.g. Beads task lists) are inserted here dynamically.
        self._plugin_insertion_index = len(stack.arrangedSubviews())

        # ── Plugins management ────────────────────────────────────────────────
        plugins_view = self._make_plugins_section()
        stack.addArrangedSubview_(plugins_view)

        # ── Service settings ─────────────────────────────────────────────────
        stack.addArrangedSubview_(_make_separator())
        stack.addArrangedSubview_(self._make_service_row())

        # ── Footer ───────────────────────────────────────────────────────────
        footer = self._make_footer_row()
        stack.addArrangedSubview_(footer)

    @objc.python_method
    def _insert_plugin_sections(self) -> None:
        """Build and insert plugin-contributed UI sections into the stack."""
        if self._app is None:
            return
        mgr = getattr(self._app, "_plugin_mgr", None)
        if mgr is None:
            return

        # Remove any previously inserted plugin views
        for view in self._plugin_section_views:
            self._root_stack.removeArrangedSubview_(view)
            view.removeFromSuperview()
        self._plugin_section_views = []
        self._plugin_sections = []

        sections = mgr.get_all_ui_sections()
        if not sections:
            return

        insert_idx = self._plugin_insertion_index
        for section in sections:
            view = section.build_view()
            if view is not None:
                self._root_stack.insertArrangedSubview_atIndex_(view, insert_idx)
                self._plugin_section_views.append(view)
                self._plugin_sections.append(section)
                insert_idx += 1
                # Add separator after each plugin section
                sep = _make_separator()
                self._root_stack.insertArrangedSubview_atIndex_(sep, insert_idx)
                self._plugin_section_views.append(sep)
                insert_idx += 1

    @objc.python_method
    def rebuild_plugin_sections(self) -> None:
        """Re-discover and rebuild plugin sections (e.g. after config change)."""
        self._insert_plugin_sections()
        self._rebuild_plugins_section()
        self._relayout()

    @objc.python_method
    def _make_header_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)   # horizontal
        row.setSpacing_(8.0)
        row.setDistribution_(3)  # NSStackViewDistributionEqualSpacing

        refresh_btn = make_button("↻ Refresh", self, "refreshNow:")
        last_refresh_lbl = make_label("—", size=11.0, secondary=True)
        last_refresh_lbl.setAlignment_(2)  # NSTextAlignmentRight

        row.addArrangedSubview_(refresh_btn)
        row.addArrangedSubview_(last_refresh_lbl)
        self._refresh_btn = refresh_btn
        self._last_refresh_lbl = last_refresh_lbl
        return row

    @objc.python_method
    def _make_plugins_section(self) -> NSView:
        """Create the Plugins management section (header + checkbox rows)."""
        outer = NSStackView.alloc().init()
        outer.setOrientation_(1)
        outer.setAlignment_(9)
        outer.setSpacing_(6.0)
        outer.addArrangedSubview_(make_label("Plugins", size=11.0, secondary=True))
        self._plugins_section_stack = outer
        return outer

    @objc.python_method
    def _rebuild_plugins_section(self) -> None:
        """Rebuild plugin checkbox rows from the current plugin manager state."""
        if self._plugins_section_stack is None or self._app is None:
            return
        mgr = getattr(self._app, "_plugin_mgr", None)
        if mgr is None:
            return

        # Remove old rows
        for view in self._plugin_row_views:
            self._plugins_section_stack.removeArrangedSubview_(view)
            view.removeFromSuperview()
        self._plugin_row_views = []

        active_names = {p.name for p in mgr.active_plugins}

        for name, plugin in mgr.all_plugins.items():
            row = NSStackView.alloc().init()
            row.setOrientation_(0)
            row.setSpacing_(8.0)
            row.setDistribution_(0)

            checkbox = NSButton.alloc().init()
            checkbox.setButtonType_(3)   # NSButtonTypeSwitch
            checkbox.setTitle_("")
            checkbox.setState_(1 if name in active_names else 0)
            checkbox.setTarget_(self)
            checkbox.setAction_("_togglePlugin:")
            checkbox.setRepresentedObject_(name)

            name_lbl = make_label(plugin.name, size=12.0, bold=True)
            desc_lbl = make_label(plugin.description, size=11.0, secondary=True)
            desc_lbl.setContentCompressionResistancePriority_forOrientation_(249, 0)

            row.addArrangedSubview_(checkbox)
            row.addArrangedSubview_(name_lbl)
            row.addArrangedSubview_(desc_lbl)

            self._plugins_section_stack.addArrangedSubview_(row)
            self._plugin_row_views.append(row)

    def _togglePlugin_(self, sender: Any) -> None:
        """Toggle a plugin on or off via the plugins management checkboxes."""
        plugin_name = str(sender.representedObject() or "")
        if not plugin_name or not self._app:
            return
        self._app.set_plugin_enabled(plugin_name, bool(sender.state()))

    @objc.python_method
    def _make_service_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)   # horizontal
        row.setSpacing_(12.0)
        row.setDistribution_(0)

        keep_alive_btn = NSButton.alloc().init()
        keep_alive_btn.setButtonType_(3)        # NSButtonTypeSwitch (checkbox)
        keep_alive_btn.setTitle_("Auto-restart")
        keep_alive_btn.setTarget_(self._app)
        keep_alive_btn.setAction_("toggleKeepAlive:")
        keep_alive_btn.setFont_(NSFont.systemFontOfSize_(12.0))

        login_btn = NSButton.alloc().init()
        login_btn.setButtonType_(3)
        login_btn.setTitle_("Launch at login")
        login_btn.setTarget_(self._app)
        login_btn.setAction_("toggleLaunchAtLogin:")
        login_btn.setFont_(NSFont.systemFontOfSize_(12.0))

        row.addArrangedSubview_(keep_alive_btn)
        row.addArrangedSubview_(login_btn)

        self._keep_alive_btn = keep_alive_btn
        self._login_btn      = login_btn
        return row

    @objc.python_method
    def _make_footer_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(0)

        report_btn = make_button("View Report", self, "viewReport:")
        prefs_btn = make_button("Preferences", self, "openPrefs:")
        quit_btn = make_button("Quit", self, "quitApp:")

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
