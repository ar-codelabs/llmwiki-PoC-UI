from __future__ import annotations

import base64
import copy
import difflib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from .clients import AwsClients
from .redaction import Redactor
from .settings import Settings
from .wiki_paths import is_navigation_markdown, metadata_path

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_KEY = "snapshot.json"
CODECOMMIT_READ_WORKERS = 1
WIKI_DETAIL_LIMIT = 60
MAX_TEXT = 9000

_REPOSITORY_READ_ERROR = (
    "Required repository content is unavailable"
)
_FILE_MISSING_ERROR = "FileDoesNotExistException"
_SNAPSHOT_CONTRACT_ERROR = "Snapshot contract is invalid"
_SNAPSHOT_FIELD_TYPES = {
    "region": str,
    "runs": list,
    "wiki": dict,
    "bootstrap": dict,
    "skills": list,
    "repos": list,
}


class CollectorService:
    def __init__(
        self,
        settings: Settings,
        clients: AwsClients,
        redactor: Redactor,
        now: Callable[[], datetime],
    ) -> None:
        self.settings = settings
        self.clients = clients
        self.redactor = redactor
        self.now = now

    @staticmethod
    def _fixed_read_error() -> ValueError:
        return ValueError(_REPOSITORY_READ_ERROR)

    def _now_utc(self) -> datetime:
        current = self.now()
        if not isinstance(current, datetime):
            raise TypeError("Collector now must return datetime")
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _is_missing_file(error: Exception) -> bool:
        if type(error).__name__ == _FILE_MISSING_ERROR:
            return True
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return False
        error_detail = response.get("Error")
        if not isinstance(error_detail, dict):
            return False
        return error_detail.get("Code") == _FILE_MISSING_ERROR

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

    @staticmethod
    def _validate_snapshot(snapshot: object) -> dict:
        try:
            if not isinstance(snapshot, dict):
                raise ValueError
            if (
                type(snapshot["schema_version"]) is not int
                or snapshot["schema_version"]
                != SNAPSHOT_SCHEMA_VERSION
            ):
                raise ValueError
            generated_at = snapshot["generated_at"]
            if not isinstance(generated_at, str):
                raise ValueError
            parsed = datetime.fromisoformat(generated_at)
            if (
                parsed.tzinfo is None
                or parsed.utcoffset() is None
            ):
                raise ValueError
            for field, expected_type in (
                _SNAPSHOT_FIELD_TYPES.items()
            ):
                if not isinstance(
                    snapshot[field],
                    expected_type,
                ):
                    raise ValueError
            if not snapshot["region"]:
                raise ValueError
        except Exception:
            raise ValueError(
                _SNAPSHOT_CONTRACT_ERROR
            ) from None
        return snapshot

    def _read_file(
        self,
        repo: str,
        path: str,
        sha: str = "refs/heads/main",
        *,
        optional: bool = False,
    ) -> str | None:
        try:
            response = self.clients.codecommit.get_file(
                repositoryName=repo,
                commitSpecifier=sha,
                filePath=path,
            )
        except Exception as error:
            if optional and self._is_missing_file(error):
                return None
            raise self._fixed_read_error() from None
        try:
            return self._decode_file_content(
                response["fileContent"]
            )
        except Exception:
            raise self._fixed_read_error() from None

    def _branch_head(self, repo: str) -> str:
        try:
            response = self.clients.codecommit.get_branch(
                repositoryName=repo,
                branchName="main",
            )
            head = response["branch"]["commitId"]
            if not isinstance(head, str) or not head:
                raise ValueError
            return head
        except Exception:
            raise self._fixed_read_error() from None

    def _commit(self, repo: str, commit_id: str) -> dict:
        try:
            commit = self.clients.codecommit.get_commit(
                repositoryName=repo,
                commitId=commit_id,
            )["commit"]
            if not isinstance(commit, dict):
                raise ValueError
            return commit
        except Exception:
            raise self._fixed_read_error() from None

    def _folder(
        self,
        repo: str,
        path: str,
        commit_id: str,
    ) -> dict:
        try:
            folder = self.clients.codecommit.get_folder(
                repositoryName=repo,
                commitSpecifier=commit_id,
                folderPath=path,
            )
            if not isinstance(folder, dict):
                raise ValueError
            return folder
        except Exception:
            raise self._fixed_read_error() from None

    def _collect_runs(
        self,
        *,
        hours: int,
        current: datetime,
    ) -> list[dict]:
        start = int(
            (current - timedelta(hours=hours)).timestamp()
            * 1000
        )
        runs: list[dict] = []
        token: str | None = None
        while True:
            request: dict[str, object] = {
                "logGroupName": (
                    self.settings.lambda_log_group
                ),
                "startTime": start,
                "limit": 10_000,
            }
            if token is not None:
                request["nextToken"] = token
            response = self.clients.logs.filter_log_events(
                **request
            )
            for event in response.get("events", []):
                message = str(
                    event.get("message", "")
                ).strip()
                if not message.startswith("{"):
                    continue
                try:
                    decoded = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (
                    not isinstance(decoded, dict)
                    or "source" not in decoded
                ):
                    continue
                if (
                    decoded.get("repo")
                    not in self.settings.source_repositories
                ):
                    continue
                decoded["_at"] = datetime.fromtimestamp(
                    event["timestamp"] / 1000,
                    UTC,
                ).isoformat(timespec="seconds")
                runs.append(decoded)
            token = response.get("nextToken")
            if not token:
                break
        runs.sort(
            key=lambda item: item["_at"],
            reverse=True,
        )
        return runs

    @staticmethod
    def _parse_frontmatter(body: str) -> dict:
        if not body.startswith("---"):
            return {}
        frontmatter = body.split("---", 2)[1]
        result: dict = {
            "verified_count": frontmatter.count(
                "pull_request_id:"
            )
        }
        for key in ("type", "status", "stale_after"):
            match = re.search(
                rf"^{key}:\s*'?([^'\n]+)'?\s*$",
                frontmatter,
                re.MULTILINE,
            )
            if match:
                result[key] = match.group(1).strip()
        result["sources"] = re.findall(
            r"^- path:\s*(\S+)",
            frontmatter,
            re.MULTILINE,
        )
        generated = re.search(
            r"^\s*at:\s*'?([^'\n]+)'?",
            frontmatter,
            re.MULTILINE,
        )
        if generated:
            result["generated_at"] = (
                generated.group(1).strip()
            )
        return result

    @staticmethod
    def _unified(
        before: str,
        after: str,
        path: str,
    ) -> list[str]:
        return list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
                n=3,
            )
        )

    def _all_differences(
        self,
        repo: str,
        before_sha: str,
        after_sha: str,
    ) -> list[dict]:
        differences: list[dict] = []
        token: str | None = None
        while True:
            request: dict[str, object] = {
                "repositoryName": repo,
                "beforeCommitSpecifier": before_sha,
                "afterCommitSpecifier": after_sha,
            }
            if token is not None:
                request["NextToken"] = token
            try:
                response = (
                    self.clients.codecommit.get_differences(
                        **request
                    )
                )
                page = response.get("differences", [])
                if not isinstance(page, list):
                    raise ValueError
                differences.extend(page)
                token = response.get("NextToken")
            except Exception:
                raise self._fixed_read_error() from None
            if not token:
                return differences

    def _file_pair_from_difference(
        self,
        repo: str,
        difference: dict,
        before_sha: str,
        after_sha: str,
    ) -> dict:
        try:
            change_type = difference["changeType"]
            before_path = (
                difference.get("beforeBlob") or {}
            ).get("path")
            after_path = (
                difference.get("afterBlob") or {}
            ).get("path")
            if change_type == "A":
                if not after_path:
                    raise ValueError
                before = ""
                after = self._read_file(
                    repo,
                    after_path,
                    after_sha,
                )
            elif change_type == "D":
                if not before_path:
                    raise ValueError
                before = self._read_file(
                    repo,
                    before_path,
                    before_sha,
                )
                after = ""
            elif change_type == "M":
                if not before_path or not after_path:
                    raise ValueError
                before = self._read_file(
                    repo,
                    before_path,
                    before_sha,
                )
                after = self._read_file(
                    repo,
                    after_path,
                    after_sha,
                )
            else:
                raise ValueError
            path = after_path or before_path
            if (
                not isinstance(before, str)
                or not isinstance(after, str)
                or not isinstance(path, str)
            ):
                raise ValueError
        except ValueError as error:
            if str(error) == _REPOSITORY_READ_ERROR:
                raise
            raise self._fixed_read_error() from None
        return {
            "path": path,
            "before": before[:MAX_TEXT],
            "after": after[:MAX_TEXT],
            "before_truncated": len(before) > MAX_TEXT,
            "after_truncated": len(after) > MAX_TEXT,
            "unified": self._unified(
                before,
                after,
                path,
            )[:400],
        }

    def _wiki_file_pair(
        self,
        path: str,
        before_sha: str,
        after_sha: str,
    ) -> dict:
        before = self._read_file(
            self.settings.wiki_repo,
            path,
            before_sha,
            optional=True,
        )
        after = self._read_file(
            self.settings.wiki_repo,
            path,
            after_sha,
        )
        if not isinstance(after, str):
            raise self._fixed_read_error()
        before_text = before or ""
        return {
            "path": path,
            "before": before_text[:MAX_TEXT],
            "after": after[:MAX_TEXT],
            "before_truncated": (
                len(before_text) > MAX_TEXT
            ),
            "after_truncated": len(after) > MAX_TEXT,
            "unified": self._unified(
                before_text,
                after,
                path,
            )[:400],
        }

    def _attach_diffs(self, runs: list[dict]) -> None:
        for run in runs:
            source_repo = str(run.get("repo") or "")
            diffs: dict[str, list[dict]] = {
                "source": [],
                "wiki": [],
            }
            base = run.get("base_sha")
            head = run.get("head_sha")
            if (
                base
                and head
                and source_repo
                in self.settings.source_repositories
            ):
                differences = self._all_differences(
                    source_repo,
                    str(base),
                    str(head),
                )
                for difference in differences[:4]:
                    diffs["source"].append(
                        self._file_pair_from_difference(
                            source_repo,
                            difference,
                            str(base),
                            str(head),
                        )
                    )

            wiki = (
                (run.get("runtime") or {}).get("wiki")
                or {}
            )
            commit_id = wiki.get("commit_id")
            if commit_id:
                commit = self._commit(
                    self.settings.wiki_repo,
                    str(commit_id),
                )
                parents = commit.get("parents", [])
                parent = parents[0] if parents else None
                if parent:
                    wiki_files = [
                        path
                        for path in wiki.get("files", [])
                        if str(path).endswith(".md")
                    ][:4]
                    for path in wiki_files:
                        diffs["wiki"].append(
                            self._wiki_file_pair(
                                str(path),
                                str(parent),
                                str(commit_id),
                            )
                        )
            if diffs["source"] or diffs["wiki"]:
                run["diffs"] = diffs

    @staticmethod
    def _attach_pairs(runs: list[dict]) -> None:
        for run in runs:
            wiki = (run.get("runtime") or {}).get("wiki")
            if not isinstance(wiki, dict):
                continue
            files = wiki.get("files")
            if not isinstance(files, list):
                continue
            file_set = {
                path
                for path in files
                if isinstance(path, str)
            }
            wiki["pairs"] = [
                {
                    "markdown": path,
                    "metadata": derived
                    if derived in file_set
                    else None,
                }
                for path in sorted(file_set)
                if path.endswith(".md")
                for derived in (metadata_path(path),)
            ]

    def _walk_paths(
        self,
        repo: str,
        commit_id: str,
        path: str = "/",
    ) -> list[str]:
        paths: list[str] = []
        stack = [path]
        while stack:
            current = stack.pop()
            folder = self._folder(
                repo,
                current,
                commit_id,
            )
            try:
                paths.extend(
                    item["absolutePath"]
                    for item in folder.get("files", [])
                )
                stack.extend(
                    item["absolutePath"]
                    for item in folder.get(
                        "subFolders",
                        [],
                    )
                )
            except Exception:
                raise self._fixed_read_error() from None
        return paths

    def _collect_wiki(self) -> dict:
        repo = self.settings.wiki_repo
        head = self._branch_head(repo)
        commit = self._commit(repo, head)
        all_paths = self._walk_paths(repo, head)
        markdown_paths = sorted(
            path
            for path in all_paths
            if (
                path.endswith(".md")
                and not is_navigation_markdown(path)
            )
        )
        json_paths = {
            path
            for path in all_paths
            if path.endswith(".json")
        }

        def document(path: str) -> dict:
            body = self._read_file(
                repo,
                path,
                head,
            )
            if not isinstance(body, str):
                raise self._fixed_read_error()
            detail = self._parse_frontmatter(body)
            detail["path"] = path
            detail["metadata_path"] = metadata_path(path)
            detail["has_json_pair"] = (
                detail["metadata_path"] in json_paths
            )
            detail["body_chars"] = len(body)
            detail["top"] = (
                path.split("/", maxsplit=1)[0]
                if "/" in path
                else "(root)"
            )
            return detail

        with ThreadPoolExecutor(
            max_workers=CODECOMMIT_READ_WORKERS
        ) as executor:
            documents = list(
                executor.map(
                    document,
                    markdown_paths,
                )
            )

        by_top: dict[str, dict] = {}
        by_status: dict[str, int] = {}
        for item in documents:
            top = by_top.setdefault(
                item["top"],
                {
                    "total": 0,
                    "current": 0,
                    "unverified": 0,
                    "stale": 0,
                    "other": 0,
                },
            )
            top["total"] += 1
            status = item.get("status") or "other"
            top[
                status
                if status in top
                else "other"
            ] += 1
            by_status[status] = (
                by_status.get(status, 0) + 1
            )

        cursor_body = self._read_file(
            repo,
            "CURSOR.json",
            head,
            optional=True,
        )
        cursors = {}
        if cursor_body is not None:
            try:
                decoded = json.loads(cursor_body)
                if not isinstance(decoded, dict):
                    raise ValueError
                cursors = decoded.get("cursors", decoded)
                if not isinstance(cursors, dict):
                    raise ValueError
            except Exception:
                raise self._fixed_read_error() from None
        index_body = self._read_file(
            repo,
            "index.md",
            head,
            optional=True,
        )
        recent = sorted(
            documents,
            key=lambda item: (
                item.get("generated_at") or ""
            ),
            reverse=True,
        )
        try:
            message = str(commit["message"]).strip()
            author = commit["author"]["name"]
        except Exception:
            raise self._fixed_read_error() from None
        return {
            "repo": repo,
            "head": head,
            "head_message": message.splitlines()[0],
            "head_author": author,
            "total_md": len(markdown_paths),
            "total_json": len(json_paths),
            "pair_ok": sum(
                1
                for item in documents
                if item["has_json_pair"]
            ),
            "by_top": by_top,
            "by_status": by_status,
            "docs": recent[:WIKI_DETAIL_LIMIT],
            "docs_truncated": (
                len(documents) > WIKI_DETAIL_LIMIT
            ),
            "cursors": cursors,
            "has_index_md": index_body is not None,
        }

    def _collect_bootstrap(self) -> dict:
        repo = self.settings.wiki_repo
        try:
            response = (
                self.clients.codecommit.list_branches(
                    repositoryName=repo,
                )
            )
            branches = response["branches"]
            if not isinstance(branches, list):
                raise ValueError
        except Exception:
            raise self._fixed_read_error() from None
        bootstrap_branches = sorted(
            branch
            for branch in branches
            if branch.startswith(
                "wikiagent/bootstrap-"
            )
        )

        pull_requests: list[dict] = []
        for status in ("CLOSED", "OPEN"):
            try:
                response = (
                    self.clients.codecommit.list_pull_requests(
                        repositoryName=repo,
                        pullRequestStatus=status,
                    )
                )
                pull_request_ids = response.get(
                    "pullRequestIds",
                    [],
                )
                if not isinstance(
                    pull_request_ids,
                    list,
                ):
                    raise ValueError
            except Exception:
                raise self._fixed_read_error() from None
            for pull_request_id in pull_request_ids:
                try:
                    pull_request = (
                        self.clients.codecommit
                        .get_pull_request(
                            pullRequestId=pull_request_id,
                        )["pullRequest"]
                    )
                    target = pull_request[
                        "pullRequestTargets"
                    ][0]
                    source = target[
                        "sourceReference"
                    ]
                except Exception:
                    raise self._fixed_read_error() from None
                if not source.startswith(
                    "refs/heads/wikiagent/bootstrap-"
                ):
                    continue
                merge = target.get("mergeMetadata") or {}
                pull_requests.append(
                    {
                        "pull_request_id": (
                            pull_request_id
                        ),
                        "status": pull_request[
                            "pullRequestStatus"
                        ],
                        "title": pull_request["title"],
                        "repo": source.rsplit(
                            "bootstrap-",
                            1,
                        )[-1],
                        "merged": merge.get("isMerged"),
                        "merge_option": merge.get(
                            "mergeOption"
                        ),
                    }
                )
        try:
            pull_requests.sort(
                key=lambda item: int(
                    item["pull_request_id"]
                )
            )
        except Exception:
            raise self._fixed_read_error() from None
        return {
            "branches": bootstrap_branches,
            "pull_requests": pull_requests,
        }

    def _collect_skills(self) -> list[dict]:
        response = self.clients.s3.list_objects_v2(
            Bucket=self.settings.skills_bucket,
        )
        skills = []
        for item in response.get("Contents", []):
            if not item["Key"].endswith("SKILL.md"):
                continue
            modified = item["LastModified"]
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            skills.append(
                {
                    "name": item["Key"].split("/")[0],
                    "key": item["Key"],
                    "size": item["Size"],
                    "modified": self._iso(modified),
                }
            )
        return sorted(
            skills,
            key=lambda item: item["name"],
        )

    def _collect_repos(self) -> list[dict]:
        result = []
        for name in self.settings.source_repositories:
            head = self._branch_head(name)
            try:
                response = (
                    self.clients.codecommit
                    .list_pull_requests(
                        repositoryName=name,
                        pullRequestStatus="OPEN",
                    )
                )
                open_pull_requests = response.get(
                    "pullRequestIds",
                    [],
                )
                if not isinstance(
                    open_pull_requests,
                    list,
                ):
                    raise ValueError
            except Exception:
                raise self._fixed_read_error() from None
            result.append(
                {
                    "repo": name,
                    "head": head,
                    "open_prs": len(
                        open_pull_requests
                    ),
                }
            )
        return result

    def _collect_wiki_fast(
        self,
        previous: dict,
    ) -> dict:
        repo = self.settings.wiki_repo
        head = self._branch_head(repo)
        commit = self._commit(repo, head)
        cursor_body = self._read_file(
            repo,
            "CURSOR.json",
            head,
            optional=True,
        )
        cursors = {}
        if cursor_body is not None:
            try:
                decoded = json.loads(cursor_body)
                if not isinstance(decoded, dict):
                    raise ValueError
                cursors = decoded.get("cursors", decoded)
                if not isinstance(cursors, dict):
                    raise ValueError
            except Exception:
                raise self._fixed_read_error() from None
        try:
            message = str(commit["message"]).strip()
            author = commit["author"]["name"]
        except Exception:
            raise self._fixed_read_error() from None

        result = copy.deepcopy(previous)
        result.update(
            {
                "head": head,
                "head_message": message.splitlines()[0],
                "head_author": author,
                "cursors": cursors,
                "fast": True,
            }
        )
        documents = list(
            result.get("docs") or []
        )[:20]
        for item in documents:
            try:
                path = item["path"]
            except Exception:
                raise self._fixed_read_error() from None
            body = self._read_file(
                repo,
                path,
                head,
            )
            if not isinstance(body, str):
                raise self._fixed_read_error()
            item.update(self._parse_frontmatter(body))
        if documents:
            result["docs"] = (
                documents
                + list(result.get("docs") or [])[20:]
            )
        return result

    @staticmethod
    def _run_key(run: dict) -> tuple[object, object, object]:
        return (
            run.get("source"),
            run.get("diff_sha256"),
            run.get("_at"),
        )

    def build_snapshot(
        self,
        *,
        hours: int = 24,
    ) -> dict:
        current = self._now_utc()
        runs = self._collect_runs(
            hours=hours,
            current=current,
        )
        self._attach_pairs(runs)
        self._attach_diffs(
            [
                run
                for run in runs
                if run.get("runtime")
            ][:12]
        )
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "generated_at": self._iso(current),
            "region": self.settings.region,
            "runs": runs,
            "wiki": self._collect_wiki(),
            "bootstrap": self._collect_bootstrap(),
            "skills": self._collect_skills(),
            "repos": self._collect_repos(),
        }

    def build_fast_snapshot(
        self,
        previous: dict,
        *,
        hours: int = 6,
    ) -> dict:
        current = self._now_utc()
        merged = {
            self._run_key(run): copy.deepcopy(run)
            for run in previous.get("runs", [])
            if (
                run.get("repo")
                in self.settings.source_repositories
            )
        }
        for run in self._collect_runs(
            hours=hours,
            current=current,
        ):
            merged[self._run_key(run)] = run
        runs = sorted(
            merged.values(),
            key=lambda item: item.get("_at") or "",
            reverse=True,
        )
        self._attach_pairs(runs)
        self._attach_diffs(
            [
                run
                for run in runs
                if run.get("runtime")
            ][:6]
        )
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "generated_at": self._iso(current),
            "region": self.settings.region,
            "runs": runs,
            "wiki": self._collect_wiki_fast(
                previous.get("wiki", {})
            ),
            "bootstrap": copy.deepcopy(
                previous.get("bootstrap", {})
            ),
            "skills": copy.deepcopy(
                previous.get("skills", [])
            ),
            "repos": copy.deepcopy(
                previous.get("repos", [])
            ),
        }

    def serialize_snapshot(self, snapshot: dict) -> bytes:
        self._validate_snapshot(snapshot)
        safe = self.redactor.redact_obj(snapshot)
        safe = self._validate_snapshot(safe)
        rendered = json.dumps(
            safe,
            ensure_ascii=False,
            indent=1,
        )
        self.redactor.assert_safe(rendered)
        return rendered.encode("utf-8")

    def write_snapshot(self, snapshot: dict) -> dict:
        body = self.serialize_snapshot(snapshot)
        safe_snapshot = json.loads(body)
        self.clients.s3.put_object(
            Bucket=self.settings.snapshot_bucket,
            Key=SNAPSHOT_KEY,
            Body=body,
            ContentType="application/json",
            CacheControl="no-store, max-age=0",
        )
        return {
            "key": SNAPSHOT_KEY,
            "schema_version": safe_snapshot.get(
                "schema_version"
            ),
            "generated_at": safe_snapshot.get(
                "generated_at"
            ),
            "bytes": len(body),
        }

    def collect_and_write(
        self,
        *,
        hours: int = 24,
    ) -> dict:
        snapshot = self.build_snapshot(hours=hours)
        return self.write_snapshot(snapshot)
