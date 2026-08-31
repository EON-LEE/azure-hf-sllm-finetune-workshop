# Lab 5 — 관리형 온라인 엔드포인트

> **Track B · 선행: [Lab 0](lab0.md), [Lab 4](lab4.md)**
> **학습 잡이 필요 없습니다** — `--hf-model` 로 Hub 가중치를 바로 서빙합니다.

> ## 🚨 여기서부터 시간당 $4.96 입니다
> `Standard_NC24ads_A100_v4` 1 인스턴스 = **$4.959/시 ≈ $119/일**.
> 온라인 엔드포인트는 **요청이 0 이어도 24시간 과금**되고 0 으로 축소되지 않습니다.
> **이 Lab 을 시작하기 전에 [Lab 7 (내리기)](lab7.md) 를 먼저 읽으세요.**

## 목표

- 관리형 온라인 엔드포인트를 실제로 띄운다
- **막힌 축이 쿼터가 아니라 리전**일 수 있다는 걸 직접 확인한다
- **기동 성공이 서빙 플래그가 맞다는 증거가 아니라는 것**을 응답 본문으로 본다
- 배포가 **Lab 0 에서 만든 그 그룹**으로 들어가는 걸 확인한다 — 마지막에
  `ffsft infra down` 한 줄이 전부를 뜻하려면 여기서 그룹이 갈라지면 안 됩니다

## 선행조건

- Lab 4 의 서빙 이미지가 ACR 에 있고, **그 참조를 손에 들고 있음**
  (`$FFSFT_ACR.azurecr.io/ffsft-serve:1`) — 이 Lab 이 그걸 배포합니다.
  Lab 0 의 `infra up` 이 그 ACR 을 **엔드포인트와 같은 그룹**에 만들었습니다
- **dedicated** GPU 를 살 수 있는 리전 — 온라인 엔드포인트에 LowPriority 옵션은 **없습니다**.
  [Lab 0 §3](lab0.md) 에서 그걸 기준으로 리전을 골랐습니다
- **워크스페이스는 이미 있습니다** — `infra up` 이 만들었습니다. 이 Lab 은 아무 그룹도
  아무 워크스페이스도 새로 만들지 않습니다
- 새 셸이면 `source ~/.ffsft-env` 입니다. `AZURE_CONFIG_DIR` 도 그 파일 안에 있습니다
  ([Lab 0 §2](lab0.md)). **`export AZURE_CONFIG_DIR=...` 만 다시 하지 마세요** — az 프로필은
  맞는데 `FFSFT_*` 가 하나도 없는 셸이 되고, 그건 오류가 아니라 저자들의 기본값
  (`rg-ffsft-kc` / `mlw-ffsft` / `koreacentral`)으로 **조용히** 갑니다
  (`AzureTarget.from_env`) → [GOTCHAS #1](../GOTCHAS.md#1)

## 소요·비용

**40분** (배포 23분 + 확인). 켜 두는 시간만큼 $4.96/시.

---

## 1. 리전부터 확인한다 — 이게 진짜 축이다

같은 구독인데 리전에 따라 답이 정반대입니다 (§57.1):

| SKU | koreacentral | polandcentral |
|---|---|---|
| `Standard_NC24ads_A100_v4` | BLOCKED (Location) | **FREE** |
| `Standard_NC48ads_A100_v4` | BLOCKED (Location) | **FREE** |
| `Standard_NV*ads_A10_v5` | BLOCKED (Zone) | BLOCKED (Location,Zone) |

koreacentral 은 이 구독에서 **전용 GPU SKU 가 하나도 없습니다.** A10 이 특별히 막힌
게 아니라 그 리전에서 dedicated GPU 를 아예 못 삽니다. LowPriority 로는 A100 이 되니
**학습만 되고 관리형 엔드포인트는 안 되는 비대칭**이 생깁니다.

```bash
az vm list-skus --location <region> --resource-type virtualMachines \
   --query "[?contains(name,'NC24ads_A100')].{n:name,r:restrictions}" -o json
```

`restrictions: []` 여야 살 수 있습니다. 쿼터 숫자는 그 다음 문제입니다.
→ [GOTCHAS #2](../GOTCHAS.md#2), [#3](../GOTCHAS.md#3)

> ### 여기서 `restrictions` 가 막혀 있으면 Lab 0 §3 을 잘못 통과한 것입니다
> [Lab 0 §3](lab0.md) 은 **이 줄**(`restrictions: []` + `MIR`)을 리전 선택 기준으로
> 삼으라고 합니다. 학습만 보고 골랐다면 지금 막힙니다 — LowPriority A100 이 열린 리전이
> 관리형 엔드포인트가 되는 리전이라는 보장은 없고, 실제로 koreacentral 이 그 반례입니다.
>
> **그때 할 일은 워크스페이스를 옮기는 게 아니라 그룹을 새로 세우는 것입니다.**
>
> ```bash
> uv run ffsft infra down --prefix <본인> --yes      # 지금 것을 먼저 내리고
> uv run ffsft infra up   --prefix <본인> --location <restrictions 가 빈 리전>
> source ~/.ffsft-env
> ```
>
> Lab 0~4 를 다시 도는 값이지, 그룹을 둘로 늘려서 될 일이 아닙니다. 그룹이 둘이 되는
> 순간 `ffsft infra down` 한 줄이 "다 내렸다"를 뜻하지 못하고, 이 리포가 실제로 그
> 상태에서 몇 주를 보냈습니다 (`src/ffsft/deploy/identity.py:411`).
>
> **아직 아무것도 안 만들었으면 내릴 것도 없습니다.** Lab 0~4 에서 돈이 나가는 것은
> Lab 4 의 ACR 빌드뿐이고 그건 몇 센트입니다. `gpu-a100-lp` 는 노드 0개면 시간당 $0
> 입니다 ([Lab 0 §4](lab0.md) 실측).

## 2. 코어를 2배로 계산한다

온라인 엔드포인트는 롤링 업데이트 여유분으로 `ceil(1.2 × 인스턴스) × 코어` 를 청구합니다.
**1 인스턴스가 코어를 두 배 먹습니다.** 36코어 승인으로 NV36 이 안 들어가는 이유입니다.

**단, A100/H100/ND 계열은 면제**입니다 (문서의 "Skip 20% Reservation").
24코어 A100 은 24코어면 됩니다. → [GOTCHAS #4](../GOTCHAS.md#4)

```bash
uv run ffsft deploy check --probe
```

`--probe` 는 쿼터 숫자를 믿는 대신 **컨트롤 플레인에 실제로 물어봅니다** — 거부는
아무것도 만들지 않고, 수락된 것은 `min=0` 으로 만들었다가 삭제하므로 공짜입니다.

> ⚠️ **이 명령도 환경변수를 봅니다.** 쿼터는 `$FFSFT_LOCATION` 으로, 프로브 create 는
> `$FFSFT_RESOURCE_GROUP`/`$FFSFT_WORKSPACE` 로 갑니다. **셋 다 Lab 0 이 굳힌 값이어야
> 합니다** — 새 셸이면 `source ~/.ffsft-env` 부터.
>
> 확인은 이 명령이 맨 위에 찍는 `LOOKED IN:` 헤더로 합니다 — `ffsft lifecycle status`
> 와 같은 헤더이고, **조회를 조향하는 세 값이 다 거기 있습니다.**
>
> | 헤더에서 볼 것 | 무엇을 정하나 |
> |---|---|
> | 첫 줄 `workspace …   resource group …` | 프로브 `create` 가 나가는 곳 |
> | 설명 줄의 `FFSFT_LOCATION=…` | dedicated 쿼터를 조회한 **리전** |
>
> **셋 다 여러분의 prefix 가 들어간 이름이어야 합니다.** `rg-ffsft-kc` / `mlw-ffsft` /
> `koreacentral` 이 보이면 그건 이 리포 저자들의 기본값이고, 오류가 아니라 **조용히**
> 거기로 갑니다 (`AzureTarget.from_env`) → [GOTCHAS #1](../GOTCHAS.md#1)
>
> 예전에는 이 자리에 `subscription <id> / <location>` 만 찍혔습니다. 리전은 라벨 없이
> 적고 **쿼리를 조향하는 rg·워크스페이스는 안 적은** 줄이라, 이 박스가 시키던 확인이
> 틀린 칸을 보고 있었습니다.

## 3. 배포

### 3.1 셸 확인 — 새로 만들 것은 없습니다

**이 Lab 은 리소스 그룹도 워크스페이스도 만들지 않습니다.** [Lab 0 §4](lab0.md) 의
`ffsft infra up` 이 둘 다 만들었고, 엔드포인트는 그 그룹 안에 섭니다. 여기서 그룹을
하나 더 만들면 마지막의 `ffsft infra down` 한 줄이 "다 내렸다"를 뜻하지 못합니다.

새 셸이면 프로필부터 싣고, 배너가 여러분의 그룹을 말하는지 봅니다:

```bash
source ~/.ffsft-env
```

기대 출력 (`<본인>` 은 Lab 0 에서 정한 prefix):

```
profile: ffsft  rg=rg-ffsft-<본인>  ws=mlw-<본인>  loc=<region>
```

> ⚠️ **`export AZURE_CONFIG_DIR=...` 만 다시 하지 마세요.** az 프로필은 맞는데
> `FFSFT_*` 가 하나도 없는 셸이 되고, 그건 오류가 아니라 저자들의 기본값
> (`rg-ffsft-kc` / `mlw-ffsft` / `koreacentral`)으로 **조용히** 갑니다
> (`AzureTarget.from_env`) → [GOTCHAS #1](../GOTCHAS.md#1)
>
> **왜 배너를 매번 보나 — `status`·`down` 은 대상을 플래그로 못 받습니다.**
> `ffsft lifecycle status --help` 는 `[-h]` 가 전부고, 대상은 오직 환경변수로
> 정해집니다. 지금은 표 위에 `LOOKED IN: workspace … / resource group … /
> subscription …` 헤더가 찍히지만 (§73.3) 그건 **조회가 끝난 뒤에** 나오는 줄이라
> 사후 진단이고, `down` 은 지울 게 있으면 그 헤더를 아예 안 찍습니다
> (`format_inventory` 를 안 부르고 `will remove:` 다음이 바로 삭제). 지우는 명령은
> **읽기 전에 행동합니다.** 배너는 명령을 치기 **전**에 찍힙니다.

**§2 의 `check --probe` 를 아직 안 돌렸으면 지금 이 셸에서 돌리세요.**

### 3.2 배포 명령

```bash
uv run ffsft deploy deploy-online \
   --endpoint ffsft-lab \
   --hf-model Qwen/Qwen3.8-27B \
   --image "$FFSFT_ACR.azurecr.io/ffsft-serve:1" \
   --deployment blue \
   --sku Standard_NC24ads_A100_v4 \
   --max-model-len 8192 \
   --traffic 100
```

### `--image` — Lab 4 에서 빌드한 그 이미지입니다

**이 플래그가 없으면 이 트랙은 여기서 끝납니다.** 기본값은 저자들의 사설 ACR
(`acrffsftkc.azurecr.io/ffsft-serve:5`)이라 다른 구독에서는 pull 이 안 됩니다.

| 우선순위 | 출처 |
|---|---|
| 1 | `--image` |
| 2 | `$FFSFT_SERVE_IMAGE` — 셸마다 한 번 내보내고 플래그를 빼는 쪽 |
| 3 | `SERVE_IMAGE` 상수 (저자들 것) |

```bash
export FFSFT_SERVE_IMAGE=$FFSFT_ACR.azurecr.io/ffsft-serve:1   # 2번을 쓰면
```

빈 값·공백뿐인 값은 **없는 것으로 치고 다음으로 내려갑니다.** 태그(`:1`)는 그대로
**Azure ML 환경 버전**이 되므로, 이미지를 다시 빌드하면 태그도 올려야 합니다
([Lab 4 §6](lab4.md)). 태그 없는 참조나 `@sha256:` 다이제스트는 **Azure 를 부르기
전에** 거부됩니다 — 23분 뒤가 아니라 즉시.

### ACR 은 이제 같은 그룹에 있습니다 — 그런데 코드는 그걸 전제하지 않습니다

[Lab 0 §4](lab0.md) 의 `infra up` 이 ACR 을 워크스페이스와 **같은 그룹**에 만들고,
`infra/workspace.bicep` 이 그걸 워크스페이스의 `containerRegistry` 로 **연결**합니다.
[Lab 4](lab4.md) 는 그 ACR (`$FFSFT_ACR`) 에 이미지를 밀어 넣습니다. 그래서 이 절이
설명하는 실패는 여러분에게 안 일어나야 정상입니다. **그래도 적어 둡니다** — 이 리포가
그 실패로 배포 한 번을 통째로 날렸고, 코드가 지금 그 모양인 이유가 여기 있습니다.

- `deploy-online` 은 ACR 을 **이름으로 구독 전체에서 찾습니다** (`acr_id_for_image`).
  배포 rg 에 있다고 가정하지 않습니다. 리전이 달라도 pull 은 됩니다 (§57.8 실측 —
  koreacentral ACR 의 이미지를 polandcentral 배포가 그대로 당겨왔습니다).
- 워크스페이스에 연결 ACR 이 **없으면** AcrPull 이 자동으로 안 붙습니다. 그때는 코드가
  엔드포인트의 시스템 할당 ID 에 직접 부여하고 전파를 60초 기다립니다.
- 필요한 것은 두 가지뿐입니다: ACR 이 엔드포인트와 **같은 구독**에 있을 것, 그리고
  여러분 계정이 그 구독의 레지스트리 목록을 읽고 **RBAC 을 쓸 수 있을 것**
  ([Lab 0 선행조건](lab0.md) 의 `User Access Administrator`).

이름 조회가 실패하면 (권한 없음·라이브러리 없음) 코드는 조용히 **배포 rg 에 있다고
가정하는 옛 동작으로 폴백**합니다. 그룹이 하나면 그 가정이 맞습니다. 그룹이 둘이던
시절에는 틀렸고, ARM 이 404 를 주고 AcrPull 이 안 붙고 배포가 ~10분 뒤
`Endpoint identity does not have pull permission` 으로 죽었습니다 — §57.7 (2) 가 정확히
그 실패입니다. 로그에 `could not grant AcrPull automatically` 가 보이면 **그 아래 인쇄된
명령을 그대로** 쓰세요. 거기 `--scope` 는 이미 옳은 ACR id 입니다.

가중치 소스는 **정확히 하나**여야 합니다:

| 플래그 | 쓰는 때 |
|---|---|
| `--hf-model` | Hub 에서 직접. **모델 자산도, 도달 가능한 스토리지도 필요 없습니다** |
| `--model-uri` | 등록된 AML 모델 (`azureml:이름:버전`) |
| `--model-blob-uri` | https blob URL. **`--model-key` 를 같이 줘야 합니다** ([Lab 8](lab8.md)) |

> `--model-blob-uri` 는 경로일 뿐이라 아키텍처를 유추할 수 없습니다. `--model-key` 가
> 없으면 `--mamba-cache-mode` 가 빈 채로 나가고, Qwen3.8 에서 그건 기본값이 아니라
> **20분 뒤의 크래시**입니다. CLI 가 그래서 거부합니다.

## 4. 기다리는 동안 — 로그는 아무 말도 안 합니다

`get_logs` 는 배포가 `Creating` 인 **동안 내내** 같은 문자열만 돌려줍니다:

```
Deployment is in deleting or creating state so logs can't be retrieved.
```

**노드가 없어서 못 읽는 것**과 **노드가 있는데 준비 중이라 못 읽는 것**을 구분하지
못합니다. 컨테이너 로그 폴링은 노드 할당 탐지기로 쓸 수 없습니다.

> CLI 는 Azure SDK 의 INFO 덤프를 기본으로 죽입니다 — 안 그러면 표가 화면 밖으로
> 밀려납니다. **배포가 실패했을 때는 그 HTTP 덤프가 유일한 증거인 경우가 많으니**
> `FFSFT_VERBOSE_AZURE=1` 로 다시 켜세요. 스위치에 켜는 쪽이 있는 이유입니다.

**배포 리소스**(엔드포인트가 아니라)의 Azure Monitor 메트릭이 독립적인 축입니다 —
인스턴스 위 에이전트만 내보낼 수 있으니까요:

| | provisioningState | DeploymentCapacity | GpuUtilization |
|---|---|---|---|
| 노드가 붙은 배포 | Creating | 포인트 7개 | 포인트 7개 |
| 73분째 노드 없는 배포 | Creating | **0개** | **0개** |

## 5. 기대 출력 (§57.4 실측 타임라인)

```
16:05  0%   노드 할당 (메트릭 방출 시작)
16:05–16:16 0%   이미지 풀 + HF 에서 54 GB 다운로드   ← 11분
16:17  34%  가중치 GPU 적재 시작
16:18  66%  적재 완료 (54 GB / 80 GB)
16:21  88%  KV 캐시 할당 완료, 서빙 시작
```

배포 생성부터 `Succeeded` 까지 **23분**.

> 이미지 풀은 **프로브 시계 밖**입니다. liveness 의 `initial_delay` 는 컨테이너 시작
> 시점부터 세는데 이미지 풀은 그 이전입니다. "다운로드가 11분이니 795초 예산이
> 모자란다" 는 추론은 틀렸습니다.

## 6. ⚠️ 200 OK 는 플래그가 맞다는 증거가 아니다

```bash
uv run bash scripts/verify_deployment.sh ffsft-lab blue
```

`--reasoning-parser` 를 빠뜨린 배포에 "대한민국의 수도는?" 을 보내면 `content` 가
이렇게 옵니다 (§57.5 실측):

```
'The user is asking in Korean: "What is the capital of South Korea? ..."
 ... I need to answer in one sentence in Korean.
</think>

대한민국의 수도는 서울입니다.'
```

**추론 트레이스와 `</think>` 가 사용자에게 나갈 본문에 그대로 섞입니다.**
200 OK 이고 한국어도 맞아서 단순 스모크는 통과합니다. 출력은 틀렸습니다.

`verify_deployment.sh` 가 이걸 잡습니다 — 200 을 세는 게 아니라 본문을 읽어서
`content` 안의 추론 트레이스, 빈 응답, 답이 시작되기 전에 잘린 응답에서 실패합니다.

> `verify_deployment.sh` 는 엔드포인트 키를 **자기가 직접** 가져다 씁니다 — 여러분
> 셸에는 아무것도 남지 않습니다. `ffsft loadtest` 가 쓰는 `$FFSFT_ENDPOINT_KEY` 는
> [Lab 6 §1.1](lab6.md) 에서 담습니다.

`--mamba-cache-mode` 는 다릅니다: 생략해도 떴습니다. 참인 명제는 "`align` 이 필수"가
아니라 "**모드 `all` 이 `NotImplementedError` 를 낸다**" 이고 vLLM 기본값은 `all` 이
아닙니다. 다만 측정해서 고른 값이므로 정상 경로에서는 `align` 을 보냅니다.

## 7. scoringUri 는 한 모양이 아니다

- 기본 추론 서버 → `.../score`
- OpenAI 라우트를 가진 커스텀 이미지 → `.../v1/chat/completions`

두 번째에 `/score` 를 덧붙이면 404 가 오는데, **그 본문이 JSON 으로 파싱돼서
아래에서는 "빈 응답"처럼 읽힙니다.** → [GOTCHAS #14](../GOTCHAS.md#14)

---

## 막히면

| 증상 | 항목 |
|---|---|
| 워크스페이스가 없음 | [Lab 0 §4](lab0.md) 의 `ffsft infra up` 을 안 돌린 것입니다 |
| 쿼터는 남는데 create 가 거부됨 | [#2](../GOTCHAS.md#2), [#3](../GOTCHAS.md#3) — `restrictions` 와 리전 |
| 36코어 승인인데 NV36 이 안 들어감 | [#4](../GOTCHAS.md#4) — 2배 규칙 |
| `Creating` 에서 1시간 넘게 안 움직임 | §57.3 — 메트릭 포인트가 0 이면 노드가 없습니다 |
| 답은 오는데 `</think>` 가 섞임 | §57.5 — `--reasoning-parser` 누락 |
| 답이 빈 것처럼 보임 | [#14](../GOTCHAS.md#14) — scoringUri 모양 |
| ACR 풀 실패 (404) | §57.7 (2) — §3 의 「ACR 은 이제 같은 그룹에…」. 이름 조회가 실패하면 배포 rg 로 폴백합니다 |
| 저자들의 ACR 을 pull 하려다 실패 | `--image` 를 안 준 겁니다 — §3 |
| `environment 'ffsft-serve:1' is already registered against ...` | 같은 태그로 다른 이미지를 밀었습니다. 새 태그를 빌드하세요 ([Lab 4 §6](lab4.md)) |
| `image '...' carries no tag` | 태그를 붙이세요. Azure 호출 전에 거부됩니다 |
| 실패 원인을 아예 못 보겠음 | `FFSFT_VERBOSE_AZURE=1` — SDK HTTP 덤프를 되살립니다 (§4) |
| 실패한 배포를 지웠는지 확인 | [#18](../GOTCHAS.md#18) — 실패해도 과금됩니다 |
| `BILLING NOW: nothing` 인데 청구는 계속됨 | 배너가 여러분의 그룹을 말하는지 보세요 — §3.1 |
| `down` 이 `down needs a scope` 로 거부 | 「정리」— `--endpoint ffsft-lab` 또는 `--all` 이 필요합니다 |

## 정리 — 여기가 갈림길입니다

먼저 지금 얼마인지만 봅니다. **배너가 여러분의 그룹을 말하는 셸에서** 돌리세요 (§3.1):

```bash
uv run ffsft lifecycle status          # 지금 시간당 얼마인가
```

**아직 아무것도 내리지 마세요.** 다음이 어디냐에 따라 답이 다릅니다:

| 하려는 것 | 다음 | 엔드포인트는 |
|---|---|---|
| TTFT/TPOT/knee 를 실측한다 | [Lab 6](lab6.md) | **살려 둡니다** — Lab 6 은 살아 있는 `blue` 에 겁니다 |
| 파인튜닝 가중치까지 서빙 (blue/green 전환) | [Lab 6](lab6.md) → [Lab 8](lab8.md) | **살려 둡니다** — Lab 8 이 blue 옆에 green 을 올립니다 |
| 여기서 끝 | [Lab 7](lab7.md) | **지금 내립니다** (아래) |

살려 두는 값은 **$4.959/시** 입니다. [Lab 6](lab6.md) 40분이면 약 $3.3, 그냥 켜 두고 자면
하룻밤(12시간)에 $59.5, 하루에 $119 입니다 ([Lab 8](lab8.md), [Lab 7](lab7.md)).
**"나중에 내리지" 는 시간당 $4.96 짜리 결정입니다.** 살려 두기로 했으면 [Lab 6](lab6.md)
으로 바로 가세요 — 이 엔드포인트는 그동안 계속 돕니다.

**여기서 끝낼 때만** 내립니다:

```bash
uv run ffsft lifecycle down --endpoint ffsft-lab --yes
```

`--endpoint` 나 `--all` 중 **하나는 필수**입니다. 스코프 없이 부르면 아무것도 안 지우고
exit 2 로 거부합니다 — Azure 를 부르기 전에 끝납니다:

```
down needs a scope: --endpoint NAME for one endpoint, or --all for everything
refusing to guess: --all deletes every billing resource in this workspace
```

`BILLING NOW: nothing` 이 나와야 끝난 것입니다. 다만 그 문장은 **이 워크스페이스에서는**
안 돈다는 뜻이지 **그룹이 비었다**는 뜻이 아닙니다 — 스토리지·ACR·Key Vault 는
엔드포인트가 아니라서 이 표에 안 나옵니다. 그룹째 내리는 것은 [Lab 7](lab7.md) 의
`ffsft infra down` 입니다.

> 표에 SKU 대신 `?` 와 `(price unknown for this SKU)` 가 뜨면, 그 리소스는 **합계에서
> 빠져 있습니다** — 아래 `EXCLUDES ... whose rate is unknown` 줄이 그걸 말합니다.
> **모르는 값은 0 이 아닙니다.** 값이 안 붙었다고 안 돌고 있는 게 아닙니다.

**다음**: 위 표대로 — [Lab 6 — 로드테스트](lab6.md) (엔드포인트를 띄워 둔 채로 이어서)
또는 [Lab 7 — 내리기](lab7.md).
