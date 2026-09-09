"""The label-check response contract, as Pydantic models.

These classes are the ONE definition of the report's shape. They are handed
to the Gemini SDK as `response_schema`, so the model is constrained to emit
exactly this JSON rather than prose that a regex has to rescue — the old
pipeline stripped ``` fences by hand and returned `None` on a parse error,
which reached the frontend as a blank page with no explanation.

They are also what the API returns and what `legal/tests.py` pins, so a field
renamed here fails the test rather than silently reshaping the frontend's
input.

Why Pydantic and not a DRF serializer
-------------------------------------
DRF serializers describe what crosses OUR API boundary; these describe what
crosses the MODEL boundary, and the SDK consumes them directly. Making the
same objects do both would mean either teaching Gemini about DRF or hand-
maintaining two copies of one shape.

Status is deliberately two-valued
---------------------------------
PASS or FAIL, nothing else. The pipeline this replaces had five statuses
(OK / MISSING / MISMATCH / NEEDS_REVIEW / NOT_APPLICABLE) and the frontend
grouped three of them into "issue" anyway, so the extra states bought
ambiguity rather than information: a reviewer still had to read the note to
learn what to do. A rule either holds on this label or it does not; the
nuance belongs in `remarks`, which is prose written for a human.

Uncertainty has a home that is not a status: `ocr_verified` records whether
the deterministic OCR pass could corroborate the finding, so "FAIL, and the
OCR text agrees the declaration is absent" is distinguishable from "FAIL, but
OCR could not read that region" without inventing a third status.
"""
from typing import Literal

from pydantic import BaseModel, Field

#: The only two verdicts. See the module docstring for why there is no third.
RuleStatus = Literal['PASS', 'FAIL']


class Region(BaseModel):
    """A highlight box, as fractions of the label image.

    Fractions, not pixels: the browser renders a downscaled preview at
    whatever width its column happens to be, and a fraction survives every one
    of those resizes without the client needing to know the OCR image's
    dimensions.
    """

    x: float
    y: float
    width: float
    height: float


class RuleFinding(BaseModel):
    """One rule, judged against one label."""

    rule_id: str = Field(
        description='The rule identifier exactly as supplied in the prompt. '
                    'Never invent one, and never renumber.',
    )
    rule_name: str = Field(
        description='The short human name of the rule, echoed back verbatim.',
    )
    status: RuleStatus = Field(
        description="'PASS' when the label satisfies the rule, 'FAIL' when it "
                    'does not. If the label is unreadable in the relevant '
                    "region, answer 'FAIL' and say so in remarks.",
    )
    remarks: str = Field(
        description='One to three sentences a compliance reviewer can act on: '
                    'what was found on the label, where, and why that does or '
                    'does not satisfy the rule. Quote the label text you '
                    'relied on. Never answer with only "PASS" or "OK".',
    )

    evidence_text: str = Field(
        default='',
        description='The exact wording copied from the label that this '
                    'finding rests on — the declaration you read, character '
                    'for character. Leave EMPTY when the rule fails because '
                    'the declaration is absent: there is no wording to quote, '
                    'and inventing one would put a highlight on the wrong '
                    'part of the label.',
    )

    #: Set by our own cross-reference pass, never by the model — the schema
    #: handed to Gemini excludes it. None means the rule declared no tokens to
    #: corroborate, so OCR had no opinion.
    ocr_verified: bool | None = Field(
        default=None,
        description='Whether the deterministic OCR text corroborates this '
                    'finding. Populated server-side after the model responds.',
    )

    #: Where `evidence_text` sits on the label, as fractions of the image
    #: (0..1), one box per printed line. Found by `ocr.locate`, never supplied
    #: by the model — see that function for why that distinction matters.
    #: Empty is normal and means "nothing to point at".
    regions: list[Region] = Field(
        default_factory=list,
        description='Highlight boxes on the label image, normalised to the '
                    'image size. Populated server-side from the OCR word '
                    'boxes.',
    )


class LabelReport(BaseModel):
    """Every finding for one uploaded label — the model's whole answer."""

    findings: list[RuleFinding] = Field(
        description='Exactly one entry per rule supplied, in the order given.',
    )


class GeminiRuleFinding(BaseModel):
    """`RuleFinding` minus the fields we fill in ourselves.

    Handed to the SDK as the response schema. `ocr_verified` and `regions`
    are excluded deliberately. Asking the model whether OCR agrees with it
    invites a confident guess about a computation it cannot perform, and that
    answer would then be indistinguishable from our own. Coordinates are the
    same problem with a sharper edge: a plausible-looking box over the wrong
    part of the label is worse than no box, so the model quotes the wording it
    read (`evidence_text`) and WE find that wording among the OCR word boxes.
    """

    rule_id: str
    rule_name: str
    status: RuleStatus
    remarks: str
    evidence_text: str


class GeminiLabelReport(BaseModel):
    """The schema Gemini is constrained to."""

    findings: list[GeminiRuleFinding]
