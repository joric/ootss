@echo off

set "src=%USERPROFILE%\Pictures\Screenshots"
set dst=..\images\levels

for %%f in ("%src%\*.png") do (
  vips webpsave "%%f" "%dst%\%%~nf.webp" --Q 70 --vips-progress
)
