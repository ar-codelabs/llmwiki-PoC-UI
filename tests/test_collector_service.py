from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from botocore.exceptions import ClientError

DEMO_UI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_UI))

import collect as collect_module  # noqa: E402
from backend import collector_service as collector_module  # noqa: E402
from backend.clients import AwsClients  # noqa: E402
from backend.collector_service import (  # noqa: E402
    SNAPSHOT_SCHEMA_VERSION,
    CollectorService,
)
from backend.redaction import Redactor  # noqa: E402
from backend.settings import Settings  # noqa: E402

FIXED_NOW = datetime(2026, 9, 13, 1, 2, 3, tzinfo=UTC)
READ_ERROR = "Required repository content is unavailable"
SNAPSHOT_ERROR = "Snapshot contract is invalid"


class FileDoesNotExistException(Exception):
    pass


class CommitDoesNotExistException(Exception):
    pass


class PathDoesNotExistException(Exception):
    pass


class CollectorCodeCommit:
    def __init__(
        self,
        *,
        branches: dict[tuple[str, str], str] | None = None,
        commits: dict[tuple[str, str], dict] | None = None,
        folders: dict[tuple[str, ...], dict] | None = None,
        files: dict[
            tuple[str, str, str],
            object,
        ] | None = None,
        listed_branches: dict[str, list[str]] | None = None,
        pull_request_ids: (
            dict[tuple[str, str], list[str]] | None
        ) = None,
        pull_requests: dict[str, dict] | None = None,
        repositories: list[str] | None = None,
        differences: (
            dict[tuple[str, str, str], list[dict]] | None
        ) = None,
    ) -> None:
        self.branches = dict(branches or {})
        self.commits = dict(commits or {})
        self.folders = dict(folders or {})
        self.files = dict(files or {})
        self.listed_branches = {
            key: list(value)
            for key, value in (listed_branches or {}).items()
        }
        self.pull_request_ids = {
            key: list(value)
            for key, value in (pull_request_ids or {}).items()
        }
        self.pull_requests = dict(pull_requests or {})
        self.repositories = list(repositories or [])
        self.differences = {
            key: list(value)
            for key, value in (differences or {}).items()
        }
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, dict(kwargs)))

    def get_branch(self, **kwargs) -> dict:
        self._record("get_branch", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["branchName"],
        )
        return {"branch": {"commitId": self.branches[key]}}

    def get_commit(self, **kwargs) -> dict:
        self._record("get_commit", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["commitId"],
        )
        return {"commit": self.commits[key]}

    def get_folder(self, **kwargs) -> dict:
        self._record("get_folder", kwargs)
        versioned_key = (
            kwargs["repositoryName"],
            kwargs.get("commitSpecifier"),
            kwargs["folderPath"],
        )
        if versioned_key in self.folders:
            return self.folders[versioned_key]
        unversioned_key = (
            kwargs["repositoryName"],
            kwargs["folderPath"],
        )
        return self.folders[unversioned_key]

    def get_file(self, **kwargs) -> dict:
        self._record("get_file", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["commitSpecifier"],
            kwargs["filePath"],
        )
        value = self.files[key]
        if isinstance(value, BaseException):
            raise value
        return {"fileContent": value}

    def list_branches(self, **kwargs) -> dict:
        self._record("list_branches", kwargs)
        return {
            "branches": list(
                self.listed_branches[
                    kwargs["repositoryName"]
                ]
            )
        }

    def list_pull_requests(self, **kwargs) -> dict:
        self._record("list_pull_requests", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["pullRequestStatus"],
        )
        return {
            "pullRequestIds": list(
                self.pull_request_ids.get(key, [])
            )
        }

    def get_pull_request(self, **kwargs) -> dict:
        self._record("get_pull_request", kwargs)
        return {
            "pullRequest": self.pull_requests[
                kwargs["pullRequestId"]
            ]
        }

    def list_repositories(self, **kwargs) -> dict:
        self._record("list_repositories", kwargs)
        return {
            "repositories": [
                {"repositoryName": name}
                for name in self.repositories
            ]
        }

    def get_differences(self, **kwargs) -> dict:
        self._record("get_differences", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["beforeCommitSpecifier"],
            kwargs["afterCommitSpecifier"],
        )
        return {
            "differences": list(
                self.differences.get(key, [])
            )
        }


class MovingMainCodeCommit(CollectorCodeCommit):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_wiki_head = "h1"

    def get_branch(self, **kwargs) -> dict:
        self._record("get_branch", kwargs)
        if (
            kwargs["repositoryName"] == "repo-wiki"
            and kwargs["branchName"] == "main"
        ):
            return {
                "branch": {
                    "commitId": self.current_wiki_head
                }
            }
        return super().get_branch(**kwargs)

    def get_folder(self, **kwargs) -> dict:
        self._record("get_folder", kwargs)
        requested = kwargs.get("commitSpecifier")
        resolved = requested or self.current_wiki_head
        key = (
            kwargs["repositoryName"],
            resolved,
            kwargs["folderPath"],
        )
        result = self.folders[key]
        if (
            requested is None
            and kwargs["folderPath"] == "/"
        ):
            self.current_wiki_head = "h2"
        return result

    def get_file(self, **kwargs) -> dict:
        self._record("get_file", kwargs)
        requested = kwargs["commitSpecifier"]
        resolved = (
            self.current_wiki_head
            if requested == "refs/heads/main"
            else requested
        )
        key = (
            kwargs["repositoryName"],
            resolved,
            kwargs["filePath"],
        )
        value = self.files[key]
        if isinstance(value, BaseException):
            raise value
        return {"fileContent": value}


class CollectorLogs:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = [dict(event) for event in events or []]
        self.calls: list[tuple[str, dict]] = []

    def filter_log_events(self, **kwargs) -> dict:
        self.calls.append(
            ("filter_log_events", dict(kwargs))
        )
        return {
            "events": [
                {
                    key: value
                    for key, value in event.items()
                    if key != "logGroupName"
                }
                for event in self.events
                if event["logGroupName"]
                == kwargs["logGroupName"]
                and event["timestamp"]
                >= kwargs["startTime"]
            ]
        }


class CollectorS3:
    def __init__(
        self,
        objects: list[dict] | None = None,
    ) -> None:
        self.objects = [dict(item) for item in objects or []]
        self.calls: list[tuple[str, dict]] = []
        self.put_bodies: list[bytes] = []

    def list_objects_v2(self, **kwargs) -> dict:
        self.calls.append(
            ("list_objects_v2", dict(kwargs))
        )
        return {
            "Contents": [
                dict(item)
                for item in self.objects
            ]
        }

    def put_object(self, **kwargs) -> dict:
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.put_bodies.append(body)
        self.calls.append(
            (
                "put_object",
                {
                    key: value
                    for key, value in kwargs.items()
                    if key != "Body"
                },
            )
        )
        return {"ETag": '"private-etag"'}


class RecordingExecutor:
    # ⚠️ 클래스 속성인 것이 의도다. 인스턴스로 내리면 안 된다.
    #    시험이 `RecordingExecutor.workers` 를 클래스에서 직접 읽는다(`clear()` · `== [1]`).
    workers: ClassVar[list[int]] = []

    def __init__(self, *, max_workers: int) -> None:
        self.workers.append(max_workers)

    def __enter__(self) -> RecordingExecutor:
        return self

    def __exit__(self, *args) -> None:
        return None

    def map(self, operation, values):
        return [
            operation(value)
            for value in values
        ]


def _settings() -> Settings:
    return Settings(
        region="test-region-1",
        wiki_repo="repo-wiki",
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
        source_repositories=("repo-alpha",),
    )


def _valid_snapshot() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-13T01:02:03+00:00",
        "region": "test-region-1",
        "runs": [],
        "wiki": {},
        "bootstrap": {},
        "skills": [],
        "repos": [],
    }


def _frontmatter(index: int, *, status: str = "current") -> bytes:
    return (
        "---\n"
        "type: guide\n"
        f"status: {status}\n"
        "stale_after: '2026-12-31'\n"
        f"- path: src/file-{index:02d}.py\n"
        f"  at: '2026-09-13T00:{index:02d}:00+00:00'\n"
        "pull_request_id: 17\n"
        "---\n"
        f"# Page {index}\n"
    ).encode()


def _collection_clients(
    *,
    document_count: int = 60,
) -> tuple[
    CollectorCodeCommit,
    CollectorLogs,
    CollectorS3,
]:
    top_count = document_count // 2
    top_pages = [
        f"guides/page-{index:02d}.md"
        for index in range(top_count)
    ]
    deep_pages = [
        f"guides/deep/page-{index:02d}.md"
        for index in range(top_count, document_count)
    ]
    top_json = [
        path.removesuffix(".md") + ".json"
        for path in top_pages
    ]
    deep_json = [
        path.removesuffix(".md") + ".json"
        for path in deep_pages
    ]
    files: dict[tuple[str, str, str], object] = {
        (
            "repo-wiki",
            "refs/heads/main",
            "index.md",
        ): b"# Index\n",
        (
            "repo-wiki",
            "refs/heads/main",
            "CURSOR.json",
        ): b'{"cursors":{"appsync":"source-head"}}',
    }
    for index, path in enumerate(
        top_pages + deep_pages
    ):
        files[
            (
                "repo-wiki",
                "refs/heads/main",
                path,
            )
        ] = _frontmatter(index)
    for (
        repository,
        _,
        file_path,
    ), value in list(files.items()):
        files[
            (
                repository,
                "wiki-head",
                file_path,
            )
        ] = value

    codecommit = CollectorCodeCommit(
        branches={
            ("repo-wiki", "main"): "wiki-head",
            ("repo-alpha", "main"): "alpha-head",
        },
        commits={
            (
                "repo-wiki",
                "wiki-head",
            ): {
                "commitId": "wiki-head",
                "message": "docs: refresh wiki\n\nbody",
                "author": {"name": "automation"},
                "parents": ["wiki-parent"],
            }
        },
        folders={
            (
                "repo-wiki",
                "/",
            ): {
                "files": [
                    {"absolutePath": "index.md"},
                    {"absolutePath": "CURSOR.json"},
                ],
                "subFolders": [
                    {"absolutePath": "/guides"}
                ],
            },
            (
                "repo-wiki",
                "/guides",
            ): {
                "files": [
                    *(
                        {"absolutePath": path}
                        for path in top_pages
                    ),
                    *(
                        {"absolutePath": path}
                        for path in top_json
                    ),
                ],
                "subFolders": [
                    {"absolutePath": "/guides/deep"}
                ],
            },
            (
                "repo-wiki",
                "/guides/deep",
            ): {
                "files": [
                    *(
                        {"absolutePath": path}
                        for path in deep_pages
                    ),
                    *(
                        {"absolutePath": path}
                        for path in deep_json
                    ),
                ],
                "subFolders": [],
            },
        },
        files=files,
        listed_branches={
            "repo-wiki": ["main"],
        },
        pull_request_ids={
            ("repo-wiki", "CLOSED"): [],
            ("repo-wiki", "OPEN"): [],
            ("repo-alpha", "OPEN"): ["17"],
        },
        repositories=["repo-wiki", "repo-alpha"],
    )
    logs = CollectorLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": int(
                    (
                        FIXED_NOW
                        - timedelta(minutes=1)
                    ).timestamp()
                    * 1000
                ),
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-alpha",
                        "diff_sha256": "diff-17",
                    }
                ),
            }
        ]
    )
    s3 = CollectorS3(
        [
            {
                "Key": "guide/SKILL.md",
                "Size": 123,
                "LastModified": datetime(
                    2026,
                    9,
                    12,
                    12,
                    0,
                    tzinfo=UTC,
                ),
            }
        ]
    )
    return codecommit, logs, s3


def _service(
    *,
    codecommit: CollectorCodeCommit | None = None,
    logs: CollectorLogs | None = None,
    s3: CollectorS3 | None = None,
    redactor: Redactor | None = None,
) -> CollectorService:
    return CollectorService(
        settings=_settings(),
        clients=AwsClients(
            codecommit=codecommit or CollectorCodeCommit(),
            logs=logs or CollectorLogs(),
            s3=s3 or CollectorS3(),
        ),
        redactor=redactor or Redactor(),
        now=lambda: FIXED_NOW,
    )


def test_collect_repos_iterates_exact_configuration_without_discovery():
    codecommit = CollectorCodeCommit(
        branches={
            ("shaka-player", "main"): "shaka-head",
            ("mediamtx", "main"): "mediamtx-head",
            ("saleor", "main"): "saleor-head",
            ("private-repository", "main"): "private-head",
        },
        pull_request_ids={
            ("shaka-player", "OPEN"): ["1"],
            ("mediamtx", "OPEN"): [],
            ("saleor", "OPEN"): ["2", "3"],
            ("private-repository", "OPEN"): ["9"],
        },
        repositories=[],
    )
    service = _service(codecommit=codecommit)
    service.settings = SimpleNamespace(
        source_repositories=(
            "shaka-player",
            "mediamtx",
            "saleor",
        ),
    )

    assert service._collect_repos() == [
        {
            "repo": "shaka-player",
            "head": "shaka-head",
            "open_prs": 1,
        },
        {
            "repo": "mediamtx",
            "head": "mediamtx-head",
            "open_prs": 0,
        },
        {
            "repo": "saleor",
            "head": "saleor-head",
            "open_prs": 2,
        },
    ]
    assert all(
        kwargs.get("repositoryName") != "private-repository"
        for _, kwargs in codecommit.calls
    )
    assert all(
        name != "list_repositories"
        for name, _ in codecommit.calls
    )


def test_snapshot_has_schema_generated_at_runs_wiki_skills_repos():
    codecommit, logs, s3 = _collection_clients()

    snapshot = _service(
        codecommit=codecommit,
        logs=logs,
        s3=s3,
    ).build_snapshot()

    assert snapshot["schema_version"] == (
        SNAPSHOT_SCHEMA_VERSION
    )
    assert snapshot["generated_at"] == (
        "2026-09-13T01:02:03+00:00"
    )
    assert snapshot["region"] == "test-region-1"
    assert [run["source"] for run in snapshot["runs"]] == [
        "event:pr#17"
    ]
    assert snapshot["wiki"]["total_md"] == 60
    assert snapshot["wiki"]["total_json"] == 61
    assert snapshot["wiki"]["pair_ok"] == 60
    assert len(snapshot["wiki"]["docs"]) == 60
    assert snapshot["wiki"]["docs_truncated"] is False
    assert sum(
        item["total"]
        for item in snapshot["wiki"]["by_top"].values()
    ) == 60
    assert snapshot["bootstrap"] == {
        "branches": [],
        "pull_requests": [],
    }
    assert snapshot["skills"] == [
        {
            "name": "guide",
            "key": "guide/SKILL.md",
            "size": 123,
            "modified": "2026-09-12T12:00:00+00:00",
        }
    ]
    assert snapshot["repos"] == [
        {
            "repo": "repo-alpha",
            "head": "alpha-head",
            "open_prs": 1,
        }
    ]
    wiki_folder_calls = [
        kwargs
        for name, kwargs in codecommit.calls
        if name == "get_folder"
        and kwargs["repositoryName"] == "repo-wiki"
    ]
    assert wiki_folder_calls
    assert {
        kwargs.get("commitSpecifier")
        for kwargs in wiki_folder_calls
    } == {"wiki-head"}
    wiki_file_calls = [
        kwargs
        for name, kwargs in codecommit.calls
        if name == "get_file"
        and kwargs["repositoryName"] == "repo-wiki"
    ]
    assert wiki_file_calls
    assert {
        kwargs["commitSpecifier"]
        for kwargs in wiki_file_calls
    } == {"wiki-head"}


def test_full_collection_uses_one_wiki_read_worker_to_avoid_throttling(
    monkeypatch,
):
    codecommit, logs, s3 = _collection_clients(
        document_count=2,
    )
    RecordingExecutor.workers.clear()
    monkeypatch.setattr(
        collector_module,
        "ThreadPoolExecutor",
        RecordingExecutor,
    )

    snapshot = _service(
        codecommit=codecommit,
        logs=logs,
        s3=s3,
    ).build_snapshot()

    assert snapshot["wiki"]["total_md"] == 2
    assert RecordingExecutor.workers == [1]


def test_full_collection_pins_tree_and_content_to_captured_head():
    codecommit = MovingMainCodeCommit(
        branches={
            ("repo-alpha", "main"): "alpha-head",
        },
        commits={
            (
                "repo-wiki",
                "h1",
            ): {
                "commitId": "h1",
                "message": "docs: h1\n",
                "author": {"name": "automation"},
                "parents": [],
            },
        },
        folders={
            (
                "repo-wiki",
                "h1",
                "/",
            ): {
                "files": [
                    {"absolutePath": "index.md"},
                    {"absolutePath": "CURSOR.json"},
                ],
                "subFolders": [
                    {"absolutePath": "/a"}
                ],
            },
            (
                "repo-wiki",
                "h1",
                "/a",
            ): {
                "files": [
                    {"absolutePath": "a/old.md"}
                ],
                "subFolders": [],
            },
            (
                "repo-wiki",
                "h2",
                "/",
            ): {
                "files": [
                    {"absolutePath": "index.md"},
                    {"absolutePath": "CURSOR.json"},
                ],
                "subFolders": [
                    {"absolutePath": "/a"},
                    {"absolutePath": "/b"},
                ],
            },
            (
                "repo-wiki",
                "h2",
                "/a",
            ): {
                "files": [
                    {"absolutePath": "a/new.md"}
                ],
                "subFolders": [],
            },
            (
                "repo-wiki",
                "h2",
                "/b",
            ): {
                "files": [
                    {"absolutePath": "b/extra.md"}
                ],
                "subFolders": [],
            },
        },
        files={
            (
                "repo-wiki",
                "h1",
                "index.md",
            ): b"# h1 index\n",
            (
                "repo-wiki",
                "h1",
                "CURSOR.json",
            ): b'{"cursors":{"alpha":"h1-source"}}',
            (
                "repo-wiki",
                "h1",
                "a/old.md",
            ): _frontmatter(1),
            (
                "repo-wiki",
                "h2",
                "index.md",
            ): b"# h2 index\n",
            (
                "repo-wiki",
                "h2",
                "CURSOR.json",
            ): b'{"cursors":{"alpha":"h2-source"}}',
            (
                "repo-wiki",
                "h2",
                "a/new.md",
            ): _frontmatter(2),
            (
                "repo-wiki",
                "h2",
                "b/extra.md",
            ): _frontmatter(3),
        },
        listed_branches={"repo-wiki": ["main"]},
        pull_request_ids={
            ("repo-wiki", "CLOSED"): [],
            ("repo-wiki", "OPEN"): [],
            ("repo-alpha", "OPEN"): [],
        },
        repositories=["repo-wiki"],
    )
    s3 = CollectorS3()

    _service(
        codecommit=codecommit,
        s3=s3,
    ).collect_and_write()

    written = json.loads(
        s3.put_bodies[-1].decode("utf-8")
    )
    assert written["wiki"]["head"] == "h1"
    assert {
        item["path"]
        for item in written["wiki"]["docs"]
    } == {"a/old.md"}
    assert written["wiki"]["cursors"] == {
        "alpha": "h1-source"
    }
    folder_calls = [
        kwargs
        for name, kwargs in codecommit.calls
        if name == "get_folder"
    ]
    assert {
        kwargs.get("commitSpecifier")
        for kwargs in folder_calls
    } == {"h1"}
    wiki_file_calls = [
        kwargs
        for name, kwargs in codecommit.calls
        if name == "get_file"
        and kwargs["repositoryName"] == "repo-wiki"
    ]
    assert {
        kwargs["commitSpecifier"]
        for kwargs in wiki_file_calls
    } == {"h1"}


def test_v2_pair_detection_uses_meta_tree_and_skips_navigation():
    codecommit, _, _ = _collection_clients(document_count=1)
    path = (
        "repositories/shaka-player/en/components/"
        "networking.md"
    )
    metadata = (
        "_meta/repositories/shaka-player/en/components/"
        "networking.json"
    )
    codecommit.folders[("repo-wiki", "/")]["subFolders"].extend(
        [
            {"absolutePath": "/repositories"},
            {"absolutePath": "/_meta"},
        ]
    )
    codecommit.folders[("repo-wiki", "/repositories")] = {
        "files": [
            {
                "absolutePath": (
                    "repositories/shaka-player/README.md"
                )
            }
        ],
        "subFolders": [
            {"absolutePath": "/repositories/shaka-player"}
        ],
    }
    codecommit.folders[
        ("repo-wiki", "/repositories/shaka-player")
    ] = {
        "files": [],
        "subFolders": [
            {
                "absolutePath": (
                    "/repositories/shaka-player/en"
                )
            }
        ],
    }
    codecommit.folders[
        ("repo-wiki", "/repositories/shaka-player/en")
    ] = {
        "files": [],
        "subFolders": [
            {
                "absolutePath": (
                    "/repositories/shaka-player/en/components"
                )
            }
        ],
    }
    codecommit.folders[
        (
            "repo-wiki",
            "/repositories/shaka-player/en/components",
        )
    ] = {
        "files": [{"absolutePath": path}],
        "subFolders": [],
    }
    codecommit.folders[("repo-wiki", "/_meta")] = {
        "files": [],
        "subFolders": [
            {"absolutePath": "/_meta/repositories"}
        ],
    }
    codecommit.folders[
        ("repo-wiki", "/_meta/repositories")
    ] = {
        "files": [],
        "subFolders": [
            {
                "absolutePath": (
                    "/_meta/repositories/shaka-player"
                )
            }
        ],
    }
    codecommit.folders[
        ("repo-wiki", "/_meta/repositories/shaka-player")
    ] = {
        "files": [],
        "subFolders": [
            {
                "absolutePath": (
                    "/_meta/repositories/shaka-player/en"
                )
            }
        ],
    }
    codecommit.folders[
        ("repo-wiki", "/_meta/repositories/shaka-player/en")
    ] = {
        "files": [],
        "subFolders": [
            {
                "absolutePath": (
                    "/_meta/repositories/shaka-player/en/components"
                )
            }
        ],
    }
    codecommit.folders[
        (
            "repo-wiki",
            "/_meta/repositories/shaka-player/en/components",
        )
    ] = {
        "files": [{"absolutePath": metadata}],
        "subFolders": [],
    }
    codecommit.files[("repo-wiki", "wiki-head", path)] = (
        _frontmatter(9)
    )
    codecommit.files[
        (
            "repo-wiki",
            "wiki-head",
            "repositories/shaka-player/README.md",
        )
    ] = b"# Repository\n"

    wiki = _service(codecommit=codecommit)._collect_wiki()
    detail = next(
        item
        for item in wiki["docs"]
        if item["path"] == path
    )

    assert detail["metadata_path"] == metadata
    assert detail["has_json_pair"] is True
    assert all(
        item["path"]
        != "repositories/shaka-player/README.md"
        for item in wiki["docs"]
    )


def test_run_pair_mapping_is_backend_supplied_for_v1_and_v2():
    runs = [
        {
            "runtime": {
                "wiki": {
                    "files": [
                        "spec/legacy.md",
                        "spec/legacy.json",
                        (
                            "repositories/shaka-player/en/components/"
                            "networking.md"
                        ),
                        (
                            "_meta/repositories/shaka-player/en/components/"
                            "networking.json"
                        ),
                    ]
                }
            }
        }
    ]

    CollectorService._attach_pairs(runs)

    assert runs[0]["runtime"]["wiki"]["pairs"] == [
        {
            "markdown": (
                "repositories/shaka-player/en/components/"
                "networking.md"
            ),
            "metadata": (
                "_meta/repositories/shaka-player/en/components/"
                "networking.json"
            ),
        },
        {
            "markdown": "spec/legacy.md",
            "metadata": "spec/legacy.json",
        },
    ]


def test_collector_preserves_fast_history_merge_contract():
    duplicate_at = (
        FIXED_NOW - timedelta(minutes=10)
    ).isoformat(timespec="seconds")
    outside_window_at = (
        FIXED_NOW - timedelta(hours=8)
    ).isoformat(timespec="seconds")
    previous = {
        "schema_version": 1,
        "generated_at": "2026-09-12T00:00:00+00:00",
        "region": "test-region-1",
        "runs": [
            {
                "source": "event:pr#old",
                "repo": "repo-alpha",
                "diff_sha256": "old-diff",
                "_at": outside_window_at,
                "marker": "preserve",
            },
            {
                "source": "event:pr#17",
                "repo": "repo-alpha",
                "diff_sha256": "same-diff",
                "_at": duplicate_at,
                "marker": "stale",
            },
        ],
        "wiki": {
            "repo": "repo-wiki",
            "head": "old-head",
            "head_message": "old",
            "head_author": "old",
            "total_md": 7,
            "total_json": 6,
            "pair_ok": 6,
            "by_top": {
                "guides": {
                    "total": 7,
                    "current": 6,
                    "unverified": 1,
                    "stale": 0,
                    "other": 0,
                }
            },
            "by_status": {
                "current": 6,
                "unverified": 1,
            },
            "docs": [
                {
                    "path": "guides/page-00.md",
                    "status": "unverified",
                }
            ],
            "docs_truncated": False,
            "cursors": {"alpha": "old-source"},
            "has_index_md": True,
        },
        "bootstrap": {"branches": ["keep"], "pull_requests": []},
        "skills": [{"name": "keep"}],
        "repos": [{"repo": "keep"}],
    }
    codecommit = CollectorCodeCommit(
        branches={
            ("repo-wiki", "main"): "new-head",
        },
        commits={
            (
                "repo-wiki",
                "new-head",
            ): {
                "message": "docs: new head\n\nbody",
                "author": {"name": "automation"},
                "parents": ["old-head"],
            }
        },
        files={
            (
                "repo-wiki",
                "refs/heads/main",
                "CURSOR.json",
            ): b'{"cursors":{"alpha":"new-source"}}',
            (
                "repo-wiki",
                "new-head",
                "CURSOR.json",
            ): b'{"cursors":{"alpha":"new-source"}}',
            (
                "repo-wiki",
                "refs/heads/main",
                "guides/page-00.md",
            ): _frontmatter(0, status="current"),
            (
                "repo-wiki",
                "new-head",
                "guides/page-00.md",
            ): _frontmatter(0, status="current"),
        },
    )
    logs = CollectorLogs(
        [
            {
                "logGroupName": "lambda-log",
                "timestamp": int(
                    datetime.fromisoformat(
                        duplicate_at
                    ).timestamp()
                    * 1000
                ),
                "message": json.dumps(
                    {
                        "source": "event:pr#17",
                        "repo": "repo-alpha",
                        "diff_sha256": "same-diff",
                        "marker": "fresh",
                    }
                ),
            }
        ]
    )

    snapshot = _service(
        codecommit=codecommit,
        logs=logs,
    ).build_fast_snapshot(previous, hours=6)

    assert [
        run["source"]
        for run in snapshot["runs"]
    ] == ["event:pr#17", "event:pr#old"]
    assert snapshot["runs"][0]["marker"] == "fresh"
    assert snapshot["runs"][1]["marker"] == "preserve"
    assert snapshot["wiki"]["head"] == "new-head"
    assert snapshot["wiki"]["total_md"] == 7
    assert snapshot["wiki"]["fast"] is True
    assert snapshot["wiki"]["docs"][0]["status"] == "current"
    assert snapshot["bootstrap"] == previous["bootstrap"]
    assert snapshot["skills"] == previous["skills"]
    assert snapshot["repos"] == previous["repos"]
    assert previous["runs"][1]["marker"] == "stale"
    assert logs.calls[0][1]["startTime"] == int(
        (
            FIXED_NOW - timedelta(hours=6)
        ).timestamp()
        * 1000
    )
    wiki_file_calls = [
        kwargs
        for name, kwargs in codecommit.calls
        if name == "get_file"
        and kwargs["repositoryName"] == "repo-wiki"
    ]
    assert {
        kwargs["commitSpecifier"]
        for kwargs in wiki_file_calls
    } == {"new-head"}


def test_collector_rejects_read_failures_before_s3_write():
    for failure in (
        RuntimeError("transient read failure"),
        b"\xff",
    ):
        codecommit, logs, s3 = _collection_clients(
            document_count=2,
        )
        codecommit.files[
            (
                "repo-wiki",
                "wiki-head",
                "guides/page-00.md",
            )
        ] = failure

        with pytest.raises(
            ValueError,
            match=f"^{READ_ERROR}$",
        ):
            _service(
                codecommit=codecommit,
                logs=logs,
                s3=s3,
            ).collect_and_write()

        assert [
            call
            for call in s3.calls
            if call[0] == "put_object"
        ] == []


def test_optional_missing_accepts_only_file_missing_contracts():
    missing_errors = (
        FileDoesNotExistException("missing file"),
        ClientError(
            {
                "Error": {
                    "Code": "FileDoesNotExistException",
                    "Message": "missing file",
                }
            },
            "GetFile",
        ),
    )
    for missing_error in missing_errors:
        codecommit, logs, s3 = _collection_clients(
            document_count=2,
        )
        codecommit.files[
            (
                "repo-wiki",
                "wiki-head",
                "CURSOR.json",
            )
        ] = missing_error

        _service(
            codecommit=codecommit,
            logs=logs,
            s3=s3,
        ).collect_and_write()

        assert len(s3.put_bodies) == 1
        written = json.loads(
            s3.put_bodies[0].decode("utf-8")
        )
        assert written["wiki"]["cursors"] == {}


def test_optional_missing_rejects_non_file_missing_errors():
    rejected_errors = (
        CommitDoesNotExistException(
            "missing commit"
        ),
        PathDoesNotExistException("missing path"),
        KeyError("malformed response"),
        RuntimeError("transport failure"),
        ClientError(
            {
                "Error": {
                    "Code": "CommitDoesNotExistException",
                    "Message": "missing commit",
                }
            },
            "GetFile",
        ),
        ClientError(
            {
                "Error": {
                    "Code": "PathDoesNotExistException",
                    "Message": "missing path",
                }
            },
            "GetFile",
        ),
    )
    for rejected_error in rejected_errors:
        codecommit, logs, s3 = _collection_clients(
            document_count=2,
        )
        codecommit.files[
            (
                "repo-wiki",
                "wiki-head",
                "CURSOR.json",
            )
        ] = rejected_error

        with pytest.raises(
            ValueError,
            match=f"^{READ_ERROR}$",
        ):
            _service(
                codecommit=codecommit,
                logs=logs,
                s3=s3,
            ).collect_and_write()

        assert [
            call
            for call in s3.calls
            if call[0] == "put_object"
        ] == []


def test_collector_redacts_before_put_object():
    s3 = CollectorS3()
    raw_snapshot = _valid_snapshot()
    raw_snapshot["principal"] = (
        "arn:aws:sts::123456789012:"
        "assumed-role/Admin/private-alias"
    )

    _service(
        s3=s3,
        redactor=Redactor(
            (("private-alias", "<person>"),)
        ),
    ).write_snapshot(raw_snapshot)

    rendered = s3.put_bodies[-1].decode("utf-8")
    assert "123456789012" not in rendered
    assert "assumed-role" not in rendered
    assert "private-alias" not in rendered
    assert json.loads(rendered)["principal"] == "<redacted>"
    assert "private-alias" in raw_snapshot["principal"]


def test_collector_writes_versioned_snapshot_key():
    s3 = CollectorS3()
    snapshot = _valid_snapshot()

    metadata = _service(s3=s3).write_snapshot(snapshot)

    assert s3.calls[-1] == (
        "put_object",
        {
            "Bucket": "snapshot-bucket",
            "Key": "snapshot.json",
            "ContentType": "application/json",
            "CacheControl": "no-store, max-age=0",
        },
    )
    body = json.loads(s3.put_bodies[-1].decode("utf-8"))
    assert body["schema_version"] == 1
    assert body["generated_at"] == (
        "2026-09-13T01:02:03+00:00"
    )
    assert metadata == {
        "key": "snapshot.json",
        "schema_version": 1,
        "generated_at": "2026-09-13T01:02:03+00:00",
        "bytes": len(s3.put_bodies[-1]),
    }


def test_collector_rejects_invalid_raw_snapshot_contract():
    invalid_snapshots = []

    wrong_version = _valid_snapshot()
    wrong_version["schema_version"] = 2
    invalid_snapshots.append(wrong_version)

    boolean_version = _valid_snapshot()
    boolean_version["schema_version"] = True
    invalid_snapshots.append(boolean_version)

    invalid_timestamp = _valid_snapshot()
    invalid_timestamp["generated_at"] = "not-an-iso-timestamp"
    invalid_snapshots.append(invalid_timestamp)

    naive_timestamp = _valid_snapshot()
    naive_timestamp["generated_at"] = (
        "2026-09-13T01:02:03"
    )
    invalid_snapshots.append(naive_timestamp)

    for field in (
        "schema_version",
        "generated_at",
        "region",
        "runs",
        "wiki",
        "bootstrap",
        "skills",
        "repos",
    ):
        missing = _valid_snapshot()
        missing.pop(field)
        invalid_snapshots.append(missing)

    for field, invalid_value in (
        ("region", 1),
        ("runs", {}),
        ("wiki", []),
        ("bootstrap", []),
        ("skills", {}),
        ("repos", {}),
    ):
        wrong_type = _valid_snapshot()
        wrong_type[field] = invalid_value
        invalid_snapshots.append(wrong_type)

    for invalid_snapshot in invalid_snapshots:
        s3 = CollectorS3()
        service = _service(s3=s3)

        with pytest.raises(
            ValueError,
            match=f"^{SNAPSHOT_ERROR}$",
        ):
            service.serialize_snapshot(invalid_snapshot)
        with pytest.raises(
            ValueError,
            match=f"^{SNAPSHOT_ERROR}$",
        ):
            service.write_snapshot(invalid_snapshot)

        assert [
            call
            for call in s3.calls
            if call[0] == "put_object"
        ] == []


def test_collector_rejects_redacted_snapshot_key_change():
    s3 = CollectorS3()
    service = _service(
        s3=s3,
        redactor=Redactor((("runs", "history"),)),
    )
    snapshot = _valid_snapshot()

    with pytest.raises(
        ValueError,
        match=f"^{SNAPSHOT_ERROR}$",
    ):
        service.serialize_snapshot(snapshot)
    with pytest.raises(
        ValueError,
        match=f"^{SNAPSHOT_ERROR}$",
    ):
        service.write_snapshot(snapshot)

    assert [
        call
        for call in s3.calls
        if call[0] == "put_object"
    ] == []


def test_local_snapshot_write_rejects_invalid_contract(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "snapshot.json"
    monkeypatch.setattr(collect_module, "OUT", output)
    snapshot = _valid_snapshot()
    snapshot["generated_at"] = "2026-09-13T01:02:03"

    with pytest.raises(
        ValueError,
        match=f"^{SNAPSHOT_ERROR}$",
    ):
        collect_module._write_local_snapshot(
            _service(),
            snapshot,
        )

    assert output.exists() is False
