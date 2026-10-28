"""Interface gráfica (PySide6)."""

from __future__ import annotations

import html
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import __version__, downloader, export, models
from .engine import LANGUAGES, Engine, Options, Transcript, fmt_time, output_stem

MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".ts", ".mts", ".m2ts", ".vob", ".ogv", ".mxf", ".f4v", ".asf", ".rm", ".rmvb",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".amr",
}

SPEAKER_COLORS = ["#1f5fbf", "#b3401a", "#2e7d32", "#7b1fa2", "#00838f", "#ad1457", "#6d4c41", "#455a64"]

STATUS = {"pending": "⏳", "running": "⚙️", "done": "✅", "error": "❌", "cancelled": "⏹"}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets" / name


class FileList(QListWidget):
    filesDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(110)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        files: list[str] = []
        for p in map(Path, paths):
            if p.is_dir():
                files += [str(f) for f in sorted(p.rglob("*")) if f.suffix.lower() in MEDIA_EXTS]
            else:
                files.append(str(p))
        self.filesDropped.emit(files)
        e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            from PySide6.QtGui import QPainter
            p = QPainter(self.viewport())
            p.setPen(self.palette().placeholderText().color())
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                       "Arraste vídeos ou áudios para cá\n(qualquer formato)")


class Worker(QObject):
    progress = Signal(int, str, float)
    log = Signal(str)
    fileDone = Signal(int, object, list)
    fileFailed = Signal(int, str)
    finished = Signal()

    def __init__(self, engine: Engine, jobs: list[tuple[int, str]], opts: Options,
                 formats: list[str], out_dir: Optional[str], cancel: threading.Event) -> None:
        super().__init__()
        self.engine, self.jobs, self.opts = engine, jobs, opts
        self.formats, self.out_dir, self.cancel = formats, out_dir, cancel

    @Slot()
    def run(self) -> None:
        for row, path in self.jobs:
            if self.cancel.is_set():
                break
            self.log.emit(f"— {Path(path).name}")
            try:
                tr = self.engine.run(
                    path, self.opts,
                    lambda m, f, r=row: self.progress.emit(r, m, f),
                    self.log.emit, self.cancel,
                )
                written = save_outputs(tr, self.formats, self.out_dir)
                for w in written:
                    self.log.emit(f"Salvo: {w}")
                self.fileDone.emit(row, tr, [str(w) for w in written])
            except InterruptedError:
                self.fileFailed.emit(row, "Cancelado")
                break
            except Exception as e:
                self.log.emit(traceback.format_exc())
                self.fileFailed.emit(row, str(e))
        self.finished.emit()


class DownloadWorker(QObject):
    progress = Signal(str, float)
    log = Signal(str)
    downloaded = Signal(list)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, jobs: list[tuple[str, str]], out_dir: Path, keep_video: bool,
                 cancel: threading.Event) -> None:
        super().__init__()
        self.jobs, self.out_dir, self.keep_video, self.cancel = jobs, out_dir, keep_video, cancel

    @Slot()
    def run(self) -> None:
        for url, title in self.jobs:
            if self.cancel.is_set():
                break
            self.log.emit(f"— Baixando {title or url}")
            try:
                paths = downloader.download(url, self.out_dir, self.keep_video,
                                            self.progress.emit, self.log.emit, self.cancel, title)
                for p in paths:
                    self.log.emit(f"Baixado: {p}")
                self.downloaded.emit(paths)
            except InterruptedError:
                break
            except Exception as e:
                self.log.emit(traceback.format_exc())
                self.failed.emit(str(e))
        self.finished.emit()


def save_outputs(tr: Transcript, formats: list[str], out_dir: Optional[str]) -> list[Path]:
    stem = output_stem(tr.source)
    if out_dir:
        stem = Path(out_dir) / stem.name
    return [export.export(tr, fmt, stem) for fmt in formats]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Transcritor")
        self.resize(1180, 860)
        self.settings = QSettings("Transcritor", "Transcritor")
        self.engine = Engine()
        self.files: list[dict] = []       # {path, status, transcript, outputs}
        self.thread: Optional[QThread] = None
        self.worker: Optional[Worker] = None
        self.cancel = threading.Event()
        self.current_row = -1
        self.dl_new: list[str] = []

        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 6, 12)

        lv.addWidget(QLabel("<b>Arquivos</b>"))
        self.list = FileList()
        self.list.filesDropped.connect(self.add_files)
        self.list.currentRowChanged.connect(self.show_row)
        lv.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Adicionar…")
        self.btn_add.clicked.connect(self.choose_files)
        self.btn_remove = QPushButton("Remover")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear = QPushButton("Limpar")
        self.btn_clear.clicked.connect(self.clear_files)
        for b in (self.btn_add, self.btn_remove, self.btn_clear):
            row.addWidget(b)
        lv.addLayout(row)

        dl = QGroupBox("Baixar da internet")
        dv = QVBoxLayout(dl)
        row = QHBoxLayout()
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("Cole um link do YouTube, Teams, SharePoint, Vimeo, Instagram…")
        self.ed_url.setClearButtonEnabled(True)
        self.ed_url.returnPressed.connect(self.download_url)
        self.btn_dl = QPushButton("Baixar")
        self.btn_dl.clicked.connect(self.download_url)
        row.addWidget(self.ed_url, 1)
        row.addWidget(self.btn_dl)
        dv.addLayout(row)
        row = QHBoxLayout()
        self.btn_login = QPushButton("Teams / SharePoint (com login)…")
        self.btn_login.setToolTip("Abre um navegador para entrar com sua conta e capturar gravações\n"
                                  "do Teams, SharePoint, OneDrive, Stream ou outros sites com login.")
        self.btn_login.clicked.connect(lambda: self.open_login())
        self.ck_video = QCheckBox("Manter o vídeo")
        self.ck_video.setToolTip("Desmarcado: baixa só o áudio, mais rápido e suficiente para transcrever.")
        self.ck_auto = QCheckBox("Transcrever ao terminar")
        row.addWidget(self.btn_login)
        row.addStretch(1)
        row.addWidget(self.ck_video)
        row.addWidget(self.ck_auto)
        dv.addLayout(row)
        lv.addWidget(dl)

        box = QGroupBox("Configurações")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.cb_lang = QComboBox()
        for k, v in LANGUAGES.items():
            self.cb_lang.addItem(v, k)
        form.addRow("Idioma:", self.cb_lang)

        self.cb_model = QComboBox()
        for m in models.WHISPER_MODELS:
            self.cb_model.addItem(f"{m.label} · {m.approx_mb / 1000:.1f} GB", m.key)
        form.addRow("Modelo:", self.cb_model)

        self.ck_diar = QCheckBox("Identificar quem está falando")
        self.ck_diar.toggled.connect(self._sync_enabled)
        form.addRow("Falantes:", self.ck_diar)

        self.sp_speakers = QSpinBox()
        self.sp_speakers.setRange(0, 20)
        self.sp_speakers.setSpecialValueText("Automático")
        self.sp_speakers.valueChanged.connect(self._sync_enabled)
        form.addRow("Nº de falantes:", self.sp_speakers)

        sens = QHBoxLayout()
        sens.addWidget(QLabel("menos"))
        self.sl_sens = QSlider(Qt.Orientation.Horizontal)
        self.sl_sens.setRange(0, 10)       # 0 → limiar 0,60 · 10 → limiar 0,30
        self.sl_sens.setToolTip("Aumente se vozes diferentes estiverem sendo juntadas;\n"
                                "diminua se a mesma pessoa aparecer como vários falantes.")
        sens.addWidget(self.sl_sens, 1)
        sens.addWidget(QLabel("mais"))
        self.lbl_sens = QLabel("Separação de vozes:")
        form.addRow(self.lbl_sens, sens)

        self.ed_hot = QLineEdit()
        self.ed_hot.setPlaceholderText("Opcional: nomes próprios, siglas, termos técnicos…")
        form.addRow("Vocabulário:", self.ed_hot)

        out = QHBoxLayout()
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("Mesma pasta do vídeo")
        self.ed_out.setClearButtonEnabled(True)
        b = QPushButton("…")
        b.setFixedWidth(36)
        b.clicked.connect(self.choose_out_dir)
        out.addWidget(self.ed_out, 1)
        out.addWidget(b)
        form.addRow("Salvar em:", out)

        fmts = QVBoxLayout()
        self.ck_fmt: dict[str, QCheckBox] = {}
        for k, v in export.FORMATS.items():
            c = QCheckBox(v)
            self.ck_fmt[k] = c
            fmts.addWidget(c)
        form.addRow("Formatos:", fmts)
        lv.addWidget(box)

        row = QHBoxLayout()
        self.btn_start = QPushButton("Transcrever")
        self.btn_start.setDefault(True)
        self.btn_start.setMinimumHeight(34)
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setMinimumHeight(34)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.stop)
        row.addWidget(self.btn_start, 2)
        row.addWidget(self.btn_cancel, 1)
        lv.addLayout(row)

        self.status = QLabel("Pronto.")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        lv.addWidget(self.status)
        lv.addWidget(self.bar)

        # Painel direito
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 12, 12, 12)
        self.tabs = QTabWidget()

        view = QWidget()
        vv = QVBoxLayout(view)
        vv.setContentsMargins(0, 6, 0, 0)
        self.lbl_title = QLabel("Selecione um arquivo transcrito para ver o resultado.")
        self.lbl_title.setWordWrap(True)
        vv.addWidget(self.lbl_title)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        vv.addWidget(self.text, 1)

        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Falante", "Nome (edite para renomear)"])
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setMaximumHeight(150)
        self.tbl.itemChanged.connect(self._rename_changed)
        vv.addWidget(self.tbl)

        row = QHBoxLayout()
        self.btn_resave = QPushButton("Salvar arquivos novamente")
        self.btn_resave.clicked.connect(self.resave)
        self.btn_open = QPushButton("Abrir pasta")
        self.btn_open.clicked.connect(self.open_folder)
        self.btn_copy = QPushButton("Copiar texto")
        self.btn_copy.clicked.connect(self.copy_text)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        row.addWidget(self.btn_resave)
        row.addWidget(self.btn_open)
        vv.addLayout(row)
        self.tabs.addTab(view, "Transcrição")

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(5000)
        self.tabs.addTab(self.logbox, "Registro")
        rv.addWidget(self.tabs)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 760])
        self.setCentralWidget(split)
        self._set_result_enabled(False)

    def _sync_enabled(self) -> None:
        on = self.ck_diar.isChecked()
        self.sp_speakers.setEnabled(on)
        auto = on and self.sp_speakers.value() == 0
        self.sl_sens.setEnabled(auto)
        self.lbl_sens.setEnabled(auto)

    def _set_result_enabled(self, on: bool) -> None:
        for w in (self.btn_resave, self.btn_open, self.btn_copy, self.tbl):
            w.setEnabled(on)

    # ------------------------------------------------------------ settings
    def _load_settings(self) -> None:
        s = self.settings
        self._set_combo(self.cb_lang, s.value("language", "pt"))
        self._set_combo(self.cb_model, s.value("model", "large-v3-turbo"))
        self.ck_diar.setChecked(s.value("diarize", True, type=bool))
        self.sp_speakers.setValue(s.value("speakers", 0, type=int))
        self.sl_sens.setValue(s.value("sensitivity", 6, type=int))
        self.ed_out.setText(s.value("out_dir", "", type=str))
        saved = s.value("formats", "txt,docx,srt", type=str).split(",")
        for k, c in self.ck_fmt.items():
            c.setChecked(k in saved)
        self.ck_video.setChecked(s.value("keep_video", False, type=bool))
        self.ck_auto.setChecked(s.value("auto_transcribe", True, type=bool))
        self._sync_enabled()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("language", self.cb_lang.currentData())
        s.setValue("model", self.cb_model.currentData())
        s.setValue("diarize", self.ck_diar.isChecked())
        s.setValue("speakers", self.sp_speakers.value())
        s.setValue("sensitivity", self.sl_sens.value())
        s.setValue("out_dir", self.ed_out.text())
        s.setValue("formats", ",".join(self._formats()))
        s.setValue("keep_video", self.ck_video.isChecked())
        s.setValue("auto_transcribe", self.ck_auto.isChecked())

    @staticmethod
    def _set_combo(cb: QComboBox, data) -> None:
        i = cb.findData(data)
        if i >= 0:
            cb.setCurrentIndex(i)

    def _formats(self) -> list[str]:
        return [k for k, c in self.ck_fmt.items() if c.isChecked()]

    def _options(self) -> Options:
        return Options(
            language=self.cb_lang.currentData(),
            model=self.cb_model.currentData(),
            diarize=self.ck_diar.isChecked(),
            num_speakers=self.sp_speakers.value(),
            cluster_threshold=0.60 - 0.03 * self.sl_sens.value(),
            hotwords=self.ed_hot.text(),
        )

    # ---------------------------------------------------------------- files
    def choose_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Escolher vídeos", self.settings.value("last_dir", str(Path.home())),
            f"Vídeos e áudios ({exts});;Todos os arquivos (*)")
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        known = {f["path"] for f in self.files}
        for p in paths:
            if p in known:
                continue
            self.files.append({"path": p, "status": "pending", "transcript": None, "outputs": []})
            item = QListWidgetItem()
            item.setToolTip(p)
            self.list.addItem(item)
            self._refresh_item(len(self.files) - 1)

    def _refresh_item(self, row: int, extra: str = "") -> None:
        f = self.files[row]
        text = f"{STATUS[f['status']]}  {Path(f['path']).name}"
        if extra:
            text += f"  —  {extra}"
        self.list.item(row).setText(text)

    def remove_selected(self) -> None:
        if self.thread:
            return
        for row in sorted((self.list.row(i) for i in self.list.selectedItems()), reverse=True):
            self.list.takeItem(row)
            del self.files[row]
        self.show_row(self.list.currentRow())

    def clear_files(self) -> None:
        if self.thread:
            return
        self.list.clear()
        self.files.clear()
        self.show_row(-1)

    def choose_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Pasta de saída", self.ed_out.text() or str(Path.home()))
        if d:
            self.ed_out.setText(d)

    # ------------------------------------------------------------- running
    def start(self) -> None:
        jobs = [(i, f["path"]) for i, f in enumerate(self.files) if f["status"] != "done"]
        if not jobs:
            QMessageBox.information(self, "Transcritor",
                                    "Adicione um ou mais vídeos primeiro (arraste para a lista).")
            return
        formats = self._formats()
        if not formats:
            QMessageBox.warning(self, "Transcritor", "Escolha pelo menos um formato de saída.")
            return
        out_dir = self.ed_out.text().strip() or None
        if out_dir and not Path(out_dir).is_dir():
            QMessageBox.warning(self, "Transcritor", "A pasta de saída não existe.")
            return
        self._save_settings()

        for i, _ in jobs:
            self.files[i]["status"] = "pending"
            self._refresh_item(i)
        self.cancel.clear()
        self.thread = QThread()
        self.worker = Worker(self.engine, jobs, self._options(), formats, out_dir, self.cancel)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.append_log)
        self.worker.fileDone.connect(self.on_done)
        self.worker.fileFailed.connect(self.on_failed)
        self.worker.finished.connect(self.on_finished)
        self._running(True)
        self.thread.start()

    def stop(self) -> None:
        self.cancel.set()
        self.status.setText("Cancelando…")
        self.btn_cancel.setEnabled(False)

    # ------------------------------------------------------------ download
    def _download_dir(self) -> Path:
        out = self.ed_out.text().strip()
        if out and Path(out).is_dir():
            return Path(out)
        return downloader.default_download_dir()

    def download_url(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.ed_url.setFocus()
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if downloader.classify(url) == "login":
            self.open_login(url)
        else:
            self.start_download([(url, "")])

    def open_login(self, url: str = "") -> None:
        from .login_browser import LoginBrowser

        start = url or self.settings.value("login_url", "https://www.office.com", type=str)
        dlg = LoginBrowser(start, self._download_dir(), self)
        dlg.exec()
        self.settings.setValue("login_url", dlg.ed_url.text() or start)
        if dlg.files:
            self.add_files(dlg.files)
        if dlg.selected:
            self.ed_url.clear()
            self.start_download(dlg.selected)
        elif dlg.files and self.ck_auto.isChecked():
            self.start()

    def start_download(self, jobs: list[tuple[str, str]]) -> None:
        if self.thread:
            return
        self._save_settings()
        self.cancel.clear()
        self.dl_new = []
        self.thread = QThread()
        self.worker = DownloadWorker(jobs, self._download_dir(), self.ck_video.isChecked(), self.cancel)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_dl_progress)
        self.worker.log.connect(self.append_log)
        self.worker.downloaded.connect(self.on_downloaded)
        self.worker.failed.connect(self.on_dl_failed)
        self.worker.finished.connect(self.on_dl_finished)
        self._running(True)
        self.thread.start()

    @Slot(str, float)
    def on_dl_progress(self, msg: str, frac: float) -> None:
        self.status.setText(msg)
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))

    @Slot(list)
    def on_downloaded(self, paths: list) -> None:
        self.dl_new += paths
        self.add_files(paths)
        self.ed_url.clear()

    @Slot(str)
    def on_dl_failed(self, msg: str) -> None:
        self.append_log(f"Erro no download: {msg}")
        self.tabs.setCurrentIndex(1)
        QMessageBox.warning(self, "Transcritor", f"Não foi possível baixar:\n\n{msg}")

    @Slot()
    def on_dl_finished(self) -> None:
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self._running(False)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        if self.cancel.is_set():
            self.status.setText("Download cancelado.")
            return
        n = len(self.dl_new)
        self.status.setText(f"{n} arquivo(s) baixado(s) em {self._download_dir()}." if n else "Nada foi baixado.")
        if n and self.ck_auto.isChecked():
            self.start()

    def _running(self, on: bool) -> None:
        self.btn_start.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        for w in (self.btn_add, self.btn_remove, self.btn_clear, self.cb_lang, self.cb_model,
                  self.ck_diar, self.sp_speakers, self.sl_sens, self.ed_hot, self.ed_out,
                  self.ed_url, self.btn_dl, self.btn_login, self.ck_video, self.ck_auto,
                  *self.ck_fmt.values()):
            w.setEnabled(not on)
        if not on:
            self._sync_enabled()

    @Slot(int, str, float)
    def on_progress(self, row: int, msg: str, frac: float) -> None:
        f = self.files[row]
        if f["status"] != "running":
            f["status"] = "running"
            self._refresh_item(row)
        name = Path(f["path"]).name
        self.status.setText(f"{name}: {msg}")
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))

    @Slot(int, object, list)
    def on_done(self, row: int, tr: Transcript, outputs: list) -> None:
        f = self.files[row]
        f.update(status="done", transcript=tr, outputs=outputs)
        extra = fmt_time(tr.duration)
        if tr.diarized:
            extra += f" · {len(tr.speaker_names)} falante(s)"
        self._refresh_item(row, extra)
        if self.list.currentRow() in (row, -1):
            self.list.setCurrentRow(row)
            self.show_row(row)

    @Slot(int, str)
    def on_failed(self, row: int, msg: str) -> None:
        cancelled = msg == "Cancelado"
        self.files[row]["status"] = "cancelled" if cancelled else "error"
        self._refresh_item(row, msg if cancelled else "erro (veja o Registro)")
        if not cancelled:
            self.append_log(f"Erro em {Path(self.files[row]['path']).name}: {msg}")

    @Slot()
    def on_finished(self) -> None:
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self._running(False)
        self.bar.setRange(0, 1000)
        done = sum(f["status"] == "done" for f in self.files)
        errors = sum(f["status"] == "error" for f in self.files)
        self.bar.setValue(1000 if done and not errors else 0)
        msg = f"Concluído: {done} transcrito(s)."
        if errors:
            msg += f" {errors} com erro — veja a aba Registro."
            self.tabs.setCurrentIndex(1)
        if self.cancel.is_set():
            msg = "Cancelado."
        self.status.setText(msg)

    @Slot(str)
    def append_log(self, msg: str) -> None:
        self.logbox.appendPlainText(msg)

    # ------------------------------------------------------------- results
    def show_row(self, row: int) -> None:
        self.current_row = row
        tr = self.files[row]["transcript"] if 0 <= row < len(self.files) else None
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(0)
        if tr is None:
            self.lbl_title.setText("Selecione um arquivo transcrito para ver o resultado.")
            self.text.clear()
            self._set_result_enabled(False)
            self.tbl.blockSignals(False)
            return
        self._set_result_enabled(True)
        lang = export.LANG_NAMES.get(tr.language, tr.language)
        self.lbl_title.setText(f"<b>{html.escape(Path(tr.source).name)}</b> · {fmt_time(tr.duration)} · {lang}")
        for spk, name in sorted(tr.speaker_names.items()):
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            a = QTableWidgetItem(f"Falante {spk + 1}")
            a.setFlags(a.flags() & ~Qt.ItemFlag.ItemIsEditable)
            a.setForeground(Qt.GlobalColor.white)
            a.setBackground(self._color(spk))
            a.setData(Qt.ItemDataRole.UserRole, spk)
            self.tbl.setItem(r, 0, a)
            self.tbl.setItem(r, 1, QTableWidgetItem(name))
        self.tbl.setVisible(tr.diarized)
        self.tbl.blockSignals(False)
        self.render(tr)

    @staticmethod
    def _color(spk: int):
        from PySide6.QtGui import QColor
        return QColor(SPEAKER_COLORS[spk % len(SPEAKER_COLORS)])

    def render(self, tr: Transcript) -> None:
        parts = []
        for u in tr.utterances:
            ts = f'<span style="color:#888">[{fmt_time(u.start)}]</span> '
            who = ""
            if tr.diarized and u.speaker is not None:
                color = SPEAKER_COLORS[u.speaker % len(SPEAKER_COLORS)]
                who = f'<b style="color:{color}">{html.escape(tr.speaker_label(u.speaker))}:</b> '
            parts.append(f'<p style="margin:0 0 10px 0">{ts}{who}{html.escape(u.text)}</p>')
        self.text.setHtml("".join(parts))

    def _rename_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 1 or self.current_row < 0:
            return
        tr: Transcript = self.files[self.current_row]["transcript"]
        spk = self.tbl.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        tr.speaker_names[spk] = item.text().strip() or f"Falante {spk + 1}"
        self.render(tr)

    def _current(self):
        if 0 <= self.current_row < len(self.files):
            return self.files[self.current_row]
        return None

    def resave(self) -> None:
        f = self._current()
        if not f or not f["transcript"]:
            return
        formats = self._formats() or ["txt"]
        out_dir = self.ed_out.text().strip() or None
        try:
            f["outputs"] = [str(p) for p in save_outputs(f["transcript"], formats, out_dir)]
            self.status.setText("Arquivos salvos: " + ", ".join(Path(p).name for p in f["outputs"]))
        except Exception as e:
            QMessageBox.critical(self, "Transcritor", f"Erro ao salvar: {e}")

    def open_folder(self) -> None:
        f = self._current()
        if not f:
            return
        target = Path(f["outputs"][0]).parent if f["outputs"] else Path(f["path"]).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def copy_text(self) -> None:
        f = self._current()
        if f and f["transcript"]:
            QApplication.clipboard().setText(export.to_text(f["transcript"]))
            self.status.setText("Texto copiado para a área de transferência.")

    def closeEvent(self, e) -> None:
        if self.thread:
            if QMessageBox.question(self, "Transcritor",
                                    "Uma tarefa está em andamento. Deseja cancelar e sair?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.cancel.set()
            self.thread.quit()
            self.thread.wait(5000)
        self._save_settings()
        e.accept()


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Transcritor.App")
        except Exception:
            pass
    try:
        # O QtWebEngine (navegador de login) precisa ser carregado antes do QApplication.
        from PySide6 import QtWebEngineWidgets  # noqa: F401
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    except ImportError:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Transcritor")
    app.setApplicationVersion(__version__)
    icon = resource_path("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    for arg in sys.argv[1:]:
        if arg.lower().startswith(("http://", "https://")):
            # Link passado na abertura: baixa (ou abre o navegador de login, se for Teams/SharePoint).
            from PySide6.QtCore import QTimer
            win.ed_url.setText(arg)
            QTimer.singleShot(300, win.download_url)
            break
        if Path(arg).is_file():
            win.add_files([arg])
    return app.exec()
