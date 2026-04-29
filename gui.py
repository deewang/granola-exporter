#!/usr/bin/env python3
"""Granola Export — modern macOS GUI inspired by Granola's design.

Workflow:
  1. App auto-loads on launch and marks any NEW meetings.
  2. Tick the rows you want (or use Select New / Select All).
  3. Click "Export Selected" → writes Markdown + updates INDEX.md.
"""

import queue
import threading
import time
import tkinter as tk
import urllib.error
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

from granola_core import (
    AuthError,
    DEFAULT_OUT_ROOT,
    SUPABASE_FILE,
    collect_existing_meta,
    fetch_transcript,
    load_access_token,
    load_documents,
    meeting_filename,
    parse_iso,
    scan_existing,
    write_index,
    write_meeting_file,
)

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

        # State
        self.out_root = ctk.StringVar(value=str(DEFAULT_OUT_ROOT))
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

        # Auth-watch state (set when waiting for token refresh)
        self._auth_watch_active = False
        self._auth_watch_after_id = None

        self._build_ui()
        self.after(100, self._drain_log)
        self.after(100, self._tick_token_status)
        self.after(200, self.refresh)

    # ---------- UI construction ----------

    def _build_ui(self):
        self._build_header()
        self._build_toolbar()
        self._build_summary_bar()
        self._build_meeting_list()
        self._build_pagination()
        self._build_footer()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent", height=78)
        header.pack(fill="x", padx=24, pady=(20, 8))
        header.pack_propagate(False)

        # Left: title + subtitle
        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", fill="y")

        ctk.CTkLabel(
            left, text="Granola Export",
            font=f(26, "bold", display=True), text_color=TEXT_PRIMARY,
        ).pack(anchor="w")
        ctk.CTkLabel(
            left, text="Export your meetings as Markdown — sortable, indexed, AI-ready.",
            font=f(13), text_color=TEXT_SECONDARY,
        ).pack(anchor="w", pady=(2, 0))

        # Right: connection status chip
        self.conn_chip = ctk.CTkLabel(
            header, text="● Connecting…",
            font=f(12, "bold"),
            text_color=CHIP_WARN_FG, fg_color=CHIP_WARN_BG,
            corner_radius=14, padx=12, pady=6,
        )
        self.conn_chip.pack(side="right", anchor="ne", pady=(2, 0))
        self.conn_chip.bind("<Button-1>", lambda _e: self._on_chip_click())

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=12,
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
        bar = ctk.CTkFrame(self, fg_color="transparent", height=30)
        bar.pack(fill="x", padx=28, pady=(0, 8))
        bar.pack_propagate(False)

        # Left: summary stats
        self.summary_label = ctk.CTkLabel(
            bar, text="Loading…", font=f(12), text_color=TEXT_SECONDARY,
        )
        self.summary_label.pack(side="left")

        # Right: selected count
        self.selected_label = ctk.CTkLabel(
            bar, text="0 selected", font=f(12, "bold"), text_color=TEXT_PRIMARY,
        )
        self.selected_label.pack(side="right")

        # Sort dropdown next to selected count
        ctk.CTkLabel(
            bar, text="Sort:", font=f(12), text_color=TEXT_SECONDARY,
        ).pack(side="right", padx=(0, 6))

        self.sort_menu = ctk.CTkOptionMenu(
            bar, values=list(SORT_OPTIONS.keys()),
            font=f(12),
            fg_color=GHOST_BTN, button_color=GHOST_BTN, button_hover_color=GHOST_BTN_HOVER,
            text_color=GHOST_BTN_TEXT, dropdown_fg_color=BG_PANEL,
            dropdown_text_color=TEXT_PRIMARY, dropdown_hover_color=GHOST_BTN_HOVER,
            corner_radius=6, height=26, width=180,
            command=self._on_sort_change,
        )
        self.sort_menu.set(self.sort_key)
        self.sort_menu.pack(side="right", padx=(0, 14))

    def _build_meeting_list(self):
        wrap = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        wrap.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        self.list_frame = ctk.CTkScrollableFrame(
            wrap, fg_color=BG_PANEL, corner_radius=10,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=TEXT_TERTIARY,
        )
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_pagination(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=34)
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

        for w in (text_col, title_lbl, meta_lbl, right, dot, pill):
            w.bind("<Button-1>",
                   lambda _e, did=doc["id"], var=check_var: self._toggle_via_click(did, var))

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

    def _show_reconnect_dialog(self):
        """Modal explaining auth + offering to open Granola + watch for refresh."""
        if hasattr(self, "_reconn_win") and self._reconn_win.winfo_exists():
            self._reconn_win.lift()
            return

        win = ctk.CTkToplevel(self)
        self._reconn_win = win
        win.title("Granola Connection")
        win.geometry("460x260")
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
                   "When it expires, click Reconnect — that opens the Granola app and "
                   "auto-detects the new session.")
        else:
            msg = ("Your Granola session has expired (or never started).\n\n"
                   "Click Reconnect — this opens the Granola desktop app. As soon as you sign in there, "
                   "this app will auto-detect the new session.")

        ctk.CTkLabel(
            win, text=msg, font=f(13), text_color=TEXT_SECONDARY,
            wraplength=420, justify="left",
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
        """Open Granola.app and poll supabase.json for a fresh token."""
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
            text="Granola opened — sign in there. Watching for new session…",
            text_color=TEXT_SECONDARY,
        )
        self._poll_supabase()

    def _poll_supabase(self):
        if not self._auth_watch_active:
            return
        elapsed = int(time.time() - self._auth_watch_started)
        if elapsed > 120:
            self._reconn_status.configure(
                text="Timed out after 2 minutes. Try clicking Reconnect again.",
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
            except AuthError:
                # File changed but still no valid token — keep waiting
                self._initial_mtime = mtime

        self._reconn_status.configure(
            text=f"Watching for sign-in… ({elapsed}s)",
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
        try:
            token, source, remaining = load_access_token()
            self.token = token
            self.token_remaining = remaining
            self._log(f"Authenticated via {source} (~{remaining // 60}min remaining)")
            self.after(0, lambda r=remaining: self._set_chip(
                f"● Connected · {r // 60}m", CHIP_OK_FG, CHIP_OK_BG,
            ))
        except AuthError as e:
            self._log(f"AUTH ERROR: {e}")
            self.token = None
            self.after(0, lambda: self._set_chip("● Session expired", CHIP_ERR_FG, CHIP_ERR_BG))
            self.after(0, lambda: self._set_status("Authentication required — click the badge", DANGER))
            self.after(0, self._show_reconnect_dialog)
            self.after(0, lambda: self._set_busy(False))
            return

        try:
            docs = load_documents()
        except Exception as e:
            self._log(f"ERROR loading cache: {e}")
            self.after(0, lambda: self._set_busy(False))
            return

        out_dir = Path(self.out_root.get()) / "transcripts"
        existing = scan_existing(out_dir)

        self.docs = docs
        self.existing = existing
        self._apply_sort()
        new_count = sum(1 for d in docs if meeting_filename(d) not in existing)

        # auto-tick new ones (across all pages)
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
        fetched = no_tx = errors = 0
        entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}
        total = len(docs)

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
                elif isinstance(resp, dict) and "transcript" in resp:
                    segments = resp["transcript"]
                    if segments:
                        fetched += 1
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self._log("AUTH expired mid-run — opening reconnect dialog.")
                    self.token = None
                    self.after(0, lambda: self._set_chip(
                        "● Session expired", CHIP_ERR_FG, CHIP_ERR_BG))
                    self.after(0, lambda: self._set_status(
                        "Auth expired mid-export. Reconnect and re-run.", DANGER))
                    self.after(0, self._show_reconnect_dialog)
                    self.after(0, lambda: self._set_busy(False))
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
        self.after(0, lambda: self._set_status(
            f"Done · {fetched} transcripts · {errors} errors",
            ACCENT if not errors else TEXT_PRIMARY,
        ))
        self.after(0, lambda: self._set_busy(False))
        self.existing = scan_existing(out_dir)
        self.checked.clear()
        self.after(0, self._update_count)

    def _mark_exported(self, doc_id: str):
        w = self.row_widgets.get(doc_id)
        if not w:
            return
        w["row"].configure(fg_color=BG_CARD)
        w["bg"] = BG_CARD
        w["is_new"] = False
        w["pill"].configure(text="EXPORTED", text_color=PILL_EXPORTED_FG, fg_color=PILL_EXPORTED_BG)
        w["var"].set(False)


if __name__ == "__main__":
    App().mainloop()
