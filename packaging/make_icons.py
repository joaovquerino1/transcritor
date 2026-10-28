"""Gera assets/icon.png, icon.icns (macOS) e icon.ico (Windows). Rode uma vez: python packaging/make_icons.py"""
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024
    m = 100 * s
    rect = QRectF(m, m, size - 2 * m, size - 2 * m)
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0, QColor("#3b6cf6"))
    g.setColorAt(1, QColor("#7b2ff7"))
    path = QPainterPath()
    path.addRoundedRect(rect, 185 * s, 185 * s)
    p.fillPath(path, g)
    # Balão de fala
    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(220 * s, 250 * s, 584 * s, 400 * s), 90 * s, 90 * s)
    tail = QPainterPath()
    tail.moveTo(330 * s, 620 * s)
    tail.lineTo(290 * s, 780 * s)
    tail.lineTo(470 * s, 640 * s)
    tail.closeSubpath()
    p.fillPath(bubble.united(tail), QColor(255, 255, 255, 240))
    # Linhas de texto / onda sonora
    pen = QPen(QColor("#4b4fe8"), 46 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    heights = [70, 150, 220, 130, 190, 90, 160, 60]
    x0 = 300 * s
    for i, h in enumerate(heights):
        x = x0 + i * 61 * s
        p.drawLine(int(x), int((450 - h / 2) * s), int(x), int((450 + h / 2) * s))
    p.end()
    return img


def main():
    QGuiApplication(sys.argv)
    ASSETS.mkdir(exist_ok=True)
    draw(1024).save(str(ASSETS / "icon.png"))
    # .ico com vários tamanhos via Pillow
    from PIL import Image
    Image.open(ASSETS / "icon.png").save(ASSETS / "icon.ico",
                                         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin":
        iconset = ASSETS / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for base in (16, 32, 128, 256, 512):
            draw(base).save(str(iconset / f"icon_{base}x{base}.png"))
            draw(base * 2).save(str(iconset / f"icon_{base}x{base}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)
        for f in iconset.iterdir():
            f.unlink()
        iconset.rmdir()
    print("Ícones gerados em", ASSETS)


if __name__ == "__main__":
    main()
