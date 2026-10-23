# OAK Mocap Recorder

macOS desktop app that previews and records several **Luxonis OAK-D Pro W** cameras at once
(RGB, RGB-aligned 16-bit depth, left/right mono, IMU) for markerless motion capture.
Built on the official **DepthAI v3** Python API (depthai 3.11), PySide6 and PyAV.

## Run

```zsh
./build_app.sh                       # builds dist/OAK Mocap Recorder.app (double-click it)
./run.sh                             # or run from source
```
First launch of the ad-hoc-signed app: right-click → Open. To set up from scratch:
`/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

### Sharing with colleagues
`./build_dist.sh` creates `release/OAK_Mocap_Recorder_<version>_macOS-arm64.zip`. It contains the app,
a "READ ME FIRST.txt" with install instructions, and an example sign list. The build:
* uses a standalone Python and the pinned versions in `requirements-dist.txt`, so it runs on **macOS 12+ on Apple Silicon**
  (`build_app.sh` builds against this Mac's newest libraries, so its app only runs on this Mac's macOS version)
* fails if any bundled binary needs a newer macOS.

The app is ad-hoc signed, not notarized, so recipients must allow it once
(System Settings → Privacy & Security → Open Anyway).

## Using it
### Recommended settings (default): stable 30 fps with 3 cameras on one USB hub
| Setting | Value | Why |
|---|---|---|
| RGB | 1080p, 30 fps, H.265 | encoded on the camera, ~2–3 MB/s over USB; full detail for MediaPipe |
| Mono / depth | 640x400 → depth saved at 640x360 | 4× less USB, camera and laptop load than 1280x800 (1280x720 depth ≈ 55 MB/s per camera over USB) |
| Depth preset | FAST_ACCURACY | less on-camera filtering, keeps up at 30 fps |
| Anti-flicker | 50 Hz (60 Hz in the Americas) | no flicker or bands from room lights |
| Undistort RGB | on | needed so depth lines up with RGB |
| Streams | RGB + depth + IMU | left/right mono off (each adds a video encode) |

"Recommended settings (stable 30 fps)" restores these (IR settings are kept). Installs with older saved settings
are asked once after updating. Detail vs. stability: 1280x800 mono gives finer depth on fingers, but with 3 cameras
it is close to the limits of a shared hub or a smaller MacBook; the panel warns about it.

1. Plug the cameras into **USB3** ports (each camera's tile shows its USB speed; a warning appears if it is not USB3).
   Cameras appear automatically, and unplugged cameras reconnect on their own.
2. Rename each tile (e.g. `front`, `left`, `right`). Names are remembered per serial number.
3. Choose the settings (resolution, fps, codec, mono resolution, streams, depth preset) and click **Apply settings**.
   The IR dot projector and flood light change live, without a restart.
4. Pick the output folder, optionally type a trial name, then click **Start recording** (or press Space) and **Stop recording** when done.

## Sign-list sessions (sign-language recording)
1. Write the signs in a `.txt` file: one sign per line, UTF-8. Greek and accents are fine;
   blank lines and lines starting with `#` are ignored.
2. Click **Sign list → Load list…**, choose the file and enter the participant ID. A session folder
   `<Save to>/<participant>_<date_time>/` is created.
3. The current sign **flashes** over the camera view for 2 s, then stays in the banner above it.
   Press **Record**/Space, sign, then press **Stop**/Space.
4. A **review window** loops the take, starting from the moment Record was pressed. You can pick the camera, and any dropped frames
   or disconnects are listed.
   * **Accept** (↩): the take is saved as `NNN_<sign>/` and the next sign flashes.
   * **Discard** (⌫): the take is moved to `_discarded/` and the same sign flashes again.
5. **Double-click** any sign in the list to record it again. The old take is moved to `_discarded/` only when the new one
   is accepted.
6. If the app is closed or crashes, it offers to **resume** at the next unrecorded sign on the next launch.
   **Open session…** resumes any earlier session folder.
   A take that was waiting for review is moved to `_discarded/`.
7. **Clear list** ends the session (all files are kept) and returns to free recording.

```
<participant>_<date_time>/
  signs.txt  progress.json
  001_ΓΕΙΑ_ΣΟΥ/
    take.json
    front_<serial>/  ΓΕΙΑ_ΣΟΥ_front_rgb.mp4  ΓΕΙΑ_ΣΟΥ_front_depth.mkv  ΓΕΙΑ_ΣΟΥ_front_rgb_timestamps.csv  …
  _discarded/   _pending/
```
`tools/sync_index.py` works on a take folder too: `python tools/sync_index.py <session>/001_ΓΕΙΑ_ΣΟΥ`.

## Output (free recording)
```
<folder>/<YYYYmmdd_HHMMSS>_<trial>/
  session.json                     settings, start/stop, per-camera frame/drop counts, disconnect gaps, warnings
  <camname>_<serial>/
    rgb.mp4                        on-device H.265/H.264/MJPEG, no re-encoding
    depth.mkv                      FFV1 lossless uint16, millimetres, 0 = invalid, aligned to RGB
    left.mp4  right.mp4  imu.csv   if enabled
    <stream>_timestamps.csv        frame_idx, sequence_num, device_ts_us, host_ts_us, written
    calib.json                     factory calibration (EEPROM)
    intrinsics.json                actual K / distortion of each recorded stream (after crop/undistortion)
    device.json                    model, USB speed, IMU, depthai version, settings
```
* **Sync:** the USB Pro W has no hardware trigger, so cameras free-run. `host_ts_us` is the DepthAI host-synced
  clock, shared by all cameras (accurate to about 1 ms). Frames from different cameras are up to half a frame apart.
  `python tools/sync_index.py <session>` writes `sync_rgb.csv`, which matches frame indices across cameras.
* **H.26x pre-roll:** video files start at the keyframe just before *Start*, which can be up to 1 s earlier.
  Always map frames through `*_timestamps.csv` (row *n* = decoded frame *n*).
* **Depth ↔ RGB:** depth is aligned to the (undistorted) RGB image and only scaled.
  RGB pixel (u, v) → depth pixel (u·Wd/Wrgb, v·Hd/Hrgb). Depth is saved at 1280×720,
  or 640×360 above 30 fps or with 640×400 mono sensors, so that lossless encoding keeps up.
* After a disconnect, the camera continues into `*_part2.*` files and the gap is logged in `session.json`.

### MediaPipe + depth example
```python
import cv2, mediapipe as mp
from tools.read_depth import iter_depth, depth_at_rgb_pixel
cap = cv2.VideoCapture("cam1_XXXX/rgb.mp4")
for depth in iter_depth("cam1_XXXX/depth.mkv"):   # pair frames via the timestamp CSVs!
    ok, bgr = cap.read()
    ...  # landmark (x, y) normalised -> u = x*W, v = y*H -> depth_at_rgb_pixel(depth, u, v, (W, H))
```
Note: rgb.mp4 and depth.mkv start at different frames (because of the pre-roll), so match rows of
`rgb_timestamps.csv` and `depth_timestamps.csv` by `sequence_num` or by timestamp.

## Tests
```zsh
.venv/bin/python tests/test_writers.py          # MP4 muxing of all codecs, FFV1 lossless round trip
.venv/bin/python tests/test_recorder_logic.py   # pre-roll, stop cutoff, drops, depth, IMU, sync tool (no hardware)
.venv/bin/python tests/test_worker_offline.py   # worker offline/retry/quit
.venv/bin/python tests/test_signlist.py         # list parsing, Greek names, accept/discard/re-record, resume
.venv/bin/python tests/test_review.py           # review player (no pre-roll, real speed, loops)
.venv/bin/python tests/test_gui_signflow.py     # full GUI flow with a simulated camera
.venv/bin/python tests/test_preview_nv12.py     # preview colour conversion
.venv/bin/python tests/test_restart_nofreeze.py # Apply / cameras on-off never block the window
.venv/bin/python tests/test_settings_defaults.py # recommended defaults, warnings, one-time prompt
"dist/OAK Mocap Recorder.app/Contents/MacOS/OAK Mocap Recorder" --selftest
```

## Hardware notes
* Give each camera its own USB3 port, or use a powered USB3 hub. Three cameras on one unpowered hub will brown out.
* Rough device limits (shown as warnings in the app): 4K is limited to 30 fps; the H.26x encoder handles about 250 MP/s in total;
  stereo at 1280×800 is reliable up to about 45 fps.
* Overlapping dot projectors from several cameras usually still help depth. If the dots disturb the mono images,
  lower or disable the projector.
