import rasterio
import numpy as np
import rasterio.features
import json

base = 'regions'
INPUT = f'{base}.png'
GEOJSON = f'{base}.json'

SCALE_FACTOR = 1.0

dx = -86 + 0.5
dy = -74 - (192-108)//2 + 0.5

colorMap = {
    '#000000': { 'name': 'center',       'title': 'Center' },
    '#ffff00': { 'name': 'crossroads',   'title': 'Crossroads'        },
    '#0000ff': { 'name': 'promesst',     'title': 'The Promise'       },
    '#ffa500': { 'name': 'mirror',       'title': 'Mirror Isles' },
    '#ff0000': { 'name': 'heroes1',      'title': 'Heroes of Hauling' },
    '#00ff00': { 'name': 'heroes2',      'title': 'Heroes 2: Monsters' },
    '#ff00ff': { 'name': 'heroes3',      'title': 'Heroes 3: Bard, Druid' },
    '#00ffff': { 'name': 'water',        'title': 'Heroes and Water' },
};

# Load image with rasterio
with rasterio.open(INPUT) as src:
    height, width = src.height, src.width
    print(f"Input: {width} x {height}")
    
    # Read the single band (indexed colors)
    if src.count == 1:
        # Use the index values directly as labels
        labels = src.read(1).astype(np.uint32)
    else:
        # If RGB, encode to integer
        r = src.read(1)
        g = src.read(2)
        b = src.read(3)
        labels = (r.astype(np.uint32) << 16) | \
                 (g.astype(np.uint32) << 8) | \
                 b.astype(np.uint32)
    
    # Extract polygons
    results = rasterio.features.shapes(labels, connectivity=8)
    
    # Build features
    features = []
    for i, (geom, value) in enumerate(results):
        if geom['type'] != 'Polygon':
            continue

        intvalue = int(value)
        color = f'#{intvalue:06x}'
        #print(color)

        scaled_coords = [
            [[(x * SCALE_FACTOR) + dx, (y * SCALE_FACTOR) + dy] for x, y in ring]
            for ring in geom['coordinates']
        ]

        p = colorMap.get(color)
        if p:
            features.append({
                "type": "Feature",
                "properties": { "color": color, "name": p['name'], "title": p['title'] },
                "geometry": { "type": "Polygon", "coordinates": scaled_coords },
            })


# Save GeoJSON
geojson = {
    "type": "FeatureCollection",
    "features": features
}

with open(GEOJSON, "w") as f:
    json.dump(geojson, f)

print(f"Done: {len(features)} polygons, {GEOJSON}")

import subprocess

subprocess.run(
    'mapshaper regions.json -simplify 100% -clean -explode -o prettify ../data/regions.json',
    shell=True,
    check=True
)

