"""macOS menu-bar integration: a status-bar icon with a small menu, plus
native notifications that activate a callback when the user clicks them.

If PyObjC isn't available, the public API still works as a no-op so the
rest of the app keeps functioning.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

try:
    from AppKit import (
        NSStatusBar,
        NSMenu,
        NSMenuItem,
        NSUserNotification,
        NSUserNotificationCenter,
        NSImage,
    )
    from Foundation import NSObject
    import objc  # noqa: F401
    PYOBJC_OK = True
except ImportError:
    PYOBJC_OK = False


def _find_menubar_icon() -> Optional[str]:
    """Locate menubar-icon@2x.png across source-tree, PyInstaller _MEIPASS,
    and bundle Resources directory."""
    candidates: list[Path] = []
    here = Path(__file__).resolve().parent
    candidates.append(here / "menubar-icon@2x.png")
    candidates.append(here / "menubar-icon.png")
    if getattr(sys, "_MEIPASS", ""):
        meipass = Path(sys._MEIPASS)
        candidates.append(meipass / "menubar-icon@2x.png")
        candidates.append(meipass / "menubar-icon.png")
    if getattr(sys, "frozen", False) and sys.executable:
        # When bundled: .../Granola Export.app/Contents/MacOS/Granola Export
        # Look in adjacent Resources/ directory for our PNG
        macos_dir = Path(sys.executable).parent
        contents = macos_dir.parent
        candidates.append(contents / "Resources" / "menubar-icon@2x.png")
        candidates.append(contents / "Resources" / "menubar-icon.png")
    for p in candidates:
        if p.exists():
            return str(p)
    return None


# ---------- bridge classes (only defined if PyObjC is available) ----------

if PYOBJC_OK:
    class _MenuTarget(NSObject):
        """Bridge from Cocoa menu-item clicks to Python callables."""
        def initWithCallbacks_(self, callbacks):
            self = objc.super(_MenuTarget, self).init()
            if self is None:
                return None
            self._callbacks = callbacks
            return self

        def menuItemClicked_(self, sender):
            key = sender.representedObject()
            cb = self._callbacks.get(str(key))
            if cb:
                try:
                    cb()
                except Exception as e:
                    print(f"menubar: action {key!r} raised {e}")

    class _NotificationDelegate(NSObject):
        """Receives user-clicks on notifications and routes them to a callback."""
        def initWithCallback_(self, callback):
            self = objc.super(_NotificationDelegate, self).init()
            if self is None:
                return None
            self._callback = callback
            return self

        def userNotificationCenter_didActivateNotification_(self, center, notification):
            info = notification.userInfo()
            payload = dict(info) if info else {}
            if self._callback:
                try:
                    self._callback(payload)
                except Exception as e:
                    print(f"menubar: notification callback raised {e}")

        def userNotificationCenter_shouldPresentNotification_(self, center, notification):
            return True


# ---------- public API ----------

class MenuBarController:
    """Holds the status-bar item and notification delegate. Methods that need
    Cocoa become no-ops when PyObjC is unavailable.
    """

    def __init__(
        self,
        title_text: str = "📓",
        menu_callbacks: dict[str, Callable[[], None]] | None = None,
        notification_callback: Callable[[dict], None] | None = None,
    ):
        self.available = PYOBJC_OK
        self._status_item = None
        self._target = None
        self._delegate = None
        self._menu_callbacks = menu_callbacks or {}

        if not PYOBJC_OK:
            return

        # ---- status bar ----
        bar = NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(-1)  # variable length
        button = self._status_item.button()
        if button is not None:
            # Prefer a template image (auto-inverts on dark menu bars). Fall
            # back to the title_text emoji if the PNG isn't found.
            icon_path = _find_menubar_icon()
            applied_image = False
            if icon_path:
                try:
                    image = NSImage.alloc().initWithContentsOfFile_(icon_path)
                    if image is not None:
                        # Constrain rendering to ~18pt (the menu-bar standard).
                        try:
                            image.setSize_((18, 18))
                        except Exception:
                            pass
                        image.setTemplate_(True)
                        button.setImage_(image)
                        # Belt-and-braces: also clear the title so we don't
                        # show emoji + image together.
                        button.setTitle_("")
                        applied_image = True
                except Exception:
                    pass
            if not applied_image:
                button.setTitle_(title_text)
            try:
                button.setToolTip_("Granola Export")
            except Exception:
                pass

        # ---- menu ----
        self._target = _MenuTarget.alloc().initWithCallbacks_(self._menu_callbacks)
        menu = NSMenu.alloc().init()
        # Spec: ordered list of (label, action_key | None for separator)
        spec = [
            ("Show window", "show_window"),
            ("Scan now", "scan_now"),
            ("Open output folder", "open_folder"),
            (None, None),
            ("Quit Granola Export", "quit"),
        ]
        for label, key in spec:
            if label is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "menuItemClicked:", "",
            )
            item.setTarget_(self._target)
            item.setRepresentedObject_(key)
            menu.addItem_(item)
        self._status_item.setMenu_(menu)

        # ---- notification delegate ----
        if notification_callback is not None:
            self._delegate = _NotificationDelegate.alloc().initWithCallback_(notification_callback)
            NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(self._delegate)

    def set_title(self, text: str):
        if not self.available or not self._status_item:
            return
        button = self._status_item.button()
        if button:
            button.setTitle_(text)

    def notify(self, title: str, message: str, subtitle: str = "", action_key: str = ""):
        """Send a native macOS notification. action_key is included as userInfo
        and surfaced when the user clicks the notification."""
        if not self.available:
            # Fallback: osascript notification (no click handling)
            self._osascript_notify(title, message, subtitle)
            return

        try:
            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(message)
            if subtitle:
                n.setSubtitle_(subtitle)
            if action_key:
                n.setUserInfo_({"action_key": action_key})
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)
        except Exception as e:
            print(f"menubar: notify failed ({e}); falling back to osascript")
            self._osascript_notify(title, message, subtitle)

    @staticmethod
    def _osascript_notify(title: str, message: str, subtitle: str = ""):
        parts = [
            "display notification",
            json.dumps(message),
            f"with title {json.dumps(title)}",
        ]
        if subtitle:
            parts.append(f"subtitle {json.dumps(subtitle)}")
        try:
            subprocess.run(
                ["osascript", "-e", " ".join(parts)],
                check=False, capture_output=True, timeout=5,
            )
        except Exception:
            pass
