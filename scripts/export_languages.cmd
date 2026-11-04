@echo off

for /f "usebackq delims=" %%a in (".env") do set %%a

for %%L in (de en es fr it ja ko pt-br ru s-cn t-cn) do (
    python export_languages.py "%GAME_DIR%\data\strings\%%L.localization" "..\data\Localization\%%L.json"
    python export_subtitles.py "%GAME_DIR%\data\strings\subtitles\%%L.subtitles" "..\data\Subtitles\%%L.json"
)



