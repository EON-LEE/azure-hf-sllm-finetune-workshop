# RUNBOOK — 직접 올리고 내리기

이 문서는 **사람이 손으로 실행하는 순서**입니다. 모든 명령은 실제로 이 구독에서
실행해 결과를 확인한 것만 적었습니다. 검증 근거는 `docs/VERIFIED.md`에 있습니다.

## 0. 환경

```bash
export FFSFT_SUBSCRIPTION_ID=cb370f4f-5f39-479b-8911-96ec487998b1
export FFSFT_RESOURCE_GROUP=rg-ffsft-kc
export FFSFT_WORKSPACE=mlw-ffsft
az login          # 또는 az login --use-device-code
```

`uv sync --extra azure` 로 의존성을 설치합니다.

---

## 1. 지금 뭐가 돈을 쓰고 있는지

**항상 여기서 시작하세요.** 온라인 엔드포인트는 요청이 없어도 24시간 과금되고
0으로 축소되지 않습니다.

```bash
uv run ffsft-lifecycle status
```

`BILLING NOW: nothing` 이면 깨끗한 상태입니다.

---

## 2. 배포 가능한 패턴 확인

```bash
uv run ffsft-deploy check
```

이 구독에서 나오는 실제 결과와 그 의미:

| 표시 | 뜻 |
|---|---|
| `datastore UNREACHABLE` | 워크스페이스 스토리지 계정이 공개 접근 차단 + 프라이빗 엔드포인트 0개. §24 참조. **모델 자산 등록이 불가능**합니다. |
| `aml_online_vllm  ok  via --hf-model` | vLLM이 Hugging Face Hub에서 직접 가중치를 받으므로 스토리지를 타지 않습니다. **이 경로만 열려 있습니다.** |
| `aml_batch  BLOCKED` | 배치 배포는 모델 자산을 이름으로 지정해야 하므로 스토리지 벽에 막힙니다. |

---

## 3. 올리기 (up)

### 3.1 SKU 고르기 — 쿼터 계산이 함정입니다

온라인 엔드포인트는 롤링 업데이트를 위해 **요청 코어의 2배**를 요구합니다.

| SKU | 코어 | 실제 필요 | A10 쿼터 36코어에서 |
|---|---|---|---|
| `Standard_NV6ads_A10_v5` | 6 | 12 | 가능 |
| `Standard_NV12ads_A10_v5` | 12 | 24 | **가능 (기본값)** |
| `Standard_NV18ads_A10_v5` | 18 | 36 | 딱 맞음 |
| `Standard_NV36ads_A10_v5` | 36 | 72 | **불가** |

기본값은 `Standard_NV12ads_A10_v5` 이고 `tests/test_serving_registry.py` 가
이 값을 실측 쿼터에 고정하므로, `--sku` 를 생략해도 쿼터 오류는 나지 않습니다.

> ⚠️ **쿼터가 있어도 배포가 안 될 수 있습니다.** 2026-08-22 기준 koreacentral 은
> NV12/NV6 둘 다 노드를 배정하지 못했습니다(§27.5). 쿼터는 *허가*이고 용량은
> *사실*입니다. 증상과 대처는 아래 §7 표를 보세요.

현재 쿼터 확인:

```bash
az quota show \
  --scope "/subscriptions/$FFSFT_SUBSCRIPTION_ID/providers/Microsoft.Compute/locations/koreacentral" \
  --resource-name standardNVADSA10v5Family --query "properties.limit.value" -o tsv
```

### 3.2 배포

```bash
uv run ffsft-lifecycle up \
  --endpoint ffsft-a10 \
  --hf-model Qwen/Qwen3.5-0.8B \
  --sku Standard_NV12ads_A10_v5 \
  --params-b 0.8 \
  --max-model-len 2048
```

15~30분 걸립니다. `--hf-model` 대신 `--model-uri azureml:이름:버전` 을 쓰면
등록된 모델을 마운트하지만, 위 §2 때문에 이 구독에서는 실패합니다.

**진행 상황을 보는 법:** `percentComplete` 는 믿지 마세요 — 같은 상태에서
`0.0` 이 나오기도 하고 `null` 이 나오기도 합니다(§27.3). `provisioningState`
전이만 신뢰할 수 있습니다.

```bash
az rest --method get --url \
 "https://management.azure.com/subscriptions/$FFSFT_SUBSCRIPTION_ID/resourceGroups/$FFSFT_RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$FFSFT_WORKSPACE/onlineEndpoints/ffsft-a10/deployments/blue?api-version=2023-04-01-preview" \
 --query "properties.provisioningState" -o tsv
```

**언제 포기할지:** readiness probe 는 최대 약 7분 20초 만에 판정합니다. 따라서
컨테이너가 떴다면 **~10분 안에** `Succeeded` 또는 `Failed` 가 나와야 정상입니다.
**15분이 넘도록 `Creating` 이면 노드가 배정되지 않은 것**이고, 더 기다려도
바뀌지 않습니다(50분·85분 두 번 확인). 바로 §6 으로 내리세요.

### 3.3 AcrPull — 자동이지만 실패할 수 있습니다

엔드포인트의 시스템 할당 ID는 커스텀 ACR(`acrffsftkc`)에 대한 pull 권한을
**자동으로 받지 못합니다**. Azure는 워크스페이스 연결 ACR에만 자동 연결해주는데
이 워크스페이스에는 연결 ACR이 없습니다.

코드가 `ensure_acr_pull`로 직접 부여하지만, 여러분의 계정에 RBAC 쓰기 권한이
없으면 실패하고 실행할 명령을 출력합니다. 수동으로 하려면:

```bash
PID=$(az ml online-endpoint show -n ffsft-a10 \
        -g $FFSFT_RESOURCE_GROUP -w $FFSFT_WORKSPACE \
        --query identity.principalId -o tsv)
ACR=$(az acr show -n acrffsftkc -g $FFSFT_RESOURCE_GROUP --query id -o tsv)
az role assignment create --assignee-object-id "$PID" \
  --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR"
```

부여 후 **1~2분 전파를 기다린 뒤** 다시 배포하세요.

이 권한이 없으면 약 10분 뒤 이렇게 죽습니다:

```
(BadArgument) Endpoint identity does not have pull permission on the registry.
```

---

## 4. 호출해보기

```bash
uv run ffsft-lifecycle status          # scoring uri 와 키 확인
```

```bash
curl -s -X POST "$SCORING_URI" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"ffsft","messages":[{"role":"user","content":"한국의 수도는?"}],
       "max_tokens":64}' | python3 -m json.tool
```

---

## 5. 로드 테스트

```bash
uv run ffsft-loadtest \
  --url "$SCORING_URI" --key "$KEY" --model ffsft \
  --concurrency 1,2,4,8 --requests 12 --max-tokens 64
```

**주의:** 이 도구는 `stream: true` 로 보내고 첫 SSE 토큰까지의 시간(TTFT)을
잽니다. 스트리밍하지 않는 엔드포인트는 HTTP 200을 받아도 `no tokens streamed`
로 전부 실패 처리됩니다. 이는 버그가 아니라 의도입니다 — 통짜 JSON 응답에서는
TTFT를 잴 수 없습니다.

---

## 6. 반드시 내리기 (down)

```bash
uv run ffsft-lifecycle down --endpoint ffsft-a10 --yes
uv run ffsft-lifecycle status          # BILLING NOW: nothing 확인
```

전부 정리하려면:

```bash
uv run ffsft-lifecycle down --all --yes
```

> 온라인 엔드포인트를 켜둔 채 잊으면 A10 기준 하루 수만 원이 나갑니다.
> `status` 로 끝내는 습관을 들이세요.

### 6.1 `Creating` 에서 멈춘 배포는 `down` 으로 안 지워집니다

Azure 는 프로비저닝 중인 배포의 삭제를 거부합니다. **엔드포인트를 통째로**
ARM DELETE 하세요 (배포도 같이 사라집니다):

```bash
az rest --method delete --url \
 "https://management.azure.com/subscriptions/$FFSFT_SUBSCRIPTION_ID/resourceGroups/$FFSFT_RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$FFSFT_WORKSPACE/onlineEndpoints/ffsft-a10?api-version=2023-04-01-preview"
```

`202` 가 오면 접수된 것이고 **완료까지 30분 넘게 걸립니다**. 그동안
`provisioningState` 는 `Deleting` 입니다. 다 지워졌는지는 개수로 확인하세요:

```bash
az rest --method get --url \
 "https://management.azure.com/subscriptions/$FFSFT_SUBSCRIPTION_ID/resourceGroups/$FFSFT_RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$FFSFT_WORKSPACE/onlineEndpoints?api-version=2023-04-01-preview" \
 --query "length(value)" -o tsv     # 0 이어야 합니다
```

---

## 7. 막혔을 때

| 증상 | 원인 | 대응 |
|---|---|---|
| `Endpoint identity does not have pull permission` | 엔드포인트 ID에 AcrPull 없음 | §3.3 |
| `not have enough quota... requested 72` | SKU 코어의 2배가 쿼터 초과 | §3.1에서 더 작은 SKU로 |
| `NoMatchingArtifactsFoundFromJob` | 학습 산출물이 스토리지에 안 올라감 (§24) | `--hf-model` 경로 사용 |
| 배포가 `Failed` 후 업데이트 거부 | Azure는 초기 프로비저닝 실패한 배포를 수정 못 함 | 코드가 자동 삭제 후 재생성. 수동이면 `az ml online-deployment delete` |
| 컨테이너 로그가 아예 없음 | 이미지 pull 자체가 거부됨 | §3.3 — 로그가 없는 것이 곧 단서입니다 |
| **15분 넘게 `Creating`, 로그 없음, `Failed` 도 안 뜸** | **리전에 물리 GPU 용량이 없음.** 쿼터가 있어도 발생합니다 | 기다려도 안 됩니다. §6.1로 내리고 리전 또는 호스팅 표면을 바꾸세요 (VERIFIED §27.6) |
| `down` 이 배포를 못 지움 | `Creating` 중인 배포는 삭제 거부 | §6.1 — 엔드포인트를 통째로 ARM DELETE |
