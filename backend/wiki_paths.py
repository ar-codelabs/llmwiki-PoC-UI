"""Automation plane 의 canonical Wiki path 계약을 가져온다.

⚠️ **경로 계약을 여기서 다시 구현하지 않는다.** 문서와 파생 JSON 의 짝, navigation 판정은
Automation plane 이 정본이고, 콘솔이 따로 구현하면 두 곳이 갈린다. 그래서 import 만 한다.

찾는 순서는 셋이다.

  1 이미 `sys.path` 에 있으면 그대로 쓴다(`PYTHONPATH` 로 연결한 경우)
  2 `WIKI_AGENT_PATH` 환경변수가 가리키는 디렉터리
  3 알려진 배치 후보: 저장소 안(`<root>/poc`)과 참조 코드 나란히 둔 배치
    (`<parent>/automation-reference/poc`)

3 이 있어서 참조 코드를 나란히 두면 `PYTHONPATH` 없이도 동작한다. 어느 것도 없으면
무엇을 어디에 두어야 하는지 알려 주고 실패한다. 조용히 자체 구현으로 넘어가지 않는다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_CONSOLE_ROOT = _HERE.parents[1]
_PARENT = _HERE.parents[2]


def _candidates() -> tuple[Path, ...]:
    override = os.environ.get("WIKI_AGENT_PATH")
    found: list[Path] = []
    if override:
        found.append(Path(override))
    found.append(_PARENT / "poc")
    found.append(_PARENT / "automation-reference" / "poc")
    found.append(_CONSOLE_ROOT / "poc")
    return tuple(found)


def _install() -> None:
    for candidate in _candidates():
        if (candidate / "wikiagent" / "paths.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    searched = "\n  ".join(str(path) for path in _candidates())
    raise ModuleNotFoundError(
        "Automation plane 의 `wikiagent` 를 찾지 못했다. 참조 코드를 이 콘솔과 "
        "나란히 두거나 `WIKI_AGENT_PATH` 로 `poc` 디렉터리를 지정한다.\n"
        f"  찾아본 경로:\n  {searched}"
    )


try:
    from wikiagent.paths import (
        expected_pair_files,
        is_navigation_markdown,
        metadata_path,
    )
except ModuleNotFoundError:
    _install()
    from wikiagent.paths import (  # type: ignore[no-redef]
        expected_pair_files,
        is_navigation_markdown,
        metadata_path,
    )

__all__ = (
    "expected_pair_files",
    "is_navigation_markdown",
    "metadata_path",
)
