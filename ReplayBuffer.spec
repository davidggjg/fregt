# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'cv2',
        'mss', 'mss.windows',
        'keyboard',
        'PIL', 'PIL.Image', 'PIL.ImageTk',
        'numpy',
        'win32com', 'win32com.client',
        'pythoncom', 'pywintypes',
        'winreg',
        'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'PyQt5', 'wx', 'sounddevice', 'soundfile'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ReplayBuffer',
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    uac_admin=True,
)
