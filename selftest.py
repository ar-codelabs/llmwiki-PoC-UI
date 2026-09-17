"""AWS 를 부르지 않는 콘솔 자체 검사.

⚠️ **저장소 배치를 전제하지 않는다.** 이 번들은 git 저장소가 아닌 단독 폴더로 배포되므로
경로는 이 파일 위치를 기준으로 잡고, git 검사는 저장소일 때만 돈다. 이전 판은
`demo-ui/tests` 를 고정 경로로 부르고 부모를 저장소 루트로 가정했다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE / "tests"
BROWSER_SMOKE = TESTS / "test_browser_smoke.py"

TEST_COMMANDS = [
    (
        "local pytest tree",
        [
            sys.executable,
            "-m",
            "pytest",
            str(TESTS),
            f"--ignore={BROWSER_SMOKE}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
    ),
    (
        "browser smoke",
        [
            sys.executable,
            "-m",
            "pytest",
            str(BROWSER_SMOKE),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
    ),
]


def _write_process_output(
    result: subprocess.CompletedProcess[str],
) -> None:
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


def _is_git_repository(path: Path, environment: dict[str, str]) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def check_git_whitespace(
    repository: Path,
    environment: dict[str, str],
) -> int:
    """git 저장소 안에서만 공백 검사를 돌린다.

    단독 배포본은 저장소가 아니므로 이 검사를 건너뛴다. 건너뛴 사실을 출력해
    통과와 구분한다.
    """
    if not _is_git_repository(repository, environment):
        print(
            "[selftest] SKIP git whitespace (git 저장소가 아니다)",
            flush=True,
        )
        return 0

    fixed_checks = (
        ("working tree whitespace", ["git", "diff", "--check"]),
        (
            "index whitespace",
            ["git", "diff", "--cached", "--check"],
        ),
    )
    for label, command in fixed_checks:
        print(f"[selftest] {label}", flush=True)
        result = subprocess.run(
            command,
            cwd=repository,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    print("[selftest] untracked whitespace", flush=True)
    discovery = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "."],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if discovery.returncode != 0:
        _write_process_output(discovery)
        return discovery.returncode

    for relative_path in discovery.stdout.splitlines():
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--check",
                "--",
                "/dev/null",
                relative_path,
            ],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        has_output = bool(result.stdout or result.stderr)
        if result.returncode in (0, 1) and not has_output:
            continue
        _write_process_output(result)
        if result.returncode in (0, 1):
            return 1
        return result.returncode
    return 0


def main() -> int:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    for label, command in TEST_COMMANDS:
        print(f"[selftest] {label}", flush=True)
        result = subprocess.run(
            command,
            cwd=HERE,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    return check_git_whitespace(HERE, environment)


if __name__ == "__main__":
    raise SystemExit(main())
