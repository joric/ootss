@echo off

set src=map_7680x4320.png
set src=map_12288x6480.png
set src=map_16384x9216.png
set dest=tiles

rem vips dzsave %src% %dest% --layout google --tile-size 512 --suffix .jpg[Q=70] --background 0 --centre --vips-progress

rem vips dzsave %src% %dest% --layout google --tile-size 512 --suffix .png --background 0 --centre --vips-progress

rem vips dzsave %src% %dest% --layout google --tile-size 512 --suffix .webp[Q=70] --background 0 --centre --vips-progress


set temp=temp.v

vips addalpha %src% %temp%

vips dzsave %temp% %dest% --layout google --tile-size 512 --suffix .webp[Q=70] --background 0 --centre --vips-progress

