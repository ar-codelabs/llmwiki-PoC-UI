# Demo Console 연결 가이드

`automation-reference`를 배포한 뒤, 그 파이프라인이 실제로 도는 것을 **화면으로 보는**
로컬 콘솔이다. Cognito 없이 `127.0.0.1`에서 실행한다.

이 콘솔은 파이프라인을 실행하지 않는다. 배포된 리소스가 남긴 상태와 로그를 읽어
단계별로 보여 주고, 사람이 승인해야 하는 두 지점에 버튼을 제공한다.

```
source 저장소 ──merge──▶ Trigger 함수 ──▶ Runtime ──▶ Wiki PR ──승인──▶ 확정
                              │              │           │            │
                              └──────── 이 콘솔이 읽는 것 ────────────┘
```

## 0. 전제

이 콘솔만으로는 아무것도 보이지 않는다. 먼저 `automation-reference`를 자기 환경에
배포해야 한다.

| 필요한 것 | 확인 |
|---|---|
| source 저장소와 Wiki 저장소 | 실제 저장소가 있고 문서 frontmatter의 `sources[]`가 채워져 있다 |
| Trigger 함수 | merge 이벤트로 발화하고 CloudWatch 로그 그룹이 있다 |
| AgentCore Runtime | `READY` 상태이고 Runtime ID를 알고 있다 |
| AWS 프로파일 | 위 리소스를 읽을 수 있다. **읽기 전용으로 시작한다** |
| Python 3.12 이상 | `python3 -V` |

배포 절차는 `automation-reference`의 `docs/DEPLOYMENT.md`를 따른다. 이 콘솔에는 배포
도구가 없다.

## 1. 설치

`automation-reference`와 나란히 둔다. 두 폴더의 부모가 같으면 아래 명령이 그대로 돈다.

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
`automation-reference`에서 가져온다. 콘솔이 따로 구현하면 두 곳이 갈리기 때문이다.

**위 배치라면 설정할 것이 없다.** 콘솔이 나란히 있는 `automation-reference/poc`를 스스로
찾는다. 폴더 이름을 바꾸거나 다른 곳에 두었으면 경로를 알려 준다.

```bash
export WIKI_AGENT_PATH=/경로/automation-reference/poc
```

찾지 못하면 어디를 찾아봤는지 출력하고 실패한다. 자체 구현으로 넘어가지 않는다.

## 2. `local.json` 만들기

```bash
cp local.json.example local.json
```

| 키 | 의미 | 확인 방법 |
|---|---|---|
| `account` | 대상 AWS 계정 ID 12자리 | `aws sts get-caller-identity` |
| `profile` | `~/.aws/config`의 프로파일 이름 | 직접 지정 |
| `region` | 배포 리전 | 배포 시 정한 값 |
| `runtime_id` | AgentCore Runtime ID | `aws bedrock-agentcore-control list-agent-runtimes` |
| `source_repositories` | 관측할 source 저장소 이름 목록 | 배포 시 정한 값 |
| `wiki_repo` | Wiki 저장소 이름 | 배포 시 정한 값 |
| `redact` | 화면에서 가릴 문자열과 대체어 쌍 | 아래 참고 |

`local.json`은 계정 좌표를 담는다. 저장소에 커밋하거나 외부에 공유하지 않는다.

`redact`는 화면과 API 응답에서 principal ARN과 사람 alias를 대체어로 바꾼다. 화면을
공유하거나 녹화할 때 쓴다. 비어 있으면 원값이 그대로 보인다.

```json
"redact": [
  ["arn:aws:sts::<계정 ID>:assumed-role/<역할>/<세션>", "<검토자 A>"],
  ["<사람 alias>", "<사용자>"]
]
```

## 3. 실행 — 읽기 전용으로 시작한다

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --read-only --no-auth
```

브라우저에서 `http://127.0.0.1:8899/` 를 연다.

| 플래그 | 효과 |
|---|---|
| `--read-only` | `/api/actions/commit`과 `/api/actions/approve`를 403으로 막는다 |
| `--no-auth` | Cognito 로그인을 건너뛴다. HTML 응답에 로컬 세션을 심는다 |

### `--no-auth`가 안전한 이유

이 서버는 원래부터 JWT를 검증하지 않는다. 화면 로그인은 배포판(CloudFront) 앞단의
장치이고, 로컬에서는 의미가 없다. `--no-auth`는 **화면 진입 게이트만** 없앤다.

AWS 권한은 서버 프로세스의 AWS 자격증명에서만 온다. 로그인을 건너뛰어도 권한이 늘지
않는다.

⚠️ 서버는 `127.0.0.1`에만 바인딩한다. 터널을 열거나 다른 인터페이스로 노출하지 않는다.

## 4. 화면

| 경로 | 보이는 것 |
|---|---|
| `/` | 마지막 실행 기록, 판정 근거, 저장소 head, Skill 목록 |
| `/live.html` | source 변경 실행, 단계별 진행, Wiki PR 검토와 승인, 확정 |
| `/wiki.html` | Wiki 트리, 커밋 이력, 커밋 diff |

`/`는 `snapshot.json`을 읽는다. 없으면 먼저 만든다. AWS 읽기다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python collect.py
```

`/live.html`과 `/wiki.html`은 AWS를 직접 읽으므로 스냅샷이 없어도 동작한다.

## 5. 쓰기 모드 — 데모 한 바퀴

`--read-only`를 빼면 두 버튼이 열린다. **이 버튼은 실제 merge를 수행하고 모델 호출
비용을 발생시킨다.** 승인 절차를 두고 쓴다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --no-auth
```

`/live.html`에서 진행되는 순서다.

1. **고정 Source 변경 실행** — 허용된 source 파일 1건에 왕복 변경을 적용하고 PR을
   만들어 merge한다. 이 merge가 Trigger를 발화시킨다.
2. 화면이 단계별 진행을 채운다. 판정 근거, 스코프, 토큰 사용량, 읽은 Skill이 나온다.
3. **wiki 승인 및 merge** — Runtime이 만든 Wiki PR의 diff를 검토하고 merge한다.
4. 확정 결과가 나온다. 문서 `status`가 `current`로 오르고 검토자가 기록된다.

3단계는 자동화하지 않는다. 사람의 승인이 문서를 검증 상태로 올리는 유일한 경로다.

## 6. 자기 환경 값으로 바꿀 곳

콘솔 코드에 참조 환경의 이름이 상수로 들어 있다. 자기 값으로 바꾼다.

| 파일 | 상수 | 내용 |
|---|---|---|
| `backend/query_service.py` | `_EDITABLE` | 편집을 허용할 source 파일 1건 |
| `backend/action_service.py` | `EDITABLE` | 위와 같은 key의 저장소 매핑 |
| `frontend/live.html` | `fixedMutation()` | 그 파일에 적용할 왕복 변경 규칙 |
| `frontend/live.html` | `FIXED_TITLE`, `FIXED_DESCRIPTION` | PR 제목과 설명 |
| `frontend/live.html`, `index.html`, `wiki.html` | 화면 문구의 저장소·파일 이름 | 표시용 |
| `collect.py` | `LOG_GROUP`, Skill 버킷 접두사 | Trigger 함수 로그 그룹과 버킷 이름 |

`fixedMutation()`은 **왕복 가능한 변경**이어야 한다. 버튼을 누를 때마다 두 상태를
번갈아 만들어 매번 의미 있는 diff가 생기게 하는 장치다. 대상 파일에 두 상태가 정확히
하나씩 있는지 가드가 검사하고, 맞지 않으면 버튼이 막힌다.

편집 허용 목록을 늘리려면 `_EDITABLE`과 `EDITABLE`을 함께 고친다. 한쪽만 고치면 읽기와
쓰기의 허용 범위가 어긋난다.

## 7. 확인

AWS를 부르지 않는 검사다.

```bash
PYTHONDONTWRITEBYTECODE=1 ../console-venv/bin/python selftest.py
```

단위·계약 테스트와 브라우저 스모크가 돈다. 통과하면 화면 계약은 맞고, 남은 것은
`local.json`의 값과 대상 환경의 상태다.

실행 중인 서버를 대상으로 응답과 익명화 누출을 보려면 다음을 쓴다. 기본 대상이
`http://127.0.0.1:8899`이므로 다른 port로 띄웠으면 `--base`로 알려 준다.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py --base http://127.0.0.1:8901
```

## 알려진 제약

- 이 콘솔은 시연·검토용이다. 상시 운영 콘솔로 설계하지 않았다.
- 배포 도구가 아니다. `automation-reference`를 먼저 배포해야 화면이 채워진다.
- 로컬 서버는 JWT를 검증하지 않는다. `127.0.0.1` 밖으로 내보내지 않는다.
- CodeCommit을 읽는다. 다른 Git 서비스는 `automation-reference`의 어댑터 이식과 함께
  이 콘솔의 조회 계층도 바꿔야 한다.
- source 편집 허용 목록이 1건으로 고정돼 있다. 6절 참고.
- 화면은 스냅샷과 로그를 읽는다. 진행 중 갱신은 새로 고침 또는 refresh 동작이 필요하다.
- Cognito·CloudFront로 팀에 공유하는 배포 경로는 이 번들에 포함하지 않았다.
