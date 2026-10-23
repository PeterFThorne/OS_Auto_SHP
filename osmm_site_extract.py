#!/usr/bin/env python3
"""
osmm_site_extract.py  --  STAGE 1 of the OS MasterMap -> MapInfo pipeline

Selects every OS MasterMap Topography feature that touches a site box and
writes them, unmodified, to a single small GML file for Stage 2 (classification
and styling into the MM_* MapInfo tables).

    centre grid reference + box width/height
        -> which tiles are needed (worked out from the grid, anywhere in GB)
        -> stream those .gz tiles, keep whole features that touch the box
        -> de-duplicate features that appear in more than one tile
        -> <output>/<site name>/site.gml.gz  +  manifest.json

Only the Python standard library is required. Cloud locations (s3://, az://,
gs:// ...) for the data or the output work if the optional `fsspec` package
(plus the matching driver, e.g. s3fs) is installed.

EXAMPLES
    python osmm_site_extract.py --centre "ST 2045 0139" --width 2500 --height 2000
    python osmm_site_extract.py --centre 320450,101390 --width 2500 --height 2000 ^
           --data "M:\\2500MMdata" --output "D:\\OSMM_sites" --name CotleighCross
    python osmm_site_extract.py --job job.json
    python osmm_site_extract.py            (no arguments: prompts for the inputs)

STRUCTURE (so the pieces can be swapped later)
    SiteRequest / Config   plain data: what to extract, and where things live
    Storage                all file access (local folder or cloud URL)
    TileIndex              finds which .gz file holds which tile
    extract_site()         the engine; no printing of prompts, no CLI knowledge
    main()                 the command-line interface, a thin wrapper

A different front end (GUI form, web page, MapBasic tool, batch list of sites)
only needs to build a SiteRequest and call extract_site(request, config).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration and request
# --------------------------------------------------------------------------

@dataclass
class Config:
    """Where things live and how the tile files are named. Any value can be
    set in a JSON config file (--config) and overridden on the command line."""
    data_root: str = r"M:\2500MMdata"      # folder or URL holding the .gz tiles
    output_root: str = "osmm_sites"        # folder or URL for results
    tile_size_m: int = 5000                # tiles are 5 km squares (ST1500 ...)
    # Tile name is taken from the file name; group 1 must be the tile name.
    filename_regex: str = r"(?:^|[-_])([A-Z]{2}\d{4})\.gz$"
    index_cache: str = "osmm_tile_index.json"   # saved list of tile files
    # If a tile has several files (different orders/dates): "newest" or "all"
    duplicates: str = "newest"
    default_width_m: float = 1000.0
    default_height_m: float = 1000.0


@dataclass
class SiteRequest:
    centre: str                 # grid ref "ST 2045 0139" or "320450,101390"
    width_m: float
    height_m: float
    name: str = ""              # output sub-folder name; auto if blank

    def box(self):
        e, n = parse_grid_reference(self.centre)
        hw, hh = self.width_m / 2.0, self.height_m / 2.0
        return (e - hw, n - hh, e + hw, n + hh)

    def site_name(self):
        if self.name:
            return re.sub(r"[^\w\-]+", "_", self.name)
        e, n = parse_grid_reference(self.centre)
        return f"{int(e)}_{int(n)}_{int(self.width_m)}x{int(self.height_m)}"


# --------------------------------------------------------------------------
# British National Grid helpers (valid for the whole of Great Britain)
# --------------------------------------------------------------------------

_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"   # no I


def letters_to_origin(letters: str):
    """100 km square letters -> (easting, northing) of its SW corner."""
    l1, l2 = _LETTERS.index(letters[0]), _LETTERS.index(letters[1])
    e = ((l1 - 2) % 5) * 5 + (l2 % 5)
    n = (19 - (l1 // 5) * 5) - (l2 // 5)
    return e * 100000, n * 100000


def origin_to_letters(e: float, n: float) -> str:
    """Easting/northing -> the 100 km square letters containing the point."""
    e100, n100 = int(e // 100000), int(n // 100000)
    if not (0 <= e100 <= 6 and 0 <= n100 <= 12):
        raise ValueError(f"({e}, {n}) is outside the British National Grid")
    l1 = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    l2 = ((19 - n100) * 5) % 25 + e100 % 5
    return _LETTERS[l1] + _LETTERS[l2]


def parse_grid_reference(text: str):
    """Accepts 'ST 2045 0139', 'ST20450139', 'ST204013' or '320450,101390'
    (also space separated). Returns (easting, northing) in metres.
    A lettered reference gives the SW corner of the square it names."""
    s = text.strip().upper()
    m = re.fullmatch(r"([A-Z]{2})\s*(\d*)\s*(\d*)", s)
    if m:
        digits = m.group(2) + m.group(3)
        if len(digits) % 2:
            raise ValueError(f"Grid reference '{text}' has an odd number of digits")
        half = len(digits) // 2
        if half > 5:
            raise ValueError(f"Grid reference '{text}' has too many digits")
        scale = 10 ** (5 - half)
        e0, n0 = letters_to_origin(m.group(1))
        de = int(digits[:half]) * scale if half else 0
        dn = int(digits[half:]) * scale if half else 0
        return float(e0 + de), float(n0 + dn)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    raise ValueError(f"Cannot read '{text}' as a grid reference or easting,northing")


def tile_name(e: float, n: float, size: int) -> str:
    """Name of the tile containing a point, e.g. (317000, 102000) -> ST1500."""
    letters = origin_to_letters(e, n)
    e0, n0 = letters_to_origin(letters)
    ek = int((e - e0) // size) * size // 1000
    nk = int((n - n0) // size) * size // 1000
    return f"{letters}{ek:02d}{nk:02d}"


def tiles_for_box(box, size: int):
    x0, y0, x1, y1 = box
    names = []
    e = math.floor(x0 / size) * size
    while e <= x1:
        n = math.floor(y0 / size) * size
        while n <= y1:
            names.append(tile_name(e, n, size))
            n += size
        e += size
    return names


# --------------------------------------------------------------------------
# Storage: local folders, or cloud URLs through fsspec
# --------------------------------------------------------------------------

class Storage:
    """All file access goes through here, so a location can be a normal
    folder (M:\\..., \\\\server\\share\\...) or a URL such as s3://bucket/path."""

    def __init__(self, root: str):
        self.root = root
        self.remote = "://" in root
        if self.remote:
            try:
                import fsspec
            except ImportError:
                sys.exit(f"'{root}' is a cloud location: install fsspec "
                         f"(pip install fsspec and the driver, e.g. s3fs).")
            self.fs, self.base = fsspec.core.url_to_fs(root)

    def join(self, *parts):
        if self.remote:
            return "/".join([self.base.rstrip("/"), *parts])
        return str(Path(self.root, *parts))

    def open(self, path, mode="rb"):
        if self.remote:
            return self.fs.open(path, mode)
        if "w" in mode:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return open(path, mode)

    def exists(self, path):
        return self.fs.exists(path) if self.remote else Path(path).exists()

    def walk_files(self, suffix=".gz"):
        """Yield (path, modified_time) for every file under the root."""
        if self.remote:
            for path, info in self.fs.find(self.base, detail=True).items():
                if path.lower().endswith(suffix):
                    mt = info.get("mtime") or info.get("LastModified") or 0
                    yield path, getattr(mt, "timestamp", lambda: mt)()
        else:
            for dirpath, _dirs, files in os.walk(self.root):
                for f in files:
                    if f.lower().endswith(suffix):
                        p = os.path.join(dirpath, f)
                        yield p, os.path.getmtime(p)


# --------------------------------------------------------------------------
# Tile index: which file holds which tile
# --------------------------------------------------------------------------

class TileIndex:
    """Scans the data location once and remembers where each tile is, so a
    country-wide archive is not re-scanned on every run. Use --reindex after
    new data has been added."""

    def __init__(self, config: Config, data: Storage, rebuild=False, log=print):
        self.config, self.data = config, data
        cache = Path(config.index_cache)
        self.tiles = {}
        if cache.exists() and not rebuild:
            saved = json.loads(cache.read_text())
            if saved.get("data_root") == config.data_root:
                self.tiles = saved["tiles"]
        if not self.tiles:
            log(f"Indexing tiles under {config.data_root} ...")
            pattern = re.compile(config.filename_regex, re.I)
            for path, mtime in data.walk_files():
                m = pattern.search(os.path.basename(path.replace("\\", "/")))
                if m:
                    self.tiles.setdefault(m.group(1).upper(), []).append([path, mtime])
            log(f"  {sum(len(v) for v in self.tiles.values())} files, "
                f"{len(self.tiles)} tiles")
            try:
                cache.write_text(json.dumps(
                    {"data_root": config.data_root, "tiles": self.tiles}))
            except OSError:
                pass

    def files_for(self, tile: str):
        found = sorted(self.tiles.get(tile, []), key=lambda r: r[1], reverse=True)
        if self.config.duplicates == "newest":
            found = found[:1]
        return [p for p, _ in found]


# --------------------------------------------------------------------------
# Geometry test: does a feature touch the box?  (no clipping is done)
# --------------------------------------------------------------------------

_COORDS = re.compile(r"<gml:coordinates>([^<]*)</gml:coordinates>")


def _parse_parts(xml: str):
    parts = []
    for block in _COORDS.findall(xml):
        pts = []
        for pair in block.split():
            x, _, y = pair.partition(",")
            pts.append((float(x), float(y.split(",")[0])))
        if pts:
            parts.append(pts)
    return parts


def _segment_hits_box(x0, y0, x1, y1, box):
    """Liang-Barsky: True if the segment passes through the box."""
    bx0, by0, bx1, by1 = box
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - bx0), (dx, bx1 - x0), (-dy, y0 - by0), (dy, by1 - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return True


def _point_in_rings(x, y, rings):
    """Even-odd rule over all rings, so holes are handled automatically."""
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


def touches_box(xml: str, box, is_area: bool) -> bool:
    parts = _parse_parts(xml)
    if not parts:
        return False
    bx0, by0, bx1, by1 = box
    xs = [p[0] for part in parts for p in part]
    ys = [p[1] for part in parts for p in part]
    if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
        return False                                   # nowhere near
    if min(xs) >= bx0 and max(xs) <= bx1 and min(ys) >= by0 and max(ys) <= by1:
        return True                                    # wholly inside
    for part in parts:
        if len(part) == 1:
            x, y = part[0]
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                return True
        for (xa, ya), (xb, yb) in zip(part, part[1:]):
            if _segment_hits_box(xa, ya, xb, yb, box):
                return True
    # No edge crosses the box: an area may still completely surround it.
    return is_area and _point_in_rings(bx0, by0, parts)


# --------------------------------------------------------------------------
# Streaming reader for one OS MasterMap GML .gz tile
# --------------------------------------------------------------------------

_MEMBER = re.compile(r"<osgb:(\w+Member)>.*?</osgb:\1>", re.S)
_FEATURE = re.compile(r"<osgb:(\w+)\s+fid=['\"]([^'\"]+)['\"]")
_VERSION = re.compile(r"<osgb:version>(\d+)</osgb:version>")


def read_members(stream, chunk_chars=4_000_000):
    """Yield ('header', text) once, then ('member', xml) for each feature.
    Reads the compressed file in pieces so a 100 MB+ tile never has to be
    held in memory."""
    with gzip.open(stream, "rt", encoding="utf-8", errors="replace") as fh:
        buffer, header_done = "", False
        while True:
            chunk = fh.read(chunk_chars)
            buffer += chunk
            last = 0
            for m in _MEMBER.finditer(buffer):
                if not header_done:
                    yield "header", buffer[:m.start()]
                    header_done = True
                yield "member", m.group(0)
                last = m.end()
            buffer = buffer[last:]
            if not chunk:
                break


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

def extract_site(request: SiteRequest, config: Config, reindex=False, log=print):
    """Run Stage 1 for one site. Returns the manifest (a dict)."""
    started = time.time()
    box = request.box()
    data = Storage(config.data_root)
    out = Storage(config.output_root)
    index = TileIndex(config, data, rebuild=reindex, log=log)

    wanted = tiles_for_box(box, config.tile_size_m)
    log(f"Site box  E {box[0]:.0f}-{box[2]:.0f}  N {box[1]:.0f}-{box[3]:.0f}")
    log(f"Tiles needed: {', '.join(wanted)}")

    kept = {}            # fid -> (version, feature class, xml)
    header = None
    used, missing = [], []
    for tile in wanted:
        paths = index.files_for(tile)
        if not paths:
            missing.append(tile)
            log(f"  WARNING: no file found for tile {tile}")
            continue
        for path in paths:
            log(f"  reading {path}")
            used.append(path)
            read = 0
            with data.open(path, "rb") as raw:
                for kind, text in read_members(raw):
                    if kind == "header":
                        header = header or text
                        continue
                    read += 1
                    f = _FEATURE.search(text)
                    if not f:
                        continue
                    cls, fid = f.group(1), f.group(2)
                    if not touches_box(text, box, cls == "TopographicArea"):
                        continue
                    v = _VERSION.search(text)
                    version = int(v.group(1)) if v else 0
                    if fid not in kept or version > kept[fid][0]:
                        kept[fid] = (version, cls, text)
            log(f"    {read} features read, {len(kept)} kept so far")

    counts = {}
    for _v, cls, _x in kept.values():
        counts[cls] = counts.get(cls, 0) + 1

    folder = request.site_name()
    gml_path = out.join(folder, "site.gml.gz")
    manifest = {
        "site": folder,
        "centre": request.centre,
        "centre_en": parse_grid_reference(request.centre),
        "width_m": request.width_m,
        "height_m": request.height_m,
        "box": box,
        "tiles_needed": wanted,
        "tiles_missing": missing,
        "source_files": used,
        "feature_counts": counts,
        "features_total": len(kept),
        "output": gml_path,
        "complete": not missing,
    }

    if kept:
        payload = io.BytesIO()
        with gzip.open(payload, "wt", encoding="utf-8") as gz:
            gz.write(header or "<?xml version='1.0' encoding='UTF-8'?>\n"
                               "<osgb:FeatureCollection>\n")
            for _v, _cls, xml in kept.values():
                gz.write(xml)
                gz.write("\n")
            gz.write("</osgb:FeatureCollection>\n")
        with out.open(gml_path, "wb") as fh:
            fh.write(payload.getvalue())
    with out.open(out.join(folder, "manifest.json"), "wb") as fh:
        fh.write(json.dumps(manifest, indent=2).encode("utf-8"))

    log(f"\n{len(kept)} features written to {gml_path}")
    for cls in sorted(counts):
        log(f"  {cls:<20} {counts[cls]}")
    if missing:
        log(f"INCOMPLETE: no data for {', '.join(missing)}")
    log(f"Done in {time.time() - started:.1f} s")
    return manifest


# --------------------------------------------------------------------------
# Command-line interface (replaceable front end)
# --------------------------------------------------------------------------

def load_config(path, overrides):
    config = Config()
    if path:
        for key, value in json.loads(Path(path).read_text()).items():
            if not hasattr(config, key):
                sys.exit(f"Unknown setting '{key}' in {path}")
            setattr(config, key, value)
    for key, value in overrides.items():
        if value is not None:
            setattr(config, key, value)
    return config


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Stage 1: extract the OS MasterMap features for a site box.")
    p.add_argument("--centre", help='grid ref "ST 2045 0139" or "320450,101390"')
    p.add_argument("--width", type=float, help="box width in metres (east-west)")
    p.add_argument("--height", type=float, help="box height in metres (north-south)")
    p.add_argument("--name", default="", help="name for the output sub-folder")
    p.add_argument("--job", help="JSON file with centre/width_m/height_m/name, "
                                 "or a list of them for several sites")
    p.add_argument("--config", help="JSON settings file (see Config in the script)")
    p.add_argument("--data", help="folder or URL holding the .gz tiles")
    p.add_argument("--output", help="folder or URL for the results")
    p.add_argument("--reindex", action="store_true",
                   help="re-scan the data location (after adding new tiles)")
    a = p.parse_args(argv)

    config = load_config(a.config, {"data_root": a.data, "output_root": a.output})

    if a.job:
        jobs = json.loads(Path(a.job).read_text())
        jobs = jobs if isinstance(jobs, list) else [jobs]
        requests = [SiteRequest(**j) for j in jobs]
    else:
        centre = a.centre or input("Centre grid reference: ")
        width = a.width or float(
            input(f"Box width in metres [{config.default_width_m:.0f}]: ")
            or config.default_width_m)
        height = a.height or float(
            input(f"Box height in metres [{config.default_height_m:.0f}]: ")
            or config.default_height_m)
        requests = [SiteRequest(centre, width, height, a.name)]

    reindex = a.reindex
    for request in requests:
        extract_site(request, config, reindex=reindex)
        reindex = False


if __name__ == "__main__":
    main()
