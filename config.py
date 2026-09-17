"""데모 UI 설정. **계정 식별자와 사람 이름을 소스에 두지 않는다.**

⚠️ 이 저장소는 익명화 규약을 가진다(`customer/verify_bundle.py` 가
`BUNDLE_ANONYMIZATION_FORBIDDEN_MATCHES=0` 을 요구한다). 그런데 데모 UI 는 화면에
나갈 값을 치환해야 하므로 **치환 대상 원문**이 필요하다. 그 원문을 소스에 적으면
익명화 규약을 스스로 깨므로, 실제 값은 커밋하지 않는 로컬 파일에서 읽는다.

설정 방법 두 가지 중 하나
  1) `demo-ui/local.json` (gitignore 됨). `local.json.example` 을 복사해 채운다.
  2) 환경변수 `VDWIKI_ACCOUNT` · `VDWIKI_PROFILE` · `VDWIKI_REGION` ·
     `VDWIKI_RUNTIME_ID` · `VDWIKI_REDACT`(JSON 배열)

⚠️ `redact` 가 비면 익명화가 **무력화**된다. 그래서 비어 있으면 경고하고, 화면·산출물
생성 경로는 `require_redact()` 로 막는다. 조용히 통과시키면 계정 ID 가 담긴 파일을
공유하게 된다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from backend.settings import (
    DEFAULT_SOURCE_REPOSITORIES,
    DEFAULT_WIKI_REPOSITORY,
    parse_source_repositories,
    repository_names,
)

HERE = Path(__file__).parent
LOCAL = HERE / "local.json"

_DEFAULTS = {
    "region": "ap-northeast-2",
    "source_repositories": DEFAULT_SOURCE_REPOSITORIES,
    "wiki_repo": DEFAULT_WIKI_REPOSITORY,
    "repo_prefix": "",
}


@dataclass(frozen=True)
class LegacyConfig:
    account: str
    profile: str
    region: str
    runtime_id: str
    source_repositories: tuple[str, ...]
    wiki_repo: str
    repo_prefix: str
    redact: tuple[tuple[str, str], ...]
    forbidden: tuple[str, ...]


def _load_values() -> dict:
    cfg = dict(_DEFAULTS)
    if LOCAL.exists():
        try:
            cfg.update(json.loads(LOCAL.read_text(encoding="utf-8")))
        except Exception as e:
            raise SystemExit(
                f"❌ {LOCAL.name} 을 읽을 수 없다: "
                f"{type(e).__name__}: {e}"
            ) from e
    for key, env in (("account", "VDWIKI_ACCOUNT"), ("profile", "VDWIKI_PROFILE"),
                     ("region", "VDWIKI_REGION"), ("runtime_id", "VDWIKI_RUNTIME_ID"),
                     ("wiki_repo", "VDWIKI_WIKI_REPO")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if os.environ.get("VDWIKI_SOURCE_REPOSITORIES"):
        cfg["source_repositories"] = os.environ[
            "VDWIKI_SOURCE_REPOSITORIES"
        ]
    if os.environ.get("VDWIKI_REDACT"):
        cfg["redact"] = json.loads(os.environ["VDWIKI_REDACT"])
    return cfg


def _need(cfg: dict, key: str) -> str:
    v = cfg.get(key)
    if not v:
        raise SystemExit(
            f"❌ 설정 없음: {key}\n"
            f"   {LOCAL.name} 을 만들거나 환경변수를 설정한다.\n"
            f"   예: cp local.json.example local.json  # 그다음 값을 채운다"
        )
    return str(v)


@lru_cache(maxsize=1)
def load() -> LegacyConfig:
    cfg = _load_values()
    account = _need(cfg, "account")
    profile = _need(cfg, "profile")
    wiki_repo = str(
        cfg.get("wiki_repo") or DEFAULT_WIKI_REPOSITORY
    )
    raw_source_repositories = cfg.get(
        "source_repositories",
        DEFAULT_SOURCE_REPOSITORIES,
    )
    if isinstance(raw_source_repositories, str):
        source_repositories = parse_source_repositories(
            raw_source_repositories,
            wiki_repo=wiki_repo,
        )
    elif isinstance(raw_source_repositories, (list, tuple)):
        source_repositories = tuple(
            raw_source_repositories
        )
        repository_names(
            source_repositories,
            wiki_repo,
        )
    else:
        raise SystemExit(
            "❌ source_repositories 설정 형식이 올바르지 않다"
        )
    redactions: list[tuple[str, str]] = [
        tuple(pair) for pair in cfg.get("redact", [])
    ]
    redactions += [(account, "<계정>"), (profile, "<프로파일>")]
    forbidden = tuple(
        [source for source, _ in redactions] + ["assumed-role"]
    )
    return LegacyConfig(
        account=account,
        profile=profile,
        region=_need(cfg, "region"),
        runtime_id=str(cfg.get("runtime_id") or ""),
        source_repositories=source_repositories,
        wiki_repo=wiki_repo,
        repo_prefix="",
        redact=tuple(redactions),
        forbidden=forbidden,
    )


def require_redact(settings: LegacyConfig | None = None) -> None:
    """치환 규칙이 사람 이름·역할까지 덮는지 확인한다.

    계정 ID 와 프로파일만 있으면 IAM 역할 ARN 안의 alias 가 그대로 남는다.
    화면과 공유 산출물을 만들기 전에 부른다.
    """
    loaded = settings or load()
    if len(loaded.redact) <= 2:
        raise SystemExit(
            "❌ 익명화 규칙이 계정·프로파일뿐이다. IAM 역할 ARN 의 사람 alias 가 남는다.\n"
            f"   {LOCAL.name} 의 `redact` 에 역할 ARN 과 alias 를 추가한다.\n"
            "   형식: [[\"arn:aws:sts::<계정>:assumed-role/Admin/<alias>\", \"<검토자 A>\"], ...]"
        )


def redact(
    text: str,
    settings: LegacyConfig | None = None,
) -> str:
    for src, mask in (settings or load()).redact:
        text = text.replace(src, mask)
    return text


def leaks(
    text: str,
    settings: LegacyConfig | None = None,
) -> list[str]:
    return [
        pattern
        for pattern in (settings or load()).forbidden
        if pattern and pattern in text
    ]


_LEGACY_FIELDS = {
    "ACCOUNT": "account",
    "PROFILE": "profile",
    "REGION": "region",
    "RUNTIME_ID": "runtime_id",
    "SOURCE_REPOSITORIES": "source_repositories",
    "WIKI_REPO": "wiki_repo",
    "REPO_PREFIX": "repo_prefix",
    "REDACT": "redact",
    "FORBIDDEN": "forbidden",
}


def __getattr__(name: str) -> object:
    field = _LEGACY_FIELDS.get(name)
    if field is None:
        raise AttributeError(name)
    settings = load()
    value = getattr(settings, field)
    if name in {"REDACT", "FORBIDDEN"}:
        return list(value)
    return value
