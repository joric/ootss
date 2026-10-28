import json
j = json.load(open('../data/markers_intro.json'))
for f in j['features']:
    o = f.get('properties')

    if o.get('type')=='Level_Entry':
        name = o.get('name')
        if name:
            print(name)
