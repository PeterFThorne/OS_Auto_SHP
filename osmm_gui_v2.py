#!/usr/bin/env python3
"""
osmm_gui_v2.py - OS MasterMap Site Builder (v2: shapefile-driven).

A separate desktop front end for the same OS MasterMap -> MapInfo pipeline.
Instead of typing a centre grid reference and box size, you give it one or
more shapefiles. It finds their most westerly, southerly, easterly and
northerly points, adds a buffer (300 m by default) on every side, and
extracts and styles the OS MasterMap data for that box.

Put this file in the same folder as:
    osmm_site_extract.py     (stage 1: select the site)
    osmm_style_mapinfo.py    (stage 2: styled MM_* tables)

Then double-click it, or run:  python osmm_gui_v2.py

It does not change the pipeline scripts. It works out the box, builds the
same command lines you would type, and runs them in the background.

Settings are saved to osmm_gui_v2_settings.json next to this file, so it
never touches the v1 app's settings.

Only standard-library Python is needed (Tkinter ships with Python on
Windows). Shapefiles are read directly, so GDAL is not needed for this step.
"""

import json
import math
import os
import queue
import re
import runpy
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "OS MasterMap Site Builder v2 (from shapefile)"
SETTINGS_NAME = "osmm_gui_v2_settings.json"

STAGE1 = "osmm_site_extract.py"
STAGE2 = "osmm_style_mapinfo.py"
SHP2TAB = "osmm_shp_to_tab.py"   # input shapefile(s) -> MapInfo TAB

TILE_M = 5000              # OS MasterMap tiles are 5 km squares (SW corner)
LARGE_SIDE_M = 20000       # ask before extracting a box bigger than this
STAGE1_TMP = "_osmm_stage1_tmp"   # stage 1 always writes to <output>/<name>;
                                  # we use this name, then move the files up

DEFAULTS = {
    "buffer_m": "300",
    "per_file": False,
    "output_root": "",
    "same_as_input": True,
    "subfolder": False,
    "site_name": "",
    "run_stage2": True,
    "shp_to_tab": True,
    "make_map": False,
    "open_when_done": True,
    "data_root": r"M:\2500MMdata",
    "scripts_dir": "",
    "reindex": False,
    "last_dir": "",
}

FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def script_command(script: Path, args):
    """Command line that runs `script` with `args`. As a packaged .exe we
    re-launch ourselves in script-runner mode, so Python isn't needed."""
    if FROZEN:
        return [sys.executable, "--run-script", str(script), *args]
    return [sys.executable, "-u", str(script), *args]


# --------------------------------------------------------------------------
# Shapefile reading (standard library only)
# --------------------------------------------------------------------------

class ShapefileError(Exception):
    pass


def read_shapefile(path: Path):
    """Return {"type": int, "bbox": (minx, miny, maxx, maxy) or None,
    "parts": [[(x, y), ...], ...], "points": [(x, y), ...], "count": n}.

    Handles point, multipoint, polyline, polygon and multipatch, with or
    without Z/M values (only X/Y are used)."""
    data = Path(path).read_bytes()
    if len(data) < 100:
        raise ShapefileError("file is too small to be a shapefile")
    if struct.unpack_from(">i", data, 0)[0] != 9994:
        raise ShapefileError("not a valid .shp file (bad header)")
    shp_type = struct.unpack_from("<i", data, 32)[0]

    parts, points = [], []
    count = 0
    pos = 100
    n = len(data)
    while pos + 8 <= n:
        _, content_words = struct.unpack_from(">ii", data, pos)
        start = pos + 8
        end = start + content_words * 2
        if end > n or content_words < 2:
            break
        rtype = struct.unpack_from("<i", data, start)[0]
        p = start + 4
        if rtype == 0:
            pass
        elif rtype in (1, 11, 21):                       # point
            x, y = struct.unpack_from("<2d", data, p)
            points.append((x, y))
            count += 1
        elif rtype in (8, 18, 28):                       # multipoint
            npts = struct.unpack_from("<i", data, p + 32)[0]
            q = p + 36
            for i in range(npts):
                points.append(struct.unpack_from("<2d", data, q + 16 * i))
            count += 1
        elif rtype in (3, 13, 23, 5, 15, 25, 31):        # line/polygon/patch
            nparts, npts = struct.unpack_from("<2i", data, p + 32)
            q = p + 40
            starts = list(struct.unpack_from(f"<{nparts}i", data, q))
            q += 4 * nparts
            if rtype == 31:                              # part types
                q += 4 * nparts
            xy = [struct.unpack_from("<2d", data, q + 16 * i)
                  for i in range(npts)]
            starts.append(npts)
            for a, b in zip(starts, starts[1:]):
                if b > a:
                    parts.append(xy[a:b])
            count += 1
        pos = end

    xs = [x for part in parts for x, _ in part] + [x for x, _ in points]
    ys = [y for part in parts for _, y in part] + [y for _, y in points]
    xs = [v for v in xs if math.isfinite(v)]
    ys = [v for v in ys if math.isfinite(v)]
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None
    return {"type": shp_type, "bbox": bbox, "parts": parts,
            "points": points, "count": count}


def check_crs(shp: Path, bbox):
    """Return (ok, message). Blocks lat/long data; warns on unknown CRS."""
    prj = shp.with_suffix(".prj")
    if not prj.exists():
        prj = next((p for p in shp.parent.glob(shp.stem + ".*")
                    if p.suffix.lower() == ".prj"), None)
    text = ""
    if prj and prj.exists():
        text = prj.read_text(errors="ignore")
    upper = text.upper()

    looks_bng = any(k in upper for k in (
        "BRITISH_NATIONAL_GRID", "BRITISH NATIONAL GRID", "OSGB",
        "27700", "TRANSVERSE_MERCATOR\"],PARAMETER[\"FALSE_EASTING\",400000"))
    is_geographic = text.strip().startswith("GEOGCS") or \
        (text and "PROJCS" not in upper and "GEOGCS" in upper)

    if bbox:
        minx, miny, maxx, maxy = bbox
        in_degrees = -180 <= minx <= 180 and -90 <= miny <= 90 and \
            -180 <= maxx <= 180 and -90 <= maxy <= 90
        in_gb = -10000 <= minx and maxx <= 800000 and \
            -10000 <= miny and maxy <= 1300000
    else:
        in_degrees, in_gb = False, True

    if is_geographic or (in_degrees and not looks_bng):
        return False, ("is in latitude/longitude, not British National Grid. "
                       "Reproject it to EPSG:27700 (e.g. in MapInfo or "
                       "ArcGIS Pro) and try again.")
    if not in_gb:
        return False, ("has coordinates outside Great Britain's National "
                       "Grid range. Check it is in EPSG:27700.")
    if not text:
        return True, "has no .prj file; assuming British National Grid."
    if not looks_bng:
        return True, ("has a .prj that doesn't name British National Grid; "
                      "coordinates look like BNG so continuing.")
    return True, ""


# --------------------------------------------------------------------------
# Grid maths
# --------------------------------------------------------------------------

LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"   # no I


def square_letters(e, n):
    e100, n100 = int(e // 100000), int(n // 100000)
    if not (0 <= e100 <= 6 and 0 <= n100 <= 12):
        return None
    l1 = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    l2 = ((19 - n100) * 5) % 25 + e100 % 5
    return LETTERS[l1] + LETTERS[l2]


def tile_name(e, n):
    e0 = int(e // TILE_M) * TILE_M
    n0 = int(n // TILE_M) * TILE_M
    sq = square_letters(e0, n0)
    if sq is None:
        return None
    return f"{sq}{(e0 % 100000) // 1000:02d}{(n0 % 100000) // 1000:02d}"


def tiles_for_box(box):
    minx, miny, maxx, maxy = box
    out = []
    e = int(minx // TILE_M) * TILE_M
    while e < maxx:
        n = int(miny // TILE_M) * TILE_M
        while n < maxy:
            t = tile_name(e, n)
            if t:
                out.append((t, e, n))
            n += TILE_M
        e += TILE_M
    return out


def buffered_box(bbox, buffer_m):
    """Bbox + buffer on every side, rounded outwards to whole metres and
    made an even size so the centre is a whole metre too."""
    minx, miny, maxx, maxy = bbox
    x0 = math.floor(minx - buffer_m)
    y0 = math.floor(miny - buffer_m)
    x1 = math.ceil(maxx + buffer_m)
    y1 = math.ceil(maxy + buffer_m)
    if (x1 - x0) % 2:
        x1 += 1
    if (y1 - y0) % 2:
        y1 += 1
    return (x0, y0, x1, y1)


def union_bbox(boxes):
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def safe_name(s):
    """Same rule stage 1 uses for its folder name, so the two always match."""
    s = re.sub(r"[^\w\-]+", "_", s.strip())
    return s.strip("_") or "site"


def short_path(p, limit=34):
    """Last part of a long path, for the narrow extent panel."""
    s = str(p)
    return s if len(s) <= limit else "…" + s[-(limit - 1):]


def find_shapefiles(folder: Path):
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() == ".shp")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1020x780")
        self.minsize(900, 680)

        self.settings_path = app_dir() / SETTINGS_NAME
        self.settings = dict(DEFAULTS)
        self._load_settings()

        self.shapes = {}          # path str -> read_shapefile() result
        self.shape_errors = {}    # path str -> message
        self.proc = None
        self.stop_flag = threading.Event()
        self.msgq = queue.Queue()

        self.vars = {}
        for key in ("buffer_m", "output_root", "site_name", "data_root",
                    "scripts_dir"):
            self.vars[key] = tk.StringVar(value=self.settings.get(key, ""))
        for key in ("per_file", "same_as_input", "subfolder", "run_stage2",
                    "shp_to_tab",
                    "make_map",
                    "open_when_done", "reindex"):
            self.vars[key] = tk.BooleanVar(value=bool(self.settings.get(key)))
        if not self.vars["scripts_dir"].get():
            self.vars["scripts_dir"].set(str(app_dir()))

        self._build()
        # Inputs deliberately start empty each session, so a previous job's
        # shapefiles can't be extracted by mistake.
        self._refresh()

        self.vars["buffer_m"].trace_add("write", lambda *_: self._refresh())
        for key in ("per_file", "subfolder", "same_as_input", "site_name",
                    "output_root"):
            self.vars[key].trace_add("write", lambda *_: self._refresh())
        self.vars["same_as_input"].trace_add(
            "write", lambda *_: self._toggle_output())
        self._toggle_output()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- settings -------------------------------------------------------
    def _load_settings(self):
        try:
            self.settings.update(json.loads(
                self.settings_path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def _save_settings(self):
        s = {k: v.get() for k, v in self.vars.items()}
        s["last_dir"] = self.settings.get("last_dir", "")
        try:
            self.settings_path.write_text(json.dumps(s, indent=2),
                                          encoding="utf-8")
        except Exception:
            pass

    # ----- layout ---------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", **pad)
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)

        # --- Input -------------------------------------------------------
        inp = ttk.LabelFrame(top, text="1. Input shapefile(s)")
        inp.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        inp.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(inp, height=6, selectmode="extended")
        self.listbox.grid(row=0, column=0, rowspan=4, sticky="nsew",
                          padx=6, pady=6)
        ttk.Button(inp, text="Add file(s)…", command=self._pick_files)\
            .grid(row=0, column=1, sticky="ew", padx=6, pady=(6, 2))
        ttk.Button(inp, text="Add folder…", command=self._pick_folder)\
            .grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(inp, text="Remove", command=self._remove_selected)\
            .grid(row=2, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(inp, text="Clear", command=self._clear_inputs)\
            .grid(row=3, column=1, sticky="ew", padx=6, pady=(2, 6))

        row = ttk.Frame(inp)
        row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6,
                 pady=(0, 6))
        ttk.Label(row, text="Buffer around shapefile extent (m):")\
            .pack(side="left")
        ttk.Spinbox(row, from_=0, to=5000, increment=50, width=7,
                    textvariable=self.vars["buffer_m"]).pack(side="left",
                                                              padx=6)
        ttk.Checkbutton(row, text="One site per shapefile",
                        variable=self.vars["per_file"]).pack(side="left",
                                                             padx=12)

        # --- Output ------------------------------------------------------
        out = ttk.LabelFrame(top, text="2. Output")
        out.grid(row=0, column=1, sticky="nsew")
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Output folder:").grid(row=0, column=0,
                                                   sticky="w", padx=6,
                                                   pady=(6, 2))
        self.out_entry = ttk.Entry(out, textvariable=self.vars["output_root"])
        self.out_entry.grid(row=0, column=1, sticky="ew", pady=(6, 2))
        self.out_btn = ttk.Button(out, text="Browse…",
                                  command=lambda: self._pick_dir("output_root"))
        self.out_btn.grid(row=0, column=2, padx=6, pady=(6, 2))
        ttk.Checkbutton(out, text="Same as input folder",
                        variable=self.vars["same_as_input"])\
            .grid(row=1, column=1, columnspan=2, sticky="w")

        ttk.Checkbutton(out, text="Create a subfolder for the site",
                        variable=self.vars["subfolder"])\
            .grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(out, text="Site name:").grid(row=3, column=0, sticky="w",
                                               padx=6, pady=2)
        self.name_entry = ttk.Entry(out, textvariable=self.vars["site_name"])
        self.name_entry.grid(row=3, column=1, columnspan=2, sticky="ew",
                             padx=(0, 6), pady=2)

        ttk.Checkbutton(out, text="Style into MM_* tables (stage 2)",
                        variable=self.vars["run_stage2"])\
            .grid(row=5, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(out, text="Copy input shapefile(s) to the output folder "
                        "as MapInfo TAB",
                        variable=self.vars["shp_to_tab"])\
            .grid(row=6, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        cb = ttk.Checkbutton(out, text="PDF/JPG map of shapefile + "
                             "MasterMap (coming later)",
                             variable=self.vars["make_map"])
        cb.grid(row=7, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        cb.state(["disabled"])
        ttk.Checkbutton(out, text="Open output folder when done",
                        variable=self.vars["open_when_done"])\
            .grid(row=8, column=0, columnspan=3, sticky="w", padx=6,
                  pady=(2, 6))

        # --- Extent + preview ---------------------------------------------
        mid = ttk.LabelFrame(outer, text="3. Site extent")
        mid.pack(fill="both", expand=True, **pad)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(mid, background="white", height=240,
                                highlightthickness=1,
                                highlightbackground="#ccc")
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.extent_lbl = ttk.Label(mid, text="", justify="left",
                                    font=("Consolas", 9))
        self.extent_lbl.grid(row=0, column=1, sticky="nw", padx=6, pady=6)

        # --- Advanced -----------------------------------------------------
        adv = ttk.LabelFrame(outer, text="Settings")
        adv.pack(fill="x", **pad)
        adv.columnconfigure(1, weight=1)
        for r, (label, key) in enumerate((
                ("OS data (.gz tiles):", "data_root"),
                ("Pipeline scripts:", "scripts_dir"))):
            ttk.Label(adv, text=label).grid(row=r, column=0, sticky="w",
                                            padx=6, pady=2)
            ttk.Entry(adv, textvariable=self.vars[key])\
                .grid(row=r, column=1, sticky="ew", pady=2)
            ttk.Button(adv, text="Browse…",
                       command=lambda k=key: self._pick_dir(k))\
                .grid(row=r, column=2, padx=6, pady=2)
        ttk.Checkbutton(adv, text="Rebuild tile index (use after new tiles "
                        "are added to the data folder)",
                        variable=self.vars["reindex"])\
            .grid(row=2, column=1, sticky="w", pady=(0, 4))

        # --- Run + log ----------------------------------------------------
        bar = ttk.Frame(outer)
        bar.pack(fill="x", **pad)
        self.run_btn = ttk.Button(bar, text="Run", command=self._run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left", padx=12)

        logf = ttk.Frame(outer)
        logf.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = tk.Text(logf, height=10, wrap="word",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#1b7f2a")
        self.log.tag_configure("head", foreground="#0b4f9c",
                               font=("Consolas", 9, "bold"))
        self.log.tag_configure("warn", foreground="#a15c00")

    # ----- inputs ---------------------------------------------------------
    def _pick_files(self):
        files = filedialog.askopenfilenames(
            title="Choose shapefile(s)",
            initialdir=self.settings.get("last_dir") or None,
            filetypes=[("Shapefiles", "*.shp"), ("All files", "*.*")])
        for f in files:
            self._add_input(Path(f), refresh=False)
        if files:
            self.settings["last_dir"] = str(Path(files[0]).parent)
        self._refresh()

    def _pick_folder(self):
        d = filedialog.askdirectory(
            title="Folder containing shapefile(s)",
            initialdir=self.settings.get("last_dir") or None)
        if not d:
            return
        self.settings["last_dir"] = d
        found = find_shapefiles(Path(d))
        if not found:
            messagebox.showinfo(APP_TITLE, "No .shp files in that folder.")
            return
        for f in found:
            self._add_input(f, refresh=False)
        self._refresh()

    def _add_input(self, path: Path, refresh=True):
        key = str(path)
        if key in self.listbox.get(0, "end"):
            return
        self.listbox.insert("end", key)
        try:
            shp = read_shapefile(path)
            if shp["bbox"] is None:
                raise ShapefileError("contains no shapes")
            ok, msg = check_crs(path, shp["bbox"])
            if not ok:
                raise ShapefileError(msg)
            self.shapes[key] = shp
            if msg:
                self._log(f"Note: {path.name} {msg}", "warn")
        except Exception as e:
            self.shape_errors[key] = str(e)
            self._log(f"Problem with {path.name}: {e}", "err")
        if refresh:
            self._refresh()

    def _remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            key = self.listbox.get(i)
            self.listbox.delete(i)
            self.shapes.pop(key, None)
            self.shape_errors.pop(key, None)
        self._refresh()

    def _clear_inputs(self):
        self.listbox.delete(0, "end")
        self.shapes.clear()
        self.shape_errors.clear()
        self._refresh()

    def _pick_dir(self, key):
        d = filedialog.askdirectory(initialdir=self.vars[key].get() or None)
        if d:
            self.vars[key].set(str(Path(d)))

    def _toggle_output(self):
        state = "disabled" if self.vars["same_as_input"].get() else "normal"
        self.out_entry.configure(state=state)
        self.out_btn.configure(state=state)

    # ----- jobs (what will be run) ----------------------------------------
    def _buffer(self):
        try:
            b = float(self.vars["buffer_m"].get())
            return b if b >= 0 else None
        except ValueError:
            return None

    def _jobs(self):
        """List of dicts: name, files, box, out_root, site_dir.
        With "Create a subfolder" ticked, results go to out_root/<name>.
        Otherwise they go straight into out_root, except when one site per
        shapefile puts several sites in the same folder: their MM_* tables
        would overwrite each other, so each then gets a subfolder."""
        good = [k for k in self.listbox.get(0, "end") if k in self.shapes]
        if not good:
            raise ValueError("Add at least one valid shapefile.")
        buf = self._buffer()
        if buf is None:
            raise ValueError("Buffer must be a number of metres (0 or more).")

        def out_for(files):
            if self.vars["same_as_input"].get():
                return Path(files[0]).parent
            return Path(self.vars["output_root"].get().strip())

        jobs = []
        if self.vars["per_file"].get():
            for k in good:
                box = buffered_box(self.shapes[k]["bbox"], buf)
                jobs.append({"name": safe_name(Path(k).stem), "files": [k],
                             "box": box, "out_root": out_for([k])})
        else:
            box = buffered_box(
                union_bbox(self.shapes[k]["bbox"] for k in good), buf)
            name = self.vars["site_name"].get().strip() \
                if self.vars["subfolder"].get() else ""
            if not name:
                name = Path(good[0]).stem if len(good) == 1 else \
                    f"{Path(good[0]).stem}_plus{len(good) - 1}"
            jobs.append({"name": safe_name(name), "files": good,
                         "box": box, "out_root": out_for(good)})
        shared = {}
        for j in jobs:
            key = str(j["out_root"]).lower()
            shared[key] = shared.get(key, 0) + 1
        sub = self.vars["subfolder"].get()
        for j in jobs:
            j["site_dir"] = j["out_root"] / j["name"] \
                if sub or shared[str(j["out_root"]).lower()] > 1 \
                else j["out_root"]
        return jobs

    # ----- preview --------------------------------------------------------
    def _refresh(self):
        named = self.vars["subfolder"].get() and not self.vars["per_file"].get()
        self.name_entry.configure(state="normal" if named else "disabled")
        try:
            jobs = self._jobs()
        except ValueError as e:
            self.extent_lbl.configure(text=str(e))
            self._draw([])
            return
        lines = []
        for j in jobs[:6]:
            x0, y0, x1, y1 = j["box"]
            tiles = tiles_for_box(j["box"])
            lines += [
                f"{j['name']}",
                f"  W {x0}  E {x1}",
                f"  S {y0}  N {y1}",
                f"  {x1 - x0:,} x {y1 - y0:,} m",
                f"  centre {(x0 + x1) // 2},{(y0 + y1) // 2}",
                f"  tiles: {', '.join(t for t, _, _ in tiles)}",
                f"  output: {short_path(j['site_dir'])}", ""]
        if len(jobs) > 6:
            lines.append(f"… and {len(jobs) - 6} more sites")
        self.extent_lbl.configure(text="\n".join(lines))
        self._draw(jobs)

    def _draw(self, jobs=None):
        if jobs is None:
            try:
                jobs = self._jobs()
            except ValueError:
                jobs = []
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not jobs or w < 20 or h < 20:
            c.create_text(w / 2, h / 2, fill="#888",
                          text="Add shapefile(s) to see the site extent")
            return
        all_box = union_bbox(j["box"] for j in jobs)
        tiles = {t: (e, n) for j in jobs for t, e, n in tiles_for_box(j["box"])}
        # Zoom to the extraction box(es) with a margin; tiles show as a grid.
        bx0, by0, bx1, by1 = all_box
        mx = max(bx1 - bx0, by1 - by0) * 0.12 + 50
        vx0, vy0, vx1, vy1 = bx0 - mx, by0 - mx, bx1 + mx, by1 + mx
        m = 14
        s = min((w - 2 * m) / max(vx1 - vx0, 1), (h - 2 * m) / max(vy1 - vy0, 1))
        ox = (w - (vx1 - vx0) * s) / 2
        oy = (h - (vy1 - vy0) * s) / 2

        def tx(x, y):
            return ox + (x - vx0) * s, h - (oy + (y - vy0) * s)

        for t, (e, n) in tiles.items():
            a, b = tx(e, n + TILE_M)
            cc, d = tx(e + TILE_M, n)
            c.create_rectangle(a, b, cc, d, outline="#b5b5b5", fill="#f6f6f6")
            lx, ly = max(a, 2), max(b, 2)
            c.create_text(lx + 4, ly + 3, anchor="nw", text=t, fill="#999",
                          font=("Segoe UI", 8))
        for j in jobs:
            x0, y0, x1, y1 = j["box"]
            a, b = tx(x0, y1)
            cc, d = tx(x1, y0)
            c.create_rectangle(a, b, cc, d, outline="#d33", dash=(4, 3),
                               width=2)
            for k in j["files"]:
                shp = self.shapes[k]
                for part in shp["parts"]:
                    step = max(1, len(part) // 2000)
                    pts = [v for x, y in part[::step] for v in tx(x, y)]
                    if len(pts) >= 4:
                        c.create_line(*pts, fill="#1f5fbf", width=1.5)
                for x, y in shp["points"][:5000]:
                    px, py = tx(x, y)
                    c.create_oval(px - 2, py - 2, px + 2, py + 2,
                                  fill="#1f5fbf", outline="")
        c.create_text(w - 6, h - 4, anchor="se", fill="#666",
                      font=("Segoe UI", 8),
                      text="blue = shapefile   red dashed = extraction box   "
                           "grey = 5 km OS tiles")

    # ----- running --------------------------------------------------------
    def _log(self, text, tag=None):
        self.msgq.put(("log", text, tag))

    def _drain(self):
        try:
            while True:
                kind, a, b = self.msgq.get_nowait()
                if kind == "log":
                    self.log.insert("end", a + "\n", b or ())
                    self.log.see("end")
                elif kind == "status":
                    self.status.configure(text=a)
                elif kind == "done":
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _validate(self):
        jobs = self._jobs()
        bad = [Path(k).name for k in self.listbox.get(0, "end")
               if k in self.shape_errors]
        if bad:
            raise ValueError("Fix or remove these shapefiles first: "
                             + ", ".join(bad))
        if not self.vars["same_as_input"].get() and \
                not self.vars["output_root"].get().strip():
            raise ValueError("Choose an output folder, or tick "
                             "'Same as input folder'.")
        sdir = Path(self.vars["scripts_dir"].get())
        need = [STAGE1]
        if self.vars["run_stage2"].get() or self.vars["shp_to_tab"].get():
            need.append(STAGE2)          # the TAB step reuses stage 2's GDAL code
        if self.vars["shp_to_tab"].get():
            need.append(SHP2TAB)
        for s in need:
            if not (sdir / s).exists():
                raise ValueError(f"Can't find {s} in {sdir}. Set 'Pipeline "
                                 "scripts' in Settings.")
        data = self.vars["data_root"].get().strip()
        if not data or not Path(data).exists():
            raise ValueError(f"OS data folder not found: {data or '(blank)'}")
        return jobs

    def _run(self):
        try:
            jobs = self._validate()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        big = [j["name"] for j in jobs
               if max(j["box"][2] - j["box"][0],
                      j["box"][3] - j["box"][1]) > LARGE_SIDE_M]
        if big and not messagebox.askyesno(
                APP_TITLE, "These sites are over "
                f"{LARGE_SIDE_M // 1000} km across and may take a while:\n"
                + "\n".join(big) + "\n\nRun anyway?"):
            return
        self._save_settings()
        self.stop_flag.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._worker, args=(jobs,), daemon=True).start()

    def _stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self._log("Stopping…", "warn")

    def _exec(self, cmd):
        self._log("> " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env,
            cwd=self.vars["scripts_dir"].get(), **kw)
        lines = []
        for line in self.proc.stdout:
            line = line.rstrip()
            lines.append(line)
            low = line.lower()
            tag = "err" if ("error" in low or "traceback" in low) else \
                "warn" if ("warning" in low or "missing" in low) else None
            self._log("  " + line, tag)
        rc = self.proc.wait()
        self.proc = None
        return rc, lines

    def _lift_stage1_output(self, tmp: Path, site: Path):
        """Move stage 1's files out of its subfolder into the output folder
        itself, replacing any from an earlier run, then remove the subfolder."""
        if not (tmp / "site.gml.gz").exists():
            if (tmp / "manifest.json").exists():
                raise RuntimeError("stage 1 found no features in the box "
                                   "(see manifest / log above)")
            raise RuntimeError(f"stage 1 output not found in {tmp}")
        for f in tmp.iterdir():
            os.replace(f, site / f.name)
        shutil.rmtree(tmp, ignore_errors=True)
        man = site / "manifest.json"
        try:                     # keep the manifest's paths truthful
            m = json.loads(man.read_text(encoding="utf-8"))
            m["site"] = site.name
            m["output"] = str(site / "site.gml.gz")
            man.write_text(json.dumps(m, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _worker(self, jobs):
        sdir = Path(self.vars["scripts_dir"].get())
        data = self.vars["data_root"].get().strip()
        done, failed, tab_failed = [], [], []
        reindex = self.vars["reindex"].get()
        for i, j in enumerate(jobs, 1):
            if self.stop_flag.is_set():
                break
            x0, y0, x1, y1 = j["box"]
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            out_root = j["out_root"]
            self.msgq.put(("status", f"Site {i}/{len(jobs)}: {j['name']}",
                           None))
            self._log(f"\n=== {j['name']}  ({i}/{len(jobs)})", "head")
            self._log("Shapefile(s): " + ", ".join(Path(f).name
                                                   for f in j["files"]))
            self._log(f"Box W{x0} S{y0} E{x1} N{y1}  "
                      f"({x1 - x0} x {y1 - y0} m), centre {cx},{cy}")
            try:
                out_root.mkdir(parents=True, exist_ok=True)
                site = j["site_dir"]
                site.mkdir(parents=True, exist_ok=True)
                tmp = site / STAGE1_TMP
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
                args = ["--centre", f"{cx},{cy}", "--width", str(x1 - x0),
                        "--height", str(y1 - y0), "--data", data,
                        "--output", str(site), "--name", STAGE1_TMP]
                if reindex:
                    args.append("--reindex")
                    reindex = False          # only once per run
                self._log("Stage 1: extracting site from OS tiles", "head")
                rc, lines = self._exec(script_command(sdir / STAGE1, args))
                if self.stop_flag.is_set():
                    break
                if rc != 0:
                    raise RuntimeError(f"stage 1 failed (exit code {rc})")
                self._lift_stage1_output(tmp, site)
                self._write_extent_record(site, j, cx, cy)
                if self.vars["run_stage2"].get():
                    self._log("Stage 2: styling into MM_* tables", "head")
                    rc, _ = self._exec(script_command(sdir / STAGE2,
                                                      [str(site)]))
                    if self.stop_flag.is_set():
                        break
                    if rc != 0:
                        raise RuntimeError(f"stage 2 failed (exit code {rc})")
                if self.vars["shp_to_tab"].get():
                    self._log("Shapefile(s) to MapInfo TAB", "head")
                    rc, _ = self._exec(script_command(
                        sdir / SHP2TAB, [*j["files"], "--output", str(site)]))
                    if self.stop_flag.is_set():
                        break
                    if rc != 0:
                        self._log("Shapefile TAB conversion had problems "
                                  "(see above); MasterMap tables are fine.",
                                  "warn")
                        tab_failed.append(j["name"])
                # PDF/JPG map output will be added here later.
                self._log(f"Finished: {site}", "ok")
                done.append(site)
            except Exception as e:
                self._log(f"FAILED {j['name']}: {e}", "err")
                failed.append(j["name"])
            finally:
                tmp = j["site_dir"] / STAGE1_TMP
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)

        if self.stop_flag.is_set():
            self._log("\nStopped.", "warn")
            self.msgq.put(("status", "Stopped", None))
        else:
            summary = f"{len(done)} site(s) done"
            if failed:
                summary += f", {len(failed)} failed: " + ", ".join(failed)
            if tab_failed:
                summary += ("; shapefile TAB not made for: "
                            + ", ".join(tab_failed))
            self._log("\n" + summary,
                      "err" if failed else "warn" if tab_failed else "ok")
            self.msgq.put(("status", summary, None))
            if done and self.vars["open_when_done"].get() and os.name == "nt":
                try:
                    os.startfile(str(done[-1]))
                except Exception:
                    pass
        self.msgq.put(("done", None, None))

    def _write_extent_record(self, site: Path, job, cx, cy):
        """Save how the box was made, next to the site output."""
        x0, y0, x1, y1 = job["box"]
        rec = {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_shapefiles": job["files"],
            "shapefile_extent": list(union_bbox(
                self.shapes[f]["bbox"] for f in job["files"])),
            "buffer_m": self._buffer(),
            "box": {"west": x0, "south": y0, "east": x1, "north": y1},
            "centre": [cx, cy],
            "size_m": [x1 - x0, y1 - y0],
            "tiles": [t for t, _, _ in tiles_for_box(job["box"])],
            "crs": "EPSG:27700",
        }
        try:
            (site / "site_extent_v2.json").write_text(
                json.dumps(rec, indent=2), encoding="utf-8")
        except Exception as e:
            self._log(f"Couldn't write site_extent_v2.json: {e}", "warn")

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP_TITLE,
                                       "A run is in progress. Stop it and "
                                       "close?"):
                return
            self._stop()
        self._save_settings()
        self.destroy()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        script = sys.argv[2]
        sys.argv = [script] + sys.argv[3:]
        sys.path.insert(0, str(Path(script).parent))
        runpy.run_path(script, run_name="__main__")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
