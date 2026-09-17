import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ASSUMED_ROLE = re.compile(
    r"arn:aws[a-z-]*:sts::\d{12}:assumed-role/[^/\s]+/[^\s\"'<>]+"
)
AWS_ARN = re.compile(
    r"arn:aws[a-z-]*:[^:\s]*:[^:\s]*:\d{12}:[^\s\"'<>]+"
)
_EMAIL = re.compile(
    r"(?<![\w.+-])"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(?![\w.-])",
    re.IGNORECASE,
)
_ANY_AWS_ARN = re.compile(
    r"\barn:aws[a-z-]*:[^\s\"'<>]+",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY = re.compile(
    r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"(?![A-Z0-9])"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{5,}\."
    r"[A-Za-z0-9_-]{5,}\."
    r"[A-Za-z0-9_-]{5,}"
    r"(?![A-Za-z0-9_-])"
)
_BEARER = re.compile(
    r"\bbearer[ \t]+"
    r"(?=[A-Za-z0-9._~+/\-]{20,}=?(?:\s|$))"
    r"(?=[A-Za-z0-9._~+/\-]*[0-9._~+/\-])"
    r"[A-Za-z0-9._~+/\-]{20,}=?"
    r"(?![A-Za-z0-9._~+/\-])",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:^|[\s,{;])"
    r"[\"']?"
    r"(?:token|password|secret|api[_-]?key|"
    r"access[_-]?key)"
    r"[\"']?[ \t]*(?:=|:)[ \t]*\S+",
    re.IGNORECASE,
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?:^|[\s,{;])"
    r"[\"']?authorization[\"']?"
    r"[ \t]*(?:=|:)[ \t]*"
    r"(?=[A-Za-z0-9._~+/\-]{20,}=?(?:\s|$))"
    r"(?=[A-Za-z0-9._~+/\-]*[0-9._~+/\-])"
    r"[A-Za-z0-9._~+/\-]{20,}=?"
    r"(?![A-Za-z0-9._~+/\-])",
    re.IGNORECASE,
)

_REDACTED = "<redacted>"
_SENSITIVE_PATTERNS = (
    ("assumed_role", ASSUMED_ROLE),
    ("aws_arn", AWS_ARN),
    ("account_id", ACCOUNT_ID),
)
_FORBIDDEN_CONTROL_CATEGORIES = {
    "Cc",
    "Cf",
    "Cs",
    "Zl",
    "Zp",
}

# @secure_recommendation Redaction rule은 Query, Action, Collector,
# HTTP envelope와 snapshot의 현재 top-level response key를 바꾸지
# 못하게 allowlist contract로 고정한다.
PROTECTED_RESPONSE_KEYS = (
    "statusCode",
    "headers",
    "body",
    "isBase64Encoded",
    "ok",
    "error",
    "request_id",
    "region",
    "user_pool_id",
    "app_client_id",
    "cognito_domain",
    "callback_path",
    "subject",
    "display_label",
    "can_execute",
    "files",
    "pull_request_id",
    "lambda_started",
    "runtime_events",
    "done",
    "payload",
    "wiki_pull_request_id",
    "status",
    "title",
    "branch",
    "review_complete",
    "source_commit",
    "all_files",
    "repo",
    "kind",
    "default_branch",
    "clone_url",
    "last_modified",
    "branch_count",
    "tree",
    "head",
    "commits",
    "commit",
    "author",
    "date",
    "message",
    "parent",
    "wiki_head",
    "wiki_head_message",
    "wiki_head_author",
    "cursor",
    "docs",
    "operation_id",
    "path",
    "merge_commit_id",
    "merged_at",
    "merged_by",
    "merge_option",
    "accepted",
    "schema_version",
    "generated_at",
    "runs",
    "wiki",
    "bootstrap",
    "skills",
    "repos",
    "key",
    "bytes",
)


class RedactionError(ValueError):
    pass


def has_forbidden_control(value: str) -> bool:
    return any(
        unicodedata.category(character)
        in _FORBIDDEN_CONTROL_CATEGORIES
        for character in value
    )


def contains_sensitive_metadata(value: str) -> bool:
    return (
        "assumed-role" in value.lower()
        or any(
            pattern.search(value) is not None
            for pattern in (
                _EMAIL,
                ACCOUNT_ID,
                ASSUMED_ROLE,
                AWS_ARN,
                _ANY_AWS_ARN,
                _AWS_ACCESS_KEY,
                _JWT,
                _BEARER,
                _SECRET_ASSIGNMENT,
                _AUTHORIZATION_ASSIGNMENT,
            )
        )
    )


def _sensitive_spans(
    text: str,
) -> tuple[tuple[int, int, str], ...]:
    candidates: list[tuple[int, int, int, str]] = []
    for priority, (name, pattern) in enumerate(_SENSITIVE_PATTERNS):
        candidates.extend(
            (match.start(), match.end(), priority, name)
            for match in pattern.finditer(text)
        )
    candidates.sort(
        key=lambda candidate: (
            candidate[0],
            candidate[2],
            -(candidate[1] - candidate[0]),
        )
    )

    spans: list[tuple[int, int, str]] = []
    previous_end = 0
    for start, end, _, name in candidates:
        if start < previous_end:
            continue
        spans.append((start, end, name))
        previous_end = end
    return tuple(spans)


def _iter_json_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_json_strings(item)


@dataclass(frozen=True)
class Redactor:
    replacements: tuple[tuple[str, str], ...] = ()

    def _apply_custom_replacements(
        self,
        text: str,
    ) -> str:
        redacted = text
        for source, replacement in self.replacements:
            if not source:
                continue
            redacted = redacted.replace(source, replacement)
        return redacted

    def _redact_text(self, text: str) -> str:
        segments: list[tuple[str | None, str]] = []
        cursor = 0
        for start, end, name in _sensitive_spans(text):
            if cursor < start:
                segments.append((None, text[cursor:start]))
            segments.append((name, text[start:end]))
            cursor = end
        if cursor < len(text):
            segments.append((None, text[cursor:]))
        if not segments:
            segments.append((None, text))

        segments = [
            (
                name,
                self._apply_custom_replacements(segment),
            )
            for name, segment in segments
        ]
        for phase_name, pattern in _SENSITIVE_PATTERNS:
            segments = [
                (
                    None,
                    _REDACTED,
                )
                if name == phase_name
                else (
                    name,
                    pattern.sub(_REDACTED, segment),
                )
                for name, segment in segments
            ]
        return "".join(segment for _, segment in segments)

    def redact_text(self, text: str) -> str:
        return self._redact_text(text)

    def _redact_json_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [
                self._redact_json_value(item)
                for item in value
            ]
        if isinstance(value, dict):
            redacted: dict[str, object] = {}
            for key, item in value.items():
                redacted_key = self._redact_text(key)
                if redacted_key in redacted:
                    raise RedactionError(
                        "Redaction produced duplicate JSON keys"
                    )
                redacted[redacted_key] = self._redact_json_value(
                    item
                )
            return redacted
        return value

    def _redact_json_document(self, rendered: str) -> str:
        decoded = json.loads(rendered)
        redacted = self._redact_json_value(decoded)
        return json.dumps(redacted, ensure_ascii=False)

    def redact_obj(self, value: object) -> object:
        rendered = json.dumps(value, ensure_ascii=False)
        redacted = self._redact_json_document(rendered)
        self.assert_safe(redacted)
        return json.loads(redacted)

    def _assert_plain_safe(
        self,
        text: str,
    ) -> None:
        custom_value_remains = any(
            source and source in text
            for source, _ in self.replacements
        )
        built_in_value_remains = (
            ASSUMED_ROLE.search(text) is not None
            or AWS_ARN.search(text) is not None
            or ACCOUNT_ID.search(text) is not None
            or "assumed-role" in text
        )
        if custom_value_remains or built_in_value_remains:
            raise RedactionError("Sensitive value remains after redaction")

    def assert_safe(self, text: str) -> None:
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            self._assert_plain_safe(text)
            return

        if (
            ASSUMED_ROLE.search(text) is not None
            or AWS_ARN.search(text) is not None
            or ACCOUNT_ID.search(text) is not None
            or "assumed-role" in text
        ):
            raise RedactionError(
                "Sensitive value remains after redaction"
            )
        for string in _iter_json_strings(decoded):
            self._assert_plain_safe(string)

    @classmethod
    def from_secret_json(cls, payload: str) -> "Redactor":
        try:
            secret = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as error:
            raise RedactionError(
                "Redaction secret must contain valid JSON"
            ) from error
        if not isinstance(secret, dict):
            raise RedactionError(
                "Redaction secret must be a JSON object"
            )

        replacements = secret.get("replacements")
        if not isinstance(replacements, list):
            raise RedactionError(
                "Redaction secret must contain a replacements list"
            )
        return cls(validate_redaction_policy(replacements))


def validate_redaction_policy(
    replacements: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(replacements, (list, tuple)):
        raise RedactionError(
            "Redaction replacements must be a list or tuple"
        )

    parsed: list[tuple[str, str]] = []
    seen_sources: dict[str, str] = {}
    for pair in replacements:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
        ):
            raise RedactionError(
                "Each replacement must be a two-string pair"
            )
        source, replacement = pair
        if not source:
            raise RedactionError(
                "Replacement source must not be empty"
            )
        if not replacement:
            raise RedactionError(
                "Replacement mask must not be empty"
            )
        if (
            has_forbidden_control(source)
            or has_forbidden_control(replacement)
        ):
            raise RedactionError(
                "Redaction replacements must not contain controls"
            )
        if source == replacement:
            raise RedactionError(
                "Replacement source and mask must differ"
            )
        if source in seen_sources:
            if seen_sources[source] == replacement:
                raise RedactionError(
                    "Redaction replacement source is duplicated"
                )
            raise RedactionError(
                "Redaction replacement source has conflicting masks"
            )
        seen_sources[source] = replacement
        parsed.append((source, replacement))

    sources = tuple(source for source, _ in parsed)
    for _, replacement in parsed:
        if any(source in replacement for source in sources):
            raise RedactionError(
                "Redaction mask retains a replacement source"
            )
        if contains_sensitive_metadata(replacement):
            raise RedactionError(
                "Redaction mask contains sensitive metadata"
            )

    redactor = Redactor(tuple(parsed))
    try:
        for source in sources:
            sample = redactor.redact_obj(
                {
                    "sample": (
                        f"before:{source}:after"
                    )
                }
            )
            if (
                not isinstance(sample, dict)
                or tuple(sample) != ("sample",)
            ):
                raise RedactionError

        protected = {
            key: f"protected-value-{index}"
            for index, key in enumerate(
                PROTECTED_RESPONSE_KEYS
            )
        }
        safe = redactor.redact_obj(protected)
        if (
            not isinstance(safe, dict)
            or tuple(safe) != PROTECTED_RESPONSE_KEYS
        ):
            raise RedactionError
    except Exception:
        raise RedactionError(
            "Redaction replacements mutate protected responses"
        ) from None
    return tuple(parsed)
