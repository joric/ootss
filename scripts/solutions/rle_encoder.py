import re, shutil

rle = lambda s: re.sub(r'(.)\1*', lambda m: m[1] + str(len(m[0]) if len(m[0]) > 1 else ''), s)

shutil.copy2('solutions.csv', 'solutions.csv.bak')

with open('solutions.csv') as f:
    rows = [l.split(',', 1) for l in f.read().splitlines() if l]

with open('solutions.csv', 'w') as f:
    f.writelines(f"{n},{rle(s)}\n" for n, s in rows)

print("Done.")
