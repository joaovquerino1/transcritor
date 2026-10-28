"""Pipeline: decodificar áudio do vídeo -> transcrever (Whisper) -> diarizar (sherpa-onnx) -> juntar."""

from __future__ import annotations

import bisect
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import models

SAMPLE_RATE = 16000

LANGUAGES = {
    "pt": "Português (Brasil)",
    "en": "Inglês",
    "auto": "Detectar automaticamente",
    "mixed": "Misto (português + inglês no mesmo vídeo)",
}

# O prompt inicial "ensina" o estilo esperado: pontuação, acentos e maiúsculas.
PROMPTS = {
    "pt": "Olá! Esta é uma transcrição em português do Brasil, com pontuação, acentuação e letras maiúsculas corretas.",
    "en": "Hello! This is an English transcription, with proper punctuation and capitalization.",
}

SENTENCE_END = (".", "?", "!", "…")


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: Optional[int] = None


@dataclass
class Utterance:
    start: float
    end: float
    speaker: Optional[int]
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    source: str
    language: str
    duration: float
    model: str
    utterances: list[Utterance]
    speaker_names: dict[int, str]
    diarized: bool

    def speaker_label(self, spk: Optional[int]) -> str:
        if spk is None:
            return ""
        return self.speaker_names.get(spk, f"Falante {spk + 1}")


@dataclass
class Options:
    language: str = "pt"            # pt | en | auto | mixed
    model: str = "large-v3-turbo"
    diarize: bool = True
    num_speakers: int = 0           # 0 = automático
    cluster_threshold: float = 0.42  # menor = separa mais falantes
    hotwords: str = ""              # nomes próprios, jargões


ProgressFn = Callable[[str, float], None]   # (mensagem, fração 0..1 ou -1)
LogFn = Callable[[str], None]


class Engine:
    def __init__(self) -> None:
        self._model = None
        self._model_key: Optional[tuple] = None
        self._diarizer = None
        self._diarizer_key: Optional[tuple] = None
        self.device = "cpu"

    # ------------------------------------------------------------------ modelos
    def _load_whisper(self, key: str, progress: ProgressFn, log: LogFn,
                      cancel: threading.Event):
        if self._model is not None and self._model_key and self._model_key[0] == key:
            return self._model

        path = models.ensure_whisper(key, lambda f, m: progress(m, f), cancel)
        progress("Carregando modelo de transcrição…", -1)

        import ctranslate2
        from faster_whisper import WhisperModel

        threads = os.cpu_count() or 4
        attempts = []
        try:
            if ctranslate2.get_cuda_device_count() > 0:
                attempts.append(("cuda", "float16"))
        except Exception:
            pass
        attempts.append(("cpu", "int8"))

        last_err: Optional[Exception] = None
        for device, compute in attempts:
            try:
                self._model = WhisperModel(path, device=device, compute_type=compute,
                                           cpu_threads=threads)
                self._model_key = (key, device)
                self.device = device
                log(f"Modelo {key} carregado ({'GPU NVIDIA' if device == 'cuda' else 'CPU'}, {compute}).")
                return self._model
            except Exception as e:  # CUDA sem cuDNN/cuBLAS, por exemplo
                last_err = e
                log(f"Não foi possível usar {device}: {e}")
        raise RuntimeError(f"Falha ao carregar o modelo: {last_err}")

    def _load_diarizer(self, opts: Options, progress: ProgressFn, cancel: threading.Event):
        key = (opts.num_speakers, round(opts.cluster_threshold, 3))
        if self._diarizer is not None and self._diarizer_key == key:
            return self._diarizer

        seg, emb = models.ensure_diarization(lambda f, m: progress(m, f), cancel)
        import sherpa_onnx as so

        threads = os.cpu_count() or 4
        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg),
                num_threads=threads,
            ),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=threads),
            clustering=so.FastClusteringConfig(
                num_clusters=opts.num_speakers if opts.num_speakers > 0 else -1,
                threshold=opts.cluster_threshold,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("Configuração de diarização inválida.")
        self._diarizer = so.OfflineSpeakerDiarization(config)
        self._diarizer_key = key
        return self._diarizer

    # ------------------------------------------------------------------ pipeline
    def run(self, path: str, opts: Options, progress: ProgressFn, log: LogFn,
            cancel: threading.Event) -> Transcript:
        def check():
            if cancel.is_set():
                raise InterruptedError

        model = self._load_whisper(opts.model, progress, log, cancel)
        check()

        progress("Extraindo áudio do vídeo…", -1)
        audio = load_audio(path)
        duration = len(audio) / SAMPLE_RATE
        if duration < 0.1:
            raise RuntimeError("O arquivo não contém áudio.")
        log(f"Áudio extraído: {fmt_time(duration)} de duração.")
        check()

        # Pesos de progresso: transcrição 75%, diarização 25%.
        w_tr = 0.75 if opts.diarize else 1.0

        language, multilingual, prompt = self._resolve_language(model, audio, opts, log)
        check()

        progress("Transcrevendo…", 0.0)
        segments, info = model.transcribe(
            audio,
            language=language,
            multilingual=multilingual,
            initial_prompt=prompt,
            hotwords=opts.hotwords.strip() or None,
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
        )
        words: list[Word] = []
        for seg in segments:
            check()
            if seg.words:
                words.extend(Word(w.start, w.end, w.word) for w in seg.words)
            elif seg.text.strip():
                words.append(Word(seg.start, seg.end, seg.text))
            frac = min(seg.end / duration, 1.0)
            progress(f"Transcrevendo… {fmt_time(seg.end)} / {fmt_time(duration)}", frac * w_tr)
        detected = language or info.language
        log(f"Transcrição concluída: {len(words)} palavras.")

        speaker_names: dict[int, str] = {}
        if opts.diarize and words:
            check()
            progress("Identificando falantes…", w_tr)
            diarizer = self._load_diarizer(opts, progress, cancel)
            progress("Identificando falantes…", w_tr)

            def cb(done: int, total: int) -> int:
                if total:
                    progress(f"Identificando falantes… ({done}/{total})",
                             w_tr + (1 - w_tr) * done / total)
                return 1 if cancel.is_set() else 0

            result = diarizer.process(audio, callback=cb)
            check()
            turns = sorted(((s.start, s.end, s.speaker) for s in result.sort_by_start_time()),
                           key=lambda t: t[0])
            assign_speakers(words, turns)
            smooth_speakers(words)
            order = renumber_speakers(words)
            speaker_names = {i: f"Falante {i + 1}" for i in range(order)}
            log(f"Falantes identificados: {order}.")

        utterances = build_utterances(words, split_on_speaker=opts.diarize)
        progress("Concluído", 1.0)
        return Transcript(
            source=path,
            language=detected,
            duration=duration,
            model=opts.model,
            utterances=utterances,
            speaker_names=speaker_names,
            diarized=bool(speaker_names),
        )

    def _resolve_language(self, model, audio, opts: Options, log: LogFn):
        if opts.language == "mixed":
            log("Modo misto: idioma detectado a cada trecho.")
            return None, True, None
        if opts.language in PROMPTS:
            return opts.language, False, PROMPTS[opts.language]
        lang, prob, _ = model.detect_language(audio, vad_filter=True,
                                              language_detection_segments=3)
        log(f"Idioma detectado: {lang} ({prob * 100:.0f}% de confiança).")
        return lang, False, PROMPTS.get(lang)


# ---------------------------------------------------------------------- áudio
def load_audio(path: str) -> np.ndarray:
    """Decodifica qualquer formato suportado pelo FFmpeg (via PyAV) para mono 16 kHz."""
    import av

    chunks: list[np.ndarray] = []
    with av.open(path, mode="r") as container:
        if not container.streams.audio:
            raise RuntimeError("O arquivo não contém nenhuma faixa de áudio.")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


# ------------------------------------------------------------------ alinhamento
def assign_speakers(words: list[Word], turns: list[tuple[float, float, int]]) -> None:
    """Atribui a cada palavra o falante com maior sobreposição temporal."""
    if not turns:
        return
    starts = [t[0] for t in turns]
    max_len = max(t[1] - t[0] for t in turns)
    for w in words:
        hi = bisect.bisect_left(starts, w.end)
        best, best_ov = None, 0.0
        i = hi - 1
        while i >= 0 and starts[i] >= w.start - max_len:
            s, e, spk = turns[i]
            ov = min(e, w.end) - max(s, w.start)
            if ov > best_ov:
                best, best_ov = spk, ov
            i -= 1
        if best is None:
            # Sem sobreposição: usa o turno mais próximo.
            mid = (w.start + w.end) / 2
            cands = turns[max(hi - 2, 0):hi + 1]
            best = min(cands, key=lambda t: min(abs(t[0] - mid), abs(t[1] - mid)))[2]
        w.speaker = best


def smooth_speakers(words: list[Word]) -> None:
    """Corrige trocas espúrias de falante em 1–2 palavras curtas no meio da fala de outro."""
    runs: list[list[int]] = []
    for i, w in enumerate(words):
        if runs and words[runs[-1][0]].speaker == w.speaker:
            runs[-1].append(i)
        else:
            runs.append([i])
    for k in range(1, len(runs) - 1):
        run = runs[k]
        prev_spk = words[runs[k - 1][0]].speaker
        next_spk = words[runs[k + 1][0]].speaker
        dur = words[run[-1]].end - words[run[0]].start
        if prev_spk == next_spk and len(run) <= 2 and dur < 0.8:
            for i in run:
                words[i].speaker = prev_spk


def renumber_speakers(words: list[Word]) -> int:
    """Renumera falantes por ordem de primeira aparição. Retorna a quantidade."""
    mapping: dict[int, int] = {}
    for w in words:
        if w.speaker is None:
            continue
        if w.speaker not in mapping:
            mapping[w.speaker] = len(mapping)
        w.speaker = mapping[w.speaker]
    return len(mapping)


def build_utterances(words: list[Word], split_on_speaker: bool = True,
                     max_gap: float = 2.0, soft_max: float = 40.0) -> list[Utterance]:
    out: list[Utterance] = []
    cur: list[Word] = []

    def flush():
        if cur:
            text = "".join(w.text for w in cur).strip()
            if text:
                out.append(Utterance(cur[0].start, cur[-1].end, cur[0].speaker, text, list(cur)))
            cur.clear()

    for w in words:
        if cur:
            last = cur[-1]
            new_speaker = split_on_speaker and w.speaker != last.speaker
            long_gap = w.start - last.end > max_gap
            long_para = (w.start - cur[0].start > soft_max
                         and last.text.strip().endswith(SENTENCE_END))
            if new_speaker or long_gap or long_para:
                flush()
        cur.append(w)
    flush()
    return out


def fmt_time(t: float, ms: bool = False, sep: str = ".") -> str:
    total_ms = int(round(max(t, 0.0) * 1000))
    secs, millis = divmod(total_ms, 1000)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{millis:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def output_stem(path: str) -> Path:
    p = Path(path)
    return p.with_name(p.stem)
