# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['bluetooth_webview.py'],
    pathex=[],
    binaries=[],
    datas=[('web_ui.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Bobs Humidity Tracker',
    icon='assets/icon.png',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Bobs Humidity Tracker',
)
app = BUNDLE(
    coll,
    name='Bobs Humidity Tracker.app',
    icon='assets/icon.png',
    bundle_identifier=None,
)
