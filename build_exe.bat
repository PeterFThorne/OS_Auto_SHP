@echo off
REM ======================================================================
REM  Builds the OS Shapefile Tool as a normal Windows program, so
REM  colleagues can use it without Python, GDAL or Pillow installed.
REM
REM  Run this from the OS_Auto_SHP folder on a PC that has Python
REM  (just double-click it).
REM  Result: dist\OS_Shapefile_Tool\      (the program - copy the WHOLE folder)
REM          dist\OS_Shapefile_Tool_vX.Y.Z.zip   (the same, zipped for sharing)
REM ======================================================================

cd /d "%~dp0"

echo Installing / updating the build tools...
python -m pip install --upgrade pyinstaller pyogrio pillow || goto :fail

echo.
echo Building (this takes a few minutes)...
REM --onedir         one folder (fast to open, fewer antivirus problems)
REM --windowed       no black console window
REM --hidden-import  analyse the pipeline scripts so everything they need
REM                  (GDAL via pyogrio for TAB files, Pillow for JPG plans)
REM                  is bundled - the app runs them from beside the .exe
REM --collect-all    include pyogrio's GDAL DLLs and projection database
REM --icon           the .exe file icon; --add-data bundles it for the window
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name OS_Shapefile_Tool --paths . ^
  --hidden-import osmm_site_extract ^
  --hidden-import osmm_style_mapinfo ^
  --hidden-import osmm_shp_to_tab ^
  --hidden-import osmm_plan ^
  --hidden-import PIL.Image --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageFont --hidden-import PIL.ImageChops ^
  --hidden-import ctypes --hidden-import html --hidden-import dataclasses ^
  --hidden-import uuid --hidden-import contextvars --hidden-import locale ^
  --hidden-import logging --hidden-import warnings --hidden-import itertools ^
  --hidden-import contextlib --hidden-import fnmatch --hidden-import struct ^
  --hidden-import importlib.util ^
  --collect-all pyogrio ^
  --icon os_shapefile_tool.ico ^
  --add-data "os_shapefile_tool.ico;." ^
  osmm_gui_v2.py || goto :fail

echo.
echo Copying the pipeline scripts, plan files and tile index next to the program...
set OUT=dist\OS_Shapefile_Tool
copy /y osmm_site_extract.py   "%OUT%\" >nul || goto :fail
copy /y osmm_style_mapinfo.py  "%OUT%\" >nul || goto :fail
copy /y osmm_shp_to_tab.py     "%OUT%\" >nul || goto :fail
copy /y osmm_plan.py           "%OUT%\" >nul || goto :fail
copy /y plan_logo.jpg          "%OUT%\" >nul || goto :fail
copy /y plan_template.json     "%OUT%\" >nul || goto :fail
copy /y os_shapefile_tool.ico  "%OUT%\" >nul || goto :fail
if exist osmm_tile_index.json copy /y osmm_tile_index.json "%OUT%\" >nul
if exist README.md copy /y README.md "%OUT%\" >nul

echo.
echo Zipping the release...
REM Version comes from APP_VERSION in osmm_gui_v2.py, so the zip name matches the app
set VER=
for /f "delims=" %%v in ('python -c "import osmm_gui_v2 as g; print(g.APP_VERSION)"') do set VER=%%v
if "%VER%"=="" goto :fail
set ZIP=dist\OS_Shapefile_Tool_v%VER%.zip
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%' -DestinationPath '%ZIP%' -Force" || goto :fail

echo.
echo ======================================================================
echo  Done. Your program is in:  %CD%\%OUT%
echo  Zipped copy for sharing:   %CD%\%ZIP%
echo  Test it by double-clicking OS_Shapefile_Tool.exe in that folder.
echo  To share: copy the zip to the other PC, unzip it, and run
echo  OS_Shapefile_Tool.exe from inside the unzipped folder.
echo ======================================================================
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
