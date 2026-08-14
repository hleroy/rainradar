# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "cairosvg>=2.7",
#   "pillow>=10",
# ]
# ///
"""Regenerate every derived app-icon asset from the master SVG.

``frontend/favicon.svg`` is the single master (edit it in Inkscape). This script
rasterizes/derives everything else from it:

    frontend/icons/icon-192.png             transparent bg, artwork bled to fill
    frontend/icons/icon-512.png             transparent bg, artwork bled to fill
    frontend/icons/icon-maskable-512.png    opaque bg edge-to-edge, artwork in safe zone
    frontend/icons/apple-touch-icon-180.png opaque bg edge-to-edge, artwork in safe zone
    docs/logo-dark.svg                      cropped artwork, no bg, for dark README bg
    docs/logo-light.svg                     cropped artwork, no bg, recolored cloud

Run it with uv — no project setup or virtualenv needed:

    uv run scripts/generate_icons.py

Rasterizes with ``cairosvg`` rather than ImageMagick: ImageMagick's SVG support falls
back to its own built-in coder (coarse Bézier tessellation — round shapes render
angular) whenever the `rsvg-convert` binary isn't on PATH. ``cairosvg`` always renders
curves properly, and uv fetches it automatically — nothing to `apt install`.

Parses the master as real XML, tolerant of however Inkscape formats it. Two things it
relies on staying true, which Inkscape preserves unless you change them yourself:

  - all the artwork (bolt + drops + cloud) is one top-level group, id="rainradar_icon"
  - the cloud shapes are grouped under a group with id="cloud" (so it can be
    recolored for docs/logo-light.svg)

The master has no background of its own — the maskable/apple-touch-icon background
color is instead the ``BG_COLOR`` constant below, kept in sync with
``frontend/manifest.webmanifest``'s ``theme_color``.

``SAFE_ZONE_SCALE`` and ``LOGO_CROP_VIEWBOX`` are tuned to this icon's proportions;
re-tune them if a redesign changes the artwork's aspect ratio noticeably.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import cairosvg
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER = REPO_ROOT / "frontend" / "favicon.svg"
ICONS_DIR = REPO_ROOT / "frontend" / "icons"
DOCS_DIR = REPO_ROOT / "docs"

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

ARTWORK_GROUP_ID = "rainradar_icon"

# Artwork occupies roughly this fraction of the 32x32 canvas; shrinking it by this
# factor (centered) keeps it inside the ~80% "safe zone" maskable icons need.
SAFE_ZONE_SCALE = 0.62

# Tight crop around the artwork, in the master's 32x32 user units, for the docs logos.
LOGO_CROP_VIEWBOX = "1 1.5 30 29"

LIGHT_BG_CLOUD_COLOR = "#566370"  # darker cloud, legible on a light README background
BG_COLOR = "#1c1c1c"  # matches manifest.webmanifest's theme_color

# Bleed icons (icon-192/512) are rasterized once at this size, then cropped to content
# and downsized per target, so every resize is a downsample (crisp, never blurry).
BLEED_RENDER_SIZE = 2048


def _strip_foreign_attribs(el: ET.Element) -> None:
    """Drop editor-namespace attributes (inkscape:*, sodipodi:*) recursively."""
    for k in [k for k in el.attrib if k.startswith("{")]:
        del el.attrib[k]
    for child in el:
        _strip_foreign_attribs(child)


def _get_fill(el: ET.Element) -> str | None:
    if "fill" in el.attrib:
        return el.attrib["fill"]
    m = re.search(r"fill:\s*(#[0-9a-fA-F]{6})", el.attrib.get("style", ""))
    return m.group(1) if m else None


def _find_fill(el: ET.Element) -> str | None:
    """The fill of el or the first descendant that has one."""
    return next(filter(None, (_get_fill(e) for e in el.iter())), None)


def _recolor(el: ET.Element, old: str, new: str) -> None:
    for e in el.iter():
        if e.attrib.get("fill") == old:
            e.attrib["fill"] = new
        if "style" in e.attrib:
            e.attrib["style"] = e.attrib["style"].replace(old, new)


def _load_master() -> ET.Element:
    """Parse the master, returning the single top-level artwork group."""
    root = ET.parse(MASTER).getroot()
    artwork = next((c for c in root if c.attrib.get("id") == ARTWORK_GROUP_ID), None)
    if artwork is None:
        msg = f'Could not find a top-level group with id="{ARTWORK_GROUP_ID}" in {MASTER}'
        raise SystemExit(msg)
    _strip_foreign_attribs(artwork)
    return artwork


def _rasterize(svg_text: str, size: int) -> Image.Image:
    """Render an SVG (viewBox 0 0 32 32) to an exact size x size RGBA image."""
    png_bytes = cairosvg.svg2png(
        bytestring=svg_text.encode(), output_width=size, output_height=size
    )
    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def _fit_and_center(im: Image.Image, size: int) -> Image.Image:
    """Scale im to fit within size x size (preserving aspect) and center it, transparent padding."""
    scale = min(size / im.width, size / im.height)
    new_w, new_h = round(im.width * scale), round(im.height * scale)
    im = im.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(im, ((size - new_w) // 2, (size - new_h) // 2), im)
    return canvas


def _write_png(im: Image.Image, out_path: Path) -> None:
    im.save(out_path)
    print(f"wrote {out_path.relative_to(REPO_ROOT)}")


def generate_bleed_icons(artwork_markup: str) -> None:
    """icon-192 / icon-512: transparent bg, artwork trimmed and bled to fill the canvas."""
    svg = f'<svg xmlns="{SVG_NS}" viewBox="0 0 32 32">{artwork_markup}</svg>'
    trimmed = _rasterize(svg, BLEED_RENDER_SIZE)
    trimmed = trimmed.crop(trimmed.getbbox())
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png")):
        _write_png(_fit_and_center(trimmed, size), ICONS_DIR / name)


def generate_safe_zone_icons(artwork_markup: str) -> None:
    """maskable-512 / apple-touch-180: opaque bg edge-to-edge, artwork shrunk into the safe zone."""
    svg = (
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" fill="{BG_COLOR}"/>'
        f'<g transform="translate(16 16) scale({SAFE_ZONE_SCALE}) '
        f'translate(-16 -16)">{artwork_markup}</g>'
        "</svg>"
    )
    for size, name in ((512, "icon-maskable-512.png"), (180, "apple-touch-icon-180.png")):
        _write_png(_rasterize(svg, size), ICONS_DIR / name)


def generate_docs_logos(artwork: ET.Element) -> None:
    """docs/logo-{dark,light}.svg: cropped artwork, no bg, cloud recolored per background."""
    cloud = next((e for e in artwork.iter() if e.attrib.get("id") == "cloud"), None)
    if cloud is None:
        raise SystemExit(f'Could not find a group with id="cloud" in {MASTER}')
    dark_bg_cloud_color = _find_fill(cloud)
    if dark_bg_cloud_color is None:
        raise SystemExit(f'The id="cloud" group in {MASTER} has no fill color')

    markup = ET.tostring(artwork, encoding="unicode")
    dark_svg = f'<svg xmlns="{SVG_NS}" viewBox="{LOGO_CROP_VIEWBOX}">{markup}</svg>\n'

    light_artwork = ET.fromstring(ET.tostring(artwork))
    _recolor(light_artwork, dark_bg_cloud_color, LIGHT_BG_CLOUD_COLOR)
    light_markup = ET.tostring(light_artwork, encoding="unicode")
    light_svg = f'<svg xmlns="{SVG_NS}" viewBox="{LOGO_CROP_VIEWBOX}">{light_markup}</svg>\n'

    for svg, name in ((dark_svg, "logo-dark.svg"), (light_svg, "logo-light.svg")):
        out_path = DOCS_DIR / name
        out_path.write_text(svg)
        print(f"wrote {out_path.relative_to(REPO_ROOT)}")


def main() -> None:
    artwork = _load_master()
    artwork_markup = ET.tostring(artwork, encoding="unicode")
    generate_bleed_icons(artwork_markup)
    generate_safe_zone_icons(artwork_markup)
    generate_docs_logos(artwork)


if __name__ == "__main__":
    main()
