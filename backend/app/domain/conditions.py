"""conditionType <-> the label Seller Central shows in its condition dropdown.

Only "New" is confirmed (from a Seller Central screenshot). The others follow Amazon's
conventional wording and must be calibrated against your live account before relying on them;
an unmatched label is safe: the check simply stays unresolved.
"""

CONDITION_LABELS = {
    "new_new": "New",
    "new_open_box": "New - Open Box",
    "new_oem": "New - OEM",
    "refurbished_refurbished": "Refurbished",
    "used_like_new": "Used - Like New",
    "used_very_good": "Used - Very Good",
    "used_good": "Used - Good",
    "used_acceptable": "Used - Acceptable",
    "collectible_like_new": "Collectible - Like New",
    "collectible_very_good": "Collectible - Very Good",
    "collectible_good": "Collectible - Good",
    "collectible_acceptable": "Collectible - Acceptable",
    "club_club": "Club",
}

_REVERSE = {" ".join(v.split()).casefold(): k for k, v in CONDITION_LABELS.items()}


def label_for(condition: str) -> str:
    return CONDITION_LABELS.get(condition, condition)


def condition_from_label(label: str | None) -> str | None:
    """None for the dropdown's placeholder ("Condition"), an empty value, or an unknown label."""
    return _REVERSE.get(" ".join((label or "").split()).casefold())
