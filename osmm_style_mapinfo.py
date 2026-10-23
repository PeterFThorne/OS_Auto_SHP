#!/usr/bin/env python3
"""
osmm_style_mapinfo.py  --  STAGE 2 of the OS MasterMap -> MapInfo pipeline

Reads the site.gml.gz written by Stage 1 (osmm_site_extract.py) and writes the
seven styled MapInfo tables that InterpOSe produces:

    MM_Region   filled polygons            MM_Symbol   symbols drawn as linework
    MM_Detail   linework                   MM_Point    spot heights / point features
    MM_Text     map text                   MM_Parish   boundary lines
    MM_Trees    vegetation patterns

Columns, LEGEND classes and styles follow the InterpOSe sample output
(v4.5.1.59). Everything is written as MIF/MID first (plain text, standard
library only, opens in any MapInfo version via Table > Import). If GDAL is
available (ogr2ogr on the PATH, the osgeo Python module, or pyogrio) the MIF
files are also converted to native .TAB in a format MapInfo 8.5 can open.

USAGE
    python osmm_style_mapinfo.py <site folder or site.gml.gz> [--output folder]
                                 [--no-tab] [--keep-mif]

Classes that did not occur in the sample output are given a sensible default
style and listed in style_report.txt so they can be reviewed.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

COORDSYS = ('CoordSys Earth Projection 8, 79, "m", -2, 49, 0.9996012717, '
            '400000, -100000 Bounds (0, 0) (2000000, 2000000)')
MAX_NODES = 30000          # keep single objects readable by old MapInfo


def rgb(hexcode: str) -> int:
    return int(hexcode.lstrip("#"), 16)


# Pen widths: 1 = 1 pixel, 12 = 0.2 pt, 20 = 1.0 pt  (MapInfo point widths)
def pen(width, pattern, colour):
    return f"Pen ({width},{pattern},{rgb(colour)})"


def brush(pattern, fore, back=None):
    if back is None:
        return f"Brush ({pattern},{rgb(fore)})"
    return f"Brush ({pattern},{rgb(fore)},{rgb(back)})"


# --------------------------------------------------------------------------
# Style rules (from the InterpOSe sample output)
# --------------------------------------------------------------------------

REGION_PEN = pen(12, 2, "#cccccc")
REGION_STYLES = {
    "0000 Manmade surface or step":   (REGION_PEN, brush(2, "#cccc99")),
    "0000 Multiple surface (garden)": (REGION_PEN, brush(2, "#ffffd0")),
    "0000 Natural surface":           (REGION_PEN, brush(2, "#ccff99")),
    "0000 Path":                      (REGION_PEN, brush(15, "#cccccc", "#cccc99")),
    "0000 Railway":                   (REGION_PEN, brush(49, "#ffffff", "#cccccc")),
    "0000 Road":                      (REGION_PEN, brush(2, "#cccccc")),
    "0000 Structure":                 (REGION_PEN, brush(2, "#ffcccc")),
    "0000 Track":                     (REGION_PEN, brush(2, "#cccccc")),
    "0000 Unclassified (or broken)":  (REGION_PEN, brush(2, "#ffff99")),
    "0321 Building":                  (REGION_PEN, brush(2, "#ffcc99")),
    "0379 Coniferous trees":          (REGION_PEN, brush(2, "#d0ffd0", "#ccff99")),
    "0384 Nonconiferous trees":       (REGION_PEN, brush(2, "#d0ffd0", "#ccff99")),
    "0385 Nonconiferous - scattered": (REGION_PEN, brush(2, "#d0ffd0", "#ffffcc")),
    "0390 Rough grassland":           (pen(1, 2, "#cccccc"), brush(2, "#edffd0", "#ccff99")),
    "0392 Scrub":                     (REGION_PEN, brush(2, "#d0ffd0", "#99ff99")),
    "0400 Inland water":              (REGION_PEN, brush(2, "#b0ffff")),
    # Added after the SH1020 / NT805413 tests (not in the InterpOSe sample)
    "0000 Tidal water":               (REGION_PEN, brush(2, "#b0ffff")),
    "0000 Foreshore":                 (REGION_PEN, brush(2, "#f5ecc8")),
    "0000 Rock":                      (REGION_PEN, brush(2, "#e0e0e0")),
    "0000 Marsh":                     (REGION_PEN, brush(2, "#d0f0e8")),
    "0000 Heath":                     (REGION_PEN, brush(2, "#f0e6f0")),
    "0000 Sand or shingle":           (REGION_PEN, brush(2, "#f5ecc8")),
}
# Non-vegetation Natural Environment terms, checked after the tree/grass terms
NATURAL_SURFACES = [
    (("Marsh", "Reeds", "Saltmarsh"), "0000 Marsh"),
    (("Rock", "Rock (Scattered)", "Boulders", "Boulders (Scattered)", "Scree"), "0000 Rock"),
    (("Sand", "Shingle", "Mud"), "0000 Sand or shingle"),
    (("Heath",), "0000 Heath"),
]
# Defaults for classes not present in the sample (reported as unverified)
REGION_FALLBACK = {
    "water":   (REGION_PEN, brush(2, "#b0ffff")),
    "veg":     (REGION_PEN, brush(2, "#edffd0", "#ccff99")),
    "build":   (REGION_PEN, brush(2, "#ffcc99")),
}

VEG_REGION_PRIORITY = [
    ("Nonconiferous Trees", "0384 Nonconiferous trees"),
    ("Coniferous Trees", "0379 Coniferous trees"),
    ("Scrub", "0392 Scrub"),
    ("Nonconiferous Trees (Scattered)", "0385 Nonconiferous - scattered"),
    ("Rough Grassland", "0390 Rough grassland"),
]

# Vegetation pattern: term -> (glyph, scale when alone, use every n-th position)
VEG_GLYPHS = {
    "Coniferous Trees": ("con", 1.0, 1),
    "Nonconiferous Trees": ("non", 1.0, 1),
    "Scrub": ("scrub", 1.0, 1),
    "Rough Grassland": ("grass", 1.0, 1),
    "Nonconiferous Trees (Scattered)": ("non", 0.8, 3),
    "Coniferous Trees (Scattered)": ("con", 0.8, 3),
}
TREE_LEGENDS = {
    frozenset(["Coniferous Trees"]): "0379 Coniferous trees",
    frozenset(["Nonconiferous Trees"]): "0384 Nonconiferous trees",
    frozenset(["Nonconiferous Trees (Scattered)"]): "0385 Nonconiferous - scattered",
    frozenset(["Rough Grassland"]): "0390 Rough grassland",
    frozenset(["Scrub"]): "0392 Scrub",
    frozenset(["Coniferous Trees", "Nonconiferous Trees"]): "0000 Conif & nonconif trees",
    frozenset(["Nonconiferous Trees", "Scrub"]): "0000 Nonconif trees & scrub",
    frozenset(["Nonconiferous Trees (Scattered)", "Scrub"]): "0000 Scrub & scat noncon trees",
    frozenset(["Nonconiferous Trees", "Coniferous Trees", "Scrub"]): "0000 Scrub, conif & nonconif",
    frozenset(["Rough Grassland", "Scrub"]): "0000 Rough grass & scrub",
    frozenset(["Coniferous Trees", "Scrub"]): "0000 Conif trees & scrub",
    frozenset(["Rough Grassland", "Nonconiferous Trees (Scattered)"]): "0000 Grass & scat noncon",
    frozenset(["Rough Grassland", "Coniferous Trees (Scattered)"]): "0000 Grass & scat conif",
    frozenset(["Rough Grassland", "Nonconiferous Trees (Scattered)", "Scrub"]):
        "0000 Grass scrub & scat noncon",
}
TREE_ABBREV = {"Coniferous Trees": "conif", "Nonconiferous Trees": "nonconif",
               "Scrub": "scrub", "Rough Grassland": "grass",
               "Nonconiferous Trees (Scattered)": "scat noncon",
               "Coniferous Trees (Scattered)": "scat conif"}
TREE_SPACING = 8.0        # metres between vegetation glyphs
TREE_THIN = {"0000 Nonconif trees & scrub", "0392 Scrub"}

TEXT_RULES = {   # descriptive group -> (legend, colour, font, italic, size factor)
    "Road Or Track": ("1000 Road name text", "#000000", "Century Gothic", 0, 1.38),
    "Political Or Administrative": ("1005 Boundary text", "#000000", "Century Gothic", 0, 1.15),
    "Buildings Or Structure": ("1006 Building / structure text", "#000000", "Century Gothic", 0, 1.15),
    "General Feature": ("1009 General feature text", "#333333", "Century Gothic", 0, 1.15),
    "General Surface": ("1009 General surface text", "#333333", "Heledd", 0, 1.15),
    "Rail": ("1009 Rail text", "#333333", "Century Gothic", 0, 1.15),
    "Roadside": ("1009 Roadside text", "#333333", "Heledd", 0, 1.15),
    "Terrain And Height": ("1009 Spot height text", "#000000", "Century Gothic", 0, 1.15),
    "Structure": ("1009 Structure text", "#333333", "Century Gothic", 0, 1.15),
    "Inland Water": ("1010 Inland water text", "#000000", "Century Gothic", 2, 1.15),
    # Added after the SH1020 / NT805413 tests (not in the InterpOSe sample)
    "Tidal Water": ("1010 Tidal water text", "#000000", "Century Gothic", 2, 1.15),
    "Historic Interest": ("1009 Historic interest text", "#333333", "Century Gothic", 2, 1.15),
    "Landform": ("1009 Landform text", "#333333", "Century Gothic", 0, 1.15),
    "Path": ("1009 Path text", "#333333", "Century Gothic", 0, 1.15),
}
TEXT_BOX_HEIGHT = 1.45      # text box height as a multiple of the text size

# Character widths as a fraction of text size (fitted to the sample's
# Century Gothic text boxes); anything else uses CHAR_DEFAULT.
CHAR_WIDTHS = {' ': 0.3, '&': 0.81, "'": 0.08, '(': 0.55, ')': 0.55, '.': 0.3, '0': 0.67, '1': 0.66, '2': 0.67, '3': 0.69, '4': 0.66, '5': 0.67, '6': 0.68, '7': 0.66, '8': 0.68, '9': 0.67, 'A': 0.83, 'B': 0.67, 'C': 0.95, 'D': 1.08, 'E': 0.58, 'F': 0.58, 'G': 1.0, 'H': 0.86, 'I': 0.31, 'J': 0.62, 'K': 0.62, 'L': 0.56, 'M': 1.16, 'N': 0.97, 'O': 0.95, 'P': 0.71, 'R': 0.71, 'S': 0.62, 'T': 0.55, 'U': 0.87, 'V': 1.01, 'W': 1.09, 'Y': 0.65, 'a': 0.76, 'b': 0.86, 'c': 0.77, 'd': 0.87, 'e': 0.78, 'f': 0.19, 'g': 0.86, 'h': 0.76, 'i': 0.17, 'k': 0.67, 'l': 0.26, 'm': 1.14, 'n': 0.76, 'o': 0.74, 'p': 0.78, 'r': 0.43, 's': 0.47, 't': 0.4, 'u': 0.66, 'v': 0.71, 'w': 1.04, 'y': 0.61}
CHAR_DEFAULT = 0.68

# Symbol templates: linework in metres about the symbol point, pointing east.
def _circle(cx, cy, r, n=36):
    return [(round(cx + r * math.cos(2 * math.pi * i / n), 3),
             round(cy + r * math.sin(2 * math.pi * i / n), 3)) for i in range(n + 1)]

SYMBOLS = {   # feature code -> (legend, pen, parts)
    10130: ("0013 Boundary half mereing", pen(1, 2, "#000000"),
            [[(0, 0), (2.25, 0)], _circle(2.88, 0, 0.625)]),
    10066: ("0026 Bench mark", pen(1, 2, "#000000"),
            [[(0.71, 0.71), (0, 0), (0.71, -0.71)], [(1.43, 0), (0, 0)]]),
    10091: ("0057 Culvert", pen(12, 2, "#000000"), [[(-0.5, 0), (0.5, 0)]]),
    10082: ("0069 Direction of flow", pen(1, 2, "#0099ff"),
            [[(0, 0), (3.44, 0)], [(3.34, 0.5), (2.84, 0), (3.34, -0.5)],
             [(0.5, 0.5), (0, 0), (0.5, -0.5)], [(3.94, 0.5), (3.44, 0), (3.94, -0.5)]]),
}
SYMBOL_FALLBACK = [[(-0.6, -0.6), (0.6, 0.6)], [(-0.6, 0.6), (0.6, -0.6)]]

GLYPHS = {'non': [[(-1.03, 0.14), (-1.17, 0.01), (-1.26, -0.15), (-1.3, -0.34), (-1.27, -0.52), (-1.19, -0.69), (-1.07, -0.82), (-0.9, -0.92), (-0.72, -0.95), (-0.54, -0.93), (-0.37, -0.85), (-0.23, -0.73), (-0.23, -0.72), (-0.03, -1.5), (0.03, -1.5), (0.24, -0.71), (0.36, -0.85), (0.54, -0.93), (0.72, -0.95), (0.91, -0.92), (1.07, -0.83), (1.2, -0.69), (1.28, -0.52), (1.3, -0.33), (1.26, -0.15), (1.18, 0.02), (1.03, 0.14), (1.13, 0.3), (1.16, 0.48), (1.14, 0.66), (1.05, 0.82), (0.92, 0.94), (0.76, 1.02), (0.58, 1.04), (0.5, 1.23), (0.36, 1.37), (0.2, 1.47), (-0.0, 1.5), (-0.19, 1.47), (-0.37, 1.37), (-0.51, 1.23), (-0.58, 1.04), (-0.76, 1.02), (-0.93, 0.94), (-1.05, 0.81), (-1.14, 0.65), (-1.17, 0.47), (-1.12, 0.3), (-1.03, 0.14)]], 
    'con': [[(-1.3, -0.92), (-1.17, -0.88), (-1.03, -0.83), (-0.9, -0.77), (-0.78, -0.7), (-0.66, -0.62), (-0.54, -0.54), (-0.43, -0.45), (-0.33, -0.35), (-0.24, -0.25), (-0.15, -0.14), (-0.06, -0.02), (-0.0, 0.1), (0.07, -0.02), (0.16, -0.14), (0.25, -0.25), (0.34, -0.35), (0.45, -0.45), (0.56, -0.54), (0.67, -0.62), (0.8, -0.7), (0.92, -0.77), (1.03, -0.83), (1.17, -0.88), (1.3, -0.92)],
    [(0.0, -1.5), (0.0, 1.5)]], 
    'scrub': [[(1.03, -1.49), (-0.05, -1.49), (0.49, -0.89), (0.62, -0.68), (0.69, -0.45), (0.72, -0.21), (0.7, -0.09), (0.63, -0.0), (0.52, 0.05), (0.31, 0.08), (0.11, 0.04), (-0.08, -0.05), (-0.35, -0.27), (-0.11, -0.03), (0.08, 0.24), (0.25, 0.55), (0.36, 0.87), (0.38, 1.02), (0.34, 1.17), (0.25, 1.3), (0.05, 1.44), (-0.2, 1.5), (-0.35, 1.48), (-0.51, 1.42), (-0.64, 1.31), (-0.83, 1.02), (-0.97, 0.7), (-1.03, 0.35), (-1.03, -0.0), (-0.95, -0.39), (-0.82, -0.75), (-0.64, -1.11), (-0.4, -1.43), (-0.35, -1.5)]], 
    'grass': [[(-2.6, -0.57), (-2.7, -0.18)],
    [(-2.08, -0.46), (-2.16, 0.02)],
    [(-1.56, -0.38), (-1.62, 0.19)],
    [(-1.04, -0.32), (-1.08, 0.34)],
    [(-0.52, -0.28), (-0.54, 0.47)],
    [(0.0, -0.27), (0.0, 0.57)],
    [(0.52, -0.28), (0.54, 0.47)],
    [(1.04, -0.32), (1.08, 0.34)],
    [(1.56, -0.38), (1.62, 0.19)],
    [(2.08, -0.46), (2.16, 0.02)],
    [(2.6, -0.57), (2.7, -0.18)]]}


# --------------------------------------------------------------------------
# Reading the Stage 1 GML
# --------------------------------------------------------------------------

_MEMBER = re.compile(r"<osgb:(\w+Member)>.*?</osgb:\1>", re.S)
_FEATURE = re.compile(r"<osgb:(\w+)\s+fid=['\"]osgb(\d+)['\"]")
_COORDS = re.compile(r"<gml:coordinates>([^<]*)</gml:coordinates>")
_RING = re.compile(r"<gml:(outerBoundaryIs|innerBoundaryIs)>.*?</gml:\1>", re.S)


def _coords(text):
    pts = []
    for pair in text.split():
        x, _, y = pair.partition(",")
        pts.append((float(x), float(y.split(",")[0])))
    return pts


class Feature:
    def __init__(self, xml):
        m = _FEATURE.search(xml)
        self.cls, self.toid = m.group(1), m.group(2).zfill(16)
        self.xml = xml

    def all(self, tag):
        return [html.unescape(v) for v in
                re.findall(rf"<osgb:{tag}>([^<]*)</osgb:{tag}>", self.xml)]

    def one(self, tag, default=""):
        v = self.all(tag)
        return v[0] if v else default

    def joined(self, tag):
        return "; ".join(self.all(tag))

    def parts(self):
        return [_coords(c) for c in _COORDS.findall(self.xml)]

    def rings(self):
        return [_coords(_COORDS.search(r.group(0)).group(1))
                for r in _RING.finditer(self.xml)]

    def last_change(self):
        dates, reasons = self.all("changeDate"), self.all("reasonForChange")
        if not dates:
            return "", ""
        i = max(range(len(dates)), key=lambda k: dates[k])
        return dates[i], reasons[i] if i < len(reasons) else ""


def read_features(path):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for m in _MEMBER.finditer(text):
        if _FEATURE.search(m.group(0)):
            yield Feature(m.group(0))


# --------------------------------------------------------------------------
# MIF/MID writer
# --------------------------------------------------------------------------

class Table:
    def __init__(self, name, columns, index=""):
        self.name, self.columns, self.index = name, columns, index
        self.rows, self.objects, self.gdal_objects = [], [], []
        self.legends = {}

    def add(self, values, obj, gdal_obj=None, legend=None):
        self.rows.append(values)
        self.objects.append(obj)
        self.gdal_objects.append(gdal_obj or obj)
        if legend is not None:
            self.legends[legend] = self.legends.get(legend, 0) + 1

    def write(self, folder: Path, for_gdal=False):
        """for_gdal=True writes a temporary copy laid out for GDAL's reader."""
        stem = f"_gdal_{self.name}" if for_gdal else self.name
        objects = self.gdal_objects if for_gdal else self.objects
        mif = folder / f"{stem}.mif"
        with open(mif, "w", encoding="cp1252", errors="replace", newline="\r\n") as f:
            f.write('Version 450\nCharset "WindowsLatin1"\nDelimiter ","\n')
            if self.index:
                f.write(f"Index {self.index}\n")
            f.write(COORDSYS + "\n")
            f.write(f"Columns {len(self.columns)}\n")
            for col, typ in self.columns:
                f.write(f"  {col} {typ}\n")
            f.write("Data\n\n")
            for obj in objects:
                f.write(obj)
                f.write("\n")
        with open(folder / f"{stem}.mid", "w", encoding="cp1252",
                  errors="replace", newline="\r\n") as f:
            for row in self.rows:
                out = []
                for (col, typ), v in zip(self.columns, row):
                    if typ.startswith("Char"):
                        width = int(typ[typ.index("(") + 1:-1])
                        out.append('"' + str(v).replace('"', "'")[:width] + '"')
                    elif typ == "Date":
                        out.append(str(v).replace("-", "")[:8])
                    elif typ == "Logical":
                        out.append("T" if v else "F")
                    else:
                        out.append("" if v in ("", None) else str(v))
                f.write(",".join(out) + "\n")
        return mif


def fmt(x, y):
    return f"{x:.2f} {y:.2f}"


def obj_region(rings, pen_s, brush_s):
    lines = [f"Region {len(rings)}"]
    for ring in rings:
        lines.append(f"  {len(ring)}")
        lines.extend(fmt(*p) for p in ring)
    lines += [f"    {pen_s}", f"    {brush_s}"]
    return "\n".join(lines)


def obj_pline(parts, pen_s):
    parts = [p for p in parts if len(p) >= 2]
    if not parts:
        return "none"
    if len(parts) == 1:
        if len(parts[0]) == 2:
            (a, b), (c, d) = parts[0]
            return f"Line {a:.2f} {b:.2f} {c:.2f} {d:.2f}\n    {pen_s}"
        lines = [f"Pline {len(parts[0])}"]
        lines.extend(fmt(*p) for p in parts[0])
    else:
        lines = [f"Pline Multiple {len(parts)}"]
        for part in parts:
            lines.append(f"  {len(part)}")
            lines.extend(fmt(*p) for p in part)
    lines.append(f"    {pen_s}")
    return "\n".join(lines)


def split_parts(parts, limit=MAX_NODES):
    """Split a long list of line parts into chunks under the node limit."""
    chunk, n = [], 0
    for p in parts:
        if chunk and n + len(p) > limit:
            yield chunk
            chunk, n = [], 0
        chunk.append(p)
        n += len(p)
    if chunk:
        yield chunk


def place(template, x, y, angle_deg, scale=1.0):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [[(x + scale * (px * c - py * s), y + scale * (px * s + py * c))
             for px, py in part] for part in template]


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.unverified = {}

    def flag(self, table, what):
        key = (table, what)
        self.unverified[key] = self.unverified.get(key, 0) + 1


def classify_region(groups, terms, make, report):
    """Return (legend, pen, brush) for a TopographicArea."""
    g = set(groups)

    def known(legend):
        return (legend,) + REGION_STYLES[legend]

    def by_make():
        if make == "Multiple":
            return known("0000 Multiple surface (garden)")
        if make == "Manmade":
            return known("0000 Manmade surface or step")
        if make == "Natural":
            return known("0000 Natural surface")
        return known("0000 Unclassified (or broken)")

    if "Building" in g:
        return known("0321 Building")
    if "Glasshouse" in g:
        report.flag("MM_Region", "Glasshouse (styled as building)")
        return ("0000 Glasshouse",) + REGION_FALLBACK["build"]
    if "Inland Water" in g:
        return known("0400 Inland water")
    if "Tidal Water" in g:
        return known("0000 Foreshore" if "Foreshore" in terms else "0000 Tidal water")
    if "Natural Environment" in g:
        for term, legend in VEG_REGION_PRIORITY:
            if term in terms:
                return known(legend)
        for names, legend in NATURAL_SURFACES:
            if any(t in names for t in terms):
                return known(legend)
        name = terms[0] if terms else "Natural environment"
        report.flag("MM_Region", f"Natural Environment: {name}")
        return (f"0000 {name}"[:30],) + REGION_FALLBACK["veg"]
    if "Rail" in g:
        return known("0000 Railway")
    if "Road Or Track" in g:
        return known("0000 Track" if "Track" in terms else "0000 Road")
    if "Path" in g:
        return known("0000 Path")
    if "Structure" in g:
        return known("0000 Structure")
    if "Unclassified" in g:
        return known("0000 Unclassified (or broken)")
    if not g & {"General Surface", "Roadside"}:
        report.flag("MM_Region", f"{'; '.join(groups)} (styled by make: {make or 'none'})")
    return by_make()


def classify_detail(groups, terms, make, presence, report):
    """Return the pen for a TopographicLine."""
    g, t = set(groups), set(terms)
    if "Building" in g:
        return pen(1, 3 if presence == "Overhead" else 2, "#000000")
    if "Inland Water" in g and "General Feature" not in g:
        if "Culvert" in t:
            return pen(12, 2, "#999999")
        return pen(1, 2, "#3075ff")
    if "Tidal Water" in g:                      # mean high / low water
        return pen(1, 2, "#3075ff")
    if "Network Or Polygon Closing Geometry" in g:
        if "Inferred Property Closing Link" in t:
            return pen(1, 3, "#999999")
        return pen(12, 3, "#999999")
    if "Rail" in g:
        if "Tunnel Edge" in t:
            return pen(20, 4, "#999999")
        return pen(1, 2, "#666666")
    if "Path" in g and "Road Or Track" not in g:  # fine dotted grey, as OS
        return pen(1, 3, "#666666")
    if "Road Or Track" in g or "Roadside" in g:
        if "Roadside" in g and "Road Or Track" not in g:
            report.flag("MM_Detail", f"{'; '.join(groups)} (styled as road edge)")
        return pen(1, 5, "#000000")
    if "Landform" in g:                         # slopes and cliffs alike
        if t & {"Bottom Of Slope", "Bottom Of Cliff"}:
            return pen(1, 3, "#808080")
        return pen(1, 2, "#808080")
    if "Structure" in g:
        return pen(12, 2, "#808080")
    if "General Surface" in g:
        return pen(1, 5, "#666666")
    if "General Feature" in g:
        if "Overhead Construction" in t:
            return pen(1, 6, "#999999")
        if presence == "Edge / Limit":
            return pen(1, 4, "#000000")
        return pen(12, 2, "#000000")
    report.flag("MM_Detail", f"{'; '.join(groups)} (thin black line)")
    return pen(12, 2, "#000000")


# --------------------------------------------------------------------------
# Vegetation pattern
# --------------------------------------------------------------------------

def _inside(x, y, rings):
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def _hash(i, j, k=0):
    h = (i * 73856093) ^ (j * 19349663) ^ (k * 83492791)
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 0xFFFFFFFF


def tree_pattern(rings, terms, spacing=TREE_SPACING):
    """Scatter vegetation glyphs through a polygon on a jittered national
    lattice (so neighbouring polygons line up), cut at the polygon edge."""
    kinds = [VEG_GLYPHS[t] for t in terms if t in VEG_GLYPHS]
    if not kinds:
        return []
    mixed = len(kinds) > 1
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    out = []
    j0, j1 = int(min(ys) // spacing) - 1, int(max(ys) // spacing) + 1
    i0, i1 = int(min(xs) // spacing) - 1, int(max(xs) // spacing) + 1
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            glyph, scale, every = kinds[int(_hash(i, j, 1) * len(kinds)) % len(kinds)]
            if every > 1 and int(_hash(i, j, 2) * every) != 0:
                continue
            if mixed and glyph != "non":
                scale *= 0.5
            x = (i + 0.5 * (j % 2) + 0.6 * (_hash(i, j, 3) - 0.5)) * spacing
            y = (j + 0.6 * (_hash(i, j, 4) - 0.5)) * spacing
            r = 2.0 * scale
            corners = [(x, y), (x - r, y - r), (x + r, y - r), (x - r, y + r), (x + r, y + r)]
            hits = [_inside(cx, cy, rings) for cx, cy in corners]
            if not any(hits):
                continue
            parts = place(GLYPHS[glyph], x, y, 0, scale)
            if all(hits):
                out.extend(parts)
                continue
            for part in parts:                       # cut at the polygon edge
                run = []
                for p in part:
                    if _inside(p[0], p[1], rings):
                        run.append(p)
                    else:
                        if len(run) >= 2:
                            out.append(run)
                        run = []
                if len(run) >= 2:
                    out.append(run)
    return out


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def text_object(string, ax, ay, height, angle, anchor, rule):
    _legend, colour, font, style, factor = rule
    size = height * factor
    w = size * sum(CHAR_WIDTHS.get(ch, CHAR_DEFAULT) for ch in string)
    h = size * TEXT_BOX_HEIGHT
    u = (0.0, w / 2, w)[min(anchor // 3, 2)]
    v = (0.0, h / 2, h)[anchor % 3]
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    ox, oy = ax - (u * c - v * s), ay - (u * s + v * c)
    corners = [(ox + px * c - py * s, oy + px * s + py * c)
               for px, py in ((0, 0), (w, 0), (0, h), (w, h))]
    x0, x1 = min(p[0] for p in corners), max(p[0] for p in corners)
    y0, y1 = min(p[1] for p in corners), max(p[1] for p in corners)
    # GDAL reads the box height as the text height, even for rotated text,
    # so the copy used for TAB conversion keeps the box exactly one line high.
    if s > 0 and c > 0:
        g0 = oy
    elif s > 0:
        g0 = oy + h * c
    elif c < 0:
        g0 = oy - h
    else:
        g0 = oy + h * c - h
    safe = string.replace("\\", "/").replace('"', "'")

    def render(ya, yb):
        lines = [f'Text "{safe}"',
                 f"    {x0:.3f} {ya:.3f} {x1:.3f} {yb:.3f}",
                 f'    Font ("{font}",{style},0,{rgb(colour)})']
        if abs(angle) > 0.05:
            lines.append(f"    Angle {angle:g}")
        return "\n".join(lines)
    return render(y0, y1), render(g0, g0 + h)


# --------------------------------------------------------------------------
# Building the seven tables
# --------------------------------------------------------------------------

COMMON = [("TOID", "Char(16)"), ("VERSION", "Integer"), ("VER_DATE", "Date"),
          ("THEME", "Char(90)"), ("DESC_GROUP", "Char(50)"), ("DESC_TERM", "Char(125)")]
TAIL = [("LA_CH_DATE", "Date"), ("LA_CH_REAS", "Char(12)"), ("PROC_DATE", "Date"),
        ("CHUNK_ORIG", "Char(6)")]
L = [("LEGEND", "Char(30)")]


def make_tables():
    return {
        "MM_Region": Table("MM_Region", L + [("TOID", "Char(16)")], "1,2"),
        "MM_Detail": Table("MM_Detail", COMMON + [("MAKE", "Char(12)")]),
        "MM_Trees": Table("MM_Trees", L + [("TOID", "Char(16)")], "1"),
        "MM_Text": Table("MM_Text", [("TXT_STRING", "Char(90)")] + L + COMMON + [
            ("MAKE", "Char(12)"), ("FEAT_CODE", "Smallint"), ("FONT", "Smallint"),
            ("TXT_HEIGHT", "Decimal(5,2)"), ("ROTATION", "Decimal(5,1)"),
            ("ANCHOR_POS", "Smallint"), ("EASTING", "Float"), ("NORTHING", "Float")] + TAIL, "1,2"),
        "MM_Symbol": Table("MM_Symbol", L + COMMON + [
            ("PHYS_PRES", "Char(12)"), ("FEAT_CODE", "Smallint"), ("PHYS_LEVEL", "Smallint"),
            ("ROTATION", "Decimal(5,1)"), ("REF_TO_FEA", "Char(16)"),
            ("EASTING", "Float"), ("NORTHING", "Float")] + TAIL, "1"),
        "MM_Point": Table("MM_Point", L + COMMON + [
            ("MAKE", "Char(12)"), ("FEAT_CODE", "Smallint"), ("ACC_OF_POS", "Char(7)"),
            ("HT_AB_DATM", "Float"), ("EASTING", "Float"), ("NORTHING", "Float")] + TAIL, "1"),
        "MM_Parish": Table("MM_Parish", L + COMMON + [
            ("FEAT_CODE", "Smallint"), ("ACC_OF_POS", "Char(7)"), ("BROKEN", "Logical")] + TAIL, "1"),
    }


def build(features, chunk="", proc_date=None):
    proc_date = proc_date or datetime.date.today().isoformat()
    tables, report = make_tables(), Report()
    for f in features:
        groups, terms = f.all("descriptiveGroup"), f.all("descriptiveTerm")
        make = f.one("make")
        code = int(f.one("featureCode", "0") or 0)
        common = [f.toid, f.one("version"), f.one("versionDate"), f.joined("theme"),
                  "; ".join(groups), "; ".join(terms)]
        ch_date, ch_reason = f.last_change()
        tail = [ch_date, ch_reason, proc_date, chunk]

        if f.cls == "TopographicArea":
            rings = f.rings()
            if not rings:
                continue
            # Slopes and pylons overlap the land polygons beneath them;
            # InterpOSe leaves them out of MM_Region (their edges stay in MM_Detail).
            if "Landform" in groups or "Pylon" in terms:
                continue
            legend, pen_s, brush_s = classify_region(groups, terms, make, report)
            tables["MM_Region"].add([legend, f.toid], obj_region(rings, pen_s, brush_s), legend=legend)
            if "Natural Environment" in groups:
                parts = tree_pattern(rings, terms)
                if parts:
                    key = frozenset(t for t in terms if t in VEG_GLYPHS)
                    tlegend = TREE_LEGENDS.get(key)
                    if not tlegend:
                        # fixed order, so the same mix always gets the same name
                        tlegend = ("0000 " + " & ".join(TREE_ABBREV[t] for t in TREE_ABBREV
                                                         if t in key))[:30]
                        tlegend = tlegend[:5] + tlegend[5:6].upper() + tlegend[6:]
                        report.flag("MM_Trees", tlegend)
                    colour = "#808080" if tlegend.startswith("0385") else "#669966"
                    tpen = pen(12 if tlegend in TREE_THIN else 1, 2, colour)
                    for chunk_parts in split_parts(parts):
                        tables["MM_Trees"].add([tlegend, f.toid],
                                               obj_pline(chunk_parts, tpen), legend=tlegend)

        elif f.cls == "TopographicLine":
            presence = f.one("physicalPresence")
            pen_s = classify_detail(groups, terms, make, presence, report)
            for chunk_parts in split_parts(f.parts()):
                tables["MM_Detail"].add(common + [make], obj_pline(chunk_parts, pen_s))

        elif f.cls == "BoundaryLine":
            if "Parish" in terms:
                legend = "0007 Civil parish or community"
            else:
                legend = ("0000 " + (terms[0] if terms else "Boundary"))[:30]
                report.flag("MM_Parish", legend)
            broken = f.one("broken").lower() == "true"
            tables["MM_Parish"].add(
                [legend] + common + [code, f.one("accuracyOfPosition"), broken] + tail,
                obj_pline(f.parts(), pen(1, 3, "#000000")), legend=legend)

        elif f.cls == "TopographicPoint":
            pts = f.parts()
            if not pts:
                continue
            x, y = pts[0][0]
            height = f.one("heightAboveDatum")
            if code == 10197 or "Spot Height" in terms:
                legend = "0027 Spot height"
                obj = f"Point {x:.2f} {y:.2f}\n    Symbol (50,{rgb('#808080')},7)"
            else:
                if set(groups) == {"Inland Water"}:
                    legend, p = "0057 Water location", pen(1, 2, "#0066cc")
                else:
                    legend, p = "0057 General / Unclass'd point", pen(20, 2, "#666666")
                obj = obj_region([_circle(x, y, 0.1)], p, "Brush (1,0,16777215)")
            tables["MM_Point"].add(
                [legend] + common + [make, code, f.one("accuracyOfPosition"), height,
                                     f"{x:.2f}", f"{y:.2f}"] + tail, obj, legend=legend)

        elif f.cls == "CartographicSymbol":
            pts = f.parts()
            if not pts:
                continue
            x, y = pts[0][0]
            angle = float(f.one("orientation", "0") or 0) / 10.0
            if code in SYMBOLS:
                legend, pen_s, template = SYMBOLS[code]
            else:
                legend = ("0000 " + (terms[0] if terms else "Symbol"))[:30]
                pen_s, template = pen(1, 2, "#000000"), SYMBOL_FALLBACK
                report.flag("MM_Symbol", f"{legend} (code {code}, drawn as a cross)")
            ref = re.search(r"referenceToFeature[^>]*href=['\"]#?osgb(\d+)", f.xml)
            tables["MM_Symbol"].add(
                [legend] + common + [f.one("physicalPresence"), code,
                                     f.one("physicalLevel"), f"{angle:.1f}",
                                     ref.group(1).zfill(16) if ref else "",
                                     f"{x:.2f}", f"{y:.2f}"] + tail,
                obj_pline(place(template, x, y, angle), pen_s), legend=legend)

        elif f.cls == "CartographicText":
            pts = f.parts()
            string = f.one("textString")
            if not pts or not string:
                continue
            x, y = pts[0][0]
            rule = next((TEXT_RULES[g] for g in groups if g in TEXT_RULES), None)
            if rule is None:
                name = groups[0] if groups else "Other"
                rule = (f"1009 {name} text"[:30], "#333333", "Century Gothic", 0, 1.15)
                report.flag("MM_Text", rule[0])
            height = float(f.one("height", "1") or 1)
            angle = float(f.one("orientation", "0") or 0) / 10.0
            anchor = int(f.one("anchorPosition", "0") or 0)
            tables["MM_Text"].add(
                [string, rule[0]] + common + [make, code, f.one("font", "0"),
                                              f"{height:.2f}", f"{angle:.1f}", anchor,
                                              f"{x:.2f}", f"{y:.2f}"] + tail,
                *text_object(string, x, y, height, angle, anchor, rule), legend=rule[0])
    return tables, report


# --------------------------------------------------------------------------
# MIF -> TAB conversion (optional, needs GDAL in one form or another)
# --------------------------------------------------------------------------

_GDAL_ARGS = ["-f", "MapInfo File", "-a_srs", "EPSG:27700",
              "-lco", "BOUNDS=0,0,2000000,2000000", "-lco", "ENCODING=CP1252"]


def _bundled_gdal():
    """GDAL library shipped inside the pyogrio package, driven directly."""
    import ctypes
    import glob
    import pyogrio
    base = os.path.dirname(pyogrio.__file__)
    libs = (glob.glob(os.path.join(base, "..", "pyogrio.libs", "*gdal*")) +
            glob.glob(os.path.join(base, "*gdal*.dll")) +
            glob.glob(os.path.join(base, ".dylibs", "*gdal*")))
    if not libs:
        raise ImportError("no bundled GDAL library found")
    lib = ctypes.CDLL(libs[0])
    vp, cp = ctypes.c_void_p, ctypes.c_char_p
    lib.GDALOpenEx.restype = vp
    lib.GDALOpenEx.argtypes = [cp, ctypes.c_uint, vp, vp, vp]
    lib.GDALVectorTranslateOptionsNew.restype = vp
    lib.GDALVectorTranslateOptionsNew.argtypes = [vp, vp]
    lib.GDALVectorTranslate.restype = vp
    lib.GDALVectorTranslate.argtypes = [cp, vp, ctypes.c_int, vp, vp, vp]
    lib.GDALClose.argtypes = [vp]
    lib.GDALAllRegister()

    def translate(src, dst):
        argv = (cp * (len(_GDAL_ARGS) + 1))(*[a.encode() for a in _GDAL_ARGS], None)
        opts = lib.GDALVectorTranslateOptionsNew(argv, None)
        ds = lib.GDALOpenEx(str(src).encode(), 4, None, None, None)
        out = lib.GDALVectorTranslate(str(dst).encode(), None, 1, (vp * 1)(ds), opts, None)
        ok = bool(out)
        if out:
            lib.GDALClose(out)
        lib.GDALClose(ds)
        return ok
    return translate


def get_converter():
    exe = shutil.which("ogr2ogr")
    if exe:
        def run(src, dst):
            return subprocess.run([exe, *_GDAL_ARGS, str(dst), str(src)],
                                  capture_output=True).returncode == 0
        return "ogr2ogr", run
    try:
        from osgeo import gdal

        def run(src, dst):
            return gdal.VectorTranslate(str(dst), str(src),
                                        options=" ".join(
                                            f'"{a}"' if " " in a else a for a in _GDAL_ARGS)) is not None
        return "osgeo.gdal", run
    except ImportError:
        pass
    try:
        return "pyogrio's GDAL", _bundled_gdal()
    except Exception:
        return None, None


def make_mapinfo85_safe(tab: Path):
    """Force the header settings MapInfo 8.5 expects."""
    text = tab.read_text(encoding="cp1252", errors="replace")
    text = text.replace("Neutral", "WindowsLatin1")
    m = re.search(r"!version (\d+)", text)
    if m and int(m.group(1)) > 450:
        return False
    tab.write_text(text, encoding="cp1252", errors="replace", newline="")
    return True


# --------------------------------------------------------------------------
# Running Stage 2
# --------------------------------------------------------------------------

def style_site(source, output=None, make_tab=True, keep_mif=True, log=print):
    source = Path(source)
    gml = source / "site.gml.gz" if source.is_dir() else source
    if not gml.exists():
        sys.exit(f"Cannot find {gml}")
    out = Path(output) if output else gml.parent
    out.mkdir(parents=True, exist_ok=True)

    chunk = ""
    manifest = gml.parent / "manifest.json"
    if manifest.exists():
        files = json.loads(manifest.read_text()).get("source_files", [])
        orders = {os.path.basename(p.replace("\\", "/"))[:6] for p in files}
        chunk = orders.pop() if len(orders) == 1 else ""

    tables, report = build(read_features(gml), chunk)

    converter_name, convert = get_converter() if make_tab else (None, None)
    log(f"Writing tables to {out}")
    lines = []
    for name, table in tables.items():
        mif = table.write(out)
        status = "MIF"
        if convert:
            tab = out / f"{name}.tab"
            for ext in (".tab", ".dat", ".map", ".id", ".ind"):
                (out / f"{name}{ext}").unlink(missing_ok=True)
            differs = table.gdal_objects != table.objects
            src = table.write(out, for_gdal=True) if differs else mif
            ok = convert(src, tab) and tab.exists() and make_mapinfo85_safe(tab)
            if differs:
                src.unlink()
                src.with_suffix(".mid").unlink()
            if ok:
                status = "TAB"
                if not keep_mif:
                    mif.unlink()
                    (out / f"{name}.mid").unlink()
            else:
                status = "MIF (TAB conversion failed)"
        line = f"  {name:<10} {len(table.rows):>6} objects   {status}"
        log(line)
        lines.append(line)
        for legend, n in sorted(table.legends.items()):
            lines.append(f"      {n:>6}  {legend}")
    if make_tab and not convert:
        log("  GDAL not found: MIF/MID written only. In MapInfo use Table > Import.")
    elif convert:
        log(f"  TAB files made with {converter_name}")

    lines.append("")
    if report.unverified:
        lines.append("Classes not present in the InterpOSe sample (default style used):")
        for (table, what), n in sorted(report.unverified.items()):
            lines.append(f"  {table:<10} {n:>6}  {what}")
        log(f"  {len(report.unverified)} class(es) used a default style: see style_report.txt")
    else:
        lines.append("Every feature matched a defined style.")
    (out / "style_report.txt").write_text("\n".join(lines) + "\n")
    return tables, report


def main(argv=None):
    p = argparse.ArgumentParser(description="Stage 2: style a Stage 1 site into MM_* MapInfo tables.")
    p.add_argument("site", help="Stage 1 site folder, or the path to site.gml.gz")
    p.add_argument("--output", help="folder for the tables (default: the site folder)")
    p.add_argument("--no-tab", action="store_true", help="write MIF/MID only")
    p.add_argument("--no-mif", action="store_true", help="delete MIF/MID after a successful TAB conversion")
    a = p.parse_args(argv)
    style_site(a.site, a.output, make_tab=not a.no_tab, keep_mif=not a.no_mif)


if __name__ == "__main__":
    main()
