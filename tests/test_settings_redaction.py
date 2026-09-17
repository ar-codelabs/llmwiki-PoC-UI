import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

# ⚠️ 저장소 배치를 전제하지 않는다. 단독 번들에서도 이 파일 위치가 기준이다.
DEMO_UI = Path(__file__).resolve().parents[1]
REPO_ROOT = DEMO_UI

sys.path.insert(0, str(DEMO_UI))

from backend.redaction import RedactionError, Redactor  # noqa: E402
from backend.settings import Settings  # noqa: E402


def _complete_environment(**overrides):
    environment = {
        "AWS_REGION": "ap-northeast-2",
        "DEMO_SOURCE_REPOSITORIES": (
            "shaka-player,mediamtx,saleor"
        ),
        "DEMO_WIKI_REPO": "apps-knowledge",
        "DEMO_LAMBDA_LOG_GROUP": "lambda-log",
        "DEMO_RUNTIME_LOG_GROUP": "runtime-log",
        "DEMO_SKILLS_BUCKET": "skills-bucket",
        "DEMO_SNAPSHOT_BUCKET": "snapshot-bucket",
        "DEMO_COLLECTOR_FUNCTION": "collector-function",
        "DEMO_REDACTION_SECRET_ARN": "redaction-secret",
        "DEMO_USER_POOL_ID": "pool",
        "DEMO_APP_CLIENT_ID": "client",
        "DEMO_COGNITO_DOMAIN": "domain",
        "DEMO_CALLBACK_PATH": "/auth/callback",
    }
    environment.update(overrides)
    return environment


def test_settings_requires_runtime_values():
    with pytest.raises(ValueError, match="DEMO_WIKI_REPO"):
        Settings.from_env({})


def test_settings_parses_exact_source_repository_tuple():
    settings = Settings.from_env(_complete_environment())

    assert settings.source_repositories == (
        "shaka-player",
        "mediamtx",
        "saleor",
    )
    assert settings.wiki_repo == "apps-knowledge"


@pytest.mark.parametrize(
    ("sources", "wiki"),
    [
        ("shaka-player,shaka-player,saleor", "apps-knowledge"),
        ("shaka-player,apps-knowledge,saleor", "apps-knowledge"),
        ("shaka-player,bad repo,saleor", "apps-knowledge"),
        ("shaka-player,mediamtx,saleor", "apps-knowledge.git"),
        ("shaka-player,,saleor", "apps-knowledge"),
    ],
    ids=[
        "duplicate",
        "wiki-overlap",
        "invalid-source",
        "git-suffix-wiki",
        "empty-source",
    ],
)
def test_settings_rejects_invalid_exact_repository_configuration(
    sources,
    wiki,
):
    with pytest.raises(ValueError):
        Settings.from_env(
            _complete_environment(
                DEMO_SOURCE_REPOSITORIES=sources,
                DEMO_WIKI_REPO=wiki,
            )
        )


def test_local_config_is_not_tracked():
    """`local.json` 은 계정 좌표를 담으므로 커밋 대상이 아니다.

    ⚠️ 이전 판은 이 파일이 tracked 라는 전제로 값까지 검사했다. 저장소가 고객 전달
    대상이 되면서 그 전제가 뒤집혔다. 이제 검사할 것은 **값이 아니라 부재**다.
    형식은 `local.json.example` 이 보여 준다.
    """
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--error-unmatch",
            "demo-ui/local.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert tracked.returncode != 0, (
        "demo-ui/local.json 이 tracked 다. 계정 ID·프로파일·principal 이 "
        "전달본에 들어간다"
    )


def test_local_config_example_carries_no_account_coordinates():
    """템플릿에는 실제 계정 좌표가 없어야 한다."""
    text = (DEMO_UI / "local.json.example").read_text(encoding="utf-8")
    example = json.loads(text)

    assert re.search(r"\b\d{12}\b", text) is None, "12자리 계정 ID 가 남아 있다"
    for key in ("account", "profile", "region", "runtime_id", "wiki_repo"):
        value = example.get(key)
        assert isinstance(value, str) and value.startswith("<"), (
            f"{key} 가 자리표시자가 아니다"
        )
    assert "repo_prefix" not in example


def test_redactor_removes_account_arns_and_assumed_roles():
    redactor = Redactor((("person-alias", "<user>"),))
    raw = {
        "principal": (
            "arn:aws:sts::123456789012:"
            "assumed-role/Admin/person-alias"
        ),
        "repo": "apps-knowledge",
    }
    safe = redactor.redact_obj(raw)
    rendered = json.dumps(safe)
    assert "123456789012" not in rendered
    assert "assumed-role" not in rendered
    assert "person-alias" not in rendered
    assert safe["repo"] == "apps-knowledge"


def test_redactor_rejects_remaining_sensitive_value():
    redactor = Redactor()
    with pytest.raises(RedactionError):
        redactor.assert_safe("account 123456789012")


def test_redactor_loads_custom_replacements_from_secret_json():
    redactor = Redactor.from_secret_json(
        '{"replacements":[["internal-name","<name>"]]}'
    )
    assert redactor.redact_text("internal-name") == "<name>"


@pytest.mark.parametrize(
    "source",
    [
        'private"alias',
        r"private\alias",
        "private\nalias",
    ],
    ids=["quote", "backslash", "newline"],
)
def test_redactor_matches_json_escaped_custom_source(source):
    redactor = Redactor(((source, "<safe>"),))

    safe = redactor.redact_obj({"value": f"before:{source}:after"})

    assert safe == {"value": "before:<safe>:after"}


@pytest.mark.parametrize(
    "replacement",
    [
        'safe"value',
        r"safe\value",
        "safe\nvalue",
    ],
    ids=["quote", "backslash", "newline"],
)
def test_redactor_json_escapes_custom_replacement(replacement):
    redactor = Redactor((("private-alias", replacement),))

    safe = redactor.redact_obj({"value": "private-alias"})

    assert safe == {"value": replacement}


@pytest.mark.parametrize(
    "replacements",
    [
        (("assumed-role", "<kind>"),),
        (
            ("sts", "service:split"),
            ("assumed-role", "<kind>"),
        ),
    ],
    ids=["resource-token", "service-and-resource-tokens"],
)
def test_redactor_redacts_entire_arn_after_custom_overlap(replacements):
    redactor = Redactor(replacements)
    raw = {
        "principal": (
            "arn:aws:sts::123456789012:"
            "assumed-role/Admin/session-name"
        )
    }

    safe = redactor.redact_obj(raw)

    assert safe == {"principal": "<redacted>"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{", "valid JSON"),
        ("[]", "object"),
        ("{}", "replacements list"),
        ('{"replacements":{}}', "replacements list"),
        ('{"replacements":[["source"]]}', "two-string pair"),
        ('{"replacements":[["source",1]]}', "two-string pair"),
        ('{"replacements":[["","<safe>"]]}', "must not be empty"),
    ],
    ids=[
        "invalid-json",
        "top-level-list",
        "missing-replacements",
        "replacements-object",
        "short-pair",
        "non-string-pair",
        "empty-source",
    ],
)
def test_redactor_rejects_invalid_secret_schema(payload, message):
    with pytest.raises(RedactionError, match=message):
        Redactor.from_secret_json(payload)


@pytest.mark.parametrize(
    ("short_arn", "long_arn"),
    [
        (
            "arn:aws:iam::123456789012:role/Team",
            "arn:aws:iam::123456789012:role/Team/Admin",
        ),
        (
            "arn:aws:sts::123456789012:"
            "assumed-role/Admin/session",
            "arn:aws:sts::123456789012:"
            "assumed-role/Admin/session-long",
        ),
    ],
    ids=["generic", "assumed-role"],
)
def test_redactor_redacts_prefix_arns_in_any_order(
    short_arn,
    long_arn,
):
    for values in (
        [short_arn, long_arn, short_arn],
        [long_arn, short_arn, long_arn],
    ):
        safe = Redactor().redact_obj({"values": values})

        assert safe == {
            "values": ["<redacted>", "<redacted>", "<redacted>"]
        }


@pytest.mark.parametrize(
    ("source", "literal", "expected_literal"),
    [
        ('"', r"\"", r"\<safe>"),
        ("\\", r"\\", "<safe><safe>"),
        ("\n", r"\n", r"\n"),
        ("\t", r"\t", r"\t"),
    ],
    ids=["quote", "backslash", "newline", "tab"],
)
def test_redactor_uses_plain_decoded_substring_semantics(
    source,
    literal,
    expected_literal,
):
    redactor = Redactor(((source, "<safe>"),))
    raw = {
        "target": f"before{source}after",
        "literal": literal,
        "values": [source, literal],
    }

    safe = redactor.redact_obj(raw)

    assert safe == {
        "target": "before<safe>after",
        "literal": expected_literal,
        "values": ["<safe>", expected_literal],
    }


@pytest.mark.parametrize(
    "source",
    [
        'private"alias',
        r"private\alias",
        "private\nalias",
        "private\talias",
    ],
    ids=["quote", "backslash", "newline", "tab"],
)
def test_redactor_assert_safe_decodes_json_string_tokens(source):
    redactor = Redactor(((source, "<safe>"),))
    rendered = json.dumps({"value": source})

    with pytest.raises(RedactionError):
        redactor.assert_safe(rendered)


@pytest.mark.parametrize(
    ("source", "value", "expected"),
    [
        ("team", r"C:\team\logs", r"C:\<safe>\logs"),
        ("root", r"C:\root", r"C:\<safe>"),
        ("name", r"\name", r"\<safe>"),
        ("beta", r"\beta", r"\<safe>"),
        ("false", r"\false", r"\<safe>"),
    ],
)
def test_redactor_replaces_multichar_source_after_backslash(
    source,
    value,
    expected,
):
    redactor = Redactor(((source, "<safe>"),))

    safe = redactor.redact_obj({"value": value})

    assert safe == {"value": expected}


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("team", r"C:\team\logs"),
        ("root", r"C:\root"),
        ("name", r"\name"),
        ("beta", r"\beta"),
        ("false", r"\false"),
    ],
)
def test_redactor_assert_safe_finds_multichar_source_after_backslash(
    source,
    value,
):
    redactor = Redactor(((source, "<safe>"),))
    rendered = json.dumps({"value": value})

    with pytest.raises(RedactionError):
        redactor.assert_safe(rendered)
