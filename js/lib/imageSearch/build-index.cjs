// node build-index.js
// Reads ./images/*, writes ./index.json
//
// Flags:
//   --in  <dir>   images directory   (default ./images)
//   --out <file>  index file         (default ./index.json)
//   --force       rebuild everything (ignore cache)
//   --workers N   concurrency        (default = CPU/2)

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const sharp = require('sharp');
const { createCanvas } = require('canvas');

// ---------------- CLI ----------------
const args = process.argv.slice(2);
const getArg = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : def;
};
const IN_DIR     = path.resolve(getArg('--in',  '../../../../ootss_tileset/images/levels'));
const OUT_FILE   = path.resolve(getArg('--out', '../../../data/image_index.json'));
const CACHE_FILE = OUT_FILE + '.cache';
const FORCE      = args.includes('--force');
const WORKERS    = Math.max(1, parseInt(getArg('--workers', String(Math.ceil(os.cpus().length / 2))), 10));

const SUPPORTED = /\.(jpe?g|png|webp|avif|gif|tiff?|svg|heic|heif)$/i;

// ============================================================
// DESCRIPTORS
// ============================================================

// 2D DCT (type-II), N x N. Only low frequencies are used by pHash,
// but we compute the full N x N for simplicity.
function dct2d(input, N) {
  const cos = [];
  for (let i = 0; i < N; i++) {
    cos[i] = [];
    for (let j = 0; j < N; j++)
      cos[i][j] = Math.cos(((2 * j + 1) * i * Math.PI) / (2 * N));
  }
  const out = new Float32Array(N * N);
  for (let u = 0; u < N; u++) {
    for (let v = 0; v < N; v++) {
      let sum = 0;
      for (let x = 0; x < N; x++)
        for (let y = 0; y < N; y++)
          sum += input[x * N + y] * cos[u][x] * cos[v][y];
      const cu = u === 0 ? 1 / Math.SQRT2 : 1;
      const cv = v === 0 ? 1 / Math.SQRT2 : 1;
      out[u * N + v] = (2 / N) * cu * cv * sum;
    }
  }
  return out;
}

// Perceptual hash: 64-bit, returned as a 16-char hex string.
// Robust to resizing, mild compression and small color shifts.
function pHashFromCanvas(srcCanvas) {
  const size = 32;
  const c = createCanvas(size, size);
  const ctx = c.getContext('2d');
  ctx.drawImage(srcCanvas, 0, 0, size, size);
  const data = ctx.getImageData(0, 0, size, size).data;

  // grayscale
  const gray = new Float32Array(size * size);
  for (let i = 0; i < size * size; i++)
    gray[i] = 0.299 * data[i * 4] + 0.587 * data[i * 4 + 1] + 0.114 * data[i * 4 + 2];

  // DCT -> take top-left 8x8 low-frequency block
  const dct = dct2d(gray, size);
  const low = [];
  for (let y = 0; y < 8; y++)
    for (let x = 0; x < 8; x++) low.push(dct[y * size + x]);

  // median of AC coefficients (skip DC at index 0)
  const sorted = low.slice(1).sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];

  let hash = 0n;
  for (let i = 0; i < 64; i++)
    if (low[i] > median) hash |= 1n << BigInt(63 - i);

  return hash.toString(16).padStart(16, '0');
}

function rgb2hsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60; if (h < 0) h += 360;
  }
  const s = max === 0 ? 0 : d / max;
  return [h, s, max];
}

// HSV color histogram, 16 x 4 x 4 = 256 bins, L2-normalized, stored as uint8.
// Captures color palette; complements pHash which only sees structure.
function colorHistogramFromCanvas(srcCanvas) {
  const size = 64;
  const c = createCanvas(size, size);
  const ctx = c.getContext('2d');
  ctx.drawImage(srcCanvas, 0, 0, size, size);
  const data = ctx.getImageData(0, 0, size, size).data;

  const H = 16, S = 4, V = 4;
  const hist = new Float32Array(H * S * V);
  for (let i = 0; i < data.length; i += 4) {
    const [h, s, v] = rgb2hsv(data[i], data[i + 1], data[i + 2]);
    const hb = Math.min(H - 1, Math.floor(h / 360 * H));
    const sb = Math.min(S - 1, Math.floor(s * S));
    const vb = Math.min(V - 1, Math.floor(v * V));
    hist[(hb * S + sb) * V + vb]++;
  }
  let n = 0;
  for (const x of hist) n += x * x;
  n = Math.sqrt(n) || 1;
  // uint8 (0..255) keeps JSON compact; precision is plenty for ranking
  return Array.from(hist, x => Math.round(x / n * 255));
}

// ============================================================
// DECODING: any format -> normalized canvas
// ============================================================

async function loadNormalized(filePath) {
  // rotate()        - apply EXIF orientation
  // flatten()       - composite alpha onto white
  // resize(512)     - cap memory; 32x32 / 64x64 is all we need downstream
  // ensureAlpha+raw - get a raw RGBA buffer we can push into canvas
  const { data, info } = await sharp(filePath, { animated: false, density: 150 })
    .rotate()
    .flatten({ background: '#ffffff' })
    .resize(512, 512, { fit: 'inside', withoutEnlargement: true })
    .toColorspace('srgb')
    .ensureAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });

  const canvas = createCanvas(info.width, info.height);
  const ctx = canvas.getContext('2d');
  const imgData = ctx.createImageData(info.width, info.height);
  imgData.data.set(data);
  ctx.putImageData(imgData, 0, 0);
  return canvas;
}

// ============================================================
// CACHE: file signature (path + size + mtime) -> descriptors
// ============================================================

function fileKey(fullPath) {
  const st = fs.statSync(fullPath);
  return crypto.createHash('sha1')
    .update(fullPath + '|' + st.size + '|' + st.mtimeMs)
    .digest('hex');
}

function loadCache() {
  if (FORCE || !fs.existsSync(CACHE_FILE)) return {};
  try { return JSON.parse(fs.readFileSync(CACHE_FILE, 'utf8')); }
  catch { return {}; }
}

function saveCache(cache) {
  fs.writeFileSync(CACHE_FILE, JSON.stringify(cache));
}

// ============================================================
// WORKER POOL (promise-based, trivial)
// ============================================================

async function runPool(items, worker, concurrency) {
  const results = new Array(items.length);
  let cursor = 0;
  let done = 0;

  async function next() {
    while (true) {
      const i = cursor++;
      if (i >= items.length) return;
      try {
        results[i] = await worker(items[i], i);
      } catch (err) {
        results[i] = { error: err };
      }
      done++;
      process.stdout.write(`\r[${done}/${items.length}]`);
    }
  }

  await Promise.all(Array.from({ length: concurrency }, next));
  process.stdout.write('\n');
  return results;
}

// ============================================================
// MAIN
// ============================================================

(async () => {
  if (!fs.existsSync(IN_DIR)) {
    console.error(`No such directory: ${IN_DIR}`);
    process.exit(1);
  }

  const files = fs.readdirSync(IN_DIR).filter(f => SUPPORTED.test(f));
  if (!files.length) {
    console.error(`No supported images in ${IN_DIR}`);
    process.exit(1);
  }

  console.log(`Input:   ${IN_DIR}`);
  console.log(`Output:  ${OUT_FILE}`);
  console.log(`Files:   ${files.length}, workers: ${WORKERS}${FORCE ? ', force' : ''}\n`);

  const cache = loadCache();
  const newCache = {};
  const t0 = Date.now();
  let reused = 0, failed = 0;

  const results = await runPool(files, async (f) => {
    const full = path.join(IN_DIR, f);
    const key = fileKey(full);

    // cache hit: reuse descriptors without decoding
    if (cache[key]) {
      reused++;
      newCache[key] = cache[key];
      return cache[key];
    }

    const canvas = await loadNormalized(full);
    const entry = {
      name: f,
      w: canvas.width,
      h: canvas.height,
      phash: pHashFromCanvas(canvas),
      hist: colorHistogramFromCanvas(canvas),
    };
    newCache[key] = entry;
    return entry;
  }, WORKERS);

  // separate successes from failures
  const index = [];
  results.forEach((r, i) => {
    if (!r || r.error) {
      failed++;
      console.warn(`FAIL ${files[i]}: ${r?.error?.message || 'unknown'}`);
    } else {
      index.push(r);
    }
  });

  fs.writeFileSync(OUT_FILE, JSON.stringify(index));
  saveCache(newCache);

  const sizeKB = (fs.statSync(OUT_FILE).size / 1024).toFixed(0);
  console.log(`\nDone: ${index.length} ok, ${failed} failed, ${reused} from cache`);
  console.log(`index.json: ${sizeKB} KB, time: ${((Date.now() - t0) / 1000).toFixed(1)} s`);
})();
