"""Download e localização dos modelos de IA (todos gratuitos e abertos)."""

from __future__ import annotations

import os
import sys
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ProgressFn = Callable[[float, str], None]  # (fração 0..1 ou -1 p/ indeterminado, mensagem)

APP_NAME = "Transcritor"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class WhisperChoice:
    key: str
    label: str
    repo_id: str
    approx_mb: int


# Modelos Whisper convertidos para CTranslate2 (faster-whisper), hospedados no Hugging Face.
WHISPER_MODELS: list[WhisperChoice] = [
    WhisperChoice("large-v3-turbo", "Equilibrado – Whisper large-v3 turbo (recomendado)",
                  "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1600),
    WhisperChoice("large-v3", "Máxima precisão – Whisper large-v3 (mais lento)",
                  "Systran/faster-whisper-large-v3", 3100),
    WhisperChoice("medium", "Intermediário – Whisper medium",
                  "Systran/faster-whisper-medium", 1500),
    WhisperChoice("small", "Rápido – Whisper small (menos preciso)",
                  "Systran/faster-whisper-small", 480),
]

_WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin",
                  "tokenizer.json", "vocabulary.json", "vocabulary.txt"]

# Diarização (quem fala quando) via sherpa-onnx: segmentação pyannote 3.0 + embeddings WeSpeaker.
_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = f"{_SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_URL = f"{_SHERPA}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"


def whisper_choice(key: str) -> WhisperChoice:
    for m in WHISPER_MODELS:
        if m.key == key:
            return m
    return WHISPER_MODELS[0]


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def ensure_whisper(key: str, progress: Optional[ProgressFn] = None,
                   cancel: Optional[threading.Event] = None) -> str:
    """Garante que o modelo Whisper está no disco e retorna a pasta local."""
    choice = whisper_choice(key)
    target = models_dir() / "whisper" / key
    marker = target / ".complete"
    if marker.exists():
        return str(target)

    from huggingface_hub import snapshot_download

    target.mkdir(parents=True, exist_ok=True)
    expected = choice.approx_mb * 1024 * 1024
    done = threading.Event()

    def monitor():
        while not done.wait(0.5):
            if progress:
                frac = min(_dir_size(target) / expected, 0.99)
                progress(frac, f"Baixando modelo {choice.key} ({frac * 100:.0f}%)…")

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    try:
        snapshot_download(choice.repo_id, local_dir=str(target),
                          allow_patterns=_WHISPER_FILES)
    finally:
        done.set()
        t.join()
    if cancel is not None and cancel.is_set():
        raise InterruptedError
    marker.write_text("ok")
    return str(target)


def _download(url: str, dest: Path, label: str, progress: Optional[ProgressFn],
              cancel: Optional[threading.Event]) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        last = 0.0
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            now = time.monotonic()
            if progress and total and now - last > 0.3:
                last = now
                progress(got / total, f"Baixando {label} ({got * 100 // total}%)…")
    tmp.replace(dest)


def ensure_diarization(progress: Optional[ProgressFn] = None,
                       cancel: Optional[threading.Event] = None) -> tuple[str, str]:
    """Retorna (modelo_segmentacao, modelo_embedding), baixando se necessário."""
    base = models_dir() / "diarization"
    base.mkdir(parents=True, exist_ok=True)

    seg_dir = base / "sherpa-onnx-pyannote-segmentation-3-0"
    seg_model = seg_dir / "model.onnx"
    if not seg_model.exists():
        archive = base / "segmentation.tar.bz2"
        _download(SEGMENTATION_URL, archive, "modelo de segmentação", progress, cancel)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(base, filter="data")
        archive.unlink(missing_ok=True)

    emb_model = base / "wespeaker_en_voxceleb_resnet34_LM.onnx"
    if not emb_model.exists():
        _download(EMBEDDING_URL, emb_model, "modelo de identificação de voz", progress, cancel)

    return str(seg_model), str(emb_model)
