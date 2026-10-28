@echo off
setlocal enabledelayedexpansion

for /f "usebackq delims=" %%a in (".env") do set %%a

set src=textures
set dst=webp

set base=..\..\ootss_tileset\images\levels
goto :list

mkdir %src%
mkdir %dst%

python level_names.py > names.txt


for /f "usebackq delims=" %%n in ("names.txt") do (
    set "name=%%n"
    set "p=!GAME_DIR!\data-pc\__!name!.texture"
    python export_texture.py "!p!" -o "!src!/!name!.png"
)
:convert

for %%f in ("%src%\*.png") do (
    vips webpsave "%%f" "%dst%\%%~nf.webp" --Q 70 --vips-progress
)

:list

dir /b %base% > ..\data\images.txt
