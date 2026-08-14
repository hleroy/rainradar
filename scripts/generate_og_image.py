# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "pillow>=10.1",
#   "fonttools[woff]>=4.50",
# ]
# ///
"""Regenerate the link-preview (Open Graph) card from the explainer's own imagery.

    frontend/img/og-image.jpg    1200x630, referenced by og:image / twitter:image

Run it with uv — no project setup or virtualenv needed:

    uv run scripts/generate_og_image.py

Sources are the two layer images the /apropos explainer already ships, so the card
is a real frame of a real storm rather than a mock-up:

    frontend/img/apropos-base.webp        OpenStreetMap base, 1000x1000
    frontend/img/apropos-composite.png    rain + reflectivity halo, 1000x1000, RGBA

Three deliberate choices, so nobody 'fixes' them later:

  - **No title text baked in.** Every platform renders og:title and og:description
    beside the image, so burning the name in duplicates it — and each platform crops
    a different aspect (Facebook 1.91:1, X 2:1), so baked text is the first thing to
    get sliced. The image carries the picture; the meta tags carry the words.

  - **Attribution IS baked in**, bottom-left, exactly as the video export burns it
    into every frame. The card redistributes OpenStreetMap cartography and
    Météo-France radar data outside the app, where Leaflet's attribution control
    cannot follow it, so the credit has to travel with the pixels.

  - **Crop, don't letterbox.** France is roughly square and the card is 1.9:1, so the
    whole country cannot fit without wide dead margins. CROP_TOP selects the
    1000x525 band that is upscaled to fill the card; it is tuned to keep the
    Channel/Brittany coastline (which is what makes the map read as France at a
    glance) together with both storm clusters. Re-tune it if the source frame ever
    changes.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = REPO_ROOT / "frontend" / "img"

BASE = IMG_DIR / "apropos-base.webp"
COMPOSITE = IMG_DIR / "apropos-composite.png"
# The explainer's small-print face, reused here. Pillow cannot read WOFF2, so it is
# decompiled to an in-memory TTF (fonttools' `woff` extra pulls in brotli). Using the
# repo's own font keeps the card reproducible on any machine — Pillow's built-in
# default font has no 'é' and silently renders "Météo" as "M□té□o".
CREDIT_FONT = REPO_ROOT / "frontend" / "fonts" / "plate-mono.woff2"
OUT = IMG_DIR / "og-image.jpg"

CARD_W, CARD_H = 1200, 630
JPEG_QUALITY = 82

# Source band, in the 1000x1000 sources' own pixels. Height is derived so the band
# already has the card's aspect ratio — one clean upscale, no distortion.
CROP_TOP = 170
CROP_H = round(1000 * CARD_H / CARD_W)  # 525

# Mandatory credit. Kept to the two sources actually visible in these layers: the
# base map and the radar mosaic. No lightning is drawn in this frame, so Blitzortung
# is deliberately absent — crediting a source the image does not show is noise.
CREDIT = "© OpenStreetMap  ·  Radar Météo-France"
CREDIT_SIZE = 21
CREDIT_INSET = 26  # from the left and bottom edges
# Chosen to clear X's 2:1 crop of a 1.91:1 card (which shaves ~15px top and bottom).
CREDIT_PAD_X, CREDIT_PAD_Y = 14, 9


def _load_layers() -> Image.Image:
    """Stack the radar composite over the base map, cropped to the card's aspect."""
    box = (0, CROP_TOP, 1000, CROP_TOP + CROP_H)
    base = Image.open(BASE).convert("RGBA").crop(box)
    # The composite is a palette PNG with per-index alpha; converting materialises it.
    overlay = Image.open(COMPOSITE).convert("RGBA").crop(box)
    return Image.alpha_composite(base, overlay).resize((CARD_W, CARD_H), Image.LANCZOS)


def _credit_font() -> ImageFont.FreeTypeFont:
    """Load plate-mono.woff2 as a Pillow font, via an in-memory TTF round-trip."""
    buf = BytesIO()
    TTFont(CREDIT_FONT).save(buf)
    buf.seek(0)
    return ImageFont.truetype(buf, CREDIT_SIZE)


def _draw_credit(card: Image.Image) -> None:
    """Burn the attribution into the bottom-left, on a scrim so it stays legible.

    The scrim matters: the credit sits over live cartography whose local brightness
    is whatever the frame happens to contain, so plain text would be readable on the
    sea and invisible over a yellow echo.
    """
    font = _credit_font()
    draw = ImageDraw.Draw(card, "RGBA")

    left, top, right, bottom = draw.textbbox((0, 0), CREDIT, font=font)
    text_w, text_h = right - left, bottom - top
    box_w = text_w + 2 * CREDIT_PAD_X
    box_h = text_h + 2 * CREDIT_PAD_Y
    box_x = CREDIT_INSET
    box_y = CARD_H - CREDIT_INSET - box_h

    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=7,
        fill=(28, 28, 28, 190),  # theme_color at ~75% — same ink as the app chrome
    )
    draw.text(
        (box_x + CREDIT_PAD_X - left, box_y + CREDIT_PAD_Y - top),
        CREDIT,
        font=font,
        fill=(255, 255, 255, 235),
    )


def main() -> None:
    card = _load_layers()
    _draw_credit(card)
    # JPEG, not PNG: the content is photographic map raster, where PNG lands around
    # 1.1 MB. WhatsApp — a share target the app explicitly targets — drops link
    # previews whose image exceeds a few hundred KB, so size is a correctness
    # concern here, not a nicety. RGB because a JPEG has no alpha to lose.
    card.convert("RGB").save(OUT, quality=JPEG_QUALITY, optimize=True, progressive=True)
    print(
        f"wrote {OUT.relative_to(REPO_ROOT)} ({CARD_W}x{CARD_H}, {OUT.stat().st_size // 1024} KB)"
    )


if __name__ == "__main__":
    main()
