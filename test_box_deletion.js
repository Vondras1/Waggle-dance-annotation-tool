// Run with: node annotation_tool/test_box_deletion.js
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/static/app.js`, 'utf8');
vm.runInNewContext(source.replace('safely(init);', '') + `
function check(value, message) { if (!value) throw new Error(message); }
function box(frame, x) { return {frame, bbox: [x, 0, x + 10, 10], orientation_deg: 0, uncertain: false}; }
state.id = "test";
state.project = {clips: [{id: "test"}]};
pause = () => {};
edited = fn => { fn(state.draft); state.draft.keyframes.sort((a,b) => a.frame-b.frame); };
state.frame = state.loadedFrame = 2;
state.draft = {start_frame: 0, end_frame: 4, status: 'unreviewed',
  keyframes: [box(0, 0), box(2, 50), box(4, 40)], excluded_frames: []};
deleteCurrentBox();
check(state.draft.excluded_frames.length === 0, 'Delete must not exclude the frame');
check(sample(state.draft, 2).bbox[0] === 20, 'Deleted anchor must interpolate');
putKey(state.draft, box(0, 10));
state.draft.keyframes.sort((a,b) => a.frame-b.frame);
check(sample(state.draft, 2).bbox[0] === 25, 'Editing an earlier box must update the deleted frame');
deleteCurrentBox();
check(sample(state.draft, 2).provenance === 'interpolated', 'Deleting interpolation must not create a hole');
state.draft.excluded_frames = [2];
putKey(state.draft, box(0, 0));
state.draft.keyframes.sort((a,b) => a.frame-b.frame);
check(sample(state.draft, 2).bbox[0] === 20, 'Earlier box must clear legacy deletion markers');
state.draft.excluded_frames = [2];
deleteCurrentBox();
check(sample(state.draft, 2) !== null, 'Delete must also restore a legacy excluded frame');
state.draft.keyframes = [box(2, 10)];
deleteCurrentBox();
check(sample(state.draft, 2) === null && state.draft.excluded_frames.length === 0, 'Deleting the only anchor leaves an ordinary empty frame');
`, {window: {ANNOTATION_CONFIG: {}}});
console.log('Box deletion regression checks passed');
