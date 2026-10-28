rem @echo off

for /f "usebackq delims=" %%a in (".env") do set %%a

set package=%GAME_DIR%\data\levels.package
set entities=%EXPORT_DIR%\data-common
set level_sets=%EXPORT_DIR%\data\level_sets
set out=%EXPORT_DIR%

if not exist "%out%" (
    python simp_tool.py extract "%package%" --output-dir "%out%"
)

if not exist schema.json (
    python simp_tool.py schema "%GAME_DIR%\sinking_star.exe" -o schema.json
)

set filter=

set filter=Level_Entry Area_Pin Marker Freezer

rem set filter=-x Inanimate -x GAME_PROM_Bars_Straight_MOR_A -x Wall_Set -x Fence -x Decal -x Group -x Procedurally_Animating -x Point_Light -x Light_Probe -x Particle_Source

python simp_tool.py markers "%package%" --output ../data/markers.json --schema schema.json --entities "%entities%\overworld.entities" --entity-properties %filter%

python simp_tool.py markers "%package%" --output ../data/markers_intro.json --schema schema.json --entities "%entities%\intro.entities" --entity-properties %filter%
