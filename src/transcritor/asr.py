"""Reconhecimento de fala além do faster-whisper.

- Whisper na GPU do Mac (MLX): o mesmo modelo e a mesma precisão, cerca de 4x mais rápido
  que no processador. Só em Macs com Apple Silicon.
- NVIDIA Parakeet TDT 0.6B v3 (via sherpa-onnx): roda no processador em qualquer sistema,
  cerca de 18x mais rápido que o Whisper turbo, com um pouco mais de erro. É o modelo da
  transcrição ao vivo e a opção "Ultrarrápido".

Os dois devolvem palavras com tempo, no mesmo formato do faster-whisper, para que a
identificação de falantes funcione igual.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from typing import Callable, Optional

import numpy as np

from . import models

RATE = 16000
ProgressFn = Callable[[float], None]       # fração 0..1 da transcrição


def _threads() -> int:
    # Nos Apple Silicon base, metade dos núcleos é de eficiência e só atrapalha.
    return max(2, min(8, (os.cpu_count() or 4) // 2))


def speech_chunks(audio: np.ndarray, max_s: float = 30.0, min_silence_ms: int = 500):
    """Trechos com fala (Silero VAD do faster-whisper), em amostras."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    opts = VadOptions(min_silence_duration_ms=min_silence_ms, speech_pad_ms=300,
                      max_speech_duration_s=max_s)
    return get_speech_timestamps(audio, opts)


# ===================================================================== MLX
_mlx_ok: Optional[bool] = None


def mlx_available() -> bool:
    global _mlx_ok
    if _mlx_ok is None:
        _mlx_ok = False
        if sys.platform == "darwin" and os.uname().machine == "arm64":
            try:
                _import_mlx_whisper()
                _mlx_ok = True
            except Exception:  # noqa: BLE001
                pass
    return _mlx_ok


def _import_mlx_whisper():
    """Importa o mlx-whisper sem o numba e o scipy (~200 MB a mais no app).

    Ele só usa os dois para o alinhamento das palavras no tempo; as versões em numpy abaixo
    fazem o mesmo cálculo.
    """
    for name in ("numba", "scipy", "scipy.signal"):
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            if name == "numba":
                mod.jit = lambda *a, **k: (lambda f: f)
            sys.modules[name] = mod
    if not hasattr(sys.modules["scipy"], "signal"):
        sys.modules["scipy"].signal = sys.modules["scipy.signal"]
    import mlx_whisper
    from mlx_whisper import timing
    timing.dtw = _dtw
    timing.median_filter = _median_filter
    return mlx_whisper


def _median_filter(x: np.ndarray, width: int) -> np.ndarray:
    pad = width // 2
    if x.shape[-1] <= pad:
        return x
    padded = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(pad, pad)], mode="reflect")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width, axis=-1)
    return np.median(windows, axis=-1).astype(np.float32)


def _dtw(x: np.ndarray) -> np.ndarray:
    """DTW do Whisper, calculado por antidiagonais (cada uma depende só das duas anteriores)."""
    x = np.asarray(x, dtype=np.float32)
    n, m = x.shape
    cost = np.full((n + 1, m + 1), np.inf, dtype=np.float32)
    trace = np.full((n + 1, m + 1), -1, dtype=np.int8)
    cost[0, 0] = 0
    for d in range(2, n + m + 1):
        i = np.arange(max(1, d - m), min(n, d - 1) + 1)
        j = d - i
        c0, c1, c2 = cost[i - 1, j - 1], cost[i - 1, j], cost[i, j - 1]
        t = np.where((c0 < c1) & (c0 < c2), 0, np.where((c1 < c0) & (c1 < c2), 1, 2))
        cost[i, j] = x[i - 1, j - 1] + np.choose(t, [c0, c1, c2])
        trace[i, j] = t
    trace[0, :] = 2
    trace[:, 0] = 1
    i, j, path = n, m, []
    while i > 0 or j > 0:
        path.append((i - 1, j - 1))
        t = trace[i, j]
        if t == 0:
            i, j = i - 1, j - 1
        elif t == 1:
            i -= 1
        else:
            j -= 1
    return np.array(path[::-1]).T


def mlx_transcribe(audio: np.ndarray, model_dir: str, language: Optional[str], prompt: Optional[str],
                   progress: ProgressFn, cancel: threading.Event, per_window_language: bool = False):
    """Transcreve com o Whisper na GPU. Devolve (palavras [(início, fim, texto)], idioma)."""
    from faster_whisper.vad import SpeechTimestampsMap, collect_chunks

    mlx_whisper = _import_mlx_whisper()
    chunks = speech_chunks(audio, max_s=30.0)
    if not chunks:
        return [], language
    # Trechos com fala, juntos em blocos: o Whisper não vê os silêncios (onde ele inventa texto)
    # e a barra de progresso anda a cada bloco.
    pieces, meta = collect_chunks(audio, chunks, max_duration=30.0 if per_window_language else 240.0)
    tsmap = SpeechTimestampsMap(chunks, RATE)
    total = sum(len(p) for p in pieces) or 1
    done, words, detected = 0, [], language
    for piece, info in zip(pieces, meta):
        if cancel.is_set():
            raise InterruptedError
        if len(piece) < RATE // 4:
            done += len(piece)
            continue
        result = mlx_whisper.transcribe(
            piece, path_or_hf_repo=model_dir, language=None if per_window_language else detected,
            initial_prompt=prompt, word_timestamps=True, condition_on_previous_text=False,
            hallucination_silence_threshold=2.0, verbose=None,
        )
        if detected is None and not per_window_language:
            detected = result.get("language")
        for seg in result["segments"]:
            for w in seg.get("words") or []:
                start = tsmap.get_original_time(info["offset"] + w["start"])
                end = tsmap.get_original_time(info["offset"] + w["end"], is_end=True)
                words.append((start, max(end, start), w["word"]))
        done += len(piece)
        progress(done / total)
    return words, detected or language


# ================================================================== Parakeet
class Parakeet:
    """NVIDIA Parakeet TDT 0.6B v3: 25 idiomas europeus, pontuação e maiúsculas, idioma automático."""

    def __init__(self, path: str, threads: Optional[int] = None) -> None:
        import sherpa_onnx as so
        from pathlib import Path
        d = Path(path)
        self.rec = so.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
            num_threads=threads or _threads(), model_type="nemo_transducer")
        self._lock = threading.Lock()

    def text(self, audio: np.ndarray) -> str:
        """Texto de um trecho curto (até ~30 s)."""
        with self._lock:
            st = self.rec.create_stream()
            st.accept_waveform(RATE, np.asarray(audio, dtype=np.float32))
            self.rec.decode_stream(st)
            return st.result.text.strip()

    def words(self, audio: np.ndarray, progress: ProgressFn, cancel: threading.Event):
        """Palavras com tempo de um áudio longo: corta nos silêncios e decodifica em lotes."""
        chunks = speech_chunks(audio, max_s=25.0)
        total = sum(c["end"] - c["start"] for c in chunks) or 1
        words, done = [], 0
        for k in range(0, len(chunks), 8):
            if cancel.is_set():
                raise InterruptedError
            batch = chunks[k:k + 8]
            with self._lock:
                streams = []
                for c in batch:
                    st = self.rec.create_stream()
                    st.accept_waveform(RATE, audio[c["start"]:c["end"]])
                    streams.append(st)
                self.rec.decode_streams(streams)
            for c, st in zip(batch, streams):
                words += _join_tokens(st.result.tokens, st.result.timestamps,
                                      c["start"] / RATE, c["end"] / RATE)
                done += c["end"] - c["start"]
            progress(done / total)
        return words


def _join_tokens(tokens, stamps, offset: float, chunk_end: float):
    """Peças do SentencePiece (" pala", "vra") -> palavras com início e fim."""
    words: list[list] = []
    for tok, t in zip(tokens, stamps):
        if tok.startswith((" ", "▁")) or not words:
            words.append([offset + t, offset + t, " " + tok.lstrip(" ▁")])
        else:
            words[-1][2] += tok
        words[-1][1] = offset + t
    out = []
    for i, (start, last, text) in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else chunk_end
        out.append((start, max(min(nxt, last + 0.6), start + 0.05), text))
    return [w for w in out if w[2].strip()]


_parakeet: Optional[Parakeet] = None
_parakeet_lock = threading.Lock()


def parakeet(progress: Optional[Callable[[float, str], None]] = None,
             cancel: Optional[threading.Event] = None) -> Parakeet:
    """Modelo compartilhado entre a transcrição ao vivo e a final (carrega uma vez só)."""
    global _parakeet
    with _parakeet_lock:
        if _parakeet is None:
            _parakeet = Parakeet(models.ensure_parakeet(progress, cancel))
        return _parakeet
