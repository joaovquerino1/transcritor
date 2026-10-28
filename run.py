"""Ponto de entrada usado pelo PyInstaller e para rodar a partir do código-fonte."""
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from transcritor.cli import main as cli_main
        sys.exit(cli_main(sys.argv[2:]))
    from transcritor.app import main
    sys.exit(main())
