"""Player interno: vídeo (ou só áudio) com a legenda da transcrição por cima.

A legenda é desenhada junto com cada quadro, usando o horário do próprio quadro, então
ela troca exatamente no quadro em que a fala começa, mesmo em velocidade alterada.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QIcon, QKeySequence, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSlider, QSplitter, QStyle, QStyleOptionSlider, QToolButton, QVBoxLayout, QWidget)

from . import export, style
from .engine import Transcript, fmt_time

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)


# ----------------------------------------------------------------- legendas
@dataclass
class SubStyle:
    show: bool = True
    family: str = ""            # vazio: fonte do sistema
    size: int = 100             # % do padrão (5% da altura do vídeo)
    bold: bool = True
    color: str = "#ffffff"
    outline: bool = False
    bg: str = "#000000"
    bg_opacity: int = 65        # %
    position: str = "bottom"    # "bottom" ou "top"
    speaker: bool = True        # nome do falante na primeira linha
    speaker_color: bool = True  # nome na cor do falante

    KEY = "legenda/"

    @classmethod
    def load(cls, s: QSettings) -> "SubStyle":
        st = cls()
        for f in fields(cls):
            v = s.value(cls.KEY + f.name)
            if v is None:
                continue
            try:
                if f.type in ("bool", bool):
                    v = v if isinstance(v, bool) else str(v).lower() == "true"
                elif f.type in ("int", int):
                    v = int(v)
                else:
                    v = str(v)
                setattr(st, f.name, v)
            except (TypeError, ValueError):
                pass
        return st

    def save(self, s: QSettings) -> None:
        for k, v in asdict(self).items():
            s.setValue(self.KEY + k, v)


@dataclass
class Cue:
    start: float
    end: float
    speaker: Optional[int]
    text: str


def build_cues(tr: Transcript) -> list[Cue]:
    """Legendas curtas (as mesmas do .srt), com um pequeno tempo extra na tela para dar
    tempo de ler, sem nunca encostar na legenda seguinte."""
    cues = [Cue(s, e, spk, txt) for s, e, spk, txt in export._cues(tr)]
    cues.sort(key=lambda c: c.start)
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else c.end + 10
        # 0,4 s a mais (e no mínimo 1 s) na tela, cortando onde a próxima começa
        c.end = max(min(max(c.end + 0.4, c.start + 1.0), nxt), c.start)
    return cues


def cue_lines(text: str, fm: QFontMetricsF, max_w: float) -> list[str]:
    """Duas linhas equilibradas quando passa de 42 caracteres; quebra mais se não couber."""
    out: list[str] = []
    for line in export._wrap(text).split("\n"):
        words, cur = line.split(), ""
        for w in words:
            test = f"{cur} {w}".strip()
            if cur and fm.horizontalAdvance(test) > max_w:
                out.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            out.append(cur)
    return out


def paint_subtitle(p: QPainter, area: QRectF, st: SubStyle, text: str,
                   label: str = "", label_color: str = "") -> None:
    """Desenha a legenda dentro de `area` (o retângulo do vídeo na tela)."""
    if not text:
        return
    px = max(11.0, area.height() * 0.05 * st.size / 100)
    font = QFont(st.family) if st.family else QFont()
    font.setPixelSize(round(px))
    font.setWeight(QFont.Weight.DemiBold if st.bold else QFont.Weight.Normal)
    fm = QFontMetricsF(font)
    pad_x, pad_y = px * 0.4, px * 0.12
    max_w = area.width() * 0.86 - 2 * pad_x
    prefix = f"{label}: " if label else ""
    lines = cue_lines(prefix + text, fm, max_w)
    line_h = fm.height() + 2 * pad_y
    total = line_h * len(lines)
    margin = area.height() * 0.06
    top = area.top() + margin if st.position == "top" else area.bottom() - margin - total

    bg = QColor(st.bg)
    bg.setAlphaF(st.bg_opacity / 100)
    fg = QColor(st.color)
    name_fg = QColor(label_color) if (label and st.speaker_color and label_color) else fg
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setFont(font)
    for i, line in enumerate(lines):
        w = fm.horizontalAdvance(line)
        x = area.center().x() - w / 2
        y = top + i * line_h
        if bg.alpha():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(x - pad_x, y, w + 2 * pad_x, line_h), px * 0.14, px * 0.14)
        base = QPointF(x, y + pad_y + fm.ascent())
        # o nome do falante vai na cor dele, só no começo da primeira linha
        runs = [(line, fg)]
        if i == 0 and prefix and line.startswith(prefix.rstrip()):
            head = prefix if line.startswith(prefix) else prefix.rstrip()
            runs = [(head, name_fg), (line[len(head):], fg)]
        for chunk, color in runs:
            if st.outline:
                path = QPainterPath()
                path.addText(base, font, chunk)
                p.setPen(QPen(QColor(0, 0, 0, 230), max(2.0, px * 0.12), Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.setPen(color)
            p.drawText(base, chunk)
            base.setX(base.x() + fm.horizontalAdvance(chunk))
    p.restore()


# ----------------------------------------------------------------- vídeo
class VideoSurface(QWidget):
    """Mostra os quadros do vídeo e a legenda do instante de cada quadro."""

    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()

    def __init__(self, subtitle_at: Callable[[float], tuple[str, str, str]], style_fn: Callable[[], SubStyle]):
        super().__init__()
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.frame = QVideoFrame()
        self.frame_t = -1.0          # horário do quadro na tela, em segundos
        self.position_fn: Callable[[], float] = lambda: 0.0
        self._subtitle_at = subtitle_at
        self._style = style_fn
        self.setMouseTracking(True)
        self.setMinimumSize(160, 90)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._click = QTimer(self, singleShot=True, interval=220)
        self._click.timeout.connect(self.clicked)

    def clear(self) -> None:
        self.frame = QVideoFrame()
        self.frame_t = -1.0
        self.update()

    def _on_frame(self, frame: QVideoFrame) -> None:
        self.frame = QVideoFrame(frame)
        t = frame.startTime()
        pos = self.position_fn()
        # Alguns arquivos têm um deslocamento no início; nesse caso vale a posição do player.
        self.frame_t = t / 1e6 if t >= 0 and abs(t / 1e6 - pos) < 1.0 else pos
        self.update()

    def current_time(self) -> float:
        return self.frame_t if self.frame_t >= 0 else self.position_fn()

    def video_rect(self) -> QRectF:
        r = QRectF(self.rect())
        if not self.frame.isValid():
            return r
        size = self.frame.size()
        if self.frame.rotation() in (QVideoFrame.RotationAngle.Rotation90, QVideoFrame.RotationAngle.Rotation270):
            size = QSize(size.height(), size.width())
        if size.isEmpty():
            return r
        s = QSize(size)
        s.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2, s.width(), s.height())

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        area = self.video_rect()
        if self.frame.isValid():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            opts = QVideoFrame.PaintOptions()
            opts.backgroundColor = Qt.GlobalColor.black
            opts.aspectRatioMode = Qt.AspectRatioMode.KeepAspectRatio
            self.frame.paint(p, QRectF(self.rect()), opts)
        st = self._style()
        if st.show:
            text, label, color = self._subtitle_at(self.current_time())
            paint_subtitle(p, area, st, text, label, color)
        p.end()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._click.start()

    def mouseDoubleClickEvent(self, e) -> None:
        self._click.stop()
        self.doubleClicked.emit()

    def mouseMoveEvent(self, e) -> None:
        self.mouseMoved.emit()
        super().mouseMoveEvent(e)


class SeekSlider(QSlider):
    """Clicar em qualquer ponto da barra pula direto para lá."""

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderGroove, self)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(e.position().toPoint()):
                span = max(groove.width() - handle.width(), 1)
                x = e.position().x() - groove.x() - handle.width() / 2
                v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x), span)
                self.setValue(v)
                self.sliderMoved.emit(v)
        super().mousePressEvent(e)


class VideoPlayer(QWidget):
    """Vídeo com legenda, barra de controles, velocidade e tela cheia."""

    positionChanged = Signal(float)     # segundos, acompanha o quadro na tela
    hasVideoChanged = Signal(bool)
    failed = Signal(str)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self.setObjectName("player")
        self.settings = settings
        self.sub = SubStyle.load(settings)
        self.tr: Optional[Transcript] = None
        self.cues: list[Cue] = []
        self._starts: list[float] = []
        self.source = ""
        self._pending_seek: Optional[int] = None
        self._has_video = False
        self.allow_full = True
        self._dialog: Optional[SubtitleDialog] = None
        self._home: Optional[tuple] = None          # onde o player estava antes da tela cheia

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        if hasattr(self.player, "setPitchCompensation"):
            self.player.setPitchCompensation(True)   # voz sem ficar fina ao acelerar

        self.surface = VideoSurface(self.subtitle_at, lambda: self.sub)
        self.surface.position_fn = lambda: self.player.position() / 1000
        self.surface.setVisible(False)
        self.player.setVideoSink(self.surface.sink)
        self.surface.clicked.connect(self.toggle_play)
        self.surface.doubleClicked.connect(self.toggle_full)
        self.surface.mouseMoved.connect(self._wake_controls)
        self.surface.sink.videoFrameChanged.connect(lambda _f: self._tick())

        self.bar = QWidget()
        self.bar.setObjectName("playerBar")
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(0, 6, 0, 6)
        bl.setSpacing(4)
        self.btn_play = self._tool("Tocar (espaço)")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_back = self._tool("Voltar 5 segundos (←)")
        self.btn_back.clicked.connect(lambda: self.skip(-5))
        self.btn_fwd = self._tool("Avançar 5 segundos (→)")
        self.btn_fwd.clicked.connect(lambda: self.skip(5))
        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName("Posição")
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setObjectName("playTime")
        self.lbl_time.setFont(style.tabular(self.lbl_time.font()))
        self.btn_speed = QToolButton()
        self.btn_speed.setObjectName("speed")
        self.btn_speed.setToolTip("Velocidade")
        self.btn_speed.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_speed.setFont(style.tabular(self.btn_speed.font()))
        menu = QMenu(self.btn_speed)
        self._speed_actions = {}
        for sp in SPEEDS:
            a = menu.addAction(self._speed_text(sp) + (" (normal)" if sp == 1 else ""))
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, v=sp: self.set_speed(v))
            self._speed_actions[sp] = a
        self.btn_speed.setMenu(menu)
        self.btn_cc = self._tool("Legendas (C)")
        self.btn_cc.setCheckable(True)
        self.btn_cc.setChecked(self.sub.show)
        self.btn_cc.toggled.connect(self._toggle_subs)
        self.btn_ccset = QPushButton("Legenda…")
        self.btn_ccset.setToolTip("Fonte, tamanho, cores e posição da legenda")
        self.btn_ccset.clicked.connect(self.edit_subtitles)
        self.btn_full = self._tool("Tela cheia (F)")
        self.btn_full.clicked.connect(self.toggle_full)
        for w in (self.btn_play, self.btn_back, self.btn_fwd):
            bl.addWidget(w)
        bl.addSpacing(4)
        bl.addWidget(self.slider, 1)
        bl.addSpacing(4)
        bl.addWidget(self.lbl_time)
        bl.addSpacing(4)
        for w in (self.btn_speed, self.btn_cc, self.btn_ccset, self.btn_full):
            bl.addWidget(w)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.surface, 1)
        lay.addWidget(self.bar)

        self.player.positionChanged.connect(lambda _ms: self._tick())
        self.player.durationChanged.connect(lambda ms: ms > 0 and self.slider.setRange(0, ms))
        self.player.playbackStateChanged.connect(lambda _s: self._sync_icons())
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.hasVideoChanged.connect(self._on_has_video)
        self.player.errorOccurred.connect(lambda _e, msg: self.failed.emit(msg))

        # Sem quadros novos (só áudio, ou pausado) a legenda e a barra andam por este relógio.
        self._clock = QTimer(self, interval=100)
        self._clock.timeout.connect(self._tick)
        self._hide_bar = QTimer(self, singleShot=True, interval=2500)
        self._hide_bar.timeout.connect(self._sleep_controls)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for keys, fn in (("Space", self.toggle_play), ("Left", lambda: self.skip(-5)),
                         ("Right", lambda: self.skip(5)), ("F", self.toggle_full),
                         ("C", lambda: self.btn_cc.toggle()), ("Esc", self._exit_full),
                         ("Shift+.", lambda: self.step_speed(1)), ("Shift+,", lambda: self.step_speed(-1))):
            QShortcut(QKeySequence(keys), self, fn, context=ctx)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.set_speed(float(settings.value("player/velocidade", 1.0)), save=False)
        self.apply_theme()

    @staticmethod
    def _tool(tip: str) -> QToolButton:
        b = QToolButton()
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setIconSize(QSize(16, 16))
        return b

    @staticmethod
    def _speed_text(sp: float) -> str:
        return f"{sp:g}×".replace(".", ",")

    def apply_theme(self) -> None:
        ink = self.palette().color(QPalette.ColorRole.WindowText)
        self.btn_back.setIcon(style.icon("back", ink))
        self.btn_fwd.setIcon(style.icon("forward", ink))
        self.btn_cc.setIcon(style.icon("captions", ink))
        self._sync_icons()

    def _sync_icons(self) -> None:
        ink = self.palette().color(QPalette.ColorRole.WindowText)
        playing = self.is_playing()
        self.btn_play.setIcon(style.icon("pause" if playing else "play", ink))
        self.btn_play.setToolTip("Pausar (espaço)" if playing else "Tocar (espaço)")
        self.btn_full.setIcon(style.icon("collapse" if self.isFullScreen() else "expand", ink))
        if playing:
            self._clock.start()
        else:
            self._clock.stop()
            self._wake_controls()
        self._tick()

    # ------------------------------------------------------------- mídia
    def set_media(self, source: str, tr: Optional[Transcript]) -> None:
        """Abre o arquivo (sem tocar) e prepara as legendas da transcrição."""
        self.set_transcript(tr)
        if source == self.source:
            return
        self.player.stop()
        self.source = source
        self._pending_seek = None
        self.surface.clear()
        self.slider.setRange(0, int((tr.duration if tr else 0) * 1000))
        self.slider.setValue(0)
        self.player.setSource(QUrl.fromLocalFile(source) if source else QUrl())
        self._tick()

    def set_transcript(self, tr: Optional[Transcript]) -> None:
        self.tr = tr
        self.cues = build_cues(tr) if tr else []
        self._starts = [c.start for c in self.cues]
        self.surface.update()

    def subtitle_at(self, t: float) -> tuple[str, str, str]:
        """(texto, nome do falante, cor do falante) da legenda no instante t."""
        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0 or t >= self.cues[i].end or self.tr is None:
            return "", "", ""
        c = self.cues[i]
        label = color = ""
        if self.tr.diarized and c.speaker is not None and self.sub.speaker:
            label = self.tr.speaker_label(c.speaker)
            pal = style.DARK.speakers          # sobre o vídeo, os tons claros leem melhor
            color = pal[c.speaker % len(pal)]
        return c.text, label, color

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def toggle_play(self) -> None:
        if not self.source:
            return
        if self.is_playing():
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def stop(self) -> None:
        self.player.pause()

    def seek(self, seconds: float, play: bool = True) -> None:
        if not self.source:
            return
        ms = int(max(seconds, 0) * 1000)
        if self.player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadingMedia, QMediaPlayer.MediaStatus.NoMedia):
            self._pending_seek = ms          # o arquivo ainda está abrindo: posiciona quando carregar
        else:
            self.player.setPosition(ms)
        if play:
            self.player.play()
        self._tick()

    def skip(self, delta: float) -> None:
        dur = self.player.duration()
        ms = self.player.position() + int(delta * 1000)
        self.player.setPosition(max(0, min(ms, dur - 100 if dur > 0 else ms)))
        self._wake_controls()

    def set_speed(self, sp: float, save: bool = True) -> None:
        sp = min(SPEEDS, key=lambda v: abs(v - sp))
        self.player.setPlaybackRate(sp)
        self.btn_speed.setText(self._speed_text(sp))
        for v, a in self._speed_actions.items():
            a.setChecked(v == sp)
        if save:
            self.settings.setValue("player/velocidade", sp)

    def step_speed(self, d: int) -> None:
        i = min(range(len(SPEEDS)), key=lambda k: abs(SPEEDS[k] - self.player.playbackRate()))
        self.set_speed(SPEEDS[max(0, min(len(SPEEDS) - 1, i + d))])

    def current_time(self) -> float:
        return self.surface.current_time() if self._has_video else self.player.position() / 1000

    def _on_status(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if self._pending_seek is not None and status in (S.LoadedMedia, S.BufferedMedia, S.BufferingMedia):
            self.player.setPosition(self._pending_seek)
            self._pending_seek = None
        if status == S.LoadedMedia and self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()      # pausado (não parado) mostra o quadro atual em vez de tela preta
        if status == S.EndOfMedia:
            self._sync_icons()

    def _on_has_video(self, has: bool) -> None:
        self._has_video = has
        self.surface.setVisible(has)
        self.btn_full.setVisible(has and self.allow_full)
        self.hasVideoChanged.emit(has)

    def has_video(self) -> bool:
        return self._has_video

    def _tick(self) -> None:
        t = self.current_time()
        if not self.slider.isSliderDown():
            self.slider.setValue(int(t * 1000))
        total = self.player.duration() / 1000 or (self.tr.duration if self.tr else 0)
        self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(total)}")
        if not self._has_video or not self.is_playing():
            self.surface.update()
        self.positionChanged.emit(t)

    # ------------------------------------------------------------- legenda
    def _toggle_subs(self, on: bool) -> None:
        self.sub.show = on
        self.sub.save(self.settings)
        self.surface.update()

    def edit_subtitles(self) -> None:
        if self._dialog is None:
            self._dialog = SubtitleDialog(self.sub, self.window())
            self._dialog.changed.connect(self._subs_changed)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _subs_changed(self) -> None:
        self.sub.save(self.settings)
        self.btn_cc.blockSignals(True)
        self.btn_cc.setChecked(self.sub.show)
        self.btn_cc.blockSignals(False)
        self.surface.update()

    # ------------------------------------------------------------- tela cheia
    def toggle_full(self) -> None:
        if self._home is not None:
            self._exit_full()
            return
        if not self._has_video or not self.allow_full:
            return
        parent = self.parentWidget()
        lay = parent.layout() if parent else None
        idx = lay.indexOf(self) if lay is not None else -1
        if parent is None or (lay is None and not hasattr(parent, "insertWidget")):
            return
        if lay is None or idx < 0:
            idx = parent.indexOf(self)            # QSplitter
            lay = None
        sizes = parent.sizes() if hasattr(parent, "sizes") else None
        self._home = (parent, lay, idx, sizes)
        self.setParent(None)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setStyleSheet("#player{background:black}")
        self.showFullScreen()
        self.setFocus()
        self._sync_icons()

    def _exit_full(self) -> None:
        if self._home is None:
            return
        parent, lay, idx, sizes = self._home
        self._home = None
        self.showNormal()
        self.setWindowFlag(Qt.WindowType.Window, False)
        self.setStyleSheet("")
        if lay is not None:
            lay.insertWidget(idx, self)
        else:
            parent.insertWidget(idx, self)
            if sizes:
                parent.setSizes(sizes)
        self.bar.show()
        self.surface.unsetCursor()
        self.show()
        self._sync_icons()

    def _wake_controls(self) -> None:
        self.bar.show()
        self.surface.unsetCursor()
        if self._home is not None and self.is_playing():
            self._hide_bar.start()

    def _sleep_controls(self) -> None:
        if self._home is not None and self.is_playing():
            self.bar.hide()
            self.surface.setCursor(Qt.CursorShape.BlankCursor)


# ----------------------------------------------------------------- configurações
class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, color: str, title: str) -> None:
        super().__init__()
        self.title = title
        self.setFixedWidth(64)
        self.setAutoDefault(False)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color: str) -> None:
        self.color = color
        pm = QPixmap(QSize(30, 16) * self.devicePixelRatioF())
        pm.setDevicePixelRatio(self.devicePixelRatioF())
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(128, 128, 128, 160), 1))
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(0.5, 0.5, 29, 15), 3, 3)
        p.end()
        self.setIcon(QIcon(pm))
        self.setIconSize(QSize(30, 16))
        self.setToolTip(color)

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self.window(), self.title)
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(c.name())


class SubtitlePreview(QWidget):
    """Amostra da legenda sobre um fundo de vídeo, para ajustar mesmo sem vídeo tocando."""

    def __init__(self, st: SubStyle) -> None:
        super().__init__()
        self.st = st
        self.setMinimumSize(320, 180)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        r = QRectF(self.rect())
        from PySide6.QtGui import QLinearGradient
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor("#3d5a73"))
        g.setColorAt(0.55, QColor("#9aa7a0"))
        g.setColorAt(1, QColor("#d9c9a3"))
        p.fillRect(r, g)
        st = SubStyle(**{**asdict(self.st), "show": True})
        paint_subtitle(p, r, st, "Claro! A região sul cresceu 12%, puxada por São Paulo e Curitiba.",
                       "Flávia", style.DARK.speakers[1])
        p.end()


class SubtitleDialog(QDialog):
    changed = Signal()

    def __init__(self, st: SubStyle, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Legenda")
        self.st = st
        self.preview = SubtitlePreview(st)

        self.chk_show = QCheckBox("Mostrar legenda no vídeo")
        self.cb_font = QComboBox()
        self.cb_font.addItem("Fonte do sistema", "")
        for fam in QFontDatabase.families():
            if not fam.startswith(".") and not QFontDatabase.isPrivateFamily(fam):
                self.cb_font.addItem(fam, fam)
        self.cb_font.setMaxVisibleItems(18)
        self.sl_size = QSlider(Qt.Orientation.Horizontal)
        self.sl_size.setRange(50, 250)
        self.sl_size.setSingleStep(5)
        self.lbl_size = QLabel()
        self.lbl_size.setFont(style.tabular(self.lbl_size.font()))
        self.lbl_size.setMinimumWidth(44)
        self.chk_bold = QCheckBox("Negrito")
        self.btn_color = ColorButton(st.color, "Cor do texto")
        self.chk_outline = QCheckBox("Contorno")
        self.btn_bg = ColorButton(st.bg, "Cor do fundo")
        self.sl_op = QSlider(Qt.Orientation.Horizontal)
        self.sl_op.setRange(0, 100)
        self.sl_op.setSingleStep(5)
        self.lbl_op = QLabel()
        self.lbl_op.setFont(style.tabular(self.lbl_op.font()))
        self.lbl_op.setMinimumWidth(44)
        self.cb_pos = QComboBox()
        self.cb_pos.addItem("Embaixo", "bottom")
        self.cb_pos.addItem("Em cima", "top")
        self.chk_spk = QCheckBox("Mostrar o nome de quem fala")
        self.chk_spk_color = QCheckBox("Nome na cor do falante")

        def row(*ws) -> QWidget:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)
            for x in ws:
                h.addWidget(x, 1 if isinstance(x, QSlider) else 0)
            if not any(isinstance(x, QSlider) for x in ws):
                h.addStretch(1)
            return w

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("", self.chk_show)
        form.addRow("Fonte", self.cb_font)
        form.addRow("Tamanho", row(self.sl_size, self.lbl_size))
        form.addRow("Texto", row(self.btn_color, self.chk_bold, self.chk_outline))
        form.addRow("Fundo", row(self.btn_bg))
        form.addRow("Opacidade do fundo", row(self.sl_op, self.lbl_op))
        form.addRow("Posição", self.cb_pos)
        form.addRow("Falante", self.chk_spk)
        form.addRow("", self.chk_spk_color)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.RestoreDefaults |
                                   QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Fechar")
        buttons.button(QDialogButtonBox.StandardButton.Close).setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setAutoDefault(False)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("Restaurar padrão")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)

        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        lay.addWidget(self.preview, 1)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self._load()

        self.chk_show.toggled.connect(lambda v: self._set("show", v))
        self.cb_font.currentIndexChanged.connect(lambda _i: self._set("family", self.cb_font.currentData()))
        self.sl_size.valueChanged.connect(lambda v: self._set("size", v))
        self.chk_bold.toggled.connect(lambda v: self._set("bold", v))
        self.btn_color.changed.connect(lambda c: self._set("color", c))
        self.chk_outline.toggled.connect(lambda v: self._set("outline", v))
        self.btn_bg.changed.connect(lambda c: self._set("bg", c))
        self.sl_op.valueChanged.connect(lambda v: self._set("bg_opacity", v))
        self.cb_pos.currentIndexChanged.connect(lambda _i: self._set("position", self.cb_pos.currentData()))
        self.chk_spk.toggled.connect(lambda v: self._set("speaker", v))
        self.chk_spk_color.toggled.connect(lambda v: self._set("speaker_color", v))

    def _load(self) -> None:
        st = self.st
        widgets = (self.chk_show, self.cb_font, self.sl_size, self.chk_bold, self.chk_outline,
                   self.sl_op, self.cb_pos, self.chk_spk, self.chk_spk_color)
        for w in widgets:
            w.blockSignals(True)
        self.chk_show.setChecked(st.show)
        self.cb_font.setCurrentIndex(max(0, self.cb_font.findData(st.family)))
        self.sl_size.setValue(st.size)
        self.chk_bold.setChecked(st.bold)
        self.btn_color.set_color(st.color)
        self.chk_outline.setChecked(st.outline)
        self.btn_bg.set_color(st.bg)
        self.sl_op.setValue(st.bg_opacity)
        self.cb_pos.setCurrentIndex(max(0, self.cb_pos.findData(st.position)))
        self.chk_spk.setChecked(st.speaker)
        self.chk_spk_color.setChecked(st.speaker_color)
        self.chk_spk_color.setEnabled(st.speaker)
        for w in widgets:
            w.blockSignals(False)
        self.lbl_size.setText(f"{st.size}%")
        self.lbl_op.setText(f"{st.bg_opacity}%")

    def _set(self, name: str, value) -> None:
        setattr(self.st, name, value)
        self.lbl_size.setText(f"{self.st.size}%")
        self.lbl_op.setText(f"{self.st.bg_opacity}%")
        self.chk_spk_color.setEnabled(self.st.speaker)
        self.preview.update()
        self.changed.emit()

    def _defaults(self) -> None:
        show = self.st.show
        for k, v in asdict(SubStyle()).items():
            setattr(self.st, k, v)
        self.st.show = show
        self._load()
        self.preview.update()
        self.changed.emit()


class MediaSplit(QSplitter):
    """Splitter do vídeo com o texto; avisa quando muda de tamanho para trocar a orientação."""

    resized = Signal()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.resized.emit()
