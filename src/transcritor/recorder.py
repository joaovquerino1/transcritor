"""Gravação de reuniões: tela + som do computador (os outros participantes) + microfone.

macOS (15 ou mais novo): ScreenCaptureKit grava tudo direto num MP4.
Windows: áudio do sistema por loopback WASAPI e microfone (biblioteca soundcard); a tela
escolhida é capturada com o mss e codificada pelo FFmpeg. No fim, as faixas de áudio são
misturadas numa só, pronta para transcrever.

Durante a gravação, `on_audio(fonte, amostras, taxa)` recebe o áudio de cada fonte
("system" ou "mic") separadamente: é o que alimenta a transcrição ao vivo e os medidores.
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
AudioFn = Callable[[str, "object", int], None]   # (fonte, np.ndarray float32 mono, taxa)
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

    def __init__(self) -> None:
        self._warnings: list[str] = []

    def start(self, dest: Path, video: bool, system_audio: bool, mic: bool, display: int,
              log: LogFn, mic_device: str = "", on_audio: Optional[AudioFn] = None) -> None:
        raise NotImplementedError

    def stop(self, log: LogFn) -> Path:
        raise NotImplementedError

    def error(self) -> Optional[str]:
        """Erro ocorrido durante a gravação (ex.: permissão revogada)."""
        return None

    def warnings(self) -> list[str]:
        """Problemas que não param a gravação (ex.: o microfone foi bloqueado)."""
        return list(self._warnings)

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

        import objc

        class TranscritorAudioOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
                try:
                    samples, rate = _mac_samples(sbuf)
                    if samples is not None and len(samples):
                        self.owner._on_audio("mic" if int(kind) == 2 else "system", samples, rate)
                except Exception:  # noqa: BLE001  (nunca derrubar a thread do ScreenCaptureKit)
                    pass

        _classes = (TranscritorStreamDelegate, TranscritorRecordingDelegate, TranscritorAudioOutput)
    return _classes


def _mac_samples(sbuf):
    """CMSampleBuffer de áudio do ScreenCaptureKit -> (np.float32 mono, taxa)."""
    import CoreMedia as CM
    import numpy as np

    n = int(CM.CMSampleBufferGetNumSamples(sbuf))
    asbd = CM.CMAudioFormatDescriptionGetStreamBasicDescription(CM.CMSampleBufferGetFormatDescription(sbuf))
    rate, flags, channels, bits = float(asbd[0]), int(asbd[2]), int(asbd[6]), int(asbd[7])
    block = CM.CMSampleBufferGetDataBuffer(sbuf)
    if block is None or n == 0 or bits != 32 or not flags & 1:      # só float32
        return None, 0
    size = CM.CMBlockBufferGetDataLength(block)
    status, data = CM.CMBlockBufferCopyDataBytes(block, 0, size, None)
    if status != 0 or not data:
        return None, 0
    arr = np.frombuffer(bytes(data), dtype=np.float32)
    if channels > 1:
        if flags & 0x20:      # kAudioFormatFlagIsNonInterleaved: canais um depois do outro
            arr = arr[:n * channels].reshape(channels, n).mean(axis=0)
        else:
            arr = arr[:n * channels].reshape(n, channels).mean(axis=1)
    return arr[:n].astype(np.float32, copy=False), int(rate)


class MacRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
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
        msg = ("O macOS bloqueou o microfone, então a sua voz não está sendo gravada. Ative o "
               "Transcritor em Ajustes do Sistema > Privacidade e Segurança > Microfone.")
        log("Aviso: " + msg)
        self._warnings.append(msg)
        return False

    @staticmethod
    def mic_devices() -> list[tuple[str, str]]:
        try:
            import AVFoundation as AVF
            devs = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeAudio) or []
            return [(str(d.uniqueID()), str(d.localizedName())) for d in devs]
        except Exception:  # noqa: BLE001
            return []

    def start(self, dest, video, system_audio, mic, display, log, mic_device="", on_audio=None) -> None:
        import objc
        with objc.autorelease_pool():      # roda numa thread comum, sem pool do Cocoa
            self._start(dest, video, system_audio, mic, display, log, mic_device, on_audio)

    def _start(self, dest, video, system_audio, mic, display, log, mic_device, on_audio) -> None:
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
            if mic_device:
                cfg.setMicrophoneCaptureDeviceID_(mic_device)

        self.dest, self.video = dest, video
        self.raw = dest.with_name(f".{dest.stem}.gravando.mp4")
        rcfg = SCK.SCRecordingOutputConfiguration.alloc().init()
        rcfg.setOutputURL_(NSURL.fileURLWithPath_(str(self.raw)))
        rcfg.setOutputFileType_("public.mpeg-4")
        rcfg.setVideoCodecType_("avc1")

        StreamDelegate, RecDelegate, _ = _delegate_classes()
        sd, rd = StreamDelegate.alloc().init(), RecDelegate.alloc().init()
        sd.owner = rd.owner = self
        self._delegates = [sd, rd]
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, sd)
        output = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(rcfg, rd)
        ok, err = stream.addRecordingOutput_error_(output, None)
        if not ok:
            raise RuntimeError(f"Não foi possível preparar a gravação: {err}")
        if on_audio:
            import dispatch
            self._on_audio = on_audio
            ad = _delegate_classes()[2].alloc().init()
            ad.owner = self
            self._delegates.append(ad)
            self._queue = dispatch.dispatch_queue_create(b"br.com.transcritor.audio", None)
            kinds = ([SCK.SCStreamOutputTypeAudio] if system_audio else []) + \
                    ([SCK.SCStreamOutputTypeMicrophone] if mic else [])
            for kind in kinds:
                ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(ad, kind, self._queue, None)
                if not ok:
                    log(f"Aviso: sem áudio para a transcrição ao vivo ({err}).")

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


def _soundcard():
    """Importa o soundcard e prepara o COM nesta thread.

    O soundcard inicializa o COM ao ser importado e trata "já inicializado" como erro:
    importa primeiro (uma thread por vez) e só depois garante o COM nesta thread.
    """
    import ctypes
    with _sc_lock:
        import soundcard as sc
    ctypes.windll.ole32.CoInitializeEx(None, 0)
    return sc


def _in_thread(fn, timeout: float = 15):
    """Roda fn numa thread própria (o COM do soundcard não pode tocar a thread da interface)."""
    box: dict = {}

    def run() -> None:
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _win_monitors() -> list[dict]:
    """Monitores em pixels físicos, o principal primeiro."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:   # coordenadas reais mesmo com escala de 125%/150% (na interface o Qt já faz isso)
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        pass

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    mons: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                               ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, rect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(dd)
            name = dd.DeviceString if user32.EnumDisplayDevicesW(mi.szDevice, 0, ctypes.byref(dd), 0) else ""
            r = mi.rcMonitor
            mons.append({"left": r.left, "top": r.top, "width": r.right - r.left,
                         "height": r.bottom - r.top, "primary": bool(mi.dwFlags & 1), "name": name})
        return True

    user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    mons.sort(key=lambda m: (not m["primary"], m["left"], m["top"]))
    return mons


class WinRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._errors: list[str] = []
        self._ff: Optional[subprocess.Popen] = None
        self._video_err: Optional[str] = None
        self.parts: list[Path] = []
        self.video_part: Optional[Path] = None
        self.dest: Optional[Path] = None

    @staticmethod
    def displays() -> list[str]:
        try:
            mons = _win_monitors()
        except Exception:  # noqa: BLE001
            mons = []
        names = []
        for i, m in enumerate(mons):
            label = f"Tela {i + 1}: {m['width']}×{m['height']}"
            extra = [x for x in (m["name"] if "generic" not in m["name"].lower() else "",
                                 "principal" if m["primary"] else "") if x]
            names.append(label + (f" ({', '.join(extra)})" if extra else ""))
        if len(mons) > 1:
            names.append("Todas as telas")
        return names or ["Tela principal"]

    @staticmethod
    def mic_devices() -> list[tuple[str, str]]:
        def find():
            sc = _soundcard()
            return [(str(m.id), str(m.name)) for m in sc.all_microphones(include_loopback=False)]
        try:
            return _in_thread(find) or []
        except Exception:  # noqa: BLE001
            return []

    def _capture(self, kind: str, path: Path, t0: float, device: str, on_audio) -> None:
        """Grava um dispositivo de áudio num WAV mono 48 kHz, preenchendo silêncios pelo relógio."""
        import numpy as np
        try:
            sc = _soundcard()
            if kind == "system":
                dev = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
            else:
                dev = sc.get_microphone(id=device) if device else sc.default_microphone()
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
                        if on_audio and len(data):
                            on_audio(kind, data, sr)
        except Exception as e:  # noqa: BLE001
            if kind == "system":
                self._errors.append(f"som do computador: {e}")
                self._warnings.append(f"O som do computador não está sendo gravado ({e}).")
            else:
                self._errors.append(f"microfone: {e}")
                self._warnings.append(_mic_problem(str(e)))

    def _grab_screen(self, display: int, t0: float) -> None:
        """Captura a tela escolhida e entrega os quadros ao FFmpeg num ritmo fixo de FPS."""
        import numpy as np
        try:
            import mss
            factory = getattr(mss, "MSS", None) or mss.mss
            mons = _win_monitors()
            if display < len(mons):
                m = mons[display]
                box = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
            else:
                box = None
            with factory() as grabber:
                box = box or grabber.monitors[0]
                step = -(-box["width"] // MAX_WIDTH)        # reduz telas 4K por um fator inteiro
                w, h = box["width"] // step, box["height"] // step
                w, h = w - w % 2, h - h % 2
                cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{w}x{h}",
                       "-framerate", str(FPS), "-i", "-",
                       "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                       "-g", str(FPS * 4), str(self.video_part)]
                self._ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, **_popen_flags())
                sent = 0
                frame = b""
                while not self._stop.is_set():
                    shot = grabber.grab(box)
                    img = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
                    frame = np.ascontiguousarray(img[::step, ::step][:h, :w]).tobytes()
                    # Repete o quadro se a captura atrasar: o vídeo fica sempre alinhado com o áudio.
                    due = int((time.monotonic() - t0) * FPS) + 1
                    while sent < due:
                        self._ff.stdin.write(frame)
                        sent += 1
                    pause = t0 + sent / FPS - time.monotonic()
                    if pause > 0:
                        time.sleep(pause)
                due = int((time.monotonic() - t0) * FPS)
                while frame and sent < due:
                    self._ff.stdin.write(frame)
                    sent += 1
        except Exception as e:  # noqa: BLE001
            self._video_err = str(e)
        finally:
            if self._ff and self._ff.stdin:
                try:
                    self._ff.stdin.close()
                except OSError:
                    pass

    def start(self, dest, video, system_audio, mic, display, log, mic_device="", on_audio=None) -> None:
        self.dest = dest
        self._stop.clear()
        tmp = dest.with_name(f".{dest.stem}.gravando")
        tmp.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        if video:
            self.video_part = tmp / "tela.mkv"
            t = threading.Thread(target=self._grab_screen, args=(display, t0), daemon=True)
            t.start()
            self._video_thread = t
        for kind, on in (("system", system_audio), ("mic", mic)):
            if on:
                p = tmp / f"{kind}.wav"
                self.parts.append(p)
                t = threading.Thread(target=self._capture, args=(kind, p, t0, mic_device, on_audio),
                                     daemon=True)
                t.start()
                self._threads.append(t)
        time.sleep(1.5)
        if video and (self._video_err or (self._ff and self._ff.poll() is not None)):
            err = self._video_err or self._ff.stderr.read().decode(errors="replace")
            self._stop.set()
            raise RuntimeError(f"Não foi possível gravar a tela: {err.strip()[-300:]}")
        if self.parts and len(self._errors) == len(self.parts) and not video:
            self._stop.set()
            raise RuntimeError("Não foi possível gravar o áudio: " + "; ".join(self._errors))

    def error(self) -> Optional[str]:
        if self._video_err:
            return f"a gravação da tela parou: {self._video_err}"
        if self._ff and self._ff.poll() not in (None, 0):
            return "a gravação da tela parou inesperadamente"
        return None

    def stop(self, log) -> Path:
        self._stop.set()
        for t in self._threads:
            t.join(10)
        if self.video_part:
            self._video_thread.join(10)
        if self._ff:
            try:
                self._ff.communicate(timeout=60)
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


def _mic_problem(err: str) -> str:
    if "0x80070005" in err or "denied" in err.lower():
        return ("O Windows bloqueou o microfone para o Transcritor, então a sua voz não está sendo "
                "gravada. Ative “Permitir que aplicativos da área de trabalho acessem o "
                "microfone” em Configurações > Privacidade > Microfone. Se a opção estiver "
                "travada, ela é controlada pela TI da empresa.")
    return f"O microfone não está sendo gravado ({err}). Escolha outro em Microfone."


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


def mic_devices() -> list[tuple[str, str]]:
    """Microfones disponíveis como (id, nome). Pode levar um instante: chame fora da interface."""
    if sys.platform == "darwin":
        return MacRecorder.mic_devices()
    if sys.platform == "win32":
        return WinRecorder.mic_devices()
    return []
