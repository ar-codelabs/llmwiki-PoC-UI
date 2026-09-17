# Demo Console 연결 가이드

`automation-reference`를 배포한 뒤, 그 파이프라인이 실제로 도는 것을 **화면으로
확인할 수 있는** 로컬 콘솔입니다. Cognito 로그인 없이 `127.0.0.1`에서 실행합니다.

이 콘솔은 파이프라인을 직접 실행하지는 않습니다. 배포된 리소스가 남긴 상태와 로그를
읽어 단계별로 보여주고, 사람이 승인해야 하는 두 지점에 버튼을 제공합니다.

```
source 저장소 ──merge──▶ Trigger 함수 ──▶ Runtime ──▶ Wiki PR ──승인──▶ 확정
                              │              │           │            │
                              └──────── 이 콘솔이 읽는 것 ────────────┘
```

## 0. 시작하기 전에

이 콘솔만으로는 화면에 아무것도 보이지 않습니다. 먼저 `automation-reference`를
자신의 환경에 배포해야 합니다.

| 필요한 것 | 확인 방법 |
|---|---|
| source 저장소와 Wiki 저장소 | 실제 저장소가 있고 문서 frontmatter의 `sources[]`가 채워져 있습니다 |
| Trigger 함수 | merge 이벤트로 발화하고 CloudWatch 로그 그룹이 있습니다 |
| AgentCore Runtime | `READY` 상태이고 Runtime ID를 알고 있습니다 |
| AWS 프로파일 | 위 리소스를 읽을 수 있어야 합니다. **처음에는 읽기 전용으로 시작합니다** |
| Python 3.12 이상 | `python3 -V`로 확인합니다 |

배포 절차는 `automation-reference`의 `docs/DEPLOYMENT.md`를 따릅니다. 이 콘솔에는
배포 도구가 포함되어 있지 않습니다.

## 1. 설치

`automation-reference`와 나란히 둡니다. 두 폴더의 부모 디렉터리가 같으면 아래 명령이
그대로 동작합니다.

```
<작업 폴더>/
  automation-reference/
  demo-console/
```

```bash
cd demo-console
python3 -m venv ../console-venv
../console-venv/bin/pip install -r requirements-dev.txt
```

이 콘솔은 Wiki 경로 계약(문서와 파생 JSON의 짝, navigation 판정)을
`automation-reference`에서 가져옵니다. 콘솔이 따로 구현하면 두 곳의 기준이 어긋날 수
있기 때문입니다.

**위와 같이 배치했다면 별도로 설정할 것이 없습니다.** 콘솔이 나란히 있는
`automation-reference/poc`를 스스로 찾습니다. 폴더 이름을 바꾸었거나 다른 곳에
두었다면 경로를 알려줍니다.

```bash
export WIKI_AGENT_PATH=/경로/automation-reference/poc
```

경로를 찾지 못하면 어디를 찾아봤는지 출력하고 실패합니다. 자체 구현으로 조용히
넘어가지 않습니다.

## 2. `local.json` 만들기

```bash
cp local.json.example local.json
```

| 키 | 의미 | 확인 방법 |
|---|---|---|
| `account` | 대상 AWS 계정 ID 12자리 | `aws sts get-caller-identity` |
| `profile` | `~/.aws/config`의 프로파일 이름 | 직접 지정합니다 |
| `region` | 배포 리전 | 배포 시 정한 값입니다 |
| `runtime_id` | AgentCore Runtime ID | `aws bedrock-agentcore-control list-agent-runtimes` |
| `source_repositories` | 관측할 source 저장소 이름 목록 | 배포 시 정한 값입니다 |
| `wiki_repo` | Wiki 저장소 이름 | 배포 시 정한 값입니다 |
| `redact` | 화면에서 가릴 문자열과 대체어 쌍 | 아래를 참고합니다 |

`local.json`에는 계정 정보가 담겨 있습니다. 저장소에 커밋하거나 외부에 공유하지
않습니다.

`redact`는 화면과 API 응답에 나오는 principal ARN과 사람 alias를 대체어로 바꿔줍니다.
화면을 공유하거나 녹화할 때 사용합니다. 비어 있으면 원래 값이 그대로 표시됩니다.

```json
"redact": [
  ["arn:aws:sts::<계정 ID>:assumed-role/<역할>/<세션>", "<검토자 A>"],
  ["<사람 alias>", "<사용자>"]
]
```

## 3. 실행 — 처음에는 읽기 전용으로 시작합니다

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --read-only --no-auth
```

브라우저에서 `http://127.0.0.1:8899/`를 엽니다.

| 플래그 | 효과 |
|---|---|
| `--read-only` | `/api/actions/commit`과 `/api/actions/approve`를 403으로 막습니다 |
| `--no-auth` | Cognito 로그인을 건너뜁니다. HTML 응답에 로컬 세션을 심습니다 |

### `--no-auth`가 안전한 이유

이 서버는 원래부터 JWT를 검증하지 않습니다. 화면 로그인은 배포판(CloudFront)
앞단의 장치이며, 로컬 환경에서는 의미가 없습니다. `--no-auth`는 **화면 진입 게이트만**
없앱니다.

AWS 권한은 서버 프로세스가 가진 AWS 자격증명에서만 나옵니다. 로그인을 건너뛰어도
권한이 늘어나지는 않습니다.

⚠️ 서버는 `127.0.0.1`에만 바인딩합니다. 터널을 열거나 다른 인터페이스로 노출하지
않습니다.

## 4. 화면

| 경로 | 보이는 것 |
|---|---|
| `/` | 마지막 실행 기록, 판정 근거, 저장소 head, Skill 목록 |
| `/live.html` | source 변경 실행, 단계별 진행, Wiki PR 검토와 승인, 확정 |
| `/wiki.html` | Wiki 트리, 커밋 이력, 커밋 diff |

`/`는 `snapshot.json`을 읽습니다. 없으면 먼저 생성해야 합니다. AWS를 읽기만 하는
작업입니다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python collect.py
```

`/live.html`과 `/wiki.html`은 AWS를 직접 읽으므로 스냅샷이 없어도 동작합니다.

## 5. 쓰기 모드 — 데모 한 바퀴 진행하기

`--read-only`를 빼면 두 버튼이 열립니다. **이 버튼은 실제 merge를 수행하고 모델
호출 비용을 발생시킵니다.** 승인 절차를 정해두고 사용합니다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --no-auth
```

`/live.html`에서는 아래 순서로 진행됩니다.

1. **고정 Source 변경 실행** — 허용된 source 파일 1건에 왕복 변경을 적용하고 PR을
   만들어 merge합니다. 이 merge가 Trigger를 발화시킵니다.
2. 화면이 단계별 진행 상황을 채웁니다. 판정 근거, 스코프, 토큰 사용량, 읽은 Skill이
   표시됩니다.
3. **wiki 승인 및 merge** — Runtime이 만든 Wiki PR의 diff를 검토하고 merge합니다.
4. 확정 결과가 나옵니다. 문서 `status`가 `current`로 올라가고 검토자가 기록됩니다.

3단계는 자동화하지 않습니다. 사람의 승인이 문서를 검증 상태로 올리는 유일한
경로입니다.

## 6. 자신의 환경 값으로 바꿀 곳

콘솔 코드에는 참조 환경의 이름이 상수로 들어 있습니다. 자신의 값으로 바꿉니다.

| 파일 | 상수 | 내용 |
|---|---|---|
| `backend/query_service.py` | `_EDITABLE` | 편집을 허용할 source 파일 1건 |
| `backend/action_service.py` | `EDITABLE` | 위와 같은 key의 저장소 매핑 |
| `frontend/live.html` | `fixedMutation()` | 그 파일에 적용할 왕복 변경 규칙 |
| `frontend/live.html` | `FIXED_TITLE`, `FIXED_DESCRIPTION` | PR 제목과 설명 |
| `frontend/live.html`, `index.html`, `wiki.html` | 화면 문구의 저장소·파일 이름 | 표시용입니다 |
| `collect.py` | `LOG_GROUP`, Skill 버킷 접두사 | Trigger 함수 로그 그룹과 버킷 이름 |

`fixedMutation()`은 **왕복 가능한 변경**이어야 합니다. 버튼을 누를 때마다 두 상태를
번갈아 만들어 매번 의미 있는 diff가 생기게 하는 장치입니다. 대상 파일에 두 상태가
정확히 하나씩 있는지 가드가 검사하며, 맞지 않으면 버튼이 막힙니다.

편집 허용 목록을 늘리려면 `_EDITABLE`과 `EDITABLE`을 함께 고칩니다. 한쪽만 고치면
읽기와 쓰기의 허용 범위가 어긋납니다.

## 7. 확인

AWS를 호출하지 않는 검사입니다.

```bash
PYTHONDONTWRITEBYTECODE=1 ../console-venv/bin/python selftest.py
```

단위·계약 테스트와 브라우저 스모크가 실행됩니다. 통과하면 화면 계약은 맞다는
뜻이며, 남은 것은 `local.json`의 값과 대상 환경의 상태입니다.

실행 중인 서버를 대상으로 응답과 익명화 누출을 확인하려면 다음을 사용합니다. 기본
대상은 `http://127.0.0.1:8899`이므로, 다른 port로 띄웠다면 `--base`로 알려줍니다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py --base http://127.0.0.1:8901
```

## 8. 정리(cleanup)

데모를 마쳤거나 사용을 중단할 때는 아래 순서로 정리합니다.

1. **서버 종료**: 서버를 실행한 터미널에서 `Ctrl+C`로 `server.py`를 종료합니다.
2. **가상환경 삭제** (선택): 더 이상 이 콘솔을 사용하지 않는다면 가상환경 폴더를
   삭제해도 됩니다.
   ```bash
   rm -rf ../console-venv
   ```
3. **로컬 자격증명 정리**: `AWS_PROFILE`로 임시 자격증명을 발급받아 사용했다면
   세션이 만료되도록 두거나 필요 시 직접 무효화합니다. 이 콘솔은 별도의 자격증명을
   생성하거나 저장하지 않습니다.
4. **쓰기 모드로 데모를 진행한 경우**: 5절에서 생성한 Wiki PR과 대상 브랜치가 남아
   있을 수 있습니다. 더 이상 필요하지 않으면 CodeCommit 콘솔에서 직접 정리합니다.
   이 콘솔은 브랜치나 PR을 자동으로 삭제하지 않습니다.
5. **`local.json` 삭제 또는 보관**: 계정 정보가 담겨 있으므로, 폴더를 공유하거나
   다른 곳으로 옮기기 전에 반드시 삭제하거나 안전한 곳에 별도로 보관합니다.

이 콘솔 자체는 AWS 리소스를 새로 생성하지 않습니다. 따라서 정리 대상은 대부분 로컬
프로세스와 자격증명, 그리고 5절에서 직접 생성한 PR·브랜치입니다.

## 알려진 제약

- 이 콘솔은 시연·검토용입니다. 상시 운영 콘솔로 설계되지 않았습니다.
- 배포 도구가 아닙니다. `automation-reference`를 먼저 배포해야 화면이 채워집니다.
- 로컬 서버는 JWT를 검증하지 않습니다. `127.0.0.1` 밖으로 내보내지 않습니다.
- CodeCommit을 읽습니다. 다른 Git 서비스를 사용한다면 `automation-reference`의
  어댑터 이식과 함께 이 콘솔의 조회 계층도 함께 바꿔야 합니다.
- source 편집 허용 목록이 1건으로 고정되어 있습니다. 6절을 참고합니다.
- 화면은 스냅샷과 로그를 읽습니다. 진행 중 갱신은 새로 고침이 필요합니다.
- Cognito·CloudFront로 팀에 공유하는 배포 경로는 이 번들에 포함하지 않았습니다.
