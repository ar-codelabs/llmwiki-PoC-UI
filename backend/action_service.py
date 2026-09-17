from __future__ import annotations

import base64
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from .clients import AwsClients
from .identity import validate_subject
from .redaction import (
    RedactionError,
    Redactor,
    contains_sensitive_metadata,
    validate_redaction_policy,
)
from .review_policy import (
    MAX_REVIEW_DIFF_LINES,
    ReviewPolicyError,
    declared_difference_paths,
    full_unified_diff,
)
from .settings import Settings

FIXED_SOURCE_KEY = "shaka-player/lib/net/backoff.js"
FIXED_SOURCE_REPOSITORY = "shaka-player"
FIXED_SOURCE_PATH = "lib/net/backoff.js"
EDITABLE: dict[str, str] = {
    FIXED_SOURCE_KEY: FIXED_SOURCE_REPOSITORY,
}
REFRESH_OPERATION_ID = "collector-refresh:accepted"

_MAX_TITLE_LENGTH = 150
_MAX_DESCRIPTION_LENGTH = 10_240
_MAX_CONTENT_BYTES = 6_291_456
_BRANCH_TOKEN = re.compile(r"[0-9a-f]{12}")
_BRANCH_NAME = re.compile(
    r"demo/[0-9]+-[0-9a-f]{12}"
)
_RESERVED_AUDIT_LINE = re.compile(
    r"^[ \t]*(?:"
    r"requested_by[ \t]+\S.*|"
    r"source[ \t]+demo-ui(?:[ \t]+\S.*)?"
    r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONSE_TEMPLATES = (
    {
        "ok": True,
        "operation_id": "source-pr:1",
        "repo": FIXED_SOURCE_REPOSITORY,
        "path": FIXED_SOURCE_PATH,
        "branch": "demo/1-abcdef123456",
        "commit": "abcdef",
        "pull_request_id": "1",
        "merge_commit_id": "abcdef",
        "merged_at": "2026-09-13T00:00:00+00:00",
    },
    {
        "ok": True,
        "operation_id": "wiki-pr:1",
        "pull_request_id": "1",
        "status": "CLOSED",
        "merged_by": (
            "00000000-0000-4000-8000-abcdef123456"
        ),
        "merge_option": "FAST_FORWARD_MERGE",
        "merge_commit_id": "abcdef",
    },
    {
        "ok": True,
        "operation_id": REFRESH_OPERATION_ID,
        "accepted": True,
    },
)


def fixed_demo_mutation(current_content: str) -> str:
    if not isinstance(current_content, str):
        raise ValueError("Source file content is invalid")
    assignments = list(
        re.finditer(
            r"(?m)^(?P<indent>[ \t]*)baseDelay:\s*"
            r"(?P<value>1000|1200),(?P<trailing>[ \t]*)$",
            current_content,
        )
    )
    if len(assignments) != 1:
        raise ValueError(
            "Source file is not in a supported demo state"
        )
    assignment = assignments[0]
    next_value = (
        "1200"
        if assignment.group("value") == "1000"
        else "1000"
    )
    replacement = (
        f"{assignment.group('indent')}baseDelay: "
        f"{next_value},{assignment.group('trailing')}"
    )
    return (
        current_content[: assignment.start()]
        + replacement
        + current_content[assignment.end() :]
    )


class ActionService:
    def __init__(
        self,
        settings: Settings,
        clients: AwsClients,
        redactor: Redactor,
        now: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = (
            lambda: uuid4().hex[:12]
        ),
    ) -> None:
        self._preflight_redactor(redactor)
        self.settings = settings
        self.clients = clients
        self.redactor = redactor
        self.now = now
        self.token_factory = token_factory

    @classmethod
    def _preflight_redactor(
        cls,
        redactor: Redactor,
    ) -> None:
        try:
            if not isinstance(redactor, Redactor):
                raise TypeError
            validate_redaction_policy(
                redactor.replacements
            )

            for template in _RESPONSE_TEMPLATES:
                safe = redactor.redact_obj(template)
                if (
                    not isinstance(safe, dict)
                    or tuple(safe)
                    != tuple(template)
                ):
                    raise ValueError
                redactor.assert_safe(
                    json.dumps(
                        safe,
                        ensure_ascii=False,
                    )
                )
        except Exception:
            raise ValueError(
                "Action redactor configuration is invalid"
            ) from None

    @staticmethod
    def _validate_actor_sub(actor_sub: object) -> str:
        try:
            return validate_subject(actor_sub)
        except ValueError:
            raise ValueError(
                "Authenticated actor subject is invalid"
            ) from None

    @staticmethod
    def _has_forbidden_control(
        value: str,
        *,
        allow_lf: bool,
    ) -> bool:
        return any(
            character != "\u200d"
            and not (allow_lf and character == "\n")
            and unicodedata.category(character)
            in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )

    @staticmethod
    def _required_string(
        value: object,
        *,
        strip: bool = False,
    ) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("Action request is invalid")
        if strip and not value.strip():
            raise ValueError("Action request is invalid")
        return value

    @staticmethod
    def _contains_sensitive_metadata(value: str) -> bool:
        return contains_sensitive_metadata(value)

    @classmethod
    def _build_pull_request_description(
        cls,
        *,
        description: str,
        actor_sub: str,
    ) -> str:
        if (
            cls._has_forbidden_control(
                description,
                allow_lf=True,
            )
            or cls._contains_sensitive_metadata(description)
            or _RESERVED_AUDIT_LINE.search(description)
        ):
            raise ValueError(
                "Pull request metadata is invalid"
            )
        final_description = (
            f"{description}\n\n"
            f"requested_by {actor_sub}\n"
            "source demo-ui"
        )
        lines = final_description.splitlines()
        if (
            lines.count(f"requested_by {actor_sub}") != 1
            or lines.count("source demo-ui") != 1
        ):
            raise ValueError(
                "Pull request metadata is invalid"
            )
        return final_description

    @classmethod
    def _validate_outbound_metadata(
        cls,
        *,
        title: str,
        description: str,
        actor_sub: str,
    ) -> str:
        cls._validate_actor_sub(actor_sub)
        if (
            cls._has_forbidden_control(
                title,
                allow_lf=False,
            )
            or cls._contains_sensitive_metadata(title)
        ):
            raise ValueError(
                "Pull request metadata is invalid"
            )
        return cls._build_pull_request_description(
            description=description,
            actor_sub=actor_sub,
        )

    @classmethod
    def _validate_commit_request(
        cls,
        *,
        key: object,
        content: object,
        title: object,
        description: object,
        expected_head: object,
        actor_sub: object,
    ) -> tuple[bytes, str, str, str]:
        cls._required_string(key)
        content_value = cls._required_string(content)
        title_value = cls._required_string(
            title,
            strip=True,
        )
        description_value = cls._required_string(
            description,
            strip=True,
        )
        cls._required_string(
            expected_head,
            strip=True,
        )
        actor_value = cls._validate_actor_sub(actor_sub)
        final_description = (
            cls._validate_outbound_metadata(
                title=title_value,
                description=description_value,
                actor_sub=actor_value,
            )
        )
        try:
            content_bytes = content_value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(
                "Action request is invalid"
            ) from error
        if (
            len(title_value) > _MAX_TITLE_LENGTH
            or len(final_description)
            > _MAX_DESCRIPTION_LENGTH
            or len(content_bytes) > _MAX_CONTENT_BYTES
        ):
            raise ValueError(
                "Action request exceeds service limits"
            )
        return (
            content_bytes,
            title_value,
            description_value,
            final_description,
        )

    @classmethod
    def _validate_approve_request(
        cls,
        *,
        wiki_pr: object,
        expected_source_commit: object,
        actor_sub: object,
    ) -> str:
        cls._required_string(wiki_pr, strip=True)
        cls._required_string(
            expected_source_commit,
            strip=True,
        )
        return cls._validate_actor_sub(actor_sub)

    @staticmethod
    def _prepare_action_time(
        value: object,
        token: str,
    ) -> tuple[str, str]:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            raise ValueError("Action timestamp is invalid")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "Action timestamp is invalid"
            ) from None
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("Action timestamp is invalid")
        try:
            timestamp = int(numeric)
            merged_at = datetime.fromtimestamp(
                numeric,
                UTC,
            ).isoformat(timespec="seconds")
        except (OSError, OverflowError, TypeError, ValueError):
            raise ValueError(
                "Action timestamp is invalid"
            ) from None
        branch = f"demo/{timestamp}-{token}"
        if (
            len(branch) > 256
            or _BRANCH_NAME.fullmatch(branch) is None
        ):
            raise ValueError("Action timestamp is invalid")
        return branch, merged_at

    def _response(
        self,
        value: dict,
        *,
        subject_fields: tuple[str, ...] = (),
    ) -> dict:
        redactable = dict(value)
        subjects: dict[str, str] = {}
        for field in subject_fields:
            try:
                subject = redactable.pop(field)
            except KeyError:
                raise TypeError(
                    "Action response subject is missing"
                ) from None
            subjects[field] = validate_subject(subject)

        safe = self.redactor.redact_obj(redactable)
        if not isinstance(safe, dict):
            raise TypeError("Action response must be a JSON object")
        return {
            key: (
                subjects[key]
                if key in subjects
                else safe[key]
            )
            for key in value
        }

    def _target(self, key: str) -> tuple[str, str, str]:
        try:
            repository = EDITABLE[key]
        except KeyError as error:
            raise ValueError(
                "편집 허용 목록에 없는 source key다"
            ) from error
        if repository not in self.settings.source_repositories:
            raise ValueError(
                "Source repository is not configured"
            )
        path = key.split("/", 1)[1]
        return repository, repository, path

    @staticmethod
    def _decode_file_content(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return base64.b64decode(
                value,
                validate=True,
            ).decode("utf-8")
        raise ValueError("Source file content is invalid")

    def commit_and_merge(
        self,
        *,
        key: str,
        content: str,
        title: str,
        description: str,
        expected_head: str,
        actor_sub: str,
    ) -> dict:
        (
            _requested_content_bytes,
            title_value,
            description_value,
            pull_request_description,
        ) = self._validate_commit_request(
            key=key,
            content=content,
            title=title,
            description=description,
            expected_head=expected_head,
            actor_sub=actor_sub,
        )
        try:
            self.redactor.assert_safe(title_value)
            self.redactor.assert_safe(description_value)
        except RedactionError:
            raise ValueError(
                "Pull request metadata is invalid"
            ) from None
        logical_repo, repository, path = self._target(key)
        codecommit = self.clients.codecommit

        current_head = codecommit.get_branch(
            repositoryName=repository,
            branchName="main",
        )["branch"]["commitId"]
        if current_head != expected_head:
            raise ValueError(
                "Source main branch changed; refresh and retry"
            )

        current_file = codecommit.get_file(
            repositoryName=repository,
            commitSpecifier=expected_head,
            filePath=path,
        )
        current_content = self._decode_file_content(
            current_file["fileContent"]
        )
        allowed_content = fixed_demo_mutation(
            current_content
        )
        if content != allowed_content:
            raise ValueError(
                "Source content does not match fixed demo mutation"
            )
        content_bytes = allowed_content.encode("utf-8")

        try:
            token = self.token_factory()
        except Exception:
            raise ValueError(
                "Action token is invalid"
            ) from None
        if (
            not isinstance(token, str)
            or _BRANCH_TOKEN.fullmatch(token) is None
        ):
            raise ValueError("Action token is invalid")
        try:
            action_time = self.now()
        except Exception:
            raise ValueError(
                "Action timestamp is invalid"
            ) from None
        branch, merged_at = self._prepare_action_time(
            action_time,
            token,
        )
        codecommit.create_branch(
            repositoryName=repository,
            branchName=branch,
            commitId=expected_head,
        )
        commit = codecommit.create_commit(
            repositoryName=repository,
            branchName=branch,
            parentCommitId=expected_head,
            authorName="demo-ui",
            email="demo-ui@example.invalid",
            commitMessage=title,
            putFiles=[
                {
                    "filePath": path,
                    "fileContent": content_bytes,
                }
            ],
        )["commitId"]

        pull_request = codecommit.create_pull_request(
            title=title,
            description=pull_request_description,
            targets=[
                {
                    "repositoryName": repository,
                    "sourceReference": branch,
                    "destinationReference": "main",
                }
            ],
        )["pullRequest"]
        pull_request_id = pull_request["pullRequestId"]

        merged = (
            codecommit.merge_pull_request_by_fast_forward(
                pullRequestId=pull_request_id,
                repositoryName=repository,
                sourceCommitId=commit,
            )
        )
        merge_metadata = merged["pullRequest"][
            "pullRequestTargets"
        ][0]["mergeMetadata"]
        return self._response(
            {
                "ok": True,
                "operation_id": (
                    f"source-pr:{pull_request_id}"
                ),
                "repo": logical_repo,
                "path": path,
                "branch": branch,
                "commit": commit,
                "pull_request_id": pull_request_id,
                "merge_commit_id": merge_metadata.get(
                    "mergeCommitId"
                ),
                "merged_at": merged_at,
            }
        )

    @staticmethod
    def _allowed_wiki_path(path: str) -> bool:
        return (
            path == "CURSOR.json"
            or path.endswith(".md")
            or path.endswith(".json")
        )

    def _differences(
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
            response = self.clients.codecommit.get_differences(
                **request
            )
            differences.extend(
                response.get("differences", [])
            )
            next_token = response.get("NextToken")
            if not next_token:
                return differences

    def _read_wiki_file_required(
        self,
        *,
        path: str,
        commit: str,
    ) -> str:
        try:
            response = self.clients.codecommit.get_file(
                repositoryName=self.settings.wiki_repo,
                commitSpecifier=commit,
                filePath=path,
            )
            return self._decode_file_content(
                response["fileContent"]
            )
        except Exception:
            raise ValueError(
                "Wiki pull request review is incomplete"
            ) from None

    def approve(
        self,
        *,
        wiki_pr: str,
        expected_source_commit: str,
        actor_sub: str,
    ) -> dict:
        actor_sub = self._validate_approve_request(
            wiki_pr=wiki_pr,
            expected_source_commit=expected_source_commit,
            actor_sub=actor_sub,
        )
        codecommit = self.clients.codecommit
        pull_request = codecommit.get_pull_request(
            pullRequestId=wiki_pr,
        )["pullRequest"]
        if pull_request.get("pullRequestStatus") != "OPEN":
            raise ValueError("Wiki pull request is not open")

        targets = pull_request.get("pullRequestTargets")
        if not isinstance(targets, list) or len(targets) != 1:
            raise ValueError(
                "Wiki pull request target is invalid"
            )
        target = targets[0]
        if (
            target.get("repositoryName")
            != self.settings.wiki_repo
        ):
            raise ValueError(
                "Pull request does not target the wiki repository"
            )
        if (
            target.get("destinationReference")
            != "refs/heads/main"
        ):
            raise ValueError(
                "Wiki pull request destination is invalid"
            )
        source_reference = target.get("sourceReference")
        if (
            not isinstance(source_reference, str)
            or not source_reference.startswith(
                "refs/heads/wikiagent/"
            )
        ):
            raise ValueError(
                "Wiki pull request source branch is invalid"
            )

        source_commit = target.get("sourceCommit")
        if source_commit != expected_source_commit:
            raise ValueError(
                "Wiki pull request source changed; refresh and retry"
            )
        destination_commit = target.get(
            "destinationCommit"
        )
        if (
            not isinstance(destination_commit, str)
            or not destination_commit
        ):
            raise ValueError(
                "Wiki pull request destination commit is invalid"
            )

        differences = self._differences(
            repository=self.settings.wiki_repo,
            before_commit=destination_commit,
            after_commit=source_commit,
        )
        if not differences:
            raise ValueError(
                "Wiki pull request has no changed files"
            )
        review_entries: list[
            tuple[str | None, str | None]
        ] = []
        for difference in differences:
            try:
                (
                    before_path,
                    after_path,
                ) = declared_difference_paths(difference)
            except ReviewPolicyError:
                raise ValueError(
                    "Wiki pull request review is incomplete"
                ) from None
            paths = tuple(
                path
                for path in (before_path, after_path)
                if path is not None
            )
            if any(
                not self._allowed_wiki_path(path)
                for path in paths
            ):
                raise ValueError(
                    "Wiki pull request changes a disallowed file"
                )
            review_entries.append(
                (before_path, after_path)
            )

        for before_path, after_path in review_entries:
            before = (
                self._read_wiki_file_required(
                    path=before_path,
                    commit=destination_commit,
                )
                if before_path is not None
                else ""
            )
            after = (
                self._read_wiki_file_required(
                    path=after_path,
                    commit=source_commit,
                )
                if after_path is not None
                else ""
            )
            unified = full_unified_diff(
                before=before,
                after=after,
                before_path=before_path,
                after_path=after_path,
                context_lines=3,
            )
            if len(unified) > MAX_REVIEW_DIFF_LINES:
                raise ValueError(
                    "Wiki pull request review is incomplete"
                )

        merged = (
            codecommit.merge_pull_request_by_fast_forward(
                pullRequestId=wiki_pr,
                repositoryName=self.settings.wiki_repo,
                sourceCommitId=expected_source_commit,
            )
        )
        merged_pull_request = merged["pullRequest"]
        merge_metadata = merged_pull_request[
            "pullRequestTargets"
        ][0]["mergeMetadata"]
        return self._response(
            {
                "ok": True,
                "operation_id": f"wiki-pr:{wiki_pr}",
                "pull_request_id": wiki_pr,
                "status": "CLOSED",
                "merged_by": actor_sub,
                "merge_option": "FAST_FORWARD_MERGE",
                "merge_commit_id": merge_metadata.get(
                    "mergeCommitId"
                ),
            },
            subject_fields=("merged_by",),
        )

    def request_refresh(self, *, actor_sub: str) -> dict:
        actor_sub = self._validate_actor_sub(actor_sub)
        lambda_client = self.clients.lambda_client
        if lambda_client is None:
            raise ValueError("Collector Lambda client is unavailable")
        lambda_client.invoke(
            FunctionName=(
                self.settings.collector_function_name
            ),
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "source": "demo-ui",
                    "requested_by": actor_sub,
                }
            ).encode("utf-8"),
        )
        return {
            "ok": True,
            "operation_id": REFRESH_OPERATION_ID,
            "accepted": True,
        }
