# Demo Console 연결 가이드

`automation-reference`를 배포하셨다면, 이제 그 파이프라인이 실제로 잘 돌아가는지
**눈으로 확인**할 수 있는 로컬 콘솔입니다. Cognito 로그인 없이 `127.0.0.1`에서 바로
실행할 수 있어요.

이 콘솔은 파이프라인을 대신 실행하지는 않습니다. 배포된 리소스가 남긴 상태와 로그를
읽어서 단계별로 보여 주고, 사람이 직접 승인해야 하는 두 지점에는 버튼을 제공하는
역할이에요.

```
source 저장소 ──merge──▶ Trigger 함수 ──▶ Runtime ──▶ Wiki PR ──승인──▶ 확정
                              │              │           │            │
                              └──────── 이 콘솔이 읽는 것 ────────────┘
```

## 0. 시작하기 전에

이 콘솔만 띄운다고 화면에 뭔가 보이지는 않아요. 먼저 `automation-reference`를 여러분의
환경에 배포해 주셔야 합니다.

| 필요한 것 | 확인 방법 |
|---|---|
| source 저장소와 Wiki 저장소 | 실제 저장소가 있고, 문서 frontmatter의 `sources[]`가 채워져 있으면 OK |
| Trigger 함수 | merge 이벤트로 발화하고, CloudWatch 로그 그룹이 있으면 OK |
| AgentCore Runtime | `READY` 상태이고 Runtime ID를 알고 있으면 OK |
| AWS 프로파일 | 위 리소스를 읽을 수 있어야 해요. **처음엔 읽기 전용으로 시작하는 걸 권장해요** |
| Python 3.12 이상 | `python3 -V`로 확인 |

배포 절차는 `automation-reference`의 `docs/DEPLOYMENT.md`를 따라 주세요. 이 콘솔에는
배포 도구가 들어있지 않아요.

## 1. 설치하기

`automation-reference`와 나란히 두시면 편해요. 두 폴더의 부모 디렉터리가 같으면 아래
명령이 바로 동작합니다.

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
`automation-reference`에서 그대로 가져와서 씁니다. 콘솔이 따로 구현하면 두 곳의 규칙이
갈릴 수 있어서, 일부러 중복 구현하지 않았어요.

**위처럼 나란히 두셨다면 별도 설정은 필요 없어요.** 콘솔이 옆에 있는
`automation-reference/poc`를 스스로 찾습니다. 폴더 이름을 바꾸셨거나 다른 곳에
두셨다면 경로를 알려주시면 됩니다.

```bash
export WIKI_AGENT_PATH=/경로/automation-reference/poc
```

경로를 찾지 못하면 어디를 찾아봤는지 알려주면서 안내 메시지를 띄워요. 조용히 다른
방식으로 넘어가지 않으니 걱정하지 않으셔도 됩니다.

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

`local.json`에는 계정 정보가 담겨요. 저장소에 커밋하거나 외부에 공유하지 않도록
주의해 주세요.

`redact`는 화면과 API 응답에 나오는 principal ARN이나 사람 alias를 대체어로 바꿔줘요.
화면을 공유하거나 녹화할 때 유용하니 필요하면 채워 주세요. 비어 있으면 원래 값이 그대로
보여요.

```json
"redact": [
  ["arn:aws:sts::<계정 ID>:assumed-role/<역할>/<세션>", "<검토자 A>"],
  ["<사람 alias>", "<사용자>"]
]
```

## 3. 실행하기 — 처음엔 읽기 전용으로 시작해 주세요

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --read-only --no-auth
```

브라우저에서 `http://127.0.0.1:8899/`를 열어 주세요.

| 플래그 | 효과 |
|---|---|
| `--read-only` | `/api/actions/commit`과 `/api/actions/approve`를 403으로 막아줘요 |
| `--no-auth` | Cognito 로그인을 건너뛰어요. HTML 응답에 로컬 세션을 심어줘요 |

### `--no-auth`를 써도 괜찮은 이유

이 서버는 원래부터 JWT를 검증하지 않아요. 화면 로그인은 배포판(CloudFront) 앞단에
있는 장치라서, 로컬에서는 의미가 없거든요. `--no-auth`는 **화면 진입 게이트만** 없애는
거예요.

AWS 권한은 서버 프로세스가 갖고 있는 AWS 자격증명에서만 나와요. 로그인을 건너뛰어도
권한이 더 생기지는 않으니 안심하셔도 됩니다.

⚠️ 다만 서버는 `127.0.0.1`에만 바인딩됩니다. 터널을 열거나 다른 인터페이스로 노출하지
않도록 해 주세요.

## 4. 화면 둘러보기

| 경로 | 보이는 것 |
|---|---|
| `/` | 마지막 실행 기록, 판정 근거, 저장소 head, Skill 목록 |
| `/live.html` | source 변경 실행, 단계별 진행, Wiki PR 검토와 승인, 확정 |
| `/wiki.html` | Wiki 트리, 커밋 이력, 커밋 diff |

`/`는 `snapshot.json`을 읽어요. 없으면 먼저 만들어 주세요. AWS를 읽기만 하는
작업이에요.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python collect.py
```

`/live.html`과 `/wiki.html`은 AWS를 직접 읽기 때문에 스냅샷이 없어도 동작해요.

## 5. 쓰기 모드로 데모 한 바퀴 돌려보기

`--read-only`를 빼면 두 버튼이 열려요. **이 버튼은 실제로 merge를 수행하고 모델 호출
비용이 발생해요.** 승인 절차를 정해두고 사용하시는 걸 권장해요.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python server.py 8899 --no-auth
```

`/live.html`에서는 아래 순서로 진행돼요.

1. **고정 Source 변경 실행** — 허용된 source 파일 1건에 왕복 변경을 적용하고 PR을
   만들어 merge해요. 이 merge가 Trigger를 발화시켜요.
2. 화면이 단계별 진행 상황을 채워줘요. 판정 근거, 스코프, 토큰 사용량, 읽은 Skill이
   나와요.
3. **wiki 승인 및 merge** — Runtime이 만든 Wiki PR의 diff를 검토하고 merge해요.
4. 확정 결과가 나와요. 문서 `status`가 `current`로 올라가고 검토자가 기록돼요.

3단계는 자동화하지 않았어요. 사람이 직접 승인해야 문서가 검증 상태로 올라가는 유일한
경로예요.

## 6. 여러분의 환경 값으로 바꿔줄 곳

콘솔 코드 안에는 참조 환경에서 쓰던 이름이 상수로 들어있어요. 여러분의 값으로 바꿔
주세요.

| 파일 | 상수 | 내용 |
|---|---|---|
| `backend/query_service.py` | `_EDITABLE` | 편집을 허용할 source 파일 1건 |
| `backend/action_service.py` | `EDITABLE` | 위와 같은 key의 저장소 매핑 |
| `frontend/live.html` | `fixedMutation()` | 그 파일에 적용할 왕복 변경 규칙 |
| `frontend/live.html` | `FIXED_TITLE`, `FIXED_DESCRIPTION` | PR 제목과 설명 |
| `frontend/live.html`, `index.html`, `wiki.html` | 화면 문구의 저장소·파일 이름 | 표시용 |
| `collect.py` | `LOG_GROUP`, Skill 버킷 접두사 | Trigger 함수 로그 그룹과 버킷 이름 |

`fixedMutation()`은 **왕복 가능한 변경**이어야 해요. 버튼을 누를 때마다 두 상태를
번갈아 만들어서 매번 의미 있는 diff가 생기게 하는 장치예요. 대상 파일에 두 상태가
정확히 하나씩 있는지 가드가 검사하고, 맞지 않으면 버튼이 막혀요.

편집 허용 목록을 늘리고 싶으시면 `_EDITABLE`과 `EDITABLE`을 함께 고쳐 주세요. 한쪽만
고치면 읽기와 쓰기의 허용 범위가 어긋나게 돼요.

## 7. 잘 붙었는지 확인하기

AWS를 부르지 않는 검사예요.

```bash
PYTHONDONTWRITEBYTECODE=1 ../console-venv/bin/python selftest.py
```

단위·계약 테스트와 브라우저 스모크가 돌아가요. 통과하면 화면 계약은 맞다는 뜻이고,
남은 건 `local.json`의 값과 대상 환경의 상태예요.

실행 중인 서버를 대상으로 응답과 익명화 누출을 확인하려면 아래를 쓰시면 돼요. 기본
대상은 `http://127.0.0.1:8899`이니, 다른 port로 띄우셨다면 `--base`로 알려주세요.

```bash
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py
AWS_PROFILE=<프로파일> ../console-venv/bin/python verify.py --base http://127.0.0.1:8901
```

## 8. 정리하기 (사용을 마치셨다면)

데모를 마치셨거나 잠시 쉬어가실 때는 아래 순서로 정리해 주세요.

1. **서버 종료**: 서버를 실행한 터미널에서 `Ctrl+C`를 눌러 `server.py`를 멈춰 주세요.
2. **가상환경 정리** (선택): 더 이상 이 콘솔을 쓰지 않으실 거면 가상환경 폴더를
   지워도 괜찮아요.
   ```bash
   rm -rf ../console-venv
   ```
3. **로컬 자격증명 정리**: `AWS_PROFILE`로 임시 자격증명을 발급받아 쓰셨다면 세션이
   만료되도록 두거나, 필요하면 직접 무효화해 주세요. 이 콘솔이 별도의 자격증명을
   만들거나 저장하지는 않아요.
4. **쓰기 모드로 데모를 진행하셨다면**: 5절에서 만든 Wiki PR과 그 대상 브랜치가 남아
   있을 수 있어요. 더 이상 필요 없으면 CodeCommit 콘솔에서 직접 정리해 주세요. 이
   콘솔 자체는 브랜치나 PR을 자동으로 지우지 않아요.
5. **`local.json` 삭제 또는 보관**: 계정 정보가 담겨 있으니, 폴더를 공유하거나 다른
   곳으로 옮기기 전에는 반드시 지우거나 안전한 곳에 따로 보관해 주세요.

이 콘솔 자체는 AWS 리소스를 새로 만들지 않기 때문에, 정리할 것은 대부분 로컬
프로세스와 자격증명, 그리고 5절에서 직접 만든 PR/브랜치 정도예요.

## 알려진 제약

- 이 콘솔은 시연·검토용으로 만들었어요. 상시 운영 콘솔로 쓰시기엔 적합하지 않아요.
- 배포 도구는 아니에요. `automation-reference`를 먼저 배포해야 화면이 채워져요.
- 로컬 서버는 JWT를 검증하지 않아요. `127.0.0.1` 밖으로 내보내지 않도록 해 주세요.
- CodeCommit을 읽는 구조예요. 다른 Git 서비스를 쓰신다면 `automation-reference`의
  어댑터 이식과 함께 이 콘솔의 조회 계층도 같이 바꿔주셔야 해요.
- source 편집 허용 목록이 1건으로 고정돼 있어요. 6절을 참고해 주세요.
- 화면은 스냅샷과 로그를 읽어요. 진행 중 갱신은 새로 고침이 필요해요.
- Cognito·CloudFront로 팀에 공유하는 배포 경로는 이 번들에 포함하지 않았어요.
