"""Download de vídeos da internet: YouTube e +1000 sites, e gravações do Teams/SharePoint/Stream
(manifesto DASH capturado no navegador de login). Tudo via yt-dlp; o FFmpeg junta áudio e vídeo."""

from __future__ import annotations

import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import models

ProgressFn = Callable[[str, float], None]   # (mensagem, fração 0..1 ou -1)
LogFn = Callable[[str], None]

# Sites que exigem login corporativo: abrimos o navegador embutido e capturamos o vídeo.
LOGIN_HOSTS = ("sharepoint.com", "teams.microsoft.com", "teams.live.com", "stream.microsoft.com",
               "onedrive.live.com", "1drv.ms", "sharepoint-df.com")

YTDLP_ZIPAPP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
YTDLP_MAX_AGE = 7 * 24 * 3600


def default_download_dir() -> Path:
    path = Path.home() / "Downloads" / "Transcritor"
    path.mkdir(parents=True, exist_ok=True)
    return path


def classify(url: str) -> str:
    """'manifest' (link direto de stream), 'login' (Teams/SharePoint) ou 'ytdlp'."""
    low = url.lower()
    if "videomanifest" in low or re.search(r"\.(m3u8|mpd)(\?|$)", low):
        return "manifest"
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS):
        return "login"
    return "ytdlp"


def normalize_manifest(url: str) -> str:
    """Links 'videomanifest' do SharePoint funcionam melhor cortados logo após 'format=dash'."""
    if "videomanifest" in url.lower():
        i = url.find("format=dash")
        if i != -1:
            return url[: i + len("format=dash")]
    return url


def safe_filename(name: str, fallback: str = "video") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "").strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name[:150] or fallback)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ---------------------------------------------------------------- yt-dlp
_ytdlp_lock = threading.Lock()


def _load_ytdlp(log: LogFn):
    """Usa a versão mais recente do yt-dlp (atualizada semanalmente a partir do GitHub oficial),
    pois os sites mudam com frequência. Se não houver internet, usa a versão embutida."""
    with _ytdlp_lock:
        if "yt_dlp" not in sys.modules:
            zipapp = models.data_dir() / "yt-dlp.zip"
            try:
                age = time.time() - zipapp.stat().st_mtime if zipapp.exists() else None
                if age is None or age > YTDLP_MAX_AGE:
                    log("Atualizando o componente de download (yt-dlp)…")
                    tmp = zipapp.with_suffix(".part")
                    req = urllib.request.Request(YTDLP_ZIPAPP_URL, headers={"User-Agent": models.APP_NAME})
                    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                        f.write(r.read())
                    tmp.replace(zipapp)
            except Exception as e:
                log(f"Não foi possível atualizar o yt-dlp ({e}); usando a versão embutida.")
            if zipapp.exists():
                sys.path.insert(0, str(zipapp))
            try:
                import yt_dlp  # noqa: F401
            except Exception:
                # Zip corrompido ou incompatível: volta para a versão embutida.
                if str(zipapp) in sys.path:
                    sys.path.remove(str(zipapp))
                zipapp.unlink(missing_ok=True)
                for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                    del sys.modules[name]
        import yt_dlp
        return yt_dlp


class _Logger:
    def __init__(self, log: LogFn) -> None:
        self.log = log

    def debug(self, msg: str) -> None:
        if msg.startswith("[download] Destination") or msg.startswith("[Merger]"):
            self.log(msg)

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if "JavaScript runtime" not in msg:
            self.log(f"Aviso: {msg}")

    def error(self, msg: str) -> None:
        self.log(msg)


def download_ytdlp(url: str, out_dir: Path, keep_video: bool, progress: ProgressFn,
                   log: LogFn, cancel: threading.Event, title: str = "") -> list[str]:
    yt_dlp = _load_ytdlp(log)
    log(f"yt-dlp {yt_dlp.version.__version__}")
    files: list[str] = []
    last = [0.0]

    def hook(d: dict) -> None:
        if cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if d["status"] == "downloading":
            now = time.monotonic()
            if now - last[0] < 0.25:
                return
            last[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes") or 0
            name = title or (d.get("info_dict") or {}).get("title", "")
            speed = d.get("speed")
            extra = f" · {speed / 1e6:.1f} MB/s" if speed else ""
            if total:
                progress(f"Baixando “{name}”… {got * 100 / total:.0f}%{extra}", got / total)
            else:
                progress(f"Baixando “{name}”… {got / 1e6:.0f} MB{extra}", -1)

    def pp_hook(d: dict) -> None:
        if d["status"] == "started":
            progress("Processando o arquivo baixado…", -1)
        if d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
            path = d["info_dict"].get("filepath")
            if path and path not in files:
                files.append(path)

    if title:
        stem = unique_path(out_dir / (safe_filename(title, "gravacao") + ".mp4")).stem
        outtmpl = str(out_dir / stem.replace("%", "%%")) + ".%(ext)s"
    else:
        outtmpl = str(out_dir / "%(title).150B [%(id)s].%(ext)s")
    # H.264/AAC primeiro: abre em qualquer player. Só áudio basta para transcrever.
    video_fmt = ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/bv*[height<=1080]+ba/b")
    opts = {
        "outtmpl": outtmpl,
        "format": video_fmt if keep_video else "ba[acodec^=mp4a]/ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_exe(),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "logger": _Logger(log),
        "windowsfilenames": True,
        "noprogress": True,
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    started = time.time() - 1
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
        except yt_dlp.utils.DownloadCancelled:
            _remove_partials(out_dir, started)
            raise InterruptedError
        except yt_dlp.utils.DownloadError as e:
            if cancel.is_set():
                _remove_partials(out_dir, started)
                raise InterruptedError
            msg = re.sub(r"^ERROR:\s*", "", str(e))
            raise RuntimeError(msg) from None
    return [f for f in files if Path(f).exists()]


def _remove_partials(out_dir: Path, since: float) -> None:
    for f in out_dir.iterdir():
        try:
            if (".part" in f.name or f.suffix == ".ytdl") and f.stat().st_mtime >= since:
                f.unlink()
        except OSError:
            pass


def download(url: str, out_dir: Path, keep_video: bool, progress: ProgressFn, log: LogFn,
             cancel: threading.Event, title: str = "") -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if classify(url) == "manifest":
        # Gravações do Teams/SharePoint: manifesto DASH com token temporário embutido no link.
        return download_ytdlp(normalize_manifest(url), out_dir, keep_video, progress, log, cancel,
                              title=title or "gravacao")
    return download_ytdlp(url, out_dir, keep_video, progress, log, cancel)
