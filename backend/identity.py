from __future__ import annotations

import re

_SUBJECT = re.compile(r"[A-Za-z0-9._:@-]{1,128}")


def validate_subject(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SUBJECT.fullmatch(value) is None
    ):
        raise ValueError("Authenticated subject is invalid")
    return value
