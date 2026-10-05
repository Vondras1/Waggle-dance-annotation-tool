// Run with: node annotation_tool/tests/test_fixed_bbox.js
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
const elements = new Map();
const storage = new Map();
const canvas = {width: 100, height: 80,
  getBoundingClientRect: () => ({left: 0, top: 0, width: 200, height: 160}),
  setPointerCapture() {}, hasPointerCapture: () => false};
elements.set('frame-canvas', canvas);
vm.runInNewContext(source.replace('safely(init);', '') + `
function check(value, message) { if (!value) throw new Error(message); }
function same(actual, expected, message) { check(JSON.stringify(actual) === JSON.stringify(expected), message); }
function event(x, y) { return {clientX: x*2, clientY: y*2, button: 0, pointerId: 1, preventDefault() {}}; }
function gesture(x, y, ex = x, ey = y) { beginDrag(event(x,y)); endDrag(event(ex,ey)); }
pause = () => {};
drawFrame = () => {};
edited = fn => { fn(state.draft); state.draft.keyframes.sort((a,b) => a.frame-b.frame); };
state.id = 'test';
state.project = {clips_dir: '/first', bbox_size: 20, clips: [{id: 'test'}]};
state.frame = state.loadedFrame = 0;
state.draft = {start_frame: 0, end_frame: 4, status: 'unreviewed', keyframes: []};
renderBoxSettings();
check(state.bboxSize === 20 && !state.fixedBbox, 'Dataset default, initially disabled');
$('fixed-bbox').checked = true;
changeBoxSettings();
gesture(50,40);
same(state.draft.keyframes[0].bbox, [40,30,60,50], 'Click places centered square in clip pixels despite display scaling');
gesture(50,40,100,80);
same(state.draft.keyframes[0].bbox, [80,60,100,80], 'Moving against bottom/right preserves size');
gesture(0,0);
same(state.draft.keyframes[0].bbox, [0,0,20,20], 'Top/left clamping preserves size');
gesture(0,0,10,10);
same(state.draft.keyframes[0].bbox, [10,10,30,30], 'Dragging a corner moves instead of resizing');
const before = clone(state.draft);
$('bbox-size').value = '12';
changeBoxSettings();
same(state.draft, before, 'Changing size does not mutate existing annotations');
gesture(20,20);
same(state.draft.keyframes[0].bbox, [14,14,26,26], 'Touching existing box applies new size');
state.project = {...state.project, clips_dir: '/second', bbox_size: 30};
renderBoxSettings();
check(state.bboxSize === 30 && !state.fixedBbox, 'Switching datasets resets preferences');
state.project = {...state.project, clips_dir: '/first', bbox_size: 20};
renderBoxSettings();
check(state.bboxSize === 12 && state.fixedBbox, 'Returning restores dataset preference');
state.boxSettingsKey = null;
renderBoxSettings();
check(state.bboxSize === 12 && state.fixedBbox, 'Preferences survive page reload');
state.project.bbox_size = 25;
renderBoxSettings();
check(state.bboxSize === 25 && !state.fixedBbox, 'New config size overrides old browser preference');
$('bbox-size').value = '0';
let invalid = false;
try { changeBoxSettings(); } catch (_) { invalid = true; }
check(invalid && state.bboxSize === 25, 'Invalid input preserves previous setting');
$('bbox-size').value = '81'; $('fixed-bbox').checked = true;
changeBoxSettings();
invalid = false;
try { beginDrag(event(50,40)); } catch (_) { invalid = true; }
check(invalid && !state.drag, 'Oversized square rejected before gesture begins');
$('bbox-size').value = '1'; changeBoxSettings();
gesture(50,40);
same(state.draft.keyframes[0].bbox, [49.5,39.5,50.5,40.5], 'One-pixel squares remain valid');
$('fixed-bbox').checked = false; changeBoxSettings();
gesture(10,10,40,30);
same(state.draft.keyframes[0].bbox, [10,10,40,30], 'Disabling restores rectangular free drawing');
gesture(40,30,60,50);
same(state.draft.keyframes[0].bbox, [10,10,60,50], 'Free corner resize remains available');
gesture(25,25,30,30);
same(state.draft.keyframes[0].bbox, [15,15,65,55], 'Free box movement remains available');
state.tool = 'direction'; state.fixedBbox = true;
gesture(30,30,50,30);
check(state.draft.direction_deg === 90, 'Direction tool still works in fixed mode');
`, {
  window: {ANNOTATION_CONFIG: {boxHitRadius: 10}},
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {value: '', checked: false});
    return elements.get(id);
  }},
  localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
});
console.log('Fixed bbox interaction and dataset preference checks passed');
