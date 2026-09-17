"""데모 1 실시간 실행. 소스 편집 → 커밋·머지 → 단계 추적 → 사람 승인 → 확정.

⚠️ **위키 저장소는 Git 이다. S3 가 아니다.** 고객이 2026-08-19 에 확정한 것이
「위키 = git repo」·「자동 merge 배제」·「md + JSON pair」다. S3 는 이 구조에서
**스킬 카탈로그 배포용**이고 문서 정본이 아니다. 문서를 S3 에 두면 되돌리기가 Git 하나로
수렴하지 않고 `verified[]`·PR 리뷰 모델이 성립하지 않는다.

⚠️ **사람이 편집하는 것은 소스 코드다. 위키 문서가 아니다.** 같은 미팅에서 고객이
「쓰기 권한은 자동 파이프라인 전용, 개발자는 읽기 전용으로 로컬에 내려 씀」을 확정했다.
UI 에 위키 문서 편집기를 두면 그 결정과 반대가 된다. 그래서 이 모듈은 소스만 쓰게 하고
위키는 **읽기와 승인(PR merge)** 만 노출한다.

⚠️ **쓰기 대상을 allowlist 로 좁힌다.** 이 서버는 인증이 없다. 오조작이 임의 repo 를
고치지 못하게 편집 가능한 (repo, path) 를 열거한다.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from functools import lru_cache

import boto3
import config
from backend.action_service import ActionService
from backend.clients import AwsClients
from backend.query_service import QueryService
from backend.redaction import Redactor
from backend.settings import Settings

LAMBDA_LG = "/aws/lambda/vdwiki-trigger"
LEGACY_ACTOR_SUB = (
    "00000000-0000-4000-8000-abcdef123456"
)


@dataclass(frozen=True)
class _LiveRuntime:
    settings: config.LegacyConfig
    codecommit: object
    logs: object
    s3: object


@lru_cache(maxsize=1)
def _runtime() -> _LiveRuntime:
    settings = config.load()
    session = boto3.Session(
        profile_name=settings.profile,
        region_name=settings.region,
    )
    return _LiveRuntime(
        settings=settings,
        codecommit=session.client("codecommit"),
        logs=session.client("logs"),
        s3=session.client("s3"),
    )


def _query_settings(settings: config.LegacyConfig) -> Settings:
    return Settings(
        region=settings.region,
        wiki_repo=settings.wiki_repo,
        repo_prefix=settings.repo_prefix,
        lambda_log_group=LAMBDA_LG,
        runtime_log_group=(
            "/aws/bedrock-agentcore/runtimes/"
            f"{settings.runtime_id}-DEFAULT"
        ),
        skills_bucket=(
            f"vdwiki-skills-{settings.account}-"
            f"{settings.region}"
        ),
        snapshot_bucket="",
        collector_function_name="",
        redaction_secret_arn="",
        user_pool_id="",
        app_client_id="",
        cognito_domain="",
        callback_path="/callback",
    )


def _action_settings(
    settings: config.LegacyConfig,
) -> Settings:
    return Settings(
        region=getattr(settings, "region", ""),
        wiki_repo=getattr(
            settings,
            "wiki_repo",
            "vdwiki-wiki",
        ),
        repo_prefix=getattr(
            settings,
            "repo_prefix",
            "vdwiki-",
        ),
        lambda_log_group=LAMBDA_LG,
        runtime_log_group="",
        skills_bucket="",
        snapshot_bucket="",
        collector_function_name="",
        redaction_secret_arn="",
        user_pool_id="",
        app_client_id="",
        cognito_domain="",
        callback_path="/callback",
    )


def _build_action_service(
    runtime: _LiveRuntime,
) -> ActionService:
    return ActionService(
        settings=_action_settings(runtime.settings),
        clients=AwsClients(
            codecommit=runtime.codecommit,
            logs=runtime.logs,
            s3=runtime.s3,
        ),
        redactor=Redactor(
            getattr(runtime.settings, "redact", ())
        ),
        now=time.time,
    )


@lru_cache(maxsize=1)
def _action_service() -> ActionService:
    return _build_action_service(_runtime())


def _action_service_for(
    runtime: _LiveRuntime,
) -> ActionService:
    service = _action_service()
    if service.clients.codecommit is runtime.codecommit:
        return service
    return _build_action_service(runtime)


@lru_cache(maxsize=1)
def _query_service() -> QueryService:
    runtime = _runtime()
    return QueryService(
        settings=_query_settings(runtime.settings),
        clients=AwsClients(
            codecommit=runtime.codecommit,
            logs=runtime.logs,
            s3=runtime.s3,
        ),
        redactor=Redactor(runtime.settings.redact),
    )


def redact(obj):
    raw = json.dumps(obj, ensure_ascii=False)
    return json.loads(config.redact(raw))


def _read(repo: str, path: str, sha: str = "refs/heads/main") -> str | None:
    codecommit = _runtime().codecommit
    try:
        r = codecommit.get_file(
            repositoryName=repo,
            commitSpecifier=sha,
            filePath=path,
        )
    except Exception:
        return None
    c = r["fileContent"]
    return c.decode("utf-8") if isinstance(c, bytes) else base64.b64decode(c).decode("utf-8")


# ---------------------------------------------------------------------------
# 1. 소스 읽기
# ---------------------------------------------------------------------------

def list_editable() -> dict:
    return _query_service().list_editable()


# ---------------------------------------------------------------------------
# 2. 커밋 + PR + 머지 (트리거 발화 지점)
# ---------------------------------------------------------------------------

def commit_and_merge(
    key: str,
    content: str,
    title: str,
    description: str,
    expected_head: str | None = None,
) -> dict:
    if not isinstance(expected_head, str) or not expected_head:
        raise ValueError(
            "Source review token is required"
        )
    runtime = _runtime()
    return _action_service_for(
        runtime
    ).commit_and_merge(
        key=key,
        content=content,
        title=title,
        description=description,
        expected_head=expected_head,
        actor_sub=LEGACY_ACTOR_SUB,
    )


def progress(pr_id: str, since_ms: int | None = None) -> dict:
    return _query_service().progress(pr_id, since_ms)


# ---------------------------------------------------------------------------
# 4. 위키 PR 내용 (승인 전 검토용)
# ---------------------------------------------------------------------------

def wiki_pr_detail(wiki_pr: str) -> dict:
    return _query_service().wiki_pr_detail(wiki_pr)


# ---------------------------------------------------------------------------
# 5. 사람 승인 = 위키 PR 머지 (자동 merge 없음)
# ---------------------------------------------------------------------------

def approve(
    wiki_pr: str,
    expected_source_commit: str | None = None,
) -> dict:
    if (
        not isinstance(expected_source_commit, str)
        or not expected_source_commit
    ):
        raise ValueError(
            "Wiki review token is required"
        )
    runtime = _runtime()
    return _action_service_for(runtime).approve(
        wiki_pr=wiki_pr,
        expected_source_commit=expected_source_commit,
        actor_sub=LEGACY_ACTOR_SUB,
    )


# ---------------------------------------------------------------------------
# 6. 확정 확인 (최신성)
# ---------------------------------------------------------------------------

def wiki_repo_info() -> dict:
    """위키가 어디에 저장되는지. 저장소 메타와 트리를 함께 준다."""
    return _query_service().wiki_repo_info()


def wiki_history(limit: int = 40) -> dict:
    """main 의 커밋 이력. 메시지 metadata 를 파싱해 종류를 나눈다."""
    return _query_service().wiki_history(limit)


def wiki_commit(commit: str) -> dict:
    """커밋 하나의 상세. 부모와의 diff 를 붙인다."""
    return _query_service().wiki_commit(commit)


def finalization(repo: str, paths: list[str]) -> dict:
    return _query_service().finalization(repo, paths)
