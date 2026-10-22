del cache\id.wikitactics.com\api\community\published\index.html
del cache\sinkingstar.wikitactics.com\index.html

python wt_crawler.py --convert-only --wt ..\data\wt.txt  --out ..\data\solutions.json

python wt_crawler.py --convert-only --info --wt ..\data\wt.txt

copy solutions.json D:\Shared\Tools\Hacking\Games\Jai\ootss-cmd\data\solutions.json

