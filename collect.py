"""AWS 계정 상태를 수집해 local snapshot.json으로 저장한다.

이 모듈은 local profile adapter다. 실제 수집, fast merge, redaction,
직렬화는 Lambda-safe ``CollectorService``가 담당한다. 설정 read,
AWS Session/client 생성, local file write는 ``main()`` 실행 뒤에만
발생한다.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import boto3
import config
from backend.clients import AwsClients
from backend.collector_service import CollectorService
from backend.redaction import Redactor
from backend.settings import Settings

LOG_GROUP = "/aws/lambda/vdwiki-trigger"
OUT = Path(__file__).parent / "snapshot.json"


def _service(
    legacy: config.LegacyConfig,
) -> CollectorService:
    settings = Settings(
        region=legacy.region,
        wiki_repo=legacy.wiki_repo,
        repo_prefix=legacy.repo_prefix,
        lambda_log_group=LOG_GROUP,
        runtime_log_group="",
        skills_bucket=(
            f"vdwiki-skills-{legacy.account}-{legacy.region}"
        ),
        snapshot_bucket="local-snapshot-unused",
        collector_function_name="",
        redaction_secret_arn="",
        user_pool_id="",
        app_client_id="",
        cognito_domain="",
        callback_path="",
    )
    session = boto3.Session(
        profile_name=legacy.profile,
        region_name=legacy.region,
    )
    clients = AwsClients(
        codecommit=session.client("codecommit"),
        logs=session.client("logs"),
        s3=session.client("s3"),
    )
    return CollectorService(
        settings=settings,
        clients=clients,
        redactor=Redactor(legacy.redact),
        now=lambda: datetime.now(UTC),
    )


def _write_local_snapshot(
    service: CollectorService,
    snapshot: dict,
) -> bytes:
    body = service.serialize_snapshot(snapshot)
    OUT.write_bytes(body)
    return body


def _print_fast(snapshot: dict, body: bytes) -> None:
    wiki = snapshot["wiki"]
    print(
        f"✅ fast · 실행 {len(snapshot['runs'])}건 · "
        f"위키 HEAD {wiki['head'][:12]} · "
        f"{len(body):,}B"
    )


def _print_full(snapshot: dict, body: bytes) -> None:
    wiki = snapshot["wiki"]
    bootstrap = snapshot["bootstrap"]
    llm_runs = sum(
        1
        for run in snapshot["runs"]
        if run.get("agent_invoked")
    )
    print(f"✅ {OUT.name} · {len(body):,} 바이트")
    print(
        f"   실행 {len(snapshot['runs'])}건 "
        f"(LLM 호출 {llm_runs}건)"
    )
    print(
        f"   위키 문서 {wiki['total_md']}건 · "
        f"JSON pair {wiki['pair_ok']}/"
        f"{wiki['total_md']} · "
        f"상태 {wiki['by_status']}"
    )
    print(
        "   폴더별 "
        + " · ".join(
            f"{name} {values['total']}"
            for name, values in sorted(
                wiki["by_top"].items(),
                key=lambda item: -item[1]["total"],
            )
        )
    )
    print(
        f"   Bootstrap 브랜치 "
        f"{len(bootstrap['branches'])}개 · "
        f"PR {len(bootstrap['pull_requests'])}건"
    )
    print(
        f"   스킬 {len(snapshot['skills'])}종 · "
        f"소스 repo {len(snapshot['repos'])}개"
    )
    print("   익명화 위반 0건")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "AWS 계정 상태를 익명화된 snapshot.json으로 "
            "수집한다."
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "기존 snapshot을 유지하며 변하는 값만 "
            "다시 수집한다."
        ),
    )
    args = parser.parse_args(argv)

    legacy = config.load()
    service = _service(legacy)
    if args.fast and OUT.exists():
        previous = json.loads(
            OUT.read_text(encoding="utf-8")
        )
        snapshot = service.build_fast_snapshot(
            previous,
            hours=6,
        )
        body = _write_local_snapshot(
            service,
            snapshot,
        )
        _print_fast(snapshot, body)
        return 0

    snapshot = service.build_snapshot()
    body = _write_local_snapshot(service, snapshot)
    _print_full(snapshot, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
