OAK Mocap Recorder
==================

Records video and depth from Luxonis OAK-D cameras (up to 3 at once), with a
sign-list mode for recording sign-language signs one by one.

WHAT YOU NEED
- A Mac with an Apple Silicon chip (M1, M2, M3, M4…) running macOS 12 or newer.
  Intel Macs are not supported.
  To check: Apple menu > About This Mac. "Chip" should say Apple M…
- OAK-D cameras plugged into USB 3 ports, or into a powered USB 3 hub.
- No other software is needed. Everything is inside the app.

INSTALL
1. Drag "OAK Mocap Recorder.app" into your Applications folder.
2. Open it. The first time, macOS will block it because it does not come from
   the App Store. To allow it:
   - Try to open the app once and click "Done" (or "OK") in the warning.
   - Open System Settings > Privacy & Security, scroll down, and click
     "Open Anyway" next to "OAK Mocap Recorder". Confirm with your password.
   - On macOS 12–14 you can instead right-click the app, choose "Open",
     then click "Open" again.
   After the first time it opens normally.

   If macOS says the app "is damaged and can't be opened", open Terminal and run:
       xattr -dr com.apple.quarantine "/Applications/OAK Mocap Recorder.app"
   Then open the app again.

QUICK START
1. Plug in the cameras. They appear in the window within a few seconds.
   Click a camera's name (e.g. cam1) to rename it (front, left, right…).
2. "Choose folder…" (top left) sets where recordings are saved.
3. Free recording: press "Start recording" (or Space), then press it again to stop.

RECORDING A SIGN LIST
1. Write the signs in a plain text file, one sign per line
   (see example_signs.txt). Greek and accented letters are fine.
2. Sign list > "Load list…", choose the file, and enter the participant ID.
3. The sign appears in large letters. Press Record (Space), let the person sign,
   then press Stop (Space).
4. A preview of the take plays:
   Accept (Enter)     = keep it and move to the next sign
   Discard (Backspace) = record the same sign again
5. To redo a sign later, double-click it in the list.
6. If you quit halfway, the app offers to continue where you stopped the next
   time it opens. "Clear list" ends the session. Recordings are never deleted.

WHERE THE FILES GO
<chosen folder>/<participant>_<date_time>/001_<sign>/<camera>_<serial>/
  <sign>_<camera>_rgb.mp4      colour video
  <sign>_<camera>_depth.mkv    depth in millimetres (16-bit, lossless)
  …_timestamps.csv              exact time of every frame (used to sync cameras)
Discarded takes are kept in the "_discarded" folder.

TIPS
- The default settings are tuned for a steady 30 fps with 3 cameras on one USB
  hub. If you change things and frames start dropping, click
  "Recommended settings (stable 30 fps)" to go back.
- "Anti-flicker" must match the mains frequency: 50 Hz in Europe, 60 Hz in the
  Americas. Otherwise room lights can flicker or show bands in the video.
- At 30 fps each frame gets at most 1/30 s of light, so a dim room gives a
  darker, grainier picture than at 15 fps. Add light rather than lowering the fps.
- If a camera tile shows a USB warning, move that camera to a different USB 3
  port or cable. USB 2 is too slow.
- Settings (resolution, frame rate, streams) are on the left. Click
  "Apply settings" after changing them. The cameras restart by themselves
  (this takes a few seconds). You can also click "Cameras: ON" to turn them
  off first, change the settings, then turn them back on.
