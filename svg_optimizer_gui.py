#!/usr/bin/env python3
"""
svg_optimizer_gui.py

Cross-platform GUI to:
- add SVG files (file picker)
- add a folder (recursively finds *.svg)
- optional drag & drop (if tkinterdnd2 is installed)
- choose output folder
- run the same optimization (H-only or H+V) and export results

Dependencies:
- Python 3.9+
- tkinter (usually bundled with Python on Windows/macOS; Linux may need distro package)
- Optional: tkinterdnd2 for drag & drop support:
    pip install tkinterdnd2

Run:
  python svg_optimizer_gui.py
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import xml.etree.ElementTree as ET

# -----------------------------
# Optimizer core (HV merge + namespace fix)
# -----------------------------

SVG_NS = "http://www.w3.org/2000/svg"
NS = {"svg": SVG_NS}


def parse_style(style: str | None) -> dict[str, str]:
    d: dict[str, str] = {}
    if not style:
        return d
    for part in style.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def norm_opacity(op_str: str | None) -> float:
    if not op_str:
        return 1.0
    try:
        op = float(op_str)
    except ValueError:
        return 1.0
    if op > 1.0:
        op = op / 255.0
    if op < 0.0:
        op = 0.0
    if op > 1.0:
        op = 1.0
    return op


def fmt_opacity(op: float) -> str:
    s = f"{op:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def optimize_svg_rects(svg_in: Path, svg_out: Path, vertical_merge: bool = True) -> tuple[int, int]:
    """
    Returns: (rect_count_out, bytes_out)
    """
    tree = ET.parse(str(svg_in))
    root = tree.getroot()

    rects = root.findall(".//svg:rect", NS)
    if not rects:
        raise ValueError("No <rect> elements found (this tool expects pixel-rect SVGs).")

    # y -> (fill,opacity) -> xs
    rows: dict[int, dict[tuple[str, float], list[int]]] = defaultdict(lambda: defaultdict(list))

    for r in rects:
        x = int(float(r.get("x", "0")))
        y = int(float(r.get("y", "0")))

        st = parse_style(r.get("style"))
        fill = st.get("fill", r.get("fill", "#000000"))
        op = round(norm_opacity(st.get("opacity", r.get("opacity"))), 6)

        rows[y][(fill, op)].append(x)

    # Horizontal runs
    merged_h: list[tuple[int, int, int, int, tuple[str, float]]] = []
    for y, style_map in rows.items():
        for stylekey, xs in style_map.items():
            xs = sorted(xs)
            start = prev = xs[0]
            for x in xs[1:]:
                if x == prev + 1:
                    prev = x
                else:
                    merged_h.append((start, y, prev - start + 1, 1, stylekey))
                    start = prev = x
            merged_h.append((start, y, prev - start + 1, 1, stylekey))

    rect_list: list[tuple[int, int, int, int, tuple[str, float]]]

    # Vertical merge stacks
    if vertical_merge:
        cols: dict[tuple[int, int, tuple[str, float]], list[int]] = defaultdict(list)
        for x, y, w, h, stylekey in merged_h:
            cols[(x, w, stylekey)].append(y)

        rect_list = []
        for (x, w, stylekey), ys in cols.items():
            ys = sorted(ys)
            start = prev = ys[0]
            for y in ys[1:]:
                if y == prev + 1:
                    prev = y
                else:
                    rect_list.append((x, start, w, prev - start + 1, stylekey))
                    start = prev = y
            rect_list.append((x, start, w, prev - start + 1, stylekey))
    else:
        rect_list = merged_h

    # Namespace fix: register default namespace; DO NOT manually set xmlns attribute.
    ET.register_namespace("", SVG_NS)

    out_attrs: dict[str, str] = {}
    for k in ("width", "height", "viewBox", "preserveAspectRatio"):
        v = root.get(k)
        if v:
            out_attrs[k] = v
    out_attrs["shape-rendering"] = "crispEdges"

    new_root = ET.Element(f"{{{SVG_NS}}}svg", out_attrs)
    g = ET.SubElement(new_root, f"{{{SVG_NS}}}g")

    rect_list_sorted = sorted(rect_list, key=lambda t: (t[1], t[0], t[2], t[3]))
    for x, y, w, h, (fill, op) in rect_list_sorted:
        r_attrs = {
            "x": str(x),
            "y": str(y),
            "width": str(w),
            "height": str(h),
            "fill": fill,
        }
        if abs(op - 1.0) > 1e-6:
            r_attrs["opacity"] = fmt_opacity(op)
        ET.SubElement(g, f"{{{SVG_NS}}}rect", r_attrs)

    data = ET.tostring(new_root, encoding="utf-8", xml_declaration=True)
    svg_out.parent.mkdir(parents=True, exist_ok=True)
    svg_out.write_bytes(data)
    return len(rect_list_sorted), len(data)


# -----------------------------
# GUI
# -----------------------------

@dataclass
class JobResult:
    input_path: Path
    output_path: Path
    ok: bool
    message: str


def find_svgs_in_folder(folder: Path, recursive: bool = True) -> list[Path]:
    if recursive:
        return sorted([p for p in folder.rglob("*.svg") if p.is_file()])
    return sorted([p for p in folder.glob("*.svg") if p.is_file()])


def try_enable_dnd(root: tk.Tk):
    """
    Optional drag & drop support. If tkinterdnd2 isn't installed, GUI still works via pickers.
    """
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    except Exception:
        return None, None

    # Recreate root as TkinterDnD.Tk for native drops
    new_root = TkinterDnD.Tk()
    root.destroy()
    return new_root, DND_FILES


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SVG Pixel-Rect Optimizer")
        self.root.geometry("900x520")

        self.files: list[Path] = []
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "optimized_svgs"))
        self.recursive = tk.BooleanVar(value=True)
        self.vertical_merge = tk.BooleanVar(value=True)
        self.preserve_tree = tk.BooleanVar(value=False)  # preserve relative folder structure under output

        self.status = tk.StringVar(value="Ready.")
        self.progress = tk.DoubleVar(value=0.0)

        self._build_ui()

    def _build_ui(self):
        # Top controls
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Add SVG Files…", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add Folder…", command=self.add_folder).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Clear List", command=self.clear_list).pack(side="left", padx=(8, 0))

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Checkbutton(top, text="Vertical merge (HV)", variable=self.vertical_merge).pack(side="left")
        ttk.Checkbutton(top, text="Recursive folder scan", variable=self.recursive).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(top, text="Preserve folder structure", variable=self.preserve_tree).pack(side="left", padx=(10, 0))

        # Middle: listbox + drop zone
        mid = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Files to process:").pack(anchor="w")

        self.listbox = tk.Listbox(left, selectmode=tk.EXTENDED)
        self.listbox.pack(fill="both", expand=True, pady=(6, 0))

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(side="left")

        right = ttk.Frame(mid, width=260)
        right.pack(side="left", fill="y", padx=(12, 0))
        right.pack_propagate(False)

        ttk.Label(right, text="Drag & drop zone:").pack(anchor="w")
        self.drop = tk.Text(right, height=8, wrap="word")
        self.drop.insert("1.0", "Drop SVG files here (optional).\n\nIf drag & drop doesn't work:\nuse “Add SVG Files…” or “Add Folder…”.")
        self.drop.configure(state="disabled")
        self.drop.pack(fill="x", pady=(6, 0))

        # Output folder
        out = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        out.pack(fill="x")

        ttk.Label(out, text="Output folder:").pack(side="left")
        self.out_entry = ttk.Entry(out, textvariable=self.output_dir)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(out, text="Choose…", command=self.choose_output_dir).pack(side="left", padx=(8, 0))

        # Bottom: run + progress + status
        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")

        self.run_btn = ttk.Button(bottom, text="Optimize", command=self.run)
        self.run_btn.pack(side="left")

        self.pb = ttk.Progressbar(bottom, variable=self.progress, maximum=100.0)
        self.pb.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ttk.Label(self.root, textvariable=self.status, padding=(10, 0, 10, 10)).pack(fill="x")

    # ---------- file list management ----------

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for p in self.files:
            self.listbox.insert(tk.END, str(p))
        self.status.set(f"{len(self.files)} file(s) queued.")

    def _add_paths(self, paths: list[Path]):
        added = 0
        existing = set(self.files)
        for p in paths:
            p = p.resolve()
            if p.suffix.lower() != ".svg":
                continue
            if p.is_file() and p not in existing:
                self.files.append(p)
                existing.add(p)
                added += 1
        if added:
            self.files.sort()
        self._refresh_listbox()

    def add_files(self):
        filenames = filedialog.askopenfilenames(
            title="Select SVG files",
            filetypes=[("SVG files", "*.svg"), ("All files", "*.*")]
        )
        if not filenames:
            return
        self._add_paths([Path(f) for f in filenames])

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select a folder containing SVGs")
        if not folder:
            return
        svgs = find_svgs_in_folder(Path(folder), recursive=self.recursive.get())
        if not svgs:
            messagebox.showinfo("No SVGs found", "No .svg files were found in the selected folder.")
            return
        self._add_paths(svgs)

    def clear_list(self):
        self.files = []
        self._refresh_listbox()

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        sel_set = set(sel)
        self.files = [p for i, p in enumerate(self.files) if i not in sel_set]
        self._refresh_listbox()

    def choose_output_dir(self):
        folder = filedialog.askdirectory(title="Choose output folder")
        if not folder:
            return
        self.output_dir.set(str(Path(folder)))
        self.status.set("Output folder set.")

    # ---------- processing ----------

    def run(self):
        if not self.files:
            messagebox.showwarning("Nothing to do", "Add at least one SVG file.")
            return

        out_dir = Path(self.output_dir.get()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Output folder error", f"Cannot create output folder:\n{e}")
            return

        self.run_btn.configure(state="disabled")
        self.progress.set(0.0)
        self.status.set("Running...")

        # Run in background thread to keep UI responsive
        t = threading.Thread(target=self._run_worker, args=(out_dir,), daemon=True)
        t.start()

    def _run_worker(self, out_dir: Path):
        vertical = self.vertical_merge.get()
        preserve_tree = self.preserve_tree.get()

        # Establish a common root for relative preservation (best-effort)
        common_root = None
        if preserve_tree:
            try:
                common_root = Path(os.path.commonpath([str(p.parent) for p in self.files]))
            except Exception:
                common_root = None

        results: list[JobResult] = []
        total = len(self.files)

        for idx, inp in enumerate(self.files, start=1):
            try:
                # Output filename suffix
                suffix = "_optimized_hv" if vertical else "_optimized_h"
                out_name = inp.stem + suffix + inp.suffix

                if preserve_tree and common_root is not None:
                    rel_parent = inp.parent.relative_to(common_root)
                    out_path = out_dir / rel_parent / out_name
                else:
                    out_path = out_dir / out_name

                rect_count, bytes_out = optimize_svg_rects(inp, out_path, vertical_merge=vertical)
                results.append(JobResult(inp, out_path, True, f"OK | rects={rect_count:,} | bytes={bytes_out:,}"))
            except Exception as e:
                results.append(JobResult(inp, out_dir / (inp.stem + "_FAILED.svg"), False, str(e)))

            pct = (idx / total) * 100.0
            self.root.after(0, self.progress.set, pct)
            self.root.after(0, self.status.set, f"Processing {idx}/{total}: {inp.name}")

        self.root.after(0, self._finish, results, out_dir)

    def _finish(self, results: list[JobResult], out_dir: Path):
        ok = sum(1 for r in results if r.ok)
        fail = len(results) - ok

        # Show a short summary + write a log file
        log_path = out_dir / "svg_optimizer_log.txt"
        lines = []
        for r in results:
            lines.append(f"{'OK  ' if r.ok else 'FAIL'} | {r.input_path} -> {r.output_path} | {r.message}")
        try:
            log_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

        self.run_btn.configure(state="normal")
        self.progress.set(100.0)

        self.status.set(f"Done. OK: {ok}, Failed: {fail}. Log: {log_path}")
        if fail:
            messagebox.showwarning(
                "Completed with errors",
                f"OK: {ok}\nFailed: {fail}\n\nSee log:\n{log_path}"
            )
        else:
            messagebox.showinfo(
                "Completed",
                f"Processed {ok} file(s).\n\nOutput:\n{out_dir}\n\nLog:\n{log_path}"
            )


def main():
    root = tk.Tk()

    # Optional DnD enablement: if tkinterdnd2 present, replace root
    new_root, dnd_files = try_enable_dnd(root)
    if new_root is not None:
        root = new_root

    app = App(root)

    # If DnD available, register drop target on the drop text widget
    if new_root is not None and dnd_files is not None:
        from tkinterdnd2 import DND_FILES  # type: ignore

        def on_drop(event):
            # event.data contains a Tcl-style list of paths; handle braces/spaces
            raw = event.data
            # Basic parsing: split respecting braces
            paths = []
            cur = ""
            in_brace = False
            for ch in raw:
                if ch == "{":
                    in_brace = True
                    cur = ""
                elif ch == "}":
                    in_brace = False
                    if cur:
                        paths.append(cur)
                        cur = ""
                elif ch.isspace() and not in_brace:
                    if cur:
                        paths.append(cur)
                        cur = ""
                else:
                    cur += ch
            if cur:
                paths.append(cur)

            app._add_paths([Path(p) for p in paths])
            return "break"

        app.drop.configure(state="normal")
        app.drop.delete("1.0", tk.END)
        app.drop.insert("1.0", "Drop SVG files here.")
        app.drop.configure(state="disabled")

        app.drop.drop_target_register(DND_FILES)
        app.drop.dnd_bind("<<Drop>>", on_drop)

    root.mainloop()


if __name__ == "__main__":
    main()
