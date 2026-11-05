import json
import sys
from pathlib import Path

def convert(input_path, output_path):
    lines = Path(input_path).read_text(encoding='utf-8').splitlines()
    result = {}

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('`'):
            key = line.lstrip('`').strip()
            j = i + 1
            value_lines = []
            while j < len(lines):
                stripped = lines[j].strip()
                # Stop when we hit the next key
                if stripped.startswith('`'):
                    break
                if stripped:  # skip blank lines
                    value_lines.append(stripped)
                j += 1
            result[key] = '\n'.join(value_lines)
            i = j
        else:
            i += 1

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )
    print(f"Wrote {len(result)} entries to {out_path}")

if __name__ == '__main__':
    inp = sys.argv[1] if len(sys.argv) > 1 else 'input.txt'
    out = sys.argv[2] if len(sys.argv) > 2 else 'output.json'
    convert(inp, out)
