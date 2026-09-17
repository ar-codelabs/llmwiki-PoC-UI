"""데모 UI 엔드포인트 점검. 응답 코드와 핵심 필드, 익명화 위반을 함께 본다."""
import argparse
import json
import re
import urllib.request
from collections.abc import Sequence

import config

BASE = "http://127.0.0.1:8899"
CHECKS = [
    ("/api/wiki-repo", ["repo", "kind", "tree", "clone_url"]),
    ("/api/wiki-history?limit=10", ["head", "commits"]),
    ("/api/editable", ["files"]),
    ("/api/finalization?repo=appsync&paths=api/appsync-metadata.md", ["docs", "cursor"]),
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="실행 중인 로컬 demo-ui endpoint를 점검한다.",
    )
    parser.add_argument(
        "--base",
        default=BASE,
        help=f"점검할 base URL (기본값: {BASE})",
    )
    args = parser.parse_args(argv)
    forbidden = config.load().forbidden

    allraw = ""
    for path, keys in CHECKS:
        try:
            with urllib.request.urlopen(
                args.base + path,
                timeout=60,
            ) as response:
                raw = response.read().decode()
                code = response.status
        except Exception as error:
            print(f"❌ {path}  {type(error).__name__}: {error}")
            continue
        allraw += raw
        data = json.loads(raw)
        missing = [key for key in keys if key not in data]
        print(
            f"{'✅' if code == 200 and not missing else '❌'} "
            f"{path}  HTTP {code}  {len(raw):,}B"
            + (f"  누락 {missing}" if missing else "")
        )

    history = json.loads(
        urllib.request.urlopen(
            args.base + "/api/wiki-history?limit=10"
        ).read()
    )
    first = history["commits"][0]["commit"]
    with urllib.request.urlopen(
        f"{args.base}/api/wiki-commit?id={first}"
    ) as response:
        wiki_commit = json.loads(response.read().decode())
    allraw += json.dumps(wiki_commit, ensure_ascii=False)
    print(
        "✅ /api/wiki-commit  "
        f"파일 {len(wiki_commit['files'])}개 · "
        f"부모 {str(wiki_commit['parent'])[:12]}"
    )

    print()
    print("커밋 이력 (상위 6)")
    for commit in history["commits"][:6]:
        metadata = commit["meta"]
        print(
            f"  [{commit['kind']}] {commit['commit'][:12]} "
            f"{commit['author']:<10} {commit['subject'][:46]}"
        )
        if metadata.get("source_pull_request"):
            print(f"        원본 PR {metadata['source_pull_request']}")
        if metadata.get("pr_description"):
            print(
                "        PR 설명 "
                f"{metadata['pr_description'].splitlines()[:3]}"
            )

    print()
    bad = {
        pattern: len(re.findall(pattern, allraw))
        for pattern in forbidden
    }
    print("익명화 위반:", bad, "· 합계", sum(bad.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
