"""Navegador embutido para sites com login (Teams, SharePoint, OneDrive, Stream e outros).

O usuário entra com a própria conta e dá play na gravação; capturamos o manifesto do vídeo
(o mesmo que o player usa) para baixá-lo com o yt-dlp. Downloads feitos pelo botão
"Baixar" do próprio site também são aceitos. Não há quebra de DRM: vídeos protegidos não são
suportados.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QToolButton,
    QVBoxLayout,
)

from . import models
from .downloader import SHAREPOINT_PAGE, Job, sharepoint_manifest

_MANIFEST = re.compile(r"videomanifest|\.m3u8(\?|$)|\.mpd(\?|$)", re.I)
_SEGMENT = re.compile(r"[/_-](seg|segment|frag|chunk)[-_]?\d|\.(ts|m4s)(\?|$)", re.I)

_profile: Optional[QWebEngineProfile] = None
_cookies: dict = {}      # (domínio, caminho, nome) -> QNetworkCookie
# Cabeçalhos da requisição do manifesto que vale a pena repetir no download.
_KEEP_HEADERS = {"authorization", "x-ms-client-request-id", "origin", "referer", "user-agent"}


def profile() -> QWebEngineProfile:
    """Perfil persistente: o login fica salvo entre usos."""
    global _profile
    if _profile is None:
        storage = models.data_dir() / "browser"
        storage.mkdir(parents=True, exist_ok=True)
        from PySide6.QtWidgets import QApplication
        _profile = QWebEngineProfile("transcritor", QApplication.instance())
        _profile.setPersistentStoragePath(str(storage))
        _profile.setCachePath(str(storage / "cache"))
        _profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        # Alguns logins corporativos recusam navegadores "desconhecidos": remove o sufixo QtWebEngine.
        ua = re.sub(r"\s*QtWebEngine/\S+", "", _profile.httpUserAgent())
        _profile.setHttpUserAgent(ua)
        store = _profile.cookieStore()
        store.cookieAdded.connect(_cookie_added)
        store.cookieRemoved.connect(_cookie_removed)
        store.loadAllCookies()
    return _profile


def _ckey(c) -> tuple:
    return (c.domain(), c.path(), bytes(c.name()).decode("utf-8", "replace"))


def _cookie_added(c) -> None:
    _cookies[_ckey(c)] = c


def _cookie_removed(c) -> None:
    _cookies.pop(_ckey(c), None)


def export_cookies() -> str:
    """Grava os cookies do login num arquivo no formato Netscape (usado pelo yt-dlp)."""
    path = models.data_dir() / "cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for c in list(_cookies.values()):
        domain = c.domain()
        if not domain:
            continue
        expiry = 0 if c.isSessionCookie() else int(c.expirationDate().toSecsSinceEpoch())
        name = bytes(c.name()).decode("utf-8", "replace")
        value = bytes(c.value()).decode("utf-8", "replace")
        lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE",
                                c.path() or "/", "TRUE" if c.isSecure() else "FALSE",
                                str(expiry), name, value]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def _headers(info: QWebEngineUrlRequestInfo) -> dict:
    out = {}
    try:
        for k, v in dict(info.httpHeaders()).items():
            k = bytes(k).decode("latin-1")
            if k.lower() in _KEEP_HEADERS:
                out[k] = bytes(v).decode("latin-1")
    except Exception:
        pass
    return out


class Interceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, sink: "Sink") -> None:
        super().__init__()
        self.sink = sink

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        url = info.requestUrl().toString()
        if not url.startswith("http"):
            return
        rt = info.resourceType()
        if rt in (QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMainFrame,
                  QWebEngineUrlRequestInfo.ResourceType.ResourceTypeSubFrame) \
                and SHAREPOINT_PAGE.match(url):
            # Página do player do SharePoint (direto ou dentro do Teams).
            self.sink.page.emit(url)
        elif _MANIFEST.search(url) and not _SEGMENT.search(url):
            self.sink.found.emit(url, "stream", _headers(info))
        elif rt == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMedia \
                and re.search(r"\.(mp4|m4a|mov|webm|mp3)(\?|$)", url, re.I):
            self.sink.found.emit(url, "arquivo", _headers(info))


class Sink(QObject):
    found = Signal(str, str, dict)
    page = Signal(str)


def _stream_key(url: str) -> str:
    """Identifica o mesmo vídeo em requisições repetidas (o player pede o manifesto várias vezes)."""
    m = re.search(r"[?&](docid|id)=([^&]+)", url)
    if m:
        return m.group(2)
    return url.split("?")[0]


class LoginBrowser(QDialog):
    """Retorna em `selected` uma lista de `Job` e em `files` os arquivos baixados direto."""

    def __init__(self, start_url: str, download_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transcritor – Entrar e capturar vídeo")
        self.resize(1100, 780)
        self.download_dir = download_dir
        self.selected: list[Job] = []
        self.files: list[str] = []
        self.streams: dict[str, Job] = {}                # chave -> vídeo detectado
        self.page_url = ""                               # última página stream.aspx vista
        self._closed = False

        self.sink = Sink()
        self.sink.found.connect(self._on_found)
        self.sink.page.connect(self._on_page)
        self.interceptor = Interceptor(self.sink)

        prof = profile()
        prof.setUrlRequestInterceptor(self.interceptor)
        prof.downloadRequested.connect(self._on_download)

        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(prof, self.view)
        self.view.setPage(self.page)
        self.view.urlChanged.connect(self._on_url)

        top = QHBoxLayout()
        back = QToolButton()
        back.setText("◀")
        back.setToolTip("Voltar")
        back.clicked.connect(self.view.back)
        reload_ = QToolButton()
        reload_.setText("⟳")
        reload_.setToolTip("Recarregar")
        reload_.clicked.connect(self.view.reload)
        self.ed_url = QLineEdit()
        self.ed_url.returnPressed.connect(self._go)
        logout = QPushButton("Sair da conta")
        logout.setToolTip("Apaga o login salvo neste navegador")
        logout.clicked.connect(self._logout)
        top.addWidget(back)
        top.addWidget(reload_)
        top.addWidget(self.ed_url, 1)
        top.addWidget(logout)

        self.tip = QLabel("1. Entre com sua conta   2. Abra a gravação e aperte <b>play</b>   "
                          "3. Clique em <b>Baixar vídeo</b>")
        self.cb = QComboBox()
        self.cb.setMinimumWidth(380)
        self.cb.setPlaceholderText("Nenhum vídeo detectado ainda")
        self.btn_get = QPushButton("Baixar vídeo")
        self.btn_get.setDefault(True)
        self.btn_get.setEnabled(False)
        self.btn_get.clicked.connect(self._accept_selected)
        close = QPushButton("Fechar")
        close.clicked.connect(self._close)

        bottom = QHBoxLayout()
        bottom.addWidget(self.tip, 1)
        bottom.addWidget(self.cb)
        bottom.addWidget(self.btn_get)
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        lay.addLayout(bottom)

        self.view.load(QUrl.fromUserInput(start_url or "https://www.office.com"))

    # ------------------------------------------------------------------
    def _go(self) -> None:
        self.view.load(QUrl.fromUserInput(self.ed_url.text().strip()))

    def _title(self) -> str:
        t = self.view.title() or "gravacao"
        t = re.sub(r"\.(mp4|mov|m4a|webm|mkv)$", "", t, flags=re.I)
        t = re.sub(r"\s*[-|–]\s*(Microsoft Stream|SharePoint|OneDrive|Microsoft Teams)\s*$", "", t)
        return t.strip() or "gravacao"

    def _on_url(self, u: QUrl) -> None:
        self.ed_url.setText(u.toString())
        if SHAREPOINT_PAGE.match(u.toString()):
            self._on_page(u.toString())

    def _on_page(self, url: str) -> None:
        self.page_url = url
        if url in self.streams:
            return
        # No SharePoint dá para baixar sem dar play: o extrator usa a página e o login.
        self._add(url, Job(url, page_url=url), "página")

    def _on_found(self, url: str, kind: str, headers: dict) -> None:
        key = _stream_key(url)
        job = Job(url, page_url=self.page_url, headers=headers)
        if key in self.streams:
            self.streams[key] = job                  # guarda a requisição mais recente (token novo)
        else:
            self._add(key, job, kind)

    def _add(self, key: str, job: Job, kind: str) -> None:
        self.streams[key] = job
        self.cb.addItem(f"{self._title()} ({kind})", key)
        self.cb.setCurrentIndex(self.cb.count() - 1)
        self.btn_get.setEnabled(True)
        self.tip.setText("<b>Vídeo detectado!</b> Clique em <b>Baixar vídeo</b>.")

    def _accept_selected(self) -> None:
        key = self.cb.currentData()
        if key not in self.streams:
            return
        job = self.streams[key]
        job.title = self._title()
        job.cookies = export_cookies()
        self.btn_get.setEnabled(False)
        if SHAREPOINT_PAGE.match(self.view.url().toString()):
            # Lê do próprio player o link do manifesto com o token vigente.
            self.page.runJavaScript(
                "(function(){try{return JSON.stringify(window.g_fileInfo||null)}catch(e){return null}})()",
                0, lambda res: self._finish(job, res))
        else:
            self._finish(job, None)

    def _finish(self, job: Job, file_info) -> None:
        try:
            info = json.loads(file_info) if file_info else None
            if info and info.get(".transformUrl"):
                job.candidates = [sharepoint_manifest(info)]
                job.page_url = job.page_url or self.view.url().toString()
        except Exception:
            pass
        self.selected = [job]
        self._close(accept=True)

    def _on_download(self, item: QWebEngineDownloadRequest) -> None:
        # Botão "Baixar" do próprio site (quando o dono da gravação permite).
        from .downloader import unique_path
        target = unique_path(self.download_dir / item.downloadFileName())
        item.setDownloadDirectory(str(target.parent))
        item.setDownloadFileName(target.name)
        self.tip.setText(f"Baixando <b>{target.name}</b>…")

        def progress() -> None:
            total = item.totalBytes()
            if total > 0:
                self.tip.setText(f"Baixando <b>{target.name}</b>… {item.receivedBytes() * 100 // total}%")

        def done() -> None:
            if item.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                self.files.append(str(target))
                self.tip.setText(f"<b>{target.name}</b> baixado. Feche esta janela para transcrever, "
                                 "ou continue baixando.")
            elif item.isFinished():
                self.tip.setText("O download foi interrompido.")

        item.receivedBytesChanged.connect(progress)
        item.isFinishedChanged.connect(done)
        item.accept()

    def _logout(self) -> None:
        if QMessageBox.question(self, "Transcritor", "Apagar o login salvo neste navegador?") \
                == QMessageBox.StandardButton.Yes:
            prof = profile()
            prof.cookieStore().deleteAllCookies()
            prof.clearHttpCache()
            self.view.load(QUrl.fromUserInput(self.ed_url.text() or "https://www.office.com"))

    def _close(self, accept: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        prof = profile()
        prof.setUrlRequestInterceptor(None)
        try:
            prof.downloadRequested.disconnect(self._on_download)
        except (RuntimeError, TypeError):
            pass
        self.view.setPage(None)
        self.page.deleteLater()
        if accept or self.files:
            self.accept()
        else:
            self.reject()

    def closeEvent(self, e) -> None:
        self._close()
        super().closeEvent(e)
