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

## 선행조건

- Lab 4 의 서빙 이미지가 ACR 에 있음
- **dedicated** GPU 를 살 수 있는 리전 — 온라인 엔드포인트에 LowPriority 옵션은 **없습니다**

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

## 2. 코어를 2배로 계산한다

온라인 엔드포인트는 롤링 업데이트 여유분으로 `ceil(1.2 × 인스턴스) × 코어` 를 청구합니다.
**1 인스턴스가 코어를 두 배 먹습니다.** 36코어 승인으로 NV36 이 안 들어가는 이유입니다.

**단, A100/H100/ND 계열은 면제**입니다 (문서의 "Skip 20% Reservation").
24코어 A100 은 24코어면 됩니다. → [GOTCHAS #4](../GOTCHAS.md#4)

```bash
uv run ffsft-deploy check --probe
```

`--probe` 는 쿼터 숫자를 믿는 대신 **컨트롤 플레인에 실제로 물어봅니다** — 거부는
아무것도 만들지 않고, 수락된 것은 `min=0` 으로 만들었다가 삭제하므로 공짜입니다.

## 3. 배포

```bash
export FFSFT_LOCATION=<dedicated GPU 가 열린 리전>
export FFSFT_RESOURCE_GROUP=<그 리전의 rg>
export FFSFT_WORKSPACE=<그 리전의 워크스페이스>

uv run ffsft-deploy deploy-online \
   --endpoint ffsft-lab \
   --hf-model Qwen/Qwen3.8-27B \
   --deployment blue \
   --sku Standard_NC24ads_A100_v4 \
   --max-model-len 8192 \
   --traffic 100
```

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
| 쿼터는 남는데 create 가 거부됨 | [#2](../GOTCHAS.md#2), [#3](../GOTCHAS.md#3) — `restrictions` 와 리전 |
| 36코어 승인인데 NV36 이 안 들어감 | [#4](../GOTCHAS.md#4) — 2배 규칙 |
| `Creating` 에서 1시간 넘게 안 움직임 | §57.3 — 메트릭 포인트가 0 이면 노드가 없습니다 |
| 답은 오는데 `</think>` 가 섞임 | §57.5 — `--reasoning-parser` 누락 |
| 답이 빈 것처럼 보임 | [#14](../GOTCHAS.md#14) — scoringUri 모양 |
| ACR 풀 실패 (404) | §57.7 (2) — ACR 이 배포와 다른 RG 에 있을 수 있습니다 |
| 실패한 배포를 지웠는지 확인 | [#18](../GOTCHAS.md#18) — 실패해도 과금됩니다 |

## 정리 — 반드시

```bash
uv run ffsft-lifecycle status          # 지금 시간당 얼마인가
uv run ffsft-lifecycle down --yes      # 다 끝났으면
```

`BILLING NOW: nothing` 이 나와야 끝난 것입니다. 자세한 절차는 [Lab 7](lab7.md).

**다음**: [Lab 6 — 로드테스트](lab6.md) (엔드포인트를 띄워 둔 채로 이어서)
