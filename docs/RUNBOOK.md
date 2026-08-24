# RUNBOOK — 직접 올리고 내리기

이 문서는 **사람이 손으로 실행하는 순서**입니다. 모든 명령은 실제로 이 구독에서
실행해 결과를 확인한 것만 적었습니다. 검증 근거는 `docs/VERIFIED.md`에 있습니다.

## 0. 환경

```bash
export FFSFT_SUBSCRIPTION_ID=cb370f4f-5f39-479b-8911-96ec487998b1
export FFSFT_RESOURCE_GROUP=rg-ffsft-kc
export FFSFT_WORKSPACE=mlw-ffsft
az login          # 또는 az login --use-device-code

# 계정이 여러 테넌트에 걸쳐 있으면 반드시 고정하세요
az account set --subscription $FFSFT_SUBSCRIPTION_ID
az account show --query "{id:id,tenant:tenantId}" -o tsv
# -> cb370f4f-...   4510ec63-0634-4550-9f93-2dc7de6cecec
```

> **`InvalidAuthenticationTokenTenant` 이 뜨면 코드 문제가 아닙니다.**
> `az` 의 기본 구독이 다른 테넌트로 바뀐 것뿐입니다. 환경변수는 구독 ID 만 정하고
> 토큰의 테넌트는 `az` 의 활성 계정에서 나오기 때문에, 둘이 어긋나면
> "access token is from the wrong issuer" 가 납니다. 위의 `az account set` 이 해결책입니다.

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
| 코드를 고쳤는데 노드에서 **예전 에러가 그대로** 재현됨 | `TRAIN_IMAGE` 는 올렸는데 환경 버전이 그대로여서 옛 이미지가 실행됨 | §8.3 — 이제 버전은 태그에서 자동 파생됩니다 |
| `Failed to pull ... 401 authentication required` | 클러스터를 재생성해서 시스템 ID가 바뀜 → AcrPull 소멸 | §8.2 |
| `Resource name is invalid...alphanumeric, dashes, underscores` | 모델 자산 이름에 `.` 이 들어감 (`kanana2-1.3b`) | §8.4 — `asset_name()` 이 처리 |
| 잡은 `Completed` 인데 등록할 게 없음 | 출력을 선언하지 않아 어댑터가 노드와 함께 사라짐 | `JobSpec.declared_outputs()` — 선언된 출력만 업로드됩니다 |
| `AssetMountOutputSession.Exception` (노드 시작 중) | 스토리지에 노드가 접근할 경로가 없음 | §8.1 — managed VNet |
| `InvalidAuthenticationTokenTenant` / `wrong issuer` | `az` 기본 구독이 다른 테넌트로 바뀜 | §0 — `az account set --subscription` |

---

## 8. 학습 → 등록 → 서빙 (2026-08-24 실측)

이 체인은 프로젝트 내내 막혀 있었습니다. 원인이 **세 개**였고 셋 다 풀려야 했습니다.

### 8.1 스토리지 — managed VNet 이 정답입니다

스토리지 계정의 `publicNetworkAccess` 는 **관리 그룹 정책이 쓰기 시점에 강제로 되돌립니다.**
ARM PATCH 는 200 을 반환하고 값은 그대로 `Disabled` 이고, `Enabled` 로 **새로 만든** 계정조차
`Disabled` 로 돌아옵니다. 공개 접근을 켜는 방향은 존재하지 않는 길입니다.

정책이 막는 것은 *공개* 경로뿐이고, 사설 경로는 정책이 원래 유도하는 길입니다.

```bash
# 1) 컴퓨트가 하나라도 있으면 거부됩니다 — 먼저 지우세요
az ml compute delete --name gpu-a100-lp -g $RG -w $WS --yes

# 2) isolationMode 를 켜고
az rest --method patch --url "$WSID?api-version=2024-10-01" \
  --body '{"properties":{"managedNetwork":{"isolationMode":"AllowInternetOutbound"}}}'

# 3) 프로비저닝 — body 가 반드시 있어야 합니다
#    없으면 "Request body could not be read" 로 실패합니다
az rest --method post --url "$WSID/provisionManagedNetwork?api-version=2024-10-01" \
  --body '{"includeSpark": false}'
```

끝나면 스토리지 계정에 **승인된 private endpoint connection 2개**가 생깁니다.
리소스 그룹의 `Microsoft.Network/privateEndpoints` 는 **0 으로 보이는 게 정상**입니다 —
서비스가 관리하는 연결이라 RG 에 리소스로 나타나지 않습니다.

이때부터 되는 것들: 노드→블롭 업로드, 다음 잡에서 그 블롭을 RO_MOUNT, 잡 로그 읽기.

> 파킹할 때는 `isolationMode: Disabled` 로 되돌리세요. private endpoint 는 개당 시간당 과금됩니다.

### 8.2 클러스터를 재생성하면 RBAC 이 조용히 사라집니다

클러스터의 system-assigned identity 는 **생성할 때마다 새로 발급**됩니다.
증상은 `Failed to pull Docker image ... 401 authentication required` 입니다.

```bash
PRINCIPAL=$(az ml compute show -n gpu-a100-lp -g $RG -w $WS --query identity.principal_id -o tsv)
az role assignment create --assignee $PRINCIPAL --role AcrPull --scope $ACR_ID
az role assignment create --assignee $PRINCIPAL \
  --role "Storage Blob Data Contributor" --scope $STORAGE_ID
```

**클러스터를 지우는 teardown 은 반드시 이 재부여를 같이 적어두세요.**

### 8.3 이미지 태그와 환경 버전은 같이 움직여야 합니다

Azure ML 환경은 **버전 단위로 불변**입니다. 이미 있는 버전에 `create_or_update` 를 해도
덮어쓰지 않고 **저장된 것을 그대로 돌려줍니다.**

`TRAIN_IMAGE` 를 `:10` 으로 올리고 `ENVIRONMENT_VERSION` 을 `"9"` 로 두면,
잡은 조용히 `ffsft-train:9` 로 실행됩니다. 실제로 그렇게 A100 을 할당받아 9GB 를 받고
**이미 고친 에러로 다시 죽었습니다** (`plum_station_dxwtzlz94q`).

지금은 `image_tag()` 가 태그에서 버전을 파생하고, `ensure_environment()` 가 기존 등록의
이미지를 대조한 뒤에만 재사용합니다. 다르면 노드를 잡기 전에 거부합니다.

**GPU 를 쓰기 전에 이미지 내용을 확인하는 법** — ACR 에서 몇 센트로 끝납니다:

```bash
az acr run --registry acrffsftkc -g $RG --cmd \
  "acrffsftkc.azurecr.io/ffsft-train:10 python -c 'import sys; sys.path.insert(0,\"/opt/ffsft/src\"); import ffsft.train.qlora as q; print(hasattr(q,\"base_load_kwargs\"))'" \
  /dev/null
```

### 8.4 등록 — 이름에 점을 못 씁니다

Azure ML 자산 이름은 영숫자·대시·언더스코어만 허용합니다. 그런데 레지스트리 키는
`kanana2-1.3b`, `qwen3.8-27b` 처럼 **대부분 점을 포함**합니다.

```python
from ffsft.deploy.model_asset import register_adapter
ref = register_adapter(client, job_name, "kanana2-1.3b",
                       base_model=spec.hf_id, mix="ko_smoke")
# -> 'kanana2-1_3b-ko-lora:1'   (원래 키는 model_key 태그에 보존)
```

URI 는 **데이터스토어 경로**여야 합니다. `azureml://jobs/{job}/outputs/{name}` 은
직관적이지만 서비스가 `NoMatchingArtifactsFoundFromJob` 로 거부합니다.

```
azureml://datastores/workspaceblobstore/paths/azureml/{job}/model_dir/
```

> `job.outputs[name].path` 를 믿지 마세요. 업로드가 성공해도 ARM 은 `null` 을 돌려줍니다.

### 8.5 등록은 증거가 아닙니다 — 마운트해서 확인하세요

서비스는 **존재하지 않는 폴더를 가리켜도 등록을 받아줍니다.** 등록 성공은
"학습된 모델이 있다"는 뜻이 아닙니다. 잡으로 마운트해서 실제로 세어보세요.

```python
inputs={"adapter": Input(type=AssetTypes.CUSTOM_MODEL,
                         path="azureml:kanana2-1_3b-ko-lora:2",
                         mode=InputOutputModes.RO_MOUNT)}
# 잡 안에서 os.walk 로 세고 mlflow.set_tag 로 돌려받습니다
```

실측(`hungry_apple_n455nrpngf`): 19개 파일 / 133,476,918 바이트,
`adapter_model.safetensors` 37,415,384 바이트, `checkpoint-30/` 존재.

### 8.6 어댑터는 그대로는 서빙이 안 됩니다

등록된 어댑터 폴더에는 `config.json` 도 베이스 가중치도 없습니다. vLLM 은 이걸 못 엽니다.
`ffsft.deploy.merge` 로 베이스와 합쳐 일반 HF 체크포인트를 만든 뒤, 그것을 다시 등록해서
배포합니다. (`adapter_modes` 의 `runtime_adapter` 는 두 번째 선택지입니다 — SERVING.md)

### 8.7 워크스테이션에서 안 되는 건 잡에서 하세요

이 워크스테이션은 블롭에 사설 경로가 없습니다. 그래서 로그 본문도 `jobs.download()` 도 403 입니다.
**하지만 노드는 됩니다.** 원칙 하나로 정리됩니다:

> 워크스테이션에서 닿지 않는 것은 잡에서 닿고, 돌려받는 통로는 MLflow 태그입니다.

- 에러 메시지: `MLClient.jobs.stream(name)` — 서비스를 거치므로 읽힙니다
- 로그 전문: 실패한 런의 아티팩트 폴더를 마운트하는 진단 잡 + `mlflow.set_tag`

```python
Input(path=f"azureml://datastores/workspaceartifactstore/paths/ExperimentRun/dcid.{run}/",
      mode=InputOutputModes.RO_MOUNT)
```
