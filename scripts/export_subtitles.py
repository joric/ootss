import json
import re
import sys
import os

RE_SECTION = re.compile(r'^:{1,3}\s*(\S+)')
RE_CUE     = re.compile(r'^=\s*(\S+)\s+([+-]?\d+(?:\.\d+)?)(?:\s+([+-]?\d+(?:\.\d+)?))?\s*$')
RE_COMMENT = re.compile(r'^\+\+')
RE_INDEX   = re.compile(r'^\[\d+\]\s*$')


def parse_subtitles(text):
    result = {}
    current_key = None
    current_cue = None

    def flush():
        nonlocal current_cue
        if current_cue is not None and current_cue["text"]:
            result[current_key].append(current_cue)
        current_cue = None

    for line in text.splitlines():
        if RE_INDEX.match(line):
            continue

        m = RE_SECTION.match(line)
        if m:
            flush()
            current_key = m.group(1)
            result.setdefault(current_key, [])
            continue

        m = RE_CUE.match(line)
        if m:
            flush()
            name = m.group(1)
            if name == '_':
                continue
            current_cue = {
                "name": name,
                "time": float(m.group(2)),
                "text": "",
            }
            continue

        if RE_COMMENT.match(line):
            continue

        if line.strip() == "":
            flush()
            continue

        if current_cue is not None:
            piece = line.strip()
            current_cue["text"] = (current_cue["text"] + " " + piece).strip() if current_cue["text"] else piece

    flush()
    return result


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.subtitles> <output.json>", file=sys.stderr)
        sys.exit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]

    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read()

    data = parse_subtitles(raw)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(data)} sections to {output_path}")


if __name__ == "__main__":
    main()
