#!/bin/zsh
# Builds "dist/OAK Mocap Recorder.app" (Apple Silicon). Run from the project root.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller; }
.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/MocapRecorder.spec
# ad-hoc signature so macOS runs it locally (first launch: right-click > Open)
codesign --force --deep --sign - "dist/OAK Mocap Recorder.app"
echo "Built: dist/OAK Mocap Recorder.app"
