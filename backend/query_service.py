from __future__ import annotations

import base64
import contextlib
import copy
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .clients import AwsClients
from .redaction import Redactor
from .review_policy import (
    MAX_REVIEW_DIFF_LINES,
    ReviewPolicyError,
    declared_difference_paths,
    full_unified_diff,
)
from .settings import Settings

_EDITABLE = (
    (
        "shaka-player/lib/net/backoff.js",
        "shaka-player",
        "lib/net/backoff.js",
    ),
)

_IDENTIFIER_PREFIX_LENGTH = 12
_STAGE_LABEL = {
    "stage_plan": "파이프라인 단계 결정",
    "wiki_index_loaded": "위키 역색인 로드",
    "stage_skill_scope": "스킬 노출 범위 산정",
    "skills_selected": "스킬 선택",
    "publish_state_inspected": "기존 게시 상태 확인 (멱등)",
    "converse_started": "Claude 호출 시작",
    "converse_finished": "Claude 응답 수신",
    "validation_passed": "결정론 검증 통과",
    "publish_state_transition": "게시 상태 전이",
}
_CLIENT_HIDDEN_WIKI_COMMIT_KEYS = {
    "commitid",
    "headcommit",
    "headcommitid",
    "headsha",
    "publishedcommit",
    "publishedcommitid",
    "sourcecommit",
    "sourcecommitid",
    "sourcesha",
}


def _identifier_prefix(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:_IDENTIFIER_PREFIX_LENGTH]


class QueryService:
    def __init__(
        self,
        settings: Settings,
        clients: AwsClients,
        redactor: Redactor,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.clients = clients
        self.redactor = redactor
        self.now = now

    def _response(self, value: dict) -> dict:
        safe = self.redactor.redact_obj(value)
        if not isinstance(safe, dict):
            raise TypeError("Query response must be a JSON object")
        self.redactor.assert_safe(
            json.dumps(safe, ensure_ascii=False)
        )
        return safe

    @staticmethod
    def _client_safe_payload(payload: dict) -> dict:
        safe = copy.deepcopy(payload)
        runtime = safe.get("runtime")
        if not isinstance(runtime, dict):
            return safe
        wiki = runtime.get("wiki")
        if not isinstance(wiki, dict):
            return safe
        for key in tuple(wiki):
            normalized = re.sub(
                r"[^a-z0-9]",
                "",
                key.lower(),
            )
            if normalized in _CLIENT_HIDDEN_WIKI_COMMIT_KEYS:
                wiki.pop(key, None)
        return safe

    def _read(
        self,
        repo: str,
        path: str,
        sha: str = "refs/heads/main",
    ) -> str | None:
        try:
            response = self.clients.codecommit.get_file(
                repositoryName=repo,
                commitSpecifier=sha,
                filePath=path,
            )
        except Exception:
            return None
        try:
            return self._decode_file_content(
                response["fileContent"]
            )
        except Exception:
            return None

    @staticmethod
    def _decode_file_content(content: object) -> str:
        if isinstance(content, bytes):
            return content.decode("utf-8")
        if isinstance(content, str):
            return base64.b64decode(
                content,
                validate=True,
            ).decode("utf-8")
        raise ValueError("Invalid repository file content")

    def _read_required(
        self,
        repo: str,
        path: str,
        sha: str,
    ) -> str:
        try:
            response = self.clients.codecommit.get_file(
                repositoryName=repo,
                commitSpecifier=sha,
                filePath=path,
            )
            return self._decode_file_content(
                response["fileContent"]
            )
        except Exception:
            raise ValueError(
                "Required repository content is unavailable"
            ) from None

    def _main_head_required(self, repo: str) -> str:
        try:
            head = self.clients.codecommit.get_branch(
                repositoryName=repo,
                branchName="main",
            )["branch"]["commitId"]
            if not isinstance(head, str) or not head:
                raise ValueError
            return head
        except Exception:
            raise ValueError(
                "Required repository content is unavailable"
            ) from None

    def _all_differences(
        self,
        *,
        repository: str,
        before_commit: str,
        after_commit: str,
    ) -> list[dict]:
        differences: list[dict] = []
        next_token: str | None = None
        while True:
            request = {
                "repositoryName": repository,
                "beforeCommitSpecifier": before_commit,
                "afterCommitSpecifier": after_commit,
            }
            if next_token is not None:
                request["NextToken"] = next_token
            response = (
                self.clients.codecommit.get_differences(
                    **request
                )
            )
            differences.extend(
                response.get("differences", [])
            )
            next_token = response.get("NextToken")
            if not next_token:
                return differences

    @staticmethod
    def _append_unique(
        values: list[str],
        seen: set[str],
        value: str | None,
    ) -> None:
        if value is not None and value not in seen:
            seen.add(value)
            values.append(value)

    def list_editable(self) -> dict:
        files = []
        for key, repo, path in _EDITABLE:
            if repo not in self.settings.source_repositories:
                raise ValueError(
                    "Required repository content is unavailable"
                )
            head = self._main_head_required(repo)
            body = self._read_required(repo, path, head)
            files.append(
                {
                    "key": key,
                    "repo": repo,
                    "path": path,
                    "content": body,
                    "lines": len(body.splitlines()),
                    "head": head,
                }
            )
        return self._response({"files": files})

    def progress(
        self,
        pr_id: str,
        since_ms: int | None,
    ) -> dict:
        logs = self.clients.logs
        start = (
            since_ms
            if since_ms is not None
            else int((self.now() - 1800) * 1000)
        )
        pull_request = self.clients.codecommit.get_pull_request(
            pullRequestId=pr_id,
        )["pullRequest"]
        target = pull_request["pullRequestTargets"][0]
        expected_repo = target["repositoryName"]
        if expected_repo not in self.settings.source_repositories:
            raise ValueError(
                "Source pull request repository is not configured"
            )
        expected_base = _identifier_prefix(
            target["mergeBase"]
        )
        expected_head = _identifier_prefix(
            target["sourceCommit"]
        )

        def matches_identity(
            value: dict,
            *,
            expected_diff: str | None = None,
        ) -> bool:
            if (
                value.get("repo") != expected_repo
                or _identifier_prefix(value.get("base_sha"))
                != expected_base
                or _identifier_prefix(value.get("head_sha"))
                != expected_head
            ):
                return False
            if expected_diff is None:
                return True
            return (
                _identifier_prefix(value.get("diff_sha256"))
                == expected_diff
            )

        payload = None
        invalid_payload_identity_seen = False
        try:
            response = logs.filter_log_events(
                logGroupName=self.settings.lambda_log_group,
                startTime=start,
                filterPattern=f'"event:pr#{pr_id}"',
                limit=20,
            )
            for event in response.get("events", []):
                message = event["message"].strip()
                if not message.startswith("{"):
                    continue
                try:
                    decoded = json.loads(message)
                except Exception:
                    continue
                if (
                    decoded.get("source")
                    == f"event:pr#{pr_id}"
                    and matches_identity(decoded)
                ):
                    if _identifier_prefix(
                        decoded.get("diff_sha256")
                    ) is None:
                        invalid_payload_identity_seen = True
                        continue
                    payload = decoded
        except Exception:
            pass

        runtime_correlation_blocked = (
            payload is None
            and invalid_payload_identity_seen
        )
        expected_diff = (
            _identifier_prefix(payload.get("diff_sha256"))
            if payload
            else None
        )

        runtime_events = []
        try:
            response = (
                {"events": []}
                if runtime_correlation_blocked
                else logs.filter_log_events(
                    logGroupName=self.settings.runtime_log_group,
                    startTime=start,
                    limit=300,
                )
            )
            for event in response.get("events", []):
                message = event["message"].strip()
                if not message.startswith("{"):
                    continue
                try:
                    decoded = json.loads(message)
                except Exception:
                    continue
                name = decoded.get("event")
                if name not in _STAGE_LABEL:
                    continue
                if not matches_identity(
                    decoded,
                    expected_diff=expected_diff,
                ):
                    continue
                runtime_events.append(
                    {
                        "event": name,
                        "label": _STAGE_LABEL[name],
                        "at": datetime.fromtimestamp(
                            event["timestamp"] / 1000,
                            UTC,
                        ).isoformat(timespec="seconds"),
                        "detail": {
                            key: value
                            for key, value in decoded.items()
                            if key != "event"
                            and not isinstance(
                                value,
                                (dict, list),
                            )
                        },
                    }
                )
        except Exception:
            pass
        runtime_events.sort(key=lambda item: item["at"])
        lambda_started = (
            payload is not None
            or bool(runtime_events)
        )

        wiki_pr = None
        client_payload = None
        if payload:
            runtime_payload = payload.get("runtime") or {}
            wiki_payload = runtime_payload.get("wiki") or {}
            wiki_pr = wiki_payload.get("pull_request_id")
            client_payload = self._client_safe_payload(
                payload
            )

        return self._response(
            {
                "pull_request_id": pr_id,
                "lambda_started": lambda_started,
                "runtime_events": runtime_events,
                "done": payload is not None,
                "payload": client_payload,
                "wiki_pull_request_id": wiki_pr,
            }
        )

    def wiki_pr_detail(self, wiki_pr: str) -> dict:
        codecommit = self.clients.codecommit
        pull_request = codecommit.get_pull_request(
            pullRequestId=wiki_pr,
        )["pullRequest"]
        target = pull_request["pullRequestTargets"][0]
        differences = self._all_differences(
            repository=self.settings.wiki_repo,
            before_commit=target["destinationCommit"],
            after_commit=target["sourceCommit"],
        )
        all_files: list[str] = []
        seen_files: set[str] = set()
        files = []
        review_complete = True
        for difference in differences:
            try:
                (
                    before_path,
                    after_path,
                ) = declared_difference_paths(difference)
            except ReviewPolicyError as error:
                raise ValueError(str(error)) from None
            self._append_unique(
                all_files,
                seen_files,
                before_path,
            )
            self._append_unique(
                all_files,
                seen_files,
                after_path,
            )

            before = (
                self._read_required(
                    self.settings.wiki_repo,
                    before_path,
                    target["destinationCommit"],
                )
                if before_path is not None
                else ""
            )
            after = (
                self._read_required(
                    self.settings.wiki_repo,
                    after_path,
                    target["sourceCommit"],
                )
                if after_path is not None
                else ""
            )
            display_path = after_path or before_path
            full_unified = full_unified_diff(
                before=before,
                after=after,
                before_path=before_path,
                after_path=after_path,
                context_lines=3,
            )
            truncated = (
                len(full_unified) > MAX_REVIEW_DIFF_LINES
            )
            if truncated:
                visible_lines = MAX_REVIEW_DIFF_LINES - 1
                hidden_lines = (
                    len(full_unified) - visible_lines
                )
                unified = [
                    *full_unified[:visible_lines],
                    (
                        "... diff truncated: "
                        f"{hidden_lines} lines hidden ..."
                    ),
                ]
                review_complete = False
            else:
                unified = full_unified
            files.append(
                {
                    "path": display_path,
                    "before": before[:9000],
                    "after": after[:9000],
                    "truncated": truncated,
                    "unified": unified,
                }
            )
        return self._response(
            {
                "pull_request_id": wiki_pr,
                "status": pull_request["pullRequestStatus"],
                "title": pull_request["title"],
                "branch": target["sourceReference"],
                "review_complete": review_complete,
                "source_commit": (
                    target["sourceCommit"]
                    if review_complete
                    else None
                ),
                "all_files": all_files,
                "files": files,
            }
        )

    def wiki_repo_info(self) -> dict:
        codecommit = self.clients.codecommit
        wiki_repo = self.settings.wiki_repo
        info = codecommit.get_repository(
            repositoryName=wiki_repo,
        )["repositoryMetadata"]
        tree: list[dict] = []

        def walk(path: str = "/", depth: int = 0) -> None:
            if depth > 2:
                return
            folder = codecommit.get_folder(
                repositoryName=wiki_repo,
                folderPath=path,
            )
            tree.extend(
                {
                    "path": item["absolutePath"],
                    "kind": "file",
                    "depth": depth,
                }
                for item in folder.get("files", [])
            )
            for subfolder in folder.get("subFolders", []):
                tree.append(
                    {
                        "path": subfolder["absolutePath"],
                        "kind": "dir",
                        "depth": depth,
                    }
                )
                walk(
                    subfolder["absolutePath"],
                    depth + 1,
                )

        walk()
        return self._response(
            {
                "repo": wiki_repo,
                "kind": "AWS CodeCommit (Git)",
                "default_branch": info.get("defaultBranch"),
                "clone_url": info["cloneUrlHttp"],
                "last_modified": info[
                    "lastModifiedDate"
                ].isoformat(timespec="seconds"),
                "branch_count": len(
                    codecommit.list_branches(
                        repositoryName=wiki_repo,
                    )["branches"]
                ),
                "tree": tree[:120],
            }
        )

    def wiki_history(self, limit: int) -> dict:
        codecommit = self.clients.codecommit
        wiki_repo = self.settings.wiki_repo
        head = codecommit.get_branch(
            repositoryName=wiki_repo,
            branchName="main",
        )["branch"]["commitId"]
        commits: list[dict] = []
        sha = head
        seen = set()
        while sha and len(commits) < limit and sha not in seen:
            seen.add(sha)
            try:
                commit = codecommit.get_commit(
                    repositoryName=wiki_repo,
                    commitId=sha,
                )["commit"]
            except Exception:
                break
            message = commit["message"] or ""
            subject = (
                message.strip().splitlines()[0]
                if message.strip()
                else "(빈 메시지)"
            )
            metadata = {}
            for key in (
                "source",
                "source_pull_request",
                "source_watermark",
                "diff_sha256",
                "wiki_merge",
                "execution",
            ):
                match = re.search(
                    rf"^{key} (\S+)$",
                    message,
                    re.M,
                )
                if match:
                    metadata[key] = match.group(1)
            match = re.search(
                r'^source_pull_request_description (".*")$',
                message,
                re.M,
            )
            if match:
                with contextlib.suppress(Exception):
                    metadata["pr_description"] = json.loads(
                        match.group(1)
                    )
            kind = (
                "확정"
                if subject.startswith("chore(freshness)")
                else "게시"
                if subject.startswith("docs")
                else "기타"
            )
            commits.append(
                {
                    "commit": commit["commitId"],
                    "subject": subject,
                    "kind": kind,
                    "author": commit["author"]["name"],
                    "date": commit["author"].get(
                        "date",
                        "",
                    ),
                    "meta": metadata,
                    "parents": commit.get("parents", []),
                }
            )
            sha = (commit.get("parents") or [None])[0]
        return self._response(
            {
                "head": head,
                "commits": commits,
            }
        )

    def wiki_commit(self, commit_id: str) -> dict:
        codecommit = self.clients.codecommit
        wiki_repo = self.settings.wiki_repo
        commit = codecommit.get_commit(
            repositoryName=wiki_repo,
            commitId=commit_id,
        )["commit"]
        parent = (commit.get("parents") or [None])[0]
        files: list[dict] = []
        if parent:
            differences = self._all_differences(
                repository=wiki_repo,
                before_commit=parent,
                after_commit=commit_id,
            )
            for difference in differences[:6]:
                try:
                    (
                        before_path,
                        after_path,
                    ) = declared_difference_paths(
                        difference
                    )
                except ReviewPolicyError as error:
                    raise ValueError(str(error)) from None
                before = (
                    self._read_required(
                        wiki_repo,
                        before_path,
                        parent,
                    )
                    if before_path is not None
                    else ""
                )
                after = (
                    self._read_required(
                        wiki_repo,
                        after_path,
                        commit_id,
                    )
                    if after_path is not None
                    else ""
                )
                path = after_path or before_path
                files.append(
                    {
                        "path": path,
                        "change": difference["changeType"],
                        "before": before[:9000],
                        "after": after[:9000],
                        "unified": full_unified_diff(
                            before=before,
                            after=after,
                            before_path=before_path,
                            after_path=after_path,
                            context_lines=3,
                        )[:300],
                    }
                )
        return self._response(
            {
                "commit": commit["commitId"],
                "author": commit["author"]["name"],
                "date": commit["author"].get("date", ""),
                "message": commit["message"],
                "parent": parent,
                "files": files,
            }
        )

    def finalization(
        self,
        repo: str,
        paths: list[str],
    ) -> dict:
        if repo not in self.settings.source_repositories:
            raise ValueError(
                "Source repository is not configured"
            )
        codecommit = self.clients.codecommit
        wiki_repo = self.settings.wiki_repo
        head = codecommit.get_branch(
            repositoryName=wiki_repo,
            branchName="main",
        )["branch"]["commitId"]
        commit = codecommit.get_commit(
            repositoryName=wiki_repo,
            commitId=head,
        )["commit"]
        cursor_body = self._read(
            wiki_repo,
            "CURSOR.json",
        )
        cursors = {}
        if cursor_body:
            try:
                decoded = json.loads(cursor_body)
                cursors = decoded.get("cursors", decoded)
            except Exception:
                pass

        docs = []
        for path in paths:
            body = self._read(wiki_repo, path)
            if not body:
                continue
            frontmatter = (
                body.split("---", 2)[1]
                if body.startswith("---")
                else ""
            )

            def frontmatter_value(
                key: str,
                content: str = frontmatter,
            ) -> str | None:
                match = re.search(
                    rf"^{key}:\s*'?([^'\n]+)'?\s*$",
                    content,
                    re.M,
                )
                return match.group(1) if match else None

            docs.append(
                {
                    "path": path,
                    "status": frontmatter_value("status"),
                    "stale_after": frontmatter_value(
                        "stale_after"
                    ),
                    "verified_count": frontmatter.count(
                        "pull_request_id:"
                    ),
                    "last_verified_pr": (
                        re.findall(
                            r"pull_request_id:\s*'?(\d+)",
                            frontmatter,
                        )
                        or [None]
                    )[-1],
                }
            )
        return self._response(
            {
                "wiki_head": head,
                "wiki_head_message": commit[
                    "message"
                ].strip().splitlines()[0],
                "wiki_head_author": commit["author"]["name"],
                "cursor": cursors.get(repo),
                "docs": docs,
            }
        )

    def snapshot(self) -> dict:
        response = self.clients.s3.get_object(
            Bucket=self.settings.snapshot_bucket,
            Key="snapshot.json",
        )
        body = response["Body"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        snapshot = json.loads(body)
        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot must be a JSON object")
        return self._response(snapshot)
