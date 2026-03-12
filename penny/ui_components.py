"""Custom NSView subclasses and label factories for the Penny popover UI."""

from __future__ import annotations

from typing import Any

import objc
from AppKit import (
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSTextField,
    NSView,
)


class ProgressBarView(NSView):
    """Rounded progress bar rendered via drawRect_.

    Color thresholds:
      green  < 60 %
      yellow 60–80 %
      red    ≥ 80 %

    Call ``setPct_()`` to update and trigger a repaint.
    """

    def initWithFrame_(self, frame: Any) -> ProgressBarView:
        self = objc.super(ProgressBarView, self).initWithFrame_(frame)
        if self is None:
            return self
        self._pct: float = 0.0
        return self

    def setFrameSize_(self, new_size: Any) -> None:
        objc.super(ProgressBarView, self).setFrameSize_(new_size)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def setPct(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, pct))
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_fixed_color(self, color: Any) -> None:
        """Use a fixed fill color instead of traffic-light thresholds."""
        self._fixed_color = color
        self.setNeedsDisplay_(True)

    # ObjC selector variant (used from popover_vc via performSelector)
    def setPct_(self, pct: float) -> None:
        self.setPct(pct)

    def drawRect_(self, rect: Any) -> None:
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        radius = h / 2.0

        # Track (background)
        track_color = NSColor.tertiaryLabelColor()
        track_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius
        )
        track_color.setFill()
        track_path.fill()

        # Fill (foreground)
        fill_w = max(h, (self._pct / 100.0) * w)  # at least a circle
        fill_rect = ((0, 0), (fill_w, h))

        pct = self._pct
        fixed = getattr(self, "_fixed_color", None)
        if fixed is not None:
            fill_color = fixed
        elif pct < 60:
            fill_color = NSColor.systemGreenColor()
        elif pct < 80:
            fill_color = NSColor.systemYellowColor()
        else:
            fill_color = NSColor.systemRedColor()

        fill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            fill_rect, radius, radius
        )
        fill_color.setFill()
        fill_path.fill()


def make_button(title: str, target: Any, action: str, small: bool = True) -> NSButton:
    """Return a styled NSButton for use in popover rows."""
    btn = NSButton.buttonWithTitle_target_action_(title, target, action)
    if small:
        btn.setControlSize_(1)   # NSControlSizeSmall
        btn.setFont_(NSFont.systemFontOfSize_(11.0))
    btn.setBezelStyle_(4)        # NSBezelStyleRounded
    return btn


def make_label(text: str = "", size: float = 13.0, bold: bool = False,
               secondary: bool = False) -> NSTextField:
    """Return a non-editable, non-selectable NSTextField label."""
    field = NSTextField.labelWithString_(text)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    if bold:
        field.setFont_(NSFont.boldSystemFontOfSize_(size))
    else:
        field.setFont_(NSFont.systemFontOfSize_(size))
    if secondary:
        field.setTextColor_(NSColor.secondaryLabelColor())
    else:
        field.setTextColor_(NSColor.labelColor())
    return field
