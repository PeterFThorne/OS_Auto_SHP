# OS Shapefile Tool

Version 1.2.0. Sister app to the OS Point Tool (`OS_Automation`).

A separate desktop app for the same OS MasterMap -> MapInfo 8.5 pipeline.
Instead of a centre grid reference and box size, the site comes from one or
more shapefiles: the app takes their most westerly, southerly, easterly and
northerly points, adds 300 m on every side, and runs stage 1 and stage 2 on
that box.

This project is kept separate from v1 (`OS_Processor` repo,
`E:\Maps\ZZ_AI_Testing\OS_Automation`). It has its own folder, repo and
settings file, with its own copies of the stage scripts.

## Setup (Windows work PC, the one with M:)

Working folder: `E:\Maps\ZZ_AI_Testing\OS_Auto_SHP`

```
OS_Auto_SHP\
    osmm_gui_v2.py           (this app: OS Shapefile Tool)
    os_shapefile_tool.ico    (app icon; the app still runs without it)
    osmm_site_extract.py     (stage 1, copied from OS_Automation)
    osmm_style_mapinfo.py    (stage 2, copied from OS_Automation)
    osmm_shp_to_tab.py       (input shapefile(s) -> MapInfo TAB)
    README.md
```

Changes to the stage scripts here don't affect v1, and vice versa.

Run `python osmm_gui_v2.py` (or double-click it). Standard-library Python
only; no GDAL is needed to read the shapefiles.

The window has two tabs:

- **Site**: inputs, output options and the preview map. This is all you
  normally need.
- **Settings**: set once and remembered (if one is wrong when you press
  Run, the app switches to this tab):
  - *Folders and data*: OS data folder, pipeline scripts folder,
    *Rebuild tile index*.
  - *Output files*:
    - **Style the shapefile TAB as a red boundary** (on): untick for
      GDAL's plain black outline.
    - **Also keep MIF/MID copies of the MM_* tables** (off): for other GIS
      software or checking (stage 2 `--keep-mif`).
    - **Delete site.gml.gz after the MM_* tables are made** (off): saves
      space; it can always be re-extracted.
    - **Save the run log in the output folder** (off): writes
      `OS_Shapefile_Tool_log.txt` next to the tables, a record of what was run.
  - *Preview*: **Show the OpenStreetMap background** (on): untick if the
    map is slow or there's no internet.

## Using it

1. **Input shapefile(s)**: *Add file(s)…* or *Add folder…* (adds every
   `.shp` in that folder). Each file is checked as it is added.
2. **Buffer**: 300 m by default, adjustable.
3. **One site per shapefile**: off = one combined box around all the
   shapefiles; on = a separate site for each shapefile.
4. **Output folder**, or tick **Same as input folder** to write next to the
   shapefile (the first shapefile's folder for a combined site).
   Results go straight into that folder; no subfolder is made.
5. **Create a subfolder for the site** (off by default): puts the results in
   `<output folder>\<site name>`. **Site name** blank = the shapefile's name
   (`RedLine`, or `RedLine_plus1` for a combined site from two files).
   With *One site per shapefile*, each subfolder is named after its shapefile.
   If several per-shapefile sites would land in the same folder, they get
   subfolders anyway, as their MM_* tables would otherwise overwrite each other.
6. **Style into MM_* tables (stage 2)**: ticked by default.
   **Copy input shapefile(s) to the output folder as MapInfo TAB**: ticked by
   default (see below).
7. **PDF/JPG map**: visible but disabled until that feature is built.
8. Press **Run**.

The preview map works like the OS Point Tool's: an OpenStreetMap background
drawn in British National Grid, the 5 km OS tiles (green, labelled), the
area that will be extracted (orange dashed outline, shapefile extent +
buffer) and the shapefile itself (red, as in the TAB output). The map is
drawn at the map tiles' own resolution so street names stay sharp. The exact W/S/E/N, size, centre, tiles and output
folder are listed beside it. Map tiles are cached in the temp folder (shared
with the OS Point Tool); with no internet it shows a plain background.

## What it checks

- Shapefiles in latitude/longitude are rejected with a message to reproject
  to EPSG:27700. A missing `.prj` gives a warning and assumes BNG if the
  coordinates are in range.
- Point, line, polygon and multipatch shapefiles all work, with or without
  Z/M values. A single point gives a 600 x 600 m box.
- Sites over 20 km across ask for confirmation before running.
- The shapefile list starts empty each time, so an old job can't be re-run
  by accident. Folder choices and ticks are remembered in
  `osmm_gui_v2_settings.json` (separate from v1's settings).

## Output

The output folder gets `site.gml.gz`, `manifest.json`, the MM_* tables,
`style_report.txt`, the shapefile TAB(s), plus
`site_extent_v2.json`, recording the source shapefiles, buffer, box, centre
and tiles, so you can see later how the extent was made.

## Shapefile to MapInfo TAB

With the tick box on, each input shapefile is converted to `<name>.tab` in the
output folder, next to the MM_* tables, by `osmm_shp_to_tab.py`. It can also be
run on its own:

```
python osmm_shp_to_tab.py RedLine.shp [more.shp ...] --output <folder> [--no-style]
```

- Opens in MapInfo 8.5: same GDAL settings as stage 2 (EPSG:27700, CP1252),
  large-integer fields (10+ digit numbers) are written as Decimal, and the
  header is checked with stage 2's `make_mapinfo85_safe()`.
- Styled so it shows over the MasterMap without hiding it: red 2 px outline
  and no fill for polygons, red 2 px line for lines, red circle for points.
  `--no-style` keeps GDAL's default (thin black, no fill).
- Numbers with decimal places are written as MapInfo Float. (MapInfo's
  Decimal is at most 20 characters wide, so e.g. an area with 15 decimals
  would only fit 4 digits before the point and the conversion failed.)
- If any records can't be converted, it is reported as a failure, with
  GDAL's own message, and the incomplete table is named in the log.
- Attribute columns are kept as they are; the record count is checked
  against the shapefile.
- Needs GDAL, found the same way as stage 2 (ogr2ogr, osgeo, or pyogrio). If
  it fails, the MasterMap tables are still made and the log says why.

## How the box is built

The box is rounded outwards to whole metres and made an even size, so the
centre passed to stage 1 is a whole-metre easting/northing. It is then run
exactly like a v1 job: `--centre E,N --width W --height H`. Stage 1 still
keeps whole features touching the box (ragged edge), as before.

## Version history

| Version | Changes |
|---|---|
| 1.2.0 | Clearer preview (map drawn 1:1 so labels stay sharp; sharp text on scaled Windows displays; larger labels on white halos/panels; outline-only extraction box); shapefile drawn red. New settings: red/plain boundary TAB, keep MIF/MID, delete site.gml.gz, save run log, map background on/off |
| 1.0.1 | Shapefile to TAB: fields with many decimal places (e.g. `area`, `perimeter`) are written as Float instead of failing; GDAL's real error message shown in the log; a table with missing records is reported as a failure |
| 1.0.0 | Renamed to OS Shapefile Tool; app icon (map grid + boundary polygon, `os_shapefile_tool.ico`); OpenStreetMap preview in British National Grid as in the OS Point Tool; Settings moved to their own tab; results go straight into the output folder, with an optional site subfolder; input shapefile(s) copied to the output as MapInfo TAB |

## Still to do

- PDF/JPG map output (shapefile over the styled MasterMap). The hook is
  marked in `_worker()` where it will run after stage 2.
