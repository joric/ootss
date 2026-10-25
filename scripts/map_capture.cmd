@echo off
setlocal EnableExtensions EnableDelayedExpansion

python map_capture.py --px 352 --tiles 8 8 --out map_tiles_64k --capture-only

cd map_tiles_64k

REM ============================================================
REM  Stitch 8x8 tiles -> resize to 32768px wide -> dzsave tileset
REM  Requires: libvips (vips.exe) on PATH
REM ============================================================

set "COLS=8"
set "OUT_W=65536"
set "GRID=grid.v"
set "RESIZED=big_65536.v"

set "TILESET=tiles"

REM --- Build the 8x8 grid row by row (progress shown) ---

if exist %RESIZED% goto :tiles
if exist %GRID% goto :resize

echo [1/3] Building 8x8 grid...
echo.
echo   Row 0...
vips arrayjoin "tile_r00_c00.png tile_r00_c01.png tile_r00_c02.png tile_r00_c03.png tile_r00_c04.png tile_r00_c05.png tile_r00_c06.png tile_r00_c07.png" row0.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 1...
vips arrayjoin "tile_r01_c00.png tile_r01_c01.png tile_r01_c02.png tile_r01_c03.png tile_r01_c04.png tile_r01_c05.png tile_r01_c06.png tile_r01_c07.png" row1.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 2...
vips arrayjoin "tile_r02_c00.png tile_r02_c01.png tile_r02_c02.png tile_r02_c03.png tile_r02_c04.png tile_r02_c05.png tile_r02_c06.png tile_r02_c07.png" row2.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 3...
vips arrayjoin "tile_r03_c00.png tile_r03_c01.png tile_r03_c02.png tile_r03_c03.png tile_r03_c04.png tile_r03_c05.png tile_r03_c06.png tile_r03_c07.png" row3.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 4...
vips arrayjoin "tile_r04_c00.png tile_r04_c01.png tile_r04_c02.png tile_r04_c03.png tile_r04_c04.png tile_r04_c05.png tile_r04_c06.png tile_r04_c07.png" row4.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 5...
vips arrayjoin "tile_r05_c00.png tile_r05_c01.png tile_r05_c02.png tile_r05_c03.png tile_r05_c04.png tile_r05_c05.png tile_r05_c06.png tile_r05_c07.png" row5.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 6...
vips arrayjoin "tile_r06_c00.png tile_r06_c01.png tile_r06_c02.png tile_r06_c03.png tile_r06_c04.png tile_r06_c05.png tile_r06_c06.png tile_r06_c07.png" row6.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo   Row 7...
vips arrayjoin "tile_r07_c00.png tile_r07_c01.png tile_r07_c02.png tile_r07_c03.png tile_r07_c04.png tile_r07_c05.png tile_r07_c06.png tile_r07_c07.png" row7.v --across %COLS% --shim 0 --vips-progress
if errorlevel 1 goto :err

echo.
echo   Stacking rows into %GRID%...
vips arrayjoin "row0.v row1.v row2.v row3.v row4.v row5.v row6.v row7.v" "%GRID%" --across 1 --shim 0 --vips-progress
if errorlevel 1 goto :err

:resize

REM --- Resize so width = 32768 (height auto-preserved) ---
echo.
echo [2/3] Resizing to %OUT_W% px wide...

set /a BIG_W=8360*8
for /f "delims=" %%a in ('powershell -NoProfile -Command "65536 / %BIG_W%"') do set "SCALE=%%a"
vips resize "%GRID%" "%RESIZED%" %SCALE% --vips-progress

if errorlevel 1 goto :err

:tiles

REM --- dzsave: centre layout, webp tiles, ---
echo.
echo [3/3] Writing tileset (dzsave, layout=centre)...
vips dzsave "%RESIZED%" "%TILESET%" --layout google --tile-size 512  --background "0 0 0 0" --suffix .webp[Q=85] --centre --vips-progress

if errorlevel 1 goto :err

REM --- Cleanup intermediates ---
rem del /q row0.v row1.v row2.v row3.v row4.v row5.v row6.v row7.v 2>nul

echo.
echo Done. Tileset written to: %TILESET%
goto :eof

:err
echo.
echo ERROR: vips command failed. Aborting.
exit /b 1

