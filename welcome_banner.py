"""Welcome / Goodbye banner generation.

Renders a hue-shifted banner with the user's avatar + a welcome/goodbye
message overlay. The banner template is a single shared file
(`assets/banner_template.png`) designed in cyan; we shift its hue at
runtime to match each server's configured embed color so the banner
visually matches the server's theme.

The username also gets hue-shifted glow — not hardcoded cyan — so when a
server picks green, the username's glow goes green too. Stays consistent.

Image generation runs in a thread pool via asyncio.to_thread so it
doesn't block the bot's event loop.
"""
from __future__ import annotations
import io
import logging
import math
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# Path to the banner template (cyan-toned, dark background, ~2172x414).
# Resolved relative to this module's directory so it works regardless of
# where the bot is launched from.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_TEMPLATE_PATH = os.path.join(_MODULE_DIR, "assets", "banner_template.png")

# Font lookups. Falls back through several candidates so it works on
# different base images. fonts-dejavu-core is installed in our Dockerfile
# so /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf is reliable.
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Arial Bold.ttf",  # macOS dev
    "/System/Library/Fonts/Helvetica.ttc",  # macOS dev
    "C:/Windows/Fonts/arialbd.ttf",  # Windows dev
]
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Return the first font that loads from the candidate list, or default."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    logger.warning("No system font found, falling back to PIL default")
    return ImageFont.load_default()


# Cyan in HSV is around hue 175-180 (full circle is 0-360).
# Pillow's HSV uses 0-255 for each channel, so cyan is around H=125.
# We compute the actual dominant hue of the template image once at module
# load so the shift math is correct regardless of how the template was
# designed.
_TEMPLATE_DOMINANT_HUE: Optional[int] = None


def _compute_template_dominant_hue(image: Image.Image) -> int:
    """Detect the dominant hue of the template image (excluding low-sat pixels).

    Hue-shifting needs a known reference point: "the template is currently
    at hue X, the server wants hue Y, shift by (Y-X)". We auto-detect X by
    histogramming the hue channel, weighted by saturation so we ignore the
    dark/desaturated background pixels and only consider the colorful
    decorative elements.

    Returns hue in PIL's 0-255 range.
    """
    hsv = image.convert("HSV")
    h_pixels = list(hsv.getdata(0))  # Hue channel
    s_pixels = list(hsv.getdata(1))  # Saturation channel

    # Weighted histogram: each pixel votes for its hue with weight=saturation.
    # Desaturated pixels (the dark background) contribute almost nothing.
    bins = [0] * 256
    for h, s in zip(h_pixels, s_pixels):
        if s > 40:  # only count meaningfully-colored pixels
            bins[h] += s

    if not any(bins):
        # Image is monochrome (no detectable hue). Return middle as a safe default.
        logger.warning("Banner template has no detectable dominant hue; using 0")
        return 0
    return bins.index(max(bins))


def _ensure_template_loaded() -> Optional[Image.Image]:
    """Load and cache the banner template image. Returns None on failure."""
    global _TEMPLATE_DOMINANT_HUE
    if not os.path.exists(BANNER_TEMPLATE_PATH):
        logger.error(f"Banner template missing at {BANNER_TEMPLATE_PATH}")
        return None
    try:
        img = Image.open(BANNER_TEMPLATE_PATH).convert("RGB")
        if _TEMPLATE_DOMINANT_HUE is None:
            _TEMPLATE_DOMINANT_HUE = _compute_template_dominant_hue(img)
            logger.info(f"Template dominant hue detected: {_TEMPLATE_DOMINANT_HUE}/255")
        return img
    except Exception as e:
        logger.error(f"Failed to load banner template: {e}")
        return None


def _rgb_to_hue(r: int, g: int, b: int) -> int:
    """Convert an RGB color to PIL hue (0-255).

    Uses standard HSV conversion. Returns 0 for greyscale colors (R=G=B).
    """
    r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r_, g_, b_), min(r_, g_, b_)
    if mx == mn:
        return 0  # greyscale — no hue
    d = mx - mn
    if mx == r_:
        h = ((g_ - b_) / d) % 6
    elif mx == g_:
        h = (b_ - r_) / d + 2
    else:
        h = (r_ - g_) / d + 4
    h = (h / 6.0) * 255.0  # PIL hue is 0-255 not 0-360
    return int(h) % 256


def _color_saturation(r: int, g: int, b: int) -> float:
    """Return saturation 0..1 — 0 means pure greyscale, 1 means fully saturated."""
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == 0:
        return 0.0
    return (mx - mn) / mx


def _hue_shift_image(img: Image.Image, target_hue: int) -> Image.Image:
    """Shift the entire image's hue so the dominant color moves to target_hue.

    The dominant hue of the template was computed at load time. We compute
    the per-channel delta and add it modulo 256 to every pixel's hue.
    Saturation and value are preserved — so the dark background stays
    dark, and bright cyan elements just shift to bright {target} elements.
    """
    if _TEMPLATE_DOMINANT_HUE is None:
        return img  # cache failure; return unmodified

    delta = (target_hue - _TEMPLATE_DOMINANT_HUE) % 256
    if delta == 0:
        return img

    hsv = img.convert("HSV")
    h, s, v = hsv.split()
    # Shift hue band — wrap around 256
    h = h.point(lambda x: (x + delta) % 256)
    shifted = Image.merge("HSV", (h, s, v)).convert("RGB")
    return shifted


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    """Crop an avatar image to a circle of the given diameter with alpha."""
    img = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _draw_glow_text(
    base: Image.Image,
    text: str,
    position: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    glow_color: tuple[int, int, int],
    text_color: tuple[int, int, int] = (255, 255, 255),
    glow_radius: int = 8,
    glow_passes: int = 3,
) -> None:
    """Draw text with a hue-shifted glow effect.

    The glow is rendered larger and more saturated than the original text,
    in the server's accent color, then blurred to create a soft halo. The
    crisp text on top can be white (most readable) or tinted with the
    accent color to pop even more.

    `base` must be RGBA.
    """
    # Glow layer: draw text at full opacity in accent color, multiple times
    # with slight offsets so the glow has body before blurring.
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    # Draw multiple offset copies to make the glow stronger / extend further
    for dx in (-2, 0, 2):
        for dy in (-2, 0, 2):
            glow_draw.text(
                (position[0] + dx, position[1] + dy),
                text, font=font, fill=(*glow_color, 255),
            )

    for _ in range(glow_passes):
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius))

    # Brighten the glow by re-pasting it on top of itself (boosts visibility)
    base.alpha_composite(glow_layer)
    base.alpha_composite(glow_layer)

    # Crisp text on top
    ImageDraw.Draw(base).text(position, text, font=font, fill=(*text_color, 255))


async def render_welcome_banner(
    *,
    username: str,
    server_name: str,
    avatar_bytes: Optional[bytes],
    accent_color: int,
    action: str = "welcome",
    custom_message: Optional[str] = None,
) -> Optional[bytes]:
    """Generate a welcome/goodbye banner PNG and return the bytes.

    Args:
        username: the user's display name
        server_name: the guild's name
        avatar_bytes: PNG/JPEG bytes of the user's avatar, or None
        accent_color: 24-bit RGB integer (the server's embed color)
        action: 'welcome' or 'goodbye'
        custom_message: optional override for the main message. If provided,
                        `{user}` and `{server}` placeholders are substituted.
                        If None, uses a sensible default per action.

    Returns:
        PNG bytes ready to upload to Discord, or None if generation failed.

    Runs in a thread pool — does not block the event loop.
    """
    import asyncio
    return await asyncio.to_thread(
        _render_sync,
        username, server_name, avatar_bytes, accent_color, action, custom_message,
    )


def _render_sync(
    username: str,
    server_name: str,
    avatar_bytes: Optional[bytes],
    accent_color: int,
    action: str,
    custom_message: Optional[str],
) -> Optional[bytes]:
    """The actual blocking image generation. Called via to_thread."""
    template = _ensure_template_loaded()
    if template is None:
        return None

    # ── 1. Hue-shift the template to the server's accent color ────────────
    r = (accent_color >> 16) & 0xFF
    g = (accent_color >> 8) & 0xFF
    b = accent_color & 0xFF

    # If the color is too desaturated (white/black/grey), skip the shift
    # entirely — the result would look weird. Banner stays cyan as-is.
    if _color_saturation(r, g, b) < 0.15:
        banner = template.copy().convert("RGBA")
        glow_rgb = (0, 245, 212)  # default cyan for username glow too
    else:
        target_hue = _rgb_to_hue(r, g, b)
        banner = _hue_shift_image(template, target_hue).convert("RGBA")
        glow_rgb = (r, g, b)

    canvas_w, canvas_h = banner.size

    # ── 2. Avatar ─────────────────────────────────────────────────────────
    # Place the avatar at the vertical middle, left-of-center but right of
    # the logo decoration. Avatar diameter scales with banner height.
    avatar_size = int(canvas_h * 0.55)
    avatar_x = int(canvas_w * 0.14)
    avatar_y = (canvas_h - avatar_size) // 2

    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar_circle = _circle_crop(avatar, avatar_size)
            # Optional: glowing ring around the avatar in accent color.
            ring_thickness = max(4, avatar_size // 36)
            ring_layer = Image.new("RGBA", banner.size, (0, 0, 0, 0))
            ImageDraw.Draw(ring_layer).ellipse(
                (avatar_x - ring_thickness, avatar_y - ring_thickness,
                 avatar_x + avatar_size + ring_thickness,
                 avatar_y + avatar_size + ring_thickness),
                outline=(*glow_rgb, 220), width=ring_thickness,
            )
            # Blur ring slightly for a soft glow
            ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(6))
            banner.alpha_composite(ring_layer)
            banner.alpha_composite(avatar_circle, (avatar_x, avatar_y))
        except Exception as e:
            logger.error(f"Failed to render avatar: {e}")

    # ── 3. Text ───────────────────────────────────────────────────────────
    # Two lines:
    #   Main: "Welcome,"/"Goodbye," in white (medium size) + username in
    #         larger glowing accent text.
    #   Sub:  Server name (welcome) or "has left" (goodbye), in muted grey.
    main_font_size = int(canvas_h * 0.26)
    sub_font_size = int(canvas_h * 0.14)
    name_font_size = int(canvas_h * 0.36)  # username starts bigger so it pops

    main_font = _find_font(FONT_CANDIDATES_BOLD, main_font_size)
    sub_font = _find_font(FONT_CANDIDATES_REGULAR, sub_font_size)

    text_x = avatar_x + avatar_size + int(canvas_h * 0.08)
    text_block_top = int(canvas_h * 0.22)

    # Truncate very long usernames; even after truncation we may still need
    # to scale the font down to fit the canvas width.
    max_chars = 22
    display_name = username if len(username) <= max_chars else username[:max_chars - 1] + "…"

    prefix = "Welcome," if action == "welcome" else "Goodbye,"
    name_text = display_name + "!"

    # Measure the prefix at its fixed size, then size the username font
    # down (if necessary) so prefix + space + name fits within the canvas.
    prefix_bbox = main_font.getbbox(prefix + " ")
    prefix_w = prefix_bbox[2] - prefix_bbox[0]
    available_w = canvas_w - text_x - prefix_w - int(canvas_h * 0.08)  # right margin

    # Shrink name font until it fits, with a sane minimum
    current_name_size = name_font_size
    name_font = _find_font(FONT_CANDIDATES_BOLD, current_name_size)
    while current_name_size > int(canvas_h * 0.18):
        bbox = name_font.getbbox(name_text)
        if (bbox[2] - bbox[0]) <= available_w:
            break
        current_name_size -= 4
        name_font = _find_font(FONT_CANDIDATES_BOLD, current_name_size)

    # Sub-line text: for welcome, the server. For goodbye, simple phrase.
    if action == "welcome":
        sub_text = f"to {server_name}" if server_name else ""
    else:
        sub_text = f"will be missed" if server_name else "has left"

    # Draw prefix (white)
    ImageDraw.Draw(banner).text(
        (text_x, text_block_top), prefix, font=main_font, fill=(255, 255, 255, 255),
    )

    # Draw username with hue-shifted glow, larger size, sitting to the
    # right of the prefix. Align baselines: shift the name down by the
    # ascent difference so the bottom of the text aligns.
    name_x = text_x + prefix_w
    main_ascent, _ = main_font.getmetrics()
    name_ascent, _ = name_font.getmetrics()
    name_y = text_block_top - (name_ascent - main_ascent)

    _draw_glow_text(
        banner,
        name_text,
        (name_x, name_y),
        name_font,
        glow_color=glow_rgb,
        text_color=(255, 255, 255),
        glow_radius=14,
        glow_passes=5,
    )

    # Sub-line below the main row
    if sub_text:
        sub_y = text_block_top + main_font_size + int(canvas_h * 0.04)
        ImageDraw.Draw(banner).text(
            (text_x, sub_y), sub_text, font=sub_font,
            fill=(200, 200, 200, 230),
        )

    # ── 4. Export ─────────────────────────────────────────────────────────
    out = io.BytesIO()
    banner.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def substitute_placeholders(template: str, *, user: str, server: str) -> str:
    """Replace {user} / {server} placeholders in a custom message string."""
    return (template or "").replace("{user}", user).replace("{server}", server)


# ── Default message templates ────────────────────────────────────────────────
DEFAULT_WELCOME = "Welcome to {server}, {user}!"
DEFAULT_GOODBYE = "{user} left {server}."
