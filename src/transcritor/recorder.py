"""Gravação de reuniões: tela + som do computador (os outros participantes) + microfone.

macOS (15 ou mais novo): ScreenCaptureKit grava tudo direto num MP4.
Windows: áudio do sistema por loopback WASAPI e microfone (biblioteca soundcard), tela pelo
FFmpeg (gdigrab). No fim, as faixas de áudio são misturadas numa só, pronta para transcrever.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .downloader import ffmpeg_exe, unique_path

LogFn = Callable[[str], None]
MAX_WIDTH = 1920
FPS = 15


def default_name(folder: Path, video: bool) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d %Hh%M")
    return unique_path(folder / f"Reunião {stamp}{'.mp4' if video else '.m4a'}")


def _popen_flags() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _ffmpeg(args: list[str], log: LogFn) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", **_popen_flags())
    if r.returncode != 0:
        log(r.stderr.strip())
        raise RuntimeError(f"O FFmpeg falhou ao finalizar a gravação: {r.stderr.strip()[-300:]}")


def _audio_streams(path: Path) -> int:
    import av
    with av.open(str(path)) as c:
        return len(c.streams.audio)


def finalize(inputs: list[Path], dest: Path, log: LogFn) -> Path:
    """Junta as entradas no arquivo final: todas as faixas de áudio viram uma só (mixagem) e,
    se o destino for .mp4, o vídeo da primeira entrada é copiado sem recodificar."""
    args: list[str] = []
    tracks: list[str] = []
    for i, src in enumerate(inputs):
        args += ["-i", str(src)]
        tracks += [f"[{i}:a:{k}]" for k in range(_audio_streams(src))]
    if not tracks:
        raise RuntimeError("Nenhum áudio foi gravado.")
    if len(tracks) > 1:
        mix = "".join(tracks) + f"amix=inputs={len(tracks)}:duration=longest:normalize=0[a]"
        args += ["-filter_complex", mix, "-map", "[a]"]
    else:
        args += ["-map", tracks[0][1:-1]]
    if dest.suffix.lower() == ".mp4":
        args += ["-map", "0:v:0?", "-c:v", "copy", "-movflags", "+faststart"]
    args += ["-c:a", "aac", "-b:a", "128k", str(dest)]
    log("Finalizando a gravação…")
    _ffmpeg(args, log)
    return dest


class Recorder:
    """Interface comum. `start` e `stop` podem levar alguns segundos: chame fora da thread da UI."""

    def start(self, dest: Path, video: bool, system_audio: bool, mic: bool, display: int,
              log: LogFn) -> None:
        raise NotImplementedError

    def stop(self, log: LogFn) -> Path:
        raise NotImplementedError

    def error(self) -> Optional[str]:
        """Erro ocorrido durante a gravação (ex.: permissão revogada)."""
        return None

    def stalled(self) -> bool:
        """A gravação parece não estar recebendo dados."""
        return False


# ===================================================================== macOS
_classes = None


def _delegate_classes():
    """Classes Objective-C só podem ser registradas uma vez por processo."""
    global _classes
    if _classes is None:
        from Foundation import NSObject

        def _msg(error, default):
            return str(error.localizedDescription()) if error else default

        class TranscritorStreamDelegate(NSObject):
            def stream_didStopWithError_(self, stream, error):
                self.owner._err = _msg(error, "a gravação parou")
                self.owner._finished.set()

        class TranscritorRecordingDelegate(NSObject):
            def recordingOutputDidStartRecording_(self, out):
                self.owner._started.set()

            def recordingOutput_didFailWithError_(self, out, error):
                self.owner._err = _msg(error, "falha na gravação")
                self.owner._started.set()
                self.owner._finished.set()

            def recordingOutputDidFinishRecording_(self, out):
                self.owner._finished.set()

        _classes = (TranscritorStreamDelegate, TranscritorRecordingDelegate)
    return _classes


class MacRecorder(Recorder):
    def __init__(self) -> None:
        self._stream = None
        self._output = None
        self._delegates = []
        self._started = threading.Event()
        self._finished = threading.Event()
        self._err: Optional[str] = None
        self.dest: Optional[Path] = None
        self.raw: Optional[Path] = None
        self.video = True

    @staticmethod
    def _content():
        import ScreenCaptureKit as SCK

        box: dict = {}
        done = threading.Event()

        def handler(content, error):
            box["content"], box["error"] = content, error
            done.set()

        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, handler)
        if not done.wait(30):
            raise RuntimeError("O macOS não respondeu ao pedido de gravação da tela.")
        if box["error"] is not None or box["content"] is None:
            raise PermissionError(
                "Sem permissão para gravar a tela.\n\nAbra Ajustes do Sistema > Privacidade e "
                "Segurança > Gravação de Tela e Áudio do Sistema, ative o Transcritor e abra o app "
                f"de novo.\n\n({box['error'].localizedDescription() if box['error'] else 'sem detalhes'})")
        return box["content"]

    @staticmethod
    def displays() -> list[str]:
        try:
            from AppKit import NSScreen
            names = {}
            for s in NSScreen.screens():
                did = s.deviceDescription().get("NSScreenNumber")
                names[int(did)] = str(s.localizedName())
            import Quartz
            ids = Quartz.CGGetActiveDisplayList(16, None, None)[1] or []
            main = Quartz.CGMainDisplayID()
            ids = sorted(ids, key=lambda d: d != main)
            return [names.get(int(d), f"Tela {i + 1}") for i, d in enumerate(ids)]
        except Exception:
            return ["Tela principal"]

    def _request_mic(self, log: LogFn) -> bool:
        import AVFoundation as AVF
        status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
        if status == AVF.AVAuthorizationStatusAuthorized:
            return True
        if status == AVF.AVAuthorizationStatusNotDetermined:
            box = {}
            done = threading.Event()

            def handler(granted):
                box["ok"] = bool(granted)
                done.set()

            AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeAudio, handler)
            done.wait(120)
            return box.get("ok", False)
        log("Aviso: sem permissão para o microfone (Ajustes do Sistema > Privacidade e Segurança > "
            "Microfone). Gravando sem a sua voz.")
        return False

    def start(self, dest, video, system_audio, mic, display, log) -> None:
        import objc
        with objc.autorelease_pool():      # roda numa thread comum, sem pool do Cocoa
            self._start(dest, video, system_audio, mic, display, log)

    def _start(self, dest, video, system_audio, mic, display, log) -> None:
        import ScreenCaptureKit as SCK
        from CoreMedia import CMTimeMake
        from Foundation import NSURL

        if mic:
            mic = self._request_mic(log)
        content = self._content()
        displays = list(content.displays())
        if not displays:
            raise RuntimeError("Nenhuma tela encontrada.")
        import Quartz
        main = Quartz.CGMainDisplayID()
        displays.sort(key=lambda d: d.displayID() != main)
        disp = displays[min(display, len(displays) - 1)]

        flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(disp, [])
        cfg = SCK.SCStreamConfiguration.alloc().init()
        if video:
            w, h = int(disp.width()), int(disp.height())
            if w > MAX_WIDTH:
                h, w = int(h * MAX_WIDTH / w), MAX_WIDTH
            cfg.setWidth_(w - w % 2)
            cfg.setHeight_(h - h % 2)
            cfg.setMinimumFrameInterval_(CMTimeMake(1, FPS))
            cfg.setShowsCursor_(True)
        else:
            # Só áudio: o ScreenCaptureKit exige vídeo, então gravamos um quadro mínimo e descartamos.
            cfg.setWidth_(64)
            cfg.setHeight_(64)
            cfg.setMinimumFrameInterval_(CMTimeMake(1, 1))
            cfg.setShowsCursor_(False)
        cfg.setCapturesAudio_(bool(system_audio))
        cfg.setExcludesCurrentProcessAudio_(True)
        cfg.setSampleRate_(48000)
        cfg.setChannelCount_(2)
        if mic:
            cfg.setCaptureMicrophone_(True)

        self.dest, self.video = dest, video
        self.raw = dest.with_name(f".{dest.stem}.gravando.mp4")
        rcfg = SCK.SCRecordingOutputConfiguration.alloc().init()
        rcfg.setOutputURL_(NSURL.fileURLWithPath_(str(self.raw)))
        rcfg.setOutputFileType_("public.mpeg-4")
        rcfg.setVideoCodecType_("avc1")

        StreamDelegate, RecDelegate = _delegate_classes()
        sd, rd = StreamDelegate.alloc().init(), RecDelegate.alloc().init()
        sd.owner = rd.owner = self
        self._delegates = [sd, rd]
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, sd)
        output = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(rcfg, rd)
        ok, err = stream.addRecordingOutput_error_(output, None)
        if not ok:
            raise RuntimeError(f"Não foi possível preparar a gravação: {err}")

        box: dict = {}
        done = threading.Event()

        def started(error):
            box["error"] = error
            done.set()

        stream.startCaptureWithCompletionHandler_(started)
        if not done.wait(30):
            raise RuntimeError("O macOS não iniciou a gravação.")
        if box["error"] is not None:
            raise RuntimeError(f"Não foi possível iniciar a gravação: {box['error'].localizedDescription()}")
        self._stream, self._output = stream, output
        self._last_dur, self._last_change = 0.0, time.monotonic()
        self._started.wait(10)
        if self._err:
            raise RuntimeError(self._err)

    def error(self) -> Optional[str]:
        return self._err

    def stalled(self) -> bool:
        # Vigia: se a duração gravada parar de avançar, a interface avisa (sem interromper).
        if self._output is not None:
            try:
                from CoreMedia import CMTimeGetSeconds
                d = float(CMTimeGetSeconds(self._output.recordedDuration()))
                now = time.monotonic()
                if d > self._last_dur + 0.1:
                    self._last_dur, self._last_change = d, now
                elif now - self._last_change > 12:
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def stop(self, log) -> Path:
        import objc
        with objc.autorelease_pool():
            return self._stop(log)

    def _stop(self, log) -> Path:
        if self._stream is not None:
            done = threading.Event()
            self._stream.stopCaptureWithCompletionHandler_(lambda e: done.set())
            done.wait(30)
            self._finished.wait(30)
            self._stream = self._output = None
        if not self.raw or not self.raw.exists() or self.raw.stat().st_size == 0:
            raise RuntimeError(f"A gravação não gerou arquivo{': ' + self._err if self._err else ''}.")
        try:
            finalize([self.raw], self.dest, log)
        finally:
            self.raw.unlink(missing_ok=True)
        return self.dest


# =================================================================== Windows
_sc_lock = threading.Lock()

class WinRecorder(Recorder):
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._errors: list[str] = []
        self._ff: Optional[subprocess.Popen] = None
        self.parts: list[Path] = []
        self.video_part: Optional[Path] = None
        self.dest: Optional[Path] = None

    @staticmethod
    def displays() -> list[str]:
        return ["Todas as telas"]

    def _capture(self, kind: str, path: Path, t0: float) -> None:
        """Grava um dispositivo de áudio num WAV mono 48 kHz, preenchendo silêncios pelo relógio."""
        import ctypes

        import numpy as np
        try:
            # O soundcard inicializa o COM ao ser importado e trata "já inicializado" como erro:
            # importa primeiro (uma thread por vez) e só depois garante o COM nesta thread.
            with _sc_lock:
                import soundcard as sc
            ctypes.windll.ole32.CoInitializeEx(None, 0)
            if kind == "system":
                dev = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
            else:
                dev = sc.default_microphone()
            sr = 48000
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                written = 0
                with dev.recorder(samplerate=sr, channels=1, blocksize=1024) as rec:
                    while not self._stop.is_set():
                        data = rec.record(numframes=sr // 20)
                        data = np.asarray(data, dtype=np.float32).reshape(-1)
                        # O loopback não entrega nada quando o PC está em silêncio: completa com zeros.
                        expected = int((time.monotonic() - t0) * sr) - len(data)
                        if expected - written > sr // 4:
                            gap = expected - written
                            w.writeframes(np.zeros(gap, dtype=np.int16).tobytes())
                            written += gap
                        pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
                        w.writeframes(pcm.tobytes())
                        written += len(pcm)
        except Exception as e:  # noqa: BLE001
            name = "som do computador" if kind == "system" else "microfone"
            self._errors.append(f"{name}: {e}")

    def start(self, dest, video, system_audio, mic, display, log) -> None:
        self.dest = dest
        self._stop.clear()
        tmp = dest.with_name(f".{dest.stem}.gravando")
        tmp.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        if video:
            self.video_part = tmp / "tela.mkv"
            vf = f"scale='min({MAX_WIDTH},iw)':-2,format=yuv420p"
            cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "gdigrab", "-framerate", str(FPS), "-draw_mouse", "1", "-i", "desktop",
                   "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                   "-g", str(FPS * 4), str(self.video_part)]
            self._ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, **_popen_flags())
        for kind, on in (("system", system_audio), ("mic", mic)):
            if on:
                p = tmp / f"{kind}.wav"
                self.parts.append(p)
                t = threading.Thread(target=self._capture, args=(kind, p, t0), daemon=True)
                t.start()
                self._threads.append(t)
        time.sleep(1.0)
        if self._ff and self._ff.poll() is not None:
            err = self._ff.stderr.read().decode(errors="replace")
            self._stop.set()
            raise RuntimeError(f"Não foi possível gravar a tela: {err.strip()[-300:]}")
        if self.parts and len(self._errors) == len(self.parts) and not video:
            self._stop.set()
            raise RuntimeError("Não foi possível gravar o áudio: " + "; ".join(self._errors))

    def error(self) -> Optional[str]:
        if self._ff and self._ff.poll() not in (None, 0):
            return "a gravação da tela parou inesperadamente"
        return None

    def stop(self, log) -> Path:
        self._stop.set()
        for t in self._threads:
            t.join(10)
        if self._ff:
            try:
                self._ff.communicate(b"q", timeout=30)
            except subprocess.TimeoutExpired:
                self._ff.kill()
        for e in self._errors:
            log(f"Aviso: não foi possível gravar o {e}")
        audio = [p for p in self.parts if p.exists() and p.stat().st_size > 44]
        video = self.video_part if self.video_part and self.video_part.exists() else None
        dest = self.dest if video else self.dest.with_suffix(".m4a")
        try:
            return finalize(([video] if video else []) + audio, dest, log)
        finally:
            tmp = self.dest.with_name(f".{self.dest.stem}.gravando")
            for f in tmp.glob("*"):
                f.unlink(missing_ok=True)
            try:
                tmp.rmdir()
            except OSError:
                pass


# ===================================================================== geral
def unsupported() -> Optional[str]:
    """Motivo pelo qual a gravação não funciona neste computador (ou None)."""
    if sys.platform == "darwin":
        import platform
        major = int(platform.mac_ver()[0].split(".")[0] or 0)
        if major < 15:
            return "A gravação de reuniões precisa do macOS 15 (Sequoia) ou mais novo."
        try:
            import ScreenCaptureKit  # noqa: F401
        except ImportError:
            return "Componente ScreenCaptureKit indisponível."
        return None
    if sys.platform == "win32":
        # Não importa o soundcard aqui: ele inicializa o COM e atrapalharia o arrastar e soltar
        # do Qt na thread principal. Ele é importado só nas threads de gravação.
        import importlib.util
        if importlib.util.find_spec("soundcard") is None:
            return "Componente de áudio indisponível."
        return None
    return "A gravação de reuniões funciona apenas no Windows e no macOS."


def create() -> Recorder:
    return MacRecorder() if sys.platform == "darwin" else WinRecorder()


def displays() -> list[str]:
    return MacRecorder.displays() if sys.platform == "darwin" else WinRecorder.displays()
