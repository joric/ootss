import json
import os

with open ('order.txt') as f:
    names = f.read().splitlines()

name_order = {name: index for index, name in enumerate(names)}

with open('../data/markers.json') as f:
    j = json.load(f)

# Sort features by the order of names
features = sorted(
    j['features'],
    key=lambda feature: name_order.get(feature.get('properties', {}).get('name'), len(names))
)

out = {"type": "FeatureCollection", "features": features}

with open('../data/markers.json', 'w', encoding='utf-8', newline='\n') as f:
    json.dump(out, f, indent=2)

print("Done.")
