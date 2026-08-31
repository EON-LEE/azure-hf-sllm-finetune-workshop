# RUNBOOK — 직접 올리고 내리기

이 문서는 **사람이 손으로 실행하는 순서**입니다. 모든 명령은 실제로 이 구독에서
실행해 결과를 확인한 것만 적었습니다.

## 0. 환경 — 프로필은 하나입니다

rg·워크스페이스·리전은 프로필 파일 **하나**에 들어 있습니다.
[Lab 0 §4](labs/lab0.md) 의 `ffsft infra up` 이 만듭니다.

```bash
source ~/.ffsft-env
# profile: ffsft  rg=…  ws=…  loc=…      <- 배너를 읽고 시작하세요
az login                       # 또는 az login --use-device-code
```

**배너가 곧 이 셸이 어디를 조회할지입니다.** `ffsft lifecycle` 은 워크스페이스를 받는
플래그가 없고 환경변수만 봅니다. 그래서 `BILLING NOW: nothing` 은
"아무 데서도 안 돈다"가 아니라 "**이 워크스페이스에서는** 안 돈다"입니다.

> **학습과 서빙이 갈리는 경우가 있습니다.** koreacentral 은 LowPriority A100 학습은
> 되는데 관리형 엔드포인트가 안 뜹니다. 워크샵은 그래서 **둘 다 파는 리전 하나**를
> 고르게 합니다([Lab 0 §3](labs/lab0.md)). 그래도 갈라야 하면 프로필을 손으로 만들지
> 말고 `ffsft infra up --prefix <다른 prefix> --write-env ~/.ffsft-env2` 로 **또 하나의
> 그룹**을 만드세요 — 내릴 때 `infra down` 을 prefix 마다 한 번씩 돌리면 됩니다.
> 이 문서에서 두 rg 를 같이 부르는 자리는 §3.3 하나뿐입니다.

프로필 파일이 없으면(워크샵을 안 거쳤으면) 손으로 내보냅니다.

```bash
export FFSFT_SUBSCRIPTION_ID=<your-subscription-id>   # az account show --query id -o tsv
export FFSFT_RESOURCE_GROUP=<your-rg>
export FFSFT_WORKSPACE=<your-workspace>
export FFSFT_LOCATION=<your-region>
```

어느 쪽이든 테넌트는 고정하고 시작합니다.

```bash
# 계정이 여러 테넌트에 걸쳐 있으면 반드시 고정하세요
az account set --subscription $FFSFT_SUBSCRIPTION_ID
az account show --query "{id:id,tenant:tenantId}" -o tsv
# -> <your-subscription-id>   <your-tenant-id>
#    두 값이 나오면 됩니다. 아래 오류는 이 둘이 어긋났을 때 납니다.
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
source ~/.ffsft-env
uv run ffsft lifecycle status
```

`BILLING NOW: nothing` 이면 GPU 는 안 도는 상태입니다 — **단, 이 워크스페이스에서만**
그렇습니다. `status` 는 지금 셸에 실린 환경변수 하나만 봅니다. **그룹이 비었다는 뜻은
아닙니다** — 스토리지·ACR·KeyVault 는 뒤에 남아서 과금됩니다. 그룹째 확인하고 없애는
것은 §9 이고, 워크샵 쪽은 [Lab 7 §7](labs/lab7.md) 입니다.

---

## 2. 배포 가능한 패턴 확인

```bash
uv run ffsft deploy check
```

이 구독에서 나오는 실제 결과와 그 의미:

| 표시 | 뜻 |
|---|---|
| `datastore UNREACHABLE` | 워크스페이스 스토리지 계정이 공개 접근 차단 + 프라이빗 엔드포인트 0개. **모델 자산 등록이 불가능**합니다. |
| `aml_online_vllm  ok  via --hf-model` | vLLM이 Hugging Face Hub에서 직접 가중치를 받으므로 스토리지를 타지 않습니다. **이 경로만 열려 있습니다.** |
| `aml_batch  BLOCKED` | 배치 배포는 모델 자산을 이름으로 지정해야 하므로 스토리지 벽에 막힙니다. |

---

## 3. 올리기 (up)

**`source ~/.ffsft-env`** — 이 절의 명령은 전부 그 워크스페이스로 나갑니다.

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

> ⚠️ **쿼터가 있어도 배포가 안 될 수 있습니다.** koreacentral 은 실측 당시
> NV12/NV6 둘 다 노드를 배정하지 못한 적이 있습니다. 쿼터는 *허가*이고 용량은
> *사실*입니다. 증상과 대처는 아래 §7 표를 보세요.

현재 쿼터 확인:

```bash
az quota show \
  --scope "/subscriptions/$FFSFT_SUBSCRIPTION_ID/providers/Microsoft.Compute/locations/$FFSFT_LOCATION" \
  --resource-name standardNVADSA10v5Family --query "properties.limit.value" -o tsv
```

### 3.2 배포

```bash
uv run ffsft lifecycle up \
  --endpoint ffsft-a10 \
  --hf-model Qwen/Qwen3.5-0.8B \
  --sku Standard_NV12ads_A10_v5 \
  --params-b 0.8 \
  --max-model-len 2048
```

15~30분 걸립니다. `--hf-model` 대신 `--model-uri azureml:이름:버전` 을 쓰면
등록된 모델을 마운트하지만, 위 [§2](#2-배포-가능한-패턴-확인) 때문에 이 구독에서는 실패합니다.

**진행 상황을 보는 법:** `percentComplete` 는 믿지 마세요 — 같은 상태에서
`0.0` 이 나오기도 하고 `null` 이 나오기도 합니다. `provisioningState`
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

엔드포인트의 시스템 할당 ID는 커스텀 ACR에 대한 pull 권한을 **자동으로 받지 못합니다**.
Azure는 워크스페이스 연결 ACR에만 자동 연결해주는데 이 워크스페이스에는 연결 ACR이 없습니다.

코드가 `ensure_acr_pull`로 직접 부여하지만, 여러분의 계정에 RBAC 쓰기 권한이
없으면 실패하고 실행할 명령을 출력합니다. 수동으로 하려면:

```bash
source ~/.ffsft-env

PID=$(az ml online-endpoint show -n ffsft-a10 \
        -g "$FFSFT_RESOURCE_GROUP" -w "$FFSFT_WORKSPACE" \
        --query identity.principalId -o tsv)

ACR=$(az acr show -n "$FFSFT_ACR" -g "$FFSFT_RESOURCE_GROUP" --query id -o tsv)

az role assignment create --assignee-object-id "$PID" \
  --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR"
```

> ⚠️ **엔드포인트와 ACR 이 다른 그룹에 있으면 위 두 줄이 같은 `-g` 를 못 씁니다.**
> 워크샵은 `infra up` 이 ACR 을 워크스페이스와 같은 그룹에 만들어서 이 문제를 없앴습니다
> ([Lab 0 §4](labs/lab0.md)). 리전을 갈라 그룹을 둘 만든 예외 경로라면 rg 를 **두 변수로
> 나눠** 쓰세요 — 하나로 재사용하면 한쪽이 빈 값이나 404 가 되고, 부여는 조용히 엉뚱한
> 스코프로 나갑니다. 이 절에 오는 사람은 **권한 없이 나간 배포를 이미 하나 들고 있는**
> 상태라, 그 실수의 값은 아래 "약 10분 뒤 죽는" 롤아웃 한 번을 **더** 태우는 것입니다.

부여 후 **1~2분 전파를 기다린 뒤** 다시 배포하세요.

이 권한이 없으면 약 10분 뒤 이렇게 죽습니다:

```
(BadArgument) Endpoint identity does not have pull permission on the registry.
```

---

## 4. 호출해보기

```bash
uv run ffsft lifecycle status          # scoring uri 와 키 확인
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
uv run ffsft loadtest \
  --url "$SCORING_URI" --key "$KEY" --model ffsft \
  --concurrency 1,2,4,8 --requests 12 --max-tokens 64
```

**주의:** 이 도구는 `stream: true` 로 보내고 첫 SSE 토큰까지의 시간(TTFT)을
잽니다. 스트리밍하지 않는 엔드포인트는 HTTP 200을 받아도 `no tokens streamed`
로 전부 실패 처리됩니다. 이는 버그가 아니라 의도입니다 — 통짜 JSON 응답에서는
TTFT를 잴 수 없습니다.

---

## 6. 반드시 내리기 (down)

**`down` 도 `status` 와 같은 환경변수로 워크스페이스를 정합니다.** 지우기 전에
`source ~/.ffsft-env` 로 배너를 확인하세요.

```bash
uv run ffsft lifecycle down --endpoint ffsft-a10 --yes
uv run ffsft lifecycle status          # BILLING NOW: nothing 확인
```

전부 정리하려면:

```bash
uv run ffsft lifecycle down --all --yes
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
| `az acr show` 가 ACR 을 못 찾음 (`ResourceNotFound`) | ACR 이 이 그룹에 없습니다 — 그룹을 갈라 만든 경우 | §3.3 — rg 를 두 변수로 나눠 부르세요 |
| `status` 는 `BILLING NOW: nothing` 인데 청구서가 나옴 | `status` 는 워크스페이스만 봅니다. 스토리지·ACR·KeyVault 는 그룹에 남아 있습니다 | §9 — 그룹째 확인하고 없애세요 |
| `not have enough quota... requested 72` | SKU 코어의 2배가 쿼터 초과 | §3.1에서 더 작은 SKU로 |
| `NoMatchingArtifactsFoundFromJob` | 학습 산출물이 스토리지에 안 올라감 | `--hf-model` 경로 사용 |
| 배포가 `Failed` 후 업데이트 거부 | Azure는 초기 프로비저닝 실패한 배포를 수정 못 함 | 코드가 자동 삭제 후 재생성. 수동이면 `az ml online-deployment delete` |
| 컨테이너 로그가 아예 없음 | 이미지 pull 자체가 거부됨 | §3.3 — 로그가 없는 것이 곧 단서입니다 |
| **15분 넘게 `Creating`, 로그 없음, `Failed` 도 안 뜸** | **리전에 물리 GPU 용량이 없음.** 쿼터가 있어도 발생합니다 | 기다려도 안 됩니다. §6.1로 내리고 리전 또는 호스팅 표면을 바꾸세요 |
| `down` 이 배포를 못 지움 | `Creating` 중인 배포는 삭제 거부 | §6.1 — 엔드포인트를 통째로 ARM DELETE |
| 코드를 고쳤는데 노드에서 **예전 에러가 그대로** 재현됨 | `TRAIN_IMAGE` 는 올렸는데 환경 버전이 그대로여서 옛 이미지가 실행됨 | §8.3 — 이제 버전은 태그에서 자동 파생됩니다 |
| `Failed to pull ... 401 authentication required` | 클러스터를 재생성해서 시스템 ID가 바뀜 → AcrPull 소멸 | §8.2 |
| `Resource name is invalid...alphanumeric, dashes, underscores` | 모델 자산 이름에 `.` 이 들어감 (`kanana2-1.3b`) | §8.4 — `asset_name()` 이 처리 |
| 잡은 `Completed` 인데 등록할 게 없음 | 출력을 선언하지 않아 어댑터가 노드와 함께 사라짐 | `JobSpec.declared_outputs()` — 선언된 출력만 업로드됩니다 |
| `AssetMountOutputSession.Exception` (노드 시작 중) | 스토리지에 노드가 접근할 경로가 없음 | §8.1 — managed VNet |
| `InvalidAuthenticationTokenTenant` / `wrong issuer` | `az` 기본 구독이 다른 테넌트로 바뀜 | §0 — `az account set --subscription` |

---

## 8. 학습 → 등록 → 서빙

학습 → 등록 → 서빙 전체 체인을 처음부터 끝까지 실행하는 절차입니다. 막히는 지점이
크게 세 곳(스토리지 접근, 클러스터 RBAC, 이미지/환경 버전)이라 순서대로 짚습니다.

> **`source ~/.ffsft-env` (배너 `profile: ffsft`).**
> 아래 명령의 `$RG`/`$WS` 는 그 프로필의 `$FFSFT_RESOURCE_GROUP`/`$FFSFT_WORKSPACE` 이고,
> `$ACR` 은 여러분의 ACR 이름(`az acr list -g $RG --query "[].name" -o tsv`)으로 바꿔 씁니다.

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
잡은 조용히 `ffsft-train:9` 로 실행됩니다. 실제로 그렇게 A100 을 할당받고
**이미 고친 에러로 다시 죽은 적이 있습니다.**

지금은 `image_tag()` 가 태그에서 버전을 파생하고, `ensure_environment()` 가 기존 등록의
이미지를 대조한 뒤에만 재사용합니다. 다르면 노드를 잡기 전에 거부합니다.

**GPU 를 쓰기 전에 이미지 내용을 확인하는 법** — ACR 에서 몇 센트로 끝납니다:

```bash
az acr run --registry $ACR -g $RG --cmd \
  "$ACR.azurecr.io/ffsft-train:10 python -c 'import sys; sys.path.insert(0,\"/opt/ffsft/src\"); import ffsft.train.qlora as q; print(hasattr(q,\"base_load_kwargs\"))'" \
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

실측 예: 19개 파일 / 133,476,918 바이트,
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

---

## 9. 그룹째 내리기 — 워크샵을 닫는 한 줄

§6 의 `lifecycle down` 은 **미터를 멈춥니다.** 워크스페이스·스토리지·ACR·KeyVault 는
그대로 남고, `status` 는 그것들을 못 봅니다. 그렇게 삭제된 VM 이 남긴 디스크·IP 만으로
**$41.66/월** 이 샌 적이 있습니다.

리소스 그룹이 청구 경계이자 삭제 단위입니다. 워크샵이 그룹 하나만 쓰는 이유가 이것입니다
([Lab 0 §4](labs/lab0.md)).

```bash
uv run ffsft infra down --prefix <본인>          # 먼저 계획만 (dry run)
uv run ffsft infra down --prefix <본인> --yes    # 실제 삭제
```

`--yes` 없이 부르면 지울 목록만 찍고 아무것도 안 지웁니다.

**그룹을 지우기 전에 그룹 안을 읽습니다.** KeyVault 는 삭제 후에도 소프트 삭제 상태로
**90일** 남아서 같은 이름을 막고, 이름은 ARM `uniqueString` 에서 나와 로컬에서 재현할 수
없습니다. 그래서 목록을 못 읽으면 purge 할 이름을 알 방법이 없습니다.

| 종료 코드 | 뜻 | 대응 |
|---|---|---|
| `0` | 지웠고, prefix 아래 남은 것이 없음을 확인 | 끝 |
| `1` | **목록을 못 읽었습니다** — 지워졌는지 아닌지 모름 | 권한·로그인부터. 다시 돌리세요 |
| `3` | 읽었고, 남은 것이 있음 | 출력된 이름을 포털에서 확인 |

**`1` 이 `3` 보다 무겁습니다.** 확인 못 한 삭제는 삭제가 아닙니다.

손으로 하려면 같은 순서입니다 — 읽고, 지우고, purge:

```bash
az keyvault list -g "$FFSFT_RESOURCE_GROUP" --query "[].name" -o tsv   # 먼저 이름을 확보
az group delete -n "$FFSFT_RESOURCE_GROUP" --yes
az keyvault purge -n <위에서 읽은 이름>                                 # 90일을 안 기다리려면
```
