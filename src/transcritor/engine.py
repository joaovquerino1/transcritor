"""Pipeline: decodificar áudio do vídeo -> transcrever (Whisper) -> diarizar (sherpa-onnx) -> juntar."""

from __future__ import annotations

import bisect
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import asr, models

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
    clip: Optional[tuple[float, float]] = None   # trecho do arquivo original (os horários já contam a partir do início dele)

    def speaker_label(self, spk: Optional[int]) -> str:
        if spk is None:
            return ""
        return self.speaker_names.get(spk, f"Falante {spk + 1}")


CLIP_CONTEXT = 300.0     # segundos de contexto em volta de um trecho, para identificar falantes


@dataclass
class Options:
    language: str = "pt"            # pt | en | auto | mixed
    model: str = "large-v3-turbo"
    diarize: bool = True
    num_speakers: int = 0           # 0 = automático
    cluster_threshold: float = 0.42  # menor = separa mais falantes
    hotwords: str = ""              # nomes próprios, jargões
    start: float = 0.0              # trecho: início, em segundos
    end: float = 0.0                # trecho: fim (0 = até o fim do arquivo)


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

        progress("Extraindo áudio do vídeo…", -1)
        clipped = opts.start > 0 or opts.end > 0
        audio = load_audio(path, opts.start, opts.end)
        duration = len(audio) / SAMPLE_RATE
        if duration < 0.1:
            raise RuntimeError("O trecho escolhido não tem áudio." if clipped else "O arquivo não contém áudio.")
        if clipped:
            log(f"Trecho de {fmt_time(opts.start)} a {fmt_time(opts.start + duration)} "
                f"({fmt_time(duration)}).")
        else:
            log(f"Áudio extraído: {fmt_time(duration)} de duração.")
        check()

        # Pesos de progresso: transcrição 75%, diarização 25%.
        w_tr = 0.75 if opts.diarize else 1.0

        def tr_progress(frac: float) -> None:
            progress(f"Transcrevendo… {fmt_time(frac * duration)} / {fmt_time(duration)}", frac * w_tr)

        words: Optional[list[Word]] = None
        detected = opts.language if opts.language in PROMPTS else None
        if opts.model == "parakeet":
            progress("Carregando o modelo Parakeet…", -1)
            model = asr.parakeet(lambda f, m: progress(m, f), cancel)
            log("Modelo Parakeet carregado (CPU, int8). Idioma reconhecido automaticamente.")
            tr_progress(0.0)
            words = [Word(*w) for w in model.words(audio, tr_progress, cancel)]
            detected = detected or "auto"
        elif asr.mlx_available():
            try:
                words, detected = self._run_mlx(audio, opts, progress, tr_progress, log, cancel)
            except InterruptedError:
                raise
            except Exception as e:  # noqa: BLE001  GPU indisponível: segue no processador
                log(f"Não foi possível usar a GPU ({e}). Transcrevendo no processador.")
                words = None
        if words is None:
            model = self._load_whisper(opts.model, progress, log, cancel)
            check()
            words, detected = self._run_ct2(model, audio, opts, tr_progress, log, check)
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

            # Num trecho curto há pouca voz de cada pessoa para separá-las bem: a identificação
            # ouve também até 5 minutos antes e depois e depois fica só com o que cai no trecho.
            ctx, shift = audio, 0.0
            if clipped:
                cs = max(opts.start - CLIP_CONTEXT, 0.0)
                ctx = load_audio(path, cs, opts.start + duration + CLIP_CONTEXT)
                shift = opts.start - cs
                check()
            result = diarizer.process(ctx, callback=cb)
            check()
            turns = sorted(((s.start - shift, s.end - shift, s.speaker) for s in result.sort_by_start_time()
                            if s.end - shift > 0 and s.start - shift < duration),
                           key=lambda t: t[0])
            assign_speakers(words, turns)
            smooth_speakers(words)
            order = renumber_speakers(words)
            speaker_names = {i: f"Falante {i + 1}" for i in range(order)}
            log(f"Falantes identificados: {order}.")

        if clipped and words:
            # horários do arquivo original: o player e a legenda usam o vídeo inteiro
            for w in words:
                w.start += opts.start
                w.end += opts.start
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
            clip=(opts.start, opts.start + duration) if clipped else None,
        )

    def _run_ct2(self, model, audio, opts: Options, tr_progress, log: LogFn, check):
        """Whisper no processador (ou GPU NVIDIA) com o faster-whisper."""
        duration = len(audio) / SAMPLE_RATE
        language, multilingual, prompt = self._resolve_language(model, audio, opts, log)
        check()
        tr_progress(0.0)
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
            tr_progress(min(seg.end / duration, 1.0))
        return words, language or info.language

    def _run_mlx(self, audio, opts: Options, progress: ProgressFn, tr_progress, log: LogFn,
                 cancel: threading.Event):
        """Whisper na GPU do Mac (MLX): mesma precisão, cerca de 4x mais rápido."""
        path = models.ensure_mlx_whisper(opts.model, lambda f, m: progress(m, f), cancel)
        log(f"Modelo {opts.model} na GPU do Mac (MLX).")
        mixed = opts.language == "mixed"
        language = opts.language if opts.language in PROMPTS else None
        prompt = PROMPTS.get(language) if language else None
        if opts.hotwords.strip():
            prompt = f"{prompt or ''} {opts.hotwords.strip()}".strip()
        if mixed:
            log("Modo misto: idioma detectado a cada trecho.")
        tr_progress(0.0)
        raw, detected = asr.mlx_transcribe(audio, path, language, prompt, tr_progress, cancel,
                                           per_window_language=mixed)
        if opts.language == "auto" and detected:
            log(f"Idioma detectado: {detected}.")
        return [Word(*w) for w in raw], detected or ("pt" if mixed else None)

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
def load_audio(path: str, start: float = 0.0, end: float = 0.0) -> np.ndarray:
    """Decodifica qualquer formato suportado pelo FFmpeg (via PyAV) para mono 16 kHz.

    Com start/end, decodifica só o trecho (pula direto para perto do início, sem ler o resto).
    """
    import av

    chunks: list[np.ndarray] = []
    first_t: Optional[float] = None
    with av.open(path, mode="r") as container:
        if not container.streams.audio:
            raise RuntimeError("O arquivo não contém nenhuma faixa de áudio.")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"
        if start > 1:
            container.seek(int((start - 1) * av.time_base), any_frame=False)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            t = float(frame.pts * frame.time_base) if frame.pts is not None else None
            if end and t is not None and t > end:
                break
            if first_t is None:
                first_t = t if t is not None else 0.0
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
    if start > 0 or end > 0:
        a = max(int(round((start - (first_t or 0.0)) * SAMPLE_RATE)), 0)
        audio = audio[a:]
        if end > start:
            audio = audio[: int(round((end - start) * SAMPLE_RATE))]
    return audio


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


def output_stem(path: str, clip: Optional[tuple[float, float]] = None) -> Path:
    p = Path(path)
    if clip:   # trecho: não sobrescreve a transcrição do arquivo inteiro
        return p.with_name(f"{p.stem} (trecho {clip_label(clip[0])} a {clip_label(clip[1])})")
    return p.with_name(p.stem)


def clip_label(t: float) -> str:
    """00h12m05s: horário que pode ir em nome de arquivo (sem ":" por causa do Windows)."""
    h, rem = divmod(int(round(t)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"


def parse_time(text: str) -> float:
    """Aceita "1:02:03", "12:30", "95" ou "95.5" e devolve segundos."""
    parts = text.strip().replace(",", ".").split(":")
    if not text.strip() or len(parts) > 3:
        raise ValueError(f"horário inválido: {text!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total
