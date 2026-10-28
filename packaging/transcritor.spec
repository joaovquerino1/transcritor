# -*- mode: python ; coding: utf-8 -*-
# Gera o aplicativo com: pyinstaller packaging/transcritor.spec --noconfirm
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from transcritor import __version__  # noqa: E402

datas = [(str(ROOT / "assets" / "icon.png"), "assets")]
datas += collect_data_files("faster_whisper")          # modelo VAD Silero
datas += collect_data_files("docx")                     # template padrão do Word
binaries = []
for pkg in ("ctranslate2", "sherpa_onnx", "onnxruntime", "av"):
    binaries += collect_dynamic_libs(pkg)
hiddenimports = collect_submodules("sherpa_onnx") + ["ctranslate2", "av", "tokenizers"]

excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "scipy", "pandas", "torch",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "assets" / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Transcritor",
    console=False,
    icon=icon,
    upx=False,
    target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Transcritor", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Transcritor.app",
        icon=icon,
        bundle_identifier="br.com.transcritor.app",
        version=__version__,
        info_plist={
            "CFBundleName": "Transcritor",
            "CFBundleDisplayName": "Transcritor",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "Software livre. Modelos: OpenAI Whisper (MIT), pyannote (MIT), WeSpeaker (Apache 2.0).",
        },
    )
