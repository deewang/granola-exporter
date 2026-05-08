#!/usr/bin/env python3
"""Granola Export — modern macOS GUI inspired by Granola's design.

Workflow:
  1. App auto-loads on launch and marks any NEW meetings.
  2. Tick the rows you want (or use Select New / Select All).
  3. Click "Export Selected" → writes Markdown + updates INDEX.md.
"""

import queue
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

from granola_core import (
    AuthError,
    DEFAULT_OUT_ROOT,
    Preferences,
    SUPABASE_FILE,
    __version__ as APP_VERSION,
    collect_existing_meta,
    fetch_transcript,
    diagnose_connection,
    install_launch_agent,
    is_launch_agent_installed,
    load_access_token,
    load_documents,
    load_preferences,
    mark_auth_ok,
    maybe_notify_auth_expired,
    meeting_filename,
    notify_macos,
    parse_iso,
    save_preferences,
    scan_existing,
    uninstall_launch_agent,
    write_index,
    write_meeting_file,
)
from menubar import MenuBarController

# ---------- design tokens (Granola-inspired warm light theme) ----------

BG_WINDOW = "#F7F4EE"
BG_CARD = "#FFFFFF"
BG_CARD_HOVER = "#FBF8F1"
BG_CARD_NEW = "#FEF7DD"
BG_CARD_NEW_HOVER = "#FBF1C9"
BG_PANEL = "#FFFFFF"
BORDER = "#E8E3D9"
BORDER_LIGHT = "#F0EBDF"

TEXT_PRIMARY = "#1F1B16"
TEXT_SECONDARY = "#736D62"
TEXT_TERTIARY = "#A8A095"
TEXT_ON_ACCENT = "#FFFFFF"

ACCENT = "#0F8A47"
ACCENT_HOVER = "#0C7038"

NEUTRAL_BTN = "#1F1B16"
NEUTRAL_BTN_HOVER = "#3A342B"
NEUTRAL_BTN_TEXT = "#FFFFFF"

GHOST_BTN = "#FFFFFF"
GHOST_BTN_HOVER = "#F4F0E7"
GHOST_BTN_TEXT = "#1F1B16"
GHOST_BTN_BORDER = "#E0DBD0"

DOT_NEW = "#F59E0B"
DOT_EXPORTED = "#10B981"
DOT_EXPIRED = "#DC2626"

PILL_NEW_BG = "#FFEBC2"
PILL_NEW_FG = "#92400E"
PILL_EXPORTED_BG = "#E5F4EB"
PILL_EXPORTED_FG = "#0F5E2C"

CHIP_OK_BG = "#E5F4EB"
CHIP_OK_FG = "#0F5E2C"
CHIP_WARN_BG = "#FFF1D6"
CHIP_WARN_FG = "#92400E"
CHIP_ERR_BG = "#FDE8E8"
CHIP_ERR_FG = "#9B1C1C"

DANGER = "#DC2626"

FONT_FAMILY = "SF Pro Text"
FONT_FAMILY_DISPLAY = "SF Pro Display"

PAGE_SIZE = 50
PEOPLE_PAGE_SIZE = 50

# Avatar colours for the People view (cycled by hashing the email/name)
AVATAR_COLORS = [
    "#A78BFA",  # purple
    "#F472B6",  # pink
    "#FB923C",  # orange
    "#FBBF24",  # amber
    "#A3E635",  # lime
    "#34D399",  # emerald
    "#22D3EE",  # cyan
    "#60A5FA",  # blue
    "#818CF8",  # indigo
    "#F87171",  # red
]

def avatar_color_for(seed: str) -> str:
    if not seed:
        return AVATAR_COLORS[0]
    return AVATAR_COLORS[abs(hash(seed)) % len(AVATAR_COLORS)]


def aggregate_people(docs: list[dict]) -> list[dict]:
    """Group meetings by participant. Returns a list sorted by last-meeting-date desc.

    Each entry: {name, email, domain, last_date, count, meetings}.
    """
    by_key: dict[str, dict] = {}
    for doc in docs:
        people = doc.get("people") or {}
        if not isinstance(people, dict):
            continue
        for grp in people.values():
            if not isinstance(grp, list):
                continue
            for p in grp:
                if not isinstance(p, dict):
                    continue
                email = (p.get("email") or "").strip().lower()
                name = (p.get("name") or "").strip()
                if not email and not name:
                    continue
                key = email or name.lower()
                bucket = by_key.setdefault(key, {
                    "email": email, "name": name, "meetings": [],
                })
                # Prefer a longer/more complete name
                if name and len(name) > len(bucket.get("name") or ""):
                    bucket["name"] = name
                # Prefer a real email over an empty one
                if email and not bucket.get("email"):
                    bucket["email"] = email
                bucket["meetings"].append(doc)

    result: list[dict] = []
    for p in by_key.values():
        p["meetings"].sort(key=lambda d: d.get("created_at") or "", reverse=True)
        p["last_date"] = p["meetings"][0].get("created_at", "") if p["meetings"] else ""
        p["count"] = len(p["meetings"])
        # Display name: keep raw name; if it's just an email handle, title-case it
        if not p["name"] and p["email"]:
            p["name"] = p["email"].split("@")[0].replace(".", " ").title()
        p["domain"] = p["email"].split("@", 1)[1] if "@" in p["email"] else ""
        result.append(p)

    result.sort(key=lambda x: x["last_date"], reverse=True)
    return result


def relative_date_label(iso_str: str) -> str:
    """Return 'Today', 'Yesterday', 'Apr 24', 'Jan 14, 2025' etc."""
    dt = None
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo)
    today = now.date()
    delta = (today - dt.astimezone().date()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return dt.astimezone().strftime("%A")
    if dt.year == now.year:
        return dt.astimezone().strftime("%b %d")
    return dt.astimezone().strftime("%b %d, %Y")

SORT_OPTIONS = {
    "Date (newest first)": ("date", True),
    "Date (oldest first)": ("date", False),
    "Title (A → Z)": ("title", False),
    "Title (Z → A)": ("title", True),
    "Status (new first)": ("status", False),
}


def f(size, weight="normal", display=False):
    family = FONT_FAMILY_DISPLAY if display else FONT_FAMILY
    return ctk.CTkFont(family=family, size=size, weight=weight)


# ---------- main app ----------

class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=BG_WINDOW)
        self.title("Granola Export")
        self.geometry("1080x780")
        self.minsize(880, 600)

        # Persistent preferences
        self.prefs: Preferences = load_preferences()

        # State
        initial_out = self.prefs.output_folder or str(DEFAULT_OUT_ROOT)
        self.out_root = ctk.StringVar(value=initial_out)
        self.docs: list[dict] = []                       # all meetings (after sort)
        self.checked: set[str] = set()                   # selected doc ids (across all pages)
        self.existing: set[str] = set()
        self.token: str | None = None
        self.token_remaining: int = 0                    # seconds
        self.log_q: queue.Queue = queue.Queue()
        self.worker_busy = False
        self.row_widgets: dict[str, dict] = {}
        self.log_visible = False
        self.current_page = 0
        self.sort_key = "Date (newest first)"

        # Auto-scan state
        self._auto_scan_after_id: str | None = None

        # Most recent AuthError message (shown in the reconnect dialog).
        self._last_auth_error: str = ""

        # macOS menu-bar status item (top of screen) — no-op if PyObjC missing
        self.menubar = MenuBarController(
            title_text="📓",
            menu_callbacks={
                "show_window": self._mb_show_window,
                "scan_now":    self._mb_scan_now,
                "open_folder": self._mb_open_folder,
                "quit":        self._mb_quit,
            },
            notification_callback=self._mb_notification_clicked,
        )

        # People-view state
        self.people_cache: list[dict] = []        # last aggregation
        self.people_filtered: list[dict] = []     # after search filter
        self.people_current_page = 0

        # Auth-watch state (set when waiting for token refresh)
        self._auth_watch_active = False
        self._auth_watch_after_id = None

        self._build_ui()

        # Red close-button → hide window, keep menu-bar item alive.
        # If PyObjC isn't installed (no menu bar) we let the close button quit
        # the app normally — otherwise the user would have no way to get back.
        if self.menubar.available:
            self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        # Cmd+Q always fully quits.
        self.bind_all("<Command-q>", lambda _e: self._mb_quit())

        self.after(100, self._drain_log)
        self.after(100, self._tick_token_status)
        self.after(200, self.refresh)
        # Arm auto-scan if user enabled it last session
        self.after(500, self._reschedule_auto_scan)

    # ---------- UI construction ----------

    def _build_ui(self):
        self._build_header()
        self._build_main_nav()

        # Stacked view container — switches between Meetings and People
        self.view_stack = ctk.CTkFrame(self, fg_color="transparent")
        self.view_stack.pack(fill="both", expand=True)

        # Meetings view (existing UI lives here)
        self._meetings_frame = ctk.CTkFrame(self.view_stack, fg_color="transparent")
        self._meetings_container = self._meetings_frame
        self._build_toolbar()
        self._build_summary_bar()
        self._build_meeting_list()
        self._build_pagination()

        # People view
        self._people_frame = ctk.CTkFrame(self.view_stack, fg_color="transparent")
        self._build_people_view()

        # Footer is shared across views
        self._build_footer()

        # Show meetings by default
        self._show_view("meetings")

    def _build_main_nav(self):
        """Custom segmented-button using two CTkButtons so we can colour the
        selected text white against the dark background."""
        nav_row = ctk.CTkFrame(self, fg_color="transparent", height=44)
        nav_row.pack(fill="x", padx=24, pady=(0, 8))
        nav_row.pack_propagate(False)

        nav_wrap = ctk.CTkFrame(nav_row, fg_color=BG_PANEL, corner_radius=10,
                                border_width=1, border_color=BORDER)
        nav_wrap.pack(side="left")

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for label in ("Meetings", "People"):
            btn = ctk.CTkButton(
                nav_wrap, text=label, font=f(13, "bold"),
                corner_radius=8, height=30, width=110,
                command=lambda l=label: self._set_nav(l),
            )
            btn.pack(side="left", padx=4, pady=4)
            self.nav_buttons[label] = btn

        self._current_nav = "Meetings"
        self._update_nav_styling()

    def _set_nav(self, choice: str):
        self._current_nav = choice
        self._update_nav_styling()
        self._show_view("people" if choice == "People" else "meetings")

    def _update_nav_styling(self):
        for label, btn in self.nav_buttons.items():
            if label == self._current_nav:
                btn.configure(
                    fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
                    text_color=NEUTRAL_BTN_TEXT,
                )
            else:
                btn.configure(
                    fg_color="transparent", hover_color=GHOST_BTN_HOVER,
                    text_color=TEXT_PRIMARY,
                )

    def _show_view(self, name: str):
        self._meetings_frame.pack_forget()
        self._people_frame.pack_forget()
        if name == "meetings":
            self._meetings_frame.pack(in_=self.view_stack, fill="both", expand=True)
        else:
            self._people_frame.pack(in_=self.view_stack, fill="both", expand=True)
            self._render_people_list()
        self.current_view = name

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent", height=78)
        header.pack(fill="x", padx=24, pady=(20, 8))
        header.pack_propagate(False)

        # Left: title + subtitle
        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", fill="y")

        title_row = ctk.CTkFrame(left, fg_color="transparent")
        title_row.pack(anchor="w")
        ctk.CTkLabel(
            title_row, text="Granola Export",
            font=f(26, "bold", display=True), text_color=TEXT_PRIMARY,
        ).pack(side="left")
        ctk.CTkLabel(
            title_row, text=f"v{APP_VERSION}",
            font=f(11, "bold"), text_color=TEXT_TERTIARY,
        ).pack(side="left", padx=(8, 0), pady=(8, 0))

        ctk.CTkLabel(
            left, text="Export your meetings as Markdown — sortable, indexed, AI-ready.",
            font=f(13), text_color=TEXT_SECONDARY,
        ).pack(anchor="w", pady=(2, 0))

        # Right: connection chip + settings button
        self.conn_chip = ctk.CTkLabel(
            header, text="● Connecting…",
            font=f(12, "bold"),
            text_color=CHIP_WARN_FG, fg_color=CHIP_WARN_BG,
            corner_radius=14, padx=12, pady=6,
        )
        self.conn_chip.pack(side="right", anchor="ne", pady=(2, 0))
        self.conn_chip.bind("<Button-1>", lambda _e: self._on_chip_click())

        ctk.CTkButton(
            header, text="⚙  Settings", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=30, width=110,
            command=self._show_settings_dialog,
        ).pack(side="right", anchor="ne", padx=(0, 10), pady=(2, 0))

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self._meetings_container, fg_color=BG_PANEL, corner_radius=12,
                           border_width=1, border_color=BORDER)
        bar.pack(fill="x", padx=24, pady=(0, 12))

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)

        self.btn_refresh = ctk.CTkButton(
            inner, text="↻  Refresh", font=f(13, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            width=100, height=34, command=self.refresh,
        )
        self.btn_refresh.pack(side="left")

        for label, fn in [
            ("Select new", self._select_new),
            ("All", self._select_all),
            ("Clear", self._select_none),
        ]:
            ctk.CTkButton(
                inner, text=label, font=f(13),
                fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
                text_color=GHOST_BTN_TEXT,
                border_color=GHOST_BTN_BORDER, border_width=1,
                corner_radius=8, height=34, width=90, command=fn,
            ).pack(side="left", padx=(8, 0))

        self.btn_export = ctk.CTkButton(
            inner, text="Export selected  →", font=f(13, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=TEXT_ON_ACCENT, corner_radius=8,
            width=170, height=34, command=self.export,
        )
        self.btn_export.pack(side="right")

        ctk.CTkButton(
            inner, text="Open folder", font=f(13),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=34, width=110, command=self._open_folder,
        ).pack(side="right", padx=(0, 8))

        ctk.CTkButton(
            inner, text="Choose…", font=f(13),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=34, width=80, command=self._pick_folder,
        ).pack(side="right", padx=(0, 8))

    def _build_summary_bar(self):
        bar = ctk.CTkFrame(self._meetings_container, fg_color="transparent", height=52)
        bar.pack(fill="x", padx=28, pady=(0, 8))
        bar.pack_propagate(False)

        # Two-row layout: top row = stats + info link, bottom row = output path
        top_row = ctk.CTkFrame(bar, fg_color="transparent")
        top_row.pack(fill="x", anchor="n")

        self.summary_label = ctk.CTkLabel(
            top_row, text="Loading…", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.summary_label.pack(side="left")

        info_link = ctk.CTkLabel(
            top_row, text="ⓘ Where does this data come from?",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, underline=True),
            text_color=TEXT_SECONDARY, cursor="hand2",
        )
        info_link.pack(side="left", padx=(10, 0))
        info_link.bind("<Button-1>", lambda _e: self._show_data_info())
        info_link.bind("<Enter>", lambda _e: info_link.configure(text_color=TEXT_PRIMARY))
        info_link.bind("<Leave>", lambda _e: info_link.configure(text_color=TEXT_SECONDARY))

        # Right: selected count
        self.selected_label = ctk.CTkLabel(
            top_row, text="0 selected", font=f(12, "bold"), text_color=TEXT_PRIMARY,
        )
        self.selected_label.pack(side="right")

        # Sort dropdown next to selected count
        ctk.CTkLabel(
            top_row, text="Sort:", font=f(12), text_color=TEXT_SECONDARY,
        ).pack(side="right", padx=(0, 6))

        self.sort_menu = ctk.CTkOptionMenu(
            top_row, values=list(SORT_OPTIONS.keys()),
            font=f(12),
            fg_color=GHOST_BTN, button_color=GHOST_BTN, button_hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT, dropdown_fg_color=BG_PANEL,
            dropdown_text_color=TEXT_PRIMARY, dropdown_hover_color=GHOST_BTN_HOVER,
            corner_radius=6, height=26, width=180,
            command=self._on_sort_change,
        )
        self.sort_menu.set(self.sort_key)
        self.sort_menu.pack(side="right", padx=(0, 14))

        # Bottom row: shows the output folder so users always know where exports land
        bottom_row = ctk.CTkFrame(bar, fg_color="transparent")
        bottom_row.pack(fill="x", anchor="s", pady=(2, 0))

        ctk.CTkLabel(
            bottom_row, text="Saving to:", font=f(11), text_color=TEXT_TERTIARY,
        ).pack(side="left")
        self.path_label = ctk.CTkLabel(
            bottom_row, text=self._short_path(self.out_root.get()),
            font=f(11, "bold"), text_color=TEXT_SECONDARY,
        )
        self.path_label.pack(side="left", padx=(4, 0))
        # Keep label in sync if user changes the folder
        self.out_root.trace_add("write", lambda *_a: self.path_label.configure(
            text=self._short_path(self.out_root.get())
        ))

    def _short_path(self, path_str: str) -> str:
        """Display a friendly version of the output path: ~/foo/bar/transcripts/"""
        try:
            p = Path(path_str)
            home = Path.home()
            if p == home or home in p.parents:
                rel = p.relative_to(home)
                return f"~/{rel}/transcripts/"
            return f"{p}/transcripts/"
        except Exception:
            return path_str

    def _build_meeting_list(self):
        wrap = ctk.CTkFrame(self._meetings_container, fg_color=BG_PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        wrap.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        self.list_frame = ctk.CTkScrollableFrame(
            wrap, fg_color=BG_PANEL, corner_radius=10,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=TEXT_TERTIARY,
        )
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_pagination(self):
        bar = ctk.CTkFrame(self._meetings_container, fg_color="transparent", height=34)
        bar.pack(fill="x", padx=24, pady=(0, 10))
        bar.pack_propagate(False)

        self.btn_prev = ctk.CTkButton(
            bar, text="← Previous", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=6, height=28, width=100, command=self._prev_page,
        )
        self.btn_prev.pack(side="left")

        self.page_label = ctk.CTkLabel(
            bar, text="Page 1 of 1", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.page_label.pack(side="left", padx=12)

        self.btn_next = ctk.CTkButton(
            bar, text="Next →", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=6, height=28, width=80, command=self._next_page,
        )
        self.btn_next.pack(side="left")

        # Right side: shortcut links
        self.btn_first = ctk.CTkButton(
            bar, text="Last page", font=f(12),
            fg_color="transparent", hover_color=GHOST_BTN_HOVER,
            text_color=TEXT_SECONDARY, border_width=0,
            corner_radius=6, height=28, width=80, command=self._last_page,
        )
        self.btn_first.pack(side="right")

        ctk.CTkButton(
            bar, text="First page", font=f(12),
            fg_color="transparent", hover_color=GHOST_BTN_HOVER,
            text_color=TEXT_SECONDARY, border_width=0,
            corner_radius=6, height=28, width=80, command=self._first_page,
        ).pack(side="right")

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 18))

        self.progress = ctk.CTkProgressBar(
            footer, height=6, corner_radius=4,
            progress_color=ACCENT, fg_color=BORDER_LIGHT,
        )
        self.progress.pack(fill="x")
        self.progress.set(0)

        status_row = ctk.CTkFrame(footer, fg_color="transparent")
        status_row.pack(fill="x", pady=(8, 0))

        self.status_label = ctk.CTkLabel(
            status_row, text="Ready", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.status_label.pack(side="left")

        self.toggle_log_btn = ctk.CTkButton(
            status_row, text="▸ Show log", font=f(12),
            fg_color="transparent", hover_color=GHOST_BTN_HOVER,
            text_color=TEXT_SECONDARY, border_width=0, corner_radius=6,
            width=90, height=22, command=self._toggle_log,
        )
        self.toggle_log_btn.pack(side="right")

        self.log_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=10,
                                       border_width=1, border_color=BORDER)
        self.log_text = ctk.CTkTextbox(
            self.log_frame, height=110,
            fg_color=BG_PANEL, text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="Menlo", size=11),
            border_width=0,
        )
        self.log_text.pack(fill="x", padx=12, pady=10)
        self.log_text.configure(state="disabled")

    # ---------- meeting row rendering ----------

    def _clear_list(self):
        for w in list(self.list_frame.winfo_children()):
            w.destroy()
        self.row_widgets.clear()

    def _build_row(self, doc: dict):
        is_new = meeting_filename(doc) not in self.existing
        bg = BG_CARD_NEW if is_new else BG_CARD
        hover = BG_CARD_NEW_HOVER if is_new else BG_CARD_HOVER

        row = ctk.CTkFrame(self.list_frame, fg_color=bg, corner_radius=8, height=64)
        row.pack(fill="x", padx=4, pady=3)
        row.pack_propagate(False)

        check_var = tk.BooleanVar(value=(doc["id"] in self.checked))
        check = ctk.CTkCheckBox(
            row, text="", variable=check_var, width=24,
            checkbox_width=20, checkbox_height=20,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            border_color=BORDER, border_width=2, corner_radius=5,
            command=lambda did=doc["id"], var=check_var: self._toggle(did, var.get()),
        )
        check.pack(side="left", padx=(16, 12))

        if is_new:
            dot_color, pill_bg, pill_fg, pill_text = DOT_NEW, PILL_NEW_BG, PILL_NEW_FG, "NEW"
        else:
            dot_color, pill_bg, pill_fg, pill_text = DOT_EXPORTED, PILL_EXPORTED_BG, PILL_EXPORTED_FG, "EXPORTED"

        dot = ctk.CTkFrame(row, width=8, height=8, fg_color=dot_color, corner_radius=4)
        dot.pack(side="left", padx=(0, 10))

        text_col = ctk.CTkFrame(row, fg_color="transparent")
        text_col.pack(side="left", fill="both", expand=True, padx=(0, 12))

        title = doc.get("title") or "Untitled"
        title_lbl = ctk.CTkLabel(
            text_col, text=title, font=f(14, "bold"),
            text_color=TEXT_PRIMARY, anchor="w",
        )
        title_lbl.pack(anchor="w", pady=(8, 0))

        people_names = self._extract_people_short(doc)
        meta_lbl = ctk.CTkLabel(
            text_col, text=people_names or "—",
            font=f(12), text_color=TEXT_SECONDARY, anchor="w",
        )
        meta_lbl.pack(anchor="w")

        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right", padx=(8, 16))

        pill = ctk.CTkLabel(
            right, text=pill_text, font=f(10, "bold"),
            text_color=pill_fg, fg_color=pill_bg,
            corner_radius=10, padx=10, pady=2,
        )
        pill.pack(anchor="e", pady=(8, 0))

        dt = parse_iso(doc.get("created_at"))
        date_str = dt.astimezone().strftime("%a %b %d, %H:%M") if dt else "—"
        ctk.CTkLabel(
            right, text=date_str, font=f(11),
            text_color=TEXT_TERTIARY, anchor="e",
        ).pack(anchor="e", pady=(2, 0))

        # Click on row body (anything except checkbox) opens the detail window.
        for w in (text_col, title_lbl, meta_lbl, right, dot, pill):
            w.bind("<Button-1>", lambda _e, d=doc: self._open_detail(d))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass

        def on_enter(_e=None, w=row, c=hover): w.configure(fg_color=c)
        def on_leave(_e=None, w=row, c=bg): w.configure(fg_color=c)
        for w in (row, text_col, title_lbl, meta_lbl, right, dot):
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        self.row_widgets[doc["id"]] = {
            "row": row, "check": check, "var": check_var,
            "is_new": is_new, "pill": pill, "bg": bg,
        }

    def _toggle_via_click(self, doc_id: str, var: tk.BooleanVar):
        new_val = not var.get()
        var.set(new_val)
        self._toggle(doc_id, new_val)

    def _open_detail(self, doc: dict):
        """Open a window showing the meeting's notes + transcript."""
        MeetingDetailWindow(self, doc)

    def _extract_people_short(self, doc: dict) -> str:
        people = doc.get("people") or {}
        names: list[str] = []
        if isinstance(people, dict):
            for grp in people.values():
                if isinstance(grp, list):
                    for p in grp:
                        if isinstance(p, dict):
                            n = p.get("name") or p.get("email")
                            if n:
                                names.append(n)
        if not names:
            return ""
        if len(names) <= 3:
            return ", ".join(names)
        return ", ".join(names[:3]) + f"  +{len(names) - 3}"

    # ---------- selection ----------

    def _toggle(self, doc_id: str, checked: bool):
        if checked:
            self.checked.add(doc_id)
        else:
            self.checked.discard(doc_id)
        w = self.row_widgets.get(doc_id)
        if w and w["var"].get() != checked:
            w["var"].set(checked)
        self._update_count()

    def _select_new(self):
        # Operates across ALL meetings, not just current page
        self.checked = {d["id"] for d in self.docs if meeting_filename(d) not in self.existing}
        self._sync_checkbox_widgets()
        self._update_count()

    def _select_all(self):
        self.checked = {d["id"] for d in self.docs}
        self._sync_checkbox_widgets()
        self._update_count()

    def _select_none(self):
        self.checked.clear()
        self._sync_checkbox_widgets()
        self._update_count()

    def _sync_checkbox_widgets(self):
        for did, w in self.row_widgets.items():
            w["var"].set(did in self.checked)

    def _update_count(self):
        n = len(self.checked)
        self.selected_label.configure(text=f"{n} selected")
        self.btn_export.configure(state="normal" if n else "disabled")

    # ---------- sort + pagination ----------

    def _on_sort_change(self, choice: str):
        self.sort_key = choice
        self._apply_sort()
        self.current_page = 0
        self._render_current_page()

    def _apply_sort(self):
        key, reverse = SORT_OPTIONS[self.sort_key]
        if key == "date":
            self.docs.sort(key=lambda d: d.get("created_at") or "", reverse=reverse)
        elif key == "title":
            self.docs.sort(key=lambda d: (d.get("title") or "Untitled").lower(), reverse=reverse)
        elif key == "status":
            # New first → False sorts before True; we want NEW first so invert
            self.docs.sort(key=lambda d: (
                meeting_filename(d) in self.existing,  # False (new) before True (exported)
                # secondary: newest first within each group
                -(self._date_int(d)),
            ))

    def _date_int(self, doc: dict) -> int:
        ts = doc.get("created_at") or ""
        try:
            return int(ts.replace("-", "").replace(":", "").replace("T", "")[:14])
        except ValueError:
            return 0

    def _total_pages(self) -> int:
        return max(1, (len(self.docs) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _get_page_docs(self) -> list[dict]:
        start = self.current_page * PAGE_SIZE
        return self.docs[start:start + PAGE_SIZE]

    def _render_current_page(self):
        self._clear_list()
        page_docs = self._get_page_docs()
        if not page_docs:
            ctk.CTkLabel(
                self.list_frame, text="No meetings found.",
                font=f(14), text_color=TEXT_TERTIARY,
            ).pack(pady=60)
        else:
            for d in page_docs:
                self._build_row(d)
        self._update_page_controls()
        # Scroll list to top on page change
        try:
            self.list_frame._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def _update_page_controls(self):
        total = self._total_pages()
        showing_from = self.current_page * PAGE_SIZE + 1 if self.docs else 0
        showing_to = min((self.current_page + 1) * PAGE_SIZE, len(self.docs))
        self.page_label.configure(
            text=f"Page {self.current_page + 1} of {total}  ·  showing {showing_from}–{showing_to} of {len(self.docs)}"
        )
        self.btn_prev.configure(state="normal" if self.current_page > 0 else "disabled")
        self.btn_next.configure(state="normal" if self.current_page < total - 1 else "disabled")

    def _prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self._render_current_page()

    def _next_page(self):
        if self.current_page < self._total_pages() - 1:
            self.current_page += 1
            self._render_current_page()

    def _first_page(self):
        self.current_page = 0
        self._render_current_page()

    def _last_page(self):
        self.current_page = self._total_pages() - 1
        self._render_current_page()

    # ---------- folder + log helpers ----------

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.out_root.get(), title="Choose output folder")
        if d:
            self.out_root.set(d)
            self.prefs.output_folder = d
            save_preferences(self.prefs)
            self.refresh()

    def _open_folder(self):
        import subprocess
        subprocess.run(["open", self.out_root.get()])

    def _toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="x", padx=24, pady=(0, 18))
            self.toggle_log_btn.configure(text="▾ Hide log")
        else:
            self.log_frame.pack_forget()
            self.toggle_log_btn.configure(text="▸ Show log")

    def _log(self, msg: str):
        self.log_q.put(msg)

    def _drain_log(self):
        wrote = False
        while not self.log_q.empty():
            msg = self.log_q.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            wrote = True
        if wrote and not self.log_visible:
            self._toggle_log()
        self.after(100, self._drain_log)

    def _set_status(self, text: str, color: str = TEXT_SECONDARY):
        self.status_label.configure(text=text, text_color=color)

    def _set_busy(self, busy: bool):
        self.worker_busy = busy
        if busy:
            self.btn_refresh.configure(state="disabled")
            self.btn_export.configure(state="disabled")
        else:
            self.btn_refresh.configure(state="normal")
            self._update_count()

    # ---------- connection chip + auth flow ----------

    def _set_chip(self, text: str, fg: str, bg: str):
        self.conn_chip.configure(text=text, text_color=fg, fg_color=bg)

    def _on_chip_click(self):
        # Always open the modal — gives info + option to reconnect
        self._show_reconnect_dialog()

    def _tick_token_status(self):
        # Periodically update the chip + warn before expiry
        if self.token and self.token_remaining > 0:
            self.token_remaining -= 5
            mins = max(0, self.token_remaining) // 60
            if self.token_remaining <= 0:
                self._set_chip("● Session expired", CHIP_ERR_FG, CHIP_ERR_BG)
                self.token = None
            elif self.token_remaining < 600:  # under 10 min
                self._set_chip(f"● {mins}m left", CHIP_WARN_FG, CHIP_WARN_BG)
            else:
                self._set_chip(f"● Connected · {mins}m", CHIP_OK_FG, CHIP_OK_BG)
        self.after(5000, self._tick_token_status)

    # ---------- People view ----------

    def _build_people_view(self):
        """Build the People list + Contact detail sub-views inside _people_frame."""
        self._people_list_frame = ctk.CTkFrame(self._people_frame, fg_color="transparent")
        self._people_detail_frame = ctk.CTkFrame(self._people_frame, fg_color="transparent")
        self._people_list_frame.pack(fill="both", expand=True)

        # ----- title row -----
        list_header = ctk.CTkFrame(self._people_list_frame, fg_color="transparent", height=44)
        list_header.pack(fill="x", padx=24, pady=(8, 6))
        list_header.pack_propagate(False)

        ctk.CTkLabel(
            list_header, text="People",
            font=f(22, "bold", display=True), text_color=TEXT_PRIMARY,
        ).pack(side="left")

        self.people_count_label = ctk.CTkLabel(
            list_header, text="", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.people_count_label.pack(side="left", padx=(10, 0), pady=(8, 0))

        # ----- search bar -----
        # Single CTkEntry that fills horizontally — wrapping in another frame
        # tripped the fill propagation, so the icon lives in the placeholder.
        self.people_search_var = tk.StringVar()
        self.people_search_var.trace_add("write", lambda *_a: self._on_people_search_change())
        search_entry = ctk.CTkEntry(
            self._people_list_frame, textvariable=self.people_search_var,
            placeholder_text="🔍   Search by name, email, or domain…",
            font=f(13), fg_color=BG_PANEL, border_width=1, border_color=BORDER,
            text_color=TEXT_PRIMARY, placeholder_text_color=TEXT_TERTIARY,
            height=34, corner_radius=8,
        )
        search_entry.pack(fill="x", padx=24, pady=(0, 8))

        # ----- column headers -----
        col_headers = ctk.CTkFrame(self._people_list_frame, fg_color="transparent", height=22)
        col_headers.pack(fill="x", padx=28, pady=(0, 4))
        col_headers.pack_propagate(False)
        ctk.CTkLabel(col_headers, text="Person", font=f(11, "bold"),
                      text_color=TEXT_TERTIARY, anchor="w").pack(side="left")
        ctk.CTkLabel(col_headers, text="Notes", font=f(11, "bold"),
                      text_color=TEXT_TERTIARY, anchor="e", width=70).pack(side="right", padx=(0, 12))
        ctk.CTkLabel(col_headers, text="Last note", font=f(11, "bold"),
                      text_color=TEXT_TERTIARY, anchor="e", width=120).pack(side="right", padx=(0, 12))

        # ----- scrollable people list -----
        list_wrap = ctk.CTkFrame(self._people_list_frame, fg_color=BG_PANEL,
                                  corner_radius=12, border_width=1, border_color=BORDER)
        list_wrap.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        self.people_scroll = ctk.CTkScrollableFrame(
            list_wrap, fg_color=BG_PANEL, corner_radius=10,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT_TERTIARY,
        )
        self.people_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # ----- pagination row -----
        pag_row = ctk.CTkFrame(self._people_list_frame, fg_color="transparent", height=34)
        pag_row.pack(fill="x", padx=24, pady=(0, 12))
        pag_row.pack_propagate(False)

        self.btn_people_prev = ctk.CTkButton(
            pag_row, text="← Previous", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=6, height=28, width=100, command=self._people_prev_page,
        )
        self.btn_people_prev.pack(side="left")

        self.people_page_label = ctk.CTkLabel(
            pag_row, text="Page 1 of 1", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.people_page_label.pack(side="left", padx=12)

        self.btn_people_next = ctk.CTkButton(
            pag_row, text="Next →", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=6, height=28, width=80, command=self._people_next_page,
        )
        self.btn_people_next.pack(side="left")

    def _render_people_list(self):
        """Re-aggregate, then render the first page."""
        self.people_cache = aggregate_people(self.docs) if self.docs else []
        # If a search query was in effect, re-apply it; otherwise show all
        query = self.people_search_var.get().strip().lower() if hasattr(self, "people_search_var") else ""
        self.people_filtered = self._filter_people(query)
        self.people_count_label.configure(
            text=f"· {len(self.people_cache)} contact{'s' if len(self.people_cache) != 1 else ''}"
        )
        self.people_current_page = 0
        self._render_people_page()

    def _filter_people(self, query: str) -> list[dict]:
        if not query:
            return list(self.people_cache)
        q = query.lower()
        return [
            p for p in self.people_cache
            if q in (p.get("name") or "").lower()
            or q in (p.get("email") or "").lower()
            or q in (p.get("domain") or "").lower()
        ]

    def _on_people_search_change(self):
        query = self.people_search_var.get().strip().lower()
        self.people_filtered = self._filter_people(query)
        self.people_current_page = 0
        self._render_people_page()

    def _render_people_page(self):
        # Clear current rows
        for w in list(self.people_scroll.winfo_children()):
            w.destroy()

        if not self.people_filtered:
            empty_msg = ("No matches — try a different search."
                         if self.people_search_var.get().strip()
                         else "No people detected yet — click Refresh on the Meetings tab first.")
            ctk.CTkLabel(
                self.people_scroll, text=empty_msg,
                font=f(13), text_color=TEXT_TERTIARY,
            ).pack(pady=60)
            self._update_people_pagination()
            return

        start = self.people_current_page * PEOPLE_PAGE_SIZE
        end = start + PEOPLE_PAGE_SIZE
        for person in self.people_filtered[start:end]:
            self._build_person_row(person)
        self._update_people_pagination()
        try:
            self.people_scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def _update_people_pagination(self):
        n = len(self.people_filtered)
        total = max(1, (n + PEOPLE_PAGE_SIZE - 1) // PEOPLE_PAGE_SIZE)
        showing_from = self.people_current_page * PEOPLE_PAGE_SIZE + 1 if n else 0
        showing_to = min((self.people_current_page + 1) * PEOPLE_PAGE_SIZE, n)
        self.people_page_label.configure(
            text=f"Page {self.people_current_page + 1} of {total}  ·  showing {showing_from}–{showing_to} of {n}"
        )
        self.btn_people_prev.configure(state="normal" if self.people_current_page > 0 else "disabled")
        self.btn_people_next.configure(state="normal" if self.people_current_page < total - 1 else "disabled")

    def _people_prev_page(self):
        if self.people_current_page > 0:
            self.people_current_page -= 1
            self._render_people_page()

    def _people_next_page(self):
        total = max(1, (len(self.people_filtered) + PEOPLE_PAGE_SIZE - 1) // PEOPLE_PAGE_SIZE)
        if self.people_current_page < total - 1:
            self.people_current_page += 1
            self._render_people_page()

    def _build_person_row(self, person: dict):
        row = ctk.CTkFrame(self.people_scroll, fg_color=BG_CARD, corner_radius=8, height=58)
        row.pack(fill="x", padx=4, pady=2)
        row.pack_propagate(False)

        # Avatar circle
        seed = person.get("email") or person.get("name") or "?"
        initial = (person.get("name") or person.get("email") or "?")[:1].upper()
        avatar = ctk.CTkLabel(
            row, text=initial, width=36, height=36, corner_radius=18,
            fg_color=avatar_color_for(seed), text_color="#FFFFFF",
            font=f(13, "bold"),
        )
        avatar.pack(side="left", padx=(14, 12), pady=11)

        # Name + email column
        text_col = ctk.CTkFrame(row, fg_color="transparent")
        text_col.pack(side="left", fill="both", expand=True)

        name = person.get("name") or "—"
        ctk.CTkLabel(
            text_col, text=name, font=f(13, "bold"),
            text_color=TEXT_PRIMARY, anchor="w",
        ).pack(anchor="w", pady=(10, 0))

        email = person.get("email") or ""
        ctk.CTkLabel(
            text_col, text=email or "(no email)",
            font=f(11), text_color=TEXT_SECONDARY, anchor="w",
        ).pack(anchor="w")

        # Notes count (right side)
        ctk.CTkLabel(
            row, text=str(person["count"]), font=f(13, "bold"),
            text_color=TEXT_PRIMARY, width=70, anchor="e",
        ).pack(side="right", padx=(0, 16))

        # Last-note date
        ctk.CTkLabel(
            row, text=relative_date_label(person.get("last_date", "")) or "—",
            font=f(12), text_color=TEXT_SECONDARY, width=120, anchor="e",
        ).pack(side="right", padx=(0, 12))

        # Hover + click
        hover = BG_CARD_HOVER
        bg = BG_CARD
        def on_enter(_e=None, w=row): w.configure(fg_color=hover)
        def on_leave(_e=None, w=row): w.configure(fg_color=bg)
        row.bind("<Enter>", on_enter)
        row.bind("<Leave>", on_leave)

        for w in (row, avatar, text_col):
            w.bind("<Button-1>", lambda _e, p=person: self._open_contact_detail(p))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass
        # Children of text_col also need the click binding
        for child in text_col.winfo_children():
            child.bind("<Button-1>", lambda _e, p=person: self._open_contact_detail(p))
            try:
                child.configure(cursor="hand2")
            except tk.TclError:
                pass

    # ---------- Contact detail sub-view ----------

    def _open_contact_detail(self, person: dict):
        self._people_list_frame.pack_forget()
        # Rebuild detail frame fresh each time so we don't leak widgets
        for w in list(self._people_detail_frame.winfo_children()):
            w.destroy()
        self._render_contact_detail(person)
        self._people_detail_frame.pack(in_=self._people_frame, fill="both", expand=True)

    def _back_to_people_list(self):
        self._people_detail_frame.pack_forget()
        self._people_list_frame.pack(in_=self._people_frame, fill="both", expand=True)

    def _render_contact_detail(self, person: dict):
        # Top bar: back button
        topbar = ctk.CTkFrame(self._people_detail_frame, fg_color="transparent", height=40)
        topbar.pack(fill="x", padx=24, pady=(8, 8))
        topbar.pack_propagate(False)
        ctk.CTkButton(
            topbar, text="‹  People", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=30, width=110,
            command=self._back_to_people_list,
        ).pack(side="left")

        # Contact header (avatar + name + email + domain)
        header = ctk.CTkFrame(self._people_detail_frame, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(8, 12))

        seed = person.get("email") or person.get("name") or "?"
        initial = (person.get("name") or person.get("email") or "?")[:1].upper()
        ctk.CTkLabel(
            header, text=initial, width=56, height=56, corner_radius=28,
            fg_color=avatar_color_for(seed), text_color="#FFFFFF",
            font=f(20, "bold", display=True),
        ).pack(side="left", padx=(0, 16))

        info = ctk.CTkFrame(header, fg_color="transparent")
        info.pack(side="left", fill="y")
        ctk.CTkLabel(
            info, text=person.get("name") or "—",
            font=f(22, "bold", display=True), text_color=TEXT_PRIMARY, anchor="w",
        ).pack(anchor="w")

        meta = []
        if person.get("email"):
            meta.append(person["email"])
        if person.get("domain"):
            meta.append(person["domain"])
        ctk.CTkLabel(
            info, text="  ·  ".join(meta) or "—",
            font=f(12), text_color=TEXT_SECONDARY, anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        # AI search box (placeholder — non-functional)
        ai_wrap = ctk.CTkFrame(self._people_detail_frame, fg_color=BG_PANEL,
                                corner_radius=12, border_width=1, border_color=BORDER, height=54)
        ai_wrap.pack(fill="x", padx=24, pady=(0, 10))
        ai_wrap.pack_propagate(False)
        ctk.CTkLabel(
            ai_wrap, text="✨", font=f(16), text_color=TEXT_SECONDARY,
        ).pack(side="left", padx=(14, 8))
        ctk.CTkEntry(
            ai_wrap, font=f(13),
            placeholder_text=f"Ask anything about {person.get('name') or 'this person'}…  (coming soon)",
            fg_color=BG_PANEL, border_width=0, text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_TERTIARY, height=32,
        ).pack(side="left", fill="x", expand=True, padx=(0, 14))
        ctk.CTkLabel(
            ai_wrap, text="Sonnet 4.6 ▾", font=f(11),
            text_color=TEXT_TERTIARY,
        ).pack(side="right", padx=(0, 14))

        # Quick action chips (placeholder)
        chips_row = ctk.CTkFrame(self._people_detail_frame, fg_color="transparent")
        chips_row.pack(fill="x", padx=24, pady=(0, 16))
        for label in ("Prep next meeting", "List outstanding items", "Coach me on next call"):
            ctk.CTkButton(
                chips_row, text=f"›  {label}", font=f(12),
                fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
                text_color=TEXT_SECONDARY,
                border_color=GHOST_BTN_BORDER, border_width=1,
                corner_radius=20, height=28, width=10,
                command=lambda l=label: self._log(f"[ai-placeholder] {l}: not implemented yet"),
            ).pack(side="left", padx=(0, 8))

        # Notes timeline header
        notes_header = ctk.CTkFrame(self._people_detail_frame, fg_color="transparent", height=32)
        notes_header.pack(fill="x", padx=24, pady=(4, 4))
        notes_header.pack_propagate(False)
        ctk.CTkLabel(
            notes_header, text=f"All notes  ·  {person['count']}",
            font=f(13, "bold"), text_color=TEXT_PRIMARY, anchor="w",
        ).pack(side="left")

        # Scrollable list of meetings with this person, grouped by date
        notes_wrap = ctk.CTkFrame(self._people_detail_frame, fg_color=BG_PANEL,
                                   corner_radius=12, border_width=1, border_color=BORDER)
        notes_wrap.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        notes_scroll = ctk.CTkScrollableFrame(
            notes_wrap, fg_color=BG_PANEL, corner_radius=10,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT_TERTIARY,
        )
        notes_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # Group meetings under date headers
        last_group = None
        for doc in person["meetings"]:
            group = relative_date_label(doc.get("created_at", "")) or "Earlier"
            if group != last_group:
                ctk.CTkLabel(
                    notes_scroll, text=group, font=f(11, "bold"),
                    text_color=TEXT_TERTIARY, anchor="w",
                ).pack(anchor="w", padx=10, pady=(10, 4))
                last_group = group
            self._build_contact_note_row(notes_scroll, doc, person)

    def _build_contact_note_row(self, parent, doc: dict, person: dict):
        is_new = meeting_filename(doc) not in self.existing
        bg = BG_CARD_NEW if is_new else BG_CARD
        hover = BG_CARD_NEW_HOVER if is_new else BG_CARD_HOVER

        row = ctk.CTkFrame(parent, fg_color=bg, corner_radius=8, height=52)
        row.pack(fill="x", padx=4, pady=2)
        row.pack_propagate(False)

        # Avatar circle on left (same as main person)
        seed = person.get("email") or person.get("name") or "?"
        initial = (person.get("name") or person.get("email") or "?")[:1].upper()
        ctk.CTkLabel(
            row, text=initial, width=30, height=30, corner_radius=15,
            fg_color=avatar_color_for(seed), text_color="#FFFFFF",
            font=f(11, "bold"),
        ).pack(side="left", padx=(14, 10), pady=11)

        # Title + sub label
        text_col = ctk.CTkFrame(row, fg_color="transparent")
        text_col.pack(side="left", fill="both", expand=True)
        title = doc.get("title") or "Untitled"
        ctk.CTkLabel(
            text_col, text=title, font=f(13, "bold"),
            text_color=TEXT_PRIMARY, anchor="w",
        ).pack(anchor="w", pady=(8, 0))
        ctk.CTkLabel(
            text_col, text=person.get("name") or person.get("email") or "—",
            font=f(11), text_color=TEXT_SECONDARY, anchor="w",
        ).pack(anchor="w")

        # Time on right
        dt = parse_iso(doc.get("created_at"))
        time_str = dt.astimezone().strftime("%-I:%M %p").lower() if dt else "—"
        ctk.CTkLabel(
            row, text=time_str, font=f(11), text_color=TEXT_TERTIARY,
            anchor="e", width=80,
        ).pack(side="right", padx=(0, 16))

        def on_enter(_e=None, w=row): w.configure(fg_color=hover)
        def on_leave(_e=None, w=row): w.configure(fg_color=bg)
        row.bind("<Enter>", on_enter)
        row.bind("<Leave>", on_leave)

        # Click → open existing meeting detail window
        for w in (row, text_col):
            w.bind("<Button-1>", lambda _e, d=doc: self._open_detail(d))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass
        for child in text_col.winfo_children():
            child.bind("<Button-1>", lambda _e, d=doc: self._open_detail(d))
            try:
                child.configure(cursor="hand2")
            except tk.TclError:
                pass

    # ---------- Menu-bar callbacks ----------

    def _on_close_window(self):
        """Red close button: hide the window but keep us alive in the menu bar."""
        try:
            self.withdraw()
        except Exception as e:
            self._log(f"close: withdraw failed: {e}")
            self.destroy()
            return

        # First-time hint so the user knows we're still running
        if not self.prefs.shown_close_hint:
            self.menubar.notify(
                title="Granola Export is still running",
                message="Click the 📓 icon in your menu bar to reopen, or use Quit from there to fully exit.",
                action_key="show_window",
            )
            self.prefs.shown_close_hint = True
            save_preferences(self.prefs)

    def _mb_show_window(self):
        """Bring the app window to the foreground from the menu-bar item."""
        try:
            self.deiconify()         # in case it's withdrawn / minimised
            self.lift()
            self.focus_force()
            # On macOS we also need to call NSRunningApplication activate
            self._activate_app()
        except Exception as e:
            self._log(f"menubar: show_window failed: {e}")

    def _mb_open_folder(self):
        """Open the output folder in Finder."""
        try:
            subprocess.run(["open", self.out_root.get()])
        except Exception as e:
            self._log(f"menubar: open_folder failed: {e}")

    def _mb_scan_now(self):
        """Trigger an immediate auto-scan from the menu-bar."""
        self._kick_auto_scan(manual=True)

    def _mb_quit(self):
        """Quit the entire app from the menu-bar."""
        try:
            self.destroy()
        except Exception:
            pass

    def _mb_notification_clicked(self, payload: dict):
        """Fired when the user clicks any of our notifications.

        Always brings the window forward + opens the output folder if the
        notification specified that action.
        """
        # Need to bounce to the Tk main thread
        self.after(0, self._mb_show_window)
        action = (payload or {}).get("action_key", "")
        if action == "open_folder":
            self.after(0, self._mb_open_folder)

    def _activate_app(self):
        """Bring our process to the foreground on macOS."""
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            # Fall back to AppleScript activation by bundle id
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application id "com.davidwang.granolaexport" to activate'],
                    check=False, capture_output=True, timeout=2,
                )
            except Exception:
                pass

    # ---------- Settings dialog + auto-scan ----------

    AUTO_SCAN_INTERVAL_OPTIONS = {
        "Every 15 minutes": 15,
        "Every 30 minutes": 30,
        "Every hour": 60,
        "Every 2 hours": 120,
        "Every 4 hours": 240,
        "Every 8 hours": 480,
    }

    def _show_settings_dialog(self):
        if hasattr(self, "_settings_win") and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return

        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.title("Settings")
        win.geometry("520x720")
        win.configure(fg_color=BG_WINDOW)
        win.resizable(False, False)
        win.transient(self)

        ctk.CTkLabel(
            win, text="Settings", font=f(20, "bold", display=True),
            text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=22, pady=(20, 14))

        # Section: Auto-scan
        sec = ctk.CTkFrame(win, fg_color=BG_PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        sec.pack(fill="x", padx=22, pady=(0, 12))

        ctk.CTkLabel(
            sec, text="Auto-scan", font=f(14, "bold"), text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(
            sec,
            text="Periodically check Granola for new meetings and export them in the background.",
            font=f(12), text_color=TEXT_SECONDARY,
            wraplength=440, justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 10))

        # Toggle row
        toggle_row = ctk.CTkFrame(sec, fg_color="transparent")
        toggle_row.pack(fill="x", padx=18, pady=(0, 8))

        self._settings_auto_scan_var = tk.BooleanVar(value=self.prefs.auto_scan_enabled)
        ctk.CTkSwitch(
            toggle_row, text="Enable auto-scan",
            variable=self._settings_auto_scan_var,
            font=f(13), text_color=TEXT_PRIMARY,
            progress_color=ACCENT, button_color="#FFFFFF", button_hover_color="#FFFFFF",
        ).pack(side="left")

        # Interval row
        interval_row = ctk.CTkFrame(sec, fg_color="transparent")
        interval_row.pack(fill="x", padx=18, pady=(4, 14))

        ctk.CTkLabel(
            interval_row, text="Frequency:",
            font=f(13), text_color=TEXT_SECONDARY,
        ).pack(side="left")

        # Map current interval back to its label
        current_label = next(
            (k for k, v in self.AUTO_SCAN_INTERVAL_OPTIONS.items()
             if v == self.prefs.auto_scan_interval_minutes),
            "Every 2 hours",
        )
        self._settings_interval_var = tk.StringVar(value=current_label)
        ctk.CTkOptionMenu(
            interval_row, values=list(self.AUTO_SCAN_INTERVAL_OPTIONS.keys()),
            variable=self._settings_interval_var,
            font=f(12),
            fg_color=GHOST_BTN, button_color=GHOST_BTN, button_hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT, dropdown_fg_color=BG_PANEL,
            dropdown_text_color=TEXT_PRIMARY, dropdown_hover_color=GHOST_BTN_HOVER,
            corner_radius=6, height=28, width=180,
        ).pack(side="left", padx=(8, 0))

        # Notify row
        self._settings_notify_var = tk.BooleanVar(value=self.prefs.notify_on_new)
        ctk.CTkSwitch(
            sec, text="Show macOS notifications for scan events",
            variable=self._settings_notify_var,
            font=f(13), text_color=TEXT_PRIMARY,
            progress_color=ACCENT,
        ).pack(anchor="w", padx=18, pady=(0, 8))

        # Background-mode (launchd) toggle + status
        self._settings_bg_var = tk.BooleanVar(value=self.prefs.background_scan_enabled)
        ctk.CTkSwitch(
            sec, text="Also run when the app is closed (background daemon)",
            variable=self._settings_bg_var,
            font=f(13), text_color=TEXT_PRIMARY,
            progress_color=ACCENT,
        ).pack(anchor="w", padx=18, pady=(0, 4))

        # Inline status of the launch agent
        bg_status_text = (
            "✓ Background daemon is currently active (via macOS launchd)"
            if is_launch_agent_installed() else
            "Background daemon is not installed"
        )
        bg_status_color = ACCENT if is_launch_agent_installed() else TEXT_TERTIARY
        self._settings_bg_status = ctk.CTkLabel(
            sec, text=bg_status_text, font=f(11), text_color=bg_status_color,
        )
        self._settings_bg_status.pack(anchor="w", padx=18, pady=(0, 14))

        # Section: Last scan info + manual trigger
        info_sec = ctk.CTkFrame(win, fg_color=BG_PANEL, corner_radius=12,
                                 border_width=1, border_color=BORDER)
        info_sec.pack(fill="x", padx=22, pady=(0, 12))

        ctk.CTkLabel(
            info_sec, text="Last scan", font=f(14, "bold"), text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(14, 2))

        last = self.prefs.last_scan_iso
        last_dt = parse_iso(last) if last else None
        if last_dt:
            last_str = last_dt.astimezone().strftime("%a %b %d, %H:%M")
            count_str = (f"  ·  {self.prefs.last_scan_new_count} new "
                         f"({self.prefs.last_scan_fetched_count} with transcripts)")
        else:
            last_str = "Never"
            count_str = ""

        ctk.CTkLabel(
            info_sec, text=last_str + count_str,
            font=f(12), text_color=TEXT_SECONDARY,
        ).pack(anchor="w", padx=18, pady=(0, 10))

        ctk.CTkButton(
            info_sec, text="Scan now", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=30, width=110,
            command=lambda: self._kick_auto_scan(manual=True),
        ).pack(anchor="w", padx=18, pady=(0, 14))

        # Section: Diagnostics
        diag_sec = ctk.CTkFrame(win, fg_color=BG_PANEL, corner_radius=12,
                                 border_width=1, border_color=BORDER)
        diag_sec.pack(fill="x", padx=22, pady=(0, 12))

        ctk.CTkLabel(
            diag_sec, text="Diagnostics", font=f(14, "bold"), text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(
            diag_sec,
            text="Walks through every step of the connection flow and writes the result to the log panel "
                 "in the main window. Useful when reconnect isn't working.",
            font=f(12), text_color=TEXT_SECONDARY,
            wraplength=440, justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 10))
        ctk.CTkButton(
            diag_sec, text="🔍  Diagnose connection", font=f(12, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            height=30, width=180,
            command=self._run_diagnose,
        ).pack(anchor="w", padx=18, pady=(0, 14))

        # Footer: caveat + buttons
        ctk.CTkLabel(
            win,
            text="Auto-scan only runs while the app is open.",
            font=f(11), text_color=TEXT_TERTIARY,
        ).pack(anchor="w", padx=22, pady=(0, 10))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=22, pady=18)

        ctk.CTkButton(
            btn_row, text="Cancel", font=f(13),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=34, width=90,
            command=win.destroy,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Save", font=f(13, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            height=34, width=90, command=self._save_settings,
        ).pack(side="right")

    def _run_diagnose(self):
        """Run the verbose connection diagnose in a worker thread + open the
        log panel so the user sees the live output."""
        # Make sure the main-window log panel is visible
        if not self.log_visible:
            self._toggle_log()
        self._log("")
        threading.Thread(
            target=lambda: diagnose_connection(self._log),
            daemon=True,
        ).start()

    def _save_settings(self):
        self.prefs.auto_scan_enabled = self._settings_auto_scan_var.get()
        self.prefs.notify_on_new = self._settings_notify_var.get()
        label = self._settings_interval_var.get()
        new_interval = self.AUTO_SCAN_INTERVAL_OPTIONS.get(label, 120)
        interval_changed = new_interval != self.prefs.auto_scan_interval_minutes
        self.prefs.auto_scan_interval_minutes = new_interval

        bg_wanted = bool(self._settings_bg_var.get())
        self.prefs.background_scan_enabled = bg_wanted

        save_preferences(self.prefs)

        # Sync the launch agent state
        if bg_wanted and self.prefs.auto_scan_enabled:
            ok, msg = install_launch_agent(self.prefs.auto_scan_interval_minutes)
            self._log(f"Background daemon: {msg}")
            if not ok:
                messagebox.showerror("Couldn't install background daemon", msg)
        elif not bg_wanted and is_launch_agent_installed():
            ok, msg = uninstall_launch_agent()
            self._log(f"Background daemon: {msg}")
        elif bg_wanted and not self.prefs.auto_scan_enabled:
            # User asked for background but turned auto-scan off — uninstall
            uninstall_launch_agent()
            self._log("Background daemon uninstalled (auto-scan is off)")
        elif interval_changed and is_launch_agent_installed():
            # Interval changed while bg already installed — reinstall with new interval
            install_launch_agent(self.prefs.auto_scan_interval_minutes)
            self._log(f"Background daemon reloaded with new interval ({new_interval} min)")

        self._log(f"Settings saved (auto-scan={'on' if self.prefs.auto_scan_enabled else 'off'}, "
                  f"every {self.prefs.auto_scan_interval_minutes}min, "
                  f"background={'on' if bg_wanted else 'off'})")
        self._reschedule_auto_scan()
        if self._settings_win.winfo_exists():
            self._settings_win.destroy()

    # ---------- auto-scan timer ----------

    def _reschedule_auto_scan(self):
        """Cancel any pending tick and reschedule based on current prefs."""
        if self._auto_scan_after_id:
            try:
                self.after_cancel(self._auto_scan_after_id)
            except Exception:
                pass
            self._auto_scan_after_id = None
        if not self.prefs.auto_scan_enabled:
            return
        ms = max(60_000, self.prefs.auto_scan_interval_minutes * 60_000)
        self._auto_scan_after_id = self.after(ms, self._auto_scan_tick)
        self._log(f"Auto-scan armed — next check in "
                  f"{self.prefs.auto_scan_interval_minutes} min")

    def _auto_scan_tick(self):
        """Periodic timer fired by self.after()."""
        self._auto_scan_after_id = None
        # Defer if user is busy interacting with the app
        if self.worker_busy:
            self._auto_scan_after_id = self.after(5 * 60_000, self._auto_scan_tick)
            return
        self._kick_auto_scan(manual=False)

    def _kick_auto_scan(self, manual: bool = False):
        """Run a scan in the background. manual=True is from 'Scan now' button."""
        threading.Thread(
            target=self._auto_scan_worker, args=(manual,), daemon=True,
        ).start()

    def _auto_scan_worker(self, manual: bool):
        # Notify "scan started"
        if self.prefs.notify_on_new:
            self.menubar.notify(
                title="Granola Export",
                message="Checking Granola for new meetings…",
            )
        self.menubar.set_title("📓⟳")  # spinning hint while scanning

        try:
            # 1) Get fresh token
            try:
                token, source, remaining = load_access_token()
                self.token = token
                self.token_remaining = remaining
                mark_auth_ok(self.prefs)
                save_preferences(self.prefs)
            except AuthError:
                self._log("Auto-scan: skipping — no valid Granola session")
                fired = maybe_notify_auth_expired(self.prefs)
                save_preferences(self.prefs)
                if fired:
                    self._log("  → notification sent (session expired)")
                return

            # 2) Reload meeting list
            try:
                docs = load_documents()
            except Exception as e:
                self._log(f"Auto-scan: cache load failed: {e}")
                return

            out_dir = Path(self.out_root.get()) / "transcripts"
            out_dir.mkdir(parents=True, exist_ok=True)
            existing = scan_existing(out_dir)
            new_docs = [d for d in docs if meeting_filename(d) not in existing]

            if not new_docs:
                self._log(f"Auto-scan: no new meetings ({len(docs)} total in Granola)")
                self.prefs.last_scan_iso = datetime.now().isoformat(timespec="seconds")
                self.prefs.last_scan_new_count = 0
                self.prefs.last_scan_fetched_count = 0
                save_preferences(self.prefs)
                # Notify "scan complete (no new)"
                if self.prefs.notify_on_new:
                    self.menubar.notify(
                        title="Granola Export",
                        message="Scan complete — no new meetings.",
                    )
                return

            # 3) Fetch + write each new meeting
            self._log(f"Auto-scan: found {len(new_docs)} new meetings, exporting…")
            entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}
            fetched = errors = 0
            for doc in new_docs:
                title = doc.get("title") or "Untitled"
                segments = None
                try:
                    resp = fetch_transcript(doc["id"], self.token)
                    if isinstance(resp, list):
                        segments = resp
                        if segments:
                            fetched += 1
                    elif isinstance(resp, dict):
                        for k in ("transcript", "segments", "data"):
                            if isinstance(resp.get(k), list):
                                segments = resp[k]
                                if segments:
                                    fetched += 1
                                break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        self._log("Auto-scan: token expired mid-scan — stopping")
                        fired = maybe_notify_auth_expired(self.prefs)
                        save_preferences(self.prefs)
                        if fired:
                            self._log("  → notification sent (session expired)")
                        return
                    errors += 1
                    self._log(f"  Auto-scan HTTP {e.code} for {title}")
                    continue
                except Exception as e:
                    errors += 1
                    self._log(f"  Auto-scan error: {e}")
                    continue

                try:
                    _, meta = write_meeting_file(out_dir, doc, segments)
                    entries_by_name[meta.filename] = meta
                except Exception as e:
                    errors += 1
                    self._log(f"  Auto-scan write error: {e}")

            try:
                write_index(Path(self.out_root.get()), list(entries_by_name.values()))
            except Exception as e:
                self._log(f"Auto-scan index error: {e}")

            self._log(
                f"Auto-scan complete · {len(new_docs)} new · {fetched} with transcripts · {errors} errors"
            )

            # 4) Persist scan stats
            self.prefs.last_scan_iso = datetime.now().isoformat(timespec="seconds")
            self.prefs.last_scan_new_count = len(new_docs)
            self.prefs.last_scan_fetched_count = fetched
            save_preferences(self.prefs)

            # 5) Notify "new meetings detected" — clickable, opens folder
            if self.prefs.notify_on_new:
                singular = len(new_docs) == 1
                phrase = "a new meeting has" if singular else f"{len(new_docs)} new meetings have"
                self.menubar.notify(
                    title="Granola Export",
                    message=f"Hey, {phrase} been detected, and the transcription has been exported to your computer.",
                    subtitle=f"{fetched} with transcripts" if fetched else "Notes only",
                    action_key="open_folder",
                )

            # Refresh the visible list so new pills flip + counts update
            self.after(0, self.refresh)

        except Exception as e:
            self._log(f"Auto-scan unexpected error: {type(e).__name__}: {e}")
        finally:
            self.menubar.set_title("📓")  # restore idle icon
            # Schedule the next interval (whether this was manual or timed)
            self.after(0, self._reschedule_auto_scan)

    def _show_data_info(self):
        """Modal explaining where the meeting list + transcripts come from."""
        if hasattr(self, "_info_win") and self._info_win.winfo_exists():
            self._info_win.lift()
            return

        win = ctk.CTkToplevel(self)
        self._info_win = win
        win.title("Where does this data come from?")
        win.geometry("560x540")
        win.configure(fg_color=BG_WINDOW)
        win.resizable(False, False)
        win.transient(self)

        ctk.CTkLabel(
            win, text="Where the data comes from",
            font=f(20, "bold", display=True), text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=22, pady=(20, 10))

        body = ctk.CTkFrame(win, fg_color=BG_PANEL, corner_radius=10,
                            border_width=1, border_color=BORDER)
        body.pack(fill="both", expand=True, padx=22, pady=(0, 12))

        sections = [
            ("Meeting list",
             "Read locally from the Granola desktop app's cache file:\n"
             "~/Library/Application Support/Granola/cache-v6.json\n\n"
             "Granola syncs this file automatically when you open the app."),
            ("Transcript text",
             "Fetched from Granola's cloud at export time:\n"
             "POST https://api.granola.ai/v1/get-document-transcript\n\n"
             "Granola only stores transcripts in their cloud — they're not on your Mac until you export them here."),
            ("Authentication",
             "Read locally from the Granola desktop app's session file:\n"
             "~/Library/Application Support/Granola/supabase.json\n\n"
             "If your session expires (~6h), use Reconnect."),
            ("Limitations",
             "• You need internet access at export time.\n"
             "• Meetings without a recording return 404 — only their notes export.\n"
             "• If Granola deletes a transcript on their end (retention, privacy), it disappears for us too.\n"
             "• Granola doesn't officially support this endpoint, so it could break in a future update."),
            ("Privacy",
             "Your transcripts only travel between Granola's cloud (HTTPS) and this app on your Mac.\n"
             "Exported Markdown files stay on your computer in the output folder you chose."),
        ]

        for heading, text in sections:
            ctk.CTkLabel(
                body, text=heading, font=f(13, "bold"), text_color=TEXT_PRIMARY,
                anchor="w",
            ).pack(anchor="w", padx=18, pady=(14, 2))
            ctk.CTkLabel(
                body, text=text, font=f(12), text_color=TEXT_SECONDARY,
                wraplength=480, justify="left", anchor="w",
            ).pack(anchor="w", padx=18, pady=(0, 4))

        ctk.CTkButton(
            win, text="Close", font=f(13, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            height=34, width=100, command=win.destroy,
        ).pack(side="bottom", anchor="e", padx=22, pady=(0, 18))

    def _show_reconnect_dialog(self):
        """Modal explaining auth + offering to open Granola + watch for refresh."""
        if hasattr(self, "_reconn_win") and self._reconn_win.winfo_exists():
            self._reconn_win.lift()
            return

        win = ctk.CTkToplevel(self)
        self._reconn_win = win
        win.title("Granola Connection")
        win.geometry("520x420")
        win.configure(fg_color=BG_WINDOW)
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(
            win, text="Connection",
            font=f(20, "bold", display=True), text_color=TEXT_PRIMARY,
        ).pack(anchor="w", padx=22, pady=(20, 4))

        if self.token and self.token_remaining > 0:
            mins = self.token_remaining // 60
            msg = (f"You're connected to Granola.\nSession expires in about {mins} minute{'s' if mins != 1 else ''}.\n\n"
                   "When it expires, click Reconnect — the app will refresh the session "
                   "automatically (no Granola interaction needed in most cases).")
        elif self._last_auth_error:
            # Surface the specific failure reason from load_access_token
            msg = self._last_auth_error
        else:
            msg = ("Your Granola session has expired (or never started).\n\n"
                   "Click Reconnect — this opens the Granola desktop app. As soon as you sign in there, "
                   "this app will auto-detect the new session.")

        ctk.CTkLabel(
            win, text=msg, font=f(13), text_color=TEXT_SECONDARY,
            wraplength=460, justify="left",
        ).pack(anchor="w", padx=22, pady=(4, 16))

        # Status line for the watch
        self._reconn_status = ctk.CTkLabel(
            win, text="", font=f(12), text_color=TEXT_TERTIARY,
        )
        self._reconn_status.pack(anchor="w", padx=22)

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=22, pady=18)

        ctk.CTkButton(
            btn_row, text="Cancel", font=f(13),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=34, width=90,
            command=lambda: self._close_reconnect_dialog(),
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Reconnect", font=f(13, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            height=34, width=130, command=self._start_auth_watch,
        ).pack(side="right")

    def _close_reconnect_dialog(self):
        self._auth_watch_active = False
        if self._auth_watch_after_id:
            try:
                self.after_cancel(self._auth_watch_after_id)
            except Exception:
                pass
            self._auth_watch_after_id = None
        if hasattr(self, "_reconn_win") and self._reconn_win.winfo_exists():
            self._reconn_win.destroy()

    def _start_auth_watch(self):
        """Try a self-refresh first; if that fails open Granola.app and watch
        for it to write a new session."""
        self._reconn_status.configure(
            text="Refreshing your session…",
            text_color=TEXT_SECONDARY,
        )
        # Force the UI to redraw before we block on the network.
        self.update_idletasks()

        # Step 1 — attempt a token refresh ourselves. If Granola has a valid
        # refresh_token, this succeeds without the user having to do anything.
        try:
            token, source, remaining = load_access_token()
            self.token = token
            self.token_remaining = remaining
            self._set_chip(f"● Connected · {remaining // 60}m", CHIP_OK_FG, CHIP_OK_BG)
            self._reconn_status.configure(
                text=f"✓ Reconnected via {source}. Loading meetings…",
                text_color=ACCENT,
            )
            self._log(f"Reconnected via {source} (no Granola interaction needed)")
            self.after(700, self._close_reconnect_dialog)
            self.after(800, self.refresh)
            return
        except AuthError as e:
            self._last_auth_error = str(e)
            self._log(f"Refresh attempt failed: {e}")
            # Show the specific reason on the status line.
            preview = str(e).split("\n", 1)[0][:200]
            self._reconn_status.configure(
                text=f"✗ {preview}",
                text_color=DANGER,
            )
            self.update_idletasks()

        # Step 2 — refresh failed. Open Granola and tell the user to interact
        # with it (clicking around forces Granola to make an authenticated API
        # call, which forces it to write a fresh refresh_token to supabase.json).
        import subprocess
        try:
            subprocess.Popen(["open", "-a", "Granola"])
        except Exception as e:
            messagebox.showerror("Couldn't open Granola", str(e))
            return

        try:
            self._initial_mtime = SUPABASE_FILE.stat().st_mtime
        except Exception:
            self._initial_mtime = 0
        self._auth_watch_active = True
        self._auth_watch_started = time.time()
        self._reconn_status.configure(
            text="Granola opened — click any meeting in Granola to wake the session, "
                 "then this app will reconnect automatically.",
            text_color=TEXT_SECONDARY,
        )
        self._poll_supabase()

    def _poll_supabase(self):
        if not self._auth_watch_active:
            return
        elapsed = int(time.time() - self._auth_watch_started)
        if elapsed > 600:  # 10 minutes
            self._reconn_status.configure(
                text="Stopped watching after 10 minutes. Click Reconnect again when you've quit + reopened Granola.",
                text_color=DANGER,
            )
            self._auth_watch_active = False
            return

        try:
            mtime = SUPABASE_FILE.stat().st_mtime
        except Exception:
            mtime = 0

        if mtime > self._initial_mtime:
            # File changed — try loading fresh token
            try:
                token, source, remaining = load_access_token()
                self.token = token
                self.token_remaining = remaining
                self._set_chip(f"● Connected · {remaining // 60}m", CHIP_OK_FG, CHIP_OK_BG)
                self._reconn_status.configure(
                    text="✓ Reconnected. Loading meetings…",
                    text_color=ACCENT,
                )
                self._log(f"Reconnected via {source}")
                self.after(700, self._close_reconnect_dialog)
                self.after(800, self.refresh)
                self._auth_watch_active = False
                return
            except AuthError as e:
                # File changed but still no valid token — keep waiting + log
                self._log(f"  supabase.json changed but refresh still fails: {str(e)[:80]}…")
                self._initial_mtime = mtime

        # Friendlier status text — tells the user what to do, not just a counter
        mins, secs = divmod(elapsed, 60)
        self._reconn_status.configure(
            text=f"Waiting for Granola to write a fresh session ({mins}m {secs}s)…\n"
                 f"If this is taking long, quit Granola (Cmd+Q) and reopen it.",
            text_color=TEXT_SECONDARY,
        )
        self._auth_watch_after_id = self.after(1000, self._poll_supabase)

    # ---------- refresh ----------

    def refresh(self):
        if self.worker_busy:
            return
        self._set_busy(True)
        self._set_status("Loading meetings…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        """Always releases worker_busy in finally so the UI can never get stuck."""
        try:
            try:
                token, source, remaining = load_access_token()
                self.token = token
                self.token_remaining = remaining
                self._log(f"Authenticated via {source} (~{remaining // 60}min remaining)")
                self.after(0, lambda r=remaining: self._set_chip(
                    f"● Connected · {r // 60}m", CHIP_OK_FG, CHIP_OK_BG,
                ))
                # Reset auth-expired throttle so next failure can fire a fresh notification
                mark_auth_ok(self.prefs)
                save_preferences(self.prefs)
            except AuthError as e:
                self._log(f"AUTH ERROR: {e}")
                self.token = None
                self._last_auth_error = str(e)
                self.after(0, lambda: self._set_chip("● Session expired", CHIP_ERR_FG, CHIP_ERR_BG))
                self.after(0, lambda: self._set_status("Authentication required — click the badge", DANGER))
                self.after(0, self._show_reconnect_dialog)
                return

            try:
                docs = load_documents()
            except Exception as e:
                self._log(f"ERROR loading cache: {e}")
                return

            out_dir = Path(self.out_root.get()) / "transcripts"
            existing = scan_existing(out_dir)

            self.docs = docs
            self.existing = existing
            self._apply_sort()
            new_count = sum(1 for d in docs if meeting_filename(d) not in existing)

            self.checked = {d["id"] for d in docs if meeting_filename(d) not in existing}
            self.current_page = 0

            self.after(0, self._render_current_page)
            self.after(0, lambda nc=new_count: self.summary_label.configure(
                text=f"{len(docs)} meetings · {nc} new · {len(docs) - nc} exported"
            ))
            self.after(0, lambda nc=new_count: self._set_status(
                f"{nc} new meeting{'s' if nc != 1 else ''} ready to export" if nc else "All caught up",
                ACCENT if nc else TEXT_PRIMARY,
            ))
            self.after(0, self._update_count)
        except Exception as e:
            self._log(f"REFRESH WORKER unexpected error: {type(e).__name__}: {e}")
        finally:
            # Guarantees the UI is unlocked, even if anything above raised.
            self.after(0, lambda: self._set_busy(False))

    # ---------- export ----------

    def export(self):
        if self.worker_busy or not self.checked:
            return
        if not self.token:
            self._show_reconnect_dialog()
            return

        out_root = Path(self.out_root.get())
        out_dir = out_root / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)

        to_export = [d for d in self.docs if d["id"] in self.checked]
        self._set_busy(True)
        self.progress.set(0)
        self._set_status(f"Exporting 0 / {len(to_export)}…")
        threading.Thread(
            target=self._export_worker, args=(to_export, out_root, out_dir), daemon=True
        ).start()

    def _export_worker(self, docs: list[dict], out_root: Path, out_dir: Path):
        """Always releases worker_busy in finally so the UI never gets stuck."""
        fetched = no_tx = errors = 0
        entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}
        total = len(docs)

        try:
            for i, doc in enumerate(docs, 1):
                title = doc.get("title") or "Untitled"
                self._log(f"[{i}/{total}] {title}")

                segments = None
                try:
                    resp = fetch_transcript(doc["id"], self.token)
                    if resp is None:
                        no_tx += 1
                    elif isinstance(resp, list):
                        segments = resp
                        if segments:
                            fetched += 1
                    elif isinstance(resp, dict):
                        for key in ("transcript", "segments", "data"):
                            if isinstance(resp.get(key), list):
                                segments = resp[key]
                                if segments:
                                    fetched += 1
                                break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        self._log("AUTH expired mid-run — opening reconnect dialog.")
                        self.token = None
                        self.after(0, lambda: self._set_chip(
                            "● Session expired", CHIP_ERR_FG, CHIP_ERR_BG))
                        self.after(0, lambda: self._set_status(
                            "Auth expired mid-export. Reconnect and re-run.", DANGER))
                        self.after(0, self._show_reconnect_dialog)
                        return
                    errors += 1
                    self._log(f"  HTTP {e.code} for {title}")
                except Exception as e:
                    errors += 1
                    self._log(f"  ERROR: {e}")

                try:
                    _, meta = write_meeting_file(out_dir, doc, segments)
                    entries_by_name[meta.filename] = meta
                    self.after(0, self._mark_exported, doc["id"])
                except Exception as e:
                    self._log(f"  WRITE ERROR: {e}")

                self.after(0, lambda v=i, n=total: self.progress.set(v / n))
                self.after(0, lambda i=i, n=total: self._set_status(f"Exporting {i} / {n}…"))
                time.sleep(0.15)

            try:
                write_index(out_root, list(entries_by_name.values()))
                self._log(f"Wrote INDEX.md ({len(entries_by_name)} entries)")
            except Exception as e:
                self._log(f"INDEX ERROR: {e}")

            self._log(f"\n✅ Done — {fetched} transcripts, {no_tx} without, {errors} errors")
            self.after(0, lambda: self.progress.set(1))
            self.after(0, lambda f=fetched, e=errors: self._set_status(
                f"Done · {f} transcripts · {e} errors",
                ACCENT if not e else TEXT_PRIMARY,
            ))
            self.existing = scan_existing(out_dir)
            self.checked.clear()
            self.after(0, self._update_count)
        except Exception as e:
            self._log(f"EXPORT WORKER unexpected error: {type(e).__name__}: {e}")
        finally:
            # GUARANTEED: always re-enable buttons no matter what
            self.after(0, lambda: self._set_busy(False))

    def _mark_exported(self, doc_id: str):
        w = self.row_widgets.get(doc_id)
        if not w:
            return
        w["row"].configure(fg_color=BG_CARD)
        w["bg"] = BG_CARD
        w["is_new"] = False
        w["pill"].configure(text="EXPORTED", text_color=PILL_EXPORTED_FG, fg_color=PILL_EXPORTED_BG)
        w["var"].set(False)

    def _on_external_export(self, doc_id: str):
        """Called by a detail window after it exported its meeting."""
        # Update existing-set + row visuals
        out_dir = Path(self.out_root.get()) / "transcripts"
        self.existing = scan_existing(out_dir)
        self._mark_exported(doc_id)
        # Update summary count
        new_count = sum(1 for d in self.docs if meeting_filename(d) not in self.existing)
        self.summary_label.configure(
            text=f"{len(self.docs)} meetings · {new_count} new · {len(self.docs) - new_count} exported"
        )


# ---------- meeting detail window ----------

class MeetingDetailWindow(ctk.CTkToplevel):
    SPEAKER_COLOR_ME = "#0F8A47"      # green for "Me"
    SPEAKER_COLOR_THEM = "#7C3AED"    # violet for "Them"

    def __init__(self, app: "App", doc: dict):
        super().__init__(app, fg_color=BG_WINDOW)
        self.app = app
        self.doc = doc
        self.title(doc.get("title") or "Untitled meeting")
        self.geometry("820x720")
        self.minsize(560, 460)

        self._build_ui()
        self.after(50, self._load_content)

    # ---------- UI ----------

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 8))

        title_text = self.doc.get("title") or "Untitled meeting"
        ctk.CTkLabel(
            header, text=title_text, font=f(22, "bold", display=True),
            text_color=TEXT_PRIMARY, wraplength=720, justify="left", anchor="w",
        ).pack(anchor="w")

        # Metadata row
        meta_parts = []
        dt = parse_iso(self.doc.get("created_at"))
        if dt:
            meta_parts.append(dt.astimezone().strftime("%A, %b %d, %Y · %H:%M"))
        people = self._people_names()
        if people:
            meta_parts.append(", ".join(people))
        ctk.CTkLabel(
            header, text="  ·  ".join(meta_parts) or "—",
            font=f(12), text_color=TEXT_SECONDARY, anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        # Action bar
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(8, 8))

        self.btn_save = ctk.CTkButton(
            actions, text="💾  Save as Markdown", font=f(12, "bold"),
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            text_color=NEUTRAL_BTN_TEXT, corner_radius=8,
            height=30, width=170, command=self._save_to_disk,
            state="disabled",
        )
        self.btn_save.pack(side="left")

        self.btn_reveal = ctk.CTkButton(
            actions, text="📂  Reveal in Finder", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=30, width=160, command=self._reveal_in_finder,
            state="disabled",
        )
        self.btn_reveal.pack(side="left", padx=(8, 0))

        self.btn_copy = ctk.CTkButton(
            actions, text="📋  Copy", font=f(12),
            fg_color=GHOST_BTN, hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT,
            border_color=GHOST_BTN_BORDER, border_width=1,
            corner_radius=8, height=30, width=80, command=self._copy_to_clipboard,
            state="disabled",
        )
        self.btn_copy.pack(side="left", padx=(8, 0))

        self.status_lbl = ctk.CTkLabel(
            actions, text="", font=f(12), text_color=TEXT_TERTIARY,
        )
        self.status_lbl.pack(side="right")

        # Content area
        wrap = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        wrap.pack(fill="both", expand=True, padx=24, pady=(4, 20))

        # tk.Text for rich markdown rendering with tags
        text_frame = tk.Frame(wrap, bg=BG_PANEL, highlightthickness=0)
        text_frame.pack(fill="both", expand=True, padx=12, pady=12)

        self.text = tk.Text(
            text_frame,
            wrap="word", relief="flat", borderwidth=0,
            bg=BG_PANEL, fg=TEXT_PRIMARY,
            padx=14, pady=10, spacing3=2,
            font=(FONT_FAMILY, 13),
        )
        scrollbar = tk.Scrollbar(text_frame, command=self.text.yview, width=12,
                                  bg=BG_PANEL, troughcolor=BG_PANEL,
                                  highlightthickness=0, relief="flat", bd=0,
                                  activebackground=TEXT_TERTIARY)
        self.text.configure(yscrollcommand=scrollbar.set, state="disabled")
        scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self._configure_text_tags()

    def _configure_text_tags(self):
        t = self.text
        t.tag_configure("h1", font=(FONT_FAMILY_DISPLAY, 20, "bold"),
                        foreground=TEXT_PRIMARY, spacing1=4, spacing3=10)
        t.tag_configure("h2", font=(FONT_FAMILY, 15, "bold"),
                        foreground=TEXT_PRIMARY, spacing1=12, spacing3=6)
        t.tag_configure("h3", font=(FONT_FAMILY, 13, "bold"),
                        foreground=TEXT_PRIMARY, spacing1=8, spacing3=4)
        t.tag_configure("body", font=(FONT_FAMILY, 13),
                        foreground=TEXT_PRIMARY, spacing3=4, lmargin1=0, lmargin2=0)
        t.tag_configure("muted", font=(FONT_FAMILY, 11),
                        foreground=TEXT_TERTIARY)
        t.tag_configure("speaker_me", font=(FONT_FAMILY, 13, "bold"),
                        foreground=self.SPEAKER_COLOR_ME)
        t.tag_configure("speaker_them", font=(FONT_FAMILY, 13, "bold"),
                        foreground=self.SPEAKER_COLOR_THEM)
        t.tag_configure("speaker_other", font=(FONT_FAMILY, 13, "bold"),
                        foreground=TEXT_SECONDARY)
        t.tag_configure("bullet", font=(FONT_FAMILY, 13),
                        foreground=TEXT_PRIMARY, lmargin1=18, lmargin2=32)
        t.tag_configure("placeholder", font=(FONT_FAMILY, 14),
                        foreground=TEXT_TERTIARY, justify="center", spacing1=80)

    # ---------- helpers ----------

    def _people_names(self) -> list[str]:
        people = self.doc.get("people") or {}
        names: list[str] = []
        if isinstance(people, dict):
            for grp in people.values():
                if isinstance(grp, list):
                    for p in grp:
                        if isinstance(p, dict):
                            n = p.get("name") or p.get("email")
                            if n:
                                names.append(n)
        return names

    def _existing_path(self) -> Path | None:
        """Path to the already-exported .md file, if it exists."""
        out_dir = Path(self.app.out_root.get()) / "transcripts"
        path = out_dir / meeting_filename(self.doc)
        return path if path.exists() else None

    def _set_status(self, text: str, color: str = TEXT_TERTIARY):
        self.status_lbl.configure(text=text, text_color=color)

    def _set_text(self, content: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._render_markdown(content)
        self.text.configure(state="disabled")

    # ---------- content loading ----------

    def _load_content(self):
        existing = self._existing_path()
        if existing:
            self._set_status(f"Loaded from {existing.name}", TEXT_SECONDARY)
            self._set_text(existing.read_text())
            self.btn_save.configure(state="normal", text="💾  Re-fetch & save")
            self.btn_reveal.configure(state="normal")
            self.btn_copy.configure(state="normal")
        else:
            # Not exported yet — show placeholder + offer to fetch
            self._show_placeholder()

    def _show_placeholder(self):
        if not self.app.token:
            self._set_text(
                "_Not exported yet, and no Granola session is active._\n\n"
                "Click **Reconnect** in the main window's connection chip, then re-open this meeting."
            )
            self.btn_save.configure(state="disabled")
            return

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "Transcript not exported yet.\n\nClick \"Save as Markdown\" to fetch it.", "placeholder")
        self.text.configure(state="disabled")
        self.btn_save.configure(state="normal", text="⬇  Fetch transcript")

    # ---------- actions ----------

    def _save_to_disk(self):
        if not self.app.token:
            messagebox.showerror("Not connected", "Reconnect to Granola first.")
            return
        self.btn_save.configure(state="disabled", text="Fetching…")
        self._set_status("Fetching transcript from Granola cloud (this may take a few seconds for long meetings)…", TEXT_SECONDARY)
        # Animated dot pulse while fetching
        self._pulse_active = True
        self._pulse_step = 0
        self.after(400, self._tick_pulse)
        threading.Thread(target=self._fetch_and_save_worker, daemon=True).start()

    def _tick_pulse(self):
        if not getattr(self, "_pulse_active", False):
            return
        dots = "." * (1 + (self._pulse_step % 3))
        self.btn_save.configure(text=f"Fetching{dots}")
        self._pulse_step += 1
        self.after(400, self._tick_pulse)

    def _fetch_and_save_worker(self):
        """Fetch + save the transcript.

        All UI updates are bundled into ONE atomic main-thread call at the end,
        so we can never end up in a half-updated state.
        """
        log = self.app._log
        title = self.doc.get("title") or "Untitled"
        log(f"[detail:{title[:30]}] worker started")

        # Final state snapshot — populated as we go, applied once at the end.
        result = {
            "status_text": "Done",
            "status_color": ACCENT,
            "button_text": "💾  Re-fetch & save",
            "content": None,         # if set, will replace the text widget
            "enable_extras": False,  # reveal + copy + parent-row update
        }

        try:
            out_dir = Path(self.app.out_root.get()) / "transcripts"
            out_dir.mkdir(parents=True, exist_ok=True)

            # 1) Fetch
            try:
                resp = fetch_transcript(self.doc["id"], self.app.token)
                log(f"[detail:{title[:30]}] fetch ok ({type(resp).__name__})")
            except urllib.error.HTTPError as e:
                log(f"[detail:{title[:30]}] HTTP {e.code}")
                if e.code in (401, 403):
                    result["status_text"] = f"Auth expired (HTTP {e.code}) — Reconnect"
                else:
                    result["status_text"] = f"HTTP {e.code} from Granola"
                result["status_color"] = DANGER
                result["button_text"] = "⬇  Try again"
                return
            except Exception as e:
                log(f"[detail:{title[:30]}] fetch error: {e}")
                result["status_text"] = f"Network error: {e}"
                result["status_color"] = DANGER
                result["button_text"] = "⬇  Try again"
                return

            # 2) Normalise response
            segments = None
            if resp is None:
                segments = None
            elif isinstance(resp, list):
                segments = resp
            elif isinstance(resp, dict):
                for key in ("transcript", "segments", "data"):
                    if isinstance(resp.get(key), list):
                        segments = resp[key]
                        break
            log(f"[detail:{title[:30]}] segments: {len(segments) if segments else 0}")

            # 3) Write to disk
            try:
                path, _meta = write_meeting_file(out_dir, self.doc, segments)
                log(f"[detail:{title[:30]}] wrote {path.name}")
            except Exception as e:
                log(f"[detail:{title[:30]}] write error: {e}")
                result["status_text"] = f"Write error: {e}"
                result["status_color"] = DANGER
                result["button_text"] = "⬇  Try again"
                return

            # 4) Read content for the text widget (do this in worker thread, not UI thread)
            try:
                result["content"] = path.read_text()
                log(f"[detail:{title[:30]}] read {len(result['content'])} chars")
            except Exception as e:
                log(f"[detail:{title[:30]}] read error: {e}")

            # 5) Compose final status
            if segments:
                result["status_text"] = f"Saved · {len(segments)} segments"
                result["status_color"] = ACCENT
            elif segments is None:
                result["status_text"] = "No transcript available — saved notes only"
                result["status_color"] = TEXT_TERTIARY
            else:
                result["status_text"] = "Empty transcript — saved notes only"
                result["status_color"] = TEXT_TERTIARY
            result["enable_extras"] = True

        except Exception as e:
            # Catch-all to be sure we never hang the UI
            log(f"[detail:{title[:30]}] unexpected error: {type(e).__name__}: {e}")
            result["status_text"] = f"Error: {e}"
            result["status_color"] = DANGER
            result["button_text"] = "⬇  Try again"

        finally:
            self._pulse_active = False
            log(f"[detail:{title[:30]}] applying results: status={result['status_text']!r}")
            # ONE atomic UI update on the main thread.
            self.after(0, lambda r=result: self._apply_worker_results(r))

    def _apply_worker_results(self, r: dict):
        """Apply all worker results in deterministic order on the UI thread."""
        try:
            self._set_status(r["status_text"], r["status_color"])
            self.btn_save.configure(state="normal", text=r["button_text"])
            if r["enable_extras"]:
                self.btn_reveal.configure(state="normal")
                self.btn_copy.configure(state="normal")
                self.app._on_external_export(self.doc["id"])
            if r["content"] is not None:
                self._set_text(r["content"])
        except Exception as e:
            self.app._log(f"[detail] apply-results error: {e}")

    def _reveal_in_finder(self):
        path = self._existing_path()
        if not path:
            return
        import subprocess
        subprocess.run(["open", "-R", str(path)])

    def _copy_to_clipboard(self):
        path = self._existing_path()
        if not path:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(path.read_text())
            self._set_status("Copied to clipboard", ACCENT)
            self.after(2000, lambda: self._set_status(""))
        except Exception as e:
            self._set_status(f"Copy failed: {e}", DANGER)

    # ---------- markdown rendering ----------

    def _render_markdown(self, raw: str):
        """Render a subset of markdown into the tk.Text widget with tags.

        Handles: YAML frontmatter (skipped/folded into a muted line),
        # / ## / ### headings, **Me:** / **Them:** speaker prefixes,
        - bullets, **bold** spans, blank lines.
        """
        t = self.text
        lines = raw.split("\n")
        i = 0

        # Strip YAML frontmatter
        if lines and lines[0].strip() == "---":
            j = 1
            while j < len(lines) and lines[j].strip() != "---":
                j += 1
            if j < len(lines):
                # Keep date + participants as a muted header line
                meta = {}
                for ln in lines[1:j]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        meta[k.strip()] = v.strip().strip('"')
                date_disp = meta.get("date", "")
                if date_disp:
                    dt = parse_iso(date_disp)
                    if dt:
                        date_disp = dt.astimezone().strftime("%A, %b %d, %Y · %H:%M")
                if date_disp:
                    t.insert("end", date_disp + "\n", "muted")
                i = j + 1

        for ln in lines[i:]:
            if not ln.strip():
                t.insert("end", "\n")
                continue
            if ln.startswith("# "):
                t.insert("end", ln[2:].rstrip() + "\n", "h1")
            elif ln.startswith("## "):
                t.insert("end", ln[3:].rstrip() + "\n", "h2")
            elif ln.startswith("### "):
                t.insert("end", ln[4:].rstrip() + "\n", "h3")
            elif ln.startswith("- ") or ln.startswith("* "):
                t.insert("end", "•  ", "bullet")
                self._insert_inline(ln[2:].rstrip() + "\n", "bullet")
            else:
                self._insert_speaker_or_body(ln + "\n")

    def _insert_speaker_or_body(self, line: str):
        """Detect **Me:** / **Them:** at start, render speaker tag + rest."""
        t = self.text
        for label, tag in [("**Me:**", "speaker_me"), ("**Them:**", "speaker_them")]:
            if line.startswith(label + " "):
                t.insert("end", label.replace("**", "") + " ", tag)
                self._insert_inline(line[len(label) + 1:], "body")
                return
        # Generic **Speaker:** prefix?
        if line.startswith("**") and "**" in line[2:]:
            end = line.index("**", 2)
            speaker = line[2:end]
            t.insert("end", speaker + " ", "speaker_other")
            self._insert_inline(line[end + 2:].lstrip(), "body")
            return
        self._insert_inline(line, "body")

    def _insert_inline(self, text: str, base_tag: str):
        """Insert text with **bold** spans honored."""
        t = self.text
        i = 0
        while i < len(text):
            j = text.find("**", i)
            if j < 0:
                t.insert("end", text[i:], base_tag)
                return
            if j > i:
                t.insert("end", text[i:j], base_tag)
            k = text.find("**", j + 2)
            if k < 0:
                t.insert("end", text[j:], base_tag)
                return
            t.insert("end", text[j + 2:k], (base_tag, "speaker_other"))
            i = k + 2


if __name__ == "__main__":
    import sys as _sys
    if "--scan-once" in _sys.argv:
        from granola_core import run_scan_once
        _sys.exit(0 if run_scan_once() >= 0 else 1)
    App().mainloop()
