from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WARDOGS_BLUE = (46, 162, 204)

MAP_DISPLAY = {
    "Kavkazi": "Bakurani",
    "Europe": "Ozeti",
    "NorthAmerica": "Zestafona",
    "Ozeti": "Ozeti",
    "Bakurani": "Bakurani",
    "Zestafona": "Zestafona",
}

MAP_FLAG = {
    "Bakurani": "RIVER VALLEY · KOLCHIA",
    "Ozeti": "EUROPEAN THEATRE · KOLCHIA",
    "Zestafona": "NORTH AMERICAN THEATRE · KOLCHIA",
}

LIGHTING_RU = {
    "DayStartClear": "Рассвет",
    "DayEarlyClear": "Раннее утро",
    "DayEarlyFog": "Раннее утро, туман",
    "DayClear": "День",
    "DayLateClear": "Поздний день",
    "DayLateGray": "Серый день",
    "DayLateGrayFog": "Серый день, туман",
    "DayEndClear": "Закат",
}


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _sanitize(name):
    return "".join(c if c.isalnum() else "_" for c in str(name))


def _gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def generate_map_card(map_code=None, lighting=None, dest_dir=None):
    """Генерирует тактическую карточку карты (кэш по map+lighting). Возвращает Path."""
    dest = Path(dest_dir) if dest_dir else Path("data/maps")
    dest.mkdir(parents=True, exist_ok=True)
    slug = f"{_sanitize(map_code or 'unknown')}_{_sanitize(lighting or 'any')}"
    out_path = dest / f"{slug}.png"
    if out_path.exists():
        return out_path

    W, H = 1280, 400
    img = _gradient((W, H), (13, 27, 39), (4, 6, 10))
    d = ImageDraw.Draw(img, "RGBA")

    # тактическая сетка
    step = 80
    for x in range(0, W, step):
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, 8), width=1)
    for y in range(0, H, step):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 8), width=1)

    # прицел по центру
    cx, cy = W // 2, H // 2
    d.ellipse([cx - 34, cy - 34, cx + 34, cy + 34], outline=(46, 162, 204, 90), width=2)
    d.ellipse([cx - 44, cy - 44, cx + 44, cy + 44], outline=(46, 162, 204, 45), width=1)
    d.line([(cx - 60, cy), (cx - 48, cy)], fill=(46, 162, 204, 120), width=2)
    d.line([(cx + 48, cy), (cx + 60, cy)], fill=(46, 162, 204, 120), width=2)
    d.line([(cx, cy - 60), (cx, cy - 48)], fill=(46, 162, 204, 120), width=2)
    d.line([(cx, cy + 48), (cx, cy + 60)], fill=(46, 162, 204, 120), width=2)

    # нижняя акцентная полоса
    d.rectangle([0, H - 8, W, H], fill=WARDOGS_BLUE)

    font_small = _font(r"C:\Windows\Fonts\consolab.ttf", 26)
    font_big = _font(r"C:\Windows\Fonts\bahnschrift.ttf", 92)
    font_sub = _font(r"C:\Windows\Fonts\arialbd.ttf", 28)
    font_sub2 = _font(r"C:\Windows\Fonts\arial.ttf", 24)

    raw = str(map_code or "?").strip()
    display = MAP_DISPLAY.get(raw, raw)
    flag = MAP_FLAG.get(display, "THEATRE OF OPERATIONS")

    d.text((48, 30), "WARDOGS // RCON LIVE", font=font_small, fill=(46, 162, 204, 255))
    d.text((48, 112), display, font=font_big, fill=(255, 255, 255, 255))
    d.text((50, 248), flag, font=font_sub, fill=(200, 214, 224, 255))
    light_ru = LIGHTING_RU.get(str(lighting or ""), lighting or "—")
    d.text((50, 290), f"Свет: {light_ru}", font=font_sub2, fill=(140, 160, 175, 255))

    img.save(out_path, "PNG")
    return out_path