"""Tokens visuais, folha de estilo e ícones desenhados da interface.

Os controles continuam nativos; a folha de estilo só alcança o que tem objectName
ou a propriedade "role", para não estragar o visual do macOS e do Windows.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap


@dataclass(frozen=True)
class Tokens:
    dark: bool
    bg: str          # área da transcrição
    side: str        # barra lateral, um pouco mais fria que o fundo
    text: str
    text2: str       # texto secundário (≥ 4,5:1 sobre bg e side)
    line: str        # divisórias
    track: str       # trilho da barra de progresso
    ok: str
    rec: str
    rec_bg: str
    speakers: tuple[str, ...]


LIGHT = Tokens(
    dark=False, bg="#FFFFFF", side="#EEF1F4", text="#1D2125", text2="#58616B",
    line="#D8DDE2", track="#D8DDE2", ok="#2F7A3B", rec="#C62828", rec_bg="#FBE9E7",
    speakers=("#2457A6", "#B4461B", "#2F7A3B", "#7A3FA0", "#0F767C", "#A8326B", "#7A5A2E", "#4A5A6A"),
)
DARK = Tokens(
    dark=True, bg="#1E1F21", side="#272A2E", text="#E6E8EA", text2="#A0A8B1",
    line="#3A3F45", track="#3A3F45", ok="#6CC57C", rec="#FF7066", rec_bg="#3B2523",
    speakers=("#86AEF2", "#F09A6E", "#7DC98A", "#C9A2EC", "#5FC6CC", "#EE8AB8", "#D6B27E", "#AAB8C6"),
)


def is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def tokens() -> Tokens:
    return DARK if is_dark() else LIGHT


def accent() -> QColor:
    """A cor de destaque escolhida pelo usuário no sistema (Highlight é a da seleção, mais apagada)."""
    pal = QGuiApplication.palette()
    role = getattr(QPalette.ColorRole, "Accent", QPalette.ColorRole.Highlight)
    return pal.color(QPalette.ColorGroup.Active, role)


def tabular(font: QFont) -> QFont:
    """Algarismos de largura fixa, para cronômetros e tempos não 'dançarem'."""
    f = QFont(font)
    try:
        f.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass
    return f


def stylesheet(t: Tokens) -> str:
    acc = accent()
    hover = acc.lighter(112) if t.dark else acc.darker(108)
    pressed = acc.darker(115)
    return f"""
    QWidget#sidebar {{ background: {t.side}; }}
    QScrollArea#sideScroll {{ background: transparent; border: none; }}
    QWidget#sideInner {{ background: transparent; }}
    QWidget#content, QWidget#emptyState {{ background: {t.bg}; }}
    QWidget#footer {{ background: {t.side}; border-top: 1px solid {t.line}; }}

    QLabel[role="section"] {{ font-weight: 600; color: {t.text}; }}
    QLabel[role="secondary"] {{ color: {t.text2}; }}
    QLabel#title {{ font-size: 19px; font-weight: 600; color: {t.text}; }}
    QLabel#emptyTitle {{ font-size: 17px; font-weight: 600; color: {t.text}; }}
    QFrame[role="divider"] {{ background: {t.line}; border: none; min-height: 1px; max-height: 1px; }}

    QListWidget#files {{ background: transparent; border: none; outline: none; }}
    QTextBrowser#transcript {{ background: {t.bg}; border: none; }}
    QPlainTextEdit#log {{ background: {t.bg}; border: none; }}

    QPushButton#primary {{
        background: {acc.name()}; color: white; border: none; border-radius: 6px;
        padding: 6px 18px; font-weight: 600; min-height: 20px;
    }}
    QPushButton#primary:hover {{ background: {hover.name()}; }}
    QPushButton#primary:pressed {{ background: {pressed.name()}; }}
    QPushButton#primary:disabled {{ background: {t.line}; color: {t.text2}; }}

    QWidget#recPanel[live="true"] {{ background: {t.rec_bg}; border-radius: 8px; }}
    QLabel#recTime {{ color: {t.rec}; font-size: 15px; font-weight: 600; }}
    QLabel#recWarn {{ color: {t.rec}; }}
    QLabel#playTime {{ color: {t.text2}; }}

    QProgressBar#bar {{ border: none; background: {t.track}; border-radius: 2px;
                        min-height: 4px; max-height: 4px; }}
    QProgressBar#bar::chunk {{ background: {acc.name()}; border-radius: 2px; }}

    QToolButton#disclosure {{ border: none; font-weight: 600; color: {t.text}; padding: 2px 0; }}
    QTabWidget::pane {{ border: none; }}
    QTabWidget::tab-bar {{ alignment: center; }}
    QToolButton#flat {{ border: none; padding: 3px; border-radius: 5px; }}
    QToolButton#flat:hover {{ background: {t.line}; }}
    QToolButton#link {{ border: none; color: {acc.name() if not t.dark else acc.lighter(130).name()};
                        padding: 2px 0; text-align: left; }}
    QToolButton#link:hover {{ text-decoration: underline; }}
    """


# ------------------------------------------------------------------ ícones

def _canvas(size: int = 32) -> tuple[QPixmap, QPainter]:
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def _stroke(color: QColor | str, width: float = 1.6) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def icon(name: str, color: QColor | str, size: int = 16) -> QIcon:
    """Ícones de traço único, todos com o mesmo peso, desenhados na hora."""
    pm, p = _canvas(size)
    s = size
    p.setPen(_stroke(color, max(1.4, s / 11)))
    c = s / 2
    if name == "plus":
        p.drawLine(QPointF(c, s * .22), QPointF(c, s * .78))
        p.drawLine(QPointF(s * .22, c), QPointF(s * .78, c))
    elif name == "minus":
        p.drawLine(QPointF(s * .22, c), QPointF(s * .78, c))
    elif name == "sidebar":
        r = QRectF(s * .12, s * .2, s * .76, s * .6)
        p.drawRoundedRect(r, 2, 2)
        p.drawLine(QPointF(s * .4, s * .2), QPointF(s * .4, s * .8))
    elif name == "chev-right":
        path = QPainterPath(QPointF(s * .38, s * .25))
        path.lineTo(s * .63, c)
        path.lineTo(s * .38, s * .75)
        p.drawPath(path)
    elif name == "chev-down":
        path = QPainterPath(QPointF(s * .25, s * .38))
        path.lineTo(c, s * .63)
        path.lineTo(s * .75, s * .38)
        p.drawPath(path)
    elif name == "play":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        path = QPainterPath(QPointF(s * .3, s * .2))
        path.lineTo(s * .8, c)
        path.lineTo(s * .3, s * .8)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(s * .26, s * .2, s * .16, s * .6), 1, 1)
        p.drawRoundedRect(QRectF(s * .58, s * .2, s * .16, s * .6), 1, 1)
    elif name == "record":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(c, c), s * .3, s * .3)
    elif name == "stop":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(s * .26, s * .26, s * .48, s * .48), 2, 2)
    elif name == "copy":
        p.drawRoundedRect(QRectF(s * .32, s * .32, s * .48, s * .5), 2, 2)
        path = QPainterPath(QPointF(s * .2, s * .64))
        path.lineTo(s * .2, s * .2)
        path.lineTo(s * .62, s * .2)
        p.drawPath(path)
    elif name == "folder":
        path = QPainterPath(QPointF(s * .12, s * .3))
        path.lineTo(s * .4, s * .3)
        path.lineTo(s * .48, s * .38)
        path.lineTo(s * .88, s * .38)
        path.lineTo(s * .88, s * .78)
        path.lineTo(s * .12, s * .78)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "save":
        p.drawLine(QPointF(c, s * .18), QPointF(c, s * .62))
        p.drawLine(QPointF(s * .32, s * .45), QPointF(c, s * .63))
        p.drawLine(QPointF(s * .68, s * .45), QPointF(c, s * .63))
        p.drawLine(QPointF(s * .2, s * .82), QPointF(s * .8, s * .82))
    p.end()
    return QIcon(pm)


def status_pixmap(status: str, frac: float, t: Tokens, size: int = 18) -> QPixmap:
    """Indicador de estado de cada arquivo da lista (substitui os emojis)."""
    pm, p = _canvas(size)
    r = QRectF(2, 2, size - 4, size - 4)
    muted = QColor(t.text2)
    if status == "pending":
        p.setPen(_stroke(muted, 1.4))
        p.drawEllipse(r)
    elif status == "running":
        acc = accent()
        p.setPen(_stroke(QColor(t.line), 2))
        p.drawEllipse(r.adjusted(1, 1, -1, -1))
        p.setPen(_stroke(acc, 2))
        span = int(-360 * 16 * max(frac, 0.06)) if frac >= 0 else -90 * 16
        p.drawArc(r.adjusted(1, 1, -1, -1), 90 * 16, span)
    elif status == "done":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.ok))
        p.drawEllipse(r)
        p.setPen(_stroke("white", 1.8))
        path = QPainterPath(QPointF(size * .3, size * .52))
        path.lineTo(size * .44, size * .66)
        path.lineTo(size * .71, size * .37)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    elif status == "error":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.rec))
        p.drawEllipse(r)
        p.setPen(_stroke("white", 1.8))
        p.drawLine(QPointF(size / 2, size * .3), QPointF(size / 2, size * .56))
        p.drawPoint(QPointF(size / 2, size * .71))
    else:  # cancelled
        p.setPen(_stroke(muted, 1.4))
        p.drawEllipse(r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(muted)
        p.drawRect(QRectF(size * .36, size * .36, size * .28, size * .28))
    p.end()
    return pm


def dot(color: str, size: int = 10) -> QPixmap:
    pm, p = _canvas(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    p.end()
    return pm
