"use strict";

const CONFIG = window.ANNOTATION_CONFIG;

const $ = id => document.getElementById(id);
const clone = value => JSON.parse(JSON.stringify(value));
const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));
const angle = value => ((value + 180) % 360 + 360) % 360 - 180;
const fmt = value => Number(value).toFixed(3);
const state = {project: null, id: null, draft: null, frame: 0, loadedFrame: -1,
  image: null, playbackRate: 1, imageToken: 0, tool: "box", drag: null, dirty: false, version: 0,
  saving: null, timer: null, conflict: false, playing: false, playToken: 0,
  view: "runs", switching: false, selected: new Set(), combImage: null, combPoints: [], dancePending: 0,
  brightness: 100, histogramContrast: 0, enhancedFrameCache: null};
let danceQueue = Promise.resolve();
let navigationQueue = Promise.resolve();
let toastTimer;
const current = () => state.project?.clips.find(c => c.id === state.id);
const meta = () => current()?.metadata;
const memberDance = id => state.project?.dances.find(d => d.run_ids.includes(id));
const sourceId = run => run.source_clip_id || run.id;
const siblings = () => state.project.clips.filter(c => sourceId(c) === sourceId(current())).sort((a,b) => (a.run_number || 1)-(b.run_number || 1));
const physicalClips = () => state.project.clips.filter(c => sourceId(c) === c.id);
const runLabel = c => `Run ${String(c.metadata.run.run_id).padStart(3,"0")}${(c.run_number || 1)>1 ? "."+c.run_number : ""}`;
const localAnnotation = c => c.id === state.id ? state.draft : c.annotation;
function lastRunKey(project = state.project) {
  return project ? `dance-annotation:last-run:${project.master_path}` : null;
}
function rememberRun() {
  try { if (state.id) localStorage.setItem(lastRunKey(), state.id); } catch (_) {}
}
function restoredRun(project) {
  try {
    const id = localStorage.getItem(lastRunKey(project));
    if (project.clips.some(c => c.id === id)) return id;
  } catch (_) {}
  return project.clips[0]?.id;
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, className, action) {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", () => safely(action));
  return node;
}
function error(message) {
  $("error-message").textContent = message;
  $("error-banner").hidden = false;
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, CONFIG.toastMs);
}
async function safely(action) {
  try { return await action(); } catch (e) { error(e.message || String(e)); }
}
async function api(path, method = "GET", body) {
  const options = {method, cache: "no-store", headers: {}};
  if (state.project?.project_id) options.headers["X-Annotation-Project"] = state.project.project_id;
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify({...body, project_id: state.project?.project_id});
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({error: `Request failed (${response.status})`}));
    const failure = new Error(detail.error);
    failure.status = response.status;
    throw failure;
  }
  return response;
}
function saveStatus(text, kind = "") {
  $("save-state").textContent = text;
  $("save-state").className = `save-state ${kind}`;
}
function covered(a) {
  const keys = a.keyframes, excluded = new Set(a.excluded_frames || []);
  let first = a.start_frame, last = a.end_frame;
  while (first <= last && excluded.has(first)) first++;
  while (last >= first && excluded.has(last)) last--;
  return first <= last && keys.length > 0 && keys[0].frame <= first && keys[keys.length - 1].frame >= last;
}
function edited(mutator) {
  if (!state.draft) return;
  const before = clone(state.draft);
  mutator(state.draft);
  state.draft.keyframes.sort((a, b) => a.frame - b.frame);
  if (state.draft.status === "accepted" && !covered(state.draft)) {
    if (memberDance(state.id)) {
      state.draft = before;
      error("This run belongs to a dance. Keep boxes covering its interval, or remove it from the dance first.");
      renderEditor();
      return;
    }
    state.draft.status = "unreviewed";
    toast("Returned to review: add box keyframes covering both interval endpoints.");
  }
  state.dirty = true;
  state.version++;
  saveStatus("Unsaved changes", "dirty");
  renderEditor();
  renderRunList();
  clearTimeout(state.timer);
  if (!state.conflict) state.timer = setTimeout(() => safely(saveRun), CONFIG.autosaveMs);
}
async function saveRun() {
  clearTimeout(state.timer);
  if (state.saving) return state.saving;
  if (!state.dirty) return;
  if (state.conflict) throw new Error("Reload the project before saving. Your local edits are still displayed.");
  state.saving = (async () => {
    while (state.dirty) {
      const version = state.version;
      const id = state.id;
      const snapshot = clone(state.draft);
      saveStatus("Saving…");
      try {
        const response = await api(`/api/runs/${encodeURIComponent(id)}`, "PUT",
          {annotation: snapshot, revision: state.project.revision, angle_convention: state.project.angle_convention});
        state.project = await response.json();
        if (state.version === version && state.id === id) {
          state.dirty = false;
          state.draft = clone(current().annotation);
        }
        renderRunList();
      } catch (e) {
        state.conflict = e.status === 409 || /revision|another|reload/i.test(e.message);
        saveStatus("Save failed · edits kept", "error");
        throw e;
      }
    }
    saveStatus("All changes saved");
    $("save-confirmation").textContent = `${runLabel(current())} saved to disk at ${new Date().toLocaleTimeString()}`;
  })();
  try { await state.saving; } finally { state.saving = null; }
}
function sample(a, frame) {
  if (frame < a.start_frame || frame > a.end_frame || a.status === "rejected" || (a.excluded_frames || []).includes(frame)) return null;
  const keys = a.keyframes;
  if (!keys.length) return null;
  const exact = keys.find(k => k.frame === frame);
  if (exact) return {...clone(exact), provenance: "keyframe"};
  const after = keys.findIndex(k => k.frame > frame);
  if (after <= 0) return {...clone(keys[after === 0 ? 0 : keys.length - 1]), frame, provenance: "held"};
  const left = keys[after - 1], right = keys[after];
  const t = (frame - left.frame) / (right.frame - left.frame);
  let orientation = null;
  if (left.orientation_deg !== null && right.orientation_deg !== null) {
    const delta = ((right.orientation_deg - left.orientation_deg + 540) % 360) - 180;
    orientation = angle(left.orientation_deg + delta * t);
  }
  return {frame, bbox: left.bbox.map((v, i) => v + t * (right.bbox[i] - v)),
    orientation_deg: orientation, uncertain: left.uncertain || right.uncertain, provenance: "interpolated"};
}
function putKey(a, key) {
  a.keyframes = a.keyframes.filter(k => k.frame !== key.frame);
  const {frame, bbox, orientation_deg, uncertain} = key;
  a.keyframes.push({frame, bbox, orientation_deg, uncertain});
  // Old versions saved persistent deletion markers. Restore normal interpolation
  // wherever the updated anchors cover those frames (without extrapolating).
  const first = Math.min(...a.keyframes.map(k => k.frame));
  const last = Math.max(...a.keyframes.map(k => k.frame));
  a.excluded_frames = (a.excluded_frames || []).filter(f => f < first || f > last);
}
function editCurrentKey(mutator) {
  if (state.loadedFrame !== state.frame) return;
  const key = sample(state.draft, state.frame);
  if (!key) { error("Draw a bounding box on this frame first."); return; }
  edited(a => { mutator(key); putKey(a, key); });
}
function renderRunList() {
  const p = state.project;
  if (!p) return;
  const search = $("run-search").value.trim().toLowerCase();
  const filter = $("run-filter").value;
  const list = $("run-list");
  list.replaceChildren();
  let reviewed = 0;
  for (const clip of physicalClips()) {
    const runs = p.clips.filter(c => sourceId(c) === clip.id);
    const annotations = runs.map(localAnnotation);
    const status = annotations.some(a=>a.status==="unreviewed") ? "unreviewed" : annotations.some(a=>a.status==="accepted") ? "accepted" : "rejected";
    if (status !== "unreviewed") reviewed++;
    if (!`${clip.id} ${clip.metadata.run.run_id}`.toLowerCase().includes(search)) continue;
    if (filter === "uncertain" ? !annotations.some(a=>a.uncertain) : filter !== "all" && status !== filter) continue;
    const selected = current() && sourceId(current()) === clip.id;
    const card = button("", `run-card ${status}${selected ? " selected" : ""}`, () => selectRun(selected ? state.id : clip.id));
    const title = el("span", undefined, "run-card-title");
    title.append(el("span", `Clip ${String(clip.metadata.run.run_id).padStart(3,"0")}`),
      el("span", status === "accepted" ? "✓" : status === "rejected" ? "×" : "○", "status-symbol"));
    const times = clip.metadata.source_frame_times_s;
    const first = Math.min(...annotations.map(a=>a.start_frame)), last = Math.max(...annotations.map(a=>a.end_frame));
    card.append(title, el("span", `${fmt(times[first])} – ${fmt(times[last])} s`, "run-card-meta"),
      el("span", `${runs.length} run${runs.length===1 ? "" : "s"} · ${annotations.reduce((n,a)=>n+a.keyframes.length,0)} keyframes`, "run-card-meta"));
    list.append(card);
  }
  if (!list.children.length) list.append(el("p", "No matching runs.", "empty-state"));
  $("run-count").textContent = physicalClips().length;
  $("review-progress-label").textContent = `${reviewed} of ${physicalClips().length} clips reviewed`;
  const percent = Math.round(100 * reviewed / Math.max(1, physicalClips().length));
  $("review-progress-percent").textContent = `${percent}%`;
  $("review-progress").value = percent;
  $("project-location").textContent = p.clips_dir.split("/").slice(-2).join("/");
  $("project-location").title = `${p.clips_dir}\nSaved to ${p.master_path}`;
  $("save-location").textContent = p.master_path;
}
function inputValue(id, value) {
  if (document.activeElement !== $(id)) $(id).value = value ?? "";
}
const BRIGHTNESS_STORAGE_KEY = "dance-annotation:brightness";
function setBrightness(value) {
  const brightness = clamp(Math.round(Number(value) || 100), 100, 180);
  state.brightness = brightness;
  $("frame-brightness").value = brightness;
  $("frame-brightness-value").textContent = `${brightness}%`;
  try { localStorage.setItem(BRIGHTNESS_STORAGE_KEY, String(brightness)); } catch (_) {}
  drawFrame();
}
function restoreBrightness() {
  let brightness = 100;
  try {
    const saved = Number(localStorage.getItem(BRIGHTNESS_STORAGE_KEY));
    if (Number.isFinite(saved)) brightness = saved;
  } catch (_) {}
  setBrightness(brightness);
}

const HISTOGRAM_CONTRAST_STORAGE_KEY = "dance-annotation:histogram-contrast";

function setHistogramContrast(value) {
  const strength = clamp(Math.round(Number(value) || 0), 0, 100);
  state.histogramContrast = strength;
  state.enhancedFrameCache = null;
  $("histogram-contrast").value = strength;
  $("histogram-contrast-value").textContent = `${strength}%`;
  try { localStorage.setItem(HISTOGRAM_CONTRAST_STORAGE_KEY, String(strength)); } catch (_) {}
  drawFrame();
}

function restoreHistogramContrast() {
  let strength = 0;
  try {
    const saved = Number(localStorage.getItem(HISTOGRAM_CONTRAST_STORAGE_KEY));
    if (Number.isFinite(saved)) strength = saved;
  } catch (_) {}
  setHistogramContrast(strength);
}

/*
 * Histogram-derived contrast stretch for annotation display only.
 *
 * We use the 1st and 99th luminance percentiles instead of full histogram
 * equalization. This expands useful dark-tone differences while avoiding
 * extreme pixels dominating the mapping and is visually more stable between
 * neighboring video frames.
 *
 * The processed image is cached because drawFrame() is also called while
 * dragging annotation geometry.
 */
function enhancedFrameImage() {
  if (!state.image || state.histogramContrast <= 0) return state.image;

  const cached = state.enhancedFrameCache;
  if (cached &&
      cached.frame === state.loadedFrame &&
      cached.strength === state.histogramContrast &&
      cached.image === state.image) {
    return cached.canvas;
  }

  const width = state.image.naturalWidth || state.image.width;
  const height = state.image.naturalHeight || state.image.height;
  const offscreen = document.createElement("canvas");
  offscreen.width = width;
  offscreen.height = height;
  const offctx = offscreen.getContext("2d", {willReadFrequently: true});
  offctx.drawImage(state.image, 0, 0, width, height);

  const imageData = offctx.getImageData(0, 0, width, height);
  const pixels = imageData.data;
  const histogram = new Uint32Array(256);
  let count = 0;

  // The camera frames are grayscale, but luminance also keeps this safe if an
  // RGB frame is supplied later.
  for (let i = 0; i < pixels.length; i += 4) {
    const y = Math.round(
      0.2126 * pixels[i] +
      0.7152 * pixels[i + 1] +
      0.0722 * pixels[i + 2]
    );
    histogram[y]++;
    count++;
  }

  const tail = count * 0.01;
  let cumulative = 0, low = 0, high = 255;
  for (let value = 0; value < 256; value++) {
    cumulative += histogram[value];
    if (cumulative >= tail) { low = value; break; }
  }
  cumulative = 0;
  for (let value = 255; value >= 0; value--) {
    cumulative += histogram[value];
    if (cumulative >= tail) { high = value; break; }
  }

  if (high > low + 1) {
    const scale = 255 / (high - low);
    const strength = state.histogramContrast / 100;

    for (let i = 0; i < pixels.length; i += 4) {
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
      const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      const stretched = clamp((y - low) * scale, 0, 255);
      const target = y + (stretched - y) * strength;
      const delta = target - y;

      pixels[i]     = clamp(Math.round(r + delta), 0, 255);
      pixels[i + 1] = clamp(Math.round(g + delta), 0, 255);
      pixels[i + 2] = clamp(Math.round(b + delta), 0, 255);
    }
    offctx.putImageData(imageData, 0, 0);
  }

  state.enhancedFrameCache = {
    frame: state.loadedFrame,
    strength: state.histogramContrast,
    image: state.image,
    canvas: offscreen,
  };
  return offscreen;
}
function renderEditor() {
  if (!current()) return;
  const m = meta(), a = state.draft, key = sample(a, state.frame);
  const ready = state.loadedFrame === state.frame;
  $("current-run-title").textContent = runLabel(current());
  renderRunSelector();
  $("delete-run").disabled = sourceId(current()) === state.id;
  $("add-run").disabled = !state.project.supports_multiple_runs;
  $("current-run-subtitle").textContent = `${m.frame_count} frames · ${m.fps} fps · ${m.width} × ${m.height} px crop`;
  $("canvas-dimensions").textContent = `${m.width} × ${m.height} PX`;
  for (const id of ["frame-number", "frame-slider", "start-frame", "end-frame"]) $(id).max = m.frame_count - 1;
  inputValue("frame-number", state.frame);
  $("frame-slider").value = state.frame;
  $("frame-total").textContent = `/ ${m.frame_count - 1}`;
  $("frame-time").textContent = `Source ${fmt(m.source_frame_times_s[state.frame])} s · output ${fmt(m.output_frame_times_s[state.frame])} s`;
  inputValue("start-frame", a.start_frame); inputValue("end-frame", a.end_frame);
  $("interval-duration").textContent = `${fmt(m.source_frame_times_s[a.end_frame] - m.source_frame_times_s[a.start_frame])} s`;
  inputValue("run-direction", a.direction_deg === null ? "" : Number(a.direction_deg.toFixed(1)));
  $("frame-uncertain").checked = !!key?.uncertain;
  $("run-uncertain").checked = a.uncertain;
  inputValue("run-notes", a.notes);
  $("frame-uncertain").disabled = !key || !ready;
  $("add-keyframe").disabled = !key || !ready || key.provenance === "keyframe";
  $("delete-box").disabled = !ready || (!key && !(a.excluded_frames || []).includes(state.frame));
  $("current-keyframe-status").textContent = key ? key.provenance : (a.excluded_frames || []).includes(state.frame) ? "Box deleted" : "No box";
  $("current-keyframe-status").className = `pill ${key?.provenance || ""}`;
  $("frame-kind").hidden = !key || state.loadedFrame !== state.frame;
  $("frame-kind").textContent = key ? `${key.provenance}${key.uncertain || a.uncertain ? " · uncertain" : ""}` : "";
  $("frame-kind").className = `canvas-tag ${key?.provenance || ""}`;
  $("outside-interval").hidden = state.frame >= a.start_frame && state.frame <= a.end_frame;
  $("outside-interval").textContent = "Outside the selected run interval";
  $("box-coordinate").textContent = key ? `Crop: ${key.bbox.map(v => v.toFixed(1)).join(", ")}\nOriginal: ${key.bbox.map((v, i) => (v + (i % 2 ? m.run.bbox_y_min_px : m.run.bbox_x_min_px)).toFixed(1)).join(", ")}` : (a.excluded_frames || []).includes(state.frame) ? "Legacy deleted box. Edit a surrounding keyframe or use Delete box to restore interpolation." : "Draw the bee's bounding box.";
  for (const status of ["unreviewed", "accepted", "rejected"]) $("status-" + status).classList.toggle("active", a.status === status);
  $("accept-help").textContent = covered(a) ? "Boxes cover both interval endpoints. Ready for review." : "To accept, add boxes covering both interval endpoints.";
  const denominator = Math.max(1, m.frame_count - 1);
  $("interval-band").style.left = `${100 * a.start_frame / denominator}%`;
  $("interval-band").style.width = `${100 * (a.end_frame - a.start_frame) / denominator}%`;
  $("keyframe-markers").replaceChildren(); $("keyframe-list").replaceChildren();
  for (const k of a.keyframes) {
    const marker = button("", `keyframe-marker${k.frame === state.frame ? " current" : ""}`, () => seek(k.frame));
    marker.style.left = `${100 * k.frame / denominator}%`;
    marker.title = `Keyframe ${k.frame}`; marker.setAttribute("aria-label", marker.title);
    $("keyframe-markers").append(marker);
    $("keyframe-list").append(button(`${k.frame}${k.uncertain ? " ?" : ""}`, `keyframe-chip${k.frame === state.frame ? " active" : ""}`, () => seek(k.frame)));
  }
  drawFrame();
}
function renderRunSelector(force = false) {
  const select = $("clip-run-select");
  // Native selects can stay open while video/network callbacks render the editor.
  if (!force && document.activeElement === select) return;
  const runs = siblings();
  if ([...select.options].map(o => o.value).join("|") !== runs.map(r => r.id).join("|")) {
    select.replaceChildren(...runs.map(run => {
      const option = el("option");option.value = run.id;return option;
    }));
  }
  runs.forEach((run, i) => { select.options[i].textContent = `Run ${run.run_number || 1} · ${localAnnotation(run).status}`; });
  select.value = state.id;
  const tabs = $("clip-run-tabs");
  if ([...tabs.children].map(node => node.dataset.runId).join("|") !== runs.map(run => run.id).join("|")) {
    tabs.replaceChildren(...runs.map(run => {
      const tab = button("", "clip-run-tab", () => selectRun(run.id));
      tab.dataset.runId = run.id;
      return tab;
    }));
  }
  runs.forEach((run, i) => {
    const tab = tabs.children[i];
    tab.textContent = `Run ${run.run_number || 1}`;
    tab.title = `${runLabel(run)} · ${localAnnotation(run).status}`;
    tab.classList.toggle("active", run.id === state.id);
    tab.setAttribute("aria-current", run.id === state.id ? "true" : "false");
  });
}
function runTransition(busy) {
  state.switching = busy;
  document.querySelector(".editor-section").inert = busy;
  document.querySelector(".properties-sidebar").inert = busy;
}
function selectRun(id) {
  // Capture the requested ID now; save completion must not read a reset select.
  const requested = String(id);
  pause();
  const switchRun = async () => {
    if (requested === state.id) { renderRunSelector(true); return; }
    runTransition(true);
    try {
      await saveRun();
      const target = state.project.clips.find(c => c.id === requested);
      if (!target) throw new Error("The selected run is no longer available. Reload the project.");
      state.imageToken++;
      state.id = requested;state.draft = clone(target.annotation);
      state.version++;state.frame = state.draft.start_frame;
      state.image = null;state.loadedFrame = -1;state.drag = null;
      // Replace all displayed values, including a field that previously had focus.
      for (const name of ["frame-number", "start-frame", "end-frame", "run-direction", "run-notes"]) $(name).blur();
      renderRunList();renderEditor();renderRunSelector(true);
      await loadFrame();
      rememberRun();
    } catch (e) {
      if (current()) renderRunSelector(true);
      throw e;
    } finally { runTransition(false); }
  };
  navigationQueue = navigationQueue.catch(() => {}).then(switchRun);
  return navigationQueue;
}
function deleteCurrentBox() {
  if (!current() || state.loadedFrame !== state.frame) return;
  pause();
  const frame = state.frame;
  edited(a => {
    a.keyframes = a.keyframes.filter(k => k.frame !== frame);
    // Removing an anchor leaves an ordinary frame, eligible for interpolation.
    a.excluded_frames = (a.excluded_frames || []).filter(f => f !== frame);
  });
}
async function loadFrame() {
  const token = ++state.imageToken, frame = state.frame, id = state.id;
  $("frame-loading").hidden = true;
  const image = new Image();
  try {
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error(`Could not decode frame ${frame} of ${id}.`));
      image.src = `/api/frame/${encodeURIComponent(id)}/${frame}?project_id=${encodeURIComponent(state.project.project_id)}`;
    });
    if (token !== state.imageToken || id !== state.id || frame !== state.frame) return;
    state.image = image; state.loadedFrame = frame;
    $("frame-loading").hidden = true;
    renderEditor();
  } catch (e) {
    if (token === state.imageToken) {
      $("frame-loading").hidden = false;
      $("frame-loading").textContent = e.message;
      pause();
      throw e;
    }
  }
}
async function seek(value, fromPlayback = false) {
  if (!current()) return;
  if (!fromPlayback) pause();
  const frame = clamp(Math.round(Number(value) || 0), 0, meta().frame_count - 1);
  state.frame = frame; state.drag = null;
  renderEditor();
  await loadFrame();
}
function pause() {
  state.playing = false; state.playToken++;
  $("play-toggle").textContent = "▶";
  $("play-toggle").setAttribute("aria-label", "Play");
}
async function togglePlay() {
  if (state.playing) { pause(); return; }
  if (!current()) return;
  if (state.frame === meta().frame_count - 1) await seek(0);
  state.playing = true;
  const token = ++state.playToken;
  $("play-toggle").textContent = "Ⅱ";
  $("play-toggle").setAttribute("aria-label", "Pause");
  while (state.playing && token === state.playToken) {
    const start = performance.now();
    if (state.frame >= meta().frame_count - 1) break;
    await seek(state.frame + 1, true);
    await new Promise(resolve => setTimeout(resolve, Math.max(0, 1000 / (meta().fps * state.playbackRate) - (performance.now() - start))));
  }
  if (token === state.playToken) pause();
}
function arrow(ctx, x, y, degrees, length, color, width = 2) {
  if (degrees === null || degrees === undefined) return;
  const rad = (degrees - 90) * Math.PI / 180, dx = Math.cos(rad), dy = Math.sin(rad);
  const endX = x + dx * length, endY = y + dy * length;
  ctx.save(); ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = width;
  ctx.setLineDash([]); ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(endX, endY); ctx.stroke();
  const head = Math.max(width * 3, 7);
  ctx.beginPath(); ctx.moveTo(endX, endY);
  ctx.lineTo(endX - dx * head + dy * head * .5, endY - dy * head - dx * head * .5);
  ctx.lineTo(endX - dx * head - dy * head * .5, endY - dy * head + dx * head * .5);
  ctx.closePath(); ctx.fill(); ctx.restore();
}
function drawFrame() {
  if (!current()) return;
  const canvas = $("frame-canvas"), m = meta(), ctx = canvas.getContext("2d");
  if (canvas.width !== m.width || canvas.height !== m.height) { canvas.width = m.width; canvas.height = m.height; }
  // Keep the last complete frame while the next request is pending.
  if (state.image && state.loadedFrame !== state.frame) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.image) return;
  const displayImage = enhancedFrameImage();
  ctx.save();
  ctx.filter = `brightness(${state.brightness / 100})`;
  ctx.drawImage(displayImage, 0, 0, canvas.width, canvas.height);
  ctx.restore();
  canvas.dataset.frame = String(state.loadedFrame);
  // The canvas belongs only to the selected run. Never leak another run's box
  // into this editor, even when their intervals share the same source frame.
  const drawing = state.draft;
  const key = sample(drawing, state.frame);
  const drag = state.drag;
  const box = drag?.box || key?.bbox;
  if (box) {
    const [x0, y0, x1, y1] = box;
    const uncertain = key?.uncertain || drawing.uncertain;
    ctx.strokeStyle = uncertain ? CONFIG.uncertainColor : key?.provenance === "keyframe" ? CONFIG.keyframeColor : CONFIG.interpolatedColor;
    ctx.lineWidth = 2;
    ctx.setLineDash(!drag && key?.provenance !== "keyframe" ? [7, 4] : []);
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0); ctx.setLineDash([]);
    ctx.fillStyle = ctx.strokeStyle;
    for (const [x, y] of [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]) ctx.fillRect(x-3, y-3, 6, 6);
    arrow(ctx, (x0+x1)/2, (y0+y1)/2, drawing.direction_deg, CONFIG.directionLength, CONFIG.directionColor);
  }
  if (drag && drag.mode !== "box") {
    const [x, y] = drag.start, [ex, ey] = drag.end;
    arrow(ctx, x, y, angle(Math.atan2(ey-y, ex-x)*180/Math.PI + 90), Math.hypot(ex-x,ey-y), CONFIG.directionColor);
  }
}
function point(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  return [clamp((event.clientX-rect.left)*canvas.width/rect.width, 0, canvas.width),
    clamp((event.clientY-rect.top)*canvas.height/rect.height, 0, canvas.height)];
}
function setTool(tool) {
  state.tool = tool; state.drag = null;
  for (const value of ["box", "direction"]) $("tool-"+value).classList.toggle("active", value === tool);
  $("tool-hint").textContent = tool === "box" ? "Drag to draw a box. Drag inside a box to move it; drag a corner to resize." : "Drag in the run's travel direction. The angle applies to the whole run.";
  drawFrame();
}
function beginDrag(event) {
  if (!current() || state.loadedFrame !== state.frame || event.button !== 0) return;
  if (state.frame < state.draft.start_frame || state.frame > state.draft.end_frame || state.draft.status === "rejected") {
    error("This frame is neutral. Mark the dancing interval first, and keep rejected clips neutral."); return;
  }
  pause(); event.preventDefault();
  const canvas = $("frame-canvas"), start = point(event, canvas), key = sample(state.draft, state.frame);
  state.drag = {mode: state.tool, start, end: start, key, frame: state.frame};
  if (state.tool === "box") {
    state.drag.operation = "draw";
    if (key) {
      const [x0,y0,x1,y1] = key.bbox, [x,y] = start;
      const radius = CONFIG.boxHitRadius * canvas.width / canvas.getBoundingClientRect().width;
      const corners = [[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
      const corner = corners.findIndex(p => Math.hypot(p[0]-x,p[1]-y) <= radius);
      if (corner >= 0) { state.drag.operation = "resize"; state.drag.anchor = corners[(corner+2)%4]; }
      else if (x >= x0 && x <= x1 && y >= y0 && y <= y1) state.drag.operation = "move";
    }
  }
  canvas.setPointerCapture(event.pointerId);
}
function moveDrag(event) {
  const canvas = $("frame-canvas"), end = point(event, canvas);
  $("cursor-coordinate").textContent = `${end[0].toFixed(0)}, ${end[1].toFixed(0)} px`;
  if (!state.drag) return;
  const d = state.drag; d.end = end;
  if (d.mode === "box") {
    if (d.operation === "move") {
      const b = d.key.bbox;
      const dx = clamp(end[0]-d.start[0], -b[0], canvas.width-b[2]);
      const dy = clamp(end[1]-d.start[1], -b[1], canvas.height-b[3]);
      d.box = [b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy];
    } else {
      const a = d.operation === "resize" ? d.anchor : d.start;
      d.box = [Math.min(a[0],end[0]),Math.min(a[1],end[1]),Math.max(a[0],end[0]),Math.max(a[1],end[1])];
    }
  }
  drawFrame();
}
function endDrag(event) {
  if (!state.drag) return;
  moveDrag(event);
  const d = state.drag; state.drag = null;
  if ($( "frame-canvas").hasPointerCapture(event.pointerId)) $("frame-canvas").releasePointerCapture(event.pointerId);
  if (d.mode === "box") {
    if (!d.box || d.box[2]-d.box[0] < 2 || d.box[3]-d.box[1] < 2) { drawFrame(); return; }
    edited(a => putKey(a, {frame:d.frame,bbox:d.box,orientation_deg:d.key?.orientation_deg ?? null,uncertain:d.key?.uncertain ?? false}));
  } else {
    if (Math.hypot(d.end[0]-d.start[0],d.end[1]-d.start[1]) < 3) return;
    const degrees = angle(Math.atan2(d.end[1]-d.start[1],d.end[0]-d.start[0])*180/Math.PI + 90);
    if (d.mode === "direction") edited(a => { a.direction_deg = degrees; });
  }
}

async function setView(view) {
  pause();
  await danceQueue.catch(() => {});
  await saveRun();
  state.view = view;
  $("runs-view").hidden = view !== "runs";
  $("comb-view").hidden = view !== "comb";
  for (const name of ["runs", "comb"]) {
    $("tab-"+name).classList.toggle("active", view === name);
    $("tab-"+name).setAttribute("aria-selected", String(view === name));
  }
  if (view === "comb") {
    renderComb(); renderDances();
    if (!state.combImage && state.project.comb.available) {
      const image = new Image();
      image.onload = () => { state.combImage = image; drawComb(); };
      image.onerror = () => error("The full-comb background could not be loaded. Run coordinates remain available.");
      image.src = `/api/comb?project_id=${encodeURIComponent(state.project.project_id)}`;
    }
  }
}
function visibleRuns() {
  const from = $("comb-time-start").value === "" ? -Infinity : Number($("comb-time-start").value);
  const to = $("comb-time-end").value === "" ? Infinity : Number($("comb-time-end").value);
  return state.project.clips.filter(c => c.annotation.status === "accepted" && c.frames.length &&
    c.frames[c.frames.length-1].source_time_s >= from && c.frames[0].source_time_s <= to &&
    (!$("comb-ungrouped").checked || !memberDance(c.id)));
}
function danceColor(id) {
  const palette = CONFIG.dancePalette;
  let hash = 0;
  for (const char of id || "") hash = ((hash * 31) + char.charCodeAt(0)) >>> 0;
  return palette[hash % palette.length];
}
function renderComb() {
  const runs = visibleRuns();
  const validIds = new Set(state.project.clips.filter(c => c.annotation.status === "accepted").map(c => c.id));
  state.selected = new Set([...state.selected].filter(id => validIds.has(id)));
  $("comb-count").textContent = `${runs.length} accepted runs visible`;
  $("selection-count").textContent = `${state.selected.size} RUN${state.selected.size === 1 ? "" : "S"} SELECTED`;
  const hiddenSelections = [...state.selected].filter(id => !runs.some(c => c.id === id)).length;
  if (hiddenSelections) $("selection-count").textContent += ` · ${hiddenSelections} outside filter`;
  $("create-dance").disabled = !state.selected.size;
  $("add-to-dance").disabled = !state.selected.size || !state.project.dances.length;
  $("comb-message").textContent = state.project.comb.available ? "Static calibrated comb background · coordinates in the full-resolution undistorted image." : `${state.project.comb.message} Showing a coordinate canvas.`;
  $("comb-message").className = `comb-message${state.project.comb.available ? "" : " fallback"}`;
  $("comb-run-list").replaceChildren();
  for (const c of runs) {
    const dance = memberDance(c.id);
    const node = button(`${runLabel(c)} · ${fmt(c.frames[0].source_time_s)} s${dance ? " · "+dance.name : ""}`,
      `comb-run-chip${state.selected.has(c.id) ? " selected" : ""}`, () => toggleSelection(c.id));
    node.setAttribute("aria-pressed", String(state.selected.has(c.id)));
    node.title = "Click to toggle selection; double-click to review this run";
    node.addEventListener("dblclick", () => safely(async () => { await setView("runs"); await selectRun(c.id); }));
    $("comb-run-list").append(node);
  }
  if (!runs.length) $("comb-run-list").append(el("p", "Accept annotated runs in Run review to see them here. Adjust time filters if needed.", "empty-state"));
  drawComb();
}
function toggleSelection(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  renderComb();
}
function drawComb() {
  if (!state.project) return;
  const canvas = $("comb-canvas"), info = state.project.comb;
  const width = info.width || Math.max(CONFIG.combWidth, ...state.project.clips.map(c => c.metadata.run.bbox_x_max_px));
  const height = info.height || Math.max(CONFIG.combHeight, ...state.project.clips.map(c => c.metadata.run.bbox_y_max_px));
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = CONFIG.combBackground; ctx.fillRect(0,0,width,height);
  if (state.combImage) {
    ctx.drawImage(state.combImage, 0,0,width,height);
    ctx.fillStyle = CONFIG.combDimOverlay; ctx.fillRect(0,0,width,height);
  } else {
    ctx.strokeStyle = CONFIG.combGridColor; ctx.lineWidth = 1;
    ctx.font = "14px monospace"; ctx.fillStyle = CONFIG.combGridTextColor;
    for (let x=0;x<width;x+=CONFIG.combGridSpacing) {ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();ctx.fillText(String(x),x+4,18);}
    for (let y=CONFIG.combGridSpacing;y<height;y+=CONFIG.combGridSpacing) {ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();ctx.fillText(String(y),4,y-4);}
  }
  state.combPoints = [];
  const scale = width / Math.max(CONFIG.combMinDisplayWidth, canvas.getBoundingClientRect().width);
  for (const c of visibleRuns()) {
    const selected = state.selected.has(c.id), dance = memberDance(c.id);
    const color = selected ? CONFIG.selectedRunColor : dance ? danceColor(dance.id) : CONFIG.ungroupedRunColor;
    const points = c.frames.map(f => [(f.bbox_original[0]+f.bbox_original[2])/2,(f.bbox_original[1]+f.bbox_original[3])/2]);
    state.combPoints.push({id:c.id,points});
    ctx.strokeStyle = color; ctx.lineWidth = (selected ? 4 : 2) * scale;
    ctx.beginPath(); points.forEach((p,i) => i ? ctx.lineTo(...p) : ctx.moveTo(...p));ctx.stroke();
    const middle = points[Math.floor(points.length/2)];
    ctx.beginPath();ctx.arc(...middle,(selected ? 8 : 5)*scale,0,2*Math.PI);ctx.fillStyle=color;ctx.fill();
    arrow(ctx,...middle,c.annotation.direction_deg,32*scale,color,2*scale);
    ctx.font = `${12*scale}px sans-serif`;
    const text = runLabel(c).replace("Run ", ""), x = middle[0]+10*scale, y=middle[1]-10*scale;
    ctx.lineWidth = 3*scale;ctx.strokeStyle=CONFIG.combBackground;ctx.strokeText(text,x,y);ctx.fillText(text,x,y);
    if (selected) {
      const b = c.frames[Math.floor(c.frames.length/2)].bbox_original;
      ctx.strokeStyle = color;ctx.lineWidth=1.5*scale;ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);
    }
  }
}
function saveDances(transform) {
  state.dancePending++;
  saveStatus("Saving dance…");
  const action = async () => {
    await saveRun();
    const dances = clone(state.project.dances);
    const result = transform(dances) || dances;
    const response = await api("/api/dances", "PUT", {dances:result, revision:state.project.revision});
    state.project = await response.json();
    saveStatus("All changes saved");
    renderDances(); renderComb(); renderRunList();
  };
  danceQueue = danceQueue.catch(() => {}).then(action).catch(e => {
    saveStatus("Dance save failed", "error");
    throw e;
  }).finally(() => { state.dancePending--; });
  return danceQueue;
}
function renderDances() {
  const list = $("dance-list"), target = $("dance-target"), previous = target.value;
  list.replaceChildren(); target.replaceChildren(el("option", "Choose a dance…"));target.firstChild.value="";
  $("dance-count").textContent = state.project.dances.length;
  for (const dance of state.project.dances) {
    const option = el("option", dance.name || dance.id);option.value=dance.id;target.append(option);
    const card = el("div", undefined, "dance-card");
    const heading = el("div", undefined, "dance-card-heading");
    const name = el("input");name.value=dance.name;name.maxLength=CONFIG.danceNameMaxLength;name.setAttribute("aria-label", "Dance name");
    name.addEventListener("change", () => safely(() => saveDances(ds => {ds.find(d=>d.id===dance.id).name=name.value;})));
    heading.append(name,button("×","icon-button danger",async () => {
      if (confirm(`Delete dance “${dance.name}”? Its runs will remain accepted and become ungrouped.`))
        await saveDances(ds => ds.filter(d=>d.id!==dance.id));
    }));
    heading.lastChild.setAttribute("aria-label", "Delete dance");
    card.append(heading);
    const members = el("div", undefined, "dance-members");
    for (const id of dance.run_ids) {
      const clip = state.project.clips.find(c=>c.id===id), member=el("span",undefined,"dance-member");
      member.append(button(runLabel(clip), "", () => toggleSelection(id)),
        button("×", "", () => saveDances(ds => ds.map(d=>d.id===dance.id ? {...d,run_ids:d.run_ids.filter(r=>r!==id)} : d).filter(d=>d.run_ids.length))));
      member.lastChild.title = "Remove run; removing the last run also deletes this empty dance";
      member.lastChild.setAttribute("aria-label", `Remove run ${clip.metadata.run.run_id} from dance`);
      members.append(member);
    }
    card.append(members);
    const uncertainLabel = el("label",undefined,"checkbox-field"), uncertain=el("input");
    uncertain.type="checkbox";uncertain.checked=dance.uncertain;
    uncertain.addEventListener("change",()=>safely(()=>saveDances(ds=>{ds.find(d=>d.id===dance.id).uncertain=uncertain.checked;})));
    uncertainLabel.append(uncertain,el("span","Uncertain dance"));card.append(uncertainLabel);
    const notes=el("textarea");notes.value=dance.notes;notes.placeholder="Dance notes…";notes.rows=2;notes.setAttribute("aria-label","Dance notes");
    notes.addEventListener("change",()=>safely(()=>saveDances(ds=>{ds.find(d=>d.id===dance.id).notes=notes.value;})));
    card.append(notes,button("Select all members","text-button",()=>{state.selected=new Set(dance.run_ids);renderComb();}));
    list.append(card);
  }
  if ([...target.options].some(o=>o.value===previous)) target.value=previous;
  if (!list.children.length) list.append(el("p","Select related runs on the comb to create your first dance.","empty-state"));
}
async function groupSelection(existing = false) {
  const ids = [...state.selected];
  if (!ids.length) throw new Error("Select at least one accepted run.");
  const target = existing ? $("dance-target").value : null;
  if (existing && !target) throw new Error("Choose a dance first.");
  for (const id of ids) {
    const dance=memberDance(id);
    if (dance && dance.id !== target) throw new Error(`Run ${id} already belongs to ${dance.name}. Remove its existing membership first.`);
  }
  const name = $("dance-name").value.trim() || `Dance ${String(state.project.dances.length+1).padStart(2,"0")}`;
  await saveDances(ds => {
    if (existing) { const d=ds.find(d=>d.id===target);d.run_ids=[...new Set([...d.run_ids,...ids])]; }
    else ds.push({id:`dance_${Date.now().toString(36)}_${Math.random().toString(36).slice(2,8)}`,name,run_ids:ids,notes:"",uncertain:false});
  });
  state.selected.clear();$("dance-name").value="";renderComb();toast(existing ? "Runs added to dance." : "Dance saved.");
}
function download(blob, filename) {
  const url=URL.createObjectURL(blob), link=el("a");link.href=url;link.download=filename;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),CONFIG.downloadRevokeMs);
}
async function exportYolo() {
  const stride=Number($("export-stride").value);
  if (!Number.isInteger(stride) || stride < 1) throw new Error("Frame stride must be a positive integer.");
  await saveRun();await danceQueue;
  $("export-download").disabled=true;$("export-state").textContent="Preparing images and labels…";
  try {
    const response=await api("/api/export","POST",{stride,include_uncertain:$("export-uncertain").checked,keyframes_only:$("export-keyframes").checked});
    download(await response.blob(),"waggle_yolo.zip");
    $("export-state").textContent="Download ready. Master JSON and provenance are included.";
  } catch(e) {$("export-state").textContent=e.message;throw e;}
  finally {$("export-download").disabled=false;}
}
async function reloadProject() {
  pause();
  if (state.conflict) {
    if (!confirm("Reload saved annotations and discard the unsaved local edits currently displayed?")) return;
    clearTimeout(state.timer);state.dirty=false;state.conflict=false;
  } else await saveRun();
  await danceQueue.catch(()=>{});
  const response=await api("/api/rescan","POST",{});
  state.project=await response.json();state.combImage=null;
  const id=state.project.clips.some(c=>c.id===state.id) ? state.id : state.project.clips[0].id;
  state.id=null;await selectRun(id);saveStatus("Project reloaded");
  if(state.view==="comb") await setView("comb");
  toast(`${physicalClips().length} clips available.`);
}
function openFolders() {
  pause();
  $("source-folder").value = state.project?.clips_dir || "";
  $("save-folder").value = state.project?.save_dir || "";
  $("folders-state").textContent = "";
  $("folders-dialog").showModal();
}
async function changeFolders(event) {
  event.preventDefault();
  if (!state.project?.project_id) throw new Error("Restart the server and refresh the browser to enable folder selection.");
  if ($("folders-apply").disabled) return;
  for (const id of ["folders-apply", "folders-close", "folders-cancel"]) $(id).disabled = true;
  $("folders-state").textContent = "Saving edits and checking folders…";
  try {
    await danceQueue;
    await saveRun();
    const response = await api("/api/project/open", "POST", {
      clips_dir: $("source-folder").value, save_dir: $("save-folder").value,
      revision: state.project.revision,
    });
    const project = await response.json();
    state.imageToken++;state.playToken++;
    state.project = project;state.id = null;state.draft = null;
    state.image = null;state.loadedFrame = -1;state.combImage = null;
    state.dirty = false;state.conflict = false;state.selected.clear();
    $("run-search").value = "";$("run-filter").value = "all";
    $("comb-time-start").value = "";$("comb-time-end").value = "";$("comb-ungrouped").checked = false;
    $("save-confirmation").textContent = "";$("error-banner").hidden = true;
    $("folders-dialog").close();
    await selectRun(project.clips[0].id);
    await setView("runs");
    saveStatus("Project opened");
    toast(`${physicalClips().length} clips opened. Annotations: ${project.master_path}`);
  } catch (e) {
    $("folders-state").textContent = e.message;
    throw e;
  } finally { for (const id of ["folders-apply", "folders-close", "folders-cancel"]) $(id).disabled = false; }
}
function setBound(which, value) {
  const number=Number(value), a=state.draft;
  if (!Number.isInteger(number) || number<0 || number>=meta().frame_count) throw new Error("Choose a frame inside this clip.");
  if ((which==="start_frame" && number>a.end_frame)||(which==="end_frame" && number<a.start_frame)) {
    if (!a.keyframes.length && a.status === "unreviewed") {
      edited(draft => { draft.start_frame = number; draft.end_frame = number; });return;
    }
    renderEditor();throw new Error("Start must be at or before end. Move the other boundary first.");
  }
  edited(a=>{a[which]=number;});
}
function readAngle(id) {
  if($(id).value==="") return null;
  const value=Number($(id).value);
  if(!Number.isFinite(value)) throw new Error("Enter a finite angle, or clear it for unknown.");
  return angle(value);
}
function on(id,event,action) {$(id).addEventListener(event,e=>safely(()=>action(e)));}
async function finishClip() {
  if (!state.draft) return;
  pause();
  if (state.draft.status !== "rejected") {
    if (!covered(state.draft)) throw new Error("Add boxes covering both dancing interval endpoints before completing this clip, or reject it.");
    edited(a => { a.status = "accepted"; });
  } else if (!state.dirty) {
    // Explicit completion always confirms a disk write, even for unchanged clips.
    edited(() => {});
  }
  const id = state.id;
  await saveRun();
  const remaining = siblings().find(c=>c.id!==id && c.annotation.status==="unreviewed");
  const i = physicalClips().findIndex(c => c.id === sourceId(current()));
  const next = remaining || physicalClips()[i + 1];
  toast(`${runLabel(current())} saved to disk.`);
  if (next) await selectRun(next.id);
}
async function addRun() {
  if (!current()) return;
  pause();
  $("add-run").disabled = true;
  try {
    await saveRun();
    const response = await api("/api/runs/add", "POST", {run_id:state.id,frame:state.frame,revision:state.project.revision});
    state.project = await response.json();
    await selectRun(state.project.selected_run_id);
    toast("New run added. Mark its end and draw its own box keyframes.");
  } finally { $("add-run").disabled = false; }
}
async function deleteAddedRun() {
  if (!current() || state.id === sourceId(current())) return;
  if (!confirm(`Delete ${runLabel(current())} and its annotations? Other runs in this clip will be kept.`)) return;
  pause();await saveRun();
  const source = sourceId(current());
  const response = await api("/api/runs/delete", "POST", {run_id:state.id,revision:state.project.revision});
  state.project = await response.json();state.id = null;state.draft = null;
  await selectRun(source);
  toast("Added run deleted.");
}
function bindEvents() {
  on("add-run", "click", addRun);
  on("delete-run", "click", deleteAddedRun);
  on("clip-run-select", "pointerdown", pause);
  on("clip-run-select", "keydown", pause);
  on("clip-run-select", "change", e => selectRun(e.target.value));
  on("clip-run-select", "blur", () => { if (current() && !state.switching) renderRunSelector(true); });
  on("folders-open", "click", openFolders);
  on("folders-dialog", "cancel", e => { if ($("folders-apply").disabled) e.preventDefault(); });
  on("folders-close", "click", () => $("folders-dialog").close());
  on("folders-cancel", "click", () => $("folders-dialog").close());
  on("folders-form", "submit", changeFolders);
  on("error-dismiss","click",()=>{$("error-banner").hidden=true;});
  on("run-search","input",renderRunList);on("run-filter","change",renderRunList);
  on("tab-runs","click",()=>setView("runs"));on("tab-comb","click",()=>setView("comb"));
  on("help-open","click",()=>$("help-dialog").showModal());on("export-open","click",()=>{$("export-state").textContent="";$("export-dialog").showModal();});
  on("export-download","click",exportYolo);on("reload-clips","click",reloadProject);
  on("master-download","click",async()=>{await saveRun();await danceQueue;download(await (await api("/api/master")).blob(),CONFIG.masterFilename);});
  on("save-run","click",async()=>{if (!state.dirty) edited(() => {});await saveRun();toast("Run saved to disk.");});
  on("save-next","click",finishClip);
  on("playback-speed","change",e=>{state.playbackRate=Number(e.target.value);});
  on("frame-brightness","input",e=>setBrightness(e.target.value));
  on("brightness-reset","click",()=>setBrightness(100));
  on("histogram-contrast","input",e=>setHistogramContrast(e.target.value));
  on("histogram-contrast-reset","click",()=>setHistogramContrast(0));
  on("reset-run","click",async()=>{
    clearTimeout(state.timer);if(state.saving) await state.saving;
    state.draft=clone(current().annotation);state.dirty=false;state.version++;renderEditor();renderRunList();saveStatus("Saved version restored");
  });
  for(const [id,delta] of [["previous-run",-1],["next-run",1]]) on(id,"click",()=>{
    const i=physicalClips().findIndex(c=>c.id===sourceId(current())),next=physicalClips()[i+delta];if(next)return selectRun(next.id);
  });
  for(const tool of ["box","direction"])on("tool-"+tool,"click",()=>setTool(tool));
  on("frame-previous","click",()=>seek(state.frame-1));on("frame-next","click",()=>seek(state.frame+1));
  on("frame-number","change",e=>seek(e.target.value));on("frame-slider","input",e=>seek(e.target.value));
  on("play-toggle","click",togglePlay);
  on("mark-start","click",()=>setBound("start_frame",state.frame));on("mark-end","click",()=>setBound("end_frame",state.frame));
  on("start-frame","change",e=>setBound("start_frame",e.target.value));on("end-frame","change",e=>setBound("end_frame",e.target.value));
  on("run-direction","change",()=>{const value=readAngle("run-direction");edited(a=>{a.direction_deg=value;});});
  on("clear-direction","click",()=>edited(a=>{a.direction_deg=null;}));
  on("frame-uncertain","change",e=>editCurrentKey(k=>{k.uncertain=e.target.checked;}));
  on("run-uncertain","change",e=>edited(a=>{a.uncertain=e.target.checked;}));
  on("run-notes","input",e=>edited(a=>{a.notes=e.target.value;}));
  on("add-keyframe","click",()=>editCurrentKey(()=>{}));
  on("delete-box", "click", deleteCurrentBox);
  for(const status of ["unreviewed","accepted","rejected"])on("status-"+status,"click",()=>{
    if(status==="accepted"&&!covered(state.draft))throw new Error("Add box keyframes covering the first and last run frames before accepting.");
    if(status!=="accepted"&&memberDance(state.id))throw new Error("Remove this run from its dance before changing its status.");
    edited(a=>{a.status=status;});
    return saveRun();
  });
  on("frame-canvas","pointerdown",beginDrag);on("frame-canvas","pointermove",moveDrag);on("frame-canvas","pointerup",endDrag);
  on("frame-canvas","pointercancel",()=>{state.drag=null;drawFrame();});
  for(const id of ["comb-time-start","comb-time-end","comb-ungrouped"])on(id,"change",renderComb);
  on("comb-select-visible","click",()=>{visibleRuns().forEach(c=>state.selected.add(c.id));renderComb();});
  on("comb-clear-selection","click",()=>{state.selected.clear();renderComb();});
  on("create-dance","click",()=>groupSelection());on("add-to-dance","click",()=>groupSelection(true));
  on("comb-canvas","pointermove",e=>{$("comb-coordinate").textContent=point(e,$("comb-canvas")).map(v=>v.toFixed(0)).join(", ")+" px";});
  on("comb-canvas","click",e=>{
    const canvas=$("comb-canvas"),p=point(e,canvas),max=CONFIG.combHitRadius*canvas.width/canvas.getBoundingClientRect().width;
    let nearest=null,distance=max;
    for(const run of state.combPoints)for(const q of run.points){const d=Math.hypot(q[0]-p[0],q[1]-p[1]);if(d<distance){distance=d;nearest=run.id;}}
    if(nearest)toggleSelection(nearest);
  });
  document.addEventListener("keydown",e=>{
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){e.preventDefault();safely(saveRun);return;}
    if(e.target.closest("input,textarea,select,dialog")||state.view!=="runs"||!state.draft||state.switching)return;
    const key=e.key.toLowerCase();
    const actions={"delete":deleteCurrentBox," ":togglePlay,"arrowleft":()=>seek(state.frame-(e.shiftKey?CONFIG.frameStep:1)),"arrowright":()=>seek(state.frame+(e.shiftKey?CONFIG.frameStep:1)),
      "[":()=>setBound("start_frame",state.frame),"]":()=>setBound("end_frame",state.frame),"b":()=>setTool("box"),"d":()=>setTool("direction")};
    if(actions[key]){e.preventDefault();safely(actions[key]);}
  });
  window.addEventListener("beforeunload",e=>{if(state.dirty||state.saving||state.dancePending){e.preventDefault();e.returnValue="";}});
  window.addEventListener("resize",()=>{if(state.view==="comb")drawComb();});
}
async function init() {
  bindEvents();
  restoreBrightness();
  restoreHistogramContrast();
  const response=await api("/api/project");
  const project=await response.json();
  if (!project.angle_convention?.startsWith("degrees clockwise from image-up;")) {
    $("frame-loading").hidden = true;
    saveStatus("Server restart required", "error");
    throw new Error("Restart the annotation server, then refresh this page to use the updated angle convention.");
  }
  if (!project.supports_box_exclusions) {
    $("frame-loading").hidden = true;
    saveStatus("Server restart required", "error");
    throw new Error("Restart the annotation server and refresh this page to load the run-switching and Delete box update.");
  }
  if (project.annotation_api_version !== 5) {
    $("frame-loading").hidden = true;
    saveStatus("Server restart required", "error");
    throw new Error("The running annotation server is outdated. Stop it, start python -m annotation_tool again, and refresh this page.");
  }
  state.project=project;
  if(!state.project.clips.length) {
    $("frame-loading").hidden = true;
    renderRunList();saveStatus("Choose project folders");openFolders();return;
  }
  await selectRun(restoredRun(state.project));
  saveStatus("All changes saved");
}
safely(init);
