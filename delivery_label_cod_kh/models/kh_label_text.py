# -*- coding: utf-8 -*-
"""Text measuring / fitting helpers for the 100 x 80 mm label.

wkhtmltopdf cannot shrink text to fit a box, so the label values are fitted server side: the
width of a string is estimated from per-character widths measured in wkhtmltopdf 0.12.6 with
the label font stack (Arial / Liberation Sans for Latin, Khmer OS Battambang for Khmer):

* Latin (Arial metrics): lowercase ~0.48 em, uppercase ~0.67 em, digits 0.556 em, bold +8 %;
* Khmer: consonants and independent vowels ~0.85 em, spacing vowel signs ~0.45 em, Khmer
  digits ~0.76 em; non-spacing signs and subscript consonants (after COENG) take no width.

A Khmer consonant is almost twice as wide as a Latin letter, so character counts alone are
misleading (a 40 character Khmer name does not fit where a 40 character Latin name does).
"""
import re
import unicodedata

MM_PER_PT = 25.4 / 72.0
KHMER_COENG = '្'
ELLIPSIS = '…'
SAFETY = 1.06  # estimated widths are inflated by 6 % before comparing with the box width


def collapse(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def char_width_em(char, bold=False, after_coeng=False):
    code = ord(char)
    if 0x1780 <= code <= 0x17FF:
        if after_coeng or char == KHMER_COENG:
            return 0.0
        if 0x17E0 <= code <= 0x17E9:
            return 0.76
        category = unicodedata.category(char)
        if category == 'Lo':
            return 0.85
        if category == 'Mc':
            return 0.45
        return 0.0
    if unicodedata.category(char) in ('Mn', 'Me', 'Cf'):
        return 0.0
    if char == ' ':
        width = 0.3
    elif char.isdigit():
        width = 0.556
    elif char.isupper():
        width = 0.667
    elif char.islower():
        width = 0.48
    elif code > 0x2000:
        width = 1.0  # symbols (☎ ✓ ⚠ ≈ …) come from DejaVu Sans, roughly square
    else:
        width = 0.33
    return width * 1.08 if bold else width


def text_width_em(text, bold=False):
    width = 0.0
    previous = ''
    for char in text or '':
        width += char_width_em(char, bold=bold, after_coeng=previous == KHMER_COENG)
        previous = char
    return width


def text_width_mm(text, size_pt, bold=False):
    return text_width_em(text, bold=bold) * size_pt * MM_PER_PT * SAFETY


def truncate(text, limit):
    """Collapse whitespace and cut ``text`` to ``limit`` characters with an ellipsis.

    The cut never splits a Khmer syllable (combining vowels / signs and subscript consonants
    written after a COENG stay with their base consonant) and prefers a nearby word boundary.
    """
    text = collapse(text)
    if not limit or len(text) <= limit:
        return text
    cut = limit - 1
    while cut > 0 and (unicodedata.category(text[cut]) in ('Mn', 'Mc') or text[cut - 1] == KHMER_COENG):
        cut -= 1
    space = text.rfind(' ', 0, cut + 1)
    if space > limit // 2 and space >= cut - 10:
        cut = space
    return text[:cut].rstrip(' ,;:-/(') + ELLIPSIS


def fit_width(text, width_mm, size_pt, bold=False, lines=1):
    """Truncate ``text`` (with an ellipsis) until it fits ``lines`` lines of ``width_mm``.

    For several lines a 15 % allowance is kept for the space lost when wrapping at word
    boundaries.
    """
    text = collapse(text)
    budget = width_mm * (lines if lines == 1 else lines * 0.85)
    if text_width_mm(text, size_pt, bold) <= budget:
        return text
    for limit in range(len(text) - 1, 0, -1):
        candidate = truncate(text, limit)
        if text_width_mm(candidate, size_pt, bold) <= budget:
            return candidate
    return ELLIPSIS


def pick_size(text, width_mm, sizes, bold=False):
    """Largest font size of ``sizes`` (descending, in pt) at which ``text`` fits one line."""
    for size in sizes:
        if text_width_mm(text, size, bold) <= width_mm:
            return size
    return sizes[-1]
