import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DEMO_UI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_UI))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import live  # noqa: E402
import server  # noqa: E402
from backend.action_service import ActionService  # noqa: E402
from backend.clients import AwsClients  # noqa: E402
from backend.query_service import QueryService  # noqa: E402
from backend.redaction import RedactionError, Redactor  # noqa: E402
from backend.settings import Settings  # noqa: E402
from fakes import FakeCodeCommit, FakeLogs, FakeS3  # noqa: E402


class FailingGetFileCodeCommit(FakeCodeCommit):
    def get_file(self, **kwargs) -> dict:
        self._record("get_file", kwargs)
        raise RuntimeError("transient read failure")


class PaginatedQueryCodeCommit(FakeCodeCommit):
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


SAFE_ACTOR = "00000000-0000-4000-8000-abcdef123456"
FIXED_SOURCE_KEY = "shaka-player/lib/net/backoff.js"
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


def _review_pull_request(
    *,
    source_commit: str = "head",
    destination_commit: str = "base",
) -> dict:
    return {
        "pullRequestId": "91",
        "pullRequestStatus": "OPEN",
        "title": "docs: update page",
        "pullRequestTargets": [
            {
                "repositoryName": "repo-wiki",
                "destinationReference": "refs/heads/main",
                "sourceReference": (
                    "refs/heads/wikiagent/update"
                ),
                "destinationCommit": destination_commit,
                "sourceCommit": source_commit,
            }
        ],
    }


def _settings() -> Settings:
    return Settings(
        region="test-region-1",
        wiki_repo="repo-wiki",
        repo_prefix="repo-",
        lambda_log_group="lambda-log",
        runtime_log_group="runtime-log",
        skills_bucket="skills-bucket",
        snapshot_bucket="snapshot-bucket",
        collector_function_name="collector",
        redaction_secret_arn="redaction-secret",
        user_pool_id="pool",
        app_client_id="client",
        cognito_domain="domain",
        callback_path="/callback",
        source_repositories=(
            "shaka-player",
            "repo-target",
            "repo-other",
        ),
    )


def _service(
    *,
    codecommit: FakeCodeCommit | None = None,
    logs: FakeLogs | None = None,
    s3: FakeS3 | None = None,
    redactor: Redactor | None = None,
    now=lambda: 1_700_000_000.0,
) -> QueryService:
    return QueryService(
        settings=_settings(),
        clients=AwsClients(
            codecommit=codecommit or FakeCodeCommit(),
            logs=logs or FakeLogs(),
            s3=s3 or FakeS3(),
        ),
        redactor=redactor or Redactor(),
        now=now,
    )


def test_list_editable_reads_only_fixed_shaka_source():
    codecommit = FakeCodeCommit(
        branches={
            ("shaka-player", "main"): "shaka-head",
            ("repo-private", "main"): "private-head",
        },
        files={
            (
                "shaka-player",
                "shaka-head",
                "lib/net/backoff.js",
            ): b"const retry = {\n  baseDelay: 1000,\n};\n",
            (
                "repo-private",
                "private-head",
                "secrets.txt",
            ): b"must not be exposed",
        },
    )

    result = _service(codecommit=codecommit).list_editable()

    assert result == {
        "files": [
            {
                "key": "shaka-player/lib/net/backoff.js",
                "repo": "shaka-player",
                "path": "lib/net/backoff.js",
                "content": (
                    "const retry = {\n"
                    "  baseDelay: 1000,\n"
                    "};\n"
                ),
                "lines": 3,
                "head": "shaka-head",
            }
        ]
    }
    assert codecommit.calls == [
        (
            "get_branch",
            {
                "repositoryName": "shaka-player",
                "branchName": "main",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": "shaka-player",
                "commitSpecifier": "shaka-head",
                "filePath": "lib/net/backoff.js",
            },
        ),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "branch",
        "file",
        "decode",
    ],
)
def test_list_editable_fails_closed_on_required_read_error(
    failure,
):
    branches = {
        ("shaka-player", "main"): "shaka-head",
    }
    files = {
        (
            "shaka-player",
            "shaka-head",
            "lib/net/backoff.js",
        ): (
            b"\xff"
            if failure == "decode"
            else b"baseDelay: 1000,\n"
        ),
    }
    if failure == "branch":
        codecommit = FakeCodeCommit()
    elif failure == "file":
        codecommit = FailingGetFileCodeCommit(
            branches=branches,
            files=files,
        )
    else:
        codecommit = FakeCodeCommit(
            branches=branches,
            files=files,
        )

    with pytest.raises(
        ValueError,
        match="Required repository content is unavailable",
    ):
        _service(codecommit=codecommit).list_editable()

    assert all(
        name
        not in {
            "create_branch",
            "create_commit",
            "create_pull_request",
            "merge_pull_request_by_fast_forward",
        }
        for name, _ in codecommit.calls
    )


def test_live_load_files_disables_commit_on_failed_or_incomplete_response():
    html = (DEMO_UI / "frontend" / "live.html").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"async function loadFiles\(\)\{.*?\n\}"
        r"\nfunction pick",
        html,
        re.S,
    )
    assert match is not None
    load_files = match.group(0).removesuffix(
        "\nfunction pick"
    )
    script = f"""
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      disabled: false,
      innerHTML: '',
      textContent: '',
      value: '',
    }});
  }}
  return elements.get(id);
}}
const $ = id => element(id);
const esc = value => String(value ?? '');
let FILES = [{{key: 'stale'}}];
let NEXT_CONTENT = null;
let READ_ONLY = false;
const FIXED_KEY = 'shaka-player/lib/net/backoff.js';
const FIXED_REPOSITORY = 'shaka-player';
const FIXED_PATH = 'lib/net/backoff.js';
function pick() {{}}
global.window = {{
  DemoApi: {{
    get: async () => {{
      throw new Error('request failed');
    }},
  }},
}};
{load_files}

(async () => {{
  await loadFiles();
  if (FILES.length !== 0 || !$('btnCommit').disabled) {{
    throw new Error('non-OK response enabled commit');
  }}

  FILES = [{{key: 'stale'}}];
  $('btnCommit').disabled = false;
  window.DemoApi.get = async () => ({{
    files: [{{
      key: 'incomplete',
      content: 'body',
    }}],
  }});
  await loadFiles();
  if (FILES.length !== 0 || !$('btnCommit').disabled) {{
    throw new Error('incomplete response enabled commit');
  }}
}})().catch(error => {{
  console.error(error.message);
  process.exit(1);
}});
"""
    result = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_live_wiki_review_disables_approve_on_incomplete_response():
    html = (DEMO_UI / "frontend" / "live.html").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"async function showWikiPR\(wpr,payload\)\{.*?\n\}"
        r"\n\n\$\('btnApprove'",
        html,
        re.S,
    )
    assert match is not None
    show_wiki_pr = match.group(0).removesuffix(
        "\n\n$('btnApprove'"
    )
    script = f"""
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      disabled: false,
      innerHTML: '',
      textContent: '',
      classList: {{
        add() {{}},
        remove() {{}},
      }},
      scrollIntoView() {{}},
    }});
  }}
  return elements.get(id);
}}
const $ = id => element(id);
const esc = value => String(value ?? '');
let RUN = {{
  wiki_pr: 'stale-pr',
  expected_source_commit: 'stale-head',
  doc_paths: [],
}};
let READ_ONLY = false;
const scrollBehavior = 'auto';
global.window = {{
  DemoApi: {{
    get: async () => {{
      throw new Error('request failed');
    }},
  }},
}};
{show_wiki_pr}

(async () => {{
  await showWikiPR('91', {{scope: []}});
  if (!$('btnApprove').disabled ||
      RUN.wiki_pr ||
      RUN.expected_source_commit !== null) {{
    throw new Error('non-OK review enabled approval');
  }}

  RUN.wiki_pr = 'stale-pr';
  RUN.expected_source_commit = 'stale-head';
  $('btnApprove').disabled = false;
  window.DemoApi.get = async () => ({{
    pull_request_id: '91',
    status: 'OPEN',
    branch: 'refs/heads/wikiagent/update',
    all_files: ['guides/page.md'],
    files: [],
  }});
  await showWikiPR('91', {{scope: []}});
  if (!$('btnApprove').disabled ||
      RUN.wiki_pr ||
      RUN.expected_source_commit !== null) {{
    throw new Error('incomplete review enabled approval');
  }}

  RUN.wiki_pr = 'stale-pr';
  RUN.expected_source_commit = 'stale-head';
  $('btnApprove').disabled = false;
  window.DemoApi.get = async () => ({{
    pull_request_id: '91',
    status: 'OPEN',
    branch: 'refs/heads/wikiagent/update',
    review_complete: false,
    source_commit: 'head',
    all_files: ['guides/large.md'],
    files: [{{
      path: 'guides/large.md',
      before: 'old',
      after: 'new',
      truncated: true,
      unified: ['... diff truncated: 10 lines hidden ...'],
    }}],
  }});
  await showWikiPR('91', {{scope: []}});
  if (!$('btnApprove').disabled ||
      RUN.wiki_pr ||
      RUN.expected_source_commit !== null ||
      !$('ainfo').textContent.includes('400줄')) {{
    throw new Error('truncated review enabled approval');
  }}

  window.DemoApi.get = async () => ({{
    pull_request_id: '91',
    status: 'OPEN',
    branch: 'refs/heads/wikiagent/update',
    review_complete: true,
    source_commit: 'head',
    all_files: ['guides/page.md'],
    files: [{{
      path: 'guides/page.md',
      before: 'old',
      after: 'new',
      truncated: false,
      unified: ['-old', '+new'],
    }}],
  }});
  await showWikiPR('91', {{scope: []}});
  if ($('btnApprove').disabled ||
      RUN.wiki_pr !== '91' ||
      RUN.expected_source_commit !== 'head') {{
    throw new Error('complete review did not enable approval');
  }}
}})().catch(error => {{
  console.error(error.message);
  process.exit(1);
}});
"""
    result = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_legacy_commit_uses_fixed_physical_repository(
    monkeypatch,
):
    codecommit = FakeCodeCommit(
        branches={
            ("shaka-player", "main"): "main-commit",
        },
        files={
            (
                "shaka-player",
                "main-commit",
                FIXED_SOURCE_PATH,
            ): FIXED_CURRENT_CONTENT.encode(),
        },
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            repo_prefix="custom-",
        ),
        codecommit=codecommit,
        logs=FakeLogs(),
        s3=FakeS3(),
    )
    monkeypatch.setattr(
        live,
        "_runtime",
        lambda: runtime,
    )
    monkeypatch.setattr(
        live,
        "redact",
        lambda value: value,
    )
    monkeypatch.setattr(
        live.time,
        "time",
        lambda: 1_234.0,
    )

    result = live.commit_and_merge(
        FIXED_SOURCE_KEY,
        FIXED_NEXT_CONTENT,
        "demo: toggle retry delay",
        "fixed scenario",
        "main-commit",
    )

    assert result["repo"] == "shaka-player"
    assert codecommit.calls[0] == (
        "get_branch",
        {
            "repositoryName": "shaka-player",
            "branchName": "main",
        },
    )
    assert codecommit.calls[1] == (
        "get_file",
        {
            "repositoryName": "shaka-player",
            "commitSpecifier": "main-commit",
            "filePath": FIXED_SOURCE_PATH,
        },
    )
    assert codecommit.calls[2][1][
        "repositoryName"
    ] == "shaka-player"
    assert codecommit.calls[3][1][
        "repositoryName"
    ] == "shaka-player"
    assert codecommit.calls[4][1]["targets"][0][
        "repositoryName"
    ] == "shaka-player"
    assert codecommit.calls[5][1][
        "repositoryName"
    ] == "shaka-player"
    description = codecommit.calls[4][1]["description"]
    assert (
        "requested_by "
        "00000000-0000-4000-8000-abcdef123456"
    ) in description
    assert "legacy-localhost" not in description


def test_legacy_commit_requires_expected_head_without_aws_call(
    monkeypatch,
):
    codecommit = FakeCodeCommit()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(repo_prefix="custom-"),
        codecommit=codecommit,
        logs=FakeLogs(),
        s3=FakeS3(),
    )
    monkeypatch.setattr(live, "_runtime", lambda: runtime)

    with pytest.raises(ValueError):
        live.commit_and_merge(
            FIXED_SOURCE_KEY,
            FIXED_NEXT_CONTENT,
            "demo: toggle retry delay",
            "fixed scenario",
        )

    assert codecommit.calls == []


def test_legacy_approve_requires_expected_source_without_aws_call(
    monkeypatch,
):
    codecommit = FakeCodeCommit()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            wiki_repo="repo-wiki",
            repo_prefix="repo-",
        ),
        codecommit=codecommit,
        logs=FakeLogs(),
        s3=FakeS3(),
    )
    monkeypatch.setattr(live, "_runtime", lambda: runtime)

    with pytest.raises(ValueError):
        live.approve("91")

    assert codecommit.calls == []


def test_legacy_commit_rejects_main_drift_without_write(
    monkeypatch,
):
    codecommit = FakeCodeCommit(
        branches={
            ("shaka-player", "main"): "current-head",
        }
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(repo_prefix="custom-"),
        codecommit=codecommit,
        logs=FakeLogs(),
        s3=FakeS3(),
    )
    monkeypatch.setattr(live, "_runtime", lambda: runtime)

    with pytest.raises(ValueError):
        live.commit_and_merge(
            FIXED_SOURCE_KEY,
            FIXED_NEXT_CONTENT,
            "demo: toggle retry delay",
            "fixed scenario",
            "reviewed-head",
        )

    assert [name for name, _ in codecommit.calls] == [
        "get_branch"
    ]
    assert all(
        name
        not in {
            "create_branch",
            "create_commit",
            "create_pull_request",
            "merge_pull_request_by_fast_forward",
        }
        for name, _ in codecommit.calls
    )


def test_legacy_approve_rejects_reviewed_source_drift_without_write(
    monkeypatch,
):
    codecommit = FakeCodeCommit(
        pull_requests={
            "91": {
                "pullRequestId": "91",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-wiki",
                        "destinationReference": (
                            "refs/heads/main"
                        ),
                        "sourceReference": (
                            "refs/heads/wikiagent/update"
                        ),
                        "destinationCommit": "base",
                        "sourceCommit": "current-head",
                    }
                ],
            }
        }
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            wiki_repo="repo-wiki",
            repo_prefix="repo-",
        ),
        codecommit=codecommit,
        logs=FakeLogs(),
        s3=FakeS3(),
    )
    monkeypatch.setattr(live, "_runtime", lambda: runtime)

    with pytest.raises(ValueError):
        live.approve("91", "reviewed-head")

    assert [name for name, _ in codecommit.calls] == [
        "get_pull_request"
    ]
    assert all(
        name != "merge_pull_request_by_fast_forward"
        for name, _ in codecommit.calls
    )


def test_local_server_forwards_optimistic_tokens(monkeypatch):
    calls = []

    def commit(*args):
        calls.append(("commit", args))
        return {"pull_request_id": "17"}

    def approve(*args):
        calls.append(("approve", args))
        return {"pull_request_id": "91"}

    monkeypatch.setattr(live, "commit_and_merge", commit)
    monkeypatch.setattr(live, "approve", approve)

    handler = object.__new__(server.Handler)
    handler.read_only = False
    handler._guard = lambda operation: operation()
    handler._require_action_headers = lambda: None

    handler.path = "/api/actions/commit"
    handler._body = lambda: {
        "key": FIXED_SOURCE_KEY,
        "content": FIXED_NEXT_CONTENT,
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "reviewed-head",
    }
    handler.do_POST()

    handler.path = "/api/actions/approve"
    handler._body = lambda: {
        "wiki_pr": "91",
        "expected_source_commit": "reviewed-wiki-head",
    }
    handler.do_POST()

    assert calls == [
        (
            "commit",
            (
                FIXED_SOURCE_KEY,
                FIXED_NEXT_CONTENT,
                "docs: update source",
                "demo change",
                "reviewed-head",
            ),
        ),
        (
            "approve",
            ("91", "reviewed-wiki-head"),
        ),
    ]


def test_live_html_posts_optimistic_tokens():
    html = (DEMO_UI / "frontend" / "live.html").read_text(
        encoding="utf-8"
    )

    assert "/api/actions/commit" in html
    assert "/api/actions/approve" in html
    assert re.search(
        r"expected_head\s*:\s*file\.head",
        html,
    )
    assert re.search(
        r"RUN\.expected_source_commit\s*="
        r"\s*response\.source_commit",
        html,
    )
    assert re.search(
        r"expected_source_commit\s*:"
        r"\s*RUN\.expected_source_commit",
        html,
    )
    assert not hasattr(live, "_PrefetchedCodeCommit")


def test_progress_returns_redacted_lambda_and_runtime_events():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    target_diff = (
        "fedcba6543210987fedcba6543210987"
        "fedcba6543210987fedcba6543210987"
    )
    principal = (
        "arn:aws:sts::123456789012:"
        "assumed-role/Admin/session-name"
    )
    raw_payload = {
        "source": "event:pr#17",
        "repo": "repo-target",
        "base_sha": target_base,
        "head_sha": target_head,
        "diff_sha256": target_diff,
        "principal": principal,
        "runtime": {
            "wiki": {
                "pull_request_id": "91",
                "commit_id": "wiki-head",
                "source_commit": "wiki-head",
                "sourceCommit": "wiki-head",
                "source_commit_id": "wiki-head",
                "status": "created",
            }
        },
    }
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": "destination-snapshot",
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_699_999_000_000,
                "message": json.dumps(raw_payload),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_699_999_100_000,
                "message": json.dumps(
                    {
                        "event": "converse_started",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "principal": principal,
                        "model": "test-model",
                    }
                ),
            },
        ]
    )

    result = _service(
        codecommit=codecommit,
        logs=logs,
    ).progress("17", None)

    assert result["pull_request_id"] == "17"
    assert result["lambda_started"] is True
    assert result["done"] is True
    assert result["wiki_pull_request_id"] == "91"
    assert result["payload"]["principal"] == "<redacted>"
    assert result["payload"]["runtime"]["wiki"] == {
        "pull_request_id": "91",
        "status": "created",
    }
    assert raw_payload["runtime"]["wiki"] == {
        "pull_request_id": "91",
        "commit_id": "wiki-head",
        "source_commit": "wiki-head",
        "sourceCommit": "wiki-head",
        "source_commit_id": "wiki-head",
        "status": "created",
    }
    assert result["runtime_events"][0]["event"] == (
        "converse_started"
    )
    assert result["runtime_events"][0]["detail"] == {
        "repo": "repo-target",
        "base_sha": "aaaabbbbcccc",
        "head_sha": "abcdef123456",
        "diff_sha256": "fedcba654321",
        "principal": "<redacted>",
        "model": "test-model",
    }
    rendered = json.dumps(result)
    assert "123456789012" not in rendered
    assert "assumed-role" not in rendered


def test_progress_filters_stale_unknown_and_unrelated_events():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    target_diff = (
        "fedcba6543210987fedcba6543210987"
        "fedcba6543210987fedcba6543210987"
    )
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": "destination-snapshot",
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_450,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": target_head,
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_500,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": target_head,
                        "diff_sha256": target_diff,
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 999,
                "message": json.dumps(
                    {
                        "event": "stage_plan",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "stale",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_100,
                "message": json.dumps(
                    {
                        "event": "unknown_stage",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "unknown",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_200,
                "message": json.dumps(
                    {
                        "event": "stage_plan",
                        "repo": "repo-other",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "wrong-repo",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_250,
                "message": json.dumps(
                    {
                        "event": "skills_selected",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "diff_sha256": "fedcba654321",
                        "marker": "missing-head",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_300,
                "message": json.dumps(
                    {
                        "event": "skills_selected",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "000000000000",
                        "diff_sha256": "fedcba654321",
                        "marker": "wrong-head",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_350,
                "message": json.dumps(
                    {
                        "event": "publish_state_inspected",
                        "repo": "repo-target",
                        "base_sha": "ddddeeeeffff",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "wrong-base",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_375,
                "message": json.dumps(
                    {
                        "event": "publish_state_transition",
                        "repo": "repo-target",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "missing-base",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_400,
                "message": json.dumps(
                    {
                        "event": "validation_passed",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "000000000000",
                        "marker": "wrong-diff",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_500,
                "message": json.dumps(
                    {
                        "event": "converse_started",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "matching",
                    }
                ),
            },
        ]
    )

    def unexpected_now() -> float:
        raise AssertionError(
            "explicit since_ms must not call now"
        )

    result = _service(
        codecommit=codecommit,
        logs=logs,
        now=unexpected_now,
    ).progress("17", 1_000)

    assert [
        event["event"]
        for event in result["runtime_events"]
    ] == ["converse_started"]
    assert result["runtime_events"][0]["detail"]["marker"] == (
        "matching"
    )
    assert [
        call[1]["startTime"]
        for call in logs.calls
        if call[0] == "filter_log_events"
    ] == [1_000, 1_000]


def test_progress_rejects_wrong_or_missing_base_before_payload():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": (
                            "destination-snapshot"
                        ),
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_100,
                "message": json.dumps(
                    {
                        "event": "stage_plan",
                        "repo": "repo-target",
                        "base_sha": "ddddeeeeffff",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "111122223333",
                        "marker": "scheduler-wrong-base",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_200,
                "message": json.dumps(
                    {
                        "event": "skills_selected",
                        "repo": "repo-target",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "222233334444",
                        "marker": "missing-base",
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_300,
                "message": json.dumps(
                    {
                        "event": "converse_started",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                        "marker": "matching",
                    }
                ),
            },
        ]
    )

    result = _service(
        codecommit=codecommit,
        logs=logs,
    ).progress("17", 1_000)

    assert [
        event["detail"]["marker"]
        for event in result["runtime_events"]
    ] == ["matching"]
    assert result["lambda_started"] is True
    assert result["done"] is False


def test_progress_preserves_zero_since_ms():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": "destination-snapshot",
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "runtime-log",
                "timestamp": 0,
                "message": json.dumps(
                    {
                        "event": "stage_plan",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "fedcba654321",
                    }
                ),
            }
        ]
    )

    def unexpected_now() -> float:
        raise AssertionError(
            "since_ms=0 must not call now"
        )

    result = _service(
        codecommit=codecommit,
        logs=logs,
        now=unexpected_now,
    ).progress("17", 0)

    assert result["lambda_started"] is True
    assert result["done"] is False
    assert result["payload"] is None
    assert [
        event["event"]
        for event in result["runtime_events"]
    ] == ["stage_plan"]
    assert [
        call[1]["startTime"]
        for call in logs.calls
        if call[0] == "filter_log_events"
    ] == [0, 0]


def test_progress_rejects_wrong_payload_and_latest_stream():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": "destination-snapshot",
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_100,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-other",
                        "base_sha": target_base,
                        "head_sha": target_head,
                        "diff_sha256": "fedcba654321",
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_200,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": (
                            "0000000000007890"
                            "abcdef1234567890abcdef12"
                        ),
                        "diff_sha256": "fedcba654321",
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_300,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": (
                            "ddddeeeeffff11112222333344445555"
                            "66667777"
                        ),
                        "head_sha": target_head,
                        "diff_sha256": "fedcba654321",
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_400,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "head_sha": target_head,
                        "diff_sha256": "fedcba654321",
                    }
                ),
            },
        ]
    )

    result = _service(
        codecommit=codecommit,
        logs=logs,
    ).progress("17", 1_000)

    assert result["lambda_started"] is False
    assert result["done"] is False
    assert result["payload"] is None
    assert result["runtime_events"] == []
    assert all(
        call[0] != "describe_log_streams"
        for call in logs.calls
    )


def test_progress_blocks_runtime_after_invalid_diff_payloads():
    target_base = (
        "aaaabbbbccccddddeeeeffff1111222233334444"
    )
    target_head = (
        "abcdef1234567890abcdef1234567890abcdef12"
    )
    codecommit = FakeCodeCommit(
        pull_requests={
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": target_base,
                        "sourceCommit": target_head,
                        "destinationCommit": (
                            "destination-snapshot"
                        ),
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            }
        }
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_100,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": target_head,
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_200,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": target_head,
                        "diff_sha256": "",
                    }
                ),
            },
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_300,
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-target",
                        "base_sha": target_base,
                        "head_sha": target_head,
                        "diff_sha256": 123,
                    }
                ),
            },
            {
                "logGroupName": "runtime-log",
                "timestamp": 1_400,
                "message": json.dumps(
                    {
                        "event": "stage_plan",
                        "repo": "repo-target",
                        "base_sha": "aaaabbbbcccc",
                        "head_sha": "abcdef123456",
                        "diff_sha256": "999999999999",
                        "marker": "arbitrary-diff",
                    }
                ),
            },
        ]
    )

    result = _service(
        codecommit=codecommit,
        logs=logs,
    ).progress("17", 1_000)

    assert result["payload"] is None
    assert result["done"] is False
    assert result["runtime_events"] == []
    assert result["lambda_started"] is False


def test_wiki_pr_detail_returns_every_allowed_text_diff():
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                "guides/page.md",
            ): b"old\nline\n",
            (
                "repo-wiki",
                "head",
                "guides/page.md",
            ): b"new\nline\n",
            (
                "repo-wiki",
                "base",
                "guides/page.json",
            ): b'{"value":"old"}',
            (
                "repo-wiki",
                "head",
                "guides/page.json",
            ): b'{"value":"new"}',
            (
                "repo-wiki",
                "base",
                "CURSOR.json",
            ): b'{"cursors":{"shaka-player":"old"}}',
            (
                "repo-wiki",
                "head",
                "CURSOR.json",
            ): b'{"cursors":{"shaka-player":"new"}}',
        },
        pull_requests={
            "91": {
                "pullRequestStatus": "OPEN",
                "title": "docs: update page",
                "pullRequestTargets": [
                    {
                        "destinationCommit": "base",
                        "sourceCommit": "head",
                        "sourceReference": (
                            "refs/heads/wikiagent/update"
                        ),
                    }
                ],
            }
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
            ): [
                {
                    "beforeBlob": {
                        "path": "guides/page.md",
                    },
                    "afterBlob": {
                        "path": "guides/page.md",
                    },
                    "changeType": "M",
                },
                {
                    "beforeBlob": {
                        "path": "guides/page.json",
                    },
                    "afterBlob": {
                        "path": "guides/page.json",
                    },
                    "changeType": "M",
                },
                {
                    "beforeBlob": {
                        "path": "CURSOR.json",
                    },
                    "afterBlob": {
                        "path": "CURSOR.json",
                    },
                    "changeType": "M",
                },
            ]
        },
    )

    result = _service(
        codecommit=codecommit,
    ).wiki_pr_detail("91")

    assert result["review_complete"] is True
    assert result["source_commit"] == "head"
    assert result["all_files"] == [
        "guides/page.md",
        "guides/page.json",
        "CURSOR.json",
    ]
    assert result["files"] == [
        {
            "path": "guides/page.md",
            "before": "old\nline\n",
            "after": "new\nline\n",
            "truncated": False,
            "unified": [
                "--- a/guides/page.md",
                "+++ b/guides/page.md",
                "@@ -1,2 +1,2 @@",
                "-old",
                "+new",
                " line",
            ],
        },
        {
            "path": "guides/page.json",
            "before": '{"value":"old"}',
            "after": '{"value":"new"}',
            "truncated": False,
            "unified": [
                "--- a/guides/page.json",
                "+++ b/guides/page.json",
                "@@ -1 +1 @@",
                '-{"value":"old"}',
                '+{"value":"new"}',
            ],
        },
        {
            "path": "CURSOR.json",
            "before": (
                '{"cursors":{"shaka-player":"old"}}'
            ),
            "after": (
                '{"cursors":{"shaka-player":"new"}}'
            ),
            "truncated": False,
            "unified": [
                "--- a/CURSOR.json",
                "+++ b/CURSOR.json",
                "@@ -1 +1 @@",
                (
                    '-{"cursors":'
                    '{"shaka-player":"old"}}'
                ),
                (
                    '+{"cursors":'
                    '{"shaka-player":"new"}}'
                ),
            ],
        },
    ]


def test_wiki_pr_detail_includes_every_allowed_deleted_text():
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                "guides/deleted.md",
            ): b"removed\nline\n",
            (
                "repo-wiki",
                "base",
                "guides/deleted.json",
            ): b'{"value":"removed"}',
        },
        pull_requests={
            "92": {
                "pullRequestStatus": "OPEN",
                "title": "docs: delete page",
                "pullRequestTargets": [
                    {
                        "destinationCommit": "base",
                        "sourceCommit": "head",
                        "sourceReference": (
                            "refs/heads/wikiagent/delete"
                        ),
                    }
                ],
            }
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
            ): [
                {
                    "beforeBlob": {
                        "path": "guides/deleted.md",
                    },
                    "changeType": "D",
                },
                {
                    "beforeBlob": {
                        "path": "guides/deleted.json",
                    },
                    "changeType": "D",
                },
            ]
        },
    )

    result = _service(
        codecommit=codecommit,
    ).wiki_pr_detail("92")

    assert result["review_complete"] is True
    assert result["source_commit"] == "head"
    assert result["all_files"] == [
        "guides/deleted.md",
        "guides/deleted.json",
    ]
    assert result["files"] == [
        {
            "path": "guides/deleted.md",
            "before": "removed\nline\n",
            "after": "",
            "truncated": False,
            "unified": [
                "--- a/guides/deleted.md",
                "+++ b/guides/deleted.md",
                "@@ -1,2 +0,0 @@",
                "-removed",
                "-line",
            ],
        },
        {
            "path": "guides/deleted.json",
            "before": '{"value":"removed"}',
            "after": "",
            "truncated": False,
            "unified": [
                "--- a/guides/deleted.json",
                "+++ b/guides/deleted.json",
                "@@ -1 +0,0 @@",
                '-{"value":"removed"}',
            ],
        },
    ]


@pytest.mark.parametrize(
    "missing_side",
    ["before", "after"],
)
def test_wiki_pr_detail_rejects_missing_declared_modification_blob(
    missing_side,
):
    files = {
        (
            "repo-wiki",
            "base",
            "guides/page.md",
        ): b"old\n",
        (
            "repo-wiki",
            "head",
            "guides/page.md",
        ): b"new\n",
    }
    files.pop(
        (
            "repo-wiki",
            "base" if missing_side == "before" else "head",
            "guides/page.md",
        )
    )
    codecommit = FakeCodeCommit(
        files=files,
        pull_requests={
            "91": _review_pull_request(),
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
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
            ]
        },
    )

    with pytest.raises(
        ValueError,
        match="Required repository content is unavailable",
    ):
        _service(
            codecommit=codecommit,
        ).wiki_pr_detail("91")

    assert all(
        name != "merge_pull_request_by_fast_forward"
        for name, _ in codecommit.calls
    )


def test_wiki_pr_detail_rejects_missing_declared_json_blob():
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                "guides/page.json",
            ): b'{"value":"old"}',
        },
        pull_requests={
            "91": _review_pull_request(),
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
            ): [
                {
                    "beforeBlob": {
                        "path": "guides/page.json",
                    },
                    "afterBlob": {
                        "path": "guides/page.json",
                    },
                    "changeType": "M",
                }
            ]
        },
    )

    with pytest.raises(
        ValueError,
        match="Required repository content is unavailable",
    ):
        _service(
            codecommit=codecommit,
        ).wiki_pr_detail("91")


@pytest.mark.parametrize(
    ("change_type", "before_blob", "after_blob"),
    [
        (
            "M",
            None,
            {"path": "guides/page.md"},
        ),
        (
            "M",
            {"path": "guides/page.md"},
            None,
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
def test_wiki_pr_detail_rejects_change_type_blob_mismatch(
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
        files={
            (
                "repo-wiki",
                "base",
                "guides/page.md",
            ): b"old\n",
            (
                "repo-wiki",
                "head",
                "guides/page.md",
            ): b"new\n",
        },
        pull_requests={
            "91": _review_pull_request(),
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
            ): [difference],
        },
    )

    with pytest.raises(
        ValueError,
        match="Wiki review is incomplete",
    ):
        _service(
            codecommit=codecommit,
        ).wiki_pr_detail("91")


def test_wiki_pr_detail_reads_rename_paths_independently():
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                "guides/old-name.md",
            ): b"old name\n",
            (
                "repo-wiki",
                "head",
                "guides/new-name.md",
            ): b"new name\n",
        },
        pull_requests={
            "91": _review_pull_request(),
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
            ): [
                {
                    "beforeBlob": {
                        "path": "guides/old-name.md",
                    },
                    "afterBlob": {
                        "path": "guides/new-name.md",
                    },
                    "changeType": "M",
                }
            ]
        },
    )

    result = _service(
        codecommit=codecommit,
    ).wiki_pr_detail("91")

    assert result["all_files"] == [
        "guides/old-name.md",
        "guides/new-name.md",
    ]
    assert [
        (
            item["path"],
            item["before"],
            item["after"],
        )
        for item in result["files"]
    ] == [
        (
            "guides/new-name.md",
            "old name\n",
            "new name\n",
        )
    ]
    assert [
        call
        for call in codecommit.calls
        if call[0] == "get_file"
    ] == [
        (
            "get_file",
            {
                "repositoryName": "repo-wiki",
                "commitSpecifier": "base",
                "filePath": "guides/old-name.md",
            },
        ),
        (
            "get_file",
            {
                "repositoryName": "repo-wiki",
                "commitSpecifier": "head",
                "filePath": "guides/new-name.md",
            },
        ),
    ]
    assert result["review_complete"] is True
    assert result["source_commit"] == "head"
    assert all(
        item["truncated"] is False
        for item in result["files"]
    )


def test_wiki_review_paginates_all_change_types_and_matches_approval():
    pages = {
        None: {
            "differences": [
                {
                    "beforeBlob": {
                        "path": "guides/visible.md",
                    },
                    "afterBlob": {
                        "path": "guides/visible.md",
                    },
                    "changeType": "M",
                }
            ],
            "NextToken": "page-2",
        },
        "page-2": {
            "differences": [
                {
                    "afterBlob": {
                        "path": "guides/added.md",
                    },
                    "changeType": "A",
                },
                {
                    "beforeBlob": {
                        "path": "guides/modified.md",
                    },
                    "afterBlob": {
                        "path": "guides/modified.md",
                    },
                    "changeType": "M",
                },
                {
                    "beforeBlob": {
                        "path": "guides/deleted.md",
                    },
                    "changeType": "D",
                },
                {
                    "beforeBlob": {
                        "path": "guides/old-name.md",
                    },
                    "afterBlob": {
                        "path": "guides/new-name.md",
                    },
                    "changeType": "M",
                },
            ]
        },
    }
    codecommit = PaginatedQueryCodeCommit(
        pages=pages,
        files={
            ("repo-wiki", "base", "guides/visible.md"):
                b"visible old\n",
            ("repo-wiki", "head", "guides/visible.md"):
                b"visible new\n",
            ("repo-wiki", "head", "guides/added.md"):
                b"added\n",
            ("repo-wiki", "base", "guides/modified.md"):
                b"modified old\n",
            ("repo-wiki", "head", "guides/modified.md"):
                b"modified new\n",
            ("repo-wiki", "base", "guides/deleted.md"):
                b"deleted\n",
            ("repo-wiki", "base", "guides/old-name.md"):
                b"old name\n",
            ("repo-wiki", "head", "guides/new-name.md"):
                b"new name\n",
        },
        pull_requests={
            "91": _review_pull_request(),
        },
    )

    review = _service(
        codecommit=codecommit,
    ).wiki_pr_detail("91")

    assert review["all_files"] == [
        "guides/visible.md",
        "guides/added.md",
        "guides/modified.md",
        "guides/deleted.md",
        "guides/old-name.md",
        "guides/new-name.md",
    ]
    assert [
        (
            item["path"],
            item["before"],
            item["after"],
        )
        for item in review["files"]
    ] == [
        (
            "guides/visible.md",
            "visible old\n",
            "visible new\n",
        ),
        ("guides/added.md", "", "added\n"),
        (
            "guides/modified.md",
            "modified old\n",
            "modified new\n",
        ),
        ("guides/deleted.md", "deleted\n", ""),
        (
            "guides/new-name.md",
            "old name\n",
            "new name\n",
        ),
    ]
    assert review["review_complete"] is True
    assert review["source_commit"] == "head"
    assert all(
        item["truncated"] is False
        for item in review["files"]
    )
    difference_calls = [
        call
        for call in codecommit.calls
        if call[0] == "get_differences"
    ]
    assert len(difference_calls) == 2
    assert difference_calls[1][1]["NextToken"] == "page-2"

    codecommit.calls.clear()
    action = ActionService(
        settings=_settings(),
        clients=AwsClients(
            codecommit=codecommit,
            logs=FakeLogs(),
            s3=FakeS3(),
        ),
        redactor=Redactor(),
    )
    approved = action.approve(
        wiki_pr="91",
        expected_source_commit=review["source_commit"],
        actor_sub=SAFE_ACTOR,
    )

    assert approved["ok"] is True
    assert [
        name
        for name, _ in codecommit.calls
        if name
        in {
            "get_differences",
            "merge_pull_request_by_fast_forward",
        }
    ] == [
        "get_differences",
        "get_differences",
        "merge_pull_request_by_fast_forward",
    ]


@pytest.mark.parametrize(
    "review_path",
    [
        "guides/large.md",
        "guides/large.json",
        "CURSOR.json",
    ],
    ids=["markdown", "json", "cursor"],
)
def test_wiki_review_truncation_blocks_recovered_direct_merge(
    review_path,
):
    before = "".join(
        f"old line {index}\n"
        for index in range(500)
    )
    after = "".join(
        (
            "HIDDEN_TAIL_MARKER\n"
            if index == 499
            else f"new line {index}\n"
        )
        for index in range(500)
    )
    source_base = "a" * 40
    source_head = "b" * 40
    source_diff = "c" * 64
    raw_payload = {
        "source": "event:pr#17",
        "repo": "repo-target",
        "base_sha": source_base,
        "head_sha": source_head,
        "diff_sha256": source_diff,
        "runtime": {
            "wiki": {
                "pull_request_id": "91",
                "commit_id": "head",
                "source_commit": "head",
            }
        },
    }
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                review_path,
            ): before.encode("utf-8"),
            (
                "repo-wiki",
                "head",
                review_path,
            ): after.encode("utf-8"),
        },
        pull_requests={
            "91": _review_pull_request(),
            "17": {
                "pullRequestId": "17",
                "pullRequestStatus": "OPEN",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-target",
                        "mergeBase": source_base,
                        "sourceCommit": source_head,
                        "destinationCommit": source_base,
                        "sourceReference": "refs/heads/demo/17",
                        "destinationReference": "refs/heads/main",
                    }
                ],
            },
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
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
            ]
        },
    )
    logs = FakeLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": 1_000,
                "message": json.dumps(raw_payload),
            }
        ]
    )
    query = _service(
        codecommit=codecommit,
        logs=logs,
    )

    review = query.wiki_pr_detail("91")

    assert review["review_complete"] is False
    assert review["source_commit"] is None
    assert review["files"][0]["truncated"] is True
    assert len(review["files"][0]["unified"]) == 400
    assert review["files"][0]["unified"][-1].startswith(
        "... diff truncated:"
    )
    assert all(
        "HIDDEN_TAIL_MARKER" not in line
        for line in review["files"][0]["unified"]
    )

    progress = query.progress("17", 0)
    assert progress["wiki_pull_request_id"] == "91"
    assert progress["payload"]["runtime"]["wiki"] == {
        "pull_request_id": "91",
    }
    assert raw_payload["runtime"]["wiki"] == {
        "pull_request_id": "91",
        "commit_id": "head",
        "source_commit": "head",
    }

    recovered_commit = raw_payload["runtime"]["wiki"][
        "commit_id"
    ]
    codecommit.calls.clear()
    action = ActionService(
        settings=_settings(),
        clients=AwsClients(
            codecommit=codecommit,
            logs=FakeLogs(),
            s3=FakeS3(),
        ),
        redactor=Redactor(),
    )

    with pytest.raises(
        ValueError,
        match="Wiki pull request review is incomplete",
    ):
        action.approve(
            wiki_pr="91",
            expected_source_commit=recovered_commit,
            actor_sub=SAFE_ACTOR,
        )

    assert all(
        name != "merge_pull_request_by_fast_forward"
        for name, _ in codecommit.calls
    )


def test_wiki_commit_rejects_missing_declared_blob():
    codecommit = FakeCodeCommit(
        files={
            (
                "repo-wiki",
                "base",
                "guides/page.md",
            ): b"old\n",
        },
        commits={
            (
                "repo-wiki",
                "head",
            ): {
                "commitId": "head",
                "message": "docs: update page",
                "author": {
                    "name": "automation",
                    "date": "2026-09-13T00:00:00+00:00",
                },
                "parents": ["base"],
            }
        },
        differences={
            (
                "repo-wiki",
                "base",
                "head",
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
            ]
        },
    )

    with pytest.raises(
        ValueError,
        match="Required repository content is unavailable",
    ):
        _service(
            codecommit=codecommit,
        ).wiki_commit("head")


def test_wiki_history_classifies_publish_and_finalize_commits():
    codecommit = FakeCodeCommit(
        branches={
            ("repo-wiki", "main"): "finalize",
        },
        commits={
            (
                "repo-wiki",
                "finalize",
            ): {
                "commitId": "finalize",
                "message": (
                    "chore(freshness): confirm wiki\n\n"
                    "source marketplace\n"
                    "wiki_merge 91\n"
                ),
                "author": {
                    "name": "automation",
                    "date": "2026-09-12T12:00:00+00:00",
                },
                "parents": ["publish"],
            },
            (
                "repo-wiki",
                "publish",
            ): {
                "commitId": "publish",
                "message": (
                    "docs: publish wiki page\n\n"
                    "source marketplace\n"
                    "source_pull_request 17\n"
                    "diff_sha256 abc123\n"
                ),
                "author": {
                    "name": "automation",
                    "date": "2026-09-12T11:59:00+00:00",
                },
                "parents": ["older"],
            },
        },
    )

    result = _service(
        codecommit=codecommit,
    ).wiki_history(2)

    assert result == {
        "head": "finalize",
        "commits": [
            {
                "commit": "finalize",
                "subject": (
                    "chore(freshness): confirm wiki"
                ),
                "kind": "확정",
                "author": "automation",
                "date": "2026-09-12T12:00:00+00:00",
                "meta": {
                    "source": "marketplace",
                    "wiki_merge": "91",
                },
                "parents": ["publish"],
            },
            {
                "commit": "publish",
                "subject": "docs: publish wiki page",
                "kind": "게시",
                "author": "automation",
                "date": "2026-09-12T11:59:00+00:00",
                "meta": {
                    "source": "marketplace",
                    "source_pull_request": "17",
                    "diff_sha256": "abc123",
                },
                "parents": ["older"],
            },
        ],
    }


def test_snapshot_reads_private_snapshot_object():
    s3 = FakeS3(
        {
            (
                "snapshot-bucket",
                "snapshot.json",
            ): json.dumps(
                {
                    "schema_version": 1,
                    "runs": [{"source": "event:pr#17"}],
                }
            ).encode("utf-8")
        }
    )

    result = _service(s3=s3).snapshot()

    assert result == {
        "schema_version": 1,
        "runs": [{"source": "event:pr#17"}],
    }
    assert s3.calls == [
        (
            "get_object",
            {
                "Bucket": "snapshot-bucket",
                "Key": "snapshot.json",
            },
        )
    ]


def test_query_response_rejects_unredacted_principal():
    s3 = FakeS3(
        {
            (
                "snapshot-bucket",
                "snapshot.json",
            ): json.dumps(
                {
                    "principal": (
                        "assumed-role/Admin/session-name"
                    )
                }
            ).encode("utf-8")
        }
    )

    with pytest.raises(RedactionError):
        _service(s3=s3).snapshot()
