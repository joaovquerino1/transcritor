"""Interface gráfica (PySide6)."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (
    QColor, QDesktopServices, QFontDatabase, QGuiApplication, QIcon, QKeySequence, QPainter, QPalette,
    QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QSystemTrayIcon, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import __version__, downloader, export, models, recorder, style, widgets
from .engine import LANGUAGES, Engine, Options, Transcript, fmt_time, output_stem
from .widgets import FRAC_ROLE, META_ROLE, STATUS_ROLE, FileDelegate, FlowBox, Responsive, SpeakerEditor, TranscriptView

MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".ts", ".mts", ".m2ts", ".vob", ".ogv", ".mxf", ".f4v", ".asf", ".rm", ".rmvb",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".amr",
}

STATUS_TEXT = {"pending": "Na fila", "running": "Preparando…", "done": "", "error": "Erro: veja o Registro",
               "cancelled": "Cancelado"}

FORMAT_SHORT = {"txt": "Texto", "docx": "Word", "srt": "SRT", "vtt": "VTT", "json": "JSON"}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets" / name


def _media_in(urls) -> list[str]:
    files: list[str] = []
    for p in (Path(u.toLocalFile()) for u in urls if u.isLocalFile()):
        if p.is_dir():
            files += [str(f) for f in sorted(p.rglob("*")) if f.suffix.lower() in MEDIA_EXTS]
        else:
            files.append(str(p))
    return files


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", "section")
    return lbl


def _divider() -> QFrame:
    f = QFrame()
    f.setProperty("role", "divider")
    return f


def _tool(icon_name: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setObjectName("flat")
    b.setToolTip(tip)
    b.setAccessibleName(tip)
    b.setIcon(style.icon(icon_name, "#000"))
    return b


def _flow(*items: QWidget, spacing: int = 14) -> QWidget:
    """Caixas de seleção lado a lado que descem de linha se a barra ficar estreita."""
    w = FlowBox(hspacing=spacing, vspacing=6)
    for it in items:
        w.flow.addWidget(it)
    return w


def _shrinkable(cb: QComboBox) -> None:
    # Sem isso a opção mais longa define a largura mínima da barra lateral inteira.
    cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(8)


class EmptyState(QWidget):
    """O que aparece antes de haver uma transcrição: diz como começar e aceita arquivos."""

    filesDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("emptyState")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        v.addStretch(2)
        self.title = QLabel()
        self.title.setObjectName("emptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.body = QLabel()
        self.body.setProperty("role", "secondary")
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setWordWrap(True)
        self.body.setMaximumWidth(440)
        v.addWidget(self.title)
        v.addSpacing(6)
        # Centralizado com espaçadores: com AlignHCenter o rótulo perde a quebra de linha.
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.body, 10)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(3)
        self.set_state(has_files=False)

    def set_state(self, has_files: bool) -> None:
        if has_files:
            self.title.setText("Nada transcrito ainda")
            self.body.setText("Clique em Transcrever. Ao terminar, o texto de cada arquivo aparece aqui, "
                              "separado por falante.")
        else:
            self.title.setText("Comece por um vídeo, um link ou uma gravação")
            self.body.setText("Arraste vídeos ou áudios para esta janela, cole um link na barra lateral "
                              "ou grave a sua próxima reunião.")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.filesDropped.emit(_media_in(e.mimeData().urls()))
        e.acceptProposedAction()


class FileList(QListWidget):
    filesDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("files")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setItemDelegate(FileDelegate(self))
        self.setUniformItemSizes(True)
        self.setMinimumHeight(150)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        self.filesDropped.emit(_media_in(e.mimeData().urls()))
        e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(style.tokens().text2))
            p.drawText(self.viewport().rect().adjusted(8, 8, -8, -8),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "Arraste vídeos ou áudios para cá.\nQualquer formato serve.")


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

    def __init__(self, jobs: list[downloader.Job], out_dir: Path, keep_video: bool,
                 cancel: threading.Event) -> None:
        super().__init__()
        self.jobs, self.out_dir, self.keep_video, self.cancel = jobs, out_dir, keep_video, cancel

    @Slot()
    def run(self) -> None:
        try:
            self._run()
        finally:
            # O arquivo de cookies tem a sessão do usuário: não deixa largado no disco.
            for job in self.jobs:
                if job.cookies:
                    Path(job.cookies).unlink(missing_ok=True)
            self.finished.emit()

    def _run(self) -> None:
        for job in self.jobs:
            if self.cancel.is_set():
                break
            self.log.emit(f"— Baixando {job.title or job.url}")
            try:
                paths = downloader.download(job, self.out_dir, self.keep_video,
                                            self.progress.emit, self.log.emit, self.cancel)
                for p in paths:
                    self.log.emit(f"Baixado: {p}")
                self.downloaded.emit(paths)
            except InterruptedError:
                break
            except Exception as e:
                self.log.emit(traceback.format_exc())
                self.failed.emit(str(e))


class RecordSignals(QObject):
    """Ponte entre as threads de gravação (início/fim levam alguns segundos) e a interface."""
    started = Signal()
    failed = Signal(str)
    stopped = Signal(str)
    log = Signal(str)


def _red_dot() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#e53935"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pm)


def save_outputs(tr: Transcript, formats: list[str], out_dir: Optional[str]) -> list[Path]:
    stem = output_stem(tr.source)
    if out_dir:
        stem = Path(out_dir) / stem.name
    return [export.export(tr, fmt, stem) for fmt in formats]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Transcritor")
        self.settings = QSettings("Transcritor", "Transcritor")
        self.engine = Engine()
        self.files: list[dict] = []       # {path, status, transcript, outputs}
        self.thread: Optional[QThread] = None
        self.worker: Optional[Worker] = None
        self.cancel = threading.Event()
        self.rec: Optional[recorder.Recorder] = None
        self.rec_busy = False             # iniciando ou finalizando
        self.rec_t0 = 0.0
        self.pending_start = False        # transcrever a gravação quando a tarefa atual acabar
        self.rec_sig = RecordSignals()
        self.rec_sig.started.connect(self.on_rec_started)
        self.rec_sig.failed.connect(self.on_rec_failed)
        self.rec_sig.stopped.connect(self.on_rec_stopped)
        self.rec_sig.log.connect(self.append_log)
        self.rec_timer = QTimer(self)
        self.rec_timer.timeout.connect(self._rec_tick)
        self.tray: Optional[QSystemTrayIcon] = None
        self.current_row = -1
        self.dl_new: list[str] = []

        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.setMinimumSize(660, 500)

        # Barra lateral: tudo o que entra (arquivos, links, gravação) e as opções.
        inner = QWidget()
        inner.setObjectName("sideInner")
        lv = QVBoxLayout(inner)
        lv.setContentsMargins(16, 14, 16, 16)
        lv.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(2)
        head.addWidget(_section("Arquivos"))
        head.addStretch(1)
        self.btn_add = _tool("plus", "Adicionar vídeos ou áudios (⌘O)")
        self.btn_add.clicked.connect(self.choose_files)
        self.btn_remove = _tool("minus", "Remover os selecionados (⌫)")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear = QToolButton()
        self.btn_clear.setObjectName("flat")
        self.btn_clear.setText("Limpar")
        self.btn_clear.setToolTip("Tirar todos os arquivos da lista")
        self.btn_clear.clicked.connect(self.clear_files)
        for b in (self.btn_add, self.btn_remove, self.btn_clear):
            head.addWidget(b)
        lv.addLayout(head)
        lv.addSpacing(6)
        self.list = FileList()
        self.list.filesDropped.connect(self.add_files)
        self.list.currentRowChanged.connect(self.show_row)
        lv.addWidget(self.list, 1)

        lv.addSpacing(18)
        lv.addWidget(_divider())
        lv.addSpacing(14)
        lv.addWidget(_section("Baixar da internet"))
        lv.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("Link do YouTube, Teams, Vimeo…")
        self.ed_url.setToolTip("YouTube, Teams, SharePoint, OneDrive, Vimeo, Instagram e muitos outros sites")
        self.ed_url.setClearButtonEnabled(True)
        self.ed_url.returnPressed.connect(self.download_url)
        self.btn_dl = QPushButton("Baixar")
        self.btn_dl.clicked.connect(self.download_url)
        row.addWidget(self.ed_url, 1)
        row.addWidget(self.btn_dl)
        lv.addLayout(row)
        lv.addSpacing(4)
        self.btn_login = QToolButton()
        self.btn_login.setObjectName("link")
        self.btn_login.setText("Entrar no Teams ou SharePoint…")
        self.btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login.setToolTip("Abre um navegador para entrar com sua conta e capturar gravações\n"
                                  "do Teams, SharePoint, OneDrive, Stream ou outros sites com login.")
        self.btn_login.clicked.connect(lambda: self.open_login())
        lv.addWidget(self.btn_login)
        lv.addSpacing(4)
        self.ck_video = QCheckBox("Manter o vídeo")
        self.ck_video.setToolTip("Desmarcado: baixa só o áudio, mais rápido e suficiente para transcrever.")
        self.ck_auto = QCheckBox("Transcrever ao terminar")
        lv.addWidget(_flow(self.ck_video, self.ck_auto))

        lv.addSpacing(18)
        lv.addWidget(_divider())
        lv.addSpacing(10)
        self.rec_panel = QWidget()
        self.rec_panel.setObjectName("recPanel")
        rp = QVBoxLayout(self.rec_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setSpacing(8)
        rp.addWidget(_section("Gravar reunião"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_rec = QPushButton("Gravar")
        self.btn_rec.setIcon(style.icon("record", style.tokens().rec, 14))
        self.btn_rec.setToolTip("Grava a chamada enquanto ela acontece. Ao parar, a transcrição\n"
                                "começa automaticamente com as opções abaixo.")
        self.btn_rec.clicked.connect(self.toggle_record)
        self.lbl_rec = QLabel("")
        self.lbl_rec.setObjectName("recTime")
        self.lbl_rec.setFont(style.tabular(self.lbl_rec.font()))
        row.addWidget(self.btn_rec)
        row.addWidget(self.lbl_rec)
        row.addStretch(1)
        rp.addLayout(row)
        self.lbl_rec_warn = QLabel("A gravação parou de receber dados. Pare e grave de novo.")
        self.lbl_rec_warn.setObjectName("recWarn")
        self.lbl_rec_warn.setWordWrap(True)
        self.lbl_rec_warn.hide()
        rp.addWidget(self.lbl_rec_warn)
        self.ck_rec_video = QCheckBox("Tela")
        self.ck_rec_video.setToolTip("Grava também o vídeo da tela. Desmarcado: só áudio (arquivo bem menor).")
        self.ck_rec_sys = QCheckBox("Som do computador")
        self.ck_rec_sys.setToolTip("O que sai no seu alto-falante ou fone: as vozes dos outros participantes.")
        self.ck_rec_mic = QCheckBox("Microfone")
        self.ck_rec_mic.setToolTip("A sua voz.")
        rp.addWidget(_flow(self.ck_rec_video, self.ck_rec_sys, self.ck_rec_mic))
        self.cb_display = QComboBox()
        self.cb_display.setToolTip("Tela a ser gravada")
        _shrinkable(self.cb_display)
        for name in recorder.displays():
            self.cb_display.addItem(name)
        self.cb_display.setVisible(self.cb_display.count() > 1)
        rp.addWidget(self.cb_display)
        lv.addWidget(self.rec_panel)

        lv.addSpacing(14)
        lv.addWidget(_divider())
        lv.addSpacing(14)
        lv.addWidget(_section("Transcrição"))
        lv.addSpacing(8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.cb_lang = QComboBox()
        _shrinkable(self.cb_lang)
        for k, v in LANGUAGES.items():
            self.cb_lang.addItem(v, k)
        form.addRow("Idioma:", self.cb_lang)
        self.cb_model = QComboBox()
        _shrinkable(self.cb_model)
        for m in models.WHISPER_MODELS:
            self.cb_model.addItem(f"{m.label} · {m.approx_mb / 1000:.1f} GB", m.key)
            self.cb_model.setItemData(self.cb_model.count() - 1, m.label, Qt.ItemDataRole.ToolTipRole)
        form.addRow("Modelo:", self.cb_model)
        lv.addLayout(form)
        lv.addSpacing(8)
        self.ck_diar = QCheckBox("Identificar quem está falando")
        self.ck_diar.toggled.connect(self._sync_enabled)
        lv.addWidget(self.ck_diar)

        lv.addSpacing(10)
        self.btn_more = QToolButton()
        self.btn_more.setObjectName("disclosure")
        self.btn_more.setText("Mais opções")
        self.btn_more.setCheckable(True)
        self.btn_more.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        lv.addWidget(self.btn_more)
        self.more = QWidget()
        mf = QFormLayout(self.more)
        mf.setContentsMargins(0, 8, 0, 0)
        mf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        mf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        mf.setHorizontalSpacing(10)
        mf.setVerticalSpacing(8)
        self.sp_speakers = QSpinBox()
        self.sp_speakers.setRange(0, 20)
        self.sp_speakers.setSpecialValueText("Automático")
        self.sp_speakers.valueChanged.connect(self._sync_enabled)
        mf.addRow("Falantes:", self.sp_speakers)
        self.sl_sens = QSlider(Qt.Orientation.Horizontal)
        self.sl_sens.setRange(0, 10)       # 0 → limiar 0,60 · 10 → limiar 0,30
        self.sl_sens.setToolTip("Para a direita se vozes diferentes estiverem sendo juntadas;\n"
                                "para a esquerda se a mesma pessoa aparecer como vários falantes.")
        self.lbl_sens = QLabel("Separação:")
        mf.addRow(self.lbl_sens, self.sl_sens)
        self.ed_hot = QLineEdit()
        self.ed_hot.setPlaceholderText("Nomes, siglas, termos técnicos")
        self.ed_hot.setToolTip("Opcional. Palavras que o modelo deve reconhecer, separadas por vírgula.")
        mf.addRow("Vocabulário:", self.ed_hot)
        out = QHBoxLayout()
        out.setSpacing(6)
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("Mesma pasta do vídeo")
        self.ed_out.setClearButtonEnabled(True)
        b = QPushButton("Escolher…")
        b.clicked.connect(self.choose_out_dir)
        out.addWidget(self.ed_out, 1)
        out.addWidget(b)
        mf.addRow("Salvar em:", out)
        self.ck_fmt: dict[str, QCheckBox] = {}
        for k, v in export.FORMATS.items():
            c = QCheckBox(FORMAT_SHORT.get(k, k))
            c.setToolTip(v)
            self.ck_fmt[k] = c
        mf.addRow("Formatos:", _flow(*self.ck_fmt.values(), spacing=10))
        lv.addWidget(self.more)
        self.btn_more.toggled.connect(self._toggle_more)

        scroll = QScrollArea()
        scroll.setObjectName("sideScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        scroll.viewport().setAutoFillBackground(False)

        # Rodapé fixo: a ação principal fica sempre à vista, mesmo com a barra rolada.
        footer = QWidget()
        footer.setObjectName("footer")
        fv = QVBoxLayout(footer)
        fv.setContentsMargins(16, 12, 16, 14)
        fv.setSpacing(8)
        self.status = QLabel("Pronto.")
        self.status.setProperty("role", "secondary")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setObjectName("bar")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_start = QPushButton("Transcrever")
        self.btn_start.setObjectName("primary")
        self.btn_start.setToolTip("Transcrever os arquivos da lista (⌘↩)")
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.stop)
        row.addWidget(self.btn_start, 1)
        row.addWidget(self.btn_cancel)
        fv.addWidget(self.status)
        fv.addWidget(self.bar)
        fv.addLayout(row)

        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(300)
        self.sidebar.setMaximumWidth(440)
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)
        sv.addWidget(scroll, 1)
        sv.addWidget(footer)

        # Conteúdo: a transcrição selecionada.
        content = QWidget()
        content.setObjectName("content")
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        self.tabs = QTabWidget()
        self.btn_side = _tool("sidebar", "Mostrar ou esconder a barra lateral (⌘⌥S)")
        self.btn_side.clicked.connect(self.toggle_sidebar)
        self.tabs.setCornerWidget(self.btn_side, Qt.Corner.TopLeftCorner)

        self.result = QStackedWidget()
        self.empty = EmptyState()
        self.empty.filesDropped.connect(self.add_files)
        self.result.addWidget(self.empty)

        view = QWidget()
        vv = QVBoxLayout(view)
        vv.setContentsMargins(24, 18, 24, 0)
        vv.setSpacing(4)
        header = Responsive(breakpoint=620)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("title")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_meta = QLabel()
        self.lbl_meta.setProperty("role", "secondary")
        titles.addWidget(self.lbl_title)
        titles.addWidget(self.lbl_meta)
        header.box.addLayout(titles, 1)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.btn_copy = QPushButton("Copiar texto")
        self.btn_copy.clicked.connect(self.copy_text)
        self.btn_open = QPushButton("Abrir pasta")
        self.btn_open.clicked.connect(self.open_folder)
        self.btn_resave = QPushButton("Salvar de novo")
        self.btn_resave.setToolTip("Grava os arquivos outra vez, com os nomes dos falantes atualizados")
        self.btn_resave.clicked.connect(self.resave)
        for w in (self.btn_copy, self.btn_open, self.btn_resave):
            actions.addWidget(w)
        actions.addStretch(1)
        header.box.addLayout(actions)
        vv.addWidget(header)

        self.speakers_box = QWidget()
        sb = QVBoxLayout(self.speakers_box)
        sb.setContentsMargins(0, 14, 0, 4)
        sb.setSpacing(6)
        hint = QLabel("Nomes dos falantes")
        hint.setProperty("role", "secondary")
        sb.addWidget(hint)
        self.speakers = SpeakerEditor()
        self.speakers.renamed.connect(self._rename_changed)
        sb.addWidget(self.speakers)
        vv.addWidget(self.speakers_box)
        vv.addSpacing(10)
        vv.addWidget(_divider())

        self.text = TranscriptView()
        vv.addWidget(self.text, 1)
        vv.setStretch(vv.indexOf(self.text), 1)
        self.result.addWidget(view)
        self.tabs.addTab(self.result, "Transcrição")

        self.logbox = QPlainTextEdit()
        self.logbox.setObjectName("log")
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(5000)
        self.logbox.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.logbox, "Registro")
        cv.addWidget(self.tabs)

        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        self.split.addWidget(self.sidebar)
        self.split.addWidget(content)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([340, 900])
        self.setCentralWidget(self.split)
        self.user_hid_sidebar = False
        self._rerender = widgets.debounce(self, 200, lambda: self.text.show_transcript(self._shown))
        self._shown: Optional[Transcript] = None

        QShortcut(QKeySequence.StandardKey.Open, self, self.choose_files)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.start)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, self.toggle_sidebar)
        for key in (QKeySequence.StandardKey.Delete, QKeySequence(Qt.Key.Key_Backspace)):
            QShortcut(key, self.list, self.remove_selected, context=Qt.ShortcutContext.WidgetShortcut)

        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _: self._apply_theme())
        self._apply_theme()
        self.show_row(-1)

    def _apply_theme(self) -> None:
        t = style.tokens()
        self.setStyleSheet(style.stylesheet(t))
        ink = self.palette().color(QPalette.ColorRole.WindowText)
        self.btn_add.setIcon(style.icon("plus", ink))
        self.btn_remove.setIcon(style.icon("minus", ink))
        self.btn_side.setIcon(style.icon("sidebar", ink, 18))
        self.btn_copy.setIcon(style.icon("copy", ink))
        self.btn_open.setIcon(style.icon("folder", ink))
        self.btn_resave.setIcon(style.icon("save", ink))
        self._set_rec_icon()
        self._toggle_more(self.btn_more.isChecked())
        self.list.viewport().update()
        if self._shown is not None:
            self.speakers.set_speakers(self._shown.speaker_names)
            self.text.show_transcript(self._shown)

    def _set_rec_icon(self) -> None:
        live = self.rec is not None
        self.btn_rec.setIcon(style.icon("stop" if live else "record", style.tokens().rec, 14))

    def _toggle_more(self, on: bool) -> None:
        ink = self.palette().color(QPalette.ColorRole.WindowText)
        self.btn_more.setIcon(style.icon("chev-down" if on else "chev-right", ink, 14))
        self.more.setVisible(on)

    # Abaixo desta largura a barra lateral sai da frente e volta pelo botão.
    COMPACT = 900

    def toggle_sidebar(self) -> None:
        show = not self.sidebar.isVisible()
        self.sidebar.setVisible(show)
        if self.width() >= self.COMPACT:
            self.user_hid_sidebar = not show

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        compact = e.size().width() < self.COMPACT
        was = e.oldSize().width() < self.COMPACT if e.oldSize().width() > 0 else None
        if compact != was:
            self.sidebar.setVisible(not compact and not self.user_hid_sidebar)

    def _sync_enabled(self) -> None:
        on = self.ck_diar.isChecked()
        self.sp_speakers.setEnabled(on)
        auto = on and self.sp_speakers.value() == 0
        self.sl_sens.setEnabled(auto)
        self.lbl_sens.setEnabled(auto)

    def _set_result_enabled(self, on: bool) -> None:
        for w in (self.btn_resave, self.btn_open, self.btn_copy):
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
        self.ck_rec_video.setChecked(s.value("rec_video", True, type=bool))
        self.ck_rec_sys.setChecked(s.value("rec_system", True, type=bool))
        self.ck_rec_mic.setChecked(s.value("rec_mic", True, type=bool))
        more = s.value("more_options", False, type=bool)
        self.btn_more.setChecked(more)
        self._toggle_more(more)
        if not self.restoreGeometry(s.value("geometry", b"")):
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1240, screen.width() - 80), min(820, screen.height() - 60))
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
        s.setValue("rec_video", self.ck_rec_video.isChecked())
        s.setValue("rec_system", self.ck_rec_sys.isChecked())
        s.setValue("rec_mic", self.ck_rec_mic.isChecked())
        s.setValue("more_options", self.btn_more.isChecked())
        s.setValue("geometry", self.saveGeometry())

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
        self.empty.set_state(has_files=bool(self.files))

    def _refresh_item(self, row: int, extra: str = "", frac: float = 0.0) -> None:
        f = self.files[row]
        item = self.list.item(row)
        item.setText(Path(f["path"]).name)
        item.setData(STATUS_ROLE, f["status"])
        item.setData(META_ROLE, extra or STATUS_TEXT[f["status"]])
        item.setData(FRAC_ROLE, frac)

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
        if self.thread:
            return
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
            self.start_download([downloader.Job(url)])

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

    def start_download(self, jobs: list[downloader.Job]) -> None:
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
            self._start_pending()
            return
        n = len(self.dl_new)
        self.status.setText(f"{n} arquivo(s) baixado(s) em {self._download_dir()}." if n else "Nada foi baixado.")
        if n and self.ck_auto.isChecked():
            self.start()
        self._start_pending()

    # ------------------------------------------------------------ gravação
    def toggle_record(self) -> None:
        if self.rec_busy:
            return
        if self.rec is None:
            self.start_record()
        else:
            self.stop_record()

    def start_record(self) -> None:
        reason = recorder.unsupported()
        if reason:
            QMessageBox.warning(self, "Transcritor", reason)
            return
        video, system, mic = (self.ck_rec_video.isChecked(), self.ck_rec_sys.isChecked(),
                              self.ck_rec_mic.isChecked())
        if not (system or mic):
            QMessageBox.information(self, "Transcritor",
                                    "Marque \"Som do computador\" e/ou \"Microfone\" para gravar.")
            return
        self._save_settings()
        dest = recorder.default_name(self._download_dir(), video)
        rec = recorder.create()
        display = max(self.cb_display.currentIndex(), 0)
        self._set_rec_busy("Iniciando…")

        def run() -> None:
            try:
                rec.start(dest, video, system, mic, display, self.rec_sig.log.emit)
                self.rec = rec
                self.rec_sig.started.emit()
            except Exception as e:  # noqa: BLE001
                self.rec_sig.log.emit(traceback.format_exc())
                self.rec_sig.failed.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    def stop_record(self) -> None:
        rec, self.rec = self.rec, None
        self.rec_timer.stop()
        self._set_rec_busy("Finalizando…")

        def run() -> None:
            try:
                self.rec_sig.stopped.emit(str(rec.stop(self.rec_sig.log.emit)))
            except Exception as e:  # noqa: BLE001
                self.rec_sig.log.emit(traceback.format_exc())
                self.rec_sig.failed.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    def _set_rec_live(self, live: bool) -> None:
        self.rec_panel.setProperty("live", live)
        self.rec_panel.layout().setContentsMargins(*((10, 8, 10, 10) if live else (0, 0, 0, 0)))
        self.rec_panel.style().unpolish(self.rec_panel)
        self.rec_panel.style().polish(self.rec_panel)
        self._set_rec_icon()

    def _set_rec_busy(self, text: str) -> None:
        self.rec_busy = True
        self.btn_rec.setEnabled(False)
        self.btn_rec.setText(text)
        for w in (self.ck_rec_video, self.ck_rec_sys, self.ck_rec_mic, self.cb_display):
            w.setEnabled(False)

    def _rec_idle(self) -> None:
        self.rec_busy = False
        self.btn_rec.setEnabled(True)
        self.btn_rec.setText("Gravar")
        self.lbl_rec.setText("")
        self.lbl_rec_warn.hide()
        self._set_rec_live(False)
        self.setWindowTitle("Transcritor")
        for w in (self.ck_rec_video, self.ck_rec_sys, self.ck_rec_mic, self.cb_display):
            w.setEnabled(True)
        if self.tray:
            self.tray.hide()

    @Slot()
    def on_rec_started(self) -> None:
        import time
        self.rec_busy = False
        self.rec_t0 = time.monotonic()
        self.rec_warned = False
        self.btn_rec.setEnabled(True)
        self.btn_rec.setText("Parar e transcrever")
        self._set_rec_live(True)
        self.rec_timer.start(500)
        self._rec_tick()
        self.append_log("— Gravação iniciada.")
        if QSystemTrayIcon.isSystemTrayAvailable():
            if self.tray is None:
                self.tray = QSystemTrayIcon(_red_dot(), self)
                menu = QMenu(self)
                menu.addAction("Parar e transcrever", self.toggle_record)
                menu.addAction("Mostrar o Transcritor", self._show_window)
                self.tray.setContextMenu(menu)
                self.tray.activated.connect(
                    lambda r: self._show_window() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
            self.tray.show()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _rec_tick(self) -> None:
        import time
        if self.rec is None:
            return
        err = self.rec.error()
        if err:
            self.append_log(f"A gravação foi interrompida: {err}")
            self.stop_record()
            return
        t = int(time.monotonic() - self.rec_t0)
        txt = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
        stalled = self.rec.stalled()
        self.lbl_rec_warn.setVisible(stalled)
        if stalled and not self.rec_warned:
            self.rec_warned = True
            self.append_log("Aviso: a gravação parou de receber áudio/vídeo do sistema.")
        self.lbl_rec.setText(txt)
        self.setWindowTitle(f"Gravando {txt} · Transcritor")
        if self.tray:
            self.tray.setToolTip(f"Transcritor: gravando ({txt})")

    @Slot(str)
    def on_rec_failed(self, msg: str) -> None:
        self.rec = None
        self.rec_timer.stop()
        self._rec_idle()
        self.append_log(f"Erro na gravação: {msg}")
        QMessageBox.warning(self, "Transcritor", f"Problema na gravação:\n\n{msg}")

    @Slot(str)
    def on_rec_stopped(self, path: str) -> None:
        self._rec_idle()
        self.append_log(f"Gravação salva: {path}")
        self.add_files([path])
        self.list.setCurrentRow(len(self.files) - 1)
        self._show_window()
        if self.thread:
            self.pending_start = True
            self.status.setText("Gravação salva. Será transcrita quando a tarefa atual terminar.")
        else:
            self.start()

    def _running(self, on: bool) -> None:
        self.btn_start.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        self.bar.setVisible(on)
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
        f["status"] = "running"
        self._refresh_item(row, msg, frac)
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
        self._refresh_item(row, self._summary(tr))
        if self.list.currentRow() in (row, -1):
            self.list.setCurrentRow(row)
            self.show_row(row)

    @Slot(int, str)
    def on_failed(self, row: int, msg: str) -> None:
        cancelled = msg == "Cancelado"
        self.files[row]["status"] = "cancelled" if cancelled else "error"
        self._refresh_item(row)
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
            msg += f" {errors} com erro: veja a aba Registro."
            self.tabs.setCurrentIndex(1)
        if self.cancel.is_set():
            msg = "Cancelado."
        self.status.setText(msg)
        self._start_pending()

    def _start_pending(self) -> None:
        if self.pending_start and not self.thread:
            self.pending_start = False
            self.start()

    @Slot(str)
    def append_log(self, msg: str) -> None:
        self.logbox.appendPlainText(msg)

    # ------------------------------------------------------------- results
    def show_row(self, row: int) -> None:
        self.current_row = row
        tr = self.files[row]["transcript"] if 0 <= row < len(self.files) else None
        self._shown = tr
        self.empty.set_state(has_files=bool(self.files))
        self._set_result_enabled(tr is not None)
        if tr is None:
            self.result.setCurrentIndex(0)
            self.text.show_transcript(None)
            return
        self.result.setCurrentIndex(1)
        self.lbl_title.setText(Path(tr.source).stem)
        self.lbl_meta.setText(self._summary(tr, with_lang=True))
        self.speakers_box.setVisible(tr.diarized)
        self.speakers.set_speakers(tr.speaker_names)
        self.text.verticalScrollBar().setValue(0)
        self.text.show_transcript(tr)

    @staticmethod
    def _summary(tr: Transcript, with_lang: bool = False) -> str:
        parts = [fmt_time(tr.duration)]
        if with_lang:
            parts.append(export.LANG_NAMES.get(tr.language, tr.language))
        if tr.diarized:
            n = len(tr.speaker_names)
            parts.append(f"{n} falante" + ("s" if n != 1 else ""))
        return ", ".join(parts)

    def _rename_changed(self, spk: int, name: str) -> None:
        tr = self._shown
        if tr is None:
            return
        tr.speaker_names[spk] = name.strip() or f"Falante {spk + 1}"
        self._rerender.start()

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
        if self.rec is not None or self.rec_busy:
            if QMessageBox.question(self, "Transcritor",
                                    "Uma gravação está em andamento. Parar, salvar o arquivo e sair?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            if self.rec is not None:
                try:
                    self.rec.stop(self.append_log)
                except Exception as ex:  # noqa: BLE001
                    self.append_log(f"Erro ao finalizar a gravação: {ex}")
                self.rec = None
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
