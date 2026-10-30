# -*- mode: python ; coding: utf-8 -*-
# Gera o aplicativo com: pyinstaller packaging/transcritor.spec --noconfirm
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from transcritor import __version__  # noqa: E402

datas = [(str(ROOT / "assets" / "icon.png"), "assets")]
datas += collect_data_files("faster_whisper")          # modelo VAD Silero
datas += collect_data_files("docx")                     # template padrão do Word
datas += collect_data_files("imageio_ffmpeg")           # executável do FFmpeg (junta áudio e vídeo)
datas += collect_data_files("yt_dlp_ejs")
binaries = []
for pkg in ("ctranslate2", "sherpa_onnx", "onnxruntime", "av"):
    binaries += collect_dynamic_libs(pkg)
# Deno: o yt-dlp precisa de um runtime de JavaScript para baixar do YouTube sem HTTP 403.
import deno  # noqa: E402
binaries += [(deno.find_deno_bin(), "deno")]
hiddenimports = collect_submodules("sherpa_onnx") + ["ctranslate2", "av", "tokenizers"]
hiddenimports += collect_submodules("yt_dlp") + ["yt_dlp_ejs", "imageio_ffmpeg"]
# Dependências opcionais do yt-dlp (o yt-dlp atualizado em tempo de execução também as usa)
hiddenimports += ["brotli", "certifi", "mutagen", "Cryptodome", "websockets", "requests", "urllib3"]
# Gravação de reuniões: ScreenCaptureKit (macOS) e loopback WASAPI (Windows)
if sys.platform == "darwin":
    hiddenimports += ["objc", "Foundation", "AppKit", "Quartz", "CoreMedia", "AVFoundation",
                      "ScreenCaptureKit", "dispatch"]
    # Whisper na GPU (MLX): biblioteca nativa, shaders Metal e os arquivos do tokenizador
    hiddenimports += collect_submodules("mlx") + collect_submodules("mlx_whisper") + \
        ["tiktoken", "tiktoken_ext", "tiktoken_ext.openai_public", "more_itertools"]
    binaries += collect_dynamic_libs("mlx")
    datas += collect_data_files("mlx", includes=["**/*.metallib"]) + collect_data_files("mlx_whisper")
elif sys.platform == "win32":
    hiddenimports += ["soundcard", "soundcard.mediafoundation", "cffi"] + collect_submodules("mss")

excludes = [
    "tkinter", "matplotlib", "PIL", "IPython", "pytest", "scipy", "pandas", "torch",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtQuick3D", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
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
# O PySide6 arrasta ~130 módulos Qt; mantemos só o que a interface e o navegador de login usam
# (fecho de dependências calculado com otool a partir de QtWebEngineWidgets). Economiza ~250 MB.
QT_KEEP = {
    "QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtDBus", "QtOpenGL", "QtPositioning",
    "QtPrintSupport", "QtQml", "QtQmlMeta", "QtQmlModels", "QtQmlWorkerScript", "QtQuick",
    "QtQuickWidgets", "QtWebChannel", "QtWebEngineCore", "QtWebEngineWidgets", "QtSvg",
}
_qt_mac = re.compile(r"Qt[/\\]lib[/\\](Qt\w+)\.framework")
_qt_win = re.compile(r"PySide6[/\\]Qt6(\w+?)\.dll$", re.I)


def _keep(entry):
    dest = entry[0]
    if re.search(r"Qt[/\\]qml[/\\]|plugins[/\\]qmltooling", dest):
        return False
    m = _qt_mac.search(dest)
    if m:
        return m.group(1) in QT_KEEP
    m = _qt_win.search(dest)
    if m:
        return "Qt" + m.group(1) in QT_KEEP
    return True


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]

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
    target_arch="arm64" if sys.platform == "darwin" else None,
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
            # Apenas Apple Silicon: nunca abrir via Rosetta.
            "LSRequiresNativeExecution": True,
            "LSArchitecturePriority": ["arm64"],
            "NSMicrophoneUsageDescription": "O Transcritor grava a sua voz nas reuniões que você escolher gravar.",
            "NSAudioCaptureUsageDescription": "O Transcritor grava o som das reuniões que você escolher gravar.",
            "NSScreenCaptureUsageDescription": "O Transcritor grava a tela das reuniões que você escolher gravar.",
            "NSHumanReadableCopyright": "Software livre. Modelos: OpenAI Whisper (MIT), pyannote (MIT), WeSpeaker (Apache 2.0).",
        },
    )
