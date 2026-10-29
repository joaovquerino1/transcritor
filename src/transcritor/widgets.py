"""Widgets próprios da interface: layout que quebra linha, lista de arquivos,
editor de nomes dos falantes e a área de leitura da transcrição."""

from __future__ import annotations

import html
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QTextFrameFormat
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTextBrowser, QWidget, QWidgetItem,
)

from . import style
from .engine import Transcript, fmt_time

STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
META_ROLE = Qt.ItemDataRole.UserRole + 2
FRAC_ROLE = Qt.ItemDataRole.UserRole + 3


class FlowLayout(QLayout):
    """Coloca os itens lado a lado e quebra a linha quando falta largura."""

    def __init__(self, parent: QWidget | None = None, hspacing: int = 12, vspacing: int = 6) -> None:
        super().__init__(parent)
        self.items: list[QWidgetItem] = []
        self.hs, self.vs = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self.items.append(item)

    def count(self) -> int:
        return len(self.items)

    def itemAt(self, i: int):
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i: int):
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self.items:
            size = size.expandedTo(it.widget().sizeHint() if it.widget() else it.sizeHint())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect: QRect, dry: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = r.x(), r.y(), 0
        for it in self.items:
            if it.widget() and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            # O tamanho do próprio widget, não o do item: no macOS o item desconta a
            # margem do anel de foco e a caixa de seleção sairia cortada embaixo.
            w = it.widget()
            hint = w.sizeHint() if w else it.sizeHint()
            if x + hint.width() > r.right() + 1 and line > 0:
                x, y, line = r.x(), y + line + self.vs, 0
            if not dry:
                if w:
                    w.setGeometry(QRect(QPoint(x, y), hint))
                else:
                    it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.hs
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


class FlowBox(QWidget):
    """Contêiner de um FlowLayout que reserva a altura das linhas quebradas.

    Dentro de um QScrollArea o layout pai não consulta heightForWidth, então a altura
    mínima é ajustada a cada mudança de largura.
    """

    def __init__(self, hspacing: int = 12, vspacing: int = 6) -> None:
        super().__init__()
        self.flow = FlowLayout(self, hspacing, vspacing)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        h = self.flow.heightForWidth(e.size().width())
        if h != self.minimumHeight():
            self.setMinimumHeight(h)


class Responsive(QWidget):
    """Linha que vira coluna abaixo de uma largura: título e ações do cabeçalho."""

    def __init__(self, breakpoint: int, spacing: int = 12) -> None:
        super().__init__()
        self.breakpoint = breakpoint
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(spacing)

    def resizeEvent(self, e) -> None:
        narrow = e.size().width() < self.breakpoint
        want = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self.box.direction() != want:
            self.box.setDirection(want)
        super().resizeEvent(e)


class FileDelegate(QStyledItemDelegate):
    """Duas linhas por arquivo: o nome e, abaixo, o estado ou a duração."""

    def sizeHint(self, option, index) -> QSize:
        fm = option.fontMetrics
        return QSize(200, fm.height() * 2 + 18)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        st = widget.style() if widget else QApplication.style()
        opt.text = ""
        st.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, p, widget)

        t = style.tokens()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        active = bool(opt.state & QStyle.StateFlag.State_Active)
        r = opt.rect.adjusted(10, 0, -10, 0)
        pm = style.status_pixmap(index.data(STATUS_ROLE) or "pending", index.data(FRAC_ROLE) or 0.0, t)
        isz = 18
        p.drawPixmap(r.x(), r.center().y() - isz // 2, pm)

        text_x = r.x() + isz + 10
        width = r.right() - text_x
        name_font = QFont(opt.font)
        meta_font = style.tabular(QFont(opt.font))
        meta_font.setPointSizeF(opt.font.pointSizeF() * 0.88)
        fm, mfm = QFontMetrics(name_font), QFontMetrics(meta_font)
        top = r.center().y() - (fm.height() + mfm.height() + 2) // 2

        on_sel = selected and (active or not style.is_dark())
        fg = opt.palette.highlightedText().color() if on_sel and active else opt.palette.text().color()
        p.save()
        p.setFont(name_font)
        p.setPen(fg)
        p.drawText(QRect(text_x, top, width, fm.height()), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideMiddle, width))
        meta = index.data(META_ROLE) or ""
        if meta:
            sec = fg if on_sel and active else style.QColor(t.text2)
            if index.data(STATUS_ROLE) == "error" and not (on_sel and active):
                sec = style.QColor(t.rec)
            p.setFont(meta_font)
            p.setPen(sec)
            p.drawText(QRect(text_x, top + fm.height() + 2, width, mfm.height()), Qt.AlignmentFlag.AlignVCenter,
                       mfm.elidedText(meta, Qt.TextElideMode.ElideRight, width))
        p.restore()


class LevelMeter(QWidget):
    """Barrinha de volume de uma fonte de áudio: mostra na hora se o microfone está chegando."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(54, 6)
        self.level = 0.0
        self.setToolTip("Volume que está chegando")

    def set_peak(self, peak: float) -> None:
        import math
        db = 20 * math.log10(max(peak, 1e-5))
        level = min(max((db + 60) / 60, 0.0), 1.0)      # -60 dB .. 0 dB
        if abs(level - self.level) > 0.01:
            self.level = level
            self.update()

    def paintEvent(self, e) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(t.line))
        p.drawRoundedRect(r, 3, 3)
        if self.level > 0:
            p.setBrush(QColor(t.ok))
            p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * self.level, r.height()), 3, 3)


class SpeakerEditor(FlowBox):
    """Um campo por falante, com a cor dele, para trocar "Falante 2" pelo nome real."""

    renamed = Signal(int, str)

    def __init__(self) -> None:
        super().__init__(hspacing=16, vspacing=8)

    def set_speakers(self, names: dict[int, str]) -> None:
        while self.flow.count():
            it = self.flow.takeAt(0)
            chip = it.widget()
            if chip:
                chip.setParent(None)
                chip.deleteLater()
        t = style.tokens()
        for spk, name in sorted(names.items()):
            chip = QWidget()
            h = QHBoxLayout(chip)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            d = QLabel()
            d.setPixmap(style.dot(t.speakers[spk % len(t.speakers)]))
            ed = QLineEdit(name)
            ed.setPlaceholderText(f"Falante {spk + 1}")
            ed.setToolTip(f"Nome do Falante {spk + 1}")
            ed.setFixedWidth(150)
            ed.textEdited.connect(lambda txt, s=spk: self.renamed.emit(s, txt))
            h.addWidget(d)
            h.addWidget(ed)
            self.flow.addWidget(chip)
        self.setMinimumHeight(self.flow.heightForWidth(max(self.width(), 1)))
        self.updateGeometry()


class TranscriptView(QTextBrowser):
    """Leitura da transcrição: coluna de falante e tempo à esquerda, fala à direita.

    A largura do texto fica limitada (linhas longas cansam) e, em janelas estreitas,
    o falante passa para cima da fala.
    """

    MEASURE = 820      # largura máxima do conteúdo, em px
    NARROW = 560       # abaixo disso, falante empilhado sobre a fala

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("transcript")
        self.setOpenExternalLinks(False)
        f = style.tabular(QFont(self.font()))
        f.setPointSizeF(f.pointSizeF() + 2)
        self.document().setDefaultFont(f)
        self._tr: Transcript | None = None
        self._narrow = False
        self.follow = False        # ao vivo: acompanha o fim do texto enquanto ele cresce
        self._timer = QTimer(self, singleShot=True, interval=120)
        self._timer.timeout.connect(self._rerender)

    def show_transcript(self, tr: Transcript | None) -> None:
        self._tr = tr
        self._rerender()

    def _rerender(self) -> None:
        bar = self.verticalScrollBar()
        pos = bar.value()
        at_end = pos >= bar.maximum() - 40
        if self._tr is None:
            self.clear()
            return
        self._narrow = self.viewport().width() < self.NARROW
        self.setHtml(transcript_html(self._tr, style.tokens(), self._narrow))
        self._apply_margins()
        bar.setValue(bar.maximum() if self.follow and at_end else pos)

    def _apply_margins(self) -> None:
        w = self.viewport().width()
        side = max(20, (w - self.MEASURE) // 2)
        root = self.document().rootFrame()
        fmt: QTextFrameFormat = root.frameFormat()
        fmt.setLeftMargin(side)
        fmt.setRightMargin(side)
        fmt.setTopMargin(16)
        fmt.setBottomMargin(28)
        root.setFrameFormat(fmt)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self._tr is None:
            return
        if (self.viewport().width() < self.NARROW) != self._narrow:
            self._timer.start()
        else:
            self._apply_margins()


def transcript_html(tr: Transcript, t: style.Tokens, narrow: bool) -> str:
    def color(spk: int) -> str:
        return t.speakers[spk % len(t.speakers)]

    rows: list[str] = []
    prev = object()
    for u in tr.utterances:
        ts = f'<span style="color:{t.text2}; font-size:small">{fmt_time(u.start)}</span>'
        who = ""
        if tr.diarized and u.speaker is not None and u.speaker != prev:
            who = (f'<span style="color:{color(u.speaker)}; font-weight:600">'
                   f'{html.escape(tr.speaker_label(u.speaker))}</span>')
        text = html.escape(u.text)
        new_turn = bool(who) or not tr.diarized
        gap = 14 if new_turn else 4
        if narrow:
            head = f"{who}&nbsp;&nbsp;{ts}" if who else ts
            rows.append(f'<p style="margin:{gap}px 0 2px 0">{head}</p>'
                        f'<p style="margin:0; color:{t.text}; line-height:130%">{text}</p>')
        else:
            gutter = f"{who}<br>{ts}" if who else ts
            rows.append(
                f'<tr><td width="150" valign="top" style="padding:{gap}px 16px 0 0">{gutter}</td>'
                f'<td valign="top" style="padding-top:{gap}px; color:{t.text}; line-height:130%">{text}</td></tr>')
        prev = u.speaker
    if narrow:
        return "".join(rows)
    return f'<table width="100%" cellspacing="0" cellpadding="0">{"".join(rows)}</table>'


def debounce(parent: QWidget, ms: int, fn: Callable[[], None]) -> QTimer:
    t = QTimer(parent, singleShot=True, interval=ms)
    t.timeout.connect(fn)
    return t
