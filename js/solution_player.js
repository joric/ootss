var SolutionPlayer = function() {

let levelsLoaded = false;

let solutions = {};
let pos = 0;
let levelName = '';
let prevLevelNo = -1;
let solutionTimer = null;

let playDelay = 75;
let playing = false;
let recording = false;
let keylog = [];
let recordedLevel = '';

const unpackSolution = s => s.replace(/([A-Z])(\d+)/g, (_,l,n)=>l.repeat(+n));
const convertSolution = s => unpackSolution(s).toLowerCase();
const packSolution = s => s.replace(/(.)\1*/g, (match, char) => char + (match.length > 1 ? match.length : ''));

function hidePlayer() {
  const select = document.getElementById('player').style.visibility='hidden';
}

// Helper function to download file using anchor tag (Firefox fallback)
function downloadFile(content, filename) {
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function action_load() {
  // Check if showOpenFilePicker is available (Chrome/Edge)
  if (window.showOpenFilePicker) {
    (async () => {
      try {
        const [h] = await window.showOpenFilePicker({ startIn: 'downloads' });
        const f = await h.getFile();
        const t = await f.text();
        const obj = {};
        t.split('\n').forEach(l => { if (l.trim()) { const [k, ...v] = l.split(':'); obj[k] = v.join(':'); } });
        Object.assign(solutions, obj);
        console.log('Loaded:', obj);
        updateControls();
      } catch (e) {
        if (e.name !== 'AbortError') console.error(e);
      }
    })();
  } else {
    // Firefox fallback: Use a hidden file input
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.txt';
    input.onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const t = await file.text();
      const obj = {};
      t.split('\n').forEach(l => { if (l.trim()) { const [k, ...v] = l.split(':'); obj[k] = v.join(':'); } });
      Object.assign(solutions, obj);
      console.log('Loaded:', obj);
      updateControls();
      input.remove();
    };
    input.click();
  }
}

function action_save() {
  const toCSV = obj => Object.entries(obj).map(([k, v]) => `${k}:${v}`).join('\n');
  const s = toCSV(solutions);
  const filename = 'solutions.txt';

  // Check if showSaveFilePicker is available (Chrome/Edge)
  if (window.showSaveFilePicker) {
    const saveFile = async (content, name) => {
      try {
        const handle = await window.showSaveFilePicker({ 
          startIn: 'downloads', 
          suggestedName: name 
        });
        const writable = await handle.createWritable();
        await writable.write(content);
        await writable.close();
        console.log('File saved successfully!');
      } catch (err) {
        if (err.name === 'AbortError') {
          console.log('User cancelled the save dialog');
        } else {
          console.error('Error saving file:', err);
        }
      }
    };

    (async () => {
      await saveFile(s, filename);
    })();
  } else {
    // Firefox fallback: Download via anchor tag
    downloadFile(s, filename);
  }
}

function action_stop() {
  reset();

  if (recording) {
    action_record();
  }
}

function reset() {
  sendMove(keyCodes.reset);
  stopSolution();
}

function recordKey(code) {
  const keymapping = {
    'KeyW': 'U',
    'ArrowUp': 'U',
    'KeyA': 'L',
    'ArrowLeft': 'L',
    'KeyS': 'D',
    'ArrowDown': 'D',
    'KeyD': 'R',
    'ArrowRight': 'R',
    'KeyX': 'X',
    'KeyC': 'C',
  };

  let c = keymapping[code];
  if (!c) return;
  //console.log('recorded', c);

  if (recording) keylog.push(c);
}

function action_record() {
  stopSolution();

  const button = document.getElementById('record');
  if (!button) return;

  if (!recording) {
    recording = true;
    button.disabled = true;
    keylog = [];
    recordedLevel = levelName;
    const canvas = document.getElementById('gameCanvas');
    canvas.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  } else {
    recording = false;
    button.disabled = false;
    if (keylog.length<1) return;

    let old_packed = solutions[recordedLevel];
    let old = unpackSolution(old_packed);

    let rec = keylog.join('');
    let rec_packed = packSolution(rec);

    console.log(`Recorded solution: ${rec_packed}`);

    if (confirm(`Level: ${recordedLevel}\nRecorded sequence: ${rec_packed}\nNew solution: ${rec_packed.length} packed / ${rec.length} unpacked.\nOld solution: ${old_packed.length} packed / ${old.length} unpacked.\nReplace old solution?`)) {
      solutions[recordedLevel] = rec_packed;
    }
    updateControls();
  }
}

function action_play() {
  updateControls();
  if (!solutionTimer) {
    playing = true;
    playSolution();
  } else {
    playing = false;
    pauseSolution();
  }
}

function playSolution() {
  updateControls();
  if (!solutionTimer) {
    solutionTimer = setInterval(sendNextMove, playDelay);
  }
}

function getMapLink(name) {
  let mapBase = location.protocol === 'file:' ? 'index.html' : './';
  return name ? mapBase + '#' + name : mapBase;
}

function pin() {
  let url = getMapLink(levelName);
  window.open(url, '_blank');
}

function updateControls() {
  document.title = `${levelName} - ${state.metadata.title}`;

  const select = document.getElementById('levelSelect');
  if (select) select.value = levelName;

  const pin = document.getElementById('pin');
  if (pin) pin.href = getMapLink(levelName);

  let solution = (convertSolution(solutions[levelName]||'')).toUpperCase();

  if (pos>=solution.length) {
    pos = solution.length;
  }

  let at = solution[pos] || ' ';
  let before = solution.slice(0, pos);
  let after = solution.slice(pos + 1);

  document.getElementById('moves-before').textContent = before;
  document.getElementById('moves-at').textContent = at;
  document.getElementById('moves-after').textContent = after;
}


const keyCodes = {
  // solution keycodes
  r: 39, // right
  l: 37, // left
  u: 38, // up
  d: 40, // down
  x: 88, // X
  c: 67, // C
  v: 86, // V

  // other keycodes
  undo: 90, // Z
  reset: 82, // R
  digit1: 49,
  digit2: 50,
  digit3: 51,

  // for btnKey
  KeyR: 82, // R
  KeyX: 88, // X
  KeyC: 67, // C
  KeyZ: 90, // Z
};

function dispatchFakeKey(type, code) {
  const fakeEvent = new KeyboardEvent(type, {
    bubbles: true,
    cancelable: true,
  });
  Object.defineProperty(fakeEvent, 'keyCode', { get: () => code });
  Object.defineProperty(fakeEvent, 'which', { get: () => code });
  document.dispatchEvent(fakeEvent);
}

function sendKey(id) {
  sendMove(keyCodes[id]);
}

function sendMove(code) {
  const canvas = document.getElementById('gameCanvas');
  canvas.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  updateControls();
  dispatchFakeKey('keydown', code);
  dispatchFakeKey('keyup', code);
}

function stepLevel(step) {

  let i = curLevelNo + step;

  if (i<0) {
    i = state.levels.length-1;
    pos = -1;
  }
  if (i>=state.levels.length) {
    i = 0;
    pos = -1;
  }

  const name = state.levels[i].section;

  location.hash = name;

  updateControls();
  gotoLevel(i);
}

function nextLevel() {
  stepLevel(1);
}

function prevLevel() {
  stepLevel(-1);
}

function stopSolution(){
  pos = 0;
  pauseSolution();
  updateControls();
  playing = false;
}

function pauseSolution() {
  if (solutionTimer) {
    clearInterval(solutionTimer);
    solutionTimer = null;
  }
}

function sendNextMove() {
  let solution = convertSolution(solutions[levelName]||'');

  if (pos < solution.length) {
    const ch = (solution[pos]||'').toLowerCase();

    //console.log('sending', ch, 'of', solution.slice(pos), 'at', pos, 'from', name);

    const code = keyCodes[ch];
    if (code !== undefined) {
      sendMove(code);
      pos++;
      //console.log(`[${pos}/${solution.length}] sent '${ch}'`);
    } else {
      console.warn(`Unknown symbol '${ch}' at position ${pos}, skipping`);
      pos++;
    }

    if (pos==solution.length-1) console.log('Solution complete.');
  } else {
    pauseSolution();
  }

  updateControls();
}

function nextMove() {
  sendNextMove();
  updateControls();
}

function prevMove() {
  action_undo();
  undo_handler();
  updateControls();
}

function undo_handler() {
  if (pos > 0) {
    pos--;
  }
  keylog.pop();
  updateControls();
}

function reset_handler(){
  keylog = [];
  stopSolution();
}

function action_undo() {
  sendMove(keyCodes.undo); // Z
}

function loadLevel(name) {
  levelName = name;
  console.log('loading level', name);
  for (const i in state.levels) {
    if (state.levels[i].section == name) {
      console.log('found level number', i, name);
      pos = 0;
      updateControls();
      location.hash = name;
      updateControls();
      gotoLevel(i);
      break;
    }
  }
}

function postLoading() {
  fetch('data/solutions.txt').then(r => r.text()).then(data => {
    solutions = Object.fromEntries(data.split('\n').map(s => s.trim().split(':', 2)).filter(([, v])=> v && v.length>=2));
    const a = Object.values(solutions).map(s=>s.length);
    if (a) {
      console.log(`${a.length} solutions`);
    }
    initPlayer();
  });
}

function initPlayer() {
  const select = document.getElementById('levelSelect');
  if (select) {
    select.innerHTML = '';
    for (const i in state.levels) {
      const option = document.createElement('option');
      const name = state.levels[i].section;
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    }
  }

  if (location.hash) {
    let levelName = location.hash.slice(1);
    setTimeout(()=>{ loadLevel(levelName); }, 500);
  }

  document.querySelectorAll('[data-key]').forEach(c => { 
    c.onclick = e => {
      e.preventDefault();
      e.stopPropagation();
      sendKey(e.target.dataset.key);
      return false;
    };
  });

  document.querySelectorAll('[data-fn]').forEach(c => { 
    c.onclick = e => { 
      e.preventDefault();
      e.stopPropagation();

      try {
        eval(e.target.dataset.fn + '()');
      } catch(err) {
        console.warn('Function not found', e.target.dataset.fn);
      }

      return false;
    };
  });

  if (select) select.onchange = function(e) {
    loadLevel(e.target.value);
  }

  updateControls();
}

function onLevelChange(name) {
  pos = 0;
  updateControls();

  if (!levelsLoaded) {
    levelsLoaded = true;
    postLoading();
  }

  if (playing) {
    setTimeout(function(){ playSolution() }, 250);
  }

}

function monitorLevelUpdate() {
  if (curLevelNo != prevLevelNo && state && state.levels && state.levels[curLevelNo] && state.levels[curLevelNo].section) {
    prevLevelNo = curLevelNo;
    levelName = state.levels[curLevelNo].section;
    console.log(`level change ${levelName}`);
    onLevelChange(levelName);
  }
}

function load_player() {
  const style = document.createElement('style');
  style.textContent = `
#player {
  position: fixed;
  top: 20px;
  left: 20px;
  background: #ccc;
  color: #000;
  padding: 0;
  border-radius: 10px;
  z-index: 9999;
  font-family: sans-serif;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  width: 330px;
  touch-action: none;
  overflow: hidden;
}
#player .header {
  padding: 8px 16px;
  background: #ddd;
  cursor: grab;
  user-select: none;
}
#player .header:active { cursor: grabbing; }
#player .body {
  padding: 16px;
  background: #ccc;
}
#player .close {
  float: right;
  margin-left: 20px;
  background: none;
  border: none;
  color: #666;
  font-size: 18px;
  cursor: pointer;
}
#player .close:hover { color: #000; }
#player select {
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid #999;
  width: 100%;
}
.buttons {
  display: flex;
  gap: 4px;
  margin-top: 8px;
}
.buttons button {
  flex: 1;
}

.moves {
  font-size: 14px;
  display: flex;
  align-items: baseline;
  font-family: Arial, sans-serif;
  overflow: hidden;
  max-width: 300px;
  margin-top: 8px;
}

#moves-before,
#moves-after {
  flex: 1 1 0;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: black;
}

#moves-before {
  direction: rtl;
  text-align: right;
}

#moves-after {
  direction: ltr;
  text-align: left;
}

#moves-at {
  flex: 0 0 auto;
  color: black;
  font-weight: bold;
}

.selectorDiv {
  display: flex;
  gap: 4px;
}


.btn {
  display: inline-flex;
  margin: 3px 0px;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 55px;
  height: 55px;
  border-radius: 50%;
  background-color: rgba(128, 128, 128, 0.5);
  border: none;
  color: white;
  font-size: 18px;
  text-align: center;
  cursor: pointer;
  transition: transform 0.2s ease, opacity 0.2s ease;
  user-select: none;
  touch-action: manipulation;
  overflow: hidden;
  outline: none;
  box-shadow: none;
  appearance: none;
  z-index: 1000;
  opacity: 0.75;
  text-decoration: none;
}

.btn:hover {
  opacity: 0.5;
}

.controls {
  position: absolute;
  padding: 16px;
}

.middle {top: 50%; transform: translateY(-25%); }
.center {left: 50%; transform: translateX(-50%); }
.top { top: 0; }
.right { right: 0; }
.left { left: 0; }
.bottom { bottom: 0; }

  `;
  document.head.appendChild(style);

  const overlay = document.createElement('div');
  overlay.id = 'player-overlay';
  overlay.style.cssText = `
    position: fixed;
    inset: 0;
    z-index: 9998;
    pointer-events: none;
    background: transparent;
  `;
  document.body.appendChild(overlay);

  /*
  const c1 = document.createElement('div');
  c1.innerHTML = `
    <div class="controls bottom left">
      <!--
      <button class="btn" data-key="KeyZ">Z</button>
      <button class="btn" data-key="KeyX">X</button>
      -->
      <button class="btn" data-key="KeyC">C</button>
    </div>
  `;
  document.body.appendChild(c1);
  */

  const el = document.createElement('div');
  el.id = 'player';
  el.innerHTML = `
    <div class="header">
      Solution Player
      <button class="close" data-fn="hidePlayer">&times;</button>
    </div>

    <div class="body">

      <div class="selectorDiv">
        <select id="levelSelect"></select>
        <button data-fn="pin" id="pin" title="Go to Map">&#x1F4CC;</button>
      </div>

      <div class="buttons">
        <button data-fn="action_play" data-key="KeyE" id="play" title="E to play, [ ] to step back and forward">Play</button>
        <button data-fn="action_record" data-key="KeyF" id="record" title="F to toggle record (supports reset and undo)">Record</button>
        <button data-fn="action_stop" data-key="KeyR" id="stop" title="R to reset">Reset</button>
        <button data-fn="action_save" id="save" title="Save solutions.txt">Save</button>
        <button data-fn="action_load" id="load" title="Load solutions.txt">Load</button>
      </div>

      <div class="moves">
        <button data-fn="prevLevel" title="PgUp - previous level">&lt;&lt;</button>
        <button data-fn="prevMove" title="[ previous move">&lt;</button>
        <span id="moves-before"></span> [ <span id="moves-at"></span> ] <span id="moves-after"></span>
        <button data-fn="nextMove" title="] next move">&gt;</button>
        <button data-fn="prevLevel" title="PgDn - next Level">&gt;&gt;</button>
      </div>

    </div>
  `;
  document.body.appendChild(el);

  const header = el.querySelector('.header');
  let drag = false;
  let pointerId;
  let x;
  let y;

  const stop = (e) => {
    // Allow events on interactive elements
    if (e.target.closest('button') || e.target.closest('select') || e.target.closest('.close')) {
      return;
    }
    e.stopPropagation();
  };


  el.addEventListener('pointerdown', (e) => {
    stop(e);

    if (!e.target.closest('.header') || e.target.closest('.close')) return;

    drag = true;
    pointerId = e.pointerId;
    x = e.clientX - el.offsetLeft;
    y = e.clientY - el.offsetTop;

    el.setPointerCapture(pointerId);
    e.preventDefault();
  }, true);

  el.addEventListener('pointermove', (e) => {
    stop(e);

    if (!drag || e.pointerId != pointerId) return;

    el.style.left = `${e.clientX - x}px`;
    el.style.top = `${e.clientY - y}px`;
    e.preventDefault();
  }, true);

  const endDrag = (e) => {
    stop(e);

    if (!drag || e.pointerId != pointerId) return;

    drag = false;

    if (el.hasPointerCapture(pointerId)) {
      el.releasePointerCapture(pointerId);
    }
  };

  el.addEventListener('pointerup', endDrag, true);
  el.addEventListener('pointercancel', endDrag, true);

  for (const type of [
    'click',
    'dblclick',
    'contextmenu',
    'wheel',
    'mousedown',
    'mouseup',
    'mousemove',
    'touchstart',
    'touchmove',
    'touchend',
    'touchcancel'
  ]) {
    el.addEventListener(type, stop, true);
  }

  el.addEventListener('touchmove', (e) => {
    e.preventDefault();
  }, { capture: true, passive: false });

  el.addEventListener('mouseenter', () => {
    overlay.style.pointerEvents = 'auto';
  });

  el.addEventListener('mouseleave', () => {
    if (!drag) overlay.style.pointerEvents = 'none';
  });

  el.addEventListener('pointerdown', () => {
    overlay.style.pointerEvents = 'auto';
  }, true);

  el.addEventListener('lostpointercapture', () => {
    drag = false;
    overlay.style.pointerEvents = 'none';
  });

  setInterval(monitorLevelUpdate, 20);

  window.addEventListener('keydown', function (e) {
    switch (e.code) {

      case 'Escape':
        if (recording) {
          e.preventDefault();
          action_record();
        }
        break;

      case 'BracketRight':
        e.preventDefault();
        nextMove();
        break;

      case 'BracketLeft':
        e.preventDefault();
        prevMove();
        break;

      case 'PageUp':    prevLevel(); break;
      case 'PageDown':  nextLevel(); break;
      case 'KeyF':      action_record(); break;
      case 'KeyE':      action_play();   break;
      case 'KeyZ':      undo_handler();  break;

      case 'KeyR':
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          location.reload();
        } else {
          reset_handler();
        }
        break;

      case 'KeyW': case 'ArrowUp':
      case 'KeyA': case 'ArrowLeft':
      case 'KeyS': case 'ArrowDown':
      case 'KeyD': case 'ArrowRight':
      case 'KeyX': case 'KeyC':
        recordKey(e.code);
        break;

    }
  }, true);
}

  window.addEventListener('load', load_player);

}();




