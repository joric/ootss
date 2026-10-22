// ---------- UI ----------

let imageCB = (matches) => { console.log(matches); }
let indexURL = 'data/image_search.json';

const dropEl    = null;
const fileEl    = null;

function loadImageFromFile(file) {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = URL.createObjectURL(file);
  });
}

async function pHash(img) {
  const size = 32;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, size, size);
  const data = ctx.getImageData(0, 0, size, size).data;

  const gray = new Float32Array(size*size);
  for (let i = 0; i < size*size; i++)
    gray[i] = 0.299*data[i*4] + 0.587*data[i*4+1] + 0.114*data[i*4+2];

  const dct = dct2d(gray, size);
  const low = [];
  for (let y = 0; y < 8; y++)
    for (let x = 0; x < 8; x++) low.push(dct[y*size + x]);

  const sorted = low.slice(1).sort((a,b) => a-b);
  const median = sorted[Math.floor(sorted.length/2)];

  let hash = 0n;
  for (let i = 0; i < 64; i++)
    if (low[i] > median) hash |= (1n << BigInt(63 - i));
  return hash.toString(16).padStart(16, '0');
}

function dct2d(input, N) {
  const cos = Array.from({length:N}, (_,i) =>
    Array.from({length:N}, (_,j) => Math.cos(((2*j+1)*i*Math.PI)/(2*N))));
  const out = new Float32Array(N*N);
  for (let u = 0; u < N; u++)
    for (let v = 0; v < N; v++) {
      let sum = 0;
      for (let x = 0; x < N; x++)
        for (let y = 0; y < N; y++)
          sum += input[x*N+y] * cos[u][x] * cos[v][y];
      const cu = u===0 ? 1/Math.SQRT2 : 1;
      const cv = v===0 ? 1/Math.SQRT2 : 1;
      out[u*N+v] = (2/N) * cu * cv * sum;
    }
  return out;
}

function colorHistogram(img) {
  const size = 64;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, size, size);
  const data = ctx.getImageData(0, 0, size, size).data;

  const H=16, S=4, V=4;
  const hist = new Float32Array(H*S*V);
  for (let i = 0; i < data.length; i += 4) {
    const [h,s,v] = rgb2hsv(data[i], data[i+1], data[i+2]);
    const hb = Math.min(H-1, Math.floor(h/360*H));
    const sb = Math.min(S-1, Math.floor(s*S));
    const vb = Math.min(V-1, Math.floor(v*V));
    hist[(hb*S+sb)*V+vb]++;
  }
  let n = 0; for (const x of hist) n += x*x;
  n = Math.sqrt(n) || 1;
  return Array.from(hist, x => x/n);
}

function rgb2hsv(r,g,b) {
  r/=255; g/=255; b/=255;
  const max = Math.max(r,g,b), min = Math.min(r,g,b), d = max-min;
  let h = 0;
  if (d) {
    if (max===r) h = ((g-b)/d) % 6;
    else if (max===g) h = (b-r)/d + 2;
    else h = (r-g)/d + 4;
    h *= 60; if (h < 0) h += 360;
  }
  const s = max===0 ? 0 : d/max;
  return [h,s,max];
}

// ---------- Distance ----------
function hammingHex(a, b) {
  let x = BigInt('0x'+a) ^ BigInt('0x'+b);
  let d = 0;
  while (x) { d += Number(x & 1n); x >>= 1n; }
  return d;
}

function histDistance(queryHist, indexHist) {
  let s = 0;
  for (let i = 0; i < queryHist.length; i++) {
    const d = queryHist[i] - indexHist[i] / 255;
    s += d * d;
  }
  return Math.sqrt(s);
}

// ---------- Search ----------

let INDEX = null;

const PHASH_STRONG = 8;
const PHASH_MATCH  = 16;

async function loadIndex() {
  if (!INDEX) INDEX = await fetch(indexURL).then(r => r.json());
  return INDEX;
}

async function search(img) {
  const idx = await loadIndex();
  const qHash = await pHash(img);
  const qHist = colorHistogram(img);

  const scored = idx.map(item => ({
    ...item,
    hp: hammingHex(qHash, item.phash),
    hd: histDistance(qHist, item.hist),
  }));

  scored.sort((a, b) => a.hp !== b.hp ? a.hp - b.hp : a.hd - b.hd);

  const best = scored[0];
  if (!best || best.hp > PHASH_MATCH) {
    return { matches: [], best, rejected: true };
  }

  const matches = scored.filter(s =>
    s.hp <= best.hp + PHASH_STRONG && s.hp <= PHASH_MATCH
  );

  return { matches, best, rejected: false };
}

function setStatus(msg) { console.log(msg); }

async function handleFile(file) {
  if (!file) {
    return;
  }

  if (!file.type.startsWith('image/')) {
    setStatus('Not an image: ' + (file.type || 'unknown type'));
    return;
  }

  // preview
  const url = URL.createObjectURL(file);

  try {
    const img = await loadImageFromFile(file);
    const { matches, best, rejected } = await search(img);
    renderResults(matches, best, rejected);
  } catch (err) {
    console.error(err);
  }
}

function renderResults(matches, best, rejected) {
  if (rejected || !matches.length) {
    imageCB([]);
    return;
  }

  const classify = (hp) => {
    if (hp <= 6) return { label: 'EXACT', color: '#0a0' };
    if (hp <= 12) return { label: 'STRONG', color: '#a80' };
    return { label: 'LOOSE', color: '#888' };
  };

  /*
  resultsEl.innerHTML = matches.map((r, i) => {
    const { label, color } = classify(r.hp);
    const isBest = r === best;
    return `
      <div class="card" style="${isBest ? 'outline:2px solid #0a0;' : ''}">
        <img src="./images/${encodeURIComponent(r.name)}" alt="${r.name}"><br>
        <div class="meta">
          <b>#${i+1} ${r.name}</b><br>
          <span style="color:${color}">[${label}]</span>
          phash:${r.hp} hist:${r.hd.toFixed(4)}
        </div>
      </div>`;
  }).join('');
  */

  imageCB(matches);
}


function initImageSearch(cb, idx) {
  if (cb) imageCB = cb;
  if (idx) indexURL = idx;

  async function loadIndex() {
    if (!INDEX) INDEX = await fetch(indexURL).then(r => r.json());
    return INDEX;
  }

  // ---------- UI ----------
  const dropEl    = document.getElementById('drop');
  const fileEl    = document.getElementById('file');


  // ---------- Input: click ----------
  dropEl.addEventListener('click', () => fileEl.click());

  fileEl.addEventListener('change', (e) => {
    handleFile(e.target.files[0]);
    fileEl.value = ''; // allow re-selecting the same file
  });

  // ---------- Input: drag & drop ----------
  ['dragenter', 'dragover'].forEach(ev =>
    dropEl.addEventListener(ev, (e) => {
      e.preventDefault();
      dropEl.classList.add('over');
    })
  );
  ['dragleave', 'drop'].forEach(ev =>
    dropEl.addEventListener(ev, (e) => {
      e.preventDefault();
      dropEl.classList.remove('over');
    })
  );
  dropEl.addEventListener('drop', (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) handleFile(file);
  });

  // also allow dropping anywhere on the page
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) handleFile(file);
  });

  // ---------- Input: paste from clipboard ----------
  // Handles:
  //   - Ctrl+V / Cmd+V on desktop
  //   - "Paste" from the context menu
  //   - pasting a copied image file from the file manager
  //   - screenshot -> paste (Chrome/Firefox/Safari put a File into clipboardData.files)
  //   - some sources only expose the image via clipboardData.items (as a Blob)
  window.addEventListener('paste', (e) => {
    const dt = e.clipboardData;
    if (!dt) return;

    // 1) try files first (screenshots, copied files)
    let file = null;
    if (dt.files && dt.files.length) {
      file = Array.from(dt.files).find(f => f.type.startsWith('image/'));
    }

    // 2) fall back to items (some browsers expose images only here)
    if (!file && dt.items) {
      for (const item of dt.items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          file = item.getAsFile();
          if (file) break;
        }
      }
    }

    // 3) last resort: HTML in the clipboard (e.g. copied from a web page).
    //    Pull the <img src="..."> out and fetch it.
    if (!file) {
      const html = dt.getData('text/html');
      if (html) {
        const m = html.match(/<img[^>]+src=["']([^"']+)["']/i);
        if (m) {
          e.preventDefault();
          setStatus('Fetching pasted image…');
          fetch(m[1], { mode: 'cors' })
            .then(r => {
              if (!r.ok) throw new Error('fetch failed: ' + r.status);
              return r.blob();
            })
            .then(blob => handleFile(new File([blob], 'pasted.png', { type: blob.type })))
            .catch(err => setStatus('Cannot fetch pasted image (CORS?): ' + err.message));
          return;
        }
      }
    }

    if (file) {
      e.preventDefault();
      handleFile(file);
    } else {
      console.log('image not found');
      //console.log('Clipboard does not contain an image');
      imageCB([]);
    }
  });
}


