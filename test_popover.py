#!/usr/bin/env python3
"""Visual layout test for the Penny popover.

Shows the ControlCenterViewController in a standalone NSWindow with dummy data
so you can verify the layout without starting the full menu bar app.

Usage:
    python3 test_popover.py
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject
import objc

from penny.popover_vc import ControlCenterViewController
from penny.analysis import Prediction


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notif):
        NSApplication.sharedApplication().setActivationPolicy_(0)  # Regular

        # Build a window to host the VC view
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(100, 200, 340, 600), style, NSBackingStoreBuffered, False
        )
        self._win.setTitle_("Penny — Popover Test")

        vc = ControlCenterViewController.alloc().init()
        vc.loadView()
        self._win.setContentViewController_(vc)
        self._win.makeKeyAndOrderFront_(None)

        # Feed dummy data
        pred = Prediction(
            pct_all=72.0,
            pct_sonnet=58.0,
            session_pct_all=31.0,
            reset_label="Mar 6 at 9pm",
            session_reset_label="2pm",
            days_remaining=2.3,
            session_hours_remaining=3.5,
            will_trigger=True,
            projected_pct_all=85.0,
        )

        from penny.tasks import Task
        dummy_tasks = [
            Task(
                task_id="SD-g3jj",
                title="Add missing seed data",
                priority="P1",
                project_path="/tmp",
                project_name="SetDigger",
            ),
            Task(
                task_id="NK-a4bc",
                title="Implement onboarding flow",
                priority="P2",
                project_path="/tmp",
                project_name="niksen",
            ),
        ]

        dummy_agents = [
            {
                "task_id": "SD-x1ab",
                "title": "Seed database records",
                "project": "SetDigger",
                "pid": 12345,
                "log": "/tmp/agent-SD-x1ab.log",
            }
        ]

        data = {
            "prediction": pred,
            "state": {"agents_running": dummy_agents},
            "ready_tasks": dummy_tasks,
        }
        vc.updateWithData_(data)

        print("Popover test window open. Close it to quit.")

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
