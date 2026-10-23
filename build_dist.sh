#!/bin/zsh
# Builds a shareable zip: release/OAK_Mocap_Recorder_<version>_macOS-arm64.zip
#
# Unlike build_app.sh (dev build), this uses a standalone Python (uv / python-build-standalone)
# and PySide6 6.9.x so the app runs on macOS 12+ (Apple Silicon), not only on the build machine's macOS.
set -e
cd "$(dirname "$0")"
MIN_MACOS=12.0
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' mocap_recorder/__init__.py)
NAME="OAK_Mocap_Recorder_${VERSION}_macOS-arm64"

# 1. isolated build environment
if [ ! -d .venv-dist ]; then
  uv venv --python 3.12 --python-preference only-managed .venv-dist
fi
# resolve wheels for the oldest supported macOS, not for the build machine
MACOSX_DEPLOYMENT_TARGET=$MIN_MACOS uv pip install --python .venv-dist/bin/python -q \
  --python-platform aarch64-apple-darwin -r requirements-dist.txt

# 2. build the .app
rm -rf build-dist dist-release
.venv-dist/bin/pyinstaller --noconfirm --clean --distpath dist-release --workpath build-dist \
  packaging/MocapRecorder.spec
APP="dist-release/OAK Mocap Recorder.app"

# 3. every bundled binary must load on MIN_MACOS
bad=0
while IFS= read -r -d '' f; do
  v=$(otool -l "$f" 2>/dev/null | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; exit}')
  if [ -n "$v" ] && [ "$(printf '%s\n%s\n' "$v" "$MIN_MACOS" | sort -V | tail -1)" != "$MIN_MACOS" ]; then
    echo "needs macOS $v: $f"; bad=1
  fi
done < <(find "$APP" -type f \( -name "*.dylib" -o -name "*.so" -o -perm +111 \) -print0)
[ $bad = 0 ] || { echo "Some binaries need a newer macOS than $MIN_MACOS — aborting."; exit 1; }

codesign --force --deep --sign - "$APP"
"$APP/Contents/MacOS/OAK Mocap Recorder" --selftest | tail -1

# 4. package: app + instructions + example list
STAGE="release/$NAME"
rm -rf "$STAGE" "release/$NAME.zip"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/OAK Mocap Recorder.app"
cp packaging/READ_ME_FIRST.txt "$STAGE/READ ME FIRST.txt"
cp packaging/example_signs.txt "$STAGE/example_signs.txt"
ditto -c -k --sequesterRsrc --keepParent "$STAGE" "release/$NAME.zip"
echo "Release: release/$NAME.zip ($(du -h "release/$NAME.zip" | cut -f1))"
