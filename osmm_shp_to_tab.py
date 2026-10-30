#!/usr/bin/env python3
"""
osmm_shp_to_tab.py  --  convert input shapefile(s) to MapInfo TAB

Part of the v2 (shapefile-driven) pipeline. The v2 GUI runs this after
stage 1/stage 2 so the site boundary shapefile sits in the site folder as a
MapInfo table, alongside the MM_* tables.

    python osmm_shp_to_tab.py <file.shp> [<file.shp> ...] --output <folder>
                              [--no-style]

The output opens in MapInfo Professional 8.5:
  * Uses the same GDAL settings as stage 2 (EPSG:27700, BNG bounds, CP1252).
  * Large-integer fields (10+ digit numbers), which 8.5 cannot open, are
    written as Decimal.
  * The header is checked and fixed with stage 2's make_mapinfo85_safe().

Styling (unless --no-style): red 2 px outline with no fill for polygons,
red 2 px line for polylines, red circle for points, so the boundary is
visible over the MasterMap without hiding it.

GDAL is found the same way as stage 2: ogr2ogr on the PATH, then the osgeo
Python module, then the copy of GDAL bundled with pyogrio.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osmm_style_mapinfo as stage2  # noqa: E402

STYLES = {
    "polygon": "PEN(c:#FF0000,w:2px)",
    "line": "PEN(c:#FF0000,w:2px)",
    "point": 'SYMBOL(c:#FF0000,s:12pt,id:"mapinfo-sym-34")',
}
SHAPE_KIND = {1: "point", 11: "point", 21: "point", 8: "point", 18: "point",
              28: "point", 3: "line", 13: "line", 23: "line", 5: "polygon",
              15: "polygon", 25: "polygon", 31: "polygon"}
EXTRA_ARGS = ["-mapFieldType", "Integer64=Real", "-skipfailures"]


# --------------------------------------------------------------------------
# Small readers for the shapefile parts we need (standard library only)
# --------------------------------------------------------------------------

def _sidecar(shp: Path, ext: str):
    """The .dbf/.shx/... next to a .shp, whatever its letter case."""
    p = shp.with_suffix(ext)
    if p.exists():
        return p
    for q in shp.parent.glob(shp.stem + ".*"):
        if q.suffix.lower() == ext:
            return q
    return None


def shape_type(shp: Path) -> int:
    with open(shp, "rb") as fh:
        head = fh.read(36)
    if len(head) < 36 or struct.unpack_from(">i", head, 0)[0] != 9994:
        raise ValueError("not a valid .shp file")
    return struct.unpack_from("<i", head, 32)[0]


def dbf_info(shp: Path):
    """(record count, [field names]) from the .dbf header."""
    dbf = _sidecar(shp, ".dbf")
    if dbf is None:
        raise ValueError("the .dbf file is missing")
    with open(dbf, "rb") as fh:
        head = fh.read(32)
        count = struct.unpack_from("<I", head, 4)[0]
        header_len = struct.unpack_from("<H", head, 8)[0]
        names = []
        while fh.tell() < header_len - 1:
            desc = fh.read(32)
            if not desc or desc[0] == 0x0D:
                break
            names.append(desc[:11].split(b"\0")[0].decode("latin-1").strip())
    return count, names


def tab_record_count(tab: Path):
    """Number of rows in a MapInfo native table (.dat is dBASE-style)."""
    dat = tab.with_suffix(".dat")
    if not dat.exists():
        dat = next((p for p in tab.parent.glob(tab.stem + ".*")
                    if p.suffix.lower() == ".dat"), None)
    if dat is None:
        return None
    with open(dat, "rb") as fh:
        return struct.unpack_from("<I", fh.read(8), 4)[0]


# --------------------------------------------------------------------------
# GDAL: same search order as stage 2, but taking per-file arguments
# --------------------------------------------------------------------------

def get_translator(log=print):
    """Return (name, run) where run(args, src, dst) -> (ok, error text)."""
    exe = shutil.which("ogr2ogr")
    if exe:
        def run(args, src, dst):
            r = subprocess.run([exe, *args, str(dst), str(src)],
                               capture_output=True, text=True)
            return r.returncode == 0, (r.stderr or "").strip()
        return "ogr2ogr", run
    reasons = []
    try:
        from osgeo import gdal
        gdal.UseExceptions()

        def run(args, src, dst):
            try:
                ds = gdal.VectorTranslate(str(dst), str(src), options=list(args))
                ok = ds is not None
                ds = None
                return ok, ""
            except Exception as exc:          # noqa: BLE001
                return False, str(exc)
        return "osgeo.gdal", run
    except ImportError as exc:
        reasons.append(f"osgeo: {exc}")
    try:
        translate = stage2._bundled_gdal()

        def run(args, src, dst):
            # The bundled route reads its options from stage 2's list.
            saved = stage2._GDAL_ARGS
            stage2._GDAL_ARGS = list(args)
            try:
                return translate(src, dst), translate.last_error
            finally:
                stage2._GDAL_ARGS = saved
        return "pyogrio's GDAL", run
    except Exception as exc:                  # noqa: BLE001
        reasons.append(f"pyogrio: {type(exc).__name__}: {exc}")
    for r in reasons:
        log(f"    {r}")
    return None, None


def build_args(shp: Path, style=True):
    args = list(stage2._GDAL_ARGS) + EXTRA_ARGS
    if not style:
        return args
    kind = SHAPE_KIND.get(shape_type(shp))
    if not kind:
        return args
    _count, fields = dbf_info(shp)
    layer = shp.stem.replace('"', '""')
    css = STYLES[kind].replace("'", "''")
    args += ["-sql", f"SELECT *, CAST('{css}' AS character(120)) AS OGR_STYLE "
                     f'FROM "{layer}"']
    if fields:      # keep the original columns only (drop OGR_STYLE)
        args += ["-select", ",".join(fields)]
    return args


def unique_tab_path(folder: Path, stem: str, taken: set):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem) or "boundary"
    name, i = safe, 2
    while name.lower() in taken or name.lower().startswith("mm_"):
        name = f"{safe}_{i}"
        i += 1
    taken.add(name.lower())
    return folder / f"{name}.tab"


def convert_shapefiles(shapefiles, output, style=True, log=print):
    """Convert each shapefile to <output>/<name>.tab. Returns the number that
    failed (0 = all good)."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name, run = get_translator(log)
    if not run:
        log("  ERROR: GDAL not found, so the shapefile(s) could not be converted "
            "to TAB. (Stage 2 uses the same GDAL; see its log for details.)")
        return len(shapefiles)
    log(f"  using {name}")
    failed, taken = 0, set()
    for src in map(Path, shapefiles):
        try:
            dst = unique_tab_path(output, src.stem, taken)
            for ext in (".tab", ".dat", ".map", ".id", ".ind"):
                dst.with_suffix(ext).unlink(missing_ok=True)
            expected, _ = dbf_info(src)
            ok, err = run(build_args(src, style), src, dst)
            if ok and style and not dst.exists():
                # Very old/unusual GDAL builds may reject the style query:
                # fall back to a plain conversion rather than nothing.
                ok, err = run(build_args(src, False), src, dst)
            if not (ok and dst.exists()):
                raise RuntimeError(err or "GDAL conversion failed")
            if not stage2.make_mapinfo85_safe(dst):
                raise RuntimeError("table needs a newer MapInfo than 8.5 "
                                   "(unsupported field type)")
            got = tab_record_count(dst)
            note = "" if got in (None, expected) else \
                f"  WARNING: {expected} records in, {got} out"
            log(f"  {src.name} -> {dst.name}  ({got if got is not None else '?'} "
                f"objects){note}")
        except Exception as exc:              # noqa: BLE001
            failed += 1
            log(f"  ERROR converting {src.name}: {exc}")
    return failed


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert shapefile(s) to MapInfo 8.5 TAB tables.")
    p.add_argument("shapefiles", nargs="+", help=".shp file(s)")
    p.add_argument("--output", required=True, help="folder for the .tab files")
    p.add_argument("--no-style", action="store_true",
                   help="keep GDAL's default style instead of the red boundary")
    a = p.parse_args(argv)
    print("Converting shapefile(s) to MapInfo TAB")
    failed = convert_shapefiles(a.shapefiles, a.output, style=not a.no_style)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
