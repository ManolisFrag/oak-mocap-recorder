#!/bin/zsh
# Run from source (development).
cd "$(dirname "$0")"
exec .venv/bin/python -m mocap_recorder
