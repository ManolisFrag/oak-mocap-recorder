# PyInstaller spec — build with ./build_app.sh
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
# depthai is a single extension module; its dylibs (libdepthai-core, libusb) are
# found by PyInstaller's binary dependency analysis.
for pkg in ("av",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["launcher.py"],
    pathex=[os.path.join(SPECPATH, "..")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["mocap_recorder.worker"],
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OAK Mocap Recorder",
          console=False, argv_emulation=False, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="OAK Mocap Recorder")
app = BUNDLE(
    coll,
    name="OAK Mocap Recorder.app",
    bundle_identifier="org.mocap.oakrecorder",
    info_plist={
        "CFBundleShortVersionString": "0.2.2",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
