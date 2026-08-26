# Lab 0 — 환경 준비

> **모든 트랙의 선행 Lab 입니다.**

## 목표

- 로컬에서 테스트 680개가 통과한다
- `az` 가 **의도한 계정·구독**을 보고 있다
- 이 리전에서 **GPU 를 실제로 빌릴 수 있는지** 돈을 쓰기 전에 안다

## 선행조건

- Azure 구독 + Foundry User / Foundry Owner 롤
- `uv` 설치 (`~/.local/bin` 은 기본 PATH 에 없습니다)
- Python 3.10 이상

## 소요·비용

**30분 / GPU 과금 없음.** 이 Lab 은 아무것도 만들지 않거나, 만들어도 즉시 지웁니다.

---

## 1. 로컬 (Azure 없이)

```bash
export PATH="$HOME/.local/bin:$PATH"
cd fabric-foundry-sllm-finetune
uv sync --extra dev
uv run pytest -q
```

기대 출력:

```
680 passed in 8.34s
```

테스트는 **네트워크도 Azure 도 건드리지 않습니다.** 여기서 실패하면 Azure 문제가 아닙니다.

```bash
uv run ffsft models list --commercial-only     # 상용 가능한 17개
uv run ffsft models show qwen3.8-27b
```

## 2. 로그인 — 프로필을 격리하세요

여러 디렉터리에 로그인된 워크스테이션에서는 `az` 의 활성 계정이
**실행 도중에 바뀝니다.** 이 워크샵 전용 프로필로 격리하는 게 가장 싼 예방입니다.

```bash
export AZURE_CONFIG_DIR=~/.azure-ffsft        # 전역 ~/.azure 와 분리
az login                                       # 또는 az login --use-device-code

export FFSFT_SUBSCRIPTION_ID=<your-subscription-id>
az account set --subscription $FFSFT_SUBSCRIPTION_ID
az account show --query "{user:user.name, sub:id, tenant:tenantId}" -o table
```

출력의 `user` 가 **의도한 계정인지 눈으로 확인하세요.** 아니면 나머지 Lab 이
전부 엉뚱한 구독에 리소스를 만듭니다.

```bash
export FFSFT_TENANT_ID=<위에서 나온 tenant>
```

> 💡 `InvalidAuthenticationTokenTenant` 는 로그인 만료도, 권한 부족도, 코드 버그도
> 아닙니다. → [GOTCHAS #1](../GOTCHAS.md#1)

## 3. 이 리전에서 GPU 를 빌릴 수 있나 — 쿼터만 보면 틀립니다

세 가지를 **각각** 확인해야 합니다.

```bash
export FFSFT_LOCATION=<region>     # 예: polandcentral

# (a) 쿼터 — 얼마까지
az vm list-usage --location $FFSFT_LOCATION -o table | grep -i "NC.*A100\|NV.*A10"

# (b) restrictions — 여기서 쓸 수 있나
az vm list-skus --location $FFSFT_LOCATION --size Standard_NC24ads_A100_v4 \
   --query "[].{name:name, restrictions:restrictions[].type}" -o json

# (c) supportedComputeTypes — 무엇으로 쓸 수 있나 (AmlCompute / ComputeInstance / MIR)
az rest --method get --url \
 "https://management.azure.com/subscriptions/$FFSFT_SUBSCRIPTION_ID/providers/Microsoft.MachineLearningServices/locations/$FFSFT_LOCATION/vmSizes?api-version=2024-04-01" \
 --query "value[?name=='Standard_NC24ads_A100_v4'].{n:name,t:supportedComputeTypes}" -o json
```

기대 출력 (polandcentral 실측):

```
restrictions : []                                   <- 비어 있어야 합니다
supportedComputeTypes : [AmlCompute, ComputeInstance, MIR]
```

`restrictions` 에 `Location` 이나 `Zone` 이 있으면 **쿼터가 아무리 많아도 안 뜹니다.**
→ [GOTCHAS #2](../GOTCHAS.md#2), [#3](../GOTCHAS.md#3)

**koreacentral 에서는 A100 이 `BLOCKED (Location)` 입니다** (§57 실측).
막히면 리전을 바꾸는 게 코드를 고치는 것보다 빠릅니다.

## 4. 워크스페이스 준비

이미 있으면 환경변수만:

```bash
export FFSFT_RESOURCE_GROUP=<rg>
export FFSFT_WORKSPACE=<workspace>
```

없으면 만듭니다 (`--dry-run` 으로 가드만 먼저 돌려보세요):

```bash
uv run python scripts/provision_azure.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --dry-run
```

## 5. 돈을 쓰기 전 마지막 무료 점검

```bash
uv run python scripts/verify_hf_ids.py                            # 모든 hf_id 를 HF API 로
uv run python scripts/probe_architecture.py qwen3.8-27b --check   # 선언 vs 실제 LoRA 타깃
uv run ffsft-deploy check --probe                                 # 실제 create 호출, min=0, 즉시 삭제
```

셋 다 불일치면 non-zero 로 끝납니다. `probe_architecture.py` 는 meta 디바이스를 쓰므로
**가중치를 안 받습니다.**

`check --probe` 는 진짜 create 를 호출하고 **곧바로 지웁니다** — 권한·SKU·이미지 pull 을
한 번에 검사하는 가장 싼 방법입니다.

---

## 기대 최종 상태

```bash
uv run ffsft-lifecycle status
# -> BILLING NOW: nothing
```

## 막히면

| 증상 | 항목 |
|---|---|
| `InvalidAuthenticationTokenTenant` | [#1](../GOTCHAS.md#1) |
| 쿼터는 있는데 `NotAvailableForSubscription` | [#2](../GOTCHAS.md#2) |
| 어느 리전에서도 A100 이 안 잡힌다 | [#3](../GOTCHAS.md#3) |
| `not a supported VM size` | [#2](../GOTCHAS.md#2) — MIR 전용 SKU 일 수 있습니다 |

## 정리

이 Lab 은 남기는 게 없습니다. `provision_azure.py` 로 클러스터를 만들었다면
`min_nodes=0` 인지 확인하세요 — 0 이면 유휴 시 과금되지 않습니다.

**다음**: Track A → [Lab 1](lab1.md) · Track B → [Lab 4](lab4.md)
