"""Transcrição ao vivo durante a gravação.

Cada fonte de áudio (o microfone, que é você, e o som do computador, que são os outros
participantes) tem seu próprio buffer. Quando alguém faz uma pausa, ou a fala passa de
alguns segundos, o trecho vai para um modelo Whisper pequeno e rápido. O texto aparece em
segundos; a transcrição completa, com identificação de cada participante, roda ao parar.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from . import models

RATE = 16000
LIVE_MODEL = "small"
MIN_CHUNK = 1.2        # s: trecho mínimo para transcrever
MAX_CHUNK = 12.0       # s: corta mesmo sem pausa, para o texto não demorar
PAUSE = 0.6            # s de silêncio que encerram uma fala
SILENCE_RMS = 0.006    # abaixo disso é silêncio (≈ -44 dBFS)
SPEECH_RMS = 0.012     # trecho sem nenhum quadro acima disso é descartado
BACKLOG_WARN = 45.0    # s de áudio esperando: avisa que está atrasado
ECHO_WAIT = 8.0        # s que uma fala do microfone espera pela do computador antes de sair
ECHO_OVERLAP = 0.45    # fração de palavras em comum que caracteriza eco
ECHO_CORR = 0.6        # correlação do volume do microfone com o do alto-falante que caracteriza eco
FRAME = RATE // 20     # 50 ms: resolução do envelope de volume

SOURCES = {"mic": 0, "system": 1}
SOURCE_NAMES = {0: "Você", 1: "Participantes"}

# Frases que o Whisper inventa em ruído ou silêncio.
_HALLUCINATIONS = re.compile(
    r"amara\.org|legendas? (pela|por)|inscreva-se|obrigad[oa] por assistir|"
    r"thanks? for watching|subtitles by|^\W*(obrigad[oa]|tchau)\W*$",
    re.I,
)


@dataclass
class LiveSegment:
    source: int        # 0 = você (microfone), 1 = participantes (som do computador)
    start: float       # segundos desde o início da gravação
    end: float
    text: str


class _Buffer:
    def __init__(self) -> None:
        self.parts: list[np.ndarray] = []
        self.size = 0
        self.t0 = 0.0          # instante (desde o início) da primeira amostra
        self.rest = np.zeros(0, dtype=np.float32)   # sobra da reamostragem 48k -> 16k
        self.quiet = 0         # amostras finais seguidas abaixo do limiar
        self.loud = False      # houve fala neste trecho


class LiveTranscriber:
    def __init__(self, language: Optional[str], hotwords: str,
                 on_segment: Callable[[LiveSegment], None],
                 on_status: Callable[[str], None],
                 log: Callable[[str], None]) -> None:
        self.language = language if language in ("pt", "en") else None
        self.hotwords = hotwords.strip() or None
        self.on_segment, self.log = on_segment, log
        self._on_status, self._status = on_status, None
        self.t_start = time.monotonic()
        self._bufs = {k: _Buffer() for k in SOURCES}
        self._queue: list[tuple[int, float, np.ndarray]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sys_env: dict[int, float] = {}               # volume do computador a cada 50 ms
        self._env_rest = np.zeros(0, dtype=np.float32)      # amostras do computador ainda sem medir
        self._env_next = 0                                 # índice (50 ms) da próxima medida
        self._held: list[tuple[float, LiveSegment]] = []   # falas do microfone esperando checagem de eco
        self._recent: list[LiveSegment] = []               # falas recentes do computador

    # ------------------------------------------------------------ entrada
    def feed(self, source: str, samples: np.ndarray, rate: int) -> None:
        """Chamado pelas threads de captura. Precisa ser rápido."""
        if source not in self._bufs or self._stop.is_set():
            return
        now = time.monotonic() - self.t_start
        b = self._bufs[source]
        x = _resample(np.concatenate([b.rest, samples]) if len(b.rest) else samples, rate, b)
        if not len(x):
            return
        with self._lock:
            if b.size == 0:
                b.t0 = max(0.0, now - len(x) / RATE)
            if source == "system":
                self._env_rest = np.concatenate([self._env_rest, x])
                expected = int(round((now - len(self._env_rest) / RATE) * 20))
                if abs(expected - self._env_next) > 4:     # o loopback do Windows some no silêncio
                    self._env_next = expected
                n = len(self._env_rest) // FRAME
                for v in _envelope(self._env_rest[:n * FRAME]):
                    self._sys_env[self._env_next] = v
                    self._env_next += 1
                self._env_rest = self._env_rest[n * FRAME:]
            b.parts.append(x)
            b.size += len(x)
            rms = float(np.sqrt(np.mean(x * x)))
            if rms < SILENCE_RMS:
                b.quiet += len(x)
            else:
                b.quiet = 0
                if rms > SPEECH_RMS:
                    b.loud = True
            dur = b.size / RATE
            if (dur >= MIN_CHUNK and b.quiet >= PAUSE * RATE) or dur >= MAX_CHUNK:
                self._cut(source, b)

    def _cut(self, source: str, b: _Buffer) -> None:
        audio = np.concatenate(b.parts)
        if source == "mic" and b.loud and self._is_echo(b.t0, audio):
            b.loud = False
        if b.loud:
            self._queue.append((SOURCES[source], b.t0, audio))
            self._wake.set()
        b.parts, b.size, b.quiet, b.loud = [], 0, 0, False

    # ------------------------------------------------------------ ciclo
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="ao-vivo")
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            for k, b in self._bufs.items():
                if b.size:
                    self._cut(k, b)
        self._stop.set()
        self._wake.set()

    def on_status(self, msg: str) -> None:
        if msg != self._status:
            self._status = msg
            self._on_status(msg)

    def backlog(self) -> float:
        with self._lock:
            return sum(len(a) for _, _, a in self._queue) / RATE

    def _run(self) -> None:
        try:
            model = self._load()
        except Exception as e:  # noqa: BLE001
            self.on_status(f"Transcrição ao vivo indisponível: {e}")
            self.log(f"Transcrição ao vivo indisponível: {e}")
            return
        self.on_status("")
        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                job = self._queue.pop(0) if self._queue else None
            if job is None:
                self._release(final=self._stop.is_set())
                if self._stop.is_set():
                    return
                continue
            if self._stop.is_set() and self.backlog() > 120:
                return        # parou com muito acumulado: a transcrição completa cuida do resto
            self._transcribe(model, *job)
            self._release()
            lag = self.backlog()
            self.on_status("A transcrição ao vivo está atrasada; o texto completo sai ao parar."
                           if lag > BACKLOG_WARN else "")

    def _load(self):
        from faster_whisper import WhisperModel

        def progress(frac: float, msg: str) -> None:
            self.on_status(msg)

        self.on_status("Preparando a transcrição ao vivo…")
        path = models.ensure_whisper(LIVE_MODEL, progress, self._stop)
        threads = max(2, min(4, (os.cpu_count() or 4) // 2))
        return WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)

    def _transcribe(self, model, source: int, t0: float, audio: np.ndarray) -> None:
        try:
            segments, _ = model.transcribe(
                audio, language=self.language, hotwords=self.hotwords, beam_size=1,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False, without_timestamps=False,
            )
            for seg in segments:
                text = seg.text.strip()
                if (not text or (seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0)
                        or seg.avg_logprob < -1.5 or _HALLUCINATIONS.search(text)):
                    continue
                out = LiveSegment(source, t0 + seg.start, t0 + seg.end, text)
                if source == SOURCES["mic"]:
                    self._held.append((time.monotonic(), out))
                else:
                    self._recent.append(out)
                    self._recent = [r for r in self._recent if r.end > out.end - 60]
                    self.on_segment(out)
        except Exception as e:  # noqa: BLE001
            self.log(f"Falha num trecho da transcrição ao vivo: {e}")

    def _is_echo(self, t0: float, audio: np.ndarray) -> bool:
        """O volume do microfone acompanha o do alto-falante? Então é o som dos outros vazando."""
        env = _envelope(audio)
        first = int(t0 * 20)
        best = 0.0
        for lag in range(0, 11):        # o eco chega até ~0,5 s depois
            ref = [self._sys_env.get(first + i - lag) for i in range(len(env))]
            pairs = [(m, r) for m, r in zip(env, ref) if r is not None]
            if len(pairs) < 20:
                continue
            m, r = np.array(pairs).T
            if m.std() < 1e-3 or r.std() < 1e-3:
                continue
            best = max(best, float(np.corrcoef(m, r)[0, 1]))
        old = first - 20 * 120
        for k in [k for k in self._sys_env if k < old]:
            del self._sys_env[k]
        return best >= ECHO_CORR

    def _system_done_until(self) -> float:
        """Até que instante o áudio do computador já foi transcrito."""
        with self._lock:
            pending = [t for src, t, _ in self._queue if src == SOURCES["system"]]
            b = self._bufs["system"]
            if b.size:
                pending.append(b.t0)
        return min(pending) if pending else time.monotonic() - self.t_start

    def _release(self, final: bool = False) -> None:
        """Libera as falas do microfone que não são eco do alto-falante.

        Sem fone, o microfone também capta os outros participantes. Cada fala do microfone
        espera o áudio do computador do mesmo momento ser transcrito; se o texto bate, é eco.
        """
        if not self._held:
            return
        done, now = self._system_done_until(), time.monotonic()
        keep: list[tuple[float, LiveSegment]] = []
        for born, seg in self._held:
            if not final and seg.end > done and now - born < ECHO_WAIT:
                keep.append((born, seg))
                continue
            near = [r.text for r in self._recent if r.start < seg.end + 2 and r.end > seg.start - 2]
            if near and _overlap(seg.text, " ".join(near)) >= ECHO_OVERLAP:
                continue
            self.on_segment(seg)
        self._held = keep


def _envelope(x: np.ndarray) -> list[float]:
    """Volume em dB a cada 50 ms."""
    n = len(x) // FRAME
    if n == 0:
        return []
    frames = x[:n * FRAME].reshape(n, FRAME)
    return list(20 * np.log10(np.sqrt((frames * frames).mean(axis=1)) + 1e-4))


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]


def _overlap(mine: str, theirs: str) -> float:
    """Fração das palavras de `mine` que também aparecem em `theirs`."""
    a, b = _words(mine), set(_words(theirs))
    return sum(w in b for w in a) / len(a) if a else 1.0


def _resample(x: np.ndarray, rate: int, b: _Buffer) -> np.ndarray:
    """Reamostra para 16 kHz. 48 kHz (o caso comum) vira média de 3 em 3 amostras."""
    x = np.asarray(x, dtype=np.float32)
    if rate == RATE:
        b.rest = np.zeros(0, dtype=np.float32)
        return x
    if rate % RATE == 0:
        k = rate // RATE
        n = len(x) - len(x) % k
        b.rest = x[n:]
        return x[:n].reshape(-1, k).mean(axis=1)
    b.rest = np.zeros(0, dtype=np.float32)
    n_out = int(len(x) * RATE / rate)
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)
