from __future__ import annotations

import unicodedata


def require_check_name(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 100
        or any(
            unicodedata.category(character) == "Cc"
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is invalid")
