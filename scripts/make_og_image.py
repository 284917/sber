"""Генерирует static/og.png (1200×630) — картинку для превью ссылки в Telegram и соцсетях.

Запуск: python scripts/make_og_image.py   (нужен Pillow; шрифт Manrope берётся из static/fonts)
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, "static", "fonts")
W, H = 1200, 630
BG, CARD, LINE = (24, 24, 24), (31, 31, 31), (49, 49, 49)
WHITE, GRAY, MUTED = (255, 255, 255), (201, 201, 201), (155, 155, 155)
GREEN, GREEN_SOFT = (33, 160, 56), (28, 52, 33)


def font(weight, size):
    return ImageFont.truetype(os.path.join(FONTS, f"manrope-{weight}.ttf"), size)


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Логотип: зелёный квадрат со стрелкой роста
    d.rounded_rectangle((72, 64, 120, 112), radius=12, fill=GREEN)
    s = 48 / 24
    pts = [(72 + x * s, 64 + y * s) for x, y in [(5, 16), (9.5, 11.5), (13, 15), (19, 8.5)]]
    d.line(pts, fill=WHITE, width=5, joint="curve")
    d.line([(72 + 15 * s, 64 + 8.5 * s), (72 + 19 * s, 64 + 8.5 * s), (72 + 19 * s, 64 + 12.5 * s)], fill=WHITE, width=5,
           joint="curve")
    d.text((136, 66), "Платформа ликвидности", font=font(700, 24), fill=WHITE)
    d.text((136, 96), "Сбер · трансграничное финансирование", font=font(400, 18), fill=MUTED)

    # Заголовок и подзаголовок
    d.text((72, 200), "Валютное финансирование", font=font(700, 44), fill=WHITE)
    d.text((72, 254), "от проверенных кредиторов", font=font(700, 44), fill=GRAY)
    d.text((72, 340), "Одна заявка, несколько предложений", font=font(400, 24), fill=MUTED)
    d.text((72, 374), "и честное сравнение по полной стоимости", font=font(400, 24), fill=MUTED)

    # Плашки
    x = 72
    for label in ("KYC/AML", "EUR · USD · CNY", "SWIFT · СПФС · CIPS"):
        w = d.textlength(label, font=font(600, 20)) + 32
        d.rounded_rectangle((x, 476, x + w, 516), radius=20, fill=GREEN_SOFT)
        d.text((x + 16, 484), label, font=font(600, 20), fill=(123, 208, 140))
        x += w + 12

    # Мини-сравнение предложений
    cx, cy, cw = 760, 176, 368
    d.rounded_rectangle((cx, cy, cx + cw, cy + 360), radius=24, fill=CARD, outline=LINE, width=2)
    d.text((cx + 28, cy + 28), "Полная стоимость, % годовых", font=font(600, 18), fill=MUTED)
    rows = [("Beta Capital", "11,6%", True, ""), ("Rongtong Capital", "12%", False, "450 000 EUR · 6 мес."),
            ("Gulf Trade Finance", "13,2%", False, "500 000 EUR · 6 мес.")]
    for i, (name, value, best, sub) in enumerate(rows):
        y = cy + 76 + i * 88
        if best:
            d.rounded_rectangle((cx + 16, y - 12, cx + cw - 16, y + 64), radius=14, fill=GREEN_SOFT)
        d.text((cx + 32, y), name, font=font(700, 22), fill=WHITE)
        d.text((cx + 32, y + 30), "Лучшее предложение" if best else sub, font=font(400, 16),
               fill=(123, 208, 140) if best else MUTED)
        vw = d.textlength(value, font=font(700, 30))
        d.text((cx + cw - 32 - vw, y + 6), value, font=font(700, 30), fill=WHITE if best else GRAY)

    out = os.path.join(ROOT, "static", "og.png")
    img.save(out, "PNG", optimize=True)
    print(out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
