"""Modo linha de comando: Transcritor --cli video.mp4 [--idioma pt] [--modelo large-v3-turbo] ..."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import export, models
from .engine import LANGUAGES, Engine, Options


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="Transcritor --cli", description="Transcreve vídeos com identificação de falantes.")
    ap.add_argument("arquivos", nargs="+")
    ap.add_argument("--idioma", choices=list(LANGUAGES), default="pt")
    ap.add_argument("--modelo", choices=[m.key for m in models.WHISPER_MODELS], default="large-v3-turbo")
    ap.add_argument("--falantes", type=int, default=0, help="0 = automático")
    ap.add_argument("--sem-falantes", action="store_true", help="não identificar falantes")
    ap.add_argument("--vocabulario", default="")
    ap.add_argument("--formatos", default="txt,docx,srt", help=",".join(export.FORMATS))
    ap.add_argument("--saida", help="pasta de saída (padrão: a do vídeo)")
    args = ap.parse_args(argv)

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
    for path in args.arquivos:
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
