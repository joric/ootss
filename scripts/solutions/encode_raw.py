from re import sub

s2rle_after = lambda s: sub(r'(.)\1*', lambda m: m[1] + (str(len(m[0])) if len(m[0]) > 1 else ''), s)

def process_file(input_path, output_path):
    with open(input_path, 'r') as infile, open(output_path, 'w') as outfile:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            name, sequence = line.split(',', 1)
            outfile.write(f"{name},{s2rle_after(sequence)}\n")

if __name__ == "__main__":
    process_file("solutions_raw.csv", "solutions_rle.csv")
    print("Done.")
