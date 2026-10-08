# -*- coding: utf-8 -*-
"""Text measuring / fitting helpers for the 100 x 80 mm label.

wkhtmltopdf cannot shrink text to fit a box, so the label values are fitted server side: the
width of a string is estimated from per-character widths measured in wkhtmltopdf 0.12.6 with
the label font stacks (Arial / Liberation Sans for Latin, Khmer OS Battambang for regular Khmer,
Noto Sans Khmer Bold for bold Khmer), then inflated by ``SAFETY``:

* Latin (Arial metrics): lowercase ~0.48 em, uppercase ~0.67 em, digits 0.556 em, bold +8 %;
* Khmer: the advance of each character measured in PDFs rendered by wkhtmltopdf (PyMuPDF), see
  ``KHMER_WIDTHS``: consonants 0.34 to 1.37 em, spacing vowel signs 0.29 to 0.69 em, Khmer
  digits ~0.76 em; vowels and signs above / below a letter and most subscript consonants (after
  COENG) take no width; a mark typed in an order the shaper cannot render gets a dotted circle.
  On real texts (provinces, names, addresses, notes, captions at 6 to 15 pt) the estimate is
  within a pixel of the rendered width.

A Khmer consonant is wider than a Latin letter, so character counts alone are misleading (a 40
character Khmer name does not fit where a 40 character Latin name does).
Character limits count visible characters: a Khmer syllable written with subscript consonants
and vowel signs (2 to 4 code points) counts once per base consonant.

Heights: wkhtmltopdf places the baseline of every line with the metrics of the first font of the
stack (Arial / Liberation Sans) whatever the font of the glyphs, and Khmer OS Battambang draws
far above and below them: up to 1.12 em above the baseline (vowels ើ ឹ) and 0.64 em below it
(subscript consonant + lower vowel, e.g. "ត្បូ"). :func:`text_box` gives the line height, top
padding and height of a box that keeps those glyphs inside it, so a box clipped with
``overflow: hidden`` never cuts Khmer subscripts and stacked lines never overprint each other.
"""
import math
import re
import unicodedata

from markupsafe import Markup, escape

MM_PER_PT = 25.4 / 72.0
PX_PER_PT = 96.0 / 72.0
MM_PER_PX = 25.4 / 96.0
KHMER_COENG = '្'
ELLIPSIS = '…'
SAFETY = 1.06  # estimated widths are inflated by 6 % before comparing with the box width

# Advance widths (em) of the Khmer characters in wkhtmltopdf 0.12.6 with the label font stacks,
# (regular: Khmer OS Battambang, bold: Noto Sans Khmer Bold), measured in PDFs with PyMuPDF.
# Vowel signs and signs not listed are drawn above / below their letter and take no room. Noto Sans
# Khmer Regular (used when Khmer OS Battambang is not installed) is narrower than both columns.
KHMER_WIDTHS = {
    'ក': (.682, .680), 'ខ': (.682, .680), 'គ': (.682, .680), 'ឃ': (1.025, .978), 'ង': (.682, .688), 'ច': (.682, .677),
    'ឆ': (.682, .693), 'ជ': (.682, .693), 'ឈ': (1.367, 1.295), 'ញ': (1.025, 1.000), 'ដ': (.682, .680), 'ឋ': (.682, .688),
    'ឌ': (.682, .677), 'ឍ': (1.025, .973), 'ណ': (1.367, 1.285), 'ត': (.682, .680), 'ថ': (.682, .693), 'ទ': (.682, .640),
    'ធ': (.682, .693), 'ន': (.682, .675), 'ប': (.682, .682), 'ផ': (.682, .693), 'ព': (.682, .672), 'ភ': (.682, .670),
    'ម': (.682, .682), 'យ': (1.025, .988), 'រ': (.343, .362), 'ល': (1.025, .970), 'វ': (.343, .395), 'ឝ': (.682, .682),
    'ឞ': (.682, .685), 'ស': (1.025, .973), 'ហ': (1.025, .973), 'ឡ': (1.025, .920), 'អ': (.682, .688), 'ឣ': (.682, .688),
    'ឤ': (1.025, .985), 'ឥ': (.682, .682), 'ឦ': (1.025, .958), 'ឧ': (.682, .680), 'ឨ': (.682, .680), 'ឩ': (.733, .810),
    'ឪ': (.682, .682), 'ឫ': (.682, .685), 'ឬ': (.682, .723), 'ឭ': (.682, .675), 'ឮ': (.682, .733), 'ឯ': (.682, .675),
    'ឰ': (.682, .672), 'ឱ': (.682, .677), 'ឲ': (.708, .615), 'ឳ': (.682, .667), '\u17b4': (.100, .307),
    '\u17b5': (.100, .307), 'ា': (.343, .292), 'ើ': (.343, .312), 'ឿ': (.685, .605), 'ៀ': (.685, .613),
    'េ': (.343, .312), 'ែ': (.343, .312), 'ៃ': (.343, .323), 'ោ': (.685, .605), 'ៅ': (.685, .605), 'ះ': (.465, .405),
    'ៈ': (.415, .282), '។': (.757, .585), '៕': (.902, .785), '៖': (.562, .477), 'ៗ': (.728, .585), '៘': (2.540, 2.112),
    '៙': (.757, .733), '៚': (1.915, 1.380), '៛': (.343, .383), 'ៜ': (.757, .728), '០': (.757, .672), '១': (.757, .672),
    '២': (.835, .835), '៣': (.882, .897), '៤': (.757, .685), '៥': (.757, .662), '៦': (.757, .672), '៧': (.880, .848),
    '៨': (.757, .672), '៩': (.757, .672),
}
# subscript consonants (COENG + letter) that take room; the others are drawn under their base letter
KHMER_SUBSCRIPT_WIDTHS = {
    'ឃ': (.343, .312), 'ឈ': (.343, .290), 'ឍ': (.343, .305), 'ប': (.343, .295), 'យ': (.343, .295), 'រ': (.343, .270),
    'ឞ': (.343, .685), 'ស': (.350, .302), 'ឡ': (1.028, .922),
}
KHMER_SHIFTERS = '៉៊'  # register shifters (MUUSIKATOAN, TRIISAP)
KHMER_DOTTED_CIRCLE = (.64, .65)  # drawn under a mark that cannot attach to a letter (misordered input)

# Vertical metrics (em) used by text_box(), measured on Liberation Sans and Khmer OS Battambang.
BASELINE_OFFSET = 0.3465  # baseline = line height / 2 + 0.3465 em (Liberation Sans: (0.905 - 0.212) / 2)
# script: (line height of one line, of several lines, height above the baseline, depth below the
# baseline of the last line when it is the only line / when there are several lines)
METRICS = {
    'latin': (1.15, 1.2, 0.95, 0.21, 0.21),
    # references, amounts and phone numbers: capitals and digits only
    'digits': (1.15, 1.2, 0.75, 0.1, 0.1),
    # Latin text after a fixed Khmer caption ("ចំណាំ Note: ...", "ទំនិញ Items ...") on the first line
    'prefixed': (1.15, 1.15, 1.12, 0.32, 0.21),
    # fixed Khmer captions of the label ("អ្នកទទួល", "ទំនិញ", "ចំនួន"...): no subscript + lower vowel
    'caption': (1.25, 1.4, 1.12, 0.37, 0.37),
    # any Khmer text: "ត្បូ" (subscript consonant + lower vowel) goes 0.64 em below the baseline,
    # "ើ" 1.2 em above it (Khmer OS Battambang; Noto Sans Khmer Bold stays within these)
    'khmer': (1.35, 1.45, 1.2, 0.66, 0.66),
}
KHMER_RUN_RE = re.compile('([\u1780-\u17ff\u19e0-\u19ff\u200b-\u200d]+)')


def has_khmer(text):
    """True when ``text`` contains Khmer letters (U+1780-U+17FF, U+19E0-U+19FF)."""
    return any('\u1780' <= char <= '\u17ff' or '\u19e0' <= char <= '\u19ff' for char in text or '')


def script_of(text, caption=False):
    """Metrics key of ``text`` for :func:`text_box`: 'khmer', 'caption' (fixed Khmer captions) or 'latin'."""
    if has_khmer(text):
        return 'caption' if caption else 'khmer'
    return 'latin'


def font_px(size_pt):
    """Pixel size of a font: Qt (wkhtmltopdf) rounds font sizes to whole pixels (8 pt -> 11 px)."""
    return int(size_pt * PX_PER_PT + 0.5)


def text_box(size_pt, lines=1, script='latin', clip=True):
    """``(line_height_px, height_px, padding_top_px)`` of a text box of ``lines`` lines at ``size_pt``.

    The top padding keeps the vowels above the first line inside the box and the height keeps the
    descent of the last line (Khmer subscripts and lower vowels) inside it, so a box clipped with
    ``overflow: hidden`` never cuts them. Whole CSS pixels: wkhtmltopdf truncates lengths.

    :param bool clip: the box clips its content; when it does not, the vowels of the first line
        may rise above it (into the descent of the line above) and no top padding is added
    """
    single, multi, above, below_single, below_multi = METRICS[script or 'latin']
    size_px = font_px(size_pt)
    lines = max(lines, 1)
    below = below_single if lines == 1 else below_multi
    line_px = int(size_px * (single if lines == 1 else multi) + 0.5)
    baseline = line_px / 2.0 + BASELINE_OFFSET * size_px
    padding = max(int(math.ceil(above * size_px - baseline - 0.3)), 0) if clip else 0
    height = padding + (lines - 1) * line_px + baseline + below * size_px
    return line_px, int(math.ceil(height - 0.05)), padding


def box_style(size_pt, lines=1, script='latin', max_height=False, clip=True):
    """Inline CSS of a text box (font size, line height, top padding, height) from :func:`text_box`."""
    line_px, height_px, padding = text_box(size_pt, lines, script, clip)
    return 'font-size: %spt; line-height: %dpx; padding-top: %dpx; %s: %dpx;' % (
        ('%.1f' % size_pt).rstrip('0').rstrip('.'), line_px, padding,
        'max-height' if max_height else 'height', height_px)


def kh_markup(text, bold=False):
    """HTML of ``text`` with its Khmer runs in ``<span class="o_kh_kh">`` (``o_kh_kb`` when bold).

    wkhtmltopdf only uses the first installed font of a CSS font stack: Khmer letters in a text
    set in Arial come from whatever font fontconfig substitutes (Khmer OS, wider than the
    estimates and without bold). The spans give the Khmer runs their own stack: Khmer OS
    Battambang for regular text, Noto Sans Khmer (with a real bold face) for bold text.
    """
    if not text:
        return ''
    css_class = 'o_kh_kb' if bold else 'o_kh_kh'
    parts = KHMER_RUN_RE.split(str(text))
    return Markup('').join(
        Markup('<span class="%s">%s</span>') % (css_class, part) if index % 2 else escape(part)
        for index, part in enumerate(parts) if part)


def px_to_mm(px):
    return px * MM_PER_PX


def collapse(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def char_width_em(char, bold=False):
    """Width of a non-Khmer character (Arial metrics), or of a Khmer letter written alone."""
    if char in KHMER_WIDTHS:
        return KHMER_WIDTHS[char][1 if bold else 0]
    code = ord(char)
    if 0x1780 <= code <= 0x17FF:
        return {'Lo': 1.025, 'Mc': .685, 'Mn': 0.0}.get(unicodedata.category(char), .76)  # not measured
    if 0x19E0 <= code <= 0x19FF:
        return 1.0  # Khmer symbols
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


def _khmer_mark_kind(char):
    """'shifter', 'vowel' or 'sign' for a Khmer combining character (COENG excluded), else None."""
    if char in KHMER_SHIFTERS:
        return 'shifter'
    code = ord(char)
    if 0x17B6 <= code <= 0x17C5:
        return 'vowel'
    if 0x17C6 <= code <= 0x17D3 or code == 0x17DD:
        return 'sign'
    return None


def text_width_em(text, bold=False):
    """Width of ``text`` in em: Arial metrics for Latin, measured Khmer widths (see KHMER_WIDTHS).

    Khmer is written in clusters: a base letter, at most two subscript consonants (COENG + letter,
    the subscript RO last), a register shifter, one vowel sign, then other signs. A mark that does
    not fit this order (no base before it, a second vowel, a vowel after a sign, a subscript
    after the vowel or after the subscript RO, as in "ស្រ្តី" or "កំុ" typed in the wrong
    order) is drawn by wkhtmltopdf on a dotted circle, which takes room.
    """
    column = 1 if bold else 0
    width = 0.0
    cluster = None  # marks of the Khmer cluster being written; None: no letter to attach a mark to
    chars = text or ''
    index = 0
    while index < len(chars):
        char = chars[index]
        index += 1
        if char in '\u200c\u200d':  # zero width (non) joiner: part of the cluster
            continue
        if char == KHMER_COENG:
            subscript = chars[index] if index < len(chars) and '\u1780' <= chars[index] <= '\u17a2' else ''
            index += bool(subscript)
            if (cluster is None or cluster['vowel'] or cluster['signs'] or cluster['ro']
                    or cluster['subscripts'] >= 2):
                width += KHMER_DOTTED_CIRCLE[column]
                cluster = _new_cluster()
            cluster['subscripts'] += 1
            cluster['ro'] = subscript == 'រ'
            width += KHMER_SUBSCRIPT_WIDTHS.get(subscript, (0.0, 0.0))[column]
            continue
        kind = _khmer_mark_kind(char)
        if kind:
            if cluster is None or (kind == 'sign' and char in cluster['signs']) or (kind != 'sign' and (
                    cluster['vowel'] or cluster['signs'] or (kind == 'shifter' and cluster['shifter']))):
                width += KHMER_DOTTED_CIRCLE[column]
                cluster = _new_cluster()
            if kind == 'sign':
                cluster['signs'].add(char)
            else:
                cluster[kind] = True
            width += char_width_em(char, bold=bold)
            continue
        # a letter starts a cluster; anything else (space, Latin, Khmer digit...) ends it
        cluster = _new_cluster() if '\u1780' <= char <= '\u17b3' else None
        width += char_width_em(char, bold=bold)
    return width


def _new_cluster():
    return {'subscripts': 0, 'ro': False, 'shifter': False, 'vowel': False, 'signs': set()}


def text_width_mm(text, size_pt, bold=False):
    """Estimated width of ``text`` at ``size_pt`` (rounded to whole pixels like Qt does)."""
    return text_width_em(text, bold=bold) * font_px(size_pt) * 0.75 * MM_PER_PT * SAFETY


def is_unit_start(text, index):
    """True when ``text[index]`` starts a new visible character (a "unit").

    Khmer combining vowels and signs, the COENG and the subscript consonant written after it are
    drawn on, under or around their base consonant: they belong to the unit of that consonant.
    """
    char = text[index]
    if char == KHMER_COENG or (index and text[index - 1] == KHMER_COENG):
        return False
    return unicodedata.category(char) not in ('Mn', 'Mc', 'Me', 'Cf')


def unit_starts(text):
    """Indexes of the visible characters of ``text`` (see :func:`is_unit_start`)."""
    return [index for index in range(len(text or '')) if is_unit_start(text, index)]


def count_units(text):
    """Number of visible characters: a Khmer syllable such as "ត្តា" counts as one character
    per base consonant, not one per code point (a Khmer word has 2 to 3 code points per letter)."""
    return len(unit_starts(text))


def _ellipsize(text):
    return text.rstrip(' ,;:-/(') + ELLIPSIS


def truncate(text, limit):
    """Collapse whitespace and cut ``text`` to ``limit`` visible characters with an ellipsis.

    The cut never splits a Khmer syllable (combining vowels / signs and subscript consonants
    written after a COENG stay with their base consonant) and prefers a nearby word boundary.
    """
    text = collapse(text)
    starts = unit_starts(text)
    if not limit or len(starts) <= limit:
        return text
    # keep limit - 1 characters and the ellipsis
    cut = starts[limit - 1]
    space = text.rfind(' ', 0, cut + 1)
    if space > starts[limit // 2] and space >= starts[max(limit - 11, 0)]:
        cut = space
    return _ellipsize(text[:cut])


def fit_width(text, width_mm, size_pt, bold=False, lines=1):
    """Truncate ``text`` (with an ellipsis) until it fits ``lines`` lines of ``width_mm``.

    The longest cut that fits is kept; it moves back to a word boundary only when the shorter text
    still fills 85 % of the room (a long name is not cut to its first two words while the box
    has room for most of the third one). For several lines a 15 % allowance is kept for the
    space lost when wrapping at word boundaries.
    """
    text = collapse(text)
    budget = width_mm * (lines if lines == 1 else lines * 0.85)
    if text_width_mm(text, size_pt, bold) <= budget:
        return text
    best = None
    for cut in reversed(unit_starts(text)[1:]):
        if text_width_mm(_ellipsize(text[:cut]), size_pt, bold) <= budget:
            best = cut
            break
    if best is None:
        return ELLIPSIS
    space = text.rfind(' ', 0, best + 1)
    if space > 0:
        at_word = _ellipsize(text[:space])
        if text_width_mm(at_word, size_pt, bold) >= 0.85 * budget:
            return at_word
    return _ellipsize(text[:best])


def wrap_lines(text, width_mm, size_pt, bold=False, max_lines=1, indent_mm=0.0, reserve_mm=0.0):
    """Break ``text`` into at most ``max_lines`` rows of ``width_mm``, like the browser would.

    Rows break at spaces; a word longer than a row (Khmer is written without spaces between
    words) is cut between two syllables. When the text does not fit, the last row ends with an
    ellipsis. The rows are printed with explicit line breaks, so a box never holds more lines
    than it was sized for (the top of a third Khmer line would show under a two-line box).

    :param float indent_mm: room taken on the first row (e.g. by a bold prefix)
    :param float reserve_mm: room kept free at the end of the last row (e.g. "+2 more")
    :return: ``(rows, truncated)``
    """
    rest = collapse(text)
    rows, separator = [], ''

    def room(index):
        return width_mm - (indent_mm if index == 0 else 0.0)

    while rest and len(rows) < max_lines:
        avail = room(len(rows))
        if text_width_mm(rest, size_pt, bold) <= avail:
            rows.append(rest)
            rest = ''
            break
        cut = None
        for index, char in enumerate(rest):
            if char == ' ':
                if text_width_mm(rest[:index], size_pt, bold) > avail:
                    break
                cut = index
        if cut:
            rows.append(rest[:cut])
            rest, separator = rest[cut + 1:], ' '
            continue
        # the first word is longer than the row: cut it between two syllables
        starts = unit_starts(rest)[1:] or [len(rest)]
        cut = starts[0]
        for start in starts:
            if text_width_mm(rest[:start], size_pt, bold) > avail:
                break
            cut = start
        rows.append(rest[:cut])
        rest, separator = rest[cut:].lstrip(' '), '' if rest[cut:cut + 1] != ' ' else ' '
    if not rows:
        return [], bool(rest)
    last = len(rows) - 1
    avail = room(last) - reserve_mm
    if rest:
        rows[last] = fit_width(rows[last] + separator + rest, avail, size_pt, bold)
        return rows, True
    if text_width_mm(rows[last], size_pt, bold) > avail:
        rows[last] = fit_width(rows[last], avail, size_pt, bold)
        return rows, True
    return rows, False


def fit_start(text, width_mm, size_pt, bold=False):
    """Cut ``text`` at its start ("…/OUT/00620") until it fits one line of ``width_mm``.

    Used for references whose end (the sequence number) identifies the document.
    """
    text = collapse(text)
    if text_width_mm(text, size_pt, bold) <= width_mm:
        return text
    for cut in unit_starts(text)[1:]:
        candidate = ELLIPSIS + text[cut:]
        if text_width_mm(candidate, size_pt, bold) <= width_mm:
            return candidate
    return text


def pick_size(text, width_mm, sizes, bold=False):
    """Largest font size of ``sizes`` (descending, in pt) at which ``text`` fits one line."""
    for size in sizes:
        if text_width_mm(text, size, bold) <= width_mm:
            return size
    return sizes[-1]
