#!/usr/bin/env python3
"""
osmm_plan.py  --  site plan (PDF and/or JPG) for the OS Shapefile Tool

Draws a Land Registry style plan: the OS MasterMap linework and text in black
and white, with the site shapefile on top as a red outline, a scale bar, and
the title strip from the company layout (location, scale, drawing number,
date, north point, logo, address and copyright).

    python osmm_plan.py --gml <site.gml.gz> --shapefile <site.shp>
                        --title "Land at ..." --drawing "ABC123-01"
                        --output <folder\\file name> [--format pdf|jpg|both]
                        [--template plan_template.json]

                        [--orientation auto|portrait|landscape]
                        [--style outline|fill] [--colour-by FIELD]
                        [--key] [--key-area] [--title-field FIELD]

Scale and paper are chosen automatically: the largest Land Registry scale
(1:500, then 1:1,250, then 1:2,500) at which the shapefile fits on A4
(portrait or landscape, or only the one asked for). If it doesn't fit on A4
even at 1:2,500, A3 is tried, then A2, A1 and A0.

The title box is always A4-portrait wide. On A4 portrait it fills the bottom
of the sheet; on landscape and larger sheets it sits in the bottom-left
corner and the map fills the rest, with the site kept clear of the box.

PDF needs only the Python standard library (this file writes the PDF
itself). JPG needs Pillow (pip install pillow).

The OS linework, symbols, tree patterns and text placement come from stage 2
(osmm_style_mapinfo.py), so the plan matches the MM_* tables exactly.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osmm_style_mapinfo as stage2  # noqa: E402

# --------------------------------------------------------------------------
# Paper, scales and the layout (measured from the company Layout.pdf)
# --------------------------------------------------------------------------

PT_PER_MM = 72 / 25.4
PAPERS = [("A4", 210, 297), ("A3", 297, 420), ("A2", 420, 594),
          ("A1", 594, 841), ("A0", 841, 1189)]          # mm, portrait
SCALES = [500, 1250, 2500]                              # Land Registry scales
FIT_MARGIN_MM = 10      # keep the site at least this far inside the map frame

# Everything below is in PDF points, measured on the A4 template.
FRAME_INSET = 6.6        # border distance from the paper edge
FRAME_WIDTH = 1.44       # border line width
STRIP_TOP = 107.1        # title strip top, from the paper bottom
# Title strip contents, as distances from the bottom edge of the paper:
TITLE_BASE = 85.2        # title baseline (17 pt Times)
LEFT_X = 55.4            # left column centre, from the left frame
LEFT_BASES = (61.5, 49.0, 36.7, 24.2)                   # 7 pt Times
COMPASS_DX, COMPASS_Y = -121.6, 46.3                    # from the page centre
LOGO_W, LOGO_H, LOGO_Y = 122.3, 36.2, 36.1              # logo bottom from paper bottom
ADDRESS_BASES = (24.0, 15.8)                            # 7 pt Helvetica
RIGHT_DX = 78.5          # right column centre, from the right paper edge
RIGHT_BASES = (57.9, 49.6, 41.4, 33.1, 24.8)           # 7 pt Times

# Title box: always the width of the A4 portrait frame, in the bottom-left.
TBOX_W = 210 * 72 / 25.4 - 2 * FRAME_INSET

# Scale bar length (m) for each scale and paper.
SCALEBAR_M = {
    500: {"A4": 20, "A3": 25, "A2": 40, "A1": 50, "A0": 60},
    1250: {"A4": 50, "A3": 60, "A2": 100, "A1": 120, "A0": 150},
    2500: {"A4": 80, "A3": 100, "A2": 150, "A1": 200, "A0": 250},
}

DEFAULT_TEMPLATE = {
    "logo": "plan_logo.jpg",
    "address": ["Little Cutsey, Cutsey, Taunton. Somerset. TA3 7NY",
                "Tel: 01823 421354 Email: patrick@mapping-company.co.uk"],
    "copyright": ["Based on Ordnance Survey 1:2,500",
                  "mapping with the permission of",
                  "the Controller of HMSO",
                  "\u00a9Crown Copyright",
                  "Licence No. VA 100033416"],
    "boundary_colour": "#ff0000",
    "boundary_width_pt": 1.5,
    "fill_colour": "#f9b9cf",          # 'pink fill' style
    "fill_edge_colour": "#e0457b",     # thin edge round the pink fill
    # Colours used when each title number / polygon gets its own colour
    # (in order of first appearance). Fill style uses a pale tint of each.
    "title_colours": ["#ff0000", "#0057e7", "#00a651", "#ff8c00", "#8e44ad",
                      "#00a5b5", "#c2185b", "#8d6e00", "#4caf50", "#3f51b5"],
}

# --------------------------------------------------------------------------
# Choosing scale and paper
# --------------------------------------------------------------------------

def frame_pt(paper_w_pt, paper_h_pt):
    """Inside of the border (x0, y0, x1, y1) in points; the map fills it."""
    return (FRAME_INSET, FRAME_INSET, paper_w_pt - FRAME_INSET,
            paper_h_pt - FRAME_INSET)


def title_box_pt():
    """The title box: A4-portrait wide, in the bottom-left corner."""
    return (FRAME_INSET, FRAME_INSET, FRAME_INSET + TBOX_W, STRIP_TOP)


def _place(w_pt, h_pt, frame, box, margin):
    """Where the site's centre can go on the sheet (x, y in points) so it is
    inside the frame and clear of the title box, or None. Two areas are
    tried: above the box (full width) and to the right of it (full height);
    the one that keeps the site nearer the middle of the sheet wins."""
    fx0, fy0, fx1, fy1 = frame
    areas = [(fx0 + margin, box[3] + margin, fx1 - margin, fy1 - margin),
             (box[2] + margin, fy0 + margin, fx1 - margin, fy1 - margin)]
    mid = ((fx0 + fx1) / 2, (fy0 + fy1) / 2)
    best = None
    for ax0, ay0, ax1, ay1 in areas:
        if ax1 - ax0 >= w_pt and ay1 - ay0 >= h_pt:
            c = ((ax0 + ax1) / 2, (ay0 + ay1) / 2)
            d = math.hypot(c[0] - mid[0], c[1] - mid[1])
            if best is None or d < best[0]:
                best = (d, c)
    return best[1] if best else None


def choose_layout(bbox, orientation="auto"):
    """bbox = (minE, minN, maxE, maxN) of the site in metres.
    orientation: "auto" (portrait first, then landscape), "portrait" or
    "landscape". Returns a dict describing the plan, or raises ValueError if
    the site is too big even for A0 at 1:2,500."""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    ce, cn = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    orients = {"portrait": ["portrait"], "landscape": ["landscape"]}.get(
        orientation, ["portrait", "landscape"])
    box = title_box_pt()
    for name, pw, ph in PAPERS:
        for scale in SCALES:
            k = 1000 / scale * PT_PER_MM          # points per ground metre
            for orient in orients:
                sw, sh = (pw, ph) if orient == "portrait" else (ph, pw)
                frame = frame_pt(sw * PT_PER_MM, sh * PT_PER_MM)
                c = _place(w * k, h * k, frame, box, FIT_MARGIN_MM * PT_PER_MM)
                if c is None:
                    continue
                # Ground coordinates of the frame corners, with the site's
                # centre at sheet position c.
                g0 = ce - (c[0] - frame[0]) / k
                gy0 = cn - (c[1] - frame[1]) / k
                return {
                    "paper": name, "orientation": orient, "scale": scale,
                    "paper_mm": (sw, sh), "frame_pt": frame, "box_pt": box,
                    "ground": (g0, gy0, g0 + (frame[2] - frame[0]) / k,
                               gy0 + (frame[3] - frame[1]) / k),
                }
    raise ValueError("the site is too large for an A0 sheet at 1:2,500"
                     + ("" if orientation == "auto" else f" ({orientation})"))


def plan_ground_box(bbox, orientation="auto"):
    """The ground area the plan's map frame will show (for the GUI, so it can
    make sure that much OS data is extracted). None if it won't fit."""
    try:
        return choose_layout(bbox, orientation)["ground"]
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


def grid_ref_6(e, n):
    """'ST 460 200' style 6-figure reference (100 m)."""
    e100, n100 = int(e // 100000), int(n // 100000)
    if not (0 <= e100 <= 6 and 0 <= n100 <= 12):
        return f"{e:.0f}, {n:.0f}"
    l1 = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    l2 = ((19 - n100) * 5) % 25 + e100 % 5
    return (f"{_LETTERS[l1]}{_LETTERS[l2]} {int(e % 100000) // 100:03d} "
            f"{int(n % 100000) // 100:03d}")


def parse_hex(colour):
    c = colour.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _sidecar(shp, ext):
    p = Path(shp).with_suffix(ext)
    if p.exists():
        return p
    for q in Path(shp).parent.glob(Path(shp).stem + ".*"):
        if q.suffix.lower() == ext:
            return q
    return None


def read_dbf(shp):
    """Attribute rows of the shapefile as a list of dicts ([] if no .dbf)."""
    dbf = _sidecar(shp, ".dbf")
    if dbf is None:
        return []
    enc = "cp1252"
    cpg = _sidecar(shp, ".cpg")
    if cpg:
        txt = cpg.read_text(errors="ignore").strip().lower()
        if "utf" in txt and "8" in txt:
            enc = "utf-8"
    data = dbf.read_bytes()
    count = struct.unpack_from("<I", data, 4)[0]
    header_len, rec_len = struct.unpack_from("<HH", data, 8)
    fields, pos = [], 32
    while pos < header_len - 1 and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("latin-1").strip()
        fields.append((name, data[pos + 16]))
        pos += 32
    rows = []
    for i in range(count):
        off = header_len + i * rec_len + 1          # skip deletion flag
        row = {}
        for name, length in fields:
            row[name] = data[off:off + length].decode(enc, "replace").strip()
            off += length
        rows.append(row)
    return rows


def read_shapefile(path):
    """Shapefile as a list of records plus the overall bbox.
    Each record: {"parts": [[(x, y), ...], ...], "points": [...],
    "closed": bool, "attrs": {...}}. Only X/Y are used."""
    data = Path(path).read_bytes()
    if len(data) < 100 or struct.unpack_from(">i", data, 0)[0] != 9994:
        raise ValueError(f"{path} is not a valid .shp file")
    attrs = read_dbf(path)
    records = []
    pos = 100
    while pos + 8 <= len(data):
        _, words = struct.unpack_from(">ii", data, pos)
        start, end = pos + 8, pos + 8 + words * 2
        if end > len(data) or words < 2:
            break
        rtype = struct.unpack_from("<i", data, start)[0]
        p = start + 4
        rec = {"parts": [], "points": [], "closed": rtype in (5, 15, 25, 31),
               "attrs": attrs[len(records)] if len(records) < len(attrs) else {}}
        if rtype in (1, 11, 21):
            rec["points"].append(struct.unpack_from("<2d", data, p))
        elif rtype in (8, 18, 28):
            npts = struct.unpack_from("<i", data, p + 32)[0]
            rec["points"] += [struct.unpack_from("<2d", data, p + 36 + 16 * i)
                              for i in range(npts)]
        elif rtype in (3, 13, 23, 5, 15, 25, 31):
            nparts, npts = struct.unpack_from("<2i", data, p + 32)
            q = p + 40
            starts = list(struct.unpack_from(f"<{nparts}i", data, q))
            q += 4 * nparts + (4 * nparts if rtype == 31 else 0)
            xy = [struct.unpack_from("<2d", data, q + 16 * i) for i in range(npts)]
            starts.append(npts)
            for a, b in zip(starts, starts[1:]):
                if b > a:
                    rec["parts"].append(xy[a:b])
        records.append(rec)
        pos = end
    xs = [x for r in records for part in r["parts"] for x, _ in part] + \
         [x for r in records for x, _ in r["points"]]
    ys = [y for r in records for part in r["parts"] for _, y in part] + \
         [y for r in records for _, y in r["points"]]
    if not xs:
        raise ValueError(f"{path} contains no shapes")
    return records, (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------
# Fonts: widths for measuring text, and TrueType files for embedding
# --------------------------------------------------------------------------

AFM_WIDTHS = {   # WinAnsi codes 32..255, from the standard PDF font metrics
'Helvetica': [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 500,
    556, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 222, 222, 333, 333, 500, 556, 1000, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 500, 500, 556, 500, 500, 500, 500, 500, 737, 500, 500, 500, 500, 500, 500,
    400, 500, 500, 500, 500, 500, 500, 278, 500, 500, 500, 500, 834, 834, 834, 500,
    500, 500, 667, 500, 500, 500, 500, 722, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 722, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
    556, 556, 500, 500, 556, 500, 500, 500, 556, 556, 500, 500, 500, 500, 500, 500,
    500, 556, 556, 556, 500, 500, 556, 500, 500, 500, 556, 500, 556, 500, 500, 500,
],
'Times-Roman': [
    250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 278, 278, 564, 564, 564, 444,
    921, 722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722,
    556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611, 333, 278, 333, 469, 500,
    333, 444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500,
    500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444, 480, 200, 480, 541, 500,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 333, 333, 444, 444, 500, 500, 1000, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 760, 500, 500, 500, 500, 500, 500,
    400, 500, 500, 500, 500, 500, 500, 250, 500, 500, 500, 500, 750, 750, 750, 500,
    500, 500, 722, 500, 500, 500, 500, 667, 500, 500, 500, 500, 500, 500, 500, 500,
    500, 722, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
    444, 444, 500, 500, 444, 500, 500, 500, 444, 444, 500, 500, 500, 500, 500, 500,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
],
}


def winansi(text):
    return text.encode("cp1252", errors="replace")


class TTF:
    """Just enough of a TrueType file to embed it in a PDF and measure text."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        d = self.data
        n = struct.unpack_from(">H", d, 4)[0]
        self.tables = {}
        for i in range(n):
            tag, _c, off, length = struct.unpack_from(">4sIII", d, 12 + 16 * i)
            self.tables[tag.decode("latin-1")] = (off, length)
        head = self.tables["head"][0]
        self.upem = struct.unpack_from(">H", d, head + 18)[0]
        self.bbox = struct.unpack_from(">4h", d, head + 36)
        hhea = self.tables["hhea"][0]
        self.ascent, self.descent = struct.unpack_from(">hh", d, hhea + 4)
        n_hmetrics = struct.unpack_from(">H", d, hhea + 34)[0]
        hmtx = self.tables["hmtx"][0]
        self.advances = [struct.unpack_from(">H", d, hmtx + 4 * i)[0]
                         for i in range(n_hmetrics)]
        self.cmap = self._cmap()
        self.cap_height = self.ascent * 0.7
        if "OS/2" in self.tables:
            os2, ln = self.tables["OS/2"]
            if ln >= 90 and struct.unpack_from(">H", d, os2)[0] >= 2:
                self.cap_height = struct.unpack_from(">h", d, os2 + 88)[0]
        name = self.path.stem
        self.ps_name = re.sub(r"[^A-Za-z0-9]", "", name) or "EmbeddedFont"
        self.widths = [self.advance(ch) for ch in
                       (bytes([b]).decode("cp1252", "replace")
                        for b in range(32, 256))]

    def _cmap(self):
        d = self.data
        base = self.tables["cmap"][0]
        n = struct.unpack_from(">H", d, base + 2)[0]
        for i in range(n):
            pid, eid, off = struct.unpack_from(">HHI", d, base + 4 + 8 * i)
            if (pid, eid) in ((3, 1), (0, 3), (0, 4)):
                sub = base + off
                if struct.unpack_from(">H", d, sub)[0] != 4:
                    continue
                segx2 = struct.unpack_from(">H", d, sub + 6)[0]
                ends = struct.unpack_from(f">{segx2 // 2}H", d, sub + 14)
                starts = struct.unpack_from(f">{segx2 // 2}H", d, sub + 16 + segx2)
                deltas = struct.unpack_from(f">{segx2 // 2}h", d, sub + 16 + 2 * segx2)
                roff_pos = sub + 16 + 3 * segx2
                roffs = struct.unpack_from(f">{segx2 // 2}H", d, roff_pos)
                cmap = {}
                for k, (s, e) in enumerate(zip(starts, ends)):
                    for c in range(s, min(e, 0xFFFE) + 1):
                        if c > 0x2122 and c not in (0x20AC,):
                            continue
                        if roffs[k] == 0:
                            g = (c + deltas[k]) & 0xFFFF
                        else:
                            a = roff_pos + 2 * k + roffs[k] + 2 * (c - s)
                            g = struct.unpack_from(">H", d, a)[0]
                            if g:
                                g = (g + deltas[k]) & 0xFFFF
                        cmap[c] = g
                return cmap
        return {}

    def advance(self, ch):
        g = self.cmap.get(ord(ch), 0)
        adv = self.advances[min(g, len(self.advances) - 1)]
        return round(adv * 1000 / self.upem)


def text_width(text, font, size):
    """Width in points of `text` in `font` (a TTF or a base-14 name)."""
    widths = font.widths if isinstance(font, TTF) else AFM_WIDTHS[font]
    return sum(widths[b - 32] if b >= 32 else 0 for b in winansi(text)) * size / 1000


def find_font(names):
    """First existing font file from a list of file names, looked up in the
    Windows font folders (and a few Linux places, for testing)."""
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/usr/share/fonts/truetype/dejavu"),
             Path("/usr/share/fonts/truetype/liberation")]
    for name in names:
        for d in dirs:
            for cand in (d / name, d / name.lower(), d / name.upper()):
                if cand.exists():
                    return cand
    return None


MAP_FONT_FILES = ["GOTHIC.TTF", "arial.ttf", "LiberationSans-Regular.ttf",
                  "DejaVuSans.ttf"]


# --------------------------------------------------------------------------
# The drawing, as a list of simple operations in PDF points (origin at the
# bottom-left of the paper). Both the PDF and the JPG are drawn from this.
# --------------------------------------------------------------------------

class Drawing:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.ops = []

    def line(self, pts, width=0.5, colour=(0, 0, 0), dash=None, closed=False):
        if len(pts) >= 2:
            self.ops.append(("line", pts, width, colour, dash, closed))

    def rect(self, x0, y0, x1, y1, fill=None, stroke=(0, 0, 0), width=0.5):
        self.ops.append(("rect", x0, y0, x1, y1, fill, stroke, width))

    def polygon(self, pts, fill):
        self.ops.append(("poly", pts, fill))

    def fill_rings(self, rings, fill):
        """Fill a polygon given as rings (holes punched by the even-odd rule)."""
        rings = [r for r in rings if len(r) >= 3]
        if rings:
            self.ops.append(("rings", rings, fill))

    def text(self, x, y, string, font, size, angle=0.0, colour=(0, 0, 0),
             align="left", oblique=False):
        """(x, y) is the baseline point given by `align` (left/centre/right).
        Each output measures the text with the font it actually draws with,
        so centred text stays centred even if a substitute font is used."""
        self.ops.append(("text", x, y, string, font, size, angle, colour,
                         oblique, align))


    def image(self, path, x0, y0, x1, y1):
        self.ops.append(("image", Path(path), x0, y0, x1, y1))

    def clip(self, x0, y0, x1, y1):
        self.ops.append(("clip", x0, y0, x1, y1))

    def unclip(self):
        self.ops.append(("unclip",))


def _aligned(x, y, angle, width, align):
    """Baseline-left start point for text of this width and alignment."""
    dx = {"left": 0, "centre": -width / 2, "right": -width}[align]
    a = math.radians(angle)
    return x + dx * math.cos(a), y + dx * math.sin(a)


# --------------------------------------------------------------------------
# PDF writer (standard library only)
# --------------------------------------------------------------------------

def jpeg_size(data):
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack_from(">HH", data, i + 5)
            comps = data[i + 9]
            return w, h, comps
        length = struct.unpack_from(">H", data, i + 2)[0]
        i += 2 + length
    raise ValueError("not a JPEG file")


def _pdf_str(s):
    b = winansi(s)
    return b"(" + b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def write_pdf(drawing, path, title=""):
    objs = []

    def add(obj):
        objs.append(obj)
        return len(objs)

    fonts, font_ids, images = {}, {}, {}

    def font_ref(font):
        key = font.ps_name if isinstance(font, TTF) else font
        if key in fonts:
            return fonts[key]
        fid = f"F{len(fonts) + 1}"
        fonts[key] = fid
        if isinstance(font, TTF):
            stream = add(None)
            objs[stream - 1] = (b"<< /Length %d /Length1 %d /Filter /FlateDecode >>"
                                % (len(zlib.compress(font.data)), len(font.data)),
                                zlib.compress(font.data))
            s = 1000 / font.upem
            desc = add(("<< /Type /FontDescriptor /FontName /%s /Flags 32 "
                        "/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent %d "
                        "/Descent %d /CapHeight %d /StemV 80 /FontFile2 %d 0 R >>"
                        % (font.ps_name, *(round(v * s) for v in font.bbox),
                           round(font.ascent * s), round(font.descent * s),
                           round(font.cap_height * s), stream)).encode())
            font_ids[fid] = add(("<< /Type /Font /Subtype /TrueType /BaseFont /%s "
                                 "/FirstChar 32 /LastChar 255 /Widths [%s] "
                                 "/Encoding /WinAnsiEncoding /FontDescriptor %d 0 R >>"
                                 % (font.ps_name, " ".join(map(str, font.widths)),
                                    desc)).encode())
        else:
            font_ids[fid] = add(("<< /Type /Font /Subtype /Type1 /BaseFont /%s "
                                 "/Encoding /WinAnsiEncoding >>" % font).encode())
        return fid

    def image_ref(p):
        if p in images:
            return images[p]
        data = p.read_bytes()
        w, h, comps = jpeg_size(data)
        cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[comps]
        extra = " /Decode [1 0 1 0 1 0 1 0]" if comps == 4 else ""
        iid = f"Im{len(images) + 1}"
        oid = add(None)
        objs[oid - 1] = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                         b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode%s "
                         b"/Length %d >>" % (w, h, cs.encode(), extra.encode(), len(data)),
                         data)
        images[p] = (iid, oid)
        return images[p]

    out = []
    f2 = lambda v: f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) >= 0.005 else "0"
    for op in drawing.ops:
        kind = op[0]
        if kind == "line":
            _, pts, width, colour, dash, closed = op
            out.append("%s %s %s RG %s w %s d 1 J 1 j" % (
                *(f2(c) for c in colour), f2(width),
                "[" + " ".join(f2(v) for v in dash) + "] 0" if dash else "[] 0"))
            seg = [f"{f2(pts[0][0])} {f2(pts[0][1])} m"]
            seg += [f"{f2(x)} {f2(y)} l" for x, y in pts[1:]]
            out.append(" ".join(seg) + (" h S" if closed else " S"))
        elif kind == "rect":
            _, x0, y0, x1, y1, fill, stroke, width = op
            r = f"{f2(x0)} {f2(y0)} {f2(x1 - x0)} {f2(y1 - y0)} re"
            if fill and stroke:
                out.append("%s %s %s rg %s %s %s RG %s w [] 0 d %s B" % (
                    *(f2(c) for c in fill), *(f2(c) for c in stroke), f2(width), r))
            elif fill:
                out.append("%s %s %s rg %s f" % (*(f2(c) for c in fill), r))
            else:
                out.append("%s %s %s RG %s w [] 0 d %s S" % (
                    *(f2(c) for c in stroke), f2(width), r))
        elif kind == "poly":
            _, pts, fill = op
            seg = [f"{f2(pts[0][0])} {f2(pts[0][1])} m"]
            seg += [f"{f2(x)} {f2(y)} l" for x, y in pts[1:]]
            out.append("%s %s %s rg %s h f" % (*(f2(c) for c in fill), " ".join(seg)))
        elif kind == "rings":
            _, rings, fill = op
            seg = []
            for ring in rings:
                seg.append(f"{f2(ring[0][0])} {f2(ring[0][1])} m")
                seg += [f"{f2(x)} {f2(y)} l" for x, y in ring[1:]]
                seg.append("h")
            out.append("%s %s %s rg %s f*" % (*(f2(c) for c in fill), " ".join(seg)))
        elif kind == "text":
            _, x, y, string, font, size, angle, colour, oblique, align = op
            x, y = _aligned(x, y, angle, text_width(string, font, size), align)
            fid = font_ref(font)
            a = math.radians(angle)
            c, s = math.cos(a), math.sin(a)
            k = 0.21 if oblique else 0.0      # slant for italic water names
            out.append("BT /%s %s Tf %s %s %s rg %s %s %s %s %s %s Tm %s Tj ET" % (
                fid, f2(size), *(f2(v) for v in colour),
                f"{c:.5f}", f"{s:.5f}", f"{k * c - s:.5f}", f"{k * s + c:.5f}",
                f2(x), f2(y), _pdf_str(string).decode("latin-1")))
        elif kind == "image":
            _, p, x0, y0, x1, y1 = op
            iid, _oid = image_ref(p)
            out.append(f"q {f2(x1 - x0)} 0 0 {f2(y1 - y0)} {f2(x0)} {f2(y0)} cm /{iid} Do Q")
        elif kind == "clip":
            _, x0, y0, x1, y1 = op
            out.append(f"q {f2(x0)} {f2(y0)} {f2(x1 - x0)} {f2(y1 - y0)} re W n")
        elif kind == "unclip":
            out.append("Q")

    content = zlib.compress("\n".join(out).encode("latin-1"))
    cid = add((b"<< /Length %d /Filter /FlateDecode >>" % len(content), content))
    res = "<< /Font << %s >> /XObject << %s >> >>" % (
        " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items()),
        " ".join(f"/{i} {o} 0 R" for i, o in images.values()))
    pages_id = len(objs) + 2
    page_id = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] "
                   "/Resources %s /Contents %d 0 R >>"
                   % (pages_id, f2(drawing.width), f2(drawing.height), res, cid)).encode())
    add(("<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page_id).encode())
    info = add(b"<< /Title " + _pdf_str(title) + b" /Producer (OS Shapefile Tool) >>")
    cat = add(("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode())

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += b"%d 0 obj\n" % i
        if isinstance(obj, tuple):
            buf += obj[0] + b"\nstream\n" + obj[1] + b"\nendstream"
        else:
            buf += obj
        buf += b"\nendobj\n"
    xref = len(buf)
    buf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    buf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    buf += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, cat, info, xref))
    Path(path).write_bytes(bytes(buf))


# --------------------------------------------------------------------------
# JPG writer (needs Pillow)
# --------------------------------------------------------------------------

def write_jpg(drawing, path, dpi):
    from PIL import Image, ImageDraw, ImageFont

    k = dpi / 72.0
    W, H = round(drawing.width * k), round(drawing.height * k)
    page = Image.new("RGB", (W, H), "white")
    layer = page                     # current target (page, or the clipped map)
    clip_box = None
    font_cache = {}

    def P(x, y):
        return (x * k, H - y * k)

    def rgb(c):
        return tuple(round(v * 255) for v in c)

    def pil_font(font, size_px):
        key = (font.path if isinstance(font, TTF) else font, round(size_px * 4))
        if key not in font_cache:
            if isinstance(font, TTF):
                path = font.path
            else:
                path = find_font({"Times-Roman": ["times.ttf", "DejaVuSerif.ttf"],
                                  "Helvetica": ["arial.ttf", "LiberationSans-Regular.ttf",
                                                "DejaVuSans.ttf"]}[font])
            try:
                font_cache[key] = ImageFont.truetype(str(path), max(1, round(size_px)))
            except Exception:
                font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    def dashed(pts, dash):
        """Split a polyline into the 'on' pieces of a dash pattern (points)."""
        pattern = [v * k for v in dash]
        out, cur = [], []
        i, left, on = 0, pattern[0], True
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            seg = math.hypot(x1 - x0, y1 - y0)
            t = 0.0
            while seg - t > 1e-9:
                step = min(left, seg - t)
                ta, tb = t / seg, (t + step) / seg
                a = (x0 + (x1 - x0) * ta, y0 + (y1 - y0) * ta)
                b = (x0 + (x1 - x0) * tb, y0 + (y1 - y0) * tb)
                if on:
                    if not cur:
                        cur = [a]
                    cur.append(b)
                t += step
                left -= step
                if left <= 1e-9:
                    if on and cur:
                        out.append(cur)
                        cur = []
                    on = not on
                    i = (i + 1) % len(pattern)
                    left = pattern[i]
        if cur:
            out.append(cur)
        return out

    draw = ImageDraw.Draw(layer)
    for op in drawing.ops:
        kind = op[0]
        if kind == "clip":
            _, x0, y0, x1, y1 = op
            a, b = P(x0, y1)
            c, d = P(x1, y0)
            clip_box = (round(a), round(b), round(c), round(d))
            layer = Image.new("RGB", (W, H), "white")
            draw = ImageDraw.Draw(layer)
        elif kind == "unclip":
            if clip_box:
                page.paste(layer.crop(clip_box), clip_box[:2])
            layer, clip_box = page, None
            draw = ImageDraw.Draw(layer)
        elif kind == "line":
            _, pts, width, colour, dash, closed = op
            pp = [P(x, y) for x, y in pts]
            if closed and pp[0] != pp[-1]:
                pp.append(pp[0])
            wpx = max(1, round(width * k))
            pieces = dashed(pp, dash) if dash else [pp]
            for piece in pieces:
                if len(piece) >= 2:
                    draw.line(piece, fill=rgb(colour), width=wpx, joint="curve")
        elif kind == "rect":
            _, x0, y0, x1, y1, fill, stroke, width = op
            a, b = P(x0, y1)
            c, d = P(x1, y0)
            draw.rectangle([a, b, c, d], fill=rgb(fill) if fill else None,
                           outline=rgb(stroke) if stroke else None,
                           width=max(1, round(width * k)) if stroke else 0)
        elif kind == "poly":
            _, pts, fill = op
            draw.polygon([P(x, y) for x, y in pts], fill=rgb(fill))
        elif kind == "rings":
            _, rings, fill = op
            from PIL import ImageChops
            mask = None
            for ring in rings:
                m = Image.new("1", (W, H), 0)
                ImageDraw.Draw(m).polygon([P(x, y) for x, y in ring], fill=1)
                mask = m if mask is None else ImageChops.logical_xor(mask, m)
            layer.paste(Image.new("RGB", (W, H), rgb(fill)), (0, 0), mask)
        elif kind == "text":
            _, x, y, string, font, size, angle, colour, oblique, align = op
            size_px = size * k
            if size_px < 3:
                continue
            f = pil_font(font, size_px)
            x, y = _aligned(x, y, angle, f.getlength(string) / k, align)
            asc, desc = f.getmetrics()
            tw = max(1, round(f.getlength(string))) + 4
            th = asc + desc + 4
            pad = round(size_px * 0.4)
            txt = Image.new("L", (tw + 2 * pad, th), 0)
            ImageDraw.Draw(txt).text((pad, 2), string, font=f, fill=255)
            if oblique:
                txt = txt.transform(txt.size, Image.AFFINE,
                                    (1, 0.21, -0.21 * (asc + 2), 0, 1, 0),
                                    resample=Image.BICUBIC)
            ox, oy = pad, 2 + asc            # baseline-left inside `txt`
            if abs(angle) > 0.01:
                side = 2 * math.ceil(math.hypot(txt.width, txt.height))
                big = Image.new("L", (side, side), 0)
                big.paste(txt, (side // 2 - ox, side // 2 - oy))
                txt = big.rotate(angle, resample=Image.BICUBIC,
                                 center=(side // 2, side // 2))
                ox = oy = side // 2
            px, py = P(x, y)
            solid = Image.new("RGB", txt.size, rgb(colour))
            layer.paste(solid, (round(px - ox), round(py - oy)), txt)
        elif kind == "image":
            _, p, x0, y0, x1, y1 = op
            a, b = P(x0, y1)
            c, d = P(x1, y0)
            im = Image.open(p).convert("RGB").resize(
                (max(1, round(c - a)), max(1, round(d - b))), Image.LANCZOS)
            layer.paste(im, (round(a), round(b)))
    page.save(path, "JPEG", quality=92, dpi=(dpi, dpi), optimize=True)


def jpg_dpi(paper_mm):
    """300 dpi, reduced on big sheets so the JPG stays a sensible size."""
    w_in, h_in = paper_mm[0] / 25.4, paper_mm[1] / 25.4
    return int(min(300, math.sqrt(60e6 / (w_in * h_in))))


# --------------------------------------------------------------------------
# Building the plan
# --------------------------------------------------------------------------

def mapinfo_pen(pen_s):
    """'Pen (w,pattern,colour)' -> (width pt, dash list or None, visible,
    colour as 0-1 RGB)."""
    m = re.match(r"Pen \((\d+),(\d+),(\d+)\)", pen_s)
    w, pattern, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Heavier than on screen, to match the printed InterpOSe/MapInfo plans
    width = 0.7 if w < 10 else max(0.45, (w - 10) / 10)
    dash = {3: [0.6, 1.4], 4: [3.0, 2.0], 5: [0.8, 1.2], 6: [4, 1.5, 0.8, 1.5],
            7: [6, 2], 8: [6, 2, 1, 2]}.get(pattern)
    colour = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
    return width, dash, pattern != 1, colour


def draw_os(d, gml, to_paper, scale, log):
    """OS MasterMap linework, symbols and tree patterns, in the same colours
    as the MM_* tables (e.g. water in blue). Returns the text, to be drawn
    later so it sits on top of the site boundary."""
    report = stage2.Report()
    texts = []
    n = 0

    def pen_line(parts, pen_s):
        width, dash, vis, colour = mapinfo_pen(pen_s)
        if vis:
            for part in parts:
                d.line([to_paper(*p) for p in part], width, colour, dash)

    for f in stage2.read_features(gml):
        n += 1
        groups, terms = f.all("descriptiveGroup"), f.all("descriptiveTerm")
        if f.cls == "TopographicLine":
            pen_line(f.parts(), stage2.classify_detail(
                groups, terms, f.one("make"), f.one("physicalPresence"), report))
        elif f.cls == "BoundaryLine":
            for part in f.parts():
                d.line([to_paper(*p) for p in part], 0.5, (0, 0, 0),
                       [4, 1.5, 0.8, 1.5])
        elif f.cls == "CartographicSymbol":
            pts = f.parts()
            if not pts:
                continue
            x, y = pts[0][0]
            angle = float(f.one("orientation", "0") or 0) / 10.0
            code = int(f.one("featureCode", "0") or 0)
            if code in stage2.SYMBOLS:
                _legend, pen_s, template = stage2.SYMBOLS[code]
            else:
                pen_s, template = "Pen (1,2,0)", stage2.SYMBOL_FALLBACK
            pen_line(stage2.place(template, x, y, angle), pen_s)
        elif f.cls == "TopographicArea":
            rings = f.rings()
            if not rings:
                continue
            if "Landform" in groups and "Cliff" in terms:
                pen_line(stage2.cliff_hatch(rings), stage2.TREE_PENS["0396 Cliff"])
                continue
            if "Natural Environment" in groups:
                key = frozenset(t for t in terms if t in stage2.VEG_GLYPHS)
                if not key:
                    continue
                tlegend = stage2.TREE_LEGENDS.get(key)
                if not tlegend:
                    tlegend = ("0000 " + " & ".join(
                        stage2.TREE_ABBREV[t] for t in stage2.TREE_ABBREV
                        if t in key))[:30]
                    tlegend = tlegend[:5] + tlegend[5:6].upper() + tlegend[6:]
                colour = "#808080" if tlegend.startswith("0385") else "#669966"
                tpen = stage2.TREE_PENS.get(tlegend) or stage2.pen(
                    12 if tlegend in stage2.TREE_THIN else 1, 2, colour)
                try:
                    parts = stage2.tree_pattern(rings, tlegend, terms)
                except Exception:
                    parts = []
                pen_line(parts, tpen)
        elif f.cls == "CartographicText":
            pts = f.parts()
            string = f.one("textString")
            if not pts or not string:
                continue
            x, y = pts[0][0]
            rule = next((stage2.TEXT_RULES[g] for g in groups
                         if g in stage2.TEXT_RULES), None)
            colour = rule[1] if rule else "#333333"
            if colour.lower() == "#ff0000":      # keep red for the site only
                colour = "#333333"
            texts.append((string, x, y,
                          float(f.one("height", "1") or 1) * (rule[4] if rule else 1.15),
                          float(f.one("orientation", "0") or 0) / 10.0,
                          int(f.one("anchorPosition", "0") or 0),
                          bool(rule and rule[3] == 2), parse_hex(colour)))
    return n, texts


def draw_os_text(d, texts, to_paper, scale, log):
    map_font_path = find_font(MAP_FONT_FILES)
    font = TTF(map_font_path) if map_font_path else "Helvetica"
    if map_font_path:
        log(f"  map text font: {map_font_path.name}")
    asc = (font.cap_height / font.upem) if isinstance(font, TTF) else 0.72
    for string, x, y, height, angle, anchor, italic, colour in texts:
        size = max(4.0, height * 1000 / scale * PT_PER_MM)   # OS height = text size
        w = text_width(string, font, size)
        u = (0.0, w / 2, w)[min(anchor // 3, 2)]
        v = (0.0, asc * size / 2, asc * size)[anchor % 3]
        a = math.radians(angle)
        c, s = math.cos(a), math.sin(a)
        px, py = to_paper(x, y)
        d.text(px - (u * c - v * s), py - (u * s + v * c), string, font, size,
               angle, colour, oblique=italic)


def nice_scalebar(scale, paper="A4"):
    """Scale bar length (m) for this scale and paper size."""
    return SCALEBAR_M.get(scale, {}).get(paper) or round(40 * scale / 1000, -1)


def draw_scalebar(d, x, y, scale, paper="A4"):
    total = nice_scalebar(scale, paper)
    length = total * 1000 / scale * PT_PER_MM
    h = 4.5
    pad = 6
    d.rect(x - pad - 4, y - 15, x + length + pad + 6, y + h + 12,
           fill=(1, 1, 1), stroke=None)
    for i in range(4):
        x0 = x + length * i / 4
        x1 = x + length * (i + 1) / 4
        d.rect(x0, y, x1, y + h, fill=(0, 0, 0) if i % 2 == 0 else (1, 1, 1),
               stroke=(0, 0, 0), width=0.5)
    for frac in (0, 0.5, 1):
        val = total * frac
        d.text(x + length * frac, y + h + 3, f"{val:g}", "Helvetica", 8,
               align="centre")
    d.text(x + length / 2, y - 10, "metres", "Helvetica", 8, align="centre")


def draw_compass(d, cx, cy):
    """Small four-point north arrow like the template's."""
    r, w = 8.5, 2.2
    black, white = (0, 0, 0), (1, 1, 1)
    for i, ang in enumerate((90, 0, 270, 180)):        # N, E, S, W
        a = math.radians(ang)
        tip = (cx + r * math.cos(a), cy + r * math.sin(a))
        l = (cx + w * math.cos(a + math.pi / 2), cy + w * math.sin(a + math.pi / 2))
        rr = (cx + w * math.cos(a - math.pi / 2), cy + w * math.sin(a - math.pi / 2))
        d.polygon([(cx, cy), l, tip], black)
        d.polygon([(cx, cy), tip, rr], white if i else black)
        d.line([(cx, cy), l, tip, rr, (cx, cy)], 0.4, black, closed=True)
    for ch, (dx, dy) in (("N", (0, r + 2)), ("S", (0, -r - 7)),
                         ("E", (r + 4.5, -2.2)), ("W", (-r - 4.5, -2.2))):
        d.text(cx + dx, cy + dy, ch, "Times-Roman", 5.5, align="centre")


def _tint(colour, amount=0.62):
    """A pale version of a colour (for fills)."""
    return tuple(c + (1 - c) * amount for c in colour)


SQM_PER_ACRE = 4046.8564224


def _ring_area(ring):
    """Unsigned area of one ring (m2), by the shoelace formula."""
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1)
                   in zip(ring, ring[1:] + ring[:1]))) / 2


def _inside_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def polygon_area(parts):
    """Area (m2) of a polygon record: rings inside an odd number of other
    rings are holes and are subtracted (works whatever the ring direction)."""
    total = 0.0
    for i, ring in enumerate(parts):
        if len(ring) < 3:
            continue
        x, y = ring[0]
        depth = sum(1 for j, other in enumerate(parts)
                    if j != i and len(other) >= 3 and _inside_ring(x, y, other))
        total += -_ring_area(ring) if depth % 2 else _ring_area(ring)
    return total


def area_text(m2):
    return f"{m2 / 10000:.2f} ha / {m2 / SQM_PER_ACRE:.2f} ac"


def _boundary_colours(records, template, style, colour_by, key_field=""):
    """Colour (outline or fill) for each record, and the key entries:
    [(label, colour, area m2 or None), ...], one per title number (or one
    for the whole site if there's no title field)."""
    single = parse_hex(template.get("boundary_colour", "#ff0000")) \
        if style == "outline" else parse_hex(template.get("fill_colour", "#f9b9cf"))
    palette = [parse_hex(c) for c in template.get("title_colours", [])] or [single]
    group_field = colour_by or key_field
    keys, colours, areas = {}, [], {}
    for r in records:
        value = (r["attrs"].get(group_field, "") or "(blank)") if group_field \
            else "Site"
        if value not in keys:
            keys[value] = palette[len(keys) % len(palette)] if colour_by else single
        base = keys[value]
        colours.append(base if style == "outline" or not colour_by else _tint(base))
        if r["closed"]:
            areas[value] = areas.get(value, 0.0) + polygon_area(r["parts"])
    key = []
    for value, c in keys.items():
        shown = c if style == "outline" or not colour_by else _tint(c)
        key.append((value, shown, areas.get(value)))
    return colours, key


def draw_legend(d, x1, y0, entries, field, style, show_area=False):
    """Key for the site, bottom-right of the map: one line per title number,
    optionally with its area, e.g. 'ST136465 (0.71 ha / 1.75 ac)'."""
    entries = entries[:14]
    label = "Title No." if field.lower() in ("title_no", "title", "titleno") \
        else (field or "Key")
    line_h, size = 10, 7
    texts = []
    for value, colour, area in entries:
        t = str(value)
        if show_area and area is not None:
            t += f" ({area_text(area)})"
        texts.append(t)
    w = max(text_width(t, "Helvetica", size) for t in texts) + 28
    w = max(w, text_width(label, "Helvetica", size) + 12)
    h = line_h * (len(entries) + 1) + 6
    x0 = x1 - w
    d.rect(x0, y0, x1, y0 + h, fill=(1, 1, 1), stroke=(0, 0, 0), width=0.5)
    d.text(x0 + 6, y0 + h - 10, label, "Helvetica", size)
    for i, ((value, colour, area), t) in enumerate(zip(entries, texts)):
        yy = y0 + h - 10 - line_h * (i + 1)
        if style == "outline":
            d.line([(x0 + 6, yy + 2.5), (x0 + 18, yy + 2.5)], 1.5, colour)
        else:
            d.rect(x0 + 6, yy - 0.5, x0 + 18, yy + 6, fill=colour,
                   stroke=(0.3, 0.3, 0.3), width=0.3)
        d.text(x0 + 22, yy, t, "Helvetica", size)


def build_plan(gml, shapefile, title, drawing_no, template, log=print,
               today=None, orientation="auto", style="outline", colour_by="",
               key=False, key_area=False, title_field="title_no"):
    records, bbox = read_shapefile(shapefile)
    fields = list(records[0]["attrs"]) if records else []
    for name in ("colour_by", "title_field"):
        f_ = locals()[name]
        if f_ and f_ not in fields:
            log(f"  Warning: the shapefile has no '{f_}' field (fields: "
                f"{', '.join(fields) or 'none'}); "
                + ("using one colour" if name == "colour_by"
                   else "the key shows the site as a whole"))
    if colour_by not in fields:
        colour_by = ""
    if title_field not in fields:
        title_field = ""
    lay = choose_layout(bbox, orientation)
    scale, paper = lay["scale"], lay["paper"]
    pw, ph = (v * PT_PER_MM for v in lay["paper_mm"])
    d = Drawing(pw, ph)
    fx0, fy0, fx1, fy1 = lay["frame_pt"]
    bx0, by0, bx1, by1 = lay["box_pt"]
    g0, gy0 = lay["ground"][0], lay["ground"][1]
    k = 1000 / scale * PT_PER_MM               # points per metre on the ground

    def to_paper(e, n):
        return (fx0 + (e - g0) * k, fy0 + (n - gy0) * k)

    log(f"  plan: 1:{scale:,} on {paper} {lay['orientation']}"
        + (", site in pink fill" if style == "fill" else ""))
    colours, legend = _boundary_colours(records, template, style, colour_by,
                                        title_field if key else "")
    bw = float(template.get("boundary_width_pt", 1.5))

    # --- map: fill (under everything), OS lines, outline, OS text on top ---
    d.clip(fx0, fy0, fx1, fy1)
    if style == "fill":
        for r, colour in zip(records, colours):
            if r["closed"]:
                d.fill_rings([[to_paper(*p) for p in part] for part in r["parts"]],
                             colour)
    n, texts = draw_os(d, gml, to_paper, scale, log)
    log(f"  {n} OS features drawn")
    for r, colour in zip(records, colours):
        if style == "fill" and r["closed"]:
            edge = parse_hex(template.get("fill_edge_colour", "#e0457b")) \
                if not colour_by else tuple(c * 0.75 for c in colour)
            for part in r["parts"]:
                d.line([to_paper(*p) for p in part], 0.6, edge, closed=True)
            continue
        line_colour = colour if style == "outline" else \
            parse_hex(template.get("boundary_colour", "#ff0000"))
        for part in r["parts"]:
            d.line([to_paper(*p) for p in part], bw, line_colour, closed=r["closed"])
        for e, nn in r["points"]:
            px, py = to_paper(e, nn)
            d.line([(px + 3 * math.cos(t / 12 * 2 * math.pi),
                     py + 3 * math.sin(t / 12 * 2 * math.pi)) for t in range(13)],
                   bw, line_colour, closed=True)
    draw_os_text(d, texts, to_paper, scale, log)
    draw_scalebar(d, fx0 + 22, by1 + 20, scale, paper)
    if key and legend:
        draw_legend(d, fx1 - 8, (fy0 if bx1 < fx1 - 120 else by1) + 8, legend,
                    colour_by or title_field, style, show_area=key_area)
        if key_area:
            for value, _c, area in legend:
                if area is not None:
                    log(f"  {value}: {area_text(area)}")
    d.unclip()

    # --- border and title box (A4 width, bottom-left) ---
    d.rect(bx0, by0, bx1, by1, fill=(1, 1, 1), stroke=None)
    d.rect(fx0, fy0, fx1, fy1, stroke=(0, 0, 0), width=FRAME_WIDTH)
    d.line([(bx0, by1), (bx1, by1)], FRAME_WIDTH, (0, 0, 0))
    if bx1 < fx1 - 0.5:
        d.line([(bx1, by0), (bx1, by1)], FRAME_WIDTH, (0, 0, 0))
    cx = (bx0 + bx1) / 2                      # middle of the title box
    if title:
        size = 17
        while size > 9 and text_width(title, "Times-Roman", size) > TBOX_W - 20:
            size -= 0.5
        d.text(cx, TITLE_BASE, title, "Times-Roman", size, align="centre")
    today = today or datetime.date.today()
    ce, cn = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    left = [f"Location: {grid_ref_6(ce, cn)}",
            f"Scale 1:{scale:,} @ {paper}",
            f"Drawing No. {drawing_no}".rstrip(),
            f"Date {today:%d.%m.%y}"]
    for text, base in zip(left, LEFT_BASES):
        d.text(bx0 + LEFT_X, base, text, "Times-Roman", 7, align="centre")
    draw_compass(d, cx + COMPASS_DX, COMPASS_Y)
    logo = template.get("logo")
    if logo:
        lp = Path(logo)
        if not lp.is_absolute():
            lp = Path(__file__).resolve().parent / lp
        if lp.exists():
            d.image(lp, cx - LOGO_W / 2, LOGO_Y, cx + LOGO_W / 2, LOGO_Y + LOGO_H)
        else:
            log(f"  Warning: logo not found ({lp}); plan made without it")
    for text, base in zip(template.get("address", []), ADDRESS_BASES):
        d.text(cx, base, text, "Helvetica", 7, align="centre")
    for text, base in zip(template.get("copyright", []), RIGHT_BASES):
        d.text(bx1 + FRAME_INSET - RIGHT_DX, base, text, "Times-Roman", 7,
               align="centre")
    return d, lay


def load_template(path):
    t = dict(DEFAULT_TEMPLATE)
    p = Path(path) if path else Path(__file__).resolve().parent / "plan_template.json"
    if p.exists():
        try:
            t.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:          # noqa: BLE001
            print(f"  Warning: couldn't read {p.name} ({exc}); using defaults")
    return t


def main(argv=None):
    p = argparse.ArgumentParser(description="Make a site plan (PDF/JPG).")
    p.add_argument("--gml", required=True, help="stage 1 site.gml.gz")
    p.add_argument("--shapefile", required=True, help="the site shapefile")
    p.add_argument("--title", default="", help="plan title")
    p.add_argument("--drawing", default="", help="drawing number")
    p.add_argument("--output", required=True,
                   help="output file name, with or without .pdf/.jpg")
    p.add_argument("--format", choices=("pdf", "jpg", "both"), default="pdf")
    p.add_argument("--template", help="plan_template.json (company details)")
    p.add_argument("--orientation", choices=("auto", "portrait", "landscape"),
                   default="auto")
    p.add_argument("--style", choices=("outline", "fill"), default="outline",
                   help="site as a red outline, or a pink fill")
    p.add_argument("--colour-by", default="",
                   help="give each value of this field (e.g. title_no) its own colour")
    p.add_argument("--key", action="store_true",
                   help="add a key listing each title number")
    p.add_argument("--key-area", action="store_true",
                   help="show each title's area (ha / acres) in the key")
    p.add_argument("--title-field", default="title_no",
                   help="field holding the title number (for the key)")
    a = p.parse_args(argv)

    print("Making the site plan")
    out = Path(a.output)
    if out.suffix.lower() in (".pdf", ".jpg", ".jpeg"):
        out = out.with_suffix("")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        d, lay = build_plan(a.gml, a.shapefile, a.title, a.drawing,
                            load_template(a.template),
                            orientation=a.orientation, style=a.style,
                            colour_by=a.colour_by.strip(), key=a.key,
                            key_area=a.key_area,
                            title_field=a.title_field.strip())
    except ValueError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)
    failed = False
    if a.format in ("pdf", "both"):
        write_pdf(d, out.with_suffix(".pdf"), a.title)
        print(f"  PDF written: {out.with_suffix('.pdf')}")
    if a.format in ("jpg", "both"):
        try:
            dpi = jpg_dpi(lay["paper_mm"])
            write_jpg(d, out.with_suffix(".jpg"), dpi)
            print(f"  JPG written: {out.with_suffix('.jpg')}  ({dpi} dpi)")
        except ImportError:
            print("  ERROR: JPG output needs the Pillow package. Install it "
                  "with:  pip install pillow   (the PDF is not affected)")
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
