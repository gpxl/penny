"""Control Center popover view controller for Penny.

Builds the entire UI programmatically — no NIB/XIB required.
Layout via NSStackView. Live updates via updateWithData_().
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import objc
from AppKit import (
    NSColor,
    NSLayoutConstraint,
    NSStackView,
    NSTextField,
    NSView,
    NSViewController,
)
from Foundation import NSEdgeInsets, NSTimer

from .analysis import format_reset_label, short_reset_label
from .ui_components import ProgressBarView, make_button, make_label

_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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
        self._lbl_all_reset: NSTextField | None = None
        self._lbl_sonnet_reset: NSTextField | None = None
        self._lbl_session_reset: NSTextField | None = None
        self._lbl_outage_warning: NSTextField | None = None
        # UI state
        self._refresh_btn: Any = None
        self._spin_timer: Any = None
        self._spin_frame: int = 0
        self._last_refresh_at: datetime | None = None
        self._last_refresh_lbl: NSTextField | None = None
        self._app: Any = None      # set by app.py after construction
        self._config: dict = {}
        self._state: dict = {}
        # Plugin UI sections
        self._plugin_section_views: list[Any] = []
        self._plugin_sections: list[Any] = []  # UISection instances
        # Update banner
        self._update_banner: Any = None
        self._update_lbl: NSTextField | None = None
        return self

    def loadView(self) -> None:
        """Build the full popover UI."""
        outer = NSView.alloc().initWithFrame_(((0, 0), (_WIDTH, 300)))
        self.setView_(outer)
        self._build(outer)
        self._insert_plugin_sections()
        self._relayout()

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

        if pred:
            self._bar_all.setPct(pred.pct_all)
            self._bar_sonnet.setPct(pred.pct_sonnet)
            self._bar_session.setPct(pred.session_pct_all)
            self._lbl_all_pct.setStringValue_(f"{pred.pct_all:.0f}%")
            self._lbl_sonnet_pct.setStringValue_(f"{pred.pct_sonnet:.0f}%")
            self._lbl_session_pct.setStringValue_(f"{pred.session_pct_all:.0f}%")
            # Inline reset labels (compact, in each bar row)
            if pred.session_reset_label:
                formatted = format_reset_label(pred.session_reset_label)
                self._lbl_session_reset.setStringValue_(formatted)
                self._lbl_session_reset.setToolTip_(f"Resets at {formatted}")
            if pred.reset_label:
                full = format_reset_label(pred.reset_label)
                self._lbl_all_reset.setStringValue_(short_reset_label(full))
                self._lbl_all_reset.setToolTip_(f"Resets at {full}")
            if pred.reset_label_sonnet:
                full = format_reset_label(pred.reset_label_sonnet)
                self._lbl_sonnet_reset.setStringValue_(short_reset_label(full))
                self._lbl_sonnet_reset.setToolTip_(f"Resets at {full}")
            if self._lbl_outage_warning is not None:
                if pred.outage:
                    self._lbl_outage_warning.setStringValue_(
                        "\u26a0\ufe0f Claude API outage \u2014 usage data may be stale"
                    )
                    self._lbl_outage_warning.setHidden_(False)
                else:
                    self._lbl_outage_warning.setHidden_(True)

        # Update banner visibility
        if self._update_banner is not None:
            uc = data.get("update_check") or {}
            latest = uc.get("latest_version", "")
            from .update_checker import is_dismissed
            if uc.get("update_available") and not is_dismissed(state, latest):
                self._update_lbl.setStringValue_(f"Update available: v{latest}")
                self._update_banner.setHidden_(False)
            else:
                self._update_banner.setHidden_(True)

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
        stack = NSStackView.alloc().initWithFrame_(((0, 0), (_WIDTH, 300)))
        stack.setOrientation_(1)      # NSUserInterfaceLayoutOrientationVertical
        stack.setAlignment_(5)        # NSLayoutAttributeLeading
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
        # ── Outage warning (hidden by default) ───────────────────────────────
        self._lbl_outage_warning = make_label("", size=11.0)
        self._lbl_outage_warning.setTextColor_(NSColor.systemOrangeColor())
        self._lbl_outage_warning.setHidden_(True)
        stack.addArrangedSubview_(self._lbl_outage_warning)

        # ── Session Budget ───────────────────────────────────────────────────
        self._bar_session, self._lbl_session_pct, self._lbl_session_reset = (
            self._add_bar_row(stack, "Session", 0.0)
        )
        stack.addArrangedSubview_(_make_separator())

        # ── Weekly Budget ────────────────────────────────────────────────────
        self._bar_all, self._lbl_all_pct, self._lbl_all_reset = (
            self._add_bar_row(stack, "All models", 0.0)
        )
        self._bar_sonnet, self._lbl_sonnet_pct, self._lbl_sonnet_reset = (
            self._add_bar_row(stack, "Sonnet", 0.0)
        )

        stack.addArrangedSubview_(_make_separator())

        # ── Plugin section insertion point ─────────────────────────────────────
        # Plugin sections (e.g. Beads task lists) are inserted here dynamically.
        self._plugin_insertion_index = len(stack.arrangedSubviews())

        # ── Update available banner (hidden by default) ───────────────────────
        update_banner = self._make_update_banner()
        update_banner.setHidden_(True)
        self._update_banner = update_banner
        stack.addArrangedSubview_(update_banner)

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
        self._relayout()

    @objc.python_method
    def _make_update_banner(self) -> NSView:
        """Create the update-available banner row."""
        row = NSStackView.alloc().init()
        row.setOrientation_(0)   # horizontal
        row.setSpacing_(8.0)
        row.setDistribution_(3)  # NSStackViewDistributionEqualSpacing
        row.setTranslatesAutoresizingMaskIntoConstraints_(False)
        row.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)

        lbl = make_label("Update available", size=11.0)
        lbl.setTextColor_(NSColor.systemBlueColor())
        self._update_lbl = lbl

        btn_row = NSStackView.alloc().init()
        btn_row.setOrientation_(0)
        btn_row.setSpacing_(6.0)

        update_btn = make_button("Update", self, "updateNow:")
        dismiss_btn = make_button("\u00d7", self, "dismissUpdate:")

        btn_row.addArrangedSubview_(update_btn)
        btn_row.addArrangedSubview_(dismiss_btn)

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(btn_row)
        return row

    @objc.python_method
    def _make_footer_row(self) -> NSView:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(3)   # NSStackViewDistributionEqualSpacing
        row.setTranslatesAutoresizingMaskIntoConstraints_(False)
        row.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)

        # Left: refresh button + stacked last-refresh text
        left = NSStackView.alloc().init()
        left.setOrientation_(0)
        left.setSpacing_(6.0)
        left.setAlignment_(8)    # NSLayoutAttributeCenterY

        refresh_btn = make_button("↻", self, "refreshNow:")
        self._refresh_btn = refresh_btn

        # Two-line vertical stack: "last refresh" (static) / time (dynamic)
        refresh_text = NSStackView.alloc().init()
        refresh_text.setOrientation_(1)   # vertical
        refresh_text.setSpacing_(1.0)
        refresh_text.setAlignment_(5)     # NSLayoutAttributeLeading
        refresh_text.addArrangedSubview_(make_label("last refresh", size=10.0, secondary=True))
        last_refresh_lbl = make_label("—", size=10.0, secondary=True)
        self._last_refresh_lbl = last_refresh_lbl
        refresh_text.addArrangedSubview_(last_refresh_lbl)

        left.addArrangedSubview_(refresh_btn)
        left.addArrangedSubview_(refresh_text)

        # Right: dashboard, quit
        right = NSStackView.alloc().init()
        right.setOrientation_(0)
        right.setSpacing_(8.0)

        report_btn = make_button("Dashboard", self, "viewReport:")
        quit_btn = make_button("Quit", self, "quitApp:")
        quit_btn.setContentTintColor_(NSColor.systemRedColor())

        right.addArrangedSubview_(report_btn)
        right.addArrangedSubview_(quit_btn)

        row.addArrangedSubview_(left)
        row.addArrangedSubview_(right)

        self._footer_btns = [report_btn, quit_btn]
        return row

    def setRefreshing_(self, refreshing: bool) -> None:
        """Start/stop the braille spinner animation on the refresh button."""
        if self._refresh_btn is None:
            return
        if refreshing:
            self._refresh_btn.setEnabled_(False)
            if self._spin_timer is None:
                self._spin_frame = 0
                self._refresh_btn.setTitle_(_SPIN_FRAMES[0])
                self._spin_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.1, self, "_tickSpinner:", None, True
                )
        else:
            if self._spin_timer is not None:
                self._spin_timer.invalidate()
                self._spin_timer = None
            self._refresh_btn.setTitle_("↻")
            self._refresh_btn.setEnabled_(True)

    def _tickSpinner_(self, timer: Any) -> None:
        """Advance the braille spinner by one frame."""
        if self._refresh_btn is None:
            return
        self._spin_frame = (self._spin_frame + 1) % len(_SPIN_FRAMES)
        self._refresh_btn.setTitle_(_SPIN_FRAMES[self._spin_frame])

    @objc.python_method
    def _add_bar_row(self, stack: NSStackView, label: str,
                     initial_pct: float) -> tuple[ProgressBarView, NSTextField, NSTextField]:
        row = NSStackView.alloc().init()
        row.setOrientation_(0)
        row.setSpacing_(8.0)
        row.setDistribution_(0)
        row.setTranslatesAutoresizingMaskIntoConstraints_(False)
        row.widthAnchor().constraintEqualToConstant_(_WIDTH - _PADDING * 2).setActive_(True)

        lbl = make_label(label, size=12.0)
        lbl.setTranslatesAutoresizingMaskIntoConstraints_(False)
        lbl.widthAnchor().constraintEqualToConstant_(80.0).setActive_(True)

        bar = ProgressBarView.alloc().initWithFrame_(((0, 0), (100.0, _BAR_HEIGHT)))
        bar.setPct(initial_pct)
        bar.setTranslatesAutoresizingMaskIntoConstraints_(False)
        bar.setContentHuggingPriority_forOrientation_(1, 0)  # stretch to fill available width
        bar.heightAnchor().constraintEqualToConstant_(_BAR_HEIGHT).setActive_(True)

        pct_lbl = make_label(f"{initial_pct:.0f}%", size=12.0)
        pct_lbl.setAlignment_(1)  # NSTextAlignmentRight
        pct_lbl.setTranslatesAutoresizingMaskIntoConstraints_(False)
        pct_lbl.widthAnchor().constraintEqualToConstant_(36.0).setActive_(True)

        reset_lbl = make_label("", size=9.0, secondary=True)
        reset_lbl.setAlignment_(1)  # NSTextAlignmentRight
        reset_lbl.setTranslatesAutoresizingMaskIntoConstraints_(False)
        reset_lbl.widthAnchor().constraintEqualToConstant_(60.0).setActive_(True)

        row.addArrangedSubview_(lbl)
        row.addArrangedSubview_(bar)
        row.addArrangedSubview_(pct_lbl)
        row.addArrangedSubview_(reset_lbl)
        stack.addArrangedSubview_(row)
        return bar, pct_lbl, reset_lbl

    # ── Footer helpers ─────────────────────────────────────────────────────

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
        self._last_refresh_lbl.setStringValue_(text)

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

    def updateNow_(self, sender: Any) -> None:
        if self._app:
            self._app.runUpdate_(sender)

    def dismissUpdate_(self, sender: Any) -> None:
        if self._app:
            self._app.dismissUpdate_(sender)

