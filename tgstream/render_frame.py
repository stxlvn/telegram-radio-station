import json
import os
import urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from fontTools.ttLib import TTFont

W, H = 1280, 720
BG = (0x17, 0x11, 0x11)
CARD = (0x25, 0x1B, 0x1A)
ON_SURFACE = (0xED, 0xE0, 0xDF)
ON_SURFACE_VARIANT = (0xD4, 0xC3, 0xC2)
PRIMARY = (0xFF, 0xB3, 0xAC)
TERTIARY = (0xFF, 0xD2, 0x7A)
# PIL's ImageDraw ignores the alpha channel on RGB-mode images (renders it as
# opaque), so "translucent white" fills used to look right in the browser
# came out solid near-white here instead. These are that same subtle tint,
# pre-blended against CARD into solid RGB.
NUM_CIRCLE_FILL = (54, 45, 44)
TRACK_TINT = (63, 54, 53)

fonts_dir = "/opt/tgstream/fonts"
webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
avatar_path = "/opt/tgstream/avatar.jpg"

# When set (standby/backup encoders that don't have their own radio pipeline
# writing nowplaying.json/queue.json/cover.jpg locally), fetch that data over
# HTTP from the primary encoder's icecast webroot instead of reading local files.
RADIO_BASE_URL = os.environ.get("RADIO_BASE_URL", "").rstrip("/")
_remote_cover_cache = "/tmp/tgstream_remote_cover.jpg"


def _fetch_json(path):
    with urllib.request.urlopen(f"{RADIO_BASE_URL}{path}", timeout=5) as r:
        return json.load(r)


def _fetch_cover():
    try:
        with urllib.request.urlopen(f"{RADIO_BASE_URL}/cover.jpg", timeout=5) as r:
            data = r.read()
        with open(_remote_cover_cache, "wb") as f:
            f.write(data)
        return _remote_cover_cache
    except Exception:
        return None

_np_title_font = None
_np_artist_font = None
_queue_head_font = None
_queue_title_font = None
_queue_artist_font = None
_queue_num_font = None
_brand_name_font = None
_brand_handle_font = None
_brand_cta_font = None
_progress_time_font = None
_glow_bg = None
_brand_avatar = None

CHANNEL_NAME = "MusicmaniA"
CHANNEL_HANDLE = "@" + os.environ.get("TELEGRAM_CHANNEL", "your_channel")
CTA_TEXT = "ПОДПИСАТЬСЯ"


def _load_font(path, size, weight=None):
    f = ImageFont.truetype(path, size)
    try:
        if weight:
            f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


# Fallback chain for glyphs the primary font (RobotoFlex) doesn't cover:
# emoji, CJK, and other symbols/scripts that turn up in track titles pulled
# from the Telegram channel.
FALLBACK_FONT_FILES = [
    os.path.join(fonts_dir, "NotoSans.ttf"),
    os.path.join(fonts_dir, "NotoSansJP.ttf"),
    os.path.join(fonts_dir, "NotoEmoji.ttf"),
]

_cmap_cache = {}
_fallback_font_cache = {}


def _cmap_for(path):
    if path not in _cmap_cache:
        try:
            _cmap_cache[path] = TTFont(path, lazy=True, fontNumber=0).getBestCmap()
        except Exception:
            _cmap_cache[path] = {}
    return _cmap_cache[path]


def _has_glyph(path, ch):
    return ord(ch) in _cmap_for(path)


def _fallback_font_for(ch, size):
    for path in FALLBACK_FONT_FILES:
        if os.path.exists(path) and _has_glyph(path, ch):
            key = (path, size)
            if key not in _fallback_font_cache:
                _fallback_font_cache[key] = ImageFont.truetype(path, size)
            return _fallback_font_cache[key]
    return None


def _split_runs(text, font):
    primary_path = font.path
    size = font.size
    runs = []
    cur_font = font
    buf = ""
    for ch in text:
        use_primary = ch.isspace() or _has_glyph(primary_path, ch)
        f = font if use_primary else (_fallback_font_for(ch, size) or font)
        if f is cur_font:
            buf += ch
        else:
            if buf:
                runs.append((buf, cur_font))
            buf, cur_font = ch, f
    if buf:
        runs.append((buf, cur_font))
    return runs


def text_length_fallback(draw, text, font):
    return sum(draw.textlength(s, font=f) for s, f in _split_runs(text, font))


def draw_text_fallback(draw, xy, text, font, fill):
    x, y = xy
    for s, f in _split_runs(text, font):
        draw.text((x, y), s, font=f, fill=fill)
        x += draw.textlength(s, font=f)
    return x


def _fonts():
    global _np_title_font, _np_artist_font, _queue_head_font
    global _queue_title_font, _queue_artist_font, _queue_num_font
    global _brand_name_font, _brand_handle_font, _brand_cta_font, _progress_time_font
    if _np_title_font is None:
        bold = os.path.join(fonts_dir, "ProductSansBold.ttf")
        regular = os.path.join(fonts_dir, "ProductSansRegular.ttf")
        _np_title_font = _load_font(bold, 36)
        _np_artist_font = _load_font(regular, 27)
        _queue_head_font = _load_font(bold, 20)
        _queue_title_font = _load_font(bold, 21)
        _queue_artist_font = _load_font(regular, 18)
        _queue_num_font = _load_font(regular, 16)
        _brand_name_font = _load_font(bold, 25)
        _brand_handle_font = _load_font(regular, 18)
        _brand_cta_font = _load_font(bold, 19)
        _progress_time_font = _load_font(regular, 16)
    return (
        _np_title_font, _np_artist_font, _queue_head_font, _queue_title_font,
        _queue_artist_font, _queue_num_font, _brand_name_font, _brand_handle_font,
        _brand_cta_font, _progress_time_font,
    )


def _brand_logo(size):
    global _brand_avatar
    if _brand_avatar is None or _brand_avatar[0] != size:
        try:
            av = Image.open(avatar_path)
        except Exception:
            av = Image.new("RGB", (size, size), CARD)
        cropped, mask = circle_crop(av, size)
        _brand_avatar = (size, cropped, mask)
    return _brand_avatar[1], _brand_avatar[2]


def format_time(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _background():
    global _glow_bg
    if _glow_bg is None:
        img = Image.new("RGB", (W, H), BG)
        glow = Image.new("RGB", (W, H), BG)
        gdraw = ImageDraw.Draw(glow)
        gdraw.ellipse((-150, -150, 350, 350), fill=(60, 30, 28))
        gdraw.ellipse((W - 400, H - 400, W + 150, H + 150), fill=(55, 45, 20))
        glow = glow.filter(ImageFilter.GaussianBlur(140))
        _glow_bg = Image.blend(img, glow, 0.5)
    return _glow_bg.copy()


def circle_crop(img, size):
    img = img.resize((size, size)).convert("RGB")
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGB", (size, size), CARD)
    out.paste(img, (0, 0), mask)
    return out, mask


def wrap_text(draw, text, font, max_width):
    if not text:
        return [""]
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_length_fallback(draw, trial, font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def truncate_text(draw, text, font, max_width):
    if text_length_fallback(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if text_length_fallback(draw, candidate, font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis if lo > 0 else ellipsis


def load_now_playing():
    artist, title, cover_local = "", "", None
    started_at, duration = None, None
    try:
        if RADIO_BASE_URL:
            np = _fetch_json("/nowplaying.json")
        else:
            with open(os.path.join(webroot, "nowplaying.json")) as f:
                np = json.load(f)
        artist = np.get("artist") or ""
        title = np.get("title") or ""
        started_at = np.get("started_at")
        duration = np.get("duration")
        cover = np.get("cover")
        if cover:
            if RADIO_BASE_URL:
                cover_local = _fetch_cover()
            else:
                local = os.path.join(webroot, "cover.jpg")
                if os.path.exists(local):
                    cover_local = local
    except Exception:
        pass
    return artist, title, cover_local, started_at, duration


def load_queue():
    try:
        if RADIO_BASE_URL:
            data = _fetch_json("/queue.json")
        else:
            with open(os.path.join(webroot, "queue.json")) as f:
                data = json.load(f)
        return (data.get("queue") or [])[:5]
    except Exception:
        return []


def render(artist, title, cover_local, queue=None, started_at=None, duration=None):
    (
        np_title_font, np_artist_font, queue_head_font, queue_title_font,
        queue_artist_font, queue_num_font, brand_name_font, brand_handle_font,
        brand_cta_font, progress_time_font,
    ) = _fonts()
    queue = queue or []
    img = _background()
    draw = ImageDraw.Draw(img)

    margin = 40
    gap = 24
    bottom_margin = 40
    bar_h = 76
    gap_above_bar = 20
    card_y = margin
    card_bottom = H - bottom_margin - bar_h - gap_above_bar
    card_h = card_bottom - card_y

    now_card_w = 480
    now_card_x = margin
    draw.rounded_rectangle(
        (now_card_x, card_y, now_card_x + now_card_w, card_y + card_h), radius=40, fill=CARD
    )

    avatar_size = 260
    ax = now_card_x + (now_card_w - avatar_size) // 2
    ay = card_y + 40
    avatar_img_path = cover_local or avatar_path
    try:
        av = Image.open(avatar_img_path)
    except Exception:
        av = Image.new("RGB", (avatar_size, avatar_size), CARD)
    cropped, mask = circle_crop(av, avatar_size)
    img.paste(cropped, (ax, ay), mask)
    draw = ImageDraw.Draw(img)

    ty = ay + avatar_size + 26
    text_max_w = now_card_w - 70

    category_text = "В ЭФИРЕ"
    cw = text_length_fallback(draw, category_text, queue_head_font)
    draw_text_fallback(draw, (now_card_x + now_card_w / 2 - cw / 2, ty), category_text, queue_head_font, ON_SURFACE_VARIANT)
    ty += 32

    if title or artist:
        lines = wrap_text(draw, title or "Неизвестный трек", np_title_font, text_max_w)
        for line in lines[:2]:
            lw = text_length_fallback(draw, line, np_title_font)
            draw_text_fallback(draw, (now_card_x + now_card_w / 2 - lw / 2, ty), line, np_title_font, ON_SURFACE)
            ty += 44
        if artist:
            alines = wrap_text(draw, artist, np_artist_font, text_max_w)
            for line in alines[:2]:
                lw = text_length_fallback(draw, line, np_artist_font)
                draw_text_fallback(
                    draw, (now_card_x + now_card_w / 2 - lw / 2, ty), line, np_artist_font, PRIMARY,
                )
                ty += 34

        if duration:
            import time as _time
            elapsed = _time.time() - (started_at or _time.time())
            elapsed = max(0.0, min(elapsed, duration))
            frac = elapsed / duration if duration else 0.0

            bar_w = now_card_w - 80
            bar_x0 = now_card_x + 40
            prog_y = ty + 14
            track_h = 6
            draw.rounded_rectangle(
                (bar_x0, prog_y, bar_x0 + bar_w, prog_y + track_h),
                radius=3, fill=TRACK_TINT,
            )
            fill_w = max(track_h, bar_w * frac)
            draw.rounded_rectangle(
                (bar_x0, prog_y, bar_x0 + fill_w, prog_y + track_h),
                radius=3, fill=PRIMARY,
            )

            time_y = prog_y + track_h + 10
            et = format_time(elapsed)
            dt = format_time(duration)
            draw.text((bar_x0, time_y), et, font=progress_time_font, fill=ON_SURFACE_VARIANT)
            dtw = draw.textlength(dt, font=progress_time_font)
            draw.text((bar_x0 + bar_w - dtw, time_y), dt, font=progress_time_font, fill=ON_SURFACE_VARIANT)
    else:
        heading = "MusicmaniA Radio"
        hw = draw.textlength(heading, font=np_title_font)
        draw.text((now_card_x + now_card_w / 2 - hw / 2, ty), heading, font=np_title_font, fill=ON_SURFACE)

    # Queue panel
    queue_x = now_card_x + now_card_w + gap
    queue_w = W - margin - queue_x
    draw.rounded_rectangle((queue_x, card_y, queue_x + queue_w, card_y + card_h), radius=40, fill=CARD)

    qpad = 36
    qy = card_y + qpad
    draw_text_fallback(draw, (queue_x + qpad, qy), "ДАЛЕЕ В ЭФИРЕ", queue_head_font, ON_SURFACE_VARIANT)
    qy += 50

    if not queue:
        draw_text_fallback(draw, (queue_x + qpad, qy), "Скоро новые треки…", queue_artist_font, ON_SURFACE_VARIANT)
    else:
        row_h = 78
        text_w = queue_w - qpad * 2 - 50
        for i, item in enumerate(queue, 1):
            row_y = qy + (i - 1) * row_h
            num_r = 16
            ncx, ncy = queue_x + qpad + num_r, row_y + num_r
            draw.ellipse(
                (ncx - num_r, ncy - num_r, ncx + num_r, ncy + num_r),
                fill=NUM_CIRCLE_FILL,
                outline=TRACK_TINT,
            )
            ns = str(i)
            draw.text((ncx, ncy), ns, font=queue_num_font, fill=ON_SURFACE, anchor="mm")

            tx = queue_x + qpad + num_r * 2 + 16
            t = truncate_text(draw, item.get("title") or "Неизвестный трек", queue_title_font, text_w)
            draw_text_fallback(draw, (tx, row_y - 4), t, queue_title_font, ON_SURFACE)
            a = item.get("artist") or ""
            if a:
                a = truncate_text(draw, a, queue_artist_font, text_w)
                draw_text_fallback(draw, (tx, row_y + 26), a, queue_artist_font, ON_SURFACE_VARIANT)

    # Brand bar: channel identity + subscribe call-to-action
    bar_x0 = margin
    bar_x1 = W - margin
    bar_y0 = card_bottom + gap_above_bar
    bar_y1 = bar_y0 + bar_h
    draw.rounded_rectangle((bar_x0, bar_y0, bar_x1, bar_y1), radius=30, fill=CARD)

    logo_size = 52
    logo_pad = 12
    logo, logo_mask = _brand_logo(logo_size)
    logo_y = bar_y0 + (bar_h - logo_size) // 2
    img.paste(logo, (bar_x0 + logo_pad, logo_y), logo_mask)
    draw = ImageDraw.Draw(img)

    text_x = bar_x0 + logo_pad + logo_size + 18
    text_block_h = 25 + 4 + 20
    name_y = bar_y0 + (bar_h - text_block_h) // 2
    draw.text((text_x, name_y), CHANNEL_NAME, font=brand_name_font, fill=ON_SURFACE)
    draw.text((text_x, name_y + 29), CHANNEL_HANDLE, font=brand_handle_font, fill=ON_SURFACE_VARIANT)

    cta_pad_x = 22
    cta_pad_y = 12
    cta_w = text_length_fallback(draw, CTA_TEXT, brand_cta_font) + cta_pad_x * 2
    cta_h = 20 + cta_pad_y * 2
    cta_x1 = bar_x1 - 18
    cta_x0 = cta_x1 - cta_w
    cta_y0 = bar_y0 + (bar_h - cta_h) // 2
    cta_y1 = cta_y0 + cta_h
    draw.rounded_rectangle((cta_x0, cta_y0, cta_x1, cta_y1), radius=cta_h / 2, fill=PRIMARY)
    ctw = text_length_fallback(draw, CTA_TEXT, brand_cta_font)
    draw_text_fallback(
        draw, (cta_x0 + (cta_w - ctw) / 2, cta_y0 + cta_pad_y - 1),
        CTA_TEXT, brand_cta_font, BG,
    )

    return img


def main():
    out_path = "/opt/tgstream/frame.jpg"
    artist, title, cover_local, started_at, duration = load_now_playing()
    img = render(artist, title, cover_local, load_queue(), started_at, duration)
    tmp = out_path + ".tmp"
    img.save(tmp, format="JPEG", quality=90)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()
