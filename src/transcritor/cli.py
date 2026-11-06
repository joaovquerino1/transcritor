"""Modo linha de comando: Transcritor --cli video.mp4 [--idioma pt] [--modelo large-v3-turbo] ..."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import downloader, export, models
from .engine import LANGUAGES, Engine, Options, output_stem, parse_time


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="Transcritor --cli", description="Transcreve vídeos com identificação de falantes.")
    ap.add_argument("arquivos", nargs="*", help="arquivos ou links (YouTube, stream .m3u8/.mpd…)")
    ap.add_argument("--gravar", type=float, metavar="SEGUNDOS",
                    help="grava tela, som do computador e microfone e depois transcreve "
                         "(0 = até apertar Enter)")
    ap.add_argument("--sem-tela", action="store_true", help="na gravação, só áudio")
    ap.add_argument("--sem-microfone", action="store_true", help="na gravação, sem o microfone")
    ap.add_argument("--tela", type=int, default=1, help="na gravação, número da tela (veja --dispositivos)")
    ap.add_argument("--microfone", default="", help="na gravação, id do microfone (veja --dispositivos)")
    ap.add_argument("--ao-vivo", action="store_true", help="na gravação, mostra a transcrição enquanto grava")
    ap.add_argument("--dispositivos", action="store_true", help="lista telas e microfones e sai")
    ap.add_argument("--testar-audio", metavar="ARQUIVO", help=argparse.SUPPRESS)   # diagnóstico do player
    ap.add_argument("--manter-video", action="store_true", help="ao baixar links, manter o vídeo")
    ap.add_argument("--idioma", choices=list(LANGUAGES), default="pt")
    ap.add_argument("--modelo", choices=[m.key for m in models.WHISPER_MODELS], default="large-v3-turbo")
    ap.add_argument("--falantes", type=int, default=0, help="0 = automático")
    ap.add_argument("--sem-falantes", action="store_true", help="não identificar falantes")
    ap.add_argument("--vocabulario", default="")
    ap.add_argument("--formatos", default="txt,docx,srt", help=",".join(export.FORMATS))
    ap.add_argument("--saida", help="pasta de saída (padrão: a do vídeo)")
    ap.add_argument("--inicio", type=parse_time, default=0.0, metavar="HH:MM:SS",
                    help="transcrever só um trecho: a partir daqui")
    ap.add_argument("--fim", type=parse_time, default=0.0, metavar="HH:MM:SS",
                    help="transcrever só um trecho: até aqui")
    args = ap.parse_args(argv)
    if args.testar_audio:
        return _test_audio(args.testar_audio)
    if args.dispositivos:
        from . import recorder
        print("Telas:")
        for i, name in enumerate(recorder.displays(), 1):
            print(f"  {i}. {name}")
        print("Microfones:")
        for dev_id, name in recorder.mic_devices():
            print(f"  {name}\n     id: {dev_id}")
        return 0
    if not args.arquivos and args.gravar is None:
        ap.error("informe arquivos, links ou --gravar")

    formats = [f for f in args.formatos.split(",") if f in export.FORMATS]
    if args.fim and args.fim <= args.inicio:
        ap.error("--fim precisa ser depois de --inicio")
    opts = Options(start=args.inicio, end=args.fim,
                   language=args.idioma, model=args.modelo, diarize=not args.sem_falantes,
                   num_speakers=args.falantes, hotwords=args.vocabulario)
    engine, cancel, last = Engine(), threading.Event(), [""]

    def progress(msg: str, frac: float) -> None:
        key = msg.split("(")[0].split("…")[0]
        if key != last[0]:
            last[0] = key
            print(f"  {msg}", flush=True)

    errors = 0
    paths: list[str] = []
    for item in args.arquivos:
        if not item.lower().startswith(("http://", "https://")):
            paths.append(item)
            continue
        print(f"== Baixando {item}", flush=True)
        try:
            out = Path(args.saida) if args.saida else downloader.default_download_dir()
            got = downloader.download(item, out, args.manter_video, progress,
                                      lambda m: print(f"  {m}", flush=True), cancel)
            for g in got:
                print(f"  Baixado: {g}", flush=True)
            paths += got
        except Exception as e:
            errors += 1
            print(f"  ERRO: {e}", file=sys.stderr, flush=True)

    if args.gravar is not None:
        try:
            paths.append(str(_record(args)))
        except Exception as e:
            errors += 1
            print(f"  ERRO na gravação: {e}", file=sys.stderr, flush=True)

    for path in paths:
        print(f"== {path}", flush=True)
        try:
            tr = engine.run(path, opts, progress, lambda m: print(f"  {m}", flush=True), cancel)
            stem = output_stem(path, tr.clip)
            if args.saida:
                stem = Path(args.saida) / stem.name
            for fmt in formats:
                print(f"  Salvo: {export.export(tr, fmt, stem)}", flush=True)
        except Exception as e:
            errors += 1
            print(f"  ERRO: {e}", file=sys.stderr, flush=True)
    return 1 if errors else 0


def _record(args) -> Path:
    import time

    from . import recorder
    reason = recorder.unsupported()
    if reason:
        raise RuntimeError(reason)
    out = Path(args.saida) if args.saida else downloader.default_download_dir()
    out.mkdir(parents=True, exist_ok=True)
    dest = recorder.default_name(out, not args.sem_tela)
    rec = recorder.create()
    log = lambda m: print(f"  {m}", flush=True)  # noqa: E731
    live = None
    if args.ao_vivo:
        from .engine import fmt_time
        from .live import SOURCE_NAMES, LiveTranscriber

        def show(seg) -> None:
            print(f"  [ao vivo {fmt_time(seg.start)}] {SOURCE_NAMES[seg.source]}: {seg.text}", flush=True)

        live = LiveTranscriber(args.idioma, args.vocabulario, show,
                               lambda m: m and print(f"  {m}", flush=True), log)
        live.start()
    rec.start(dest, not args.sem_tela, True, not args.sem_microfone, max(args.tela - 1, 0), log,
              mic_device=args.microfone, on_audio=live.feed if live else None)
    for w in rec.warnings():
        print(f"  Aviso: {w}", flush=True)
    print(f"== Gravando em {dest}" + (" (Enter para parar)" if not args.gravar else ""), flush=True)
    try:
        if args.gravar:
            time.sleep(args.gravar)
        else:
            input()
    except KeyboardInterrupt:
        pass
    if live:
        live.stop()
        live._thread.join(60)
    path = rec.stop(log)
    print(f"  Gravado: {path}", flush=True)
    return path


def _test_audio(path: str) -> int:
    """Toca o arquivo a partir de 5 s por 2 s e confere se o player andou e se chegaram
    quadros de vídeo com o horário certo (plugins do Qt Multimedia, usados pelo player com legenda)."""
    import time

    from PySide6.QtCore import QCoreApplication, QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    app = QCoreApplication.instance() or QCoreApplication([])
    player, out, sink = QMediaPlayer(), QAudioOutput(), QVideoSink()
    out.setVolume(0.0)
    player.setAudioOutput(out)
    player.setVideoSink(sink)
    frames: list[float] = []
    sink.videoFrameChanged.connect(lambda f: f.isValid() and frames.append(f.startTime() / 1e6))
    player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
    t = time.time()
    while player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadingMedia, QMediaPlayer.MediaStatus.NoMedia) \
            and time.time() - t < 10:
        app.processEvents()
        time.sleep(0.02)
    player.setPosition(5000)
    player.play()
    t = time.time()
    while time.time() - t < 2:
        app.processEvents()
        time.sleep(0.02)
    pos = player.position() / 1000
    ok = pos > 5.5
    print(f"Player: {pos:.1f} s, {player.errorString() or 'sem erro'} -> {'OK' if ok else 'FALHOU'}", flush=True)
    import os
    print(f"Vídeo: hasVideo={player.hasVideo()} faixas={len(player.videoTracks())} quadros={len(frames)} "
          f"hw={os.environ.get('QT_FFMPEG_DECODING_HW_DEVICE_TYPES', 'padrão')!r}", flush=True)
    if player.hasVideo() or player.videoTracks():
        last = frames[-1] if frames else -1.0
        good = len(frames) > 10 and abs(last - pos) < 0.5
        print(f"Vídeo: {len(frames)} quadros, último em {last:.2f} s -> {'OK' if good else 'FALHOU'}", flush=True)
        ok = ok and good
    return 0 if ok else 1
