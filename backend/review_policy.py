from __future__ import annotations

import difflib

MAX_REVIEW_DIFF_LINES = 400


class ReviewPolicyError(ValueError):
    pass


def declared_difference_paths(
    difference: dict,
) -> tuple[str | None, str | None]:
    def declared_path(blob_name: str) -> str | None:
        if blob_name not in difference:
            return None
        blob = difference.get(blob_name)
        if not isinstance(blob, dict):
            raise ReviewPolicyError(
                "Wiki review is incomplete"
            )
        path = blob.get("path")
        if not isinstance(path, str) or not path:
            raise ReviewPolicyError(
                "Wiki review is incomplete"
            )
        return path

    before_path = declared_path("beforeBlob")
    after_path = declared_path("afterBlob")
    change_type = difference.get("changeType")
    valid = (
        change_type == "A"
        and before_path is None
        and after_path is not None
    ) or (
        change_type == "D"
        and before_path is not None
        and after_path is None
    ) or (
        change_type == "M"
        and before_path is not None
        and after_path is not None
    )
    if not valid:
        raise ReviewPolicyError(
            "Wiki review is incomplete"
        )
    return before_path, after_path


def is_markdown_change(
    before_path: str | None,
    after_path: str | None,
) -> bool:
    return any(
        path is not None and path.endswith(".md")
        for path in (before_path, after_path)
    )


def full_unified_diff(
    *,
    before: str,
    after: str,
    before_path: str | None,
    after_path: str | None,
    context_lines: int,
) -> list[str]:
    return list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{before_path or after_path}",
            tofile=f"b/{after_path or before_path}",
            lineterm="",
            n=context_lines,
        )
    )
