"""Modo linha de comando: Transcritor --cli video.mp4 [--idioma pt] [--modelo large-v3-turbo] ..."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import downloader, export, models
from .engine import LANGUAGES, Engine, Options


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="Transcritor --cli", description="Transcreve vídeos com identificação de falantes.")
    ap.add_argument("arquivos", nargs="*", help="arquivos ou links (YouTube, stream .m3u8/.mpd…)")
    ap.add_argument("--gravar", type=float, metavar="SEGUNDOS",
                    help="grava tela, som do computador e microfone e depois transcreve "
                         "(0 = até apertar Enter)")
    ap.add_argument("--sem-tela", action="store_true", help="na gravação, só áudio")
    ap.add_argument("--sem-microfone", action="store_true", help="na gravação, sem o microfone")
    ap.add_argument("--manter-video", action="store_true", help="ao baixar links, manter o vídeo")
    ap.add_argument("--idioma", choices=list(LANGUAGES), default="pt")
    ap.add_argument("--modelo", choices=[m.key for m in models.WHISPER_MODELS], default="large-v3-turbo")
    ap.add_argument("--falantes", type=int, default=0, help="0 = automático")
    ap.add_argument("--sem-falantes", action="store_true", help="não identificar falantes")
    ap.add_argument("--vocabulario", default="")
    ap.add_argument("--formatos", default="txt,docx,srt", help=",".join(export.FORMATS))
    ap.add_argument("--saida", help="pasta de saída (padrão: a do vídeo)")
    args = ap.parse_args(argv)
    if not args.arquivos and args.gravar is None:
        ap.error("informe arquivos, links ou --gravar")

    formats = [f for f in args.formatos.split(",") if f in export.FORMATS]
    opts = Options(language=args.idioma, model=args.modelo, diarize=not args.sem_falantes,
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
            stem = Path(path).with_suffix("")
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
    rec.start(dest, not args.sem_tela, True, not args.sem_microfone, 0, log)
    print(f"== Gravando em {dest}" + (" (Enter para parar)" if not args.gravar else ""), flush=True)
    try:
        if args.gravar:
            time.sleep(args.gravar)
        else:
            input()
    except KeyboardInterrupt:
        pass
    path = rec.stop(log)
    print(f"  Gravado: {path}", flush=True)
    return path
