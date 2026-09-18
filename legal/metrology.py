"""Dimensional compliance — the measurement stage, and the only one with no AI in it.

The rest of the pipeline asks a model to judge sentences against a picture.
Nothing here does. Every answer in this module is arithmetic against a
statutory table, and that is the point: "is this letter 2.5 mm tall" and "is
that circle 4 mm across" are questions a vision model answers confidently and
wrongly. The same instinct that keeps coordinates out of Gemini's response
schema (`schemas.GeminiRuleFinding`) keeps millimetres out of it too.

What is here and what is not
----------------------------
This is the logo half: PDP area, the veg/non-veg mark, the fortification
logo's size and its colours. Measuring the HEIGHT of printed letters is the
other half and is deliberately absent — it needs per-glyph ink extents off
either a vector PDF or a 500-dpi raster, and it is the part most likely to be
subtly wrong. The tables it will need (`FSSAI_HEIGHT`, `LM_HEIGHT`) are
written out below anyway, because they are transcription and transcribing them
twice is how two copies end up disagreeing.

Where the numbers come from
---------------------------
The FSSAI extract (PDP definition, letter heights, veg mark, fortification
logo) and the Legal Metrology (Packaged Commodities) Rules, 2011 extract
(letter heights for net quantity, MRP, expiry and consumer care). They
disagree about one thing and it is not a transcription error — see
`PDP_FRACTION`.

Units, stated once
------------------
Lengths are millimetres. Areas are square centimetres. Both appear in the
source tables in exactly those units, and converting to a single internal unit
would only mean converting back to compare against the table it came from.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The statutory tables, transcribed
# ---------------------------------------------------------------------------

#: Share of the package that counts as the Principal Display Panel.
#:
#: The two extracts disagree for irregular shapes: FSSAI says 20% of total
#: surface area, Legal Metrology says 40% of it. That is NOT reconciled here
#: and must not be — which one applies depends on which declaration is being
#: checked, so it is an input (`PackageSpec.regulation`) rather than a
#: decision this module is entitled to make on a reviewer's behalf.
PDP_FRACTION = {
    # 40% of height x width of the largest panel.
    'RECTANGULAR': 0.40,
    # 40% of height x average circumference. Covers cylindrical, nearly
    # cylindrical, round, nearly round, oval and nearly oval.
    'CYLINDRICAL': 0.40,
    'OTHER_FSSAI': 0.20,
    'OTHER_LEGAL_METROLOGY': 0.40,
}

#: A package at or below this capacity may carry its PDP on an affixed card or
#: tape instead of the package itself, so there is no panel area to compute.
SMALL_PACKAGE_CAPACITY_CM3 = 10.0

#: FSSAI minimum height of letters and numerals, by PDP area.
#: Rows are (inclusive upper bound in cm2, normal mm, blown/formed/moulded/
#: perforated mm); `None` is the open-ended top row. Present for the height
#: stage that is not built yet — see the module docstring.
FSSAI_HEIGHT = (
    (200.0, 1.0, 2.0),
    (500.0, 2.0, 4.0),
    (2500.0, 3.0, 5.0),
    (None, 6.0, 8.0),
)

#: Legal Metrology minimum height, for net quantity, retail sale price,
#: expiry/best before/use by, and consumer-care declarations.
#:
#: The top row really is 6.0 in both columns. Every other row in both tables
#: makes the blown/moulded requirement larger than the normal one; this one
#: does not, and it is transcribed as published rather than "corrected".
LM_HEIGHT = (
    (50.0, 1.0, 1.5),
    (100.0, 2.0, 3.0),
    (500.0, 2.5, 4.0),
    (2500.0, 4.0, 6.0),
    (None, 6.0, 6.0),
)

#: Width of a letter or numeral must be at least this share of its height.
WIDTH_TO_HEIGHT_RATIO = 1.0 / 3.0

#: ...except these, which are narrow by design and exempt in both extracts.
WIDTH_RATIO_EXEMPT = frozenset({'1', 'i', 'I', 'l'})

#: Minimum size of the vegetarian / non-vegetarian mark, by PDP area.
#: Rows are (inclusive upper bound in cm2, circle diameter, triangle side,
#: square side), all in mm.
VEG_MARK_MIN = (
    (100.0, 3.0, 2.5, 6.0),
    (500.0, 4.0, 3.5, 8.0),
    (2500.0, 6.0, 5.0, 12.0),
    (None, 8.0, 7.0, 16.0),
)

#: Index into a `VEG_MARK_MIN` row by the shape of the mark.
VEG_MARK_SHAPES = {'CIRCLE': 0, 'TRIANGLE': 1, 'SQUARE': 2}

#: What the measured size means, per shape, for a remark a human reads.
MARK_DIMENSION_NAME = {
    'CIRCLE': 'diameter',
    'TRIANGLE': 'side length',
    'SQUARE': 'side length',
}

#: The five prescribed sizes of the fortification logo, keyed by A (which
#: equals B in every published row — the logo's outer box is square).
#:
#: Values are the published artwork dimensions in mm:
#:   C, D, a       construction dimensions
#:   E, F, G       (width, height) of the 'F', the '+' and the swoosh
#:
#: Held verbatim rather than derived by scaling, because the published values
#: are rounded at each size (C at A=80 is 8.9, not 2 x 4.4) and a reviewer
#: checking artwork against the spec is checking the printed numbers, not our
#: multiplication.
FORT_LOGO_SIZES = {
    20.0: {'C': 2.2, 'D': 3.1, 'a': 0.8,
           'E': (7.27, 9.51), 'F': (5.67, 5.84), 'G': (16.98, 10.93)},
    40.0: {'C': 4.4, 'D': 6.3, 'a': 1.7,
           'E': (14.54, 19.03), 'F': (11.35, 11.68), 'G': (33.96, 21.87)},
    80.0: {'C': 8.9, 'D': 12.5, 'a': 3.4,
           'E': (29.08, 38.07), 'F': (22.70, 23.36), 'G': (67.92, 43.75)},
    160.0: {'C': 17.9, 'D': 25.4, 'a': 6.9,
            'E': (58.17, 76.14), 'F': (45.39, 46.72), 'G': (135.85, 87.50)},
    320.0: {'C': 35.6, 'D': 50.6, 'a': 13.8,
            'E': (116.35, 152.29), 'F': (90.77, 93.44), 'G': (275.25, 175.01)},
}

#: The smallest prescribed size, used as the basis when artwork is at a size
#: the table does not list. The drawing is proportional, so a logo at any
#: other size is checkable against this row multiplied through.
FORT_LOGO_BASE = 20.0

#: Fortification logo colours.
FORT_BLUE = '#0074C8'    # PANTONE 3005 C
FORT_BLACK = '#231F20'   # PANTONE BLACK

#: How far a colour on the artwork may sit from the specified one, as CIE76
#: Delta-E in Lab. Not the print industry's ~2: by the time we see it, the
#: colour has been through a PDF raster and a JPEG encode at quality 95, and a
#: tolerance tight enough for a press is tight enough to fail every correct
#: label. Overridable per rule via `ComplianceRule.params['tolerance_de']`.
COLOUR_TOLERANCE_DE = 10.0

#: The logo's outer box is square at every prescribed size, so a measured A
#: differing from B by more than this means the artwork has been stretched —
#: the one thing the published drawing explicitly warns against.
SQUARENESS_TOLERANCE_MM = 0.2

#: How close a measured A must be to a prescribed size to count as that size,
#: proportionally. 2% of 20 mm is 0.4 mm, about the precision anyone measures
#: artwork to.
SIZE_MATCH_TOLERANCE = 0.02


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------

def _number(value):
    """`value` as a float, or None for anything blank or unparseable.

    Form fields arrive as strings and an untouched one arrives as ''. None
    means "not supplied", which every check below treats as a reason to say so
    rather than a reason to assume a default — a PDP computed from a number
    nobody entered is worse than no PDP at all.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    # A negative or zero dimension is a typo, not a measurement.
    return number if number > 0 else None


def _choice(value, allowed, default=''):
    """`value` upper-cased if it is one of `allowed`, else `default`."""
    text = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    return text if text in allowed else default


def _flag(value):
    """Truthiness as an HTML form expresses it."""
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


SHAPES = {'RECTANGULAR', 'CYLINDRICAL', 'OTHER', 'SMALL'}
CONTAINERS = {'NORMAL', 'BLOWN'}
REGULATIONS = {'FSSAI', 'LEGAL_METROLOGY'}
VEG_MARKS = {'NONE', 'VEG', 'NON_VEG'}


@dataclass(frozen=True)
class PackageSpec:
    """What the reviewer tells us about the physical pack.

    None of this is on the artwork. A die-line does not say whether the pack
    is a bottle or a carton, which panel is the largest, or how it was
    moulded — and the artwork's page size is the printed sheet, which may
    carry bleed and several panels. So it is asked for, and `from_request`
    keeps every unanswered question as None instead of inventing an answer.
    """

    shape: str = ''
    container: str = 'NORMAL'
    regulation: str = 'FSSAI'

    #: RECTANGULAR: the largest panel.
    panel_height_mm: float | None = None
    panel_width_mm: float | None = None
    #: CYLINDRICAL and the shapes grouped with it.
    height_mm: float | None = None
    circumference_mm: float | None = None
    #: OTHER.
    surface_area_cm2: float | None = None
    #: Any shape, for the <= 10 cm3 exemption.
    capacity_cm3: float | None = None

    veg_mark: str = 'NONE'
    veg_mark_shape: str = 'CIRCLE'
    veg_mark_mm: float | None = None

    fortified: bool = False
    fort_a_mm: float | None = None
    fort_b_mm: float | None = None

    #: Where the panel dimensions came from when the reviewer did not type
    #: them: '' (they did), 'DECLARED' (read from the artwork's internal-use
    #: block as vector text), or 'DECLARED_OCR' (read from the same block, but
    #: off the OCR pass because the PDF was flattened).
    #:
    #: Reported on the finding, because the three are not equally trustworthy
    #: and only the reviewer can judge the difference.
    panel_source: str = ''

    #: True when the size was written on the artwork without a unit and taken
    #: as centimetres. See `parse_declared_size`.
    panel_unit_assumed: bool = False

    @classmethod
    def from_request(cls, data) -> 'PackageSpec':
        """Build a spec from POSTed form fields, tolerating absence."""
        get = data.get if hasattr(data, 'get') else (lambda *_: None)
        return cls(
            shape=_choice(get('package_shape'), SHAPES),
            container=_choice(get('container_type'), CONTAINERS, 'NORMAL'),
            regulation=_choice(get('regulation'), REGULATIONS, 'FSSAI'),
            panel_height_mm=_number(get('panel_height_mm')),
            panel_width_mm=_number(get('panel_width_mm')),
            height_mm=_number(get('height_mm')),
            circumference_mm=_number(get('circumference_mm')),
            surface_area_cm2=_number(get('surface_area_cm2')),
            capacity_cm3=_number(get('capacity_cm3')),
            veg_mark=_choice(get('veg_mark'), VEG_MARKS, 'NONE'),
            veg_mark_shape=_choice(get('veg_mark_shape'), VEG_MARK_SHAPES,
                                   'CIRCLE'),
            veg_mark_mm=_number(get('veg_mark_mm')),
            fortified=_flag(get('fortified')),
            fort_a_mm=_number(get('fort_a_mm')),
            fort_b_mm=_number(get('fort_b_mm')),
        )

    @property
    def configured(self) -> bool:
        """True when the reviewer filled the panel in at all.

        An untouched form means the dimensional checks were not asked for, and
        `service` skips them rather than failing a label for questions nobody
        answered. Distinct from "filled in wrongly", which does fail.
        """
        return bool(self.shape)

    def with_declared_panel(self, declared) -> 'PackageSpec':
        """A copy using the size declared on the artwork, if one is needed.

        `declared` is a `DeclaredSize` or None. Only fills a gap: a reviewer
        who typed the panel keeps what they typed.

        NOT the PDF page size
        ---------------------
        An earlier version of this defaulted to the page dimensions, and the
        project's own uploads show why that was wrong. The artwork PDF is a
        SHEET, not a trim: `Draft_Label.pdf` is a 64.88 x 100.94 mm page whose
        internal-use block declares the pack as 2.6 x 8 cm, and
        `Jivo_Cold_Pressed_...pdf` is a full A4 page. Taking the page as the
        panel overstated the first by a factor of three and the second by
        enough to cross the 200 cm2 boundary in `FSSAI_HEIGHT` — a wrong
        letter-height requirement, stated with confidence.

        So there is no page-size fallback. Either the artwork declares its
        size or the reviewer types it; otherwise the PDP is unknown and the
        finding says so, which is the honest answer and the safe one.
        """
        if self.shape != 'RECTANGULAR' or declared is None:
            return self
        if self.panel_height_mm or self.panel_width_mm:
            return self
        return replace(
            self,
            panel_width_mm=round(declared.width_mm, 2),
            panel_height_mm=round(declared.height_mm, 2),
            panel_source=declared.source,
            panel_unit_assumed=declared.unit_assumed,
        )

    def as_dict(self) -> dict:
        """The spec as stored on the report, so a reopened check explains itself."""
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Principal Display Panel
# ---------------------------------------------------------------------------

class MissingDimension(ValueError):
    """The spec does not carry what this computation needs.

    Raised rather than returning None so the caller has a sentence to put in
    front of a reviewer: every message here names what was blank.
    """


def pdp_area_cm2(spec: PackageSpec) -> float:
    """The Principal Display Panel area for `spec`, in square centimetres.

    Raises `MissingDimension` when the shape was not chosen or the dimensions
    it needs were not supplied. See `PDP_FRACTION` for why the irregular-shape
    fraction depends on which regulation is being applied.
    """
    if spec.shape == 'SMALL':
        raise MissingDimension(
            'A package of 10 cubic centimetres or less has no computed PDP '
            'area: the panel may be a card or tape affixed to the package.')

    if spec.shape == 'RECTANGULAR':
        if not (spec.panel_height_mm and spec.panel_width_mm):
            raise MissingDimension(
                'The height and width of the largest panel were not supplied.')
        # mm x mm -> cm2 is a division by 100, not by 10.
        return PDP_FRACTION['RECTANGULAR'] * (
            spec.panel_height_mm * spec.panel_width_mm) / 100.0

    if spec.shape == 'CYLINDRICAL':
        if not (spec.height_mm and spec.circumference_mm):
            raise MissingDimension(
                'The height and average circumference were not supplied.')
        return PDP_FRACTION['CYLINDRICAL'] * (
            spec.height_mm * spec.circumference_mm) / 100.0

    if spec.shape == 'OTHER':
        if not spec.surface_area_cm2:
            raise MissingDimension('The total surface area was not supplied.')
        return PDP_FRACTION[f'OTHER_{spec.regulation}'] * spec.surface_area_cm2

    raise MissingDimension('The package shape was not chosen.')


def is_small_package(spec: PackageSpec) -> bool:
    """True when the 10 cm3 card-or-tape exemption applies."""
    if spec.shape == 'SMALL':
        return True
    return bool(spec.capacity_cm3
                and spec.capacity_cm3 <= SMALL_PACKAGE_CAPACITY_CM3)


def _table_row(area_cm2: float, table):
    """The row of `table` covering `area_cm2`.

    Bounds are inclusive upper bounds, which is what "up to 200" and "above
    200 to 500" mean: 200 belongs to the first row, 200.01 to the second.
    """
    for ceiling, *values in table:
        if ceiling is None or area_cm2 <= ceiling:
            return values
    # Unreachable: every table ends in a None ceiling. Kept so a mistyped
    # table fails loudly here rather than returning None into arithmetic.
    raise MissingDimension(f'No table row covers a PDP of {area_cm2:.2f} cm2.')


def required_letter_height_mm(area_cm2: float, spec: PackageSpec,
                              table=FSSAI_HEIGHT) -> float:
    """Minimum height of letters and numerals for this PDP and container."""
    normal, blown = _table_row(area_cm2, table)
    return blown if spec.container == 'BLOWN' else normal


def required_mark_size_mm(area_cm2: float, mark_shape: str) -> float:
    """Minimum size of the veg/non-veg mark: diameter, or side length."""
    index = VEG_MARK_SHAPES.get(mark_shape)
    if index is None:
        raise MissingDimension(f'Unknown mark shape {mark_shape!r}.')
    return _table_row(area_cm2, VEG_MARK_MIN)[index]


# ---------------------------------------------------------------------------
# Fortification logo geometry
# ---------------------------------------------------------------------------

def nearest_prescribed_size(a_mm: float):
    """The prescribed logo size `a_mm` matches, or None.

    "Matches" means within `SIZE_MATCH_TOLERANCE` proportionally — artwork is
    measured off a drawing, not with a micrometer.
    """
    for size in sorted(FORT_LOGO_SIZES):
        if abs(a_mm - size) <= size * SIZE_MATCH_TOLERANCE:
            return size
    return None


def fortification_dimensions(a_mm: float) -> tuple:
    """The full A-G dimension set for a logo of overall size `a_mm`.

    Returns `(dimensions, prescribed)`. When `a_mm` is one of the five
    published sizes the published row is returned verbatim and `prescribed` is
    True. Otherwise the 20 mm row is scaled linearly and `prescribed` is
    False — the drawing is proportional, so scaling is the correct reading,
    but the caller has to be able to say which of the two it is showing.
    """
    size = nearest_prescribed_size(a_mm)
    if size is not None:
        return dict(FORT_LOGO_SIZES[size]), True

    factor = a_mm / FORT_LOGO_BASE
    scaled = {}
    for key, value in FORT_LOGO_SIZES[FORT_LOGO_BASE].items():
        if isinstance(value, tuple):
            scaled[key] = (round(value[0] * factor, 2),
                           round(value[1] * factor, 2))
        else:
            scaled[key] = round(value * factor, 2)
    return scaled, False


def format_dimensions(dims: dict) -> str:
    """The A-G set as one line a reviewer can check artwork against."""
    parts = [f'{key}={dims[key]}' for key in ('C', 'D', 'a')]
    parts.extend(f'{key}={dims[key][0]}x{dims[key][1]}'
                 for key in ('E', 'F', 'G'))
    return ', '.join(parts) + ' (mm)'


# ---------------------------------------------------------------------------
# Colour
#
# Everything below measures colour off the WHOLE artwork, not off the logo:
# nothing here locates the logo, because locating it reliably is shape
# detection and shape detection is what the manual dimension inputs exist to
# avoid. So the question answered is "does the specified colour appear on this
# label", which is weaker than "the logo is printed in it" — and every remark
# generated from it says so. A check that overstates itself is worse than one
# that does less and admits it.
# ---------------------------------------------------------------------------

def hex_to_rgb(value: str) -> tuple:
    """'#0074C8' -> (0, 116, 200)."""
    text = value.lstrip('#')
    return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))


def rgb_to_hex(rgb) -> str:
    """(0, 116, 200) -> '#0074C8'."""
    return '#%02X%02X%02X' % tuple(int(round(channel)) for channel in rgb)


def _srgb_to_lab(rgb):
    """sRGB (0-255) to CIE Lab, D65. Vectorised over any leading shape.

    Lab rather than RGB distance because RGB distance does not correspond to
    how different two colours look, and the whole question here is whether a
    printed blue reads as the specified blue.
    """
    import numpy as np

    channels = np.asarray(rgb, dtype=float) / 255.0
    linear = np.where(channels <= 0.04045, channels / 12.92,
                      ((channels + 0.055) / 1.055) ** 2.4)

    matrix = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ])
    xyz = linear @ matrix.T
    xyz = xyz / np.array([0.95047, 1.0, 1.08883])

    delta = 6.0 / 29.0
    f = np.where(xyz > delta ** 3, np.cbrt(xyz),
                 xyz / (3 * delta * delta) + 4.0 / 29.0)
    return np.stack([
        116.0 * f[..., 1] - 16.0,
        500.0 * (f[..., 0] - f[..., 1]),
        200.0 * (f[..., 1] - f[..., 2]),
    ], axis=-1)


#: Longest edge the artwork is scaled to before colour is measured. The
#: question is "is this colour present", which survives downscaling; running a
#: Lab conversion over a full 500-dpi render would be tens of millions of
#: pixels to answer it.
COLOUR_SAMPLE_MAX_EDGE = 800

#: Pixels within tolerance have to be at least this share of the artwork to
#: count as present. A handful of pixels at the right hue is JPEG ringing
#: along an edge, not an inked area. 0.0002 is about 100 pixels of a 640x800
#: sample, which a 20 mm logo's strokes clear comfortably.
COLOUR_MIN_SHARE = 0.0002


@dataclass(frozen=True)
class ColourMatch:
    """How close the artwork gets to one specified colour."""

    target_hex: str
    #: Smallest Delta-E between the target and any pixel on the artwork.
    delta_e: float
    #: The hex of that closest pixel — what to tell the reviewer was found.
    closest_hex: str
    #: Share of the artwork within tolerance of the target.
    share: float
    tolerance: float

    @property
    def present(self) -> bool:
        """True when the colour is on the artwork in a printable quantity."""
        return self.delta_e <= self.tolerance and self.share >= COLOUR_MIN_SHARE


def measure_colour(image, target_hex: str,
                   tolerance: float = COLOUR_TOLERANCE_DE) -> ColourMatch:
    """How closely `image` carries `target_hex`, as a `ColourMatch`.

    Every pixel is compared, on a downscaled copy. Quantising to a palette
    first would be faster and wrong for exactly the case that matters:
    median-cut allocates palette entries by area, so a small logo's blue gets
    merged into whatever large region is nearest it, and the measured Delta-E
    becomes the distance to that region instead.
    """
    import numpy as np
    from PIL import Image

    sample = image.convert('RGB')
    if max(sample.size) > COLOUR_SAMPLE_MAX_EDGE:
        sample = sample.copy()
        # NEAREST, not LANCZOS: a smooth resample invents intermediate colours
        # along every edge, and one of those inventions landing on the target
        # would be a colour that was never printed.
        sample.thumbnail((COLOUR_SAMPLE_MAX_EDGE, COLOUR_SAMPLE_MAX_EDGE),
                         Image.NEAREST)

    pixels = np.asarray(sample, dtype=np.uint8)
    lab = _srgb_to_lab(pixels)
    target = _srgb_to_lab(np.array(hex_to_rgb(target_hex), dtype=float))

    distance = np.linalg.norm(lab - target, axis=-1)
    index = np.unravel_index(int(np.argmin(distance)), distance.shape)

    return ColourMatch(
        target_hex=target_hex.upper(),
        delta_e=float(distance[index]),
        closest_hex=rgb_to_hex(pixels[index]),
        share=float((distance <= tolerance).mean()),
        tolerance=tolerance,
    )


# ---------------------------------------------------------------------------
# Artwork page size
# ---------------------------------------------------------------------------

#: The pack size as the artwork's own internal-use block writes it:
#:
#:     Size(cm): 2.6 x 8
#:     Size: 13 x 4.2
#:
#: Tolerant of what OCR does to that line — a missing colon, a lost bracket,
#: 'X' or '*' or the multiplication sign for the 'x', extra spacing — because
#: the flattened artwork in this project has no vector text and OCR is the
#: only way to read it at all.
DECLARED_SIZE = re.compile(
    r'size\s*'                       # the label
    r'(?:\(\s*(?P<unit>cm|mm|in)\s*\)\s*)?'   # optional "(cm)"
    r'[:\-]?\s*'                     # optional colon, which OCR drops
    r'(?P<width>\d+(?:\.\d+)?)\s*'
    r'[x×*]\s*'                 # x, the multiplication sign, or *
    r'(?P<height>\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

#: Multipliers onto millimetres.
DECLARED_UNITS = {'cm': 10.0, 'mm': 1.0, 'in': 25.4}

#: The unit assumed when the block writes a bare "Size: 13 x 4.2".
#:
#: Centimetres, because the labelled variant on this project's own artwork is
#: "Size(cm)", and because the alternative is absurd on its face: a 13 x 4.2
#: MILLIMETRE pack is smaller than the licence number printed on it. The
#: assumption is recorded (`unit_assumed`) and stated on the finding rather
#: than made quietly.
DECLARED_DEFAULT_UNIT = 'cm'

#: Words that mean a "Size:" line is about something else. The nutrition panel
#: carries "Serving Size: 15 ml/13.65 g (1 Tablespoon)", which the pattern
#: above will not match — but "Serving Size: 2 x 15 ml" would, and reading a
#: serving as a pack is exactly the kind of confident wrong answer worth
#: spending a lookbehind to avoid.
DECLARED_EXCLUDE = ('serving', 'portion', 'font', 'type', 'image', 'logo')

#: How far back to look for one of those words.
DECLARED_EXCLUDE_WINDOW = 24


@dataclass(frozen=True)
class DeclaredSize:
    """A pack size read off the artwork's own internal-use block."""

    width_mm: float
    height_mm: float
    #: True when the block gave no unit and centimetres were assumed.
    unit_assumed: bool
    #: 'DECLARED' from vector text, 'DECLARED_OCR' from the OCR pass.
    source: str


def parse_declared_size(text: str, source: str = 'DECLARED'):
    """The pack size declared in `text`, as a `DeclaredSize`, or None.

    `text` is the artwork's text — from PyMuPDF for a vector PDF, or from the
    OCR pass for a flattened one. The internal-use block at the foot of the
    artwork carries the pack's real dimensions, which is a far better answer
    than any default we could compute and the only one the artwork itself
    vouches for.

    When several candidates appear, the one inside the internal-use block
    wins; otherwise the first that is not plainly about something else.
    """
    if not text:
        return None

    lowered = text.lower()
    # The block is at the foot of the artwork, so a match after this marker is
    # the pack size rather than something in the nutrition panel.
    marker = lowered.find('internal use')

    best = None
    for match in DECLARED_SIZE.finditer(text):
        start = match.start()
        preceding = lowered[max(0, start - DECLARED_EXCLUDE_WINDOW):start]
        if any(word in preceding for word in DECLARED_EXCLUDE):
            continue

        unit = (match.group('unit') or '').lower()
        scale = DECLARED_UNITS.get(unit or DECLARED_DEFAULT_UNIT)
        candidate = DeclaredSize(
            width_mm=float(match.group('width')) * scale,
            height_mm=float(match.group('height')) * scale,
            unit_assumed=not unit,
            source=source,
        )
        # A size of zero is a misread, not a pack.
        if not (candidate.width_mm and candidate.height_mm):
            continue
        # Inside the internal-use block beats anything before it, and the
        # first such match beats later ones.
        if marker >= 0 and start > marker:
            return candidate
        if best is None:
            best = candidate

    return best


def artwork_declared_size(path: str):
    """The pack size declared in a PDF's vector text, or None.

    Vector text only: exact, and free — the text is already in the file. A
    flattened artwork has none, and `service` then retries against the OCR
    text rather than giving up.
    """
    import os

    if os.path.splitext(path)[1].lower() != '.pdf':
        return None
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            if not document.page_count:
                return None
            return parse_declared_size(document[0].get_text(), 'DECLARED')
    except Exception:  # noqa: BLE001 — PyMuPDF raises its own types
        logger.exception('Could not read the declared size of %s', path)
        return None
