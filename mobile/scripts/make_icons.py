#!/usr/bin/env python3
"""Generate the Android launcher icons of the ABJ app from mobile/resources/.

Inputs (1024x1024 PNG, produced together with the Odoo module icons):
  resources/icon.png             full icon, opaque
  resources/icon-foreground.png  the ABJ mark on a transparent background

Outputs (android/app/src/main/res/mipmap-<density>/):
  ic_launcher.png             legacy square icon with rounded corners (Android 7 launchers)
  ic_launcher_round.png       legacy round icon
  ic_launcher_foreground.png  adaptive icon foreground, 108 dp canvas (also the splash screen mark)
  ic_launcher_monochrome.png  Android 13+ themed icon: white silhouette of the foreground

The adaptive icon background is the colour ic_launcher_background (res/values/colors.xml) and the XML
descriptors live in mipmap-anydpi-v26/; neither is touched here.

Usage (from mobile/): python3 scripts/make_icons.py      (needs Pillow: pip install Pillow)
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / 'resources'
RES = ROOT / 'android' / 'app' / 'src' / 'main' / 'res'

DENSITIES = {'mdpi': 1.0, 'hdpi': 1.5, 'xhdpi': 2.0, 'xxhdpi': 3.0, 'xxxhdpi': 4.0}
LEGACY_DP = 48
ADAPTIVE_DP = 108
VISIBLE_DP = 72  # part of the adaptive canvas a launcher shows
# Launcher masks may cut everything outside a centred 66 dp circle: keep the mark inside 64 dp.
SAFE_DIAMETER_DP = 64
SLATE = (0x22, 0x34, 0x3A, 255)
SUPERSAMPLE = 4


def load(name):
    path = RESOURCES / name
    if not path.exists():
        sys.exit(f'Missing {path}: it is generated with the Odoo module icons (see mobile/README.md).')
    image = Image.open(path).convert('RGBA')
    if image.width != image.height:
        sys.exit(f'{path} must be square, got {image.width}x{image.height}')
    return image


def fit_into_safe_zone(foreground):
    """Shrink the mark (around the canvas centre) until it fits in the adaptive-icon safe circle."""
    size = foreground.width
    bbox = foreground.getchannel('A').getbbox()
    if not bbox:
        sys.exit('icon-foreground.png is fully transparent')
    left, top, right, bottom = bbox
    centre = size / 2
    radius = max(((x - centre) ** 2 + (y - centre) ** 2) ** 0.5 for x in (left, right) for y in (top, bottom))
    safe_radius = size * SAFE_DIAMETER_DP / ADAPTIVE_DP / 2
    if radius <= safe_radius:
        return foreground
    side = round(size * safe_radius / radius)
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(foreground.resize((side, side), Image.LANCZOS), ((size - side) // 2, (size - side) // 2))
    return canvas


def antialiased_mask(size, draw):
    big = Image.new('L', (size * SUPERSAMPLE, size * SUPERSAMPLE), 0)
    draw(ImageDraw.Draw(big), size * SUPERSAMPLE - 1)
    return big.resize((size, size), Image.LANCZOS)


def legacy_square(icon, px):
    image = icon.resize((px, px), Image.LANCZOS)
    image.putalpha(antialiased_mask(px, lambda d, s: d.rounded_rectangle((0, 0, s, s), radius=round(s * 0.2), fill=255)))
    return image


def legacy_round(foreground, px):
    # The visible 72 dp of the adaptive canvas, on a slate disc
    size = foreground.width
    margin = round(size * (ADAPTIVE_DP - VISIBLE_DP) / ADAPTIVE_DP / 2)
    visible = foreground.crop((margin, margin, size - margin, size - margin))
    disc = Image.new('RGBA', visible.size, SLATE)
    disc.alpha_composite(visible)
    image = disc.resize((px, px), Image.LANCZOS)
    image.putalpha(antialiased_mask(px, lambda d, s: d.ellipse((0, 0, s, s), fill=255)))
    return image


def monochrome(foreground, px):
    image = Image.new('RGBA', (px, px), (255, 255, 255, 0))
    image.putalpha(foreground.resize((px, px), Image.LANCZOS).getchannel('A'))
    return image


def main():
    icon = load('icon.png')
    foreground = fit_into_safe_zone(load('icon-foreground.png'))
    for density, scale in DENSITIES.items():
        folder = RES / f'mipmap-{density}'
        folder.mkdir(parents=True, exist_ok=True)
        legacy_px = round(LEGACY_DP * scale)
        adaptive_px = round(ADAPTIVE_DP * scale)
        outputs = {
            'ic_launcher.png': legacy_square(icon, legacy_px),
            'ic_launcher_round.png': legacy_round(foreground, legacy_px),
            'ic_launcher_foreground.png': foreground.resize((adaptive_px, adaptive_px), Image.LANCZOS),
            'ic_launcher_monochrome.png': monochrome(foreground, adaptive_px),
        }
        for name, image in outputs.items():
            image.save(folder / name, optimize=True)
        print(f'{folder.relative_to(ROOT)}: {", ".join(outputs)} ({legacy_px}/{adaptive_px} px)')


if __name__ == '__main__':
    main()
