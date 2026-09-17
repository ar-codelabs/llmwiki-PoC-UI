import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SOURCE_REPOSITORIES = (
    "shaka-player",
    "mediamtx",
    "saleor",
)
DEFAULT_WIKI_REPOSITORY = "apps-knowledge"

_CODECOMMIT_REPOSITORY_NAME = re.compile(r"[\w.-]{1,100}")
_STRING_ENVIRONMENT_FIELDS = (
    ("region", "AWS_REGION"),
    ("wiki_repo", "DEMO_WIKI_REPO"),
    ("lambda_log_group", "DEMO_LAMBDA_LOG_GROUP"),
    ("runtime_log_group", "DEMO_RUNTIME_LOG_GROUP"),
    ("skills_bucket", "DEMO_SKILLS_BUCKET"),
    ("snapshot_bucket", "DEMO_SNAPSHOT_BUCKET"),
    ("collector_function_name", "DEMO_COLLECTOR_FUNCTION"),
    ("redaction_secret_arn", "DEMO_REDACTION_SECRET_ARN"),
    ("user_pool_id", "DEMO_USER_POOL_ID"),
    ("app_client_id", "DEMO_APP_CLIENT_ID"),
    ("cognito_domain", "DEMO_COGNITO_DOMAIN"),
    ("callback_path", "DEMO_CALLBACK_PATH"),
)


def _validated_repository_name(
    value: str,
    *,
    label: str,
) -> str:
    if (
        not isinstance(value, str)
        or _CODECOMMIT_REPOSITORY_NAME.fullmatch(value) is None
        or value.lower().endswith(".git")
    ):
        raise ValueError(
            f"{label}: 올바른 CodeCommit repository 이름이 필요하다"
        )
    return value


def repository_names(
    source_repositories: tuple[str, ...],
    wiki_repo: str,
) -> tuple[str, ...]:
    validated_wiki = _validated_repository_name(
        wiki_repo,
        label="wiki repository",
    )
    if not source_repositories:
        raise ValueError("source repository가 하나 이상 필요하다")
    validated_sources = tuple(
        _validated_repository_name(
            repository,
            label="source repository",
        )
        for repository in source_repositories
    )
    if len(set(validated_sources)) != len(validated_sources):
        raise ValueError(
            "source repository 이름은 중복될 수 없다"
        )
    if validated_wiki in validated_sources:
        raise ValueError(
            "source repository와 Wiki repository는 달라야 한다"
        )
    return (*validated_sources, validated_wiki)


def parse_source_repositories(
    value: str,
    *,
    wiki_repo: str = DEFAULT_WIKI_REPOSITORY,
) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(
            "source repository 설정은 문자열이어야 한다"
        )
    source_repositories = tuple(value.split(","))
    repository_names(source_repositories, wiki_repo)
    return source_repositories


@dataclass(frozen=True)
class Settings:
    region: str
    wiki_repo: str
    lambda_log_group: str
    runtime_log_group: str
    skills_bucket: str
    snapshot_bucket: str
    collector_function_name: str
    redaction_secret_arn: str
    user_pool_id: str
    app_client_id: str
    cognito_domain: str
    callback_path: str
    source_repositories: tuple[str, ...] = (
        DEFAULT_SOURCE_REPOSITORIES
    )
    repo_prefix: str = ""

    def __post_init__(self) -> None:
        repository_names(
            self.source_repositories,
            self.wiki_repo,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        missing = [
            environment_name
            for _, environment_name in (
                _STRING_ENVIRONMENT_FIELDS
            )
            if not environ.get(environment_name)
        ]
        if not environ.get("DEMO_SOURCE_REPOSITORIES"):
            missing.append("DEMO_SOURCE_REPOSITORIES")
        if missing:
            raise ValueError(
                "Missing required environment variables: "
                + ", ".join(missing)
            )

        wiki_repo = environ["DEMO_WIKI_REPO"]
        return cls(
            **{
                field_name: environ[environment_name]
                for field_name, environment_name in (
                    _STRING_ENVIRONMENT_FIELDS
                )
            },
            source_repositories=parse_source_repositories(
                environ["DEMO_SOURCE_REPOSITORIES"],
                wiki_repo=wiki_repo,
            ),
        )
