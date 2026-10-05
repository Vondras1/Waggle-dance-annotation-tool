// Run with: node annotation_tool/tests/test_uncertain_run.js
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
const elements = new Map();
const context = {
  window: {ANNOTATION_CONFIG: {}, addEventListener() {}},
  document: {
    addEventListener() {},
    querySelector: () => ({}),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {blur() {}});
      return elements.get(id);
    },
  },
  setTimeout: () => 1, clearTimeout() {},
};
vm.runInNewContext(source.replace('safely(init);', '') + `
(async () => {
  function check(value, message) { if (!value) throw new Error(message); }
  async function rejects(action, pattern) {
    let failure;
    try { await action(); } catch (e) { failure = e; }
    check(failure && pattern.test(failure.message), 'Expected validation error: ' + pattern);
  }
  const empty = () => ({start_frame: 0, end_frame: 4, status: 'unreviewed',
    uncertain: true, direction_deg: null, keyframes: [], excluded_frames: []});
  const keys = [0, 4].map(frame => ({frame, bbox: [0,0,10,10], orientation_deg: null, uncertain: true}));
  const handlers = new Map();
  on = (id, event, action) => handlers.set(id + ':' + event, action);
  bindEvents();
  pause = renderRunList = renderEditor = renderRunSelector = saveStatus = toast = () => {};
  loadFrame = async () => {};
  let saves = 0;
  saveRun = async () => {
    if (state.dirty) {
      current().annotation = clone(state.draft);
      state.dirty = false;
      saves++;
    }
  };
  function reset(annotation = empty()) {
    state.project = {dances: [], clips: [
      {id: 'first', metadata: {run: {run_id: 1}}, annotation: clone(annotation)},
      {id: 'second', metadata: {run: {run_id: 2}}, annotation: empty()},
    ]};
    state.id = 'first'; state.draft = clone(annotation); state.dirty = false;
    state.conflict = false; saves = 0;
  }

  reset();
  await finishClip();
  check(saves === 1 && state.id === 'second', 'Save & next saves before navigating');
  const saved = state.project.clips[0].annotation;
  check(saved.status === 'accepted' && saved.uncertain, 'Uncertain review completes');
  check(saved.direction_deg === null && saved.keyframes.length === 0, 'No invented annotations');

  reset();
  await handlers.get('status-accepted:click')();
  check(state.draft.status === 'accepted' && saves === 1, 'Accept permits uncertain incomplete run');
  await selectRun('second');
  check(state.id === 'second', 'Manual navigation permits uncertain run without direction');

  reset({...empty(), status: 'accepted'});
  edited(a => { a.notes = 'Occluded'; });
  check(state.draft.status === 'accepted', 'Editing uncertain incomplete run keeps completion');
  handlers.get('run-uncertain:change')({target: {checked: false}});
  check(state.draft.status === 'unreviewed', 'Removing uncertainty restores endpoint requirement');
  await rejects(finishClip, /endpoint/);
  await rejects(handlers.get('status-accepted:click'), /first and last/);

  reset({...empty(), uncertain: false, keyframes: keys});
  await rejects(finishClip, /direction/);
  check(saves === 0, 'Uncertain frame flags do not waive whole-run requirements');
  state.draft.status = 'accepted';
  await rejects(() => selectRun('second'), /direction/);
  check(state.id === 'first', 'Certain accepted run still requires direction for navigation');
  handlers.get('run-uncertain:change')({target: {checked: true}});
  await selectRun('second');
  check(state.id === 'second', 'Marking run uncertain unblocks navigation');

  reset({...empty(), uncertain: false, direction_deg: 90, keyframes: keys});
  await finishClip();
  check(state.id === 'second' && saves === 1, 'Normal completed run still saves and advances');
})()
`, context).then(() => console.log('Uncertain run completion and navigation checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
