from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO


class FakeCodeCommit:
    branches: dict[tuple[str, str], str]
    files: dict[tuple[str, str, str], bytes]
    commits: dict[tuple[str, str], dict]
    pull_requests: dict[str, dict]
    differences: dict[tuple[str, str, str], list[dict]]

    def __init__(
        self,
        *,
        branches: dict[tuple[str, str], str] | None = None,
        files: dict[tuple[str, str, str], bytes] | None = None,
        commits: dict[tuple[str, str], dict] | None = None,
        pull_requests: dict[str, dict] | None = None,
        differences: (
            dict[tuple[str, str, str], list[dict]] | None
        ) = None,
        repositories: dict[str, dict] | None = None,
        folders: dict[tuple[str, str], dict] | None = None,
    ) -> None:
        self.branches = dict(branches or {})
        self.files = dict(files or {})
        self.commits = dict(commits or {})
        self.pull_requests = dict(pull_requests or {})
        self.differences = {
            key: list(value)
            for key, value in (differences or {}).items()
        }
        self.repositories = dict(repositories or {})
        self.folders = dict(folders or {})
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, dict(kwargs)))

    def get_file(self, **kwargs) -> dict:
        self._record("get_file", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["commitSpecifier"],
            kwargs["filePath"],
        )
        return {"fileContent": self.files[key]}

    def get_pull_request(self, **kwargs) -> dict:
        self._record("get_pull_request", kwargs)
        return {
            "pullRequest": self.pull_requests[
                kwargs["pullRequestId"]
            ]
        }

    def get_differences(self, **kwargs) -> dict:
        self._record("get_differences", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["beforeCommitSpecifier"],
            kwargs["afterCommitSpecifier"],
        )
        return {"differences": list(self.differences[key])}

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

    def get_repository(self, **kwargs) -> dict:
        self._record("get_repository", kwargs)
        repository_name = kwargs["repositoryName"]
        metadata = self.repositories.get(
            repository_name,
            {
                "defaultBranch": "main",
                "cloneUrlHttp": (
                    "https://example.invalid/"
                    f"{repository_name}"
                ),
                "lastModifiedDate": datetime(
                    2026,
                    9,
                    12,
                    tzinfo=UTC,
                ),
            },
        )
        return {"repositoryMetadata": metadata}

    def get_folder(self, **kwargs) -> dict:
        self._record("get_folder", kwargs)
        key = (
            kwargs["repositoryName"],
            kwargs["folderPath"],
        )
        return self.folders.get(
            key,
            {"files": [], "subFolders": []},
        )

    def list_branches(self, **kwargs) -> dict:
        self._record("list_branches", kwargs)
        repository_name = kwargs["repositoryName"]
        branches = sorted(
            branch
            for repo, branch in self.branches
            if repo == repository_name
        )
        return {"branches": branches}

    def create_branch(self, **kwargs) -> dict:
        self._record("create_branch", kwargs)
        self.branches[
            (
                kwargs["repositoryName"],
                kwargs["branchName"],
            )
        ] = kwargs["commitId"]
        return {}

    def create_commit(self, **kwargs) -> dict:
        self._record("create_commit", kwargs)
        return {"commitId": "created-commit"}

    def create_pull_request(self, **kwargs) -> dict:
        self._record("create_pull_request", kwargs)
        return {
            "pullRequest": {
                "pullRequestId": "created-pr",
            }
        }

    def merge_pull_request_by_fast_forward(
        self,
        **kwargs,
    ) -> dict:
        self._record(
            "merge_pull_request_by_fast_forward",
            kwargs,
        )
        return {
            "pullRequest": {
                "pullRequestStatus": "CLOSED",
                "pullRequestTargets": [
                    {
                        "mergeMetadata": {
                            "isMerged": True,
                            "mergedBy": "demo-ui",
                            "mergeOption": (
                                "FAST_FORWARD_MERGE"
                            ),
                            "mergeCommitId": "merge-commit",
                        }
                    }
                ],
            }
        }


class FakeLogs:
    events: list[dict]

    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = [dict(event) for event in events or []]
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, dict(kwargs)))

    def _matching_events(self, kwargs: dict) -> list[dict]:
        log_group_name = kwargs["logGroupName"]
        start_time = kwargs.get("startTime", 0)
        pattern = kwargs.get("filterPattern", "").strip('"')
        matches = [
            event
            for event in self.events
            if event.get("logGroupName") == log_group_name
            and event.get("timestamp", 0) >= start_time
            and (
                not pattern
                or pattern in str(event.get("message", ""))
            )
        ]
        return [
            {
                key: value
                for key, value in event.items()
                if key != "logGroupName"
            }
            for event in matches[: kwargs.get("limit")]
        ]

    def filter_log_events(self, **kwargs) -> dict:
        self._record("filter_log_events", kwargs)
        return {"events": self._matching_events(kwargs)}

    def describe_log_streams(self, **kwargs) -> dict:
        self._record("describe_log_streams", kwargs)
        matches = self._matching_events(
            {
                "logGroupName": kwargs["logGroupName"],
                "startTime": 0,
            }
        )
        if not matches:
            return {"logStreams": []}
        return {
            "logStreams": [
                {
                    "lastEventTimestamp": max(
                        event["timestamp"]
                        for event in matches
                    )
                }
            ]
        }


class FakeS3:
    objects: dict[tuple[str, str], bytes]

    def __init__(
        self,
        objects: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.objects = dict(objects or {})
        self.calls: list[tuple[str, dict]] = []

    def get_object(self, **kwargs) -> dict:
        self.calls.append(("get_object", dict(kwargs)))
        key = (kwargs["Bucket"], kwargs["Key"])
        return {"Body": BytesIO(self.objects[key])}
