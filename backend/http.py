from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qs

from .identity import validate_subject

_REQUEST_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$"
)
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{5,}\."
    r"[A-Za-z0-9_-]{5,}\."
    r"[A-Za-z0-9_-]{5,}"
)
_ARN = re.compile(
    r"arn:aws[a-z-]*:[^\s\"'<>]+",
    re.IGNORECASE,
)
_ROLE_MARKER = re.compile(
    r"(?:^|[-_./:=])(?:assumed-role|role)"
    r"(?=$|[-_./:=])",
    re.IGNORECASE,
)
_BEARER_MARKER = re.compile(
    r"bearer",
    re.IGNORECASE,
)
_JSON_CONTENT_TYPE = re.compile(
    r"^\s*application/json\s*"
    r"(?:;\s*charset\s*=\s*[A-Za-z0-9._-]+\s*)?$",
    re.IGNORECASE,
)


class HttpBoundaryError(Exception):
    pass


class AuthenticationError(HttpBoundaryError):
    pass


class InvalidRequestError(HttpBoundaryError):
    pass


class ServiceInitializationError(HttpBoundaryError):
    pass


class ServiceExecutionError(HttpBoundaryError):
    pass


class ServiceContractError(HttpBoundaryError):
    pass


def json_response(status_code: int, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ServiceContractError(
            "HTTP response body must be an object"
        )
    try:
        rendered = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise ServiceContractError(
            "HTTP response body is not serializable"
        ) from None
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": (
                "application/json; charset=utf-8"
            ),
            "Cache-Control": "no-store",
        },
        "body": rendered,
        "isBase64Encoded": False,
    }


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str = "unknown",
) -> dict:
    return json_response(
        status_code,
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
            "request_id": request_id,
        },
    )


def actor_sub(event: dict) -> str:
    try:
        sub = event["requestContext"]["authorizer"][
            "jwt"
        ]["claims"]["sub"]
    except (KeyError, TypeError):
        raise AuthenticationError(
            "Authenticated subject is unavailable"
        ) from None
    try:
        return validate_subject(sub)
    except ValueError:
        raise AuthenticationError(
            "Authenticated subject is unavailable"
        ) from None


def contains_sensitive_identifier(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return True
    return (
        _ACCOUNT_ID.search(value) is not None
        or _JWT.search(value) is not None
        or _ARN.search(value) is not None
        or _ROLE_MARKER.search(value) is not None
        or _BEARER_MARKER.search(value) is not None
    )


def request_id(event: object, context: object) -> str:
    candidates: list[object] = []
    if isinstance(event, dict):
        request_context = event.get("requestContext")
        if isinstance(request_context, dict):
            candidates.append(
                request_context.get("requestId")
            )
    candidates.append(
        getattr(context, "aws_request_id", None)
    )
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and _REQUEST_ID.fullmatch(candidate) is not None
            and not contains_sensitive_identifier(candidate)
        ):
            return candidate
    return "unknown"


def request_route(event: dict) -> tuple[str, str]:
    if not isinstance(event, dict):
        raise InvalidRequestError(
            "Invalid API Gateway event"
        )

    route_method = None
    route_path = None
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and route_key != "$default":
        parts = route_key.split(" ", 1)
        if len(parts) != 2:
            raise InvalidRequestError(
                "Invalid API Gateway route"
            )
        route_method, route_path = parts

    request_context = event.get("requestContext")
    context_method = None
    if isinstance(request_context, dict):
        http = request_context.get("http")
        if isinstance(http, dict):
            context_method = http.get("method")
    if context_method is None:
        context_method = event.get("httpMethod")

    raw_path = event.get("rawPath")
    method = context_method or route_method
    path = raw_path or route_path
    if (
        not isinstance(method, str)
        or not method
        or not isinstance(path, str)
        or not path.startswith("/")
    ):
        raise InvalidRequestError(
            "Invalid API Gateway route"
        )

    method = method.upper()
    if (
        route_method is not None
        and route_method.upper() != method
    ):
        raise InvalidRequestError(
            "Ambiguous API Gateway route"
        )
    if route_path is not None and route_path != path:
        raise InvalidRequestError(
            "Ambiguous API Gateway route"
        )
    return method, path


def query_parameters(event: dict) -> dict[str, str]:
    parameters = event.get("queryStringParameters")
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise InvalidRequestError(
                "Invalid query parameters"
            )
        parsed: dict[str, str] = {}
        for key, value in parameters.items():
            if not isinstance(key, str) or not isinstance(
                value,
                str,
            ):
                raise InvalidRequestError(
                    "Invalid query parameters"
                )
            parsed[key] = value
        return parsed

    raw_query = event.get("rawQueryString", "")
    if not isinstance(raw_query, str):
        raise InvalidRequestError(
            "Invalid query parameters"
        )
    return {
        key: values[0]
        for key, values in parse_qs(
            raw_query,
            keep_blank_values=True,
            strict_parsing=False,
        ).items()
        if values
    }


def json_body(event: dict) -> dict:
    body = event.get("body")
    if body in (None, ""):
        return {}
    if not isinstance(body, str):
        raise InvalidRequestError(
            "Invalid request body"
        )
    try:
        raw = (
            base64.b64decode(
                body,
                validate=True,
            ).decode("utf-8")
            if event.get("isBase64Encoded") is True
            else body
        )
        decoded = json.loads(raw)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise InvalidRequestError(
            "Invalid request body"
        ) from None
    if not isinstance(decoded, dict):
        raise InvalidRequestError(
            "Invalid request body"
        )
    return decoded


def parse_bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(
        value,
        str,
    ):
        raise InvalidRequestError(
            "Invalid integer parameter"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise InvalidRequestError(
            "Invalid integer parameter"
        ) from None
    if parsed < minimum or (
        maximum is not None and parsed > maximum
    ):
        raise InvalidRequestError(
            "Invalid integer parameter"
        )
    return parsed


def require_action_headers(event: dict) -> None:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        raise InvalidRequestError(
            "Required action headers are missing"
        )

    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(
            value,
            str,
        ):
            raise InvalidRequestError(
                "Invalid action headers"
            )
        normalized_key = key.lower()
        if normalized_key in normalized:
            raise InvalidRequestError(
                "Ambiguous action headers"
            )
        normalized[normalized_key] = value

    content_type = normalized.get("content-type")
    if (
        not isinstance(content_type, str)
        or _JSON_CONTENT_TYPE.fullmatch(
            content_type
        )
        is None
    ):
        raise InvalidRequestError(
            "Invalid Content-Type"
        )
    if normalized.get("x-demo-request") != "1":
        raise InvalidRequestError(
            "Invalid X-Demo-Request"
        )
