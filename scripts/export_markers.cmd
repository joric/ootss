rem @echo off

for /f "usebackq delims=" %%a in (".env") do set %%a

set package=%GAME_DIR%\data\levels.package
set entities=%EXPORT_DIR%\data-common
set level_sets=%EXPORT_DIR%\data\level_sets
set export=%EXPORT_DIR%

if not exist "%export%" (
    python simp_tool.py extract "%package%" --output-dir "%export%"
)

if not exist schema.json (
    python simp_tool.py schema "%GAME_DIR%\sinking_star.exe" -o schema.json
)

set filter=Level_Entry Area_Pin Marker Freezer Teleporter Voice_Log Treasure Sign

set out=../data/markers.json

if "%1"=="dev" (
    set filter=-x Inanimate -x GAME_PROM_Bars_Straight_MOR_A -x Wall_Set -x Fence -x Decal -x Group -x Procedurally_Animating -x Point_Light -x Light_Probe -x Particle_Source
    set out=markers.json
)


python simp_tool.py markers "%package%" --output %out% --schema schema.json --entity-properties %filter% --entities "%entities%\overworld.entities" --entities "%entities%\intro.entities" --entities "%entities%\skipping_stones.entities"
