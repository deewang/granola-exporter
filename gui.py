#!/usr/bin/env python3
"""Granola Export — desktop GUI.

Workflow:
  1. Click "Refresh from Granola" → loads all meetings, marks NEW ones.
  2. Tick the rows you want (or use Select New / Select All).
  3. Click "Export Selected" → writes Markdown + updates INDEX.md.
"""

import queue
import threading
import time
import tkinter as tk
import urllib.error
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from granola_core import (
    AuthError,
    DEFAULT_OUT_ROOT,
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Granola Export")
        self.geometry("1100x680")
        self.minsize(800, 500)

        self.out_root = tk.StringVar(value=str(DEFAULT_OUT_ROOT))
        self.docs: list[dict] = []
        self.checked: set[str] = set()  # doc ids
        self.existing: set[str] = set()  # filenames
        self.token: str | None = None
        self.log_q: queue.Queue = queue.Queue()
        self.worker_busy = False

        self._build_ui()
        self.after(100, self._drain_log)
        # Auto-refresh on launch
        self.after(200, self.refresh)

    # ---------- UI ----------

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(top, textvariable=self.status_var, font=("SF Pro", 12, "bold")).pack(side="left")

        ttk.Label(top, text="  Output:").pack(side="left", padx=(20, 4))
        ttk.Entry(top, textvariable=self.out_root, width=50).pack(side="left")
        ttk.Button(top, text="…", width=3, command=self._pick_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Open Folder", command=self._open_folder).pack(side="left", padx=4)

        # Action bar
        actions = ttk.Frame(self, padding=(10, 0))
        actions.pack(fill="x")

        self.btn_refresh = ttk.Button(actions, text="🔄 Refresh from Granola", command=self.refresh)
        self.btn_refresh.pack(side="left")

        ttk.Button(actions, text="Select New", command=self._select_new).pack(side="left", padx=(20, 4))
        ttk.Button(actions, text="Select All", command=self._select_all).pack(side="left", padx=4)
        ttk.Button(actions, text="Clear", command=self._select_none).pack(side="left", padx=4)

        self.btn_export = ttk.Button(actions, text="⬇  Export Selected", command=self.export, style="Accent.TButton")
        self.btn_export.pack(side="right")
        self.selected_count = tk.StringVar(value="0 selected")
        ttk.Label(actions, textvariable=self.selected_count).pack(side="right", padx=10)

        # Treeview
        tree_frame = ttk.Frame(self, padding=10)
        tree_frame.pack(fill="both", expand=True)

        cols = ("check", "status", "date", "title", "participants")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("check", text="✓")
        self.tree.heading("status", text="Status")
        self.tree.heading("date", text="Date")
        self.tree.heading("title", text="Title")
        self.tree.heading("participants", text="Participants")
        self.tree.column("check", width=36, anchor="center", stretch=False)
        self.tree.column("status", width=90, anchor="w", stretch=False)
        self.tree.column("date", width=140, anchor="w", stretch=False)
        self.tree.column("title", width=400, anchor="w")
        self.tree.column("participants", width=300, anchor="w")

        self.tree.tag_configure("new", background="#E8F4FD")
        self.tree.tag_configure("exported", foreground="#666")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)

        # Progress + log
        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")

        self.log_text = tk.Text(bottom, height=6, wrap="word", state="disabled",
                                 background="#F5F5F7", relief="flat", font=("Menlo", 11))
        self.log_text.pack(fill="x", pady=(8, 0))

        # Style accent button
        style = ttk.Style()
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
        style.configure("Accent.TButton", font=("SF Pro", 12, "bold"))

    # ---------- helpers ----------

    def _log(self, msg: str):
        self.log_q.put(msg)

    def _drain_log(self):
        while not self.log_q.empty():
            msg = self.log_q.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(100, self._drain_log)

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.out_root.get(), title="Choose output folder")
        if d:
            self.out_root.set(d)

    def _open_folder(self):
        import subprocess
        subprocess.run(["open", self.out_root.get()])

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _set_busy(self, busy: bool):
        self.worker_busy = busy
        state = "disabled" if busy else "normal"
        self.btn_refresh.configure(state=state)
        self.btn_export.configure(state=state)

    # ---------- selection ----------

    def _toggle(self, doc_id: str):
        if doc_id in self.checked:
            self.checked.discard(doc_id)
        else:
            self.checked.add(doc_id)
        self._refresh_check_column(doc_id)
        self._update_count()

    def _refresh_check_column(self, doc_id: str):
        if not self.tree.exists(doc_id):
            return
        vals = list(self.tree.item(doc_id, "values"))
        vals[0] = "☑" if doc_id in self.checked else "☐"
        self.tree.item(doc_id, values=vals)

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if region == "cell" and col == "#1" and row:
            self._toggle(row)
            return "break"

    def _on_space(self, event):
        for row in self.tree.selection():
            self._toggle(row)
        return "break"

    def _select_new(self):
        self.checked = {d["id"] for d in self.docs if meeting_filename(d) not in self.existing}
        self._redraw_check_column()
        self._update_count()

    def _select_all(self):
        self.checked = {d["id"] for d in self.docs}
        self._redraw_check_column()
        self._update_count()

    def _select_none(self):
        self.checked = set()
        self._redraw_check_column()
        self._update_count()

    def _redraw_check_column(self):
        for d in self.docs:
            self._refresh_check_column(d["id"])

    def _update_count(self):
        self.selected_count.set(f"{len(self.checked)} selected")

    # ---------- refresh (load docs) ----------

    def refresh(self):
        if self.worker_busy:
            return
        self._set_busy(True)
        self._set_status("Refreshing…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            token, source, remaining = load_access_token()
            self.token = token
            self._log(f"Auth OK via {source} (~{remaining // 60}min remaining)")
        except AuthError as e:
            self._log(f"AUTH ERROR: {e}")
            self.after(0, lambda: messagebox.showerror(
                "Authentication required",
                f"{e}\n\nOpen the Granola app, sign in, then click Refresh again.",
            ))
            self.after(0, lambda: self._set_busy(False))
            self.after(0, lambda: self._set_status("Auth required"))
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
        new_count = sum(1 for d in docs if meeting_filename(d) not in existing)

        self._log(f"Loaded {len(docs)} meetings — {new_count} new, {len(docs) - new_count} already exported")

        self.after(0, lambda: self._populate_tree())
        self.after(0, lambda: self._select_new())  # auto-tick new ones
        self.after(0, lambda: self._set_status(f"{len(docs)} meetings ({new_count} new)"))
        self.after(0, lambda: self._set_busy(False))

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        for d in self.docs:
            fname = meeting_filename(d)
            is_new = fname not in self.existing
            dt = parse_iso(d.get("created_at"))
            date_str = dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"
            title = d.get("title") or "Untitled"
            people = d.get("people") or {}
            names: list[str] = []
            if isinstance(people, dict):
                for grp in people.values():
                    if isinstance(grp, list):
                        for p in grp:
                            if isinstance(p, dict):
                                n = p.get("name") or p.get("email")
                                if n:
                                    names.append(n)
            participants = ", ".join(names[:3])
            if len(names) > 3:
                participants += f" +{len(names) - 3}"

            status = "🆕 NEW" if is_new else "✓ Exported"
            tags = ("new",) if is_new else ("exported",)
            self.tree.insert(
                "", "end",
                iid=d["id"],
                values=("☐", status, date_str, title, participants or "—"),
                tags=tags,
            )

    # ---------- export ----------

    def export(self):
        if self.worker_busy:
            return
        if not self.checked:
            messagebox.showinfo("Nothing selected", "Tick some meetings first (click the ✓ column).")
            return
        if not self.token:
            messagebox.showerror("Not authenticated", "Click Refresh first.")
            return

        out_root = Path(self.out_root.get())
        out_dir = out_root / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)

        to_export = [d for d in self.docs if d["id"] in self.checked]
        self._set_busy(True)
        self.progress.configure(maximum=len(to_export), value=0)
        self._set_status(f"Exporting 0 / {len(to_export)}…")
        threading.Thread(target=self._export_worker, args=(to_export, out_root, out_dir), daemon=True).start()

    def _export_worker(self, docs: list[dict], out_root: Path, out_dir: Path):
        fetched = no_tx = errors = 0
        # start with existing entries so the index includes everything
        entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}

        for i, doc in enumerate(docs, 1):
            title = doc.get("title") or "Untitled"
            self._log(f"[{i}/{len(docs)}] {title}")

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
                    self._log("AUTH expired mid-run. Open Granola app, then Refresh + retry.")
                    self.after(0, lambda: self._set_busy(False))
                    self.after(0, lambda: self._set_status("Auth expired"))
                    return
                errors += 1
                self._log(f"  HTTP {e.code} for {title}")
            except Exception as e:
                errors += 1
                self._log(f"  ERROR: {e}")

            try:
                _, meta = write_meeting_file(out_dir, doc, segments)
                entries_by_name[meta.filename] = meta
                # Mark row as exported
                self.after(0, self._mark_exported, doc["id"])
            except Exception as e:
                self._log(f"  WRITE ERROR: {e}")

            self.after(0, lambda v=i: self.progress.configure(value=v))
            self.after(0, lambda i=i, n=len(docs): self._set_status(f"Exporting {i} / {n}…"))
            time.sleep(0.2)

        try:
            write_index(out_root, list(entries_by_name.values()))
            self._log(f"Wrote INDEX.md ({len(entries_by_name)} entries)")
        except Exception as e:
            self._log(f"INDEX ERROR: {e}")

        self._log(f"\n✅ Done. fetched={fetched}  no_transcript={no_tx}  errors={errors}")
        self.after(0, lambda: self._set_status(f"Done — {fetched} transcripts, {errors} errors"))
        self.after(0, lambda: self._set_busy(False))
        # refresh existing set so re-runs know what's exported
        self.existing = scan_existing(out_dir)
        self.checked = set()
        self.after(0, self._update_count)

    def _mark_exported(self, doc_id: str):
        if not self.tree.exists(doc_id):
            return
        vals = list(self.tree.item(doc_id, "values"))
        vals[0] = "☐"
        vals[1] = "✓ Exported"
        self.tree.item(doc_id, values=vals, tags=("exported",))


if __name__ == "__main__":
    App().mainloop()
