@echo off


for /f "usebackq delims=" %%a in (".env") do set %%a

set package=%GAME_DIR%\data\levels.package
set entities=%EXPORT_DIR%\data-common
set level_sets=%EXPORT_DIR%\data\level_sets
set out=%EXPORT_DIR%

python export_audio.py --package "%package%" --out ../data/level_audio.json --subtitles "%GAME_DIR%\data\strings\subtitles\en.subtitles"

