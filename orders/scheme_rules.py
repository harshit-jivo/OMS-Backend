import re

from django.apps import apps


PUNJAB_STATE_ALIASES = {"PB", "PUNJAB"}

def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)

def _normalized_text(*values):
    text = " ".join(str(value or "") for value in values)
    text = re.sub(r"\s*\+\s*", " + ", text)
    return re.sub(r"\s+", " ", text).strip().upper()

def get_party_state(card_code):
    if not card_code:
        return None

    for app_label, model_name in (("sap_sync", "Party"), ("orders", "Parties")):
        try:
            PartyModel = apps.get_model(app_label, model_name)
        except LookupError:
            continue

        state = (
            PartyModel.objects
            .filter(card_code=card_code)
            .exclude(state__isnull=True)
            .exclude(state__exact="")
            .values_list("state", flat=True)
            .first()
        )
        if state:
            return state

    return None

def get_party_state_code(card_code):
    state = get_party_state(card_code)
    if not state:
        return None

    normalized_state = _normalized_text(state)
    if not normalized_state:
        return None

    try:
        StateModel = apps.get_model("users", "State")
    except LookupError:
        StateModel = None

    if StateModel:
        state_row = (
            StateModel.objects
            .filter(code__iexact=str(state).strip())
            .values("code")
            .first()
        )
        if not state_row:
            state_row = (
                StateModel.objects
                .filter(name__iexact=str(state).strip())
                .values("code")
                .first()
            )
        if state_row and state_row.get("code"):
            return state_row["code"]

    if normalized_state in PUNJAB_STATE_ALIASES or "PUNJAB" in normalized_state:
        return "PB"

    return normalized_state

def get_ordered_quantity(value):
    if isinstance(value, dict):
        qty = _to_float(value.get("qty"), 0)
        # pcs = _to_float(value.get("pcs"), 0)
        # ltrs = _to_float(value.get("ltrs"), 0)
    else:
        qty = _to_float(getattr(value, "qty", None), 0)
        # pcs = _to_float(getattr(value, "pcs", None), 0)
        # ltrs = _to_float(getattr(value, "ltrs", None), 0)

    # if pcs > 0 and ltrs > 0:
    #     if abs((qty * pcs) - ltrs) <= 0.01:
    #         return qty
    #     if abs((ltrs * pcs) - qty) <= 0.01:
    #         return ltrs

    # if qty > 0:
    #     return qty

    if qty > 0:
        return qty

    return 0.0

def is_punjab_party(card_code):
    state = _normalized_text(get_party_state(card_code))
    state_code = _normalized_text(get_party_state_code(card_code))
    return (
        state in PUNJAB_STATE_ALIASES
        or "PUNJAB" in state
        or state_code in PUNJAB_STATE_ALIASES
        or "PUNJAB" in state_code
    )

def is_cold_press_1ltr_combo_10_set(*values):
    text = _normalized_text(*values)
    return bool(
        re.search(r"COLD\s*PRESS", text)
        and re.search(r"1\s*(?:LTR|LITRE|LITER|L)\s*\+\s*1\s*(?:LTR|LITRE|LITER|L)", text)
        and re.search(r"COMBO\s*10\s*SETS?", text)
    )

def is_combo_item_text(*values):
    text = _normalized_text(*values)
    return "+" in text or "COMBO" in text

def should_mirror_punjab_combo_scheme_qty(card_code, item_name=None, scheme_name=None):
    if not is_combo_item_text(item_name, scheme_name):
        return False

    if not is_punjab_party(card_code):
        return False

    return not is_cold_press_1ltr_combo_10_set(item_name, scheme_name)

def get_party_product_scheme(card_code, item_code, category=None):
    if not card_code or not item_code:
        return None

    PartyProductAssignment = apps.get_model("users", "PartyProductAssignment")
    queryset = PartyProductAssignment.objects.filter(
        card_code=card_code,
        item_code=item_code,
        is_active=True,
        scheme__isnull=False,
    ).select_related("scheme")

    if category:
        queryset = queryset.filter(category=category)

    assignment = queryset.order_by("id").first()
    return assignment.scheme if assignment else None
