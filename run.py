"""Ponto de entrada usado pelo PyInstaller e para rodar a partir do código-fonte."""
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))


def _ensure_streams() -> None:
    """No Windows, apps sem console têm sys.stdout/stderr = None, e o tqdm (download dos
    modelos) quebra ao escrever neles. Redireciona para um arquivo de log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from transcritor.models import data_dir

    log = open(data_dir() / "transcritor.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _ensure_streams()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from transcritor.cli import main as cli_main
        sys.exit(cli_main(sys.argv[2:]))
    from transcritor.app import main
    sys.exit(main())
