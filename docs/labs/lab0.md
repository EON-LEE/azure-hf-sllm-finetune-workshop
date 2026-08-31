# Lab 0 — 환경 준비

> **모든 트랙의 선행 Lab 입니다.**

## 목표

- 로컬에서 테스트 1232개가 통과한다
- `az` 가 **의도한 계정·구독**을 보고 있고, 그 설정이 **다음 셸에서도 살아 있다**
- 이 리전에서 **GPU 를 실제로 빌릴 수 있는지** 돈을 쓰기 전에 안다
- Lab 2 가 요구하는 학습 클러스터 `gpu-a100-lp` 가 실재한다
- **이 워크샵이 만드는 모든 것이 리소스 그룹 하나에 들어 있다** — 마지막 날
  `ffsft infra down` 한 줄로 전부 내릴 수 있게

## 선행조건

- **Azure 구독에서 리소스 그룹을 만들고 지울 수 있는 권한** — 구독 스코프 `Contributor`
  이상. 이 워크샵의 정리 절차가 `az group delete` 라서, 그룹을 만들 수 없으면 지울 수도
  없습니다
- **롤 할당을 만들 수 있는 권한** — `Owner` 또는 `User Access Administrator`.
  Lab 5 의 엔드포인트가 자기 관리 ID 로 ACR 에서 이미지를 당기려면 `AcrPull` 을
  **부여**해야 하고, 그건 `Microsoft.Authorization/roleAssignments/write` 입니다
  (`src/ffsft/deploy/identity.py:635`). `Contributor` 만으로는 이 한 줄에서 막히는데,
  증상이 배포 실패로 나와서 원인을 찾기 어렵습니다 — **먼저 확인하세요**
- `uv` 설치 (`~/.local/bin` 은 기본 PATH 에 없습니다)
- Python 3.10 이상

> **권한 확인:**
> ```bash
> az role assignment list --assignee $(az ad signed-in-user show --query id -o tsv) \
>    --include-inherited --query "[].roleDefinitionName" -o tsv | sort -u
> ```
> `Owner` 가 있으면 둘 다 됩니다. `Contributor` 뿐이면 Lab 5 전에 관리자에게
> `User Access Administrator` 를 요청하세요.

## 소요·비용

**30분 / GPU 과금 $0.**

이 Lab 이 남기는 것은 워크스페이스와 학습 클러스터 `gpu-a100-lp` 뿐입니다.
클러스터는 `min_instances=0` 으로 만들어지고 유휴 15분이면 스케일다운되므로
**노드가 0개인 동안 시간당 $0** 입니다 (§6 실측: AmlCompute `min_instances=0` = 무과금).
5번의 `check --probe` 가 만드는 것도 min=0 이고 곧바로 지웁니다.

돈이 나가기 시작하는 지점은 여기가 아니라 **Lab 2 의 학습 잡**(노드가 뜨는 순간)과
**Lab 5 의 관리형 온라인 엔드포인트**(유휴에도 24시간 과금)입니다.

---

## 1. 로컬 (Azure 없이)

```bash
export PATH="$HOME/.local/bin:$PATH"
cd fabric-foundry-sllm-finetune
uv sync --extra dev --extra train --extra azure
uv run pytest
```

기대 출력:

```
1232 passed, 2 skipped, 1 xfailed, 472 warnings in 9.41s
```

- 스킵 2개는 `ffsft` 와 `ffsft-plot` 이 **자기 로깅을 설정하지 않는다**는 것을 확인하는
  테스트입니다. 실패가 아니라 그렇게 되어 있어야 정상입니다.
- 경고 수백 줄은 `azure-ai-ml` 이 marshmallow 에 대해 내는 것입니다. 무시하세요.

테스트는 **네트워크도 Azure 도 건드리지 않습니다.** 여기서 실패하면 Azure 문제가 아닙니다.

> **왜 extra 를 세 개 까나.** 테스트만 돌릴 거면 `--extra dev` 하나로 충분합니다.
> 하지만 이 Lab 의 5번이 `probe_architecture.py`(→ `transformers` = **`train`**) 와
> `ffsft deploy check --probe`(→ `azure.ai.ml` = **`azure`**) 를 씁니다.
> `dev` 만 깔면 Lab 0 이 자기 마지막 절을 못 돌립니다.
>
> extra 가 쪼개져 있는 이유는 **CPU 쪽 절반이 CUDA 없이 깔려야 하기 때문**입니다.
> torch 를 끌고 오는 것은 `train` 하나뿐이라 데이터·서빙·테스트만 하는 사람은 그걸
> 안 받습니다. 대신 `--extra train` 을 넣는 순간 이 `uv sync` 가 이 Lab 에서 제일
> 오래 걸리는 명령이 됩니다 — GPU 가 없는 노트북에서도 설치 자체는 됩니다.

```bash
uv run ffsft models list --commercial-only     # 상용 가능한 17개
uv run ffsft models show qwen3.8-27b
```

### `ffsft --help` 이 이 워크샵의 지도입니다

```bash
uv run ffsft --help
```

Lab 순서대로 나옵니다 — `models`/`serving`/`bench`(Lab 0) → `train`(2) →
`eval`(3) → `deploy`(5) → `lifecycle`(5,7) → `loadtest`/`plot`(6) → `merge`(8),
마지막이 `serve-local`(GPU 도 Azure 도 안 씀).

- 서브커맨드는 **기존 console script 를 그대로 부릅니다.** `ffsft loadtest` 와
  `ffsft-loadtest` 는 같은 코드입니다. **Lab 문서는 `ffsft <cmd>` 하나로만 씁니다** —
  외울 이름이 하나면 충분합니다.
- `ffsft <cmd> --help` 는 **위임 대상의 도움말**을 보여줍니다.
- 무거운 의존성은 함수 안에서만 import 합니다 — `--extra dev` 만 깔린 노트북에서도
  `ffsft models list` 는 돕니다. 필요한 extra 가 없으면 **설치 명령을 알려줍니다.**

## 2. 로그인 — 프로필을 격리하고, 그걸 파일로 남기세요

여러 디렉터리에 로그인된 워크스테이션에서는 `az` 의 활성 계정이
**실행 도중에 바뀝니다** (§39: 한 세션에 두 번 드리프트). 이 워크샵 전용 프로필로
격리하는 게 가장 싼 예방입니다.

`AZURE_CONFIG_DIR` 은 **`az login` 보다 먼저** 있어야 합니다. 나중에 내보내면
로그인은 이미 전역 `~/.azure` 에 쓰인 뒤입니다.

```bash
cat > ~/.ffsft-env <<'EOF'
export PATH="$HOME/.local/bin:$PATH"
export AZURE_CONFIG_DIR=~/.azure-ffsft        # 전역 ~/.azure 와 분리
EOF
source ~/.ffsft-env

az login                                       # 또는 az login --use-device-code
export FFSFT_SUBSCRIPTION_ID=<your-subscription-id>
az account set --subscription $FFSFT_SUBSCRIPTION_ID
az account show --query "{user:user.name, sub:id, tenant:tenantId}" -o table
```

출력의 `user` 가 **의도한 계정인지 눈으로 확인하세요.** 아니면 나머지 Lab 이
전부 엉뚱한 구독에 리소스를 만듭니다.

확인했으면 같은 파일에 굳힙니다:

```bash
cat >> ~/.ffsft-env <<EOF
export FFSFT_SUBSCRIPTION_ID=$FFSFT_SUBSCRIPTION_ID
export FFSFT_TENANT_ID=$(az account show --query tenantId -o tsv)
EOF
```

> 첫 heredoc 은 `<<'EOF'`(따옴표 있음), 두 번째는 `<<EOF`(없음)입니다. 앞은
> `$HOME` 을 **글자 그대로** 남겨야 어느 계정에서 열어도 맞고, 뒤는 **지금 확인한
> 값**이 박혀야 하므로 확장시킵니다. 바꿔 쓰면 파일에 `$FFSFT_SUBSCRIPTION_ID`
> 라는 글자가 남고, 다음 셸에서 빈 값이 됩니다.

> ⚠️ **모든 Lab 의 첫 줄은 `source ~/.ffsft-env` 입니다.** Lab 0 부터 Lab 8 까지
> 파일 하나, 셸 하나입니다 — 프로필을 바꾸는 Lab 은 없습니다. 나머지 변수는 아래
> 4번의 `ffsft infra up` 이 같은 파일에 이어 씁니다.
> 새 터미널·재부팅·다음 날 이어 하기가 전부 여기서 갈립니다. 이 줄 없이 새 셸에서
> `az` 를 부르면 `AZURE_CONFIG_DIR` 이 없으므로 **전역 프로필로 조용히 돌아갑니다** —
> 오류가 아니라 *다른 계정으로 성공하는* 쪽이라 알아채기 어렵습니다.
>
> 자격증명은 이 파일에 없습니다. 토큰은 `$AZURE_CONFIG_DIR` 안에 있고, 이 파일은
> 리포지터리 밖(`~`)이라 커밋될 일이 없습니다.

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

### 합격 기준은 트랙마다 다릅니다 — 여기서 갈립니다

위 (b) 의 `restrictions` 는 **dedicated 를 살 수 있나**를 묻습니다. 학습 클러스터는
LowPriority 라 이 검사가 막혀도 만들어집니다. 실제로 koreacentral 이 그 모양입니다 —
A100 이 dedicated 로는 `BLOCKED (Location)` 인데 **LowPriority 로는 뜹니다**
(§57.1, [Lab 5 §1](lab5.md)). 그래서 한 리전이 두 트랙에 같은 답을 주지 않습니다.

| 트랙 | 이 리전에서 사야 하는 것 | 합격 기준 |
|---|---|---|
| Track A (0→1→2→3) | 학습 클러스터 = **LowPriority** | 4번에서 `gpu-a100-lp` 가 생성됨. `restrictions` 가 막혀 있어도 됩니다 |
| Track B (0→4→5→6→7) | 관리형 온라인 엔드포인트 = **dedicated 전용** | `restrictions: []` **그리고** `supportedComputeTypes` 에 `MIR` |
| 풀사이클 (0→…→8→7) | 둘 다 | 한 리전이 둘 다 통과하면 하나로 끝. 아니면 **리전이 두 개**가 됩니다 |

> ⚠️ **Track B 만 할 거면 LowPriority 가용성으로 리전을 고르지 마세요.**
> 관리형 온라인 엔드포인트에 LowPriority 옵션은 **아예 없습니다**
> ([Lab 5 §1](lab5.md)). LowPriority A100 만 되는 리전을 골라 4번까지 마치면,
> **Lab 5 에 가서야** 그 리전에는 배포할 수 없다는 걸 알게 됩니다 — 그때는 워크스페이스를
> 다른 리전에 다시 만드는 것 말고 할 수 있는 게 없습니다. Track B 의 통과 조건은
> 위 표의 두 번째 줄, `restrictions: []` + `MIR` 하나뿐입니다.

### 이 워크샵의 리전은 **하나**입니다 — 엄격한 쪽 기준으로 고르세요

**위 표 두 번째 줄(`restrictions: []` + `MIR`)을 통과하는 리전 하나를 고르세요.** 세
트랙 중 조건이 제일 센 줄이고, 그 리전은 학습도 됩니다 — dedicated A100 을 파는 리전이
LowPriority 를 거절한 적은 이 리포 실측에 없습니다. 그리고 그건 믿고 넘어가는 게 아니라
4번의 `--dry-run` 이 **돈 쓰기 전에 실제로 확인**합니다.

**한 리전으로 가는 이유는 내릴 때 드러납니다.** 리전이 둘이면 리소스 그룹도 둘이고,
그러면 `ffsft infra down` 한 번이 "다 내렸다"를 뜻하지 못합니다. 이 리포가 실제로 그
상태였습니다 — 학습은 koreacentral 그룹, 서빙은 polandcentral 그룹, 그리고 polandcentral
엔드포인트가 koreacentral 의 ACR 을 가리켰습니다 (`src/ffsft/deploy/identity.py:411`).
어느 쪽을 지워도 다른 쪽이 깨지니 결국 **아무것도 못 지웠습니다.**

> ⚠️ **두 줄 다 통과하는 리전이 이 구독에 없다면, 풀사이클(Lab 8)은 한 그룹에서 못 합니다.**
> 그때 할 일은 트랙을 **하나만** 고르는 것입니다 — Track A 도 Track B 도 각각은 한 리전
> · 한 그룹으로 끝까지 갑니다. 그래도 풀사이클을 하고 싶으면 prefix 를 둘 만들어
> (`--prefix kim01` / `--prefix kim01p`) 그룹을 둘 두고, 내릴 때도 `infra down` 을
> **두 번** 부르세요. 그리고 [Lab 8](lab8.md) 의 교차 참조 함정을 먼저 읽으세요 —
> 위 문단이 그 함정의 청구서입니다.

여기서 정한 리전이 4번에서 `--location` 으로 들어갑니다.

## 4. 워크스페이스 + 학습 클러스터

Lab 2 는 클러스터 `gpu-a100-lp` 가 있다고 전제합니다. **그걸 만드는 것은 이 절뿐입니다.**

**이름을 하나만 정합니다: 여러분의 prefix.** 나머지 이름은 전부 거기서 나옵니다.

```bash
uv run ffsft infra up --prefix <본인> --location <region>
```

- `<본인>` — 영소문자·숫자 3~8자, 글자로 시작 (`eonlee`, `kim01`). 리소스 이름이 여기서
  조립되므로 Azure 가 거절할 값은 배포 4분 뒤가 아니라 **즉시** 거절합니다
- `<region>` — 3번에서 고른 **그 리전 하나**. 학습도 서빙도 여기서 합니다

이 한 줄이 `infra/main.bicep` 을 구독 스코프로 배포해서 **리소스 그룹 하나**를 만들고,
그 안에 워크스페이스·스토리지·Key Vault·Application Insights·Log Analytics·ACR 을
넣습니다. 그리고 나머지 Lab 이 읽을 값을 `~/.ffsft-env` 에 **병합**합니다 — 2번에서
쓴 `PATH`·`AZURE_CONFIG_DIR`·`FFSFT_TENANT_ID` 줄은 그대로 두고 아래 넷만 씁니다.

```
export FFSFT_SUBSCRIPTION_ID=<구독 id>
export FFSFT_RESOURCE_GROUP=rg-ffsft-<본인>
export FFSFT_WORKSPACE=mlw-<본인>
export FFSFT_LOCATION=<region>
export FFSFT_ACR=acr<본인><해시>
```

```bash
source ~/.ffsft-env
```

> 💡 **왜 그룹이 하나인가 — 이건 취향이 아니라 청구서에서 배운 것입니다.**
> 이 리포는 한때 학습을 koreacentral 그룹에, 서빙을 polandcentral 그룹에 두었습니다.
> 그러다 polandcentral 엔드포인트가 `rg-ffsft-kc` 에 있는 ACR 을 가리켰고
> (`src/ffsft/deploy/identity.py:411`), 그 뒤로 **"다 껐나?" 라는 질문에 답할 수 없게**
> 됐습니다. 어느 쪽 그룹을 지워도 다른 쪽이 깨지니까요.
> 리소스 그룹은 Azure 의 **청구 경계이자 삭제 단위**입니다. 하나면
> `ffsft infra down` 이 전부를 뜻하고, 둘이면 아무것도 뜻하지 않습니다.

> ⚠️ **이미 워크스페이스가 있어도 이 명령을 쓰세요.** ARM 배포는 upsert 라
> 여러 번 돌려도 안전합니다. 반대로 이 절을 건너뛰고 손으로 만든 이름을 쓰면,
> `--resource-group`/`--workspace`/`--location` 의 기본값이 `rg-ffsft-kc` /
> `mlw-ffsft` / `koreacentral` — **이 리포 저자들의 리소스 이름**이라
> (`scripts/provision_azure.py:33-35`) 빈 변수 하나가 여러분을 남의 워크스페이스로
> 조용히 보냅니다.

### 4.1 학습 클러스터

`infra up` 은 GPU 클러스터를 만들지 않습니다 — 일부러입니다. 클러스터는 SKU 가
이 구독·이 리전에서 실제로 빌려지는지에 달려 있고, 그 판정은 Bicep 이 아니라
`provision_azure.py` 의 sizing/eligibility 가드가 합니다. ARM 배포 실패 몇 분이 아니라
1초짜리 `FAIL` 로 알기 위해서입니다.

**먼저 `--dry-run`** — 가드만 돌고 아무것도 안 만듭니다.

```bash
uv run python scripts/provision_azure.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --dry-run
```

기대 출력:

```
sizing check : OK -- qlora of qwen3.8-27b needs ~28 GB, Standard_NC24ads_A100_v4 provides 80 GB across 1 GPU(s) -- 52 GB headroom
quota needed : 24 of the pooled TotalLowPriorityCores in <region>

dry run, nothing created
```

`FAIL` 이면 여기서 멈추세요 — 대신 쓸 수 있는 SKU 목록을 같이 찍어줍니다.

`OK` 면 **`--dry-run` 만 빼고 그대로 다시** 돌립니다. 이번엔 진짜 만듭니다. 이 명령도
같은 변수들을 읽으므로, **그 사이 셸을 새로 열었으면 `source ~/.ffsft-env` 부터**입니다:

```bash
uv run python scripts/provision_azure.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION
```

기대 출력 (끝의 두 줄):

```
compute      : creating gpu-a100-lp (Standard_NC24ads_A100_v4, min=0 max=1) ...
DONE. The cluster scales to zero when idle, but verify in the portal.
```

- 기본값이 그대로 Lab 2 가 기대하는 값입니다: `--compute-name gpu-a100-lp`,
  `--priority LowPriority`, `--max-nodes 1`, SKU 는 레지스트리의 `recommended_sku`.
  다른 모델로 갈 거면 `--model` 만 바꾸세요 — SKU 는 따라옵니다.
- **만들어 두고 하루 뒤에 와도 됩니다.** `min_instances=0` + 유휴 15분 스케일다운이라
  노드가 0개인 클러스터는 과금되지 않습니다 (§6 실측).

> ⚠️ **다 썼다고 클러스터를 지우지 마세요.** AmlCompute 는 만들 때마다 **새 시스템
> 할당 identity** 를 받고, 이전 클러스터의 롤 할당은 **하나도 따라오지 않습니다.**
> 지우고 다시 만들면 첫 잡이 75초 만에
> `401 authentication required` (이미지 pull) 로 죽습니다 (§31.3, §60.2).
> `provision_azure.py` 는 호출할 때마다 새 principal 에 데이터 롤을 다시 부여하지만,
> 포털에서 손으로 지우고 손으로 만들면 그 경로를 안 탑니다.
> **정리는 삭제가 아니라 0으로 스케일다운입니다** — 클러스터 정의는 공짜입니다.

> 💡 **Track B 만 할 거면 `gpu-a100-lp` 를 쓰지 않습니다.** Lab 4~6 은 학습 잡을 하나도
> 안 냅니다. 그렇다고 따로 할 일은 없습니다 — `provision_azure.py` 에 클러스터만 건너뛰는
> 플래그가 없고, `min_instances=0` 이라 만들어 둔 채로도 시간당 $0 입니다 (§6 실측).
> 나중에 풀사이클로 넘어가면 [Lab 8](lab8.md) 의 병합 잡이 이 클러스터를 씁니다.

### 4.2 셸이 **여러분의** 그룹을 보고 있는지

`infra up` 이 값을 파일에 썼고 `source` 로 셸에 실었습니다. 실렸는지는 봐야 압니다:

```bash
printf 'rg=[%s]\nws=[%s]\nloc=[%s]\nacr=[%s]\n' \
  "$FFSFT_RESOURCE_GROUP" "$FFSFT_WORKSPACE" "$FFSFT_LOCATION" "$FFSFT_ACR"
```

기대 출력 (`<본인>` 은 4번에서 정한 prefix):

```
rg=[rg-ffsft-<본인>]
ws=[mlw-<본인>]
loc=[<region>]
acr=[acr<본인><해시>]
```

**네 줄 다 여러분의 prefix 가 들어 있어야 합니다.** `[]` 로 비어 있으면 `source
~/.ffsft-env` 를 안 한 것이고, `rg-ffsft-kc` 처럼 **정한 적 없는 이름**이 보이면 이
리포 저자들의 리소스를 가리키고 있는 것입니다 (`scripts/provision_azure.py:33-35` 의
기본값). 둘 다 지금 멈추면 `source` 한 번이지만, 여기를 지나면 **다음 Lab 들이 전부
그 값을 읽습니다.**

이제 셸을 열 때마다 그걸 스스로 말하게 합니다. 파일에 **한 번만** 답니다:

```bash
grep -q 'profile: ffsft' ~/.ffsft-env || cat >> ~/.ffsft-env <<'EOF'
echo "profile: ffsft  rg=$FFSFT_RESOURCE_GROUP  ws=$FFSFT_WORKSPACE  loc=$FFSFT_LOCATION"
EOF

source ~/.ffsft-env
```

기대 출력:

```
profile: ffsft  rg=rg-ffsft-<본인>  ws=mlw-<본인>  loc=<region>
```

> **heredoc 은 `<<'EOF'`(따옴표 있음)이어야 합니다.** 배너의 `$FFSFT_*` 는 **글자
> 그대로 남아야** 파일을 읽을 때마다 *그 순간 실린 값*을 찍습니다. `<<EOF` 로 쓰면
> 오늘 값이 배너에 구워져서, 나중에 값이 바뀌어도 **영원히 같은 소리를 합니다** — 즉
> 확인용으로 만든 줄이 제일 먼저 거짓말을 하게 됩니다.
>
> 이 한 줄이 필요한 이유는 `ffsft lifecycle status`·`down` 이 **워크스페이스를 플래그로
> 못 받기** 때문입니다 — `status --help` 는 `[-h]` 가 전부고, 대상은 오직 환경변수로
> 정해집니다 (`AzureTarget.from_env`).
>
> **화면이 그걸 안 적는다는 뜻은 아닙니다.** `status` 는 표 위에
> `LOOKED IN: workspace … / resource group … / subscription …` 헤더를 먼저 찍습니다
> (§73.3). 틀린 셸에서 돌렸는지는 그 줄로 압니다. 다만 그건 **조회가 끝난 뒤에** 나오는
> 줄이라 사후 진단이고, 지우는 쪽은 그 진단조차 없습니다: `down` 은 지울 게 있으면
> 헤더를 아예 안 찍습니다 (`format_inventory` 를 안 부르고 `will remove:` 다음이 바로
> 삭제입니다. 헤더가 나오는 건 지울 게 없거나 조회가 실패했을 때뿐입니다).
>
> 배너는 명령을 치기 **전**, 파일을 `source` 하는 순간에 찍힙니다. 사후에 진단되는
> 셸 대신 **처음부터 맞는 셸**을 만드는 것이 이 파일이 하는 일입니다.

## 5. 돈을 쓰기 전 마지막 무료 점검

```bash
uv run python scripts/verify_hf_ids.py                            # 모든 hf_id 를 HF API 로
uv run python scripts/probe_architecture.py qwen3.8-27b --check   # 선언 vs 실제 LoRA 타깃
uv run ffsft deploy check --probe                                 # 실제 create 호출, min=0, 즉시 삭제
```

셋 다 불일치면 non-zero 로 끝납니다. `probe_architecture.py` 는 meta 디바이스를 쓰므로
**가중치를 안 받습니다.**

`check --probe` 는 진짜 create 를 호출하고 **곧바로 지웁니다** — 권한·SKU·이미지 pull 을
한 번에 검사하는 가장 싼 방법입니다. 거절당하면 아무것도 안 만들어집니다.

### 5.1 Track A: 잡 결과를 읽을 수 있는지 — 절반은 지금, 절반은 Lab 2 §1

이 워크스페이스에서 **잡 stdout 은 안 읽힙니다.** Azure ML 은
`user_logs/std_log.txt` 를 워크스페이스 기본 blob 에 쓰고 SAS URL 로 내주는데,
스토리지가 네트워크 격리면 그 URL 이 VNet 밖에서 `AuthorizationFailure` 를 돌려줍니다 —
**잡은 정상이고 결과만 안 보입니다** (§19.1). 뚫려 있는 채널은 MLflow 뿐이고,
[Lab 3](lab3.md) 이 델타를 읽는 곳이 바로 거기입니다.

지금 공짜로 확인할 수 있는 절반은 **토큰**입니다 — `watch_jobs.sh:34` 가 폴링마다
하는 것과 같은 호출입니다:

```bash
[ -n "$(az account get-access-token --resource https://ml.azure.com \
        --query accessToken -o tsv 2>/dev/null)" ] \
  && echo "mlflow token OK" || echo "mlflow token FAILED"
```

```
mlflow token OK
```

**나머지 절반은 잡이 하나 있어야 답이 나오고, Lab 0 에는 잡이 없습니다.** 메트릭이
실제로 도착하는지는 런이 있어야만 물어볼 수 있습니다. 그래서 그 확인은 워크샵에서
**제일 싼 잡**에 붙입니다 — [Lab 2 §1 의 프리플라이트](lab2.md)(몇 분). 자기검사 결과를
`publish()` 가 **MLflow 로** 올리므로, 그 잡이 곧 채널 점검입니다.

> ⚠️ **프리플라이트를 제출하면 그 자리에서 `watch_jobs.sh` 를 붙이세요.**
> ```bash
> uv run bash scripts/watch_jobs.sh PREFLIGHT:<run-name>
> ```
> `preflight.*` 메트릭이 **한 줄이라도** 뜨면 Lab 3 이 읽을 채널이 살아 있는 것입니다.
> 상태만 바뀌고 `preflight.*` 가 끝까지 안 오면 **거기서 멈추세요** — 그 상태로
> [Lab 2 §4](lab2.md) 의 42분·약 $1.5 짜리 잡을 내면, 델타는 만들어지지만
> 읽을 방법이 없습니다. → [GOTCHAS #9](../GOTCHAS.md#9)

---

## 기대 최종 상태

```bash
source ~/.ffsft-env            # 새 셸이면 이 줄부터
uv run ffsft lifecycle status
```

> **헤더부터 읽으세요 — 이 명령을 처음 치는 자리입니다.** 표 **위에** 오는
> `LOOKED IN: workspace … / resource group … / subscription …` 여섯 줄이 **이 화면이
> 어느 워크스페이스 얘기인지**를 적는 줄입니다 (§73.3, `format_inventory` →
> `scope_lines`). 거기 적힌 워크스페이스·리소스그룹이 위 4번에서 정한 값과 다르면,
> 아래 `BILLING NOW` 는 **여러분 워크스페이스 얘기가 아닙니다.**
>
> 아래는 **실측 전체 블록**입니다 — [`PERFORMANCE.md §13`](../PERFORMANCE.md) 에서
> 잘라 왔습니다 (기대 출력은 실측에서 잘라 오는 것이지 지어내는 것이 아닙니다).
> 이름 `mlw-ffsft` / `rg-ffsft-kc` 와 가려 둔 구독 id 는 **저자들 값**이고, 여러분
> 화면에는 **여러분 값과 실제 GUID** 가 찍힙니다. 첫 줄이 비어 있는 것이 정상이고,
> `LOOKED IN` 줄이 안 보이면 구판 코드입니다.

```

LOOKED IN: workspace mlw-ffsft   resource group rg-ffsft-kc
           subscription <your-subscription-id>
           that triple is what get_ml_client sends, and it scopes every row that came back
           through it. LEFTOVERS does not: it is a separate ARM scan of resource group rg-ffsft-kc,
           same subscription, no workspace. FFSFT_LOCATION=koreacentral is sent by neither, so it
           does not scope this read and cannot explain a missing resource.

KIND                 NAME                               SKU                            $/hr  NOTE
------------------------------------------------------------------------------------------------------------------------------------
  compute-cluster    gpu-a100-lp                        Standard_NC24ads_A100_v4          -  min_instances=0 (low_priority): idle costs nothing
------------------------------------------------------------------------------------------------------------------------------------
BILLING NOW: nothing. No always-on compute in this workspace.
```

클러스터는 목록에 뜨지만 `min_instances=0 (low_priority): idle costs nothing` 이 붙어
과금 대상이 아닙니다. 여기서 클러스터가 `always-on charge` 로 나오면 `min_instances`
가 0 이 아닌 것이니 Lab 2 로 넘어가기 전에 고치세요.

- `~/.ffsft-env` 에 `export` 8줄 (`PATH`, `AZURE_CONFIG_DIR`, `FFSFT_SUBSCRIPTION_ID`,
  `FFSFT_TENANT_ID`, `FFSFT_LOCATION`, `FFSFT_RESOURCE_GROUP`, `FFSFT_WORKSPACE`,
  `FFSFT_ACR`) **+ 맨 끝에 `profile: ffsft …` 배너 한 줄**. 파일이 `source` 될 때마다
  **지금 어느 그룹에 서 있는지**를 찍는 줄입니다 (위 4.2)
- 클러스터 `gpu-a100-lp`, 노드 0개 (Track B 만 할 거면 안 씁니다 — 위 4번)

## 막히면

| 증상 | 항목 |
|---|---|
| `InvalidAuthenticationTokenTenant` | [#1](../GOTCHAS.md#1) |
| 쿼터는 있는데 `NotAvailableForSubscription` | [#2](../GOTCHAS.md#2) |
| 어느 리전에서도 A100 이 안 잡힌다 | [#3](../GOTCHAS.md#3) |
| `not a supported VM size` | [#2](../GOTCHAS.md#2) — MIR 전용 SKU 일 수 있습니다 |
| 새 셸에서 `az` 가 딴 계정을 본다 | `source ~/.ffsft-env` 를 안 했습니다 — 위 2번 |

## 정리

지울 것이 없습니다. 클러스터는 **남겨두고** 노드만 0인지 확인하세요 (위 4번의 경고).
`~/.ffsft-env` 도 남깁니다 — 나머지 Lab 이 그 파일을 전제합니다.

**다음**: Track A → [Lab 1](lab1.md) · Track B → [Lab 4](lab4.md)
