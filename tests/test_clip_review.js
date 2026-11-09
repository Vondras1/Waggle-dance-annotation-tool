// Run with: node annotation_tool/tests/test_clip_review.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const pending = [];
const draws = [], clears = [];
function node() {
  return {value:'', checked:false, replaceChildren() {}, append() {},
    addEventListener() {}, setAttribute() {}};
}
const canvas = {width:100, height:80, getContext:() => ({
  clearRect: (...args) => clears.push(args),
  drawImage: image => draws.push(image),
})};
elements.set('review-canvas', canvas);
const context = vm.createContext({
  window: {ANNOTATION_CONFIG: {}},
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  }, createElement:node},
  Image: class {
    naturalWidth = 100;naturalHeight = 80;
    set src(value) { this.url = value;pending.push(this); }
  },
  assert, pending, draws, clears,
});
vm.runInContext(fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8').replace('safely(init);', ''), context);
vm.runInContext(`
(async () => {
  const fullRender = renderReview;
  let fullRenders = 0;
  renderReview = () => { fullRenders++; };pauseReview = () => {};
  state.project = {project_id:'project'};
  const metadata = {frame_count:5, fps:10, run:{run_id:1},
    source_frame_times_s:[0,.1,.2,.3,.4], output_frame_times_s:[0,.1,.2,.3,.4]};
  state.clipReview = {root:'/clips', labels_token:null, clips:[{id:'clip', metadata, label:null}]};
  state.reviewId = 'clip';$('review-filter').value = 'all';
  const displayed = {naturalWidth:100, naturalHeight:80};
  state.reviewImage = displayed;state.reviewLoadedFrame = 0;
  $('review-frame-help').textContent = 'Original clip · choose Run, No run, or Unsure.';
  fullRender();
  assert.equal($('review-yes').disabled, false);
  const older = seekReview(1), newer = seekReview(2);
  assert.equal(fullRenders, 0, 'Playback must not rebuild the list and label controls for each frame');
  assert.equal($('review-frame-help').textContent, 'Original clip · choose Run, No run, or Unsure.',
    'The help text stays stable between frames');
  fullRender();
  assert.equal($('review-yes').disabled, false, 'Labels stay enabled while the next frame loads');
  assert.equal(clears.length, 0, 'Loading frames must not blank the canvas');
  assert.equal(draws.at(-1), displayed, 'Keep the last frame visible until the next is ready');
  assert.ok(pending[1].url.includes('/api/review/frame/clip/2?'));
  pending[1].onload();await newer;
  const image = state.reviewImage;
  pending[0].onload();await older;
  assert.equal(fullRenders, 0, 'Completing a playback frame does not rebuild the controls');
  assert.equal(state.reviewLoadedFrame, 2, 'An older request cannot replace a newer frame');
  assert.equal(state.reviewImage, image);
  assert.equal(draws.at(-1), image, 'Display the newly decoded frame');
  assert.equal(clears.length, 0, 'Decoded frames replace the image without clearing');
  const previousFolderFrame = seekReview(3);
  state.reviewImageToken++;
  state.reviewImage = null;state.reviewLoadedFrame = -1;
  pending[2].onload();await previousFolderFrame;
  assert.equal(state.reviewImage, null, 'An old folder cannot restore its frame after switching');
  drawReviewFrame();
  assert.equal(clears.length, 1, 'Clear the previous clip when its image is discarded');

  state.reviewFrame = state.reviewLoadedFrame = 0;
  state.reviewImage = displayed;
  state.reviewSaving = true;
  assert.equal(await navigateReview(1), undefined, 'Navigation is blocked during a save');
  state.reviewSaving = false;
  $('review-filter').value = 'unreviewed';$('review-auto-next').checked = true;
  let selection = 'not called', saves = 0;
  selectReview = async id => { selection = id; };
  saveStatus = () => {};
  api = async (path, method, payload) => {
    saves++;
    assert.equal(path, '/api/review');assert.equal(method, 'PUT');
    assert.equal(payload.clip_id, 'clip');assert.equal(payload.label, 'yes');
    return {json:async () => ({...state.clipReview, labels_token:'saved',
      clips:[{id:'clip', metadata, label:'yes'}]})};
  };
  state.clipReview.clips[0].label_source = 'run_review';
  fullRender();
  assert.equal($('review-yes').disabled, true, 'Inherited labels are changed in mode 01');
  await labelReview('yes');
  assert.equal(saves, 0, 'Inherited labels cannot be overwritten by shortcuts');
  state.clipReview.clips[0].label_source = 'manual';
  await labelReview('yes');
  assert.equal(saves, 1);assert.equal(state.reviewSaving, false);
  assert.equal(selection, undefined, 'Completing the final filtered clip clears the selection');
  assert.equal(reviewClips().length, 0);
  state.clipReview.clips[0].label = null;
  api = async () => { throw new Error('Clip labels changed'); };
  await assert.rejects(labelReview('no'), /labels changed/);
  assert.equal(state.reviewSaving, false, 'Save failures release controls');
  assert.equal(state.clipReview.clips[0].label, null, 'Failed saves do not change local labels');

  const draft = {notes:'unsaved annotation'};
  state.draft = draft;state.dirty = true;state.reviewFrame = 3;
  state.reviewImage = displayed;
  saveRun = async () => { throw new Error('Reload must never save annotations'); };
  selectReview = async () => { throw new Error('Reload must not reset the current frame'); };
  api = async (path, method, body) => {
    assert.equal(path, '/api/review');
    assert.equal(method, undefined, 'Reload uses the default GET method');
    assert.equal(body, undefined, 'Reload sends no annotation data');
    return {json:async () => ({...state.clipReview,
      clips:[{id:'clip', metadata, label:'no', label_source:'run_review'}]})};
  };
  await reloadReviewLabels();
  assert.equal(state.clipReview.clips[0].label, 'no');
  assert.equal(state.reviewFrame, 3);
  assert.equal(state.reviewImage, displayed);
  assert.equal(state.draft, draft);
  assert.equal(state.dirty, true, 'Unsaved annotations stay unsaved');
})()
`, context).then(() => console.log('Manual clip review race, filter, and save conflict checks passed'))
  .catch(error => { console.error(error);process.exitCode = 1; });
