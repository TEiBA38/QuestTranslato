# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')] + collect_data_files('tkinterdnd2') + collect_data_files('customtkinter'),
    hiddenimports=[
        'ui_screens',
        'modpack_manager',
        'translation_runner',
        'translation_engines',
        'file_processors',
        'review_checks',
        'hqm_parser',
        'customtkinter',
        'tkinterdnd2',
        'PIL',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='QuestTranslatorPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
    icon='icon.ico',
)
