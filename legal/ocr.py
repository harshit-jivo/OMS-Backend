"""Deterministic text extraction — the first half of the hybrid pipeline.

Tesseract reads what is literally printed on the label. It is worse than
Gemini at understanding a layout and hopeless at judging a rule, but it has
the one property the model lacks: it does not invent. A string in this output
was on the page. That is what makes it worth running BEFORE the model and
worth keeping afterwards, and it is the whole basis of `service.cross_reference`.

Degrades, deliberately
----------------------
Tesseract is a native binary, not a Python package: `pip install pytesseract`
gives you a wrapper around an executable that may not be installed, and on
Windows usually is not. A missing binary must not take the feature down — it
costs the deterministic half, so the report says so on every finding instead
of the request 500ing. Same posture as `core.permissions.effective_keys`
degrading when a table is missing, and for the same reason: the half that
works should keep working.
"""
import logging
import os
import re
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


class OcrUnavailable(RuntimeError):
    """Tesseract is not installed or not reachable on this host."""


def _tesseract():
    """The configured `pytesseract` module, or raise `OcrUnavailable`.

    Imported lazily rather than at module scope so that importing `legal`
    (which Django does at boot, for every management command) never depends on
    an optional native dependency.
    """
    try:
        import pytesseract
    except ImportError as exc:  # package itself absent
        raise OcrUnavailable('pytesseract is not installed') from exc

    # Windows installs Tesseract outside PATH more often than not, so the
    # location is configurable. Empty (the default) means "trust PATH", which
    # is the normal case on the Linux host this deploys to.
    #
    # THE SETTING IS VERIFIED, NOT TRUSTED, and that is worth a paragraph.
    # `.env` is per-host (compose `env_file`), so a deployment file seeded
    # from a developer's is a normal thing to happen — and it carries
    # `TESSERACT_CMD=C:\Program Files\...`, which on the Linux host names
    # nothing. Assigning it would override a `tesseract` that IS installed and
    # IS on PATH (the image apt-installs it), and the whole deterministic half
    # of the pipeline would go quiet behind "OCR unavailable, AI review only"
    # — a banner that reads as "not installed" and sends you to fix the wrong
    # thing. A path that is not there cannot be what was meant, so PATH wins
    # and the log says why.
    binary = getattr(settings, 'TESSERACT_CMD', '') or ''
    if binary and not os.path.exists(binary):
        logger.warning(
            'TESSERACT_CMD points at %s, which does not exist on this host — '
            'ignoring it and looking for tesseract on PATH. Clear the '
            'setting in the .env for this host if the binary is on PATH here.',
            binary)
        binary = ''
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    return pytesseract


@dataclass(frozen=True)
class Word:
    """One word Tesseract read, and where it sat on the page.

    `line` groups words that share a printed line (Tesseract's own
    block/paragraph/line numbering). Highlighting uses it to draw one box per
    line instead of a single rectangle swallowing everything between the start
    of a wrapped phrase and its end.
    """

    text: str
    left: int
    top: int
    width: int
    height: int
    line: tuple


@dataclass(frozen=True)
class OcrRead:
    """Everything one Tesseract pass produced."""

    text: str
    words: tuple
    size: tuple  # (width, height) of the image the boxes refer to


#: Tesseract reports -1 for structural (non-text) rows; those are dropped.
#: Real words are kept whatever their confidence, and that is a correction of
#: a measured mistake rather than a default.
#:
#: This was 30, on the reasoning that a low-confidence word would draw a box
#: over blank card. Measured on a real Jivo label, that threshold discarded
#: the two most important declarations on the pack:
#:
#:     '1001506400541'  (the FSSAI licence number)  confidence 10
#:     'MRP'                                        confidence  9
#:
#: Both were read CORRECTLY. Tesseract's confidence tracks how small and
#: stylised the print is, not whether it got it right, and statutory
#: declarations are exactly the small stylised print at the bottom of a label.
#: Dropping them meant the FSSAI failure — the one a reviewer most needs
#: pointed at — could never be highlighted.
#:
#: Keeping low-confidence words is safe here because nothing is drawn from
#: this list directly: a box appears only where a word MATCHES a quotation
#: (see `locate`), so noise is filtered by relevance instead of by
#: confidence. Noise that also happens to match the quoted wording is not
#: noise.
MIN_WORD_CONFIDENCE = 0


def read(image) -> OcrRead:
    """Read `image` once, returning both its text and its word boxes.

    One pass, not two: `image_to_string` followed by `image_to_data` would run
    the whole engine twice over a 300-dpi page for data the second call
    already contains. The text is rebuilt from the word data, preserving line
    breaks, so the two halves cannot disagree about what was read.

    Raises `OcrUnavailable` when the binary is missing — callers decide
    whether that is fatal. `service.run_label_check` decides it is not.
    """
    pytesseract = _tesseract()
    try:
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as exc:  # noqa: BLE001 — pytesseract raises its own types
        # TesseractNotFoundError is the common one, but a corrupt install
        # surfaces as several different errors; they all mean the same thing
        # to a caller.
        raise OcrUnavailable(f'Tesseract failed: {exc}') from exc

    words = []
    lines = {}
    for index, raw in enumerate(data.get('text') or []):
        token = (raw or '').strip()
        if not token:
            continue
        key = (data['block_num'][index], data['par_num'][index],
               data['line_num'][index])
        lines.setdefault(key, []).append(token)

        try:
            confidence = float(data['conf'][index])
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence < MIN_WORD_CONFIDENCE:
            continue

        words.append(Word(
            text=token,
            left=int(data['left'][index]), top=int(data['top'][index]),
            width=int(data['width'][index]), height=int(data['height'][index]),
            line=key,
        ))

    text = '\n'.join(' '.join(tokens) for tokens in lines.values())
    return OcrRead(text=text, words=tuple(words), size=tuple(image.size))


def extract_text(image) -> str:
    """The text Tesseract reads from `image` (a PIL Image)."""
    return read(image).text


# ---------------------------------------------------------------------------
# Text matching
#
# Everything below compares a rule's `critical_tokens` against OCR output, so
# it has to survive OCR noise without becoming so loose that it matches
# anything. The normalisation is the minimum that does that.
# ---------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation Tesseract invents.

    OCR splits words across line breaks, doubles spaces around a fold in the
    packaging, and reads `.` where the print has a speck. Comparing raw
    strings would report a missing declaration for a label that plainly
    carries it — the failure mode that makes people stop trusting the tool.
    """
    lowered = (text or '').lower()
    # Keep letters, digits and spaces; everything else becomes a space so
    # "Best-Before" and "Best Before" compare equal.
    stripped = re.sub(r'[^a-z0-9]+', ' ', lowered)
    return re.sub(r'\s+', ' ', stripped).strip()


#: How close a window has to be to count as the token. 0.85 accepts one
#: dropped or swapped character in a six-letter word and two in a longer
#: phrase, which is the error rate real scans produce; it rejects genuinely
#: different words of the same length.
FUZZY_RATIO = 0.85


def contains(haystack: str, token: str, *, fuzzy: bool = True) -> bool:
    """True when `token` appears in `haystack`, allowing for OCR errors.

    The exact test comes first and answers almost every call. The fuzzy pass
    exists because of a failure observed on a real install: Tesseract read
    "FSSAI Lic. No." as "FSSA Lic. No,", dropping one character from the most
    important token on the label. An exact-only match reported the declaration
    as absent, and `service.cross_reference` would then overturn a correct
    PASS into a FAIL on a statutory rule — the precise false-failure this
    module's conservatism is meant to avoid.

    Windows start at word boundaries only. Sliding character by character
    would be several times the work for matches that are not words, and a
    token that begins mid-word is a coincidence rather than a reading.

    Short tokens are compared exactly (`len < 4`): at three characters, one
    substitution is a 0.67 ratio and two different words are indistinguishable,
    so fuzziness there buys false positives and nothing else.
    """
    needle = normalise(token)
    if not needle:
        return False

    hay = normalise(haystack)
    if needle in hay:
        return True
    if not fuzzy or len(needle) < 4:
        return False

    from difflib import SequenceMatcher

    # Compare whole words, not a fixed-width character window. A window sized
    # to the token drags in whatever follows it — "fssai" against "fssa " (the
    # trailing space) scores 0.80 and misses, while against the word "fssa" it
    # scores 0.89 and matches, which is the answer a reader would give.
    words = hay.split(' ')
    span = len(needle.split(' '))
    matcher = SequenceMatcher(None, needle, '', autojunk=False)

    for index in range(len(words) - span + 1):
        candidate = ' '.join(words[index:index + span])
        matcher.set_seq2(candidate)
        # `real_quick_ratio` is a cheap upper bound, so most candidates are
        # rejected on length alone before the real comparison runs.
        if (matcher.real_quick_ratio() >= FUZZY_RATIO
                and matcher.ratio() >= FUZZY_RATIO):
            return True
    return False


#: An FSSAI licence number is exactly 14 digits. This is checkable without a
#: model, and getting it wrong is a statutory problem rather than a cosmetic
#: one, so the pipeline checks it itself and tells the reviewer the number it
#: found. `\D` bounds rather than `\b` because OCR frequently glues the number
#: to the surrounding text ("Lic.No.10012345678901").
FSSAI_NUMBER = re.compile(r'(?<!\d)(\d{14})(?!\d)')


def fssai_numbers(text: str) -> list[str]:
    """Every 14-digit sequence in `text` — candidate FSSAI licence numbers."""
    digits_only = re.sub(r'[^0-9]', '', text or '')
    # Search the original first (correct spacing), then fall back to the
    # digits-only view, which rescues numbers OCR broke with a stray space.
    found = FSSAI_NUMBER.findall(text or '')
    if not found and len(digits_only) >= 14:
        found = FSSAI_NUMBER.findall(digits_only)
    # dict.fromkeys: de-duplicate while keeping the order they appear in.
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Locating a phrase on the page
#
# This is what makes highlighting honest. The model is asked to QUOTE the label
# wording a finding rests on; we then find that wording among the words
# Tesseract actually read, and the box we draw is Tesseract's own pixel
# measurement of it. The model never supplies a coordinate, so it cannot
# invent one — the worst it can do is quote text that is not on the label, and
# then nothing is found and nothing is drawn.
# ---------------------------------------------------------------------------

#: A located phrase has to be a better match than a passing resemblance, but
#: looser than `FUZZY_RATIO`: by the time we are locating, the finding is
#: already decided, and the cost of a slightly-off box is a highlight a few
#: words wide rather than a wrong verdict.
LOCATE_RATIO = 0.72


def _union(words) -> dict:
    """The smallest box containing every word in `words`."""
    left = min(word.left for word in words)
    top = min(word.top for word in words)
    right = max(word.left + word.width for word in words)
    bottom = max(word.top + word.height for word in words)
    return {'left': left, 'top': top, 'width': right - left,
            'height': bottom - top}


def _normalise_box(box: dict, size: tuple, pad: int = 2) -> dict:
    """A pixel box as fractions of the image, padded slightly.

    Fractions rather than pixels because the browser shows a downscaled
    preview at whatever width the column happens to be. A fraction survives
    every one of those resizes; a pixel coordinate would need the frontend to
    know the OCR image's dimensions and recompute on every layout change.

    The padding is cosmetic: a box drawn on the exact glyph bounds looks like
    it is clipping the text it points at.
    """
    width, height = size
    if not width or not height:
        return {'x': 0.0, 'y': 0.0, 'width': 0.0, 'height': 0.0}

    left = max(0, box['left'] - pad)
    top = max(0, box['top'] - pad)
    right = min(width, box['left'] + box['width'] + pad)
    bottom = min(height, box['top'] + box['height'] + pad)
    return {
        'x': round(left / width, 5),
        'y': round(top / height, 5),
        'width': round(max(0, right - left) / width, 5),
        'height': round(max(0, bottom - top) / height, 5),
    }


#: A candidate shorter than this (letters and digits, spaces removed) carries
#: no locating power — "of", "the", "by no". Measured as a WHOLE rather than
#: per word: judging each word separately threw away "USE BY", because "use"
#: and "by" are individually short while the pair names a declaration that is
#: printed exactly once. Coverage is what guards against a weak match; this
#: only screens out fragments too small to mean anything.
MIN_DISTINCTIVE_LENGTH = 5

#: Longest sub-phrase ladder to try. Beyond this the windows are so specific
#: that the whole-phrase attempt has already covered them.
MAX_SUBPHRASE_WORDS = 8

#: A sub-phrase is weaker evidence than a whole quotation, so it has to be a
#: near-exact reading rather than merely the closest thing on the page.
SUB_MIN_RATIO = 0.85

#: ...and it has to account for at least this share of the quotation. One word
#: out of seven is not "where that sentence is", it is a coincidence.
#:
#: 0.30 rather than a third, deliberately: "USE BY: (U) REFER TO BODY"
#: normalises to six tokens, so the two that actually name the declaration —
#: "use by" — come to 0.333 and a threshold of a third excluded them by a
#: rounding hair. Thresholds should not sit exactly on a real case.
SUB_MIN_COVERAGE = 0.30

#: Except for a long alphanumeric run — a licence, batch or registration
#: number. Those identify a place by themselves however short a share of the
#: sentence they are, and they are exactly what a failed rule quotes.
DECISIVE_TOKEN_LENGTH = 8
DECISIVE_TOKEN_RATIO = 0.9


def _distinctive(candidate: str) -> bool:
    """True when `candidate` is specific enough to locate something by.

    Anything containing a digit qualifies — a licence or batch number is the
    most locatable thing on a label. Otherwise the candidate as a whole has to
    be long enough that matching it means something.
    """
    if any(ch.isdigit() for ch in candidate):
        return True
    return len(candidate.replace(' ', '')) >= MIN_DISTINCTIVE_LENGTH


def _locate_run(target: str, words, size: tuple):
    """The best contiguous word run matching `target`, as (score, boxes).

    Returns `(0.0, [])` when nothing clears `LOCATE_RATIO`. The score is
    returned rather than discarded because `locate` has to CHOOSE between
    several candidate sub-phrases, and picking the first that merely passes
    put boxes on the wrong words — see the note there.
    """
    from difflib import SequenceMatcher

    span = len(target.split(' '))
    tokens = [normalise(word.text) for word in words]

    best_score = 0.0
    best_run = None
    matcher = SequenceMatcher(None, target, '', autojunk=False)

    # Try the phrase's own length first, then one word either side: OCR splits
    # and joins words often enough that an exact word count would miss.
    for width in {max(1, span - 1), span, span + 1}:
        for start in range(0, max(0, len(words) - width) + 1):
            candidate = ' '.join(tokens[start:start + width]).strip()
            if not candidate:
                continue
            matcher.set_seq2(candidate)
            if matcher.real_quick_ratio() < best_score:
                continue
            score = matcher.ratio()
            if score > best_score:
                best_score, best_run = score, words[start:start + width]

    if best_run is None or best_score < LOCATE_RATIO:
        return 0.0, []

    # One box per printed line, in reading order.
    by_line = {}
    for word in best_run:
        by_line.setdefault(word.line, []).append(word)

    return best_score, [
        _normalise_box(_union(line_words), size)
        for _, line_words in sorted(by_line.items())
    ]


def locate(phrase: str, words, size: tuple) -> list:
    """Where `phrase` appears on the page, as normalised boxes.

    Returns one box per printed LINE the phrase covers, so a quotation that
    wraps is highlighted as the two pieces a reader sees rather than as one
    rectangle spanning the paragraph between them. Empty when nothing can be
    found with confidence — the honest answer for a rule that failed BECAUSE
    the declaration is absent, and the caller then renders no highlight rather
    than an arbitrary one.

    The whole phrase is tried first. Failing that, contiguous sub-phrases are
    tried, because failures quote badly by their nature: a rule that failed
    over a wrong licence number quotes the WRONG number, and a rule about an
    expiry date may quote two declarations printed in different places.

    Why the sub-phrase bar is so much higher than the whole-phrase one
    ------------------------------------------------------------------
    The first version of this ladder accepted the first sub-phrase that beat
    `LOCATE_RATIO`, and drawn on a real label it put a box around the word
    "Image" in the artwork's internal-use footer — because the quotation
    "Images are for illustrative purposes only" was set in grey on maroon and
    OCR never read it, so the ladder fell all the way down to one weak word
    that happened to appear somewhere else entirely.

    A box on the wrong words is worse than no box: the reviewer cannot tell it
    is wrong, and it quietly discredits every other box on the page. So a
    sub-phrase must now be a NEAR-exact match (`SUB_MIN_RATIO`) AND account
    for a real share of the quotation (`SUB_MIN_COVERAGE`), and the best
    candidate wins rather than the first. One exception, for the case that
    matters most: a long alphanumeric run — a licence or batch number — is
    decisive on its own however small a share of the sentence it is.
    """
    target = normalise(phrase)
    if not target or not words:
        return []

    score, boxes = _locate_run(target, words, size)
    if boxes:
        return boxes

    tokens = target.split(' ')
    if len(tokens) < 2:
        return []

    best_rank = (0.0, 0.0)
    best_boxes: list = []

    for width in range(min(len(tokens), MAX_SUBPHRASE_WORDS) - 1, 0, -1):
        coverage = width / len(tokens)
        for start in range(len(tokens) - width + 1):
            candidate = ' '.join(tokens[start:start + width])
            if not _distinctive(candidate):
                continue

            # A long unbroken alphanumeric run identifies a place by itself.
            decisive = (
                width == 1
                and len(candidate) >= DECISIVE_TOKEN_LENGTH
                and any(ch.isdigit() for ch in candidate)
            )
            if coverage < SUB_MIN_COVERAGE and not decisive:
                continue

            score, boxes = _locate_run(candidate, words, size)
            if not boxes:
                continue
            floor = DECISIVE_TOKEN_RATIO if decisive else SUB_MIN_RATIO
            if score < floor:
                continue

            # Rank on how much of the quotation was matched first, then on how
            # well: a longer confirmed phrase is better evidence of place than
            # a shorter perfect one.
            rank = (coverage, score)
            if rank > best_rank:
                best_rank, best_boxes = rank, boxes

    return best_boxes
