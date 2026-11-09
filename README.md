# Waggle dance annotation tool - work in progress

This local browser tool lets you review detector-generated waggle-run clips,
mark the bee with bounding boxes, group accepted runs into dances, and export a
dataset. It works from MP4 clips and their matching JSON sidecars.

![Waggle-run annotation interface](guide/screen_main.png)

## Install and start

From the parent directory containing `annotation_tool`, create and activate a Python 3.10+ environment, then
install the dependency:

```bash
conda create -n dance-annotation
conda activate dance-annotation
conda install -c conda-forge opencv
```

Now start the tool:
```bash
python -m annotation_tool
```

Open [http://localhost:8765](http://localhost:8765). The server listens only on
your computer. The dataset selected in `config.py` is loaded on startup.
To try the example, set `DEFAULT_DATASET = "example_dataset"`. Click **Folders** in the top bar
to change the source and save folders. If the default clips folder cannot be
found, choose these folders in the opening dialog.

![Choose clip and annotation folders](guide/choose_paths.png)

The source folder must contain matching MP4 and JSON clip pairs (see example in [dataset example](guide/example_dataset/)). The resulting annotations are
saved as `annotations/master.json` beside the clips by default; the original
clips and sidecars are never changed.

To set paths when starting the tool:

```bash
python -m annotation_tool \
  --clips-dir path/to/run_clips \
  --annotations path/to/annotations/master.json
```

Edit [config.py](config.py) to change the default clip locations, server port,
image/cache settings, and browser settings. Restart the server after editing it.
Command-line options take precedence over the defaults.

In `config.py`, select `DEFAULT_DATASET` and edit its entry in `DATASETS`:

```python
DEFAULT_DATASET = "prague_2026-09-08T14"
DATASETS = {
    "prague_2026-09-08T14": {
        "clips_dir": REPO / "dance_decoder/yowo_data/prague_2026-09-08T14/run_clips",
        "bbox_size": 64,  # Square side length in clip pixels; tune for this dataset.
    },
    # Add more datasets here.
}
```

Only the selected path is used; an existing folder elsewhere never overrides it.
Folders selected with **Folders** or `--clips-dir` use the size from the matching
`clips_dir` entry, or `DEFAULT_BBOX_SIZE` if unlisted.

For square annotations, edit **Square size** in the **Fixed bounding box** panel beside **Run in this clip** and enable
**Use fixed bbox size** underneath it. Click to place a square centered on the
pointer, or drag inside a box to move it. At image edges the square moves inward
without shrinking. A size larger than the clip is rejected; choose a smaller
size or disable fixed mode. Disabling it restores free drawing and resizing.
Existing boxes change only when you place or move them; changing this setting
does not resize all saved keyframes. Interpolation still uses the saved boxes.

The size and toggle are remembered per source folder in this browser. They do
not rewrite `config.py`; changing the configured size and restarting/refreshing
resets the browser preference to the new size with fixed mode off.

## Annotate a run ([Watch the annotation demo](guide/example_runs.webm))

1. Select a candidate and set its first and last dancing frames with **[** and
   **]**.
2. In box mode, draw boxes around the bee at both ends of the run. Boxes between
   keyframes are interpolated automatically. Redraw an interpolated box to make
   it a correction keyframe.
3. **Delete box** removes the keyframe on the current frame and returns that
   frame to normal interpolation from surrounding keyframes.
4. Check the automatic run direction, uncertainty, and notes, then accept the run or reject
   it as a false detection.

Run direction defaults to **Auto · annotated frames**. The PCA helper in `angles.py`
fits the centers of box keyframes within the definite waggle interval, in frame
order; first-to-last motion determines the arrow's sign. Moving/deleting boxes or
changing the interval recalculates it. At least two distinct positions are needed;
stationary or ambiguous trajectories leave the direction unset. The calculation
uses annotated keyframes, not interpolated frames or uncertain boundary transitions.
Choose **Manual** to enter an angle or draw it with **D**. Manual values stay fixed
when boxes change; returning to Auto recalculates the direction. Existing saved
angles without a mode are preserved as manual. The saved annotation records
`direction_mode` alongside `direction_deg`.

Boundary intervals are **optional**. Each boundary defaults to **Exact**; existing
annotations keep their exact frames. To mark an uncertain start, select **Interval
(optional)** for Start boundary, set the first definitely waggle frame, then set
the last definitely non-waggle frame before it. For an uncertain end, set the last
definitely waggle frame and the first definitely non-waggle frame after it. Use
**Set to current frame** or enter frame numbers. Start and end modes are independent;
**[** and **]** still set the definite waggle endpoints. Switching back to Exact
keeps the definite endpoint and removes that boundary's interval.

The timeline shows a solid definite portion and hatched transitions, with clickable
boundary markers. Only frames strictly between a non-waggle marker and its waggle
endpoint are ambiguous. Non-waggle markers themselves are never positive examples.
For a boundary outside the clip, choose **Not visible in clip** and mark the first
or last frame you can confidently identify as waggle. No missing boundary is inferred.
Boundary uncertainty does not require checking **Uncertain run**: box coverage is
required only over the definite portion, and the normal direction requirement remains.

Exports omit ambiguous transition frames by default, including their images. The
**Include uncertain runs, frames, and boundary transitions** option includes available
boxes on those frames; held boxes remain excluded. The manifest records
`boundary_uncertain` separately from other uncertainty. The master JSON (schema 5)
keeps `start_frame` / `end_frame` as inclusive definite waggle endpoints, with optional
`start_non_waggle_frame` / `end_non_waggle_frame` markers and `start_not_visible` /
`end_not_visible` flags. Older masters are upgraded with a backup on the next save.

Accepted runs normally need boxes at both interval endpoints. If a run cannot
be annotated reliably, check **Uncertain run**: direction and endpoint boxes
are optional, and **Accept** or **Save clip & next** completes the review with
whatever annotations are available. This exception applies to the whole-run
checkbox, not **Uncertain frame**. Unchecking it restores the normal requirements.
Uncertain runs are excluded from exports by default; explicitly including them
exports only available keyframes/interpolation, never missing or held boxes.

A clip may contain
several independent runs; use **+ Add run** to create another one. Use **Save
clip & next** to save and continue.

Keyboard controls: **←/→** step a frame, **Shift+←/→** step ten frames,
**Space** play/pause, **B** box mode, **D** direction mode, and **Ctrl/Cmd+S**
save.

<<<<<<< Updated upstream
## Group runs into dances - Not fully finished
=======
## 02 Run / no run: manually review clips

All three modes share the clips folder selected in **Folders** or `--clips-dir`.
Mode 02 shows the same clips as mode 01; it has no separate source path.
No detection models or predicted boxes are loaded.

Mode 01 annotations automatically determine the mode 02 label: any accepted run
makes the clip **Run**; if its accepted runs are all uncertain, the clip is **Unsure**.
A clip is **No run** only when every run in it is rejected. Partial reviews with no
accepted run leave the clip's manual label unchanged. Inherited labels show
**from 01** and must be changed in Run review. Switching to mode 02 refreshes them.
Inheritance reads `master.json` directly and does not overwrite manual labels.

For clips without an inherited label, choose **Run**, **No run**, or **Unsure**, or
press **1**, **2**, or **3**. Manual labels save to `annotations/clip_labels.json`
beside the shared clips folder. Automatic advance can be disabled. Filters show
each effective label or unreviewed clips. The **⟳** icon at the top of the left
sidebar rereads saved labels and Run review statuses without saving annotations
or changing the current frame.

Use play/pause, the frame number, the timeline, or **←/→** to navigate frames;
**Shift+←/→** steps ten frames. Playback speed ranges from 0.25× to 2×.
**Brightness** and **Hist. contrast** use the same display processing as mode 01,
with shared settings and reset buttons. Original clips and exports are unchanged.

Run/no-run labels describe whole clips; they do not create box annotations or
make clips eligible for YOLO export or dance grouping. Use mode 01 for those.

## 03 Dances: connect related runs

Group manually annotated, accepted runs from mode 01. Select related runs, name
the dance, and use **Group selected runs** or add them to an existing dance.
Dance names, notes, uncertainty, and memberships save in `master.json`.
**Download dances JSON** exports the dance memberships.
>>>>>>> Stashed changes

The full-comb view shows accepted runs in original image coordinates. Select
related runs and assign them to a named dance. A run can belong to one dance.
Each dance has a distinct color shared by all its runs' arrows, trajectories, and
run labels. Ungrouped runs share a separate color. Selected runs keep their dance
<<<<<<< Updated upstream
colors and gain thicker strokes. **Show only this dance**
on a dance card filters the comb and run list to its members. Only one dance can
be shown at a time; uncheck it to show all dances again. Time filters still apply.
=======
colors and gain thicker strokes. Check **Show this dance** on one or more cards
to display their combined members in the comb and run list. Clear all checks or
click **Show all dances** to show all runs again. Time filters still apply.

To merge dances, check **Select for merge** on two or more cards, choose the
existing dance name to keep in **Merge into**, then click **Merge selected dances**.
The chosen dance keeps its ID and name, takes all selected dances' runs and notes,
and stays uncertain if any selected dance was uncertain. Other selected dance
cards are replaced by the merged dance. Display filters follow the merged dance.
Merging preserves the manually annotated runs in each selected dance.
>>>>>>> Stashed changes

For a comb-image background, provide an undistorted full-resolution image:

```bash
python -m annotation_tool --comb-image path/to/full_comb_undistorted.png
```

By default, the tool uses a coordinate canvas and does not load a background
photo or read the source ROS bag. Background loading only happens if explicitly
requested with `--comb-image` or `--bag`.

## Export

Export creates a ZIP with cropped images, normalized (YOWO) labels, `classes.txt`,
`manifest.json`, and `master.json`. Class `0` is `waggle_bee`. Only accepted runs
inside their marked intervals are exported. You can include uncertain frames,
export keyframes only, or use a frame stride from the export dialog.

Boxes use crop coordinates in the editor. The master file also stores matching
undistorted full-image coordinates and source/output timestamps.

## Tests

Regression tests live in `tests/`. Run them from the parent directory containing
`annotation_tool` (Python tests require OpenCV and NumPy; JavaScript tests require Node.js):

```bash
python -m unittest discover -s annotation_tool/tests -t .
node annotation_tool/tests/test_box_deletion.js
node annotation_tool/tests/test_fixed_bbox.js
node annotation_tool/tests/test_uncertain_run.js
node annotation_tool/tests/test_boundary_intervals.js
<<<<<<< Updated upstream
=======
node annotation_tool/tests/test_dances.js
node annotation_tool/tests/test_clip_review.js
>>>>>>> Stashed changes
```
