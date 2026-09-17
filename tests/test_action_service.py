import json
import sys
from pathlib import Path

import pytest

DEMO_UI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_UI))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import action_service as action_module  # noqa: E402
from backend.action_service import ActionService  # noqa: E402
from backend.clients import AwsClients  # noqa: E402
from backend.redaction import Redactor  # noqa: E402
from backend.settings import Settings  # noqa: E402
from fakes import FakeCodeCommit, FakeLogs, FakeS3  # noqa: E402

SOURCE_KEY = "shaka-player/lib/net/backoff.js"
SOURCE_REPO = "shaka-player"
SOURCE_PATH = "lib/net/backoff.js"
WIKI_REPO = "repo-wiki"
SAFE_ACTOR = "00000000-0000-7000-c000-abcdef123456"
ACCOUNT_COLLISION_ACTOR = "opaque-123456789012-subject"
ARN_COLLISION_ACTOR = (
    "arn:aws:iam::123456789012:role:Opaque"
)
ROLE_COLLISION_ACTOR = "opaque-role-marker"
BEARER_COLLISION_ACTOR = "opaque-bearer-marker"
REFRESH_OPERATION_ID = "collector-refresh:accepted"
WRITE_METHODS = {
    "create_branch",
    "create_commit",
    "create_pull_request",
    "merge_pull_request_by_fast_forward",
}
FIXED_SOURCE_KEY = "shaka-player/lib/net/backoff.js"
FIXED_SOURCE_REPO = "shaka-player"
FIXED_SOURCE_PATH = "lib/net/backoff.js"
FIXED_CURRENT_CONTENT = (
    "const retry = {\n"
    "  baseDelay: 1000,\n"
    "};\n"
)
FIXED_NEXT_CONTENT = (
    "const retry = {\n"
    "  baseDelay: 1200,\n"
    "};\n"
)
_FAKE_AWS_ACCESS_KEY = (
    bytes((65, 75, 73, 65)).decode("ascii")
    + "ABCDEFGHIJKLMNOP"
)


class FakeLambda:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, **kwargs) -> dict:
        self.calls.append(("invoke", dict(kwargs)))
        return {"StatusCode": 202}


class PaginatedCodeCommit(FakeCodeCommit):
    def __init__(self, *, pages, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pages = pages

    def get_differences(self, **kwargs) -> dict:
        self._record("get_differences", kwargs)
        page = self.pages[kwargs.get("NextToken")]
        response = dict(page)
        response["differences"] = list(
            page.get("differences", [])
        )
        return response


def _settings() -> Settings:
    return Settings(
        region="test-region-1",
        wiki_repo=WIKI_REPO,
        repo_prefix="repo-",
        lambda_log_group="lambda-log",
        runtime_log_group="runtime-log",
        skills_bucket="skills-bucket",
        snapshot_bucket="snapshot-bucket",
        collector_function_name="collector-function",
        redaction_secret_arn="redaction-secret",
        user_pool_id="pool",
        app_client_id="client",
        cognito_domain="domain",
        callback_path="/callback",
    )


def _service(
    *,
    codecommit: FakeCodeCommit | None = None,
    lambda_client: FakeLambda | None = None,
    redactor: Redactor | None = None,
    now=lambda: 1_700_000_000.0,
    token_factory=lambda: "123abc456def",
) -> ActionService:
    return ActionService(
        settings=_settings(),
        clients=AwsClients(
            codecommit=codecommit or FakeCodeCommit(),
            logs=FakeLogs(),
            s3=FakeS3(),
            lambda_client=lambda_client or FakeLambda(),
        ),
        redactor=redactor or Redactor(),
        now=now,
        token_factory=token_factory,
    )


def _source_codecommit(
    *,
    head: str = "source-head",
    content: bytes = FIXED_CURRENT_CONTENT.encode(),
) -> FakeCodeCommit:
    return FakeCodeCommit(
        branches={(SOURCE_REPO, "main"): head},
        files={(SOURCE_REPO, head, SOURCE_PATH): content},
    )


def _wiki_pull_request(
    *,
    status: str = "OPEN",
    repository: str = WIKI_REPO,
    destination: str = "refs/heads/main",
    source: str = "refs/heads/wikiagent/update",
    source_commit: str = "wiki-head",
) -> dict:
    return {
        "pullRequestId": "91",
        "pullRequestStatus": status,
        "title": "docs: update wiki",
        "pullRequestTargets": [
            {
                "repositoryName": repository,
                "destinationReference": destination,
                "sourceReference": source,
                "destinationCommit": "wiki-base",
                "sourceCommit": source_commit,
            }
        ],
    }


def _assert_no_writes(codecommit: FakeCodeCommit) -> None:
    assert [
        name
        for name, _ in codecommit.calls
        if name in WRITE_METHODS
    ] == []


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (FIXED_CURRENT_CONTENT, FIXED_NEXT_CONTENT),
        (FIXED_NEXT_CONTENT, FIXED_CURRENT_CONTENT),
    ],
    ids=["increase", "restore"],
)
def test_fixed_demo_mutation_toggles_only_supported_shaka_value(
    current,
    expected,
):
    mutation = action_module.fixed_demo_mutation

    assert mutation(current) == expected


@pytest.mark.parametrize(
    "current",
    [
        "const retry = {};\n",
        (
            "baseDelay: 1000,\n"
            "baseDelay: 1000,\n"
        ),
        (
            "baseDelay: 1000,\n"
            "baseDelay: 1200,\n"
        ),
        "baseDelay: 1500,\n",
    ],
    ids=["missing", "duplicate", "both", "third-value"],
)
def test_fixed_demo_mutation_rejects_ambiguous_or_unknown_state(
    current,
):
    mutation = action_module.fixed_demo_mutation

    with pytest.raises(ValueError):
        mutation(current)


def test_fixed_demo_mutation_ignores_real_shaka_ternary_colon():
    current = (
        "this.baseDelay_ = (parameters.baseDelay == null) ?\n"
        "    defaults.baseDelay : parameters.baseDelay;\n"
        "static defaultRetryParameters() {\n"
        "  return {\n"
        "    baseDelay: 1000,\n"
        "  };\n"
        "}\n"
    )

    changed = action_module.fixed_demo_mutation(current)

    assert "defaults.baseDelay : parameters.baseDelay" in changed
    assert "baseDelay: 1200," in changed


def test_commit_derives_allowed_content_from_current_shaka_blob():
    codecommit = FakeCodeCommit(
        branches={(FIXED_SOURCE_REPO, "main"): "source-head"},
        files={
            (
                FIXED_SOURCE_REPO,
                "source-head",
                FIXED_SOURCE_PATH,
            ): FIXED_CURRENT_CONTENT.encode(),
        },
    )

    result = _service(codecommit=codecommit).commit_and_merge(
        key=FIXED_SOURCE_KEY,
        content=FIXED_NEXT_CONTENT,
        title="demo: toggle Shaka retry delay",
        description="Fixed Maintenance scenario",
        expected_head="source-head",
        actor_sub=SAFE_ACTOR,
    )

    create_commit = next(
        kwargs
        for name, kwargs in codecommit.calls
        if name == "create_commit"
    )
    assert create_commit["repositoryName"] == FIXED_SOURCE_REPO
    assert create_commit["putFiles"] == [
        {
            "filePath": FIXED_SOURCE_PATH,
            "fileContent": FIXED_NEXT_CONTENT.encode(),
        }
    ]
    assert result["repo"] == FIXED_SOURCE_REPO
    assert result["path"] == FIXED_SOURCE_PATH


def test_commit_rejects_browser_content_not_equal_to_server_mutation():
    codecommit = FakeCodeCommit(
        branches={(FIXED_SOURCE_REPO, "main"): "source-head"},
        files={
            (
                FIXED_SOURCE_REPO,
                "source-head",
                FIXED_SOURCE_PATH,
            ): FIXED_CURRENT_CONTENT.encode(),
        },
    )

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            key=FIXED_SOURCE_KEY,
            content=(
                "const retry = {\n"
                "  baseDelay: 9999,\n"
                "};\n"
            ),
            title="demo: tampered request",
            description="Browser content must not be trusted",
            expected_head="source-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_branch",
        "get_file",
    ]
    _assert_no_writes(codecommit)


def test_rejects_source_key_outside_allowlist():
    codecommit = FakeCodeCommit()
    rejected_key = "private/123456789012/secrets.txt"

    with pytest.raises(ValueError) as error:
        _service(codecommit=codecommit).commit_and_merge(
            key=rejected_key,
            content="new secret",
            title="should not run",
            description="should not run",
            expected_head="source-head",
            actor_sub=SAFE_ACTOR,
        )

    assert codecommit.calls == []
    assert rejected_key not in str(error.value)
    assert "123456789012" not in str(error.value)


def test_rejects_stale_expected_head_before_branch_creation():
    codecommit = _source_codecommit(head="current-head")

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            key=SOURCE_KEY,
            content=FIXED_NEXT_CONTENT,
            title="docs: update source",
            description="demo change",
            expected_head="stale-head",
            actor_sub=SAFE_ACTOR,
        )

    assert codecommit.calls == [
        (
            "get_branch",
            {
                "repositoryName": SOURCE_REPO,
                "branchName": "main",
            },
        )
    ]
    _assert_no_writes(codecommit)


def test_rejects_identical_content():
    codecommit = _source_codecommit()

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            key=SOURCE_KEY,
            content=FIXED_CURRENT_CONTENT,
            title="docs: update source",
            description="demo change",
            expected_head="source-head",
            actor_sub=SAFE_ACTOR,
        )

    assert codecommit.calls == [
        (
            "get_branch",
            {
                "repositoryName": SOURCE_REPO,
                "branchName": "main",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": SOURCE_REPO,
                "commitSpecifier": "source-head",
                "filePath": SOURCE_PATH,
            },
        ),
    ]
    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "actor_sub",
    [
        SAFE_ACTOR,
        ACCOUNT_COLLISION_ACTOR,
        ARN_COLLISION_ACTOR,
    ],
    ids=[
        "cognito-specific",
        "account-marker",
        "arn-role-marker",
    ],
)
def test_commit_creates_branch_commit_pr_and_fast_forward_merge(
    actor_sub,
):
    codecommit = _source_codecommit()
    service = _service(
        codecommit=codecommit,
        redactor=Redactor(
            (
                ("123abc456def", "<token>"),
                ("merge-commit", "<merge>"),
            )
        ),
    )

    result = service.commit_and_merge(
        key=SOURCE_KEY,
        content=FIXED_NEXT_CONTENT,
        title="docs: update source",
        description="demo change",
        expected_head="source-head",
        actor_sub=actor_sub,
    )

    assert [name for name, _ in codecommit.calls] == [
        "get_branch",
        "get_file",
        "create_branch",
        "create_commit",
        "create_pull_request",
        "merge_pull_request_by_fast_forward",
    ]
    assert codecommit.calls[2] == (
        "create_branch",
        {
            "repositoryName": SOURCE_REPO,
            "branchName": "demo/1700000000-123abc456def",
            "commitId": "source-head",
        },
    )
    assert codecommit.calls[3] == (
        "create_commit",
        {
            "repositoryName": SOURCE_REPO,
            "branchName": "demo/1700000000-123abc456def",
            "parentCommitId": "source-head",
            "authorName": "demo-ui",
            "email": "demo-ui@example.invalid",
            "commitMessage": "docs: update source",
            "putFiles": [
                {
                    "filePath": SOURCE_PATH,
                    "fileContent": FIXED_NEXT_CONTENT.encode(),
                }
            ],
        },
    )
    assert codecommit.calls[4] == (
        "create_pull_request",
        {
            "title": "docs: update source",
            "description": (
                "demo change\n\n"
                f"requested_by {actor_sub}\n"
                "source demo-ui"
            ),
            "targets": [
                {
                    "repositoryName": SOURCE_REPO,
                    "sourceReference": (
                        "demo/1700000000-123abc456def"
                    ),
                    "destinationReference": "main",
                }
            ],
        },
    )
    assert codecommit.calls[5] == (
        "merge_pull_request_by_fast_forward",
        {
            "pullRequestId": "created-pr",
            "repositoryName": SOURCE_REPO,
            "sourceCommitId": "created-commit",
        },
    )
    description = codecommit.calls[4][1]["description"]
    assert "example.invalid" not in description
    assert "123abc456def" not in description
    assert description.splitlines().count(
        f"requested_by {actor_sub}"
    ) == 1
    assert description.splitlines().count("source demo-ui") == 1
    assert result == {
        "ok": True,
        "operation_id": "source-pr:created-pr",
        "repo": SOURCE_REPO,
        "path": SOURCE_PATH,
        "branch": "demo/1700000000-<token>",
        "commit": "created-commit",
        "pull_request_id": "created-pr",
        "merge_commit_id": "<merge>",
        "merged_at": "2023-11-14T22:13:20+00:00",
    }


def test_approve_rejects_non_wiki_repository():
    invalid_targets = (
        _wiki_pull_request(status="CLOSED"),
        _wiki_pull_request(repository="repo-other"),
        _wiki_pull_request(
            destination="refs/heads/release",
        ),
    )

    for pull_request in invalid_targets:
        codecommit = FakeCodeCommit(
            pull_requests={"91": pull_request}
        )

        with pytest.raises(ValueError):
            _service(codecommit=codecommit).approve(
                wiki_pr="91",
                expected_source_commit="wiki-head",
                actor_sub=SAFE_ACTOR,
            )

        assert [name for name, _ in codecommit.calls] == [
            "get_pull_request"
        ]
        _assert_no_writes(codecommit)


def test_approve_rejects_non_wikiagent_source_branch():
    invalid_sources = (
        _wiki_pull_request(
            source="refs/heads/demo/not-wikiagent",
        ),
        _wiki_pull_request(
            source="refs/heads/wikiagent",
        ),
    )
    for pull_request in invalid_sources:
        codecommit = FakeCodeCommit(
            pull_requests={"91": pull_request}
        )

        with pytest.raises(ValueError):
            _service(codecommit=codecommit).approve(
                wiki_pr="91",
                expected_source_commit="wiki-head",
                actor_sub=SAFE_ACTOR,
            )

        assert [name for name, _ in codecommit.calls] == [
            "get_pull_request"
        ]
        _assert_no_writes(codecommit)

    invalid_differences = (
        [],
        [
            {
                "afterBlob": {"path": "notes.txt"},
                "changeType": "A",
            }
        ],
        [
            {
                "beforeBlob": {"path": "config.yaml"},
                "afterBlob": {"path": "config.yaml"},
                "changeType": "M",
            }
        ],
        [
            {
                "beforeBlob": {"path": "scripts/run.sh"},
                "changeType": "D",
            }
        ],
    )
    for differences in invalid_differences:
        codecommit = FakeCodeCommit(
            pull_requests={
                "91": _wiki_pull_request(),
            },
            differences={
                (
                    WIKI_REPO,
                    "wiki-base",
                    "wiki-head",
                ): differences,
            },
        )

        with pytest.raises(ValueError):
            _service(codecommit=codecommit).approve(
                wiki_pr="91",
                expected_source_commit="wiki-head",
                actor_sub=SAFE_ACTOR,
            )

        assert [name for name, _ in codecommit.calls] == [
            "get_pull_request",
            "get_differences",
        ]
        _assert_no_writes(codecommit)


def test_approve_rejects_source_commit_drift():
    codecommit = FakeCodeCommit(
        pull_requests={
            "91": _wiki_pull_request(
                source_commit="new-wiki-head",
            )
        }
    )

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="stale-wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_pull_request"
    ]
    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "actor_sub",
    [
        SAFE_ACTOR,
        ACCOUNT_COLLISION_ACTOR,
        ARN_COLLISION_ACTOR,
    ],
    ids=[
        "cognito-specific",
        "account-marker",
        "arn-role-marker",
    ],
)
def test_approve_merges_valid_open_pr(actor_sub):
    differences = [
        {
            "afterBlob": {"path": "guides/new.md"},
            "changeType": "A",
        },
        {
            "beforeBlob": {"path": "guides/data.json"},
            "afterBlob": {"path": "guides/data.json"},
            "changeType": "M",
        },
        {
            "beforeBlob": {"path": "CURSOR.json"},
            "changeType": "D",
        },
    ]
    codecommit = FakeCodeCommit(
        files={
            (
                WIKI_REPO,
                "wiki-head",
                "guides/new.md",
            ): b"new page\n",
            (
                WIKI_REPO,
                "wiki-base",
                "guides/data.json",
            ): b'{"value":"old"}',
            (
                WIKI_REPO,
                "wiki-head",
                "guides/data.json",
            ): b'{"value":"new"}',
            (
                WIKI_REPO,
                "wiki-base",
                "CURSOR.json",
            ): b'{"cursor":"old"}',
        },
        pull_requests={"91": _wiki_pull_request()},
        differences={
            (
                WIKI_REPO,
                "wiki-base",
                "wiki-head",
            ): differences,
        },
    )

    result = _service(
        codecommit=codecommit,
        redactor=Redactor(
            (("merge-commit", "<merge>"),)
        ),
    ).approve(
        wiki_pr="91",
        expected_source_commit="wiki-head",
        actor_sub=actor_sub,
    )

    assert codecommit.calls == [
        (
            "get_pull_request",
            {"pullRequestId": "91"},
        ),
        (
            "get_differences",
            {
                "repositoryName": WIKI_REPO,
                "beforeCommitSpecifier": "wiki-base",
                "afterCommitSpecifier": "wiki-head",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": WIKI_REPO,
                "commitSpecifier": "wiki-head",
                "filePath": "guides/new.md",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": WIKI_REPO,
                "commitSpecifier": "wiki-base",
                "filePath": "guides/data.json",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": WIKI_REPO,
                "commitSpecifier": "wiki-head",
                "filePath": "guides/data.json",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": WIKI_REPO,
                "commitSpecifier": "wiki-base",
                "filePath": "CURSOR.json",
            },
        ),
        (
            "merge_pull_request_by_fast_forward",
            {
                "pullRequestId": "91",
                "repositoryName": WIKI_REPO,
                "sourceCommitId": "wiki-head",
            },
        ),
    ]
    assert result == {
        "ok": True,
        "operation_id": "wiki-pr:91",
        "pull_request_id": "91",
        "status": "CLOSED",
        "merged_by": actor_sub,
        "merge_option": "FAST_FORWARD_MERGE",
        "merge_commit_id": "<merge>",
    }


@pytest.mark.parametrize(
    ("change_type", "before_blob", "after_blob"),
    [
        (
            "M",
            None,
            {"path": "guides/page.md"},
        ),
        (
            "A",
            {"path": "guides/page.md"},
            {"path": "guides/page.md"},
        ),
        (
            "D",
            {"path": "guides/page.md"},
            {"path": "guides/page.md"},
        ),
    ],
)
def test_approve_rejects_incomplete_difference_shape(
    change_type,
    before_blob,
    after_blob,
):
    difference = {"changeType": change_type}
    if before_blob is not None:
        difference["beforeBlob"] = before_blob
    if after_blob is not None:
        difference["afterBlob"] = after_blob
    codecommit = FakeCodeCommit(
        pull_requests={"91": _wiki_pull_request()},
        differences={
            (
                WIKI_REPO,
                "wiki-base",
                "wiki-head",
            ): [difference],
        },
    )

    with pytest.raises(
        ValueError,
        match="Wiki pull request review is incomplete",
    ):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    _assert_no_writes(codecommit)


def test_approve_rejects_missing_declared_blob_content():
    codecommit = FakeCodeCommit(
        files={
            (
                WIKI_REPO,
                "wiki-base",
                "guides/page.md",
            ): b"old\n",
        },
        pull_requests={"91": _wiki_pull_request()},
        differences={
            (
                WIKI_REPO,
                "wiki-base",
                "wiki-head",
            ): [
                {
                    "beforeBlob": {
                        "path": "guides/page.md",
                    },
                    "afterBlob": {
                        "path": "guides/page.md",
                    },
                    "changeType": "M",
                }
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="Wiki pull request review is incomplete",
    ):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [
        name
        for name, _ in codecommit.calls
        if name == "get_file"
    ] == ["get_file", "get_file"]
    _assert_no_writes(codecommit)


def test_approve_rejects_invalid_declared_blob_encoding():
    codecommit = FakeCodeCommit(
        files={
            (
                WIKI_REPO,
                "wiki-head",
                "guides/page.md",
            ): "!!!",
        },
        pull_requests={"91": _wiki_pull_request()},
        differences={
            (
                WIKI_REPO,
                "wiki-base",
                "wiki-head",
            ): [
                {
                    "afterBlob": {
                        "path": "guides/page.md",
                    },
                    "changeType": "A",
                }
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="Wiki pull request review is incomplete",
    ):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "review_path",
    [
        "guides/large.md",
        "guides/large.json",
        "CURSOR.json",
    ],
    ids=["markdown", "json", "cursor"],
)
def test_approve_rejects_text_diff_over_review_limit(
    review_path,
):
    before = "".join(
        f"old line {index}\n"
        for index in range(500)
    )
    after = "".join(
        f"new line {index}\n"
        for index in range(500)
    )
    codecommit = FakeCodeCommit(
        files={
            (
                WIKI_REPO,
                "wiki-base",
                review_path,
            ): before.encode("utf-8"),
            (
                WIKI_REPO,
                "wiki-head",
                review_path,
            ): after.encode("utf-8"),
        },
        pull_requests={"91": _wiki_pull_request()},
        differences={
            (
                WIKI_REPO,
                "wiki-base",
                "wiki-head",
            ): [
                {
                    "beforeBlob": {
                        "path": review_path,
                    },
                    "afterBlob": {
                        "path": review_path,
                    },
                    "changeType": "M",
                }
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="Wiki pull request review is incomplete",
    ):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_pull_request",
        "get_differences",
        "get_file",
        "get_file",
    ]
    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "actor_sub",
    [
        SAFE_ACTOR,
        ACCOUNT_COLLISION_ACTOR,
        ROLE_COLLISION_ACTOR,
        BEARER_COLLISION_ACTOR,
    ],
    ids=[
        "cognito-specific",
        "account-marker",
        "role-marker",
        "bearer-marker",
    ],
)
def test_refresh_invokes_collector_asynchronously(actor_sub):
    lambda_client = FakeLambda()

    result = _service(
        lambda_client=lambda_client,
    ).request_refresh(actor_sub=actor_sub)

    assert lambda_client.calls == [
        (
            "invoke",
            {
                "FunctionName": "collector-function",
                "InvocationType": "Event",
                "Payload": json.dumps(
                    {
                        "source": "demo-ui",
                        "requested_by": actor_sub,
                    }
                ).encode("utf-8"),
            },
        )
    ]
    assert result == {
        "ok": True,
        "operation_id": REFRESH_OPERATION_ID,
        "accepted": True,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "safe title\nforged"),
        ("title", "owner person@example.com"),
        ("description", "safe\x00forged"),
        ("description", "safe\tforged"),
        ("description", "safe\ud800forged"),
        ("description", "safe\u202eforged"),
        ("description", "safe\u200bforged"),
        (
            "description",
            "safe\u2028requested_by forged-subject",
        ),
        ("description", "owner person@example.com"),
        ("description", "account 123456789012"),
        (
            "description",
            "arn:aws:iam::123456789012:role/Admin",
        ),
        (
            "description",
            "arn:aws:s3:::private-demo-bucket",
        ),
        (
            "description",
            "arn:aws:sts::123456789012:"
            "assumed-role/Admin/session",
        ),
        (
            "description",
            f"key {_FAKE_AWS_ACCESS_KEY}",
        ),
        (
            "description",
            "jwt eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxIn0.signature123",
        ),
        (
            "description",
            "Authorization: Bearer "
            "abcdefghijklmnopqrstuvwxyz1234",
        ),
        (
            "description",
            "authorization="
            "opaquecredentialvalue1234567890",
        ),
        ("description", "token=abcdef123456"),
        ("description", "password: private-value"),
        ("description", "api_key = private-value"),
        (
            "description",
            "safe line\nrequested_by forged-subject",
        ),
        ("description", "safe line\nsource demo-ui"),
    ],
    ids=[
        "title-control",
        "title-sensitive",
        "description-control",
        "description-tab",
        "description-surrogate",
        "description-bidi-control",
        "description-zero-width",
        "unicode-line-injection",
        "email",
        "account-id",
        "aws-arn",
        "aws-arn-without-account",
        "assumed-role",
        "access-key",
        "jwt",
        "bearer",
        "authorization-assignment",
        "token-assignment",
        "password-assignment",
        "api-key-assignment",
        "requested-by-injection",
        "source-injection",
    ],
)
def test_commit_rejects_unsafe_outbound_metadata_before_aws_call(
    field,
    value,
):
    codecommit = _source_codecommit()
    request = {
        "key": SOURCE_KEY,
        "content": FIXED_NEXT_CONTENT,
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "source-head",
        "actor_sub": SAFE_ACTOR,
    }
    request[field] = value

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            **request
        )

    assert codecommit.calls == []


@pytest.mark.parametrize(
    "description",
    [
        "Use Bearer authentication for this endpoint",
        "authorization: IAM mode is required",
        "source schema migration details",
        "개발자 👩‍💻 검토 완료",
    ],
    ids=[
        "bearer-explanation",
        "authorization-mode",
        "source-explanation",
        "emoji-zwj",
    ],
)
def test_commit_allows_normal_security_and_unicode_description(
    description,
):
    codecommit = _source_codecommit()

    result = _service(
        codecommit=codecommit,
    ).commit_and_merge(
        key=SOURCE_KEY,
        content=FIXED_NEXT_CONTENT,
        title="docs: update source",
        description=description,
        expected_head="source-head",
        actor_sub=SAFE_ACTOR,
    )

    assert result["ok"] is True
    assert [
        name
        for name, _ in codecommit.calls
        if name in WRITE_METHODS
    ] == [
        "create_branch",
        "create_commit",
        "create_pull_request",
        "merge_pull_request_by_fast_forward",
    ]


@pytest.mark.parametrize(
    ("field", "sensitive_value"),
    [
        (
            "title",
            "opaque-private-value-987654321",
        ),
        (
            "description",
            "https://internal.example.corp/private/path",
        ),
        (
            "description",
            "raw-custom-sensitive-material",
        ),
    ],
    ids=[
        "opaque-title",
        "internal-url",
        "raw-custom-secret",
    ],
)
def test_commit_rejects_configured_sensitive_source_before_aws_read(
    field,
    sensitive_value,
):
    codecommit = _source_codecommit()
    request = {
        "key": SOURCE_KEY,
        "content": FIXED_NEXT_CONTENT,
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "source-head",
        "actor_sub": SAFE_ACTOR,
    }
    request[field] = f"rotate {sensitive_value}"
    redactor = Redactor(
        ((sensitive_value, "<private>"),)
    )

    with pytest.raises(
        ValueError,
        match="Pull request metadata is invalid",
    ):
        _service(
            codecommit=codecommit,
            redactor=redactor,
        ).commit_and_merge(**request)

    assert codecommit.calls == []


@pytest.mark.parametrize(
    "actor_sub",
    [
        None,
        "",
        " ",
        "subject\t1",
        f"{SAFE_ACTOR}\n",
        "subject/1",
        "a" * 129,
    ],
    ids=[
        "non-string",
        "empty",
        "whitespace",
        "control",
        "newline",
        "slash",
        "over-length",
    ],
)
def test_commit_rejects_unsafe_actor_before_aws_call(
    actor_sub,
):
    codecommit = _source_codecommit()

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            key=SOURCE_KEY,
            content=FIXED_NEXT_CONTENT,
            title="docs: update source",
            description="demo change",
            expected_head="source-head",
            actor_sub=actor_sub,
        )

    assert codecommit.calls == []


def test_approve_rejects_unsafe_actor_before_aws_call():
    codecommit = FakeCodeCommit(
        pull_requests={"91": _wiki_pull_request()}
    )

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub="unsafe/subject",
        )

    assert codecommit.calls == []


def test_refresh_rejects_unsafe_actor_without_invoke():
    lambda_client = FakeLambda()

    with pytest.raises(ValueError):
        _service(
            lambda_client=lambda_client,
        ).request_refresh(
            actor_sub=(
                f"{SAFE_ACTOR}\n"
                "authorization=Bearer private"
            )
        )

    assert lambda_client.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", None),
        ("key", []),
        ("key", ""),
        ("content", None),
        ("content", b"bytes"),
        ("content", ""),
        ("title", None),
        ("title", 1),
        ("title", ""),
        ("description", None),
        ("description", {}),
        ("description", ""),
        ("expected_head", None),
        ("expected_head", 1),
        ("expected_head", ""),
        ("actor_sub", None),
    ],
    ids=[
        "key-none",
        "key-list",
        "key-empty",
        "content-none",
        "content-bytes",
        "content-empty",
        "title-none",
        "title-int",
        "title-empty",
        "description-none",
        "description-dict",
        "description-empty",
        "expected-head-none",
        "expected-head-int",
        "expected-head-empty",
        "actor-none",
    ],
)
def test_commit_rejects_malformed_required_input_before_aws_read(
    field,
    value,
):
    codecommit = _source_codecommit()
    request = {
        "key": SOURCE_KEY,
        "content": FIXED_NEXT_CONTENT,
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "source-head",
        "actor_sub": SAFE_ACTOR,
    }
    request[field] = value

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            **request
        )

    assert codecommit.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wiki_pr", None),
        ("wiki_pr", ""),
        ("expected_source_commit", None),
        ("expected_source_commit", ""),
        ("actor_sub", None),
    ],
)
def test_approve_rejects_malformed_required_input_before_aws_read(
    field,
    value,
):
    codecommit = FakeCodeCommit(
        pull_requests={"91": _wiki_pull_request()}
    )
    request = {
        "wiki_pr": "91",
        "expected_source_commit": "wiki-head",
        "actor_sub": SAFE_ACTOR,
    }
    request[field] = value

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).approve(**request)

    assert codecommit.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "t" * 151),
        (
            "description",
            "d"
            * (
                10_240
                - len(
                    f"\n\nrequested_by {SAFE_ACTOR}\n"
                    "source demo-ui"
                )
                + 1
            ),
        ),
        ("content", "가" * 2_097_153),
    ],
    ids=[
        "title",
        "final-description",
        "utf8-content",
    ],
)
def test_commit_rejects_service_limit_overflow_before_aws_read(
    field,
    value,
):
    codecommit = _source_codecommit()
    request = {
        "key": SOURCE_KEY,
        "content": FIXED_NEXT_CONTENT,
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "source-head",
        "actor_sub": SAFE_ACTOR,
    }
    request[field] = value

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).commit_and_merge(
            **request
        )

    assert codecommit.calls == []


@pytest.mark.parametrize(
    "token",
    [
        "ABCDEF123456",
        "abcdef12345",
        "abcdef12345g",
        123456789012,
    ],
    ids=[
        "uppercase",
        "short",
        "non-hex",
        "non-string",
    ],
)
def test_commit_rejects_invalid_token_before_branch_creation(
    token,
):
    codecommit = _source_codecommit()

    with pytest.raises(ValueError):
        _service(
            codecommit=codecommit,
            token_factory=lambda: token,
        ).commit_and_merge(
            key=SOURCE_KEY,
            content=FIXED_NEXT_CONTENT,
            title="docs: update source",
            description="demo change",
            expected_head="source-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_branch",
        "get_file",
    ]
    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "timestamp",
    [
        253_402_300_800,
        -1,
        float("inf"),
        float("nan"),
        "1700000000",
    ],
    ids=[
        "year-10000",
        "negative",
        "infinite",
        "nan",
        "non-numeric",
    ],
)
def test_commit_rejects_invalid_timestamp_before_first_write(
    timestamp,
):
    codecommit = _source_codecommit()

    with pytest.raises(ValueError):
        _service(
            codecommit=codecommit,
            now=lambda: timestamp,
        ).commit_and_merge(
            key=SOURCE_KEY,
            content=FIXED_NEXT_CONTENT,
            title="docs: update source",
            description="demo change",
            expected_head="source-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_branch",
        "get_file",
    ]
    _assert_no_writes(codecommit)


@pytest.mark.parametrize(
    "redactor",
    [
        Redactor((("ok", "<safe>"),)),
        Redactor(
            (
                ("ok", "same-key"),
                ("operation_id", "same-key"),
            )
        ),
        Redactor((("internal-name", "123456789012"),)),
        Redactor((("internal-name", "internal-name-safe"),)),
        Redactor((("source-only",),)),
    ],
    ids=[
        "response-key-change",
        "response-key-collision",
        "unsafe-replacement",
        "residual-source",
        "malformed-schema",
    ],
)
def test_action_service_rejects_invalid_redactor_before_aws_calls(
    redactor,
):
    codecommit = FakeCodeCommit()

    with pytest.raises(
        ValueError,
        match="Action redactor configuration is invalid",
    ):
        _service(
            codecommit=codecommit,
            redactor=redactor,
        )

    assert codecommit.calls == []


def test_approve_rejects_target_cardinality_and_missing_destination():
    no_targets = _wiki_pull_request()
    no_targets["pullRequestTargets"] = []

    two_targets = _wiki_pull_request()
    two_targets["pullRequestTargets"].append(
        dict(two_targets["pullRequestTargets"][0])
    )

    missing_destination = _wiki_pull_request()
    missing_destination["pullRequestTargets"][0].pop(
        "destinationCommit"
    )

    for pull_request in (
        no_targets,
        two_targets,
        missing_destination,
    ):
        codecommit = FakeCodeCommit(
            pull_requests={"91": pull_request}
        )

        with pytest.raises(ValueError):
            _service(codecommit=codecommit).approve(
                wiki_pr="91",
                expected_source_commit="wiki-head",
                actor_sub=SAFE_ACTOR,
            )

        assert [name for name, _ in codecommit.calls] == [
            "get_pull_request"
        ]
        _assert_no_writes(codecommit)


def test_approve_paginates_and_rejects_disallowed_rename():
    codecommit = PaginatedCodeCommit(
        pull_requests={"91": _wiki_pull_request()},
        pages={
            None: {
                "differences": [
                    {
                        "afterBlob": {
                            "path": "guides/page.md",
                        },
                        "changeType": "A",
                    }
                ],
                "NextToken": "page-2",
            },
            "page-2": {
                "differences": [
                    {
                        "beforeBlob": {
                            "path": "guides/old.md",
                        },
                        "afterBlob": {
                            "path": "guides/new.txt",
                        },
                        "changeType": "M",
                    }
                ]
            },
        },
    )

    with pytest.raises(ValueError):
        _service(codecommit=codecommit).approve(
            wiki_pr="91",
            expected_source_commit="wiki-head",
            actor_sub=SAFE_ACTOR,
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_pull_request",
        "get_differences",
        "get_differences",
    ]
    assert codecommit.calls[2][1]["NextToken"] == "page-2"
    _assert_no_writes(codecommit)


def test_approve_paginates_allowed_rename_and_deletion():
    codecommit = PaginatedCodeCommit(
        files={
            (
                WIKI_REPO,
                "wiki-base",
                "guides/old.md",
            ): b"old name\n",
            (
                WIKI_REPO,
                "wiki-head",
                "guides/new.md",
            ): b"new name\n",
            (
                WIKI_REPO,
                "wiki-base",
                "CURSOR.json",
            ): b'{"cursor":"old"}',
        },
        pull_requests={"91": _wiki_pull_request()},
        pages={
            None: {
                "differences": [
                    {
                        "beforeBlob": {
                            "path": "guides/old.md",
                        },
                        "afterBlob": {
                            "path": "guides/new.md",
                        },
                        "changeType": "M",
                    }
                ],
                "NextToken": "page-2",
            },
            "page-2": {
                "differences": [
                    {
                        "beforeBlob": {
                            "path": "CURSOR.json",
                        },
                        "changeType": "D",
                    }
                ]
            },
        },
    )

    result = _service(codecommit=codecommit).approve(
        wiki_pr="91",
        expected_source_commit="wiki-head",
        actor_sub=SAFE_ACTOR,
    )

    assert result["ok"] is True
    assert result["operation_id"] == "wiki-pr:91"
    assert [name for name, _ in codecommit.calls] == [
        "get_pull_request",
        "get_differences",
        "get_differences",
        "get_file",
        "get_file",
        "get_file",
        "merge_pull_request_by_fast_forward",
    ]
    assert codecommit.calls[2][1]["NextToken"] == "page-2"
