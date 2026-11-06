"""Cortar um trecho: escolher início e fim vendo o vídeo, para transcrever só essa parte
(ou salvá-la como um vídeo novo)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess, QRectF, QSettings, Qt, QTime, QUrl, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTimeEdit,
                               QVBoxLayout, QWidget)

from . import style
from .downloader import ffmpeg_exe, unique_path
from .engine import clip_label, fmt_time
from .player import VideoPlayer

MIN_CLIP = 1.0      # segundos


class RangeBar(QWidget):
    """Linha do tempo do arquivo: o trecho escolhido em destaque, com uma alça em cada ponta.

    Arrastar uma alça muda o início ou o fim; clicar em outro ponto leva o vídeo até ali.
    """

    rangeChanged = Signal(float, float)
    scrubbed = Signal(float)

    HANDLE = 7

    def __init__(self) -> None:
        super().__init__()
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead = 0.0
        self._drag: Optional[str] = None
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName("Trecho escolhido")

    def set_duration(self, d: float) -> None:
        self.duration = max(d, 0.0)
        if self.end <= 0 or self.end > self.duration:
            self.end = self.duration
        self.start = min(self.start, max(self.end - MIN_CLIP, 0.0))
        self.update()

    def set_range(self, a: float, b: float) -> None:
        self.start, self.end = a, b
        self.update()

    def set_playhead(self, t: float) -> None:
        self.playhead = t
        self.update()

    def _track(self) -> QRectF:
        m = self.HANDLE + 2
        return QRectF(m, self.height() / 2 - 5, max(self.width() - 2 * m, 1), 10)

    def _x(self, t: float) -> float:
        tr = self._track()
        return tr.left() + (t / self.duration * tr.width() if self.duration else 0)

    def _t(self, x: float) -> float:
        tr = self._track()
        return min(max((x - tr.left()) / tr.width(), 0.0), 1.0) * self.duration

    def paintEvent(self, _e) -> None:
        t = style.tokens()
        acc = style.accent()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tr = self._track()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.line))
        p.drawRoundedRect(tr, 4, 4)
        if self.duration > 0:
            x0, x1 = self._x(self.start), self._x(self.end)
            sel = QRectF(x0, tr.top(), max(x1 - x0, 1), tr.height())
            fill = QColor(acc)
            fill.setAlpha(110)
            p.setBrush(fill)
            p.drawRect(sel)
            p.setBrush(acc)
            for x in (x0, x1):
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, 3, self.HANDLE, self.height() - 6), 3, 3)
            p.setPen(QPen(QColor(t.text), 2))
            xp = self._x(self.playhead)
            p.drawLine(int(xp), int(tr.top() - 6), int(xp), int(tr.bottom() + 6))
        p.end()

    def _handle_at(self, x: float) -> Optional[str]:
        if self.duration <= 0:
            return None
        d0, d1 = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        if min(d0, d1) > self.HANDLE + 3:
            return None
        return "start" if d0 < d1 or (d0 == d1 and x < self._x(self.start)) else "end"

    def mousePressEvent(self, e) -> None:
        x = e.position().x()
        self._drag = self._handle_at(x)
        if self._drag is None:
            self.scrubbed.emit(self._t(x))
            self._drag = "playhead"
        else:
            self._move(x)

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._drag:
            self._move(x)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._handle_at(x) else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def _move(self, x: float) -> None:
        t = self._t(x)
        if self._drag == "start":
            self.start = min(t, self.end - MIN_CLIP)
            self.start = max(self.start, 0.0)
            self.scrubbed.emit(self.start)
        elif self._drag == "end":
            self.end = max(t, self.start + MIN_CLIP)
            self.end = min(self.end, self.duration)
            self.scrubbed.emit(self.end)
        else:
            self.scrubbed.emit(t)
            return
        self.update()
        self.rangeChanged.emit(self.start, self.end)


def _qtime(t: float) -> QTime:
    t = max(t, 0.0)
    h, rem = divmod(int(round(t)), 3600)
    m, s = divmod(rem, 60)
    return QTime(min(h, 23), m, s)


def _secs(q: QTime) -> float:
    return q.hour() * 3600 + q.minute() * 60 + q.second()


class TrimDialog(QDialog):
    """Resultado em `action`: "use" (transcrever só o trecho) ou "save" (vídeo novo já salvo em `saved`)."""

    def __init__(self, path: str, duration: float, settings: QSettings, parent: QWidget,
                 clip: Optional[tuple[float, float]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Cortar trecho: {Path(path).name}")
        self.path = path
        self.action = ""
        self.saved = ""
        self._proc: Optional[QProcess] = None
        self._stopped = False
        self._out = ""
        self.resize(820, 640)

        self.video = VideoPlayer(settings)
        self.video.sub.show = False           # ainda não há transcrição para mostrar
        self.video.allow_full = False
        for w in (self.video.btn_cc, self.video.btn_ccset, self.video.btn_full, self.video.slider):
            w.hide()
        self.video.bar.layout().insertStretch(4, 1)
        self.video.set_media(path, None)
        self.video.positionChanged.connect(self._on_position)
        self.video.player.durationChanged.connect(lambda ms: ms > 0 and self._set_duration(ms / 1000))
        self.video.hasVideoChanged.connect(lambda has: self.video.setMaximumHeight(
            16777215 if has else self.video.bar.sizeHint().height()))

        self.bar = RangeBar()
        self.bar.rangeChanged.connect(self._bar_changed)
        self.bar.scrubbed.connect(lambda t: self.video.seek(t, play=False))

        self.ed_start = QTimeEdit()
        self.ed_end = QTimeEdit()
        for ed in (self.ed_start, self.ed_end):
            ed.setDisplayFormat("HH:mm:ss")
            ed.setFont(style.tabular(ed.font()))
            ed.timeChanged.connect(self._edits_changed)
        self.btn_mark_start = QPushButton("Marcar aqui")
        self.btn_mark_start.setToolTip("Começar o trecho no ponto do vídeo (I)")
        self.btn_mark_start.clicked.connect(lambda: self._mark("start"))
        self.btn_mark_end = QPushButton("Marcar aqui")
        self.btn_mark_end.setToolTip("Terminar o trecho no ponto do vídeo (O)")
        self.btn_mark_end.clicked.connect(lambda: self._mark("end"))
        self.lbl_len = QLabel()
        self.lbl_len.setProperty("role", "secondary")
        self.lbl_len.setFont(style.tabular(self.lbl_len.font()))

        marks = QHBoxLayout()
        marks.setSpacing(8)
        for w in (QLabel("Início"), self.ed_start, self.btn_mark_start):
            marks.addWidget(w)
        marks.addSpacing(18)
        for w in (QLabel("Fim"), self.ed_end, self.btn_mark_end):
            marks.addWidget(w)
        marks.addStretch(1)
        marks.addWidget(self.lbl_len)

        hint = QLabel("Arraste as pontas da barra ou use “Marcar aqui” enquanto assiste. "
                      "Só o trecho é transcrito, e os horários continuam os do vídeo original.")
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)

        self.btn_preview = QPushButton("Ver o trecho")
        self.btn_preview.setToolTip("Toca a partir do início do trecho")
        self.btn_preview.clicked.connect(lambda: self.video.seek(self.bar.start))
        self.btn_save = QPushButton("Salvar como vídeo…")
        self.btn_save.setToolTip("Cria um arquivo novo só com o trecho, na mesma pasta do original")
        self.btn_save.clicked.connect(self._save)
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_use = QPushButton("Transcrever só este trecho")
        self.btn_use.setObjectName("primary")
        self.btn_use.setDefault(True)
        self.btn_use.clicked.connect(self._use)
        for b in (self.btn_preview, self.btn_save, self.btn_cancel):
            b.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_preview)
        buttons.addStretch(1)
        for b in (self.btn_save, self.btn_cancel, self.btn_use):
            buttons.addWidget(b)
        self.lbl_status = QLabel()
        self.lbl_status.setProperty("role", "secondary")
        self.lbl_status.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 16)
        lay.setSpacing(10)
        lay.addWidget(self.video, 1)
        lay.addWidget(self.bar)
        lay.addLayout(marks)
        lay.addWidget(hint)
        lay.addWidget(self.lbl_status)
        lay.addLayout(buttons)

        QShortcut(QKeySequence("I"), self, lambda: self._mark("start"))
        QShortcut(QKeySequence("O"), self, lambda: self._mark("end"))
        self.video.setFocus()       # espaço toca e pausa, ← → andam 5 s

        self._set_duration(duration)
        if clip:
            self.bar.set_range(clip[0], min(clip[1], self.bar.duration) if self.bar.duration else clip[1])
        self._sync_edits()

    # ---------------------------------------------------------------- estado
    @property
    def clip(self) -> tuple[float, float]:
        return self.bar.start, self.bar.end

    def _set_duration(self, d: float) -> None:
        if d <= 0 or abs(d - self.bar.duration) < 0.5:
            return
        whole = self.bar.end in (0.0, self.bar.duration)
        self.bar.set_duration(d)
        if whole:
            self.bar.end = d
        top = _qtime(d)
        for ed in (self.ed_start, self.ed_end):
            ed.setMaximumTime(top)
        self._sync_edits()

    def _sync_edits(self) -> None:
        for ed, t in ((self.ed_start, self.bar.start), (self.ed_end, self.bar.end)):
            ed.blockSignals(True)
            ed.setTime(_qtime(t))
            ed.blockSignals(False)
        n = self.bar.end - self.bar.start
        self.lbl_len.setText(f"Trecho: {fmt_time(n)}")
        whole = self.bar.start <= 0 and self.bar.end >= self.bar.duration - 0.5
        self.btn_use.setEnabled(n >= MIN_CLIP and not whole)
        self.btn_save.setEnabled(n >= MIN_CLIP and not whole)
        self.bar.update()

    def _bar_changed(self, _a: float, _b: float) -> None:
        self._sync_edits()

    def _edits_changed(self) -> None:
        # só o campo editado muda; o outro mantém o valor exato (com fração de segundo)
        if self.sender() is self.ed_start:
            a, b = _secs(self.ed_start.time()), self.bar.end
        else:
            a, b = self.bar.start, _secs(self.ed_end.time())
        if b - a < MIN_CLIP:
            if self.sender() is self.ed_start:
                a = max(b - MIN_CLIP, 0.0)
            else:
                b = min(a + MIN_CLIP, self.bar.duration or a + MIN_CLIP)
        self.bar.set_range(a, b)
        self._sync_edits()
        self.video.seek(a if self.sender() is self.ed_start else b, play=False)

    def _mark(self, which: str) -> None:
        t = self.video.current_time()
        a, b = self.clip
        if which == "start":
            a = min(t, b - MIN_CLIP) if b - t >= MIN_CLIP else t
            if b - a < MIN_CLIP:
                b = min(a + 60, self.bar.duration)
        else:
            b = t
            if b - a < MIN_CLIP:
                a = max(b - 60, 0.0)
        self.bar.set_range(max(a, 0.0), min(b, self.bar.duration or b))
        self._sync_edits()

    def _on_position(self, t: float) -> None:
        self.bar.set_playhead(t)

    # ---------------------------------------------------------------- ações
    def _use(self) -> None:
        self.action = "use"
        self.video.stop()
        self.accept()

    def _save(self) -> None:
        a, b = self.clip
        src = Path(self.path)
        suffix = ".m4a" if not self.video.has_video() else ".mp4"
        out = unique_path(src.with_name(f"{src.stem} (trecho {clip_label(a)} a {clip_label(b)}){suffix}"))
        self._out = str(out)
        self._proc = QProcess(self)
        self._proc.setProgram(ffmpeg_exe())
        self._proc.setArguments(cut_args(self.path, a, b, self._out, self.video.has_video()))
        self._proc.readyReadStandardOutput.connect(self._save_progress)
        self._proc.finished.connect(self._save_done)
        self._saving(True)
        self.lbl_status.setText("Salvando o trecho… 0%")
        self._proc.start()

    def _saving(self, on: bool) -> None:
        for w in (self.btn_preview, self.btn_save, self.btn_use, self.bar, self.ed_start, self.ed_end,
                  self.btn_mark_start, self.btn_mark_end):
            w.setEnabled(not on)
        self.btn_cancel.setText("Parar" if on else "Cancelar")
        if on:
            self.video.stop()
        else:
            self._sync_edits()

    def _save_progress(self) -> None:
        a, b = self.clip
        for line in bytes(self._proc.readAllStandardOutput()).decode(errors="ignore").splitlines():
            if line.startswith("out_time_us=") and line[12:].strip().isdigit():
                frac = min(int(line[12:]) / 1e6 / max(b - a, 0.1), 1.0)
                self.lbl_status.setText(f"Salvando o trecho… {frac:.0%}")

    def _save_done(self, code: int, _status) -> None:
        proc, self._proc = self._proc, None
        self._saving(False)
        if code == 0 and Path(self._out).exists() and not self._stopped:
            self.saved = self._out
            self.action = "save"
            self.accept()
            return
        Path(self._out).unlink(missing_ok=True)
        if self._stopped:
            self.lbl_status.setText("Salvamento interrompido.")
        else:
            err = bytes(proc.readAllStandardError()).decode(errors="ignore").strip().splitlines()
            self.lbl_status.setText("Não foi possível salvar o trecho: " + (err[-1] if err else f"código {code}"))
        self._stopped = False

    def reject(self) -> None:
        if self._proc is not None:      # salvando: o botão vira "Parar"
            self._stopped = True
            self._proc.kill()
            return
        super().reject()

    def done(self, r: int) -> None:
        self.video.player.stop()
        self.video.player.setSource(QUrl())      # solta o arquivo
        super().done(r)


def cut_args(src: str, start: float, end: float, out: str, video: bool) -> list[str]:
    """Corte exato: recodifica o trecho (copiar sem recodificar só começaria num quadro-chave,
    às vezes vários segundos antes do ponto marcado)."""
    args = ["-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}", "-map", "0:a?"]
    if video:
        args += ["-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
    return args


def cut_file(src: str, start: float, end: float, out: str, video: bool = True) -> None:
    """O mesmo corte, sem interface (testes e linha de comando)."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run([ffmpeg_exe(), *cut_args(src, start, end, out, video)],
                       capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0 or not Path(out).exists():
        Path(out).unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "ffmpeg falhou").strip().splitlines()[-1])
