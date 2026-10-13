// Run with: node annotation_tool/tests/test_boundary_intervals.js
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
const elements = new Map();
vm.runInNewContext(source.replace('safely(init);', '') + `
function check(value, message) { if (!value) throw new Error(message); }
function rejects(action) { let failed=false; try { action(); } catch { failed=true; } check(failed, 'Invalid boundary must fail'); }
renderEditor = renderRunList = saveStatus = toast = () => {};
state.id = 'run';
state.project = {clips: [{id: 'run', metadata: {frame_count: 20}}], dances: []};
state.draft = {start_frame: 5, end_frame: 15, status: 'accepted', uncertain: false,
  direction_deg: 90, keyframes: [5,15].map(frame => ({frame, bbox:[0,0,10,10], orientation_deg:null, uncertain:false}))};
check(covered(state.draft), 'Legacy exact annotations still covered');
setBoundaryMode('start', 'interval');
setBound('start_non_waggle_frame', 1);
check(state.draft.end_non_waggle_frame == null, 'Start interval is independent');
setBoundaryMode('end', 'interval');
setBound('end_non_waggle_frame', 19);
check(covered(state.draft) && state.draft.status === 'accepted', 'Only definite core requires boxes');
check(boundaryUncertain(state.draft, 2) && boundaryUncertain(state.draft, 18), 'Both transitions uncertain');
check(!boundaryUncertain(state.draft, 5) && !boundaryUncertain(state.draft, 15), 'Waggle anchors definite');
check(sample(state.draft, 1) === null && sample(state.draft, 19) === null, 'Non-waggle anchors have no positive boxes');
check(sample(state.draft, 2).provenance === 'held', 'Transitions can preview available boxes');
rejects(() => setBound('start_non_waggle_frame', 5));
rejects(() => setBound('end_frame', 19));
rejects(() => setBound('start_frame', ''));
check(state.draft.start_non_waggle_frame === 1 && state.draft.end_frame === 15, 'Failed edits are atomic');
setBoundaryMode('start', 'exact');
check(state.draft.start_frame === 5 && state.draft.start_non_waggle_frame === null, 'Exact keeps definite endpoint');
check(state.draft.end_non_waggle_frame === 19, 'Other interval retained');
setBoundaryMode('end', 'not-visible');
check(state.draft.end_not_visible && state.draft.end_non_waggle_frame === null, 'Not visible clears interval');
setBoundaryMode('end', 'exact');
check(!state.draft.end_not_visible, 'Exact clears missing boundary flag');
setBound('start_frame', 0);
rejects(() => setBoundaryMode('start', 'interval'));
check(state.draft.start_non_waggle_frame === null, 'No invented marker outside clip');
`, {
  window: {ANNOTATION_CONFIG: {}, addEventListener() {}},
  document: {addEventListener() {}, getElementById(id) {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  }}, setTimeout: () => 1, clearTimeout() {},
});
console.log('Optional boundary interval checks passed');
