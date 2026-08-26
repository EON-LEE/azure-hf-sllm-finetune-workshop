# 검증 기록 (Verified Findings)

이 문서는 **실제로 실행해서 확인한 것만** 기록한다. 추정·문서·블로그 근거는
`docs/PLAN.md`에 있고, 여기에는 라이브 Azure 구독과 라이브 Hugging Face에 대해
직접 호출한 결과만 남긴다.

- 구독: `ME-MngEnvMCAP277524-eonlee-1` (`cb370f4f-…`), 테넌트 `4510ec63-…`
- 검증일: **2026-08-20**
- 재현 도구: `scripts/probe_architecture.py`, `scripts/verify_hf_ids.py`

---

## 0. ⛔ 가장 중요한 결론 — 실패 원인은 스토리지가 아니라 **엔드포인트 자신의 권한**이었다

> **정정 기록.** 이 절은 원래 "워크스페이스 스토리지 계정이 접근 불가라서
> 온라인 배포가 불가능하다"고 단정했다. **그 결론은 틀렸다.** 사용자가
> "모델 웨이트나 코드가 블롭에 있으면 되는 거 아니냐"고 되물어서 다시 확인했고,
> 그 과정에서 뒤집혔다. 틀린 진단을 지우지 않고 남기는 이유는 §0.4에 있다.

**두 번의 배포(`ffsft-smoke`, `ffsft-smoke2`)가 모두 실패했고 원인은 같다.**
모델도, 이미지도, 프로브도, 스토리지도 아니었다. **온라인 엔드포인트에 붙는
시스템 할당 관리 ID가 이미지를 pull 할 권한이 없었다.**

갓 만든 엔드포인트 셸에서 직접 측정한 값(배포를 붙이지 않으면 컴퓨트 요금 0):

```
endpoint ffsft-acrtest  MI = 87ec28b5-bcc6-43f7-abd8-617abdfc13e6
  roles on acrffsftkc            -> NONE      ← 9.15 GB 이미지를 읽을 권한 없음
  roles on mlwffsftstorage8cb...  -> NONE
workspace mlw-ffsft
  properties.containerRegistry   -> ""        ← 연결된 ACR이 없다
```

마지막 줄이 핵심이다. Azure는 **워크스페이스에 연결된 ACR**에 대해서만 엔드포인트
ID에 pull 권한을 자동으로 준다. 이 워크스페이스는 연결된 ACR이 없으므로
`acrffsftkc`는 **고객 소유 레지스트리**이고, 아무도 권한을 주지 않는다.

### 0.1 모든 증상이 이 사실 하나에서 나온다

| 관측 | 설명 |
|---|---|
| `Creating` 상태로 68분+ | 이미지 pull 재시도 루프 |
| 컨테이너 로그 없음 | 컨테이너가 **생성조차 되지 않았다** |
| App Insights `traces` 비어 있음 | 실행된 코드가 0줄 |
| 엔드포인트는 `Succeeded` | 컨트롤 플레인은 정상 (ID 발급까지만 함) |
| 최종 `InternalServerError` | 플랫폼 타임아웃 시 범용 코드 |

**느린 실패가 자기 원인을 가린다.** 그래서 같은 원인에 두 번 당했다.

### 0.2 왜 스토리지 진단이 틀렸나 — 다시 밟지 말 것

| 내가 근거로 삼은 것 | 실제 |
|---|---|
| `publicNetworkAccess: Disabled` | 맞다. 하지만 **`networkAcls.bypass: AzureServices`** 를 안 읽었다 |
| "PNA가 ACL보다 우선한다" | **거짓.** MS 문서: 신뢰할 수 있는 서비스 접근이 *"takes the highest precedence over other network access restrictions"* |
| "AML이 스토리지에 못 간다" | AML(`Microsoft.MachineLearningServices`)은 **신뢰 서비스 목록에 있다** |
| `allowSharedKeyAccess: false` | 데이터스토어가 `credentialsType: None`(ID 기반)이라 **무관** |
| 워크스페이스 MI 권한 | `Storage Blob Data Contributor` + `AcrPull` **이미 있었다** |

출처: `learn.microsoft.com/azure/storage/common/storage-network-security-limitations`

**진짜인 스토리지 사실은 하나뿐이다.** 내 로컬 PC가 네트워크로 차단된다
(`az storage container list --auth-mode login` → `blocked by network rules`).
이건 §2.2의 **로컬에서의 코드 스냅샷 업로드** 실패만 설명하고, Azure 내부
동작과는 아무 관련이 없다.

### 0.3 얻은 교훈 (코드 주석과 테스트로 고정)

> **워크스페이스 ID가 어떤 권한을 갖고 있다는 사실은, 엔드포인트 ID가 그
> 권한을 갖고 있는지에 대해 아무것도 말해주지 않는다.**

두 principal은 완전히 다른 객체다. 나는 워크스페이스만 확인하고 "권한은 문제
없다"고 결론지었다.

### 0.4 그래서 코드로 막았다

`src/ffsft/deploy/identity.py` 의 `identity_blocker()` 가 `deploy_online()`
앞에서 ARM 4회 읽기로 판정한다. **90분 침묵 + $2.16/hr 대신 2초 만에** 중단하고
바로 실행 가능한 명령을 출력한다:

```
az role assignment create --assignee 87ec28b5-... \
  --role AcrPull --scope /subscriptions/.../registries/acrffsftkc
```

`storage_blocker()` 도 함께 고쳤다 — 이제 `networkAcls.bypass` 를 읽고,
`AzureServices` 가 있으면 통과시킨다. 고치지 않았다면 이 구독의 **모든** 배포를
존재하지도 않는 이유로 영구히 거부했을 것이다.

**틀린 진단을 지우지 않는 이유:** 원래 `storage_blocker()` 의 독스트링은
*"`networkAcls`는 일부러 보지 않는다"* 고 자신 있게 못 박아 두었다. 그 자신감이
바로 다음 사람이 확인하지 않게 만드는 장치였다. 확신에 찬 문장이 틀렸을 때
가장 비싸다는 게 이 절의 진짜 교훈이다.

### 0.5 상태

- ✅ `ffsft-acrtest` 엔드포인트 ID에 `AcrPull` + `Storage Blob Data Reader` 부여 완료
- ⏳ **아직 실제 배포로 검증되지 않았다.** 성공 가능성이 있는 첫 배포이며,
  $2.160/hr 이 드는 실험이다.
- ❌ 프라이빗 엔드포인트 + managed VNet 은 **필요 없다.** (틀린 진단의 산물)

---

## 1. Qwen3.8-27B 아키텍처 — 실측 완료 ✅

`scripts/probe_architecture.py qwen3.8-27b --check` 로 재현 가능.
GPU도, 가중치 다운로드도 필요 없다 (`meta` 디바이스 인스턴스화).

```
config class       : Qwen3_5Config          model_type: qwen3_5
architectures      : ['Qwen3_5ForConditionalGeneration']   multimodal: True
natively supported : True   (transformers 5.15.1, trust_remote_code 불필요)
instantiated as    : Qwen3_5ForCausalLM
layer map          : {'linear_attention': 48, 'full_attention': 16}
total params       : 26.90 B                tie_word_embeddings: False
non-Linear modules : {'Embedding': 1, 'Conv1d': 48}
korean efficiency  : 13 tokens / 20 chars   (vocab 248,044)
```

### 1.1 ⚠️ 가장 중요한 발견 — LoRA `target_modules`

64개 레이어 중 **48개가 `linear_attention`**이고, 이 레이어들은
`q_proj/k_proj/v_proj/o_proj`를 **가지고 있지 않다.** 대신
`in_proj_qkv / in_proj_z / in_proj_a / in_proj_b / out_proj`를 노출한다.

| target_modules | 적용된 Linear | 커버리지 |
|---|---|---|
| 관례적인 `q,k,v,o_proj` | 64 / 497 | **13 %** — 16개 레이어만 |
| `configs/models.yaml`의 실측 12개 | 496 / 497 | **100 %** — 64개 레이어 전부 |

PEFT 기본값을 쓰면 **에러 없이 학습이 돌아가고**, 네트워크의 3/4가 얼어붙은 채
조용히 나쁜 모델이 나온다. 그래서 `ModelSpec.lora_target_modules`를 추가하고
`--check` 플래그로 레지스트리와 실제 아키텍처가 일치하는지 CI에서 검증한다.

### 1.2 QLoRA 메모리 — 실측

`params × 0.5` 같은 어림짐작이 아니라 파라미터를 그룹별로 세어서 계산했다.

| 항목 | 크기 | 비고 |
|---|---|---|
| NF4로 양자화되는 Linear | **12.91 GB** | 24.35 B 파라미터 |
| bf16으로 남는 나머지 | **5.09 GB** | embedding 1.27 B + lm_head 1.27 B |
| LoRA(r=16) + grad + AdamW | 0.49 GB | |
| activations (grad ckpt, seq 1024–2048) | 4–8 GB | |
| **피크** | **22.5 – 26.5 GB** | |

`tie_word_embeddings: False`가 핵심이다. 입력 임베딩과 `lm_head`가 각각 1.27 B이고
**둘 다 양자화되지 않으므로**(bitsandbytes는 `nn.Linear`만, transformers는 출력 헤드를
full width로 유지) 5 GB가 그냥 고정 비용으로 붙는다.

> **결론: 24 GB 카드는 안전하지 않다.** 40 GB가 최소 현실선이다.
> `nn.Conv1d` 48개는 양자화 대상이 아니지만 총 2 M 파라미터라 무시해도 된다.

### 1.3 reasoning 제어

챗 템플릿에 `enable_thinking`과 `reasoning_effort`가 **둘 다** 있다
(`reasoning_effort`는 Qwen3.8 전용, 3.6/3.5에는 없다).
`enable_thinking=false`로 렌더링하면:

```
<|im_start|>user\n안녕하세요<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n
```

**thinking 블록이 사라지는 게 아니라 빈 블록으로 남는다.** 따라서 SFT 타깃도
반드시 동일한 템플릿으로 만들어야 추론 시 동작이 어긋나지 않는다.

---

## 2. Azure GPU 쿼터 — 실측 결과 ⚠️

`Microsoft.Quota` REST API로 직접 신청했다 (`az extension add --name quota`는
이 머신에서 pip 오류로 설치 실패).

| 지역 | 패밀리 | 신청 | 결과 |
|---|---|---|---|
| koreacentral | `standardNVADSA10v5Family` | 36 vCPU | ✅ **승인 (limit 36)** |
| koreacentral | `standardNCADSA100v4Family` | 24 vCPU | ❌ 실패 (2회) |
| koreacentral | `standardNCadsH100v5Family` | 40 vCPU | ❌ 실패 |
| eastus2 | A100 / A10 | — | ❌ 실패 |

승인된 A10을 제외하면 **A100·H100은 셀프서비스 API로 자동 거부된다.**
이 구독에서 80 GB급을 쓰려면 지원 티켓이 필요하다.

### 2.1 ⚠️ 처음 내린 결론은 틀렸다 — 진짜 원인은 `tier`였다

이게 이번 검증에서 가장 값비싼 발견이자, **가장 값비싼 오진**이었다.

처음에 본 증상은 이랬다. 지역 조회 API
(`Microsoft.MachineLearningServices/locations/koreacentral/vmSizes`)는
`Standard_NV36ads_A10_v5`를 **지원 목록에 포함해서** 응답하고,
`Microsoft.Quota`도 이 패밀리 쿼터를 **정상 승인**해줬는데, 정작 생성은
AmlCompute와 ComputeInstance 양쪽 다 실패했다:

```
InvalidPropertyValue: The specified value Standard_NV36ads_A10_v5 for property
Cluster.Properties.VMSize is not a supported VM size.
```

여기서 "AML 프로비저너가 자체 허용목록을 들고 있다"고 결론냈는데, **틀렸다.**
이 에러 메시지가 거짓말을 한다. 실제 원인은 SKU가 아니라 **`tier`**다.

- Azure ML은 `Microsoft.Compute`와 **별도의 자체 쿼터**를 들고 있다.
  조회는 `Microsoft.MachineLearningServices/locations/{loc}/usages`.
- **Dedicated** 쿼터는 패밀리 단위(`standardNCADSA100v4Family` 등)로 잡히는데,
  이 구독에는 해당 항목이 **아예 존재하지 않는다.** 항목이 없으면 AmlCompute는
  "쿼터 0"이 아니라 위의 **"지원하지 않는 VM 크기"** 로 응답한다.
- **LowPriority** 쿼터는 패밀리로 쪼개지지 않고 지역당 하나로 묶인
  `TotalLowPriorityCores`이며, 이 구독은 **300 코어**가 열려 있다.

> `tier="LowPriority"` 하나 바꿨더니 **`Standard_NC24ads_A100_v4`(A100 80GB)
> 클러스터가 30초 만에 생성됐다.** 쿼터 신청도, 지원 티켓도 필요 없었다.

테넌트 정책(`MCAPSGovDenyPolicies / VirtualMachine_SKU_Deny`, ref `BlockVMSKUs_N`)
도 같은 방향을 가리킨다. 이 정책의 조건은 `priority notEquals "Spot"` 이라서,
**N 시리즈는 Spot/LowPriority일 때만 허용**된다. 즉 이 구독에서 GPU를 쓰는
정상 경로가 애초에 저우선순위였던 것이다.

이 사실은 `src/ffsft/azure_ml.py`의 `GPU_SKUS[...]["low_priority"]`와
`AzureTarget.vm_priority`(기본값 `LowPriority`)에 박아뒀고,
`check_sku_fits()`가 저우선순위를 지원하지 않는 SKU를 배포 전에 막는다.
`tests/test_azure_ml.py`가 이를 고정한다.

koreacentral에서 AML이 실제로 받아주는 GPU SKU는 16종이고, 그중
`low_priority`가 아닌 것은 A100 다중 GPU 2종뿐이다:

| SKU | GPU | vCPU | LowPriority | 결과 |
|---|---|---|---|---|
| `Standard_NC24ads_A100_v4` | 1× A100 80GB | 24 | ✅ | **실제 생성 성공** |
| `Standard_NC40ads_H100_v5` | 1× H100 94GB | 40 | ✅ | 미시도 |
| `Standard_NC48ads_A100_v4` | 2× A100 80GB | 48 | ❌ | 사용 불가 |
| `Standard_NC96ads_A100_v4` | 4× A100 80GB | 96 | ❌ | 사용 불가 |
| `Standard_NV36ads_A10_v5` | 1× A10 24GB | 36 | ✅ | (Dedicated로만 실패했던 것) |

참고로 `Microsoft.Compute` 쪽 `Total Regional Low-priority vCPUs`는
`used=0 limit=100`이다.

> **정정 (2026-08-25).** 이 문단은 원래 그 수치를 `0/100`으로 적고
> "**일반 VM으로는 Spot GPU도 못 띄운다**"고 결론지었다. **틀렸다.**
> `0/100`은 100 코어가 부여되어 있고 하나도 안 쓰고 있다는 뜻이다. 같은 문단의
> 괄호가 이미 반증하고 있었다 — A10 Spot VM `vm-a10-ffsft`는 **실제로 떴다.**
> 실패한 것은 할당이 아니라 GRID 드라이버 확장(exit 14)이었다.
> (`ubuntu-hpc` 이미지가 apt로 이미 `nvidia-driver-580-open`을 깔아둬서 `.run`
> 설치기가 스스로 중단한다.) AML 경로를 찾은 뒤 삭제했다.
>
> 이 정정이 중요한 이유는 §43.6이다. 평범한
> `Microsoft.Compute/virtualMachines`가 **GPU SKU + Spot으로 이 구독에서 이미
> 할당에 성공한 적이 있다**는 뜻이고, 따라서 벽 A(SKU restriction)를 Spot이
> 우회하는 것은 AmlCompute 전용 성질이 아니다. AKS Spot 노드풀 경로의 세 번째
> 리스크는 이 관측만큼 줄어든다. VMSS는 VM과 다른 리소스 타입이라 완전히
> 증명된 것은 아니지만, 미지의 것에서 **개연성 높은 것**으로 바뀐다.

### 2.2 ⚠️ 워크스페이스 스토리지가 네트워크 격리되어 코드 스냅샷 업로드가 막힌다

`command(code=".")`로 잡을 제출하면 클라이언트가 워크스페이스 스토리지 계정에
zip을 올린다. 그런데 이 구독에서 자동 생성된 `mlwffsftstorage8cb451dd1`은:

- `allowSharedKeyAccess: false` → 키 기반 인증 불가 (`KeyBasedAuthenticationNotPermitted`)
- `publicNetworkAccess: Disabled` → **프라이빗 엔드포인트도 없음**

`az storage account update --public-network-access Enabled`는 rc=0으로 성공한 척
하고 **조용히 되돌아간다.** Azure Policy의 `modify` 이펙트라 우회 불가다.

RBAC(`Storage Blob Data Contributor`)과
`systemDatastoresAuthMode: identity`(preview api-version에서만 읽기/쓰기 가능)로
인증 문제는 풀었지만, **네트워크 도달 자체가 안 되므로 업로드는 불가능하다.**

> 해결책은 **코드를 이미지에 굽고 `code=`를 아예 쓰지 않는 것**이다.
> 마이크로소프트의 공식 finetune 컴포넌트들도 정확히 이 방식이다
> (`azureml-assets`가 `COPY finetune_run.py /azureml/finetune/run.py`).
> 대가는 코드 수정 시 이미지 재빌드다.

### 2.3 학습 이미지: 커스텀 빌드가 선택이 아니라 필수다

세 가지가 각각 독립적으로 커스텀 이미지를 강제한다:

1. **내장 `chat_completion_finetune` 컴포넌트는 QLoRA를 못 한다.** 노출된 파라미터가
   `apply_lora / lora_r / lora_alpha / lora_dropout` 뿐이고 `load_in_4bit`나
   `bnb_4bit_quant_type`이 없다. NF4를 쓰려면 커스텀 command job 외 선택지가 없다.
2. **큐레이티드 `acft-hf-nlp-gpu`는 `transformers==5.5.0`으로 고정**돼 있다.
   Qwen3.8은 `model_type: qwen3_5`라 5.8 이상이 필요하다.
3. 그렇다고 맨바닥 우분투에 torch부터 새로 까는 건 낭비다. 드라이버/NCCL/RDMA
   조합을 마이크로소프트가 검증해둔 걸 버리는 셈이라서.

그래서 **ACPT 이미지를 베이스로 쓰고 모델 쪽 라이브러리만 올린다.**
실측한 베이스(`aifx/acpt/stable-ubuntu2204-cu126-py310-torch280`) 내용:

| 항목 | 값 |
|---|---|
| Python | **3.10.20** |
| torch | 2.8.0+cu126 (torchvision 0.23.0, torchaudio 2.8.0) |
| deepspeed | 0.15.1 |
| triton / numpy | 3.4.0 / 2.2.6 |
| transformers·peft·trl·bitsandbytes·datasets | **없음** (우리가 설치) |

두 가지가 여기서 갈렸다:

- **ACPT는 전 태그가 py3.10이다.** py3.11+ 태그는 MCR에 존재하지 않는다
  (`aifx/acpt/*` 전수 확인). 그래서 `ffsft`의 하한을 3.10으로 내리고
  `ffsft/models/spec.py`에 `StrEnum` 백포트를 넣었다. `str, Enum` 믹스인만으로는
  3.10에서 `str(Provider.HF)`가 `"Provider.HF"`가 되어 3.11과 동작이 달라지므로
  `__str__`/`__format__`을 `str`의 것으로 고정했다.
- **torch 하한을 2.9 → 2.8로 내렸다.** ACPT가 검증한 건 2.8.0+cu126이고,
  이미지 빌드 시 `-c` 제약파일로 이 버전을 고정한다. 의존성 하나가 다른 torch를
  끌어오려 하면 빌드가 **조용히 바뀌는 대신 실패**하도록 한 것이다.

빌드는 로컬이 아니라 **`az acr build`로 서버 사이드**에서 돈다. 컨텍스트만
올라가므로 20 GB짜리 이미지를 사용자 회선으로 push하지 않는다.

---

## 3. 생성된 Azure 리소스

| 리소스 | 이름 | 상태 |
|---|---|---|
| Resource Group | `rg-ffsft-kc` (koreacentral) | ✅ 생성됨 |
| Azure ML Workspace | `mlw-ffsft` | ✅ 생성됨 (+ storage/KV/AppInsights) |
| **GPU 컴퓨트** | **`gpu-a100-lp`** | ✅ **A100 80GB / LowPriority / min0-max1** |
| Container Registry | `acrffsftkc` | ✅ 생성됨 (Basic, admin 비활성, MSI에 `AcrPull`) |
| A10 Spot VM | `vm-a10-ffsft` | 🗑️ 삭제함 (드라이버 실패, AML 경로로 대체) |

`gpu-a100-lp`는 `min_instances=0`이라 **유휴 시 과금이 없고**, 잡이 도는 동안만
과금된다. 현재 실행 중인 VM은 없다.

| 서빙 리소스 | 이름 | 상태 |
|---|---|---|
| 컨테이너 이미지 | `acrffsftkc.azurecr.io/ffsft-serve:2` | ✅ ACR 빌드 성공 |
| AML Environment | `ffsft-serve:1` → 위 이미지 | ✅ 등록됨 |
| Online Endpoint | `ffsft-smoke` | ⚙️ 스모크 테스트용, 테스트 후 삭제 |

---

## 4. vLLM 서빙 이미지 — 실측 완료 ✅

`docker/Dockerfile.serve`는 빌드 중에 `docker/verify_serve.py`를 실행해서
**이미지가 잘못 만들어지면 빌드 자체가 실패**하게 만들었다. ACR 빌드 4회 만에 통과.

```
vllm 0.27.1   torch 2.13.0+cu130   python 3.12
arch Qwen3_5ForConditionalGeneration: registered   ← 모델 지원 확인
flags 12 required: all present   (--language-model-only 포함)
```

### 4.1 빌드하면서 실제로 밟은 지뢰 4개

| 회차 | 증상 | 원인 |
|---|---|---|
| 1 | `python: not found` | vLLM 이미지는 `python3`만 있다 |
| 2 | `cannot import name 'FlexibleArgumentParser' from 'vllm.utils'` | 0.27에서 위치가 바뀜 |
| 3 | `RuntimeError: Failed to infer device type` | `make_arg_parser()`가 `VllmConfig`를 만들고, 이게 **GPU를 요구**한다. ACR 빌드 에이전트는 CPU 전용 |
| 4 | — | 통과 |

3번 때문에 플래그 검증을 **파서 생성이 아니라 vLLM 소스 2,360개 파일 텍스트 스캔**으로
바꿨다. 빌드 타임에는 GPU가 없다는 게 핵심 제약이다.

### 4.2 모델 교체 가능성 — 실제로 깨졌다가 고친 부분

Qwen3.8 전용 플래그(`--mamba-cache-mode align`, `--language-model-only`,
`--reasoning-parser qwen3`)를 **무조건** 붙이고 있었다. 이러면 `Qwen3-0.6B` 같은
dense 모델에서 서버가 안 뜬다. 해당 env 를 비우면 빠지는 **opt-out** 방식으로
바꾸고 `ffsft-serve:2`로 재빌드했다.

> Qwen3.8은 48개 GDN 레이어 때문에 `--mamba-cache-mode align`이 **필수**다.
> `all`로 두면 `NotImplementedError`가 난다.

---

## 5. Online Endpoint 쿼터 — 실측 완료 ✅

이번 세그먼트에서 가장 비싼 발견이다.

```
(OutOfQuota) The amount of CPU quota requested is 72
             and your maximum amount of quota is [N/A]
```

`Standard_NV36ads_A10_v5`(36 코어)를 1 인스턴스 배포했는데 **72 코어**를 요구했다.
Managed Online Endpoint 는 **롤링 업데이트용으로 인스턴스 세트를 하나 더 예약**하기
때문에 실제 필요량은 항상 **SKU 코어 × 인스턴스 수 × 2** 다.

| SKU | 코어 | 1 인스턴스 배포 시 필요 | A10 쿼터 36 에서 |
|---|---|---|---|
| `Standard_NV36ads_A10_v5` | 36 | **72** | ❌ 불가 |
| `Standard_NV18ads_A10_v5` | 18 | **36** | ✅ 가능 |

그래서 `src/ffsft/deploy/spec.py`에 `ONLINE_ENDPOINT_CORE_MULTIPLIER = 2`와
`required_dedicated_cores()`를 넣고, `blocked_reason()`이 **배포 전에** 막도록 했다.
20분짜리 롤아웃이 실패한 뒤에 알게 되는 것과 즉시 거부되는 것의 차이다.

### 5.1 실패한 배포는 복구 불가

```
Specified deployment [blue] failed during initial provisioning
and is in an unrecoverable state. Delete and re-create.
```

파라미터를 고쳐서 재시도하면 **원래 문제와 무관한 이 에러**로 또 실패한다.
`deploy_online()`이 재배포 전에 실패한 배포를 먼저 지우도록 수정했다.

### 5.2 이미지 ENV 기본값이 모델 교체를 깨뜨렸다 — 실측 ⚠️

`Qwen/Qwen3-0.6B`(dense, 텍스트 전용) 스모크 배포가 **45분 동안 `Creating`에
머물다 실패**했다. 원인은 배포가 아니라 **이미지**였다.

`ffsft-serve:2`는 ENV 기본값으로 `MAMBA_CACHE_MODE=align`,
`LANGUAGE_MODEL_ONLY=1`, `REASONING_PARSER=qwen3`을 갖고 있었다. Qwen3.8-27B에
필요한 값이라 넣은 것인데, `deploy_online()`이 이 값들을 **덮어쓰지 않아서**
Mamba 상태도 비전 타워도 없는 0.6B 모델이 그 플래그로 기동됐다.

> **교훈: 모델이 바뀌는 에셋에서 아키텍처 플래그를 이미지에 굽지 마라.**
> 배포가 키를 생략하면 이미지 기본값을 **조용히 상속**한다.

수정 방향(극성을 뒤집음):
1. 이미지 기본값을 **전부 중립**으로 (`ffsft-serve:3`)
2. `ModelSpec`에 `multimodal` / `mamba_cache_mode` / `reasoning_parser` 추가
3. `serving_env()`가 **중립값일 때도 세 키를 항상 명시적으로 전송** —
   이미지 기본값이 다시는 조용히 적용될 수 없게

> **정정 (나중에 밝혀진 것):** 이 버그는 **진짜 버그가 맞지만, 배포 실패의
> 원인은 아니었다.** 세 플래그를 모두 중립으로 고쳐서 다시 배포한
> `ffsft-smoke2` 도 **똑같이** 68분간 `Creating` 에 머물다 실패했다.
> 진짜 원인은 §0 의 스토리지 도달 불가다. 당시에는 `InternalServerError` 라는
> 범용 코드밖에 없어서 이 가설이 "강하게 시사되지만 증명되지 않음" 상태였는데,
> 재배포가 그 가설을 **반증**했다. 수정 자체는 모델 교체 가능성을 위해 유지한다.

### 5.3 45분이 걸린 이유 — 프로브 예산

관측된 타임라인이 정확히 설명된다:

```
노드 할당 + 이미지 pull (~20GB)        ~20 분
readiness initialDelay  PT10M          10 분
failureThreshold 30 x period PT30S     15 분
                                    = 약 45 분
```

즉 **틀린 배포가 실패를 보고하는 데 45분**이 걸렸고, Azure는 **터미널 상태가
되기 전까지 컨테이너 로그를 주지 않는다.** 느린 실패가 실패 원인까지 가린다.

`startup_grace_for(params_b)`로 모델 크기에 비례하게 바꿨다.
0.6B → 135초, 27B → 795초. 상한 1800초.

> **정정:** 두 번째 배포는 프로브 예산(10분 + 10×30초 = 15분)에 이미지 pull을
> 더해도 **35분**이어야 하는데 **68분**을 넘겼다. 즉 프로브 산수만으로는
> 설명되지 않는다 — 컨테이너가 아예 시작되지 않으면 프로브 예산은 시작조차
> 하지 않기 때문이다(§0). App Insights `traces` 가 완전히 비어 있던 것이
> 그 증거다.

> 참고: 워크스페이스는 `managedNetwork.isolationMode: Disabled`,
> `publicNetworkAccess: Enabled` 라서 **HF Hub 다운로드는 막히지 않았다.**
> (네트워크 격리 가설은 실측으로 배제됨. 단, 이건 *워크스페이스* 의
> `publicNetworkAccess` 이고 §0 은 *스토리지 계정* 의 값이다 — 다른 리소스의
> 같은 이름 속성이라 헷갈리기 쉽다.)

### 5.3.1 그 수정이 실제 배포에서는 적용되지 않았다 — 실측 ⚠️

`ffsft-smoke2` 배포를 ARM으로 확인하니 `initialDelay`가 **`PT2M15S`가 아니라
`PT10M`** 이었다. 원인: 스모크 배포는 `--hf-model Qwen/Qwen3-0.6B` 로 나갔고
**레지스트리 키(`--model`)가 없었다.** 그래서 `model_spec`이 `None`,
`params_b`도 `None`, 결국 보수적 기본값 600초로 되돌아갔다.

즉 5.3에서 없앤 문제가 **"교체 가능한 모델은 레지스트리에 없을 수도 있다"** 는
바로 그 경로로 다시 새어 들어왔다. 모델 교체 가능성이 이 리포의 전제이므로
이건 예외가 아니라 정상 경로다.

수정: `params_from_hf_id()` 가 Hub repo id에서 크기를 복원한다.

| repo id | 파싱 결과 | 이유 |
|---|---|---|
| `Qwen/Qwen3-0.6B` | 0.6 | |
| `meta-llama/Llama-3.1-8B-Instruct` | 8.0 | `3.1`은 버전, `B` 접미사만 크기 |
| `Qwen/Qwen3-30B-A3B` | **30.0** | 마지막이 아니라 **최댓값**. 기동 비용은 다운로드(총 파라미터)가 결정 |
| `mistralai/Mixtral-8x7B-...` | 56.0 | MoE 축약은 곱해서 편다 |
| `google-bert/bert-base-uncased` | `None` | 추측하지 않는다 → 보수적 기본값 유지 |
| `model-7Base`, `...-4bit` | `None` | `B` 뒤 문자/숫자 금지 가드 |

`resolve_params_b()` 우선순위는 **그 숫자를 실제로 얼마나 확인했는가** 순서다:
명시적 `--params-b` > 레지스트리 spec > repo id 파싱 > `None`.

### 5.4 엔드포인트 삭제는 느리다

프로비저닝 중인 엔드포인트를 삭제하면 **20분 이상** `Deleting`에 머문다.
테어다운을 실험 종료 직전에 몰아서 하지 말고 여유를 두는 편이 낫다.

### 5.5 `serving_env()` 수정은 실제 배포에서 확인됐다 ✅

`ffsft-smoke2` 배포의 ARM 응답:

```
environment: environments/ffsft-serve/versions/2 → acrffsftkc.azurecr.io/ffsft-serve:3
environmentVariables:
  MODEL_PATH: Qwen/Qwen3-0.6B
  LANGUAGE_MODEL_ONLY: "0"      ← 이미지 기본값(1)을 덮어씀
  MAMBA_CACHE_MODE: ""          ← 이미지 기본값(align)을 덮어씀
  REASONING_PARSER: ""          ← 이미지 기본값(qwen3)을 덮어씀
```

세 키가 **중립값이어도 항상 명시적으로 나간다**는 것이 회귀 방지 장치다.
빠뜨리면 이미지 기본값이 조용히 상속된다(5.2).

---

## 6. 테어다운 — 실측 완료 ✅

`ffsft lifecycle down --endpoint ffsft-smoke --yes` 를 **실제 구독에 대해 실행**했다.

```
ffsft-smoke / blue  Standard_NV36ads_A10_v5 x1
  → $4.320/hr  ~$3,154/month
deleted: onlineEndpoint ffsft-smoke
BILLING NOW: nothing
```

과금 분류 기준이 실측으로 확인됐다:

| 리소스 | 유휴 시 |
|---|---|
| Managed Online Endpoint | **24시간 과금** (scale-to-zero 없음) |
| AmlCompute `min_instances=0` | **무과금** |
| Batch Endpoint | **무과금** (잡 돌 때만) |

그래서 `teardown()`은 **엔드포인트는 삭제**하고 **클러스터는 0으로 스케일 다운**만
한다. 클러스터 정의는 공짜고 재생성에 수 분이 걸린다.

koreacentral PAYG 실측 단가($/hr): `NC16as_T4_v3` 1.481 · `NV18ads_A10_v5` 2.160 ·
`NV36ads_A10_v5` 4.320 · `NC24ads_A100_v4` 4.959 · `NC40ads_H100_v5` 9.423

---

## 7. 한국어 데이터 전처리 — NFC 정규화 필수

macOS와 일부 크롤러는 한글을 **NFD(자모 분해)** 로 내보낸다. `가` 가
`U+1100 U+1161` 로 저장되는데, **화면에는 똑같이 보이고 해시는 다르다.**
정규화 없이 dedup 하면 섞인 코퍼스에서 **중복 제거가 조용히 무력화**된다.
`normalize_text()`가 해싱 전에 NFC를 적용한다.

---

## 8. 아직 검증 못 한 것

- [ ] **Qwen3.8-27B QLoRA 실제 학습** — bitsandbytes NF4가 hybrid
      linear-attention/Conv1d 레이어에서 실제로 도는지. 최대 리스크.
- [ ] 22.5–26.5 GB 실측 추정이 실제 피크와 맞는지
- [ ] vLLM LoRA가 GDN projection(`in_proj_qkvz`, `in_proj_ba`)에도 실제로 붙는지
- [ ] Fabric → OneLake → AML 데이터 경로
- [ ] `benchmarks.yaml`의 한국어 harness task 이름
- [ ] `trl` 1.10 / `peft` 0.20 이 `transformers` 5.15와 호환되는지
- [ ] 27B를 A10 24GB로 서빙하려면 **Int4 체크포인트**가 필요 (bf16 머지본 불가)
- [ ] **라이브 엔드포인트 대상 로드테스트** — 클라이언트 정확도는 §9에서
      검증했지만, 실제 vLLM 엔드포인트에는 아직 못 붙였다(§0 때문에 차단)

### 8.1 해결된 항목 — `AutoModelForCausalLM` vs 멀티모달 체크포인트 ✅

Qwen3.8은 `Qwen3_5ForConditionalGeneration`(멀티모달)인데 `qlora.py`는
`AutoModelForCausalLM`을 쓴다. 이게 되는지가 학습 쪽 최대 미해결 질문이었다.

transformers 업스트림 소스에서 직접 확인했다
(`src/transformers/models/auto/modeling_auto.py`):

```python
MODEL_FOR_CAUSAL_LM_MAPPING_NAMES = OrderedDict([
    ...
    ("qwen3_5", "Qwen3_5ForCausalLM"),   # VLM compatibility
    ("qwen3_5_text", "Qwen3_5ForCausalLM"),
])
MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES = OrderedDict([
    ("qwen3_5", "Qwen3_5ForConditionalGeneration"),
])
```

`# VLM compatibility` 주석이 붙은 항목이 명시적으로 존재한다. 즉
**`AutoModelForCausalLM.from_pretrained("Qwen/Qwen3.8-27B")` 는 vision tower를
버리고 언어 타워만 `Qwen3_5ForCausalLM` 으로 로드한다.** `qlora.py`를
`AutoModelForImageTextToText`로 바꿀 필요가 없다.

서빙 쪽과도 일관된다: `LANGUAGE_MODEL_ONLY=1` 로 vLLM도 같은 텍스트 전용
서브셋을 띄운다. **텍스트로 학습하고 텍스트로 서빙**한다.

이 매핑은 `transformers>=5.8` 에서 들어왔고, `docker/Dockerfile.train` 이
이미 그 하한을 강제하고 있다.


---

## 9. 로드테스트 클라이언트 — 정확도 실측 완료 ✅

라이브 엔드포인트가 없어도(§0) **측정 도구 자체가 옳은지**는 검증할 수 있다.
`scripts/mock_vllm_server.py` 가 TTFT/ITL을 제어할 수 있는 SSE 서버를 띄우고,
`ffsft-loadtest` 를 **알려진 정답 스트림**에 붙여서 측정치를 대조했다.

| 항목 | 주입한 값 | 측정된 값 | 오차 |
|---|---|---|---|
| TTFT p50 | 0.200 s | 0.202 s | ~1% |
| TPOT p50 | 0.010 s | 0.0103 s | ~3% |

동시성에 따른 처리량도 선형으로 증가했다:

```
concurrency 1 →  61 tok/s
concurrency 4 → 244 tok/s
concurrency 8 → 483 tok/s
```

knee(무릎점) 탐지도 동작했다. 즉 **로드테스트가 보고하는 숫자는 믿어도 된다** —
남은 건 실제 엔드포인트에 붙이는 것뿐이다.

리포트 JSON 키 이름 주의: `ttft_p50`, `tpot_p50`, `output_tok_per_s`
(백분위 키에는 `_s` 접미사가 **없다**).

이 대조는 `tests/test_loadtest_e2e.py` 로 승격되어 실제 소켓 위에서 매번 돈다.

---

## 10. 이번 세션에서 코드로 고정한 것들

실측으로 알아낸 사실은 문서만으로는 다시 잃어버린다. 그래서 전부 테스트로 고정했다.

| 테스트 파일 | 고정한 사실 | 개수 |
|---|---|---|
| `tests/test_serving_env.py` | 아키텍처 플래그 3종은 중립일 때도 항상 전송 | 17 |
| `tests/test_startup_grace.py` | 프로브 예산은 모델 크기에 비례 | 16 |
| `tests/test_model_size_inference.py` | 레지스트리에 없어도 repo id에서 크기 복원 | 18 |
| `tests/test_serve_entrypoint.py` | HF repo id로도 엔트리포인트가 죽지 않음 + blob fetch 실패 시 컨테이너 사망(§62.5) | 11 |
| `tests/test_preflight_storage.py` | 스토리지 도달 불가면 즉시 거부 | 23 |
| `tests/test_loadtest_e2e.py` | 로드테스트 측정 수식이 정답과 일치 | 5 |
| `tests/test_submit_merge_guard.py` | 머지 경로도 스토리지를 선검사한다 (§63의 재발 방지) | 7 |
| `tests/test_deploy_egress.py` | 매니지드 VNet 워크스페이스엔 egress 설정을 **보내지 않는다** (§64) | 6 |
| `tests/test_endpoint_traffic_preserved.py` | 재배포가 엔드포인트 트래픽 맵을 지우지 않는다 (§65) | 6 |

전체 **652 테스트 통과**, `ruff` 클린. (개수는 세션마다 실측해서 갱신한다 — 표에 남은 옛 숫자는 문서가 아니라 주장이 된다.)

---

## 11. 삭제된 VM이 남긴 요금 — 실측 $41.66/월 누수 ✅

`ffsft-lifecycle status` 는 **엔드포인트와 클러스터만** 본다. AML 워크스페이스
클라이언트로 조회하기 때문인데, **디스크와 공인 IP는 워크스페이스 리소스가 아니라
리소스 그룹 리소스**라 애초에 보이지 않는다. 구조적인 사각지대였다.

리소스 그룹을 직접 훑어보니 삭제한 스팟 A10 VM의 잔해가 그대로 과금되고 있었다:

| 리소스 | 상태 | 실제 요금 |
|---|---|---|
| `vm-a10-ffsft_OsDisk_...` | 256 GB Premium_LRS, **Unattached** | **$38.01/월** |
| `vm-a10-ffsftPublicIP` | Standard static IPv4 | **$3.65/월** |
| `vm-a10-ffsftVMNic` / NSG / VNET | 고아 | $0 |

**합계 $41.66/월** — 아무 일도 하지 않는 리소스에 대해. 전부 삭제 완료.

### 11.1 공인 IP가 함정이었다

이 IP 는 `ipConfiguration` 이 **정상적으로 붙어 있었다.** 그래서
`ipConfiguration is None` 만 보는 검사는 이걸 **정상이라고 판정한다.**
실제로는 그 NIC 의 `virtualMachine` 이 `null` — VM 이 이미 삭제된 시체에
붙어 있었던 것이다. 그래서 판정은 **NIC 를 한 단계 더 따라가야** 한다.
`test_public_ip_attached_to_orphaned_nic_is_still_an_orphan` 이 이걸 고정한다.

### 11.2 가격은 기억이 아니라 Retail Prices API 에서

koreacentral 소비(Consumption) 단가를 API 로 직접 조회했다.
처음 적어둔 6개 티어 중 **2개(P4·P6)가 틀렸다.**

```
P4  LRS   5.2795 USD/월      P15 LRS  38.012142 USD/월  ← 실제 누수분
P6  LRS  10.207  USD/월      P20 LRS  73.22     USD/월
P10 LRS  19.71   USD/월      P30 LRS 135.17     USD/월
Standard IPv4 Static Public IP  0.005 USD/시 → 3.65 USD/월
```

> ⚠️ 단순 조회하면 **1년 예약(Reservation)** 행이 같이 나온다. P30 이
> `1541.0` 으로 보이는데 소비 단가는 `135.17` 이다. **약 11배 차이** —
> `type: Consumption` 으로 걸러야 한다. 리포트가 자신 있게 거짓말하는 전형적인 경로다.

가격을 모르는 SKU 는 **추측하지 않고 "unknown" 으로 보고**한다.
비용 리포트의 지어낸 숫자는 없는 것보다 나쁘다. 믿어버리기 때문이다.

### 11.3 `down` 은 이걸 건드리지 않는다

고아 리소스는 `bills_when_idle=True` 라 `inv.billing` 에 들어가지만,
`teardown()` 은 의도적으로 무시하고 **삭제 명령만 출력**한다.
디스크 삭제는 되돌릴 수 없고, `up` 이 다시 만들어주지도 않는다 — 사람이 판단할 일이다.
`test_teardown_never_touches_orphans` 가 폭발하는 가짜 클라이언트로 이걸 고정한다.

### 11.4 라이브 검증

```
RESULT Microsoft.Compute/disks:            http=200 count=0
RESULT Microsoft.Network/publicIPAddresses: http=200 count=0
RESULT Microsoft.Network/networkInterfaces: http=200 count=0
read_orphans -> []
REPLAY of the real leak -> [('vm-a10-ffsft_OsDisk_1', 38.01), ('vm-a10-ffsftPublicIP', 3.65)]
TOTAL $/month: 41.66
```

`http=200 count=0` 이 중요하다. `read_orphans` 는 어떤 실패에도 `[]` 를 돌려주므로
"고아 없음"이 "인증 실패"를 가리고 있을 수 있다. 200/0 이니 **진짜로 없는 것**이다.

### 11.5 ACR 정리

`ffsft-serve:1,2` · `ffsft-train:1,2,3` · `ffsft-probe:1,2` 삭제.
**23.33 GB → 19.51 GB.** 레이어가 공유되어 절감폭은 작다.
남은 19.5 GB 는 `serve:3`(9.15 GB) + `train:4`(10.36 GB) 로 **둘 다 필요한 최소치**다.
(ACR Basic 포함 용량은 10 GB 라 초과분은 과금된다.)

---

## 12. 두 번째 배포도 동일하게 사망 — signature 확정 ✅

> **정정.** 이 절은 원래 제목이 "근본 원인 최종 확정"이었고, 실패가 스토리지
> 진단을 확증한다고 적혀 있었다. 확증한 것은 **실패의 signature**(로그 없음 /
> traces 없음 / 범용 에러)이지 원인이 아니다. 같은 signature는 **이미지 pull
> 실패**에서도 똑같이 나오며, 실제 원인은 그쪽이었다. §0 참조.

`ffsft-smoke2` 배포 명령이 **1시간 54분** 만에 최종 실패로 돌아왔다.

```
=== deploying ffsft-smoke2 : Qwen/Qwen3-0.6B on NV18ads_A10_v5, image :3 ===
시작  Fri Aug 21 01:48:15 KST 2026
종료  Fri Aug 21 03:42:33 KST 2026
Code: InternalServerError
Message: Internal error. Please see troubleshooting guide ... #error-internalservererror
```

| 예측한 signature | 실제 |
|---|---|
| 한 시간 넘게 `Creating` | **1시간 54분** |
| 컨테이너 로그 없음 | 종료 시점까지 계속 withheld |
| App Insights `traces` 비어 있음 | 비어 있었음 |
| 최종 `InternalServerError` | **정확히 그것** |

이로써 **0.6B 모델 / 중립 플래그 / 수정된 엔트리포인트** 조합으로도 실패한다는 게
확정됐다. 모델도, 이미지 ENV 도, 엔트리포인트도 원인이 아니다.
**컨테이너가 시작조차 못 했다**는 사실이 전부 설명하며, 그 이유는 스토리지가
아니라 **엔드포인트 ID에 `AcrPull` 이 없어서 이미지를 못 읽은 것**이다(§0).

여기서 배운 방법론: **signature 일치는 원인 확정이 아니다.** "예측대로
실패했다"는 관측은 같은 signature를 내는 다른 원인을 전혀 배제하지 못한다.
나는 이걸 확증으로 읽고 커밋까지 했다.

그리고 이 실패에 **$2.160/시 × 1.9시간 ≈ $4.1** 이 청구됐다.
프리플라이트(§0.4)는 이걸 **2초**에 거부한다. 이 한 번의 실패만으로도 값을 한다.

---

## 13. 테어다운 완료 — `BILLING NOW: nothing` ✅

```
18:07:57  DELETE 요청 (ARM 직접 호출)
18:42:33  create 오퍼레이션이 InternalServerError 로 최종 종료  ← §12
18:48:20  endpoints=0  ALL ENDPOINTS DELETED
```

**총 40분 24초.** 그런데 주목할 점은 **삭제가 생성 뒤에 줄 서 있었다**는 것이다.
create 가 18:42:33 에 풀리고 나서 **6분 만에** 삭제가 끝났다.
즉 앞의 34분은 삭제가 느린 게 아니라 **아직 돌고 있는 create 를 기다린 시간**이다.

> 실무 함의: 실패한 배포를 `down` 하면 오래 걸린다고 놀랄 필요 없다.
> **원래 배포 오퍼레이션이 끝나야 삭제가 시작된다.** 그동안 GPU 는 계속 과금된다.
> 이것이 프리플라이트가 사후 정리보다 훨씬 값싼 또 하나의 이유다.

최종 상태:

```
KIND                 NAME              SKU                          $/hr  NOTE
  compute-cluster    gpu-a100-lp       Standard_NC24ads_A100_v4        -  min_instances=0: idle costs nothing
BILLING NOW: nothing. No always-on compute in this workspace.
```

- 온라인 엔드포인트: **0개**
- A100 클러스터: 정의는 유지, **0 노드 → 무료** (재실험 시 `up` 으로 즉시 복구)
- 삭제된 VM 잔해: **전부 제거** (§11)
- ACR: 사용 중인 2개 태그만 유지 (§11.5)

**지금 이 구독에서 이 프로젝트로 과금되는 리소스는 없다.**

---

## 14. ⛔ 세 번째 배포도 실패 — `AcrPull` 가설도 틀렸다

**§0의 진단(엔드포인트 ID에 `AcrPull` 없음)도 원인이 아니었다.**
권한을 부여하고 재배포했으나 **113분 만에 동일한 `InternalServerError`**.

```
09:37:19  deploy 시작 (ffsft-acrtest, Qwen3-0.6B, NV18ads_A10_v5)
11:30:40  InternalServerError            ← 113분
비용      113분 × $2.160/hr ≈ $4.07
```

배포 직전 실측으로 확인한 것들 — **전부 정상이었다**:

| 확인 항목 | 결과 |
|---|---|
| 엔드포인트 MI `AcrPull` on `acrffsftkc` | ✅ 부여됨 |
| 엔드포인트 MI `Storage Blob Data Reader` | ✅ 부여됨 |
| 환경 `ffsft-serve:2` → 이미지 태그 | ✅ `ffsft-serve:3`, ACR에 실존 |
| `inferenceConfig` (liveness/readiness/scoring) | ✅ `/health:8000`, `/v1/chat/completions:8000` |
| `StandardNVADSA10v5Family` 온라인 쿼터 | ✅ **limit=72** |
| ACR `publicNetworkAccess` | ✅ Enabled |

### 14.1 이번에 처음 얻은 진짜 신호

종료 상태에서 로그를 조회했더니 **`Creating` 중과 다른 메시지**가 나왔다:

```
Creating 중 : "Deployment is in deleting or creating state so logs can't be retrieved."
Failed 후   : "There are no logs for this deployment at the moment."
```

두 문장은 전혀 다른 뜻이다. 앞은 "안 보여준다", 뒤는 "없다"이다.
`ffsft/deploy/logs.py` 가 이 구분을 강제하는 이유가 이것이다.

다만 **뒤 문장도 "컨테이너가 없었다"의 증명은 아니다.** 로그 수집 지연일 수도
있다. 이번엔 단정하지 않는다 — 두 번 그렇게 틀렸다.

### 14.2 Activity Log에는 아무것도 없다

```
az monitor activity-log list ... --query "[?status.value=='Failed']"
FAILED rows: 0
```

실패가 **ARM 컨트롤 플레인에 아예 기록되지 않는다.** AML 데이터 플레인 내부에서
끝난다는 뜻이고, 외부에서 원인을 볼 수 있는 창구가 사실상 없다.

### 14.3 이번 실수 — 로그를 못 건지고 지웠다

`Failed` 확정 직후 로그가 "no logs"로 나왔고, 곧바로 엔드포인트를 삭제했다.
**로그 수집이 지연됐던 것이라면 몇 분 뒤엔 나왔을 수도 있다.** 삭제하면 영영
못 본다. 다음부터는 **터미널 상태에서 최소 10분간 로그를 재시도한 뒤** 삭제한다.

### 14.4 지금까지 배제된 것 / 남은 가설

배제됨(실측):
- 모델 크기 (0.6B로도 실패)
- 이미지 ENV / 엔트리포인트 (중립 플래그로도 실패)
- 스토리지 네트워크 (`bypass: AzureServices`)
- 엔드포인트 ID 권한 (`AcrPull` + `Blob Data Reader` 부여 후에도 실패)
- 온라인 엔드포인트 쿼터 (A10 v5 limit=72)
- 삭제된 이미지 태그 (태그 3 실존)

남은 가설(**미검증**):
1. **A10 v5 노드를 온라인 엔드포인트에 할당하지 못한다** — 학습 클러스터는
   저우선순위로 통과했지만 온라인 엔드포인트는 전용 할당이라 테넌트 정책의
   N-series 거부에 걸릴 수 있다.
2. 이미지 자체가 이 런타임에서 기동 실패 (9.15 GB, vLLM)
3. 리전/구독 단위의 플랫폼 문제

§14.5의 CPU SKU 실험이 1번과 2번을 가른다.

### 14.5 CPU SKU 진단 — 8분 $0.04로 원인 확정 ✅

GPU에서 113분 $4.07 을 태우고도 못 얻은 답을, **같은 이미지를 CPU SKU
(`Standard_DS3_v2`, $0.29/hr)에 배포**해서 **8분 $0.04** 에 얻었다.

vLLM 은 GPU 없이는 못 뜨지만, **이미지 pull 은 SKU 와 무관**하다. 그래서
"pull 문제인가, GPU 노드 문제인가"를 가르는 데 GPU 가 전혀 필요 없다.

**1차 CPU 테스트(권한 없음)** — 8분 만에 터미널, 그리고 처음으로 진짜 로그:

```
UserContainerImagePull: InProgress
Kind: Pod, Name: ImagePullFailed, Message: Image pull failed, retrying.

ImageFetcher: "Found user ACR image" IdentityType:"XDS"
              clientid: 8d8cc0ae-ca15-4d88-929a-132e3cde3a1a
HTTP status code: 401
exchange refresh token failed: {"code":"UNAUTHORIZED",
  "message":"authentication required, visit https://aka.ms/acr/authorization"}
```

`8d8cc0ae-...` 를 조회하니:

```
displayName: mlw-ffsft/onlineEndpoints/ffsft-cputest
type:        ManagedIdentity
```

**엔드포인트 자신의 MI 가 맞다.** (로그에 찍히는 건 appId, 롤 부여에 쓰는 건
objectId 라서 값이 달라 보인다.) §0 의 진단 방향은 옳았다.

**2차 CPU 테스트(AcrPull 부여 + 5분 대기)**:

```
elapsed: 60.6s    total: 4.5 Gi (76.8 MiB/s)
unpacking linux/amd64 sha256:e7d8024a...
done: 5m16.5s
"Imagefetcher runs successfully."

UserContainerImagePull: Succeeded      ← 이전엔 InProgress→Failed
UserContainerStart: Waiting            ← CPU라 vLLM 기동 불가 (예상된 실패)
```

### 14.6 그래서 `ffsft-acrtest` 는 왜 실패했나 — 롤 전파

`AcrPull` 을 부여하고 **거의 즉시** 배포했다. ACR 토큰 교환은 롤 전파가
끝나야 성공한다. 2차 CPU 테스트는 **5분을 기다린 뒤** 배포했고 통과했다.

> **부여 후 최소 5분 기다린 뒤 배포할 것.** 안 기다리면 113분 뒤에
> `InternalServerError` 로 돌아오고, 그 실패는 권한이 없을 때와 구별되지 않는다.

### 14.7 방법론 교훈 — 이게 이 절에서 제일 중요하다

| | GPU 로 진단 | CPU 로 진단 |
|---|---|---|
| 1회 소요 | **113분** | **8분** |
| 1회 비용 | **$4.07** | **$0.04** |
| 얻은 정보 | `InternalServerError` (무의미) | 401 + clientid + 스택트레이스 |

**비싼 자원 위에서 디버깅하지 말 것.** 실패 지점(이미지 pull)이 비싼
자원(GPU)과 무관하다면, 싼 SKU 에서 똑같이 재현된다. 세 번의 GPU 실패
(68 + 114 + 113 = 295분, 약 $10.6) 중 **단 한 번도** 8분짜리 CPU 테스트가
준 정보를 주지 못했다.

---

## 15. 통제 실험 — 남은 블로커는 **A10 GPU 노드 할당**이다

권한 전파까지 검증된 **동일 엔드포인트**(`ffsft-pulltest`)에서 **SKU 하나만**
바꿔 두 번 배포했다. 이미지·모델·환경·ID·권한 전부 동일하다.

| | `Standard_DS3_v2` (CPU) | `Standard_NV18ads_A10_v5` (GPU) |
|---|---|---|
| 이미지 pull | ✅ **Succeeded** (4.5 GiB @ 76.8 MiB/s) | 확인 불가 |
| 터미널 도달 | ✅ 14분 | ❌ **65분+ `Creating` 유지** |
| 로그 | ✅ 조회됨 | ❌ 끝까지 withheld |
| 결과 | vLLM이 GPU 없어 기동 실패(예상됨) | 미해결 |

**변수는 SKU 하나뿐이다.** 따라서 남은 원인은 권한도, 이미지도, 스토리지도,
설정도 아닌 **A10 GPU 노드를 매니지드 온라인 엔드포인트에 할당하는 단계**다.

### 15.1 쿼터는 있는데 왜 안 되나 (가설, 미검증)

> **⚠️ 이 절의 가설은 §22 에서 반증됐다.** 정책 거부가 아니라
> **전용 A100 쿼터가 0** 인 것이 원인이다. 아래는 기록으로만 남긴다.

```
StandardNVADSA10v5Family  limit=72   ← 쿼터는 충분
```

쿼터가 있는데도 노드가 안 붙는다. 유력한 설명은 **테넌트 정책이 N-series
전용(dedicated) 할당을 거부**한다는 것이다. 근거(정황):

- 학습용 A100 클러스터는 **저우선순위(LowPriority)로만** 생성에 성공했다(§2).
- 매니지드 온라인 엔드포인트는 **저우선순위 옵션이 없다** — 항상 전용이다.
- 전용 N-series 생성 시 "not a supported VM size" 류의 오해를 부르는 오류가
  났던 것도 같은 정책 신호였다(§2).

**아직 확정하지 않는다.** 이 문서에서 확신에 찬 문장으로 두 번 틀렸다.
확정하려면 Azure 지원 티켓이나 정책 할당 조회 권한이 필요하다.

### 15.2 배포 시도 총계

| 시도 | SKU | 소요 | 결과 |
|---|---|---|---|
| `ffsft-smoke` | A10 | 68분 | InternalServerError |
| `ffsft-smoke2` | A10 | 114분 | InternalServerError |
| `ffsft-acrtest` | A10 | 113분 | InternalServerError (롤 전파 전 배포) |
| `ffsft-cputest` | **CPU** | **8분** | ImagePullFailed 401 — **원인 규명** |
| `ffsft-pulltest` #1 | **CPU** | **14분** | **Pull 성공**, GPU 없어 기동 실패 |
| `ffsft-pulltest` #2 | A10 | 65분+ | 미도달 → 컷 |

GPU 총 360분(6시간), 약 **$12.9**. CPU 총 22분, 약 **$0.11**.
**원인을 규명한 건 CPU 쪽 22분이다.**

### 15.3 다음에 할 일

1. A10 전용 할당 가능 여부를 Azure 지원/정책 조회로 확정
2. 불가하다면 서빙은 **AKS 연결(Kubernetes online endpoint)** 또는
   **저우선순위 클러스터 + 배치 엔드포인트**로 전환
3. 어느 쪽이든 **CPU SKU로 먼저 pull/기동을 검증**한 뒤 GPU로 올린다

---

## 16. ✅ 학습은 된다 — 막힌 것은 추론뿐이다

사용자 질문: **"지금 학습이 안되는거야 추론이 안되는거야??"**
문서가 아니라 워크스페이스의 잡 이력에서 직접 확인했다.

```
$ az rest --method get --url ".../workspaces/mlw-ffsft/jobs?api-version=2024-10-01"
dreamy_airport_8vfzj212yl  Completed  preflight-Standard_NC24ads_A100_v4  gpu-a100-lp
serene_spade_rctv64wnc0    Completed  preflight-Standard_NC24ads_A100_v4  gpu-a100-lp
```

`dreamy_airport` 의 MLflow 태그가 노드에서 실측한 스택 전체다:

```
device: NVIDIA A100 80GB PCIe   capability 8.0   bf16_supported: True
torch 2.8.0+cu126   transformers 5.15.1   trl 1.10.0   peft 0.20.0   bnb 0.50.1
nf4_matmul_ok: True        ← 가장 위험한 의존성(bitsandbytes CUDA 커널) 통과
preflight.passed: true     ← Qwen3-0.6B QLoRA 실제 학습 스텝 성공
```

### 16.1 두 경로의 차이는 노드 할당 방식 하나다

| | 학습 (AML Job) | 추론 (Managed Online Endpoint) |
|---|---|---|
| 컴퓨트 티어 | **LowPriority 선택 가능** | **선택 불가 — 전용만** |
| 대기→실행 | **3분** (`Queued`→`Running`) | 65분+ `Creating` 후 타임아웃 |
| 로그 | **로컬에서 스트리밍됨** | 종료 후에도 안 나옴 |
| 결과 | Completed | 5회 전부 실패 |

테넌트 정책 `VirtualMachine_SKU_Deny` 의 유일한 예외가 `priority equals "Spot"`
이고(§2), 매니지드 온라인 엔드포인트에는 저우선순위 옵션 자체가 없다. **§15의
가설과 정확히 일치한다.**

> **⚠️ 위 문단의 정책 설명은 §22 에서 반증됐다.** 그런 정책 할당은 존재하지
> 않는다. 표의 관찰(저우선순위 선택 가능 여부가 갈림길이라는 것)은 옳았지만
> 이유가 틀렸다 — 진짜 이유는 **전용 A100 쿼터 0** 이다.

### 16.2 잡 로그는 노트북에서 읽힌다 — 엔드포인트 로그와 다르다

`MLClient.jobs.stream()` 이 실패 원인을 그대로 뱉는다. 엔드포인트 로그가 끝까지
withheld 였던 것과 대조적이다. **학습 디버깅은 추론 디버깅보다 비교할 수 없이 싸다.**

---

## 17. ⛔ `mount_outputs=True` 는 이 워크스페이스에서 반드시 실패한다

`green_kettle_w1zpbvd64q` (Qwen3.8-27B, mount_outputs=True):

```
OrchestrateJobError: Service 'DATA_CAPABILITY' returned code 500:
  data-capability.AssetMountOutputSession.Exception  target: AssetMountOutputSession:model_dir
Failed to mount URI azureml://.../datastores/workspaceblobstore/paths/azureml/<run>/model_dir/
```

### 17.1 이것은 §0에서 뒤집힌 그 진단이 **아니다**

혼동하기 쉬우므로 명시한다. 범위가 완전히 다르다.

| | 틀렸던 진단(§0) | 이번에 실측된 사실(§17) |
|---|---|---|
| 주체 | Azure ML **서비스** | 컴퓨트 **노드**의 FUSE 마운트 세션 |
| 주장 | 스토리지에 아예 못 간다 | 출력 마운트만 안 된다 |
| 근거 | 없음(문서 오독) | 잡 실패 로그 |

`AzureServices` 신뢰 서비스 바이패스는 컨트롤 플레인에 적용되고, **노드가 여는
data-capability 마운트 세션에는 적용되지 않는다.** 같은 노드가 ACR 이미지는
정상적으로 pull 했고 HF 허브에서 가중치도 받았다. 스토리지 계정 **하나**의,
**출력 마운트 한 경로**만 막힌다.

### 17.2 우회는 `./outputs`

`JobSpec.mount_outputs` 의 기본값을 **False 로 뒤집었다.** `./outputs` 는
run-history 아티팩트 서비스가 업로드하며, 이는 마운트가 아닌 별도 경로다.
비용: 노드 할당 + 9 GB 이미지 pull 후 사망 = 약 5분 A100. 27B였다면 54 GB
다운로드까지 마친 뒤 죽었을 것이다.

---

## 18. 스모크런이 27B 한 시간을 아꼈다 — `warmup_ratio`

`quiet_animal_s39032rvj6` (Qwen3.5-0.8B, max_steps=10):

```
TypeError: SFTConfig.__init__() got an unexpected keyword argument 'warmup_ratio'
  at qlora.py:218
```

거기까지 **전부 성공했다**: 모델 다운로드 → NF4 양자화 →
`prepare_model_for_kbit_training` → LoRA 구성 → **한국어 데이터셋(`carrotai_ko_instruction`)
로드** → 챗 템플릿 렌더링. 끊긴 곳은 마지막 한 줄이다.

원인은 transformers v5의 파괴적 변경이다. `warmup_ratio` 가 제거되고
`warmup_steps` 가 1 미만 float 을 비율로 해석하도록 바뀌었다(`huggingface/peft#2949`,
`MIGRATION_GUIDE_V5.md`). trl 도 `max_seq_length` → `max_length` 로 옮겼다.
이미지는 Qwen3.8 때문에 transformers 5.15.1 에 고정돼 있으므로 **이 종류의 이동은
계속 생긴다.**

`qlora.sft_config_kwargs()` 가 이름을 실제 클래스에 대해 해석하고, 대체 이름이
있으면 바꿔 넣고, 둘 다 없으면 경고 후 버린다. 이제 이런 rename 은 로그 한 줄이다.

### 18.1 규칙: 27B 전에 0.8B 를 돌려라

| | 스모크(0.8B) | 실전(27B) |
|---|---|---|
| 가중치 | ~2 GB | ~54 GB |
| 실패까지 | 약 6분 | 약 1시간 추정 |
| 비용 | 약 $0.10 | 약 $1.5 |

§14에서 CPU SKU 가 GPU 배포 원인을 8분에 찾아낸 것과 같은 규칙이다.
**가장 싼 재현 수단에서 먼저 실패시켜라.**

---

## 19. 학습 파이프라인 첫 성공 — `olive_machine_58qllrq6y9`

이미지 `ffsft-train:6`, Qwen3.5-0.8B, 10 스텝. **`FINAL: Completed`.**
프로젝트를 시작한 이래 처음으로 학습이 끝까지 갔다.

```
START 2026-08-21T06:06:29Z   END 2026-08-21T06:11:46Z   (5분 17초)
```

MLflow 에서 읽은 수치 (블롭이 아니라 `report.py` 가 심어놓은 것):

| 키 | 값 | 의미 |
|---|---|---|
| `train.train_loss` | **1.601** | 실제로 학습이 일어났다 |
| `train.steps` | 10 | 요청한 만큼 |
| `train.wall_seconds` | 276.3 | 순수 학습 시간 |
| `train.effective_batch_size` | 16 | batch 1 × grad_accum 16 |
| `train.examples` | 128 | `carrotai_ko_instruction` |
| `train.max_seq_length` | 512 | |
| `train.trainable_params_m` | 5.41 | LoRA rank 8 |
| `train.trainable_pct` | **1.0631** | 전체의 1% 만 학습 = QLoRA 가 의도대로 동작 |
| `train.vram_peak_gb` | **2.79** | / `train.vram_card_gb` 85.1 |
| `train.torch` | 2.8.0+cu126 | |

### 19.1 `report.py` 가 없었으면 이 표는 존재하지 않는다

스트림은 여전히 이렇게 끝난다:

```
Streaming user_logs/std_log.txt
<Error><Code>AuthorizationFailure</Code>...
```

**성공한 런의 stdout 은 노트북에서 읽을 수 없다.** §17 과 같은 스토리지 경계
문제다. 실패는 run-history 의 오류 메타데이터로 진단이 됐지만, 성공은 아무것도
남기지 않는다 — 로그를 못 읽으면 "Completed" 라는 단어 하나가 전부다.
`ffsft/train/report.py` 가 숫자는 `log_metric`, 나머지는 `set_tag` 로 보내고
절대 예외를 던지지 않기 때문에 위 표를 얻었다. **이 워크스페이스에서 유일하게
동작하는 판독 채널이다.**

주의: 로컬에 `mlflow` 가 없으면 읽을 수 없고, run-history REST 의 `rundata` 는
**태그만** 돌려준다(문자열). 숫자는 metric 서비스에 따로 있다. 실제로 통한 방법:

```bash
uv run --with mlflow --with azureml-mlflow python -c "..."   # MlflowClient.get_run
```

### 19.2 세 개의 실제 버그가 이 한 번의 성공을 만들었다

`warmup_ratio` (§18) → trl `_is_vlm` 오판 → 그리고 blob 판독 불가.
셋 다 0.8B 스모크런이 5분 안에 드러냈다. 27B 로 먼저 갔다면 같은 정보에
회당 1시간과 $1.5 를 냈을 것이다.

---

## 20. Qwen3.8-27B 실학습 완료 — `olden_bean_302vkc7nbz`

목표 모델 그 자체를, 한국어 상업이용 안전 믹스로, 실제로 학습시켰다.

```
model  Qwen/Qwen3.8-27B      mix  ko_commercial_safe (340 examples)
rank   16                    seq  1024      batch 1 x grad_accum 16
image  ffsft-train:6         SKU  Standard_NC24ads_A100_v4 (LowPriority)
STATUS Completed
```

| 키 | 값 |
|---|---|
| `train.train_loss` | **1.2637** |
| `train.steps` | 30 |
| `train.wall_seconds` | **2496.4** (41.6분) |
| `setup.vram_after_load_gb` | **17.67** |
| `train.vram_peak_gb` | **28.19** |
| `train.vram_card_gb` | 85.1 |
| `train.trainable_params_m` | **116.73** |
| `train.trainable_pct` | 0.7867 |

### 20.1 하이브리드 어텐션에 QLoRA 가 먹히는가 — 먹힌다

미해결 질문이었다. Qwen3.8-27B 는 64개 층 중 **48개가 Gated DeltaNet** 이고,
bitsandbytes NF4 가 그 층들을 정상 양자화·역전파할지 검증된 적이 없었다.

- 27B(26.9B 파라미터)가 **17.67 GB** 로 적재됐다. bf16이면 약 54 GB 다.
  → NF4 양자화가 실제로 적용됐다.
- 손실이 1.2637 로 **수렴했다**. 양자화된 층을 통과하는 그래디언트가 살아있다.

### 20.2 `lora_target_modules` 를 명시한 것이 116.73M 로 증명된다

`configs/models.yaml` 은 13종 모듈을 명시한다 —
`q/k/v/o_proj` (full-attention 16개 층) + `in_proj_{qkv,z,b,a}` / `out_proj`
(linear-attention 48개 층) + `gate/up/down_proj` (전 층).

PEFT 기본값이었다면 `q/k/v/o_proj` 만 잡아 **48개 층이 조용히 학습에서 빠진다.**
학습 파라미터가 116.73M (전체의 0.79%) 나온 것이 명시 목록이 실제로 걸렸다는
증거다. §9의 `probe_architecture.py` 결과가 런타임에서 확인됐다.

### 20.3 "A100 이 너무 큰 거 아니야? 20GB 로는 안 되나" — 실측 답

| GPU | VRAM | 이 설정(27B/rank16/seq1024)에서 |
|---|---|---|
| T4 / A10 (일부) | 16 GB | ✗ 적재조차 안 됨 (17.67 GB 필요) |
| **RTX 4090 / A10 24GB** | 24 GB | ✗ 적재는 되나 피크 28.19 GB 에서 OOM |
| **A100 40GB / L40S 48GB** | 40–48 GB | ✓ **여유 있음** — 실측 피크의 1.4배 |
| A100 80GB (실사용) | 85.1 GB | ✓ 3배 여유. 과했다 |

**20 GB 로는 안 된다. 40 GB 면 된다.** 다만 80GB 를 쓴 이유는 남아있다 —
이 구독에서 실제로 할당에 성공한 유일한 GPU SKU 가 `NC24ads_A100_v4` 이고,
LowPriority 라 유휴 비용이 0 이다(§16.1). seq 를 2048 로 올리거나 rank 를
키우면 피크는 다시 올라간다.

### 20.4 처리량과 비용

```
2496.4초 / 30스텝 = 83.2초/스텝  (유효 배치 16, seq 1024)
                  = 5.2초/샘플
```

전체 잡은 큐→완료 약 65분(54 GB 다운로드 + 양자화 + 학습).
A100 LowPriority 기준 **약 $1**. 같은 정보를 전용 A100 으로 얻었다면 약 $5 였다.

---

## 21. 평가 단계가 세 번 죽었다 — lm-eval 로더를 우리 코드로 가져온 이유

평가는 학습이 **성공한 뒤에야** 실행된다. 그래서 평가 버그 하나의 값은
`이미지 빌드(7~13분) + 학습 완주(스모크 5분 / 27B 42분)` 이다.
전부 같은 자리, `HFLM.__init__` 에서 터졌다.

### 21.1 세 번의 실패

| 잡 | 이미지 | 예외 |
|---|---|---|
| `hungry_bird_hlyr5cwzl8` | 7 | `TypeError: Qwen3_5ForCausalLM.__init__() got an unexpected keyword argument 'load_in_4bit'` |
| `ashy_hamster_9lvxsm0y2s` | 8 | `TypeError: HFLM._create_model() got multiple values for keyword argument 'quantization_config'` |
| `calm_giraffe_dk4cm4gk9y` | 8 | 위와 동일 |

세 번 모두 학습은 정상이었다 — `train_loss` 1.6015 / 1.6009 / 1.6014.

**1번**: transformers v4 는 `load_in_4bit` 를 가로채 `BitsAndBytesConfig` 로
바꿔줬고, v5 가 그 shim 을 삭제했다. lm-eval 은 모르는 키를
`from_pretrained` 로 그대로 흘려보내므로 모델 생성자까지 내려가 터진다.

**2번**: `quantization_config` 로 바꿔 불렀더니 이번엔 중복이었다.
`lm_eval/models/huggingface.py` 를 직접 읽어 원인을 확정했다:

```python
# HFLM.__init__, line ~359 — 로컬 변수에 대한 walrus
if (quantization_config := getattr(self.config, "quantization_config", None)
   ) is not None and isinstance(quantization_config, dict):
    quantization_config = AutoQuantizationConfig.from_dict(quantization_config)

self._create_model(..., quantization_config=quantization_config, **kwargs)  # line ~384
```

`quantization_config` 는 **`__init__` 의 파라미터가 아니다.** 이미 양자화된
체크포인트(AWQ/GPTQ 등)가 자기 `config.json` 에 실어 보내는 값을 읽어
lm-eval 이 **스스로** 넘기는 이름이다. 우리 것은 `**kwargs` 로 들어가
같은 자리에 두 번 도달한다. **이 버전의 HFLM 에는 즉석 bitsandbytes
양자화를 요청할 통로가 아예 없다.**

**3번**은 진단이 아니라 내 착각이었다. 수정을 커밋하고 재제출했는데
트레이스백이 여전히 옛 줄번호(`line 169, lm = HFLM(**kwargs)`)를 가리켰다.
경로가 답이었다 — `/opt/ffsft/src/ffsft/eval/run.py`. **소스는 이미지에
구워져 있다**(§17: `code=` 업로드가 스토리지 방화벽에 막혀 `COPY . /opt/ffsft`
로 대체됐다). 즉 **파이썬 한 줄만 바꿔도 ACR 재빌드가 필수**다.
"코드는 잡과 함께 업로드된다"는 가정이 틀렸다.

### 21.2 고친 방법 — 모델을 우리가 만들어 넘긴다

```python
def __init__(
    self,
    pretrained: str | transformers.PreTrainedModel,
    backend: Literal["default", "causal", "seq2seq"] = "default",
    tokenizer: str | PreTrainedTokenizer | PreTrainedTokenizerFast | None = None,
    ...
```

```python
# line ~218
if not isinstance(pretrained, str):
    self._model = pretrained
    self._device = self._model.device
    self._config = self._model.config
    gpus = 0
else:
    ...
if isinstance(pretrained, str):      # line ~367
    self._create_model(...)          # ← 문제의 로더. 객체면 아예 안 탄다
```

그래서 `ffsft.eval.run.load_for_eval()` 이 직접 적재하고 HFLM 에는 **객체**를
넘긴다. 얻는 것이 세 가지다.

1. `_create_model` 을 타지 않으므로 인자 배관 문제가 통째로 사라진다.
2. `.to(self.device)` 도 `isinstance(pretrained, str)` 로 가드돼 있어
   bitsandbytes 모델이 거부하는 이동이 일어나지 않는다.
3. 양자화를 **학습과 글자 그대로 동일하게** 맞출 수 있다 —
   NF4 + double-quant + `device_map={"": 0}` + `attn_implementation="sdpa"`.
   §20 에서 27B 를 28.19 GB 로 돌린 바로 그 조합이다.

어댑터도 `PeftModel.from_pretrained` 로 직접 붙인다. lm-eval 의 `peft=`
인자는 `_create_model` 안에서만 처리되는데, 그 함수를 안 타기 때문이다.

> **평가를 bf16 으로 하지 않은 이유.** 27B bf16 은 54 GB 로 85 GB 카드에
> 들어가긴 한다. 그러나 QLoRA 어댑터를 bf16 으로 재는 것은 **서빙되지 않을
> 구성**을 재는 것이고, 양자화 오차가 우리가 찾으려는 파인튜닝 델타와
> 같은 자릿수다.

### 21.3 같은 계열을 빌드에서 잡도록 만들었다

`docker/verify_stack.py` 가 이제 계약을 빌드 시점에 검증한다. 오프라인이고
비용이 0 이다.

- `pretrained` / `backend` / `tokenizer` / `max_length` / `batch_size` 존재
- `pretrained` 의 어노테이션에 `PreTrainedModel` 포함
- **`quantization_config` 가 여전히 부재**할 것 —
  lm-eval 이 나중에 이 인자를 받기 시작하면 우리 것이 또 두 번 가게 된다

세 번의 실패는 전부 "GPU 20분 왕복"으로만 알 수 있던 것들이었다.
이제 이미지 레이어에서 걸린다.

### 21.4 벤치마크 태스크 이름은 검증 대상이다

`configs/benchmarks.yaml` 이 존재하지 않는 태스크를 두 개 들고 있었다.
업스트림 `EleutherAI/lm-evaluation-harness` 를 직접 확인한 결과:

| 설정에 있던 이름 | 실제 |
|---|---|
| `ifeval_ko` | **없음.** 하네스는 영어 `ifeval` 만 제공 |
| `hae_rae_bench` | **`haerae`** |
| `kobest`, `kmmlu` | 정상 (그룹) |

틀린 이름은 `simple_evaluate` 안에서야 터지는데, 그 시점은 **모델을 이미
내려받아 양자화한 뒤**다. 27B 면 수 분의 A100 을 태우고 YAML 오타를 배우는
셈이다. `unknown_harness_tasks()` 가 적재 **전에** 전부 모아서 거절한다.
`TaskManager().all_tasks` 는 태스크·그룹·태그를 모두 포함하므로 `kobest`
같은 그룹명도 오탐하지 않는다.

### 21.5 ✅ 파이프라인 첫 관통 — `hungry_bell_lpf45kx8kv`

이미지 9, `qwen3.5-0.8b`, 10스텝, `eval_suite=ko_fast`, `eval_limit=5`.
**학습 → 어댑터 → base 평가 → tuned 평가 → 델타** 가 한 잡에서 끝났다.

```
train.train_loss = 1.6009
eval.kobest.base = 0.4    eval.kobest.tuned = 0.4    eval.kobest.delta = 0.0
eval.kobest_boolq      0.8 / 0.8 / 0.0
eval.kobest_copa       0.4 / 0.4 / 0.0
eval.kobest_hellaswag  0.4 / 0.4 / 0.0
eval.kobest_sentineg   0.6 / 0.6 / 0.0
eval.kobest_wic        0.6 / 0.6 / 0.0
```

**델타 0 은 여기서 정상이다.** `limit=5` 면 눈금이 0.2 이고, 128 샘플 10스텝
LoRA 가 그 눈금을 움직일 리 없다. 이 잡이 증명하는 것은 점수가 아니라
**배관**이다 — 어댑터가 같은 노드에서 평가로 넘어가고(§17 회피), base 와
tuned 가 동일 조건으로 측정되며, 두 모델이 순차 적재돼도 OOM 이 없다.

### 21.6 평가가 별도 잡이 될 수 없는 이유

어댑터를 다른 잡으로 넘기려면 `workspaceblobstore` 를 거쳐야 하는데,
그 경로가 §17 에서 실패가 증명된 바로 그 경로다. 그래서 `build_command` 가
`train && eval` 로 **한 노드 안에서** 이어 붙인다. 부수 효과로 54 GB
다운로드도 한 번만 일어난다.

---

## 22. ✅ 서빙 블로커 확정 — **전용 A100 쿼터가 0 이다**

§15.1 과 §16.1 은 "테넌트 정책이 전용 N-series 를 거부한다"는 **가설**을
적어두고 미검증으로 남겨뒀다. 이제 확정한다. **가설은 틀렸고, 진짜 원인은
훨씬 단순했다.**

### 22.1 정책은 원인이 아니다 — 조회로 반증

```
$ az policy assignment list --scope /subscriptions/<sub> --disable-scope-strict-match
Defender for Containers provisioning Policy extension for Arc-enabled ...
Defender for Containers provisioning Azure Policy Addon for Kubernetes ...
Defender for Containers provisioning ARC k8s Enabled
ASC OpenSourceRelationalDatabasesProtection
ASC DataProtection
Defender for SQL Servers on Machines provisioning
```

전부 Defender/ASC 프로비저닝 정책이다. **`VirtualMachine_SKU_Deny` 는 구독
어디에도 할당돼 있지 않다.** 관리그룹(Tenant Root Group)까지 조회해도 없고,
`policy state` 에 SKU/Deny 관련 비준수 레코드도 없다.

§2 에서 본 그 문자열은 **정책 목록이 아니라 오류 메시지**였다. 정황을
원인으로 승격시킨 것이 잘못이었다.

### 22.2 진짜 원인 — API 가 직접 말해준다

전용 A100 클러스터 생성을 시도했다.

```
ClusterMinNodesExceedCoreQuota:
"The specified subscription has a Standard NCADSA100v4 family vCPU quota of 0
 and cannot accomodate for at least 1 requested managed compute nodes which
 maps to 24 vCPUs."
```

**전용(dedicated) A100 쿼터가 문자 그대로 0 이다.**

쿼터 API 가 같은 말을 한다.

```
$ az rest ... /providers/Microsoft.MachineLearningServices/locations/koreacentral/quotas
   0  Standard NCADSA100v4 Family Cluster Dedicated vCPUs   ← 학습에 쓰는 그 패밀리
  72  Standard NVADSA10v5 Family Cluster Dedicated vCPUs
 100  Standard NC Family Cluster Dedicated vCPUs            ← K80. 27B 불가
```

그리고 저우선순위 쪽은 넉넉하다.

```
TotalLowPriorityCores        24 / 300     ← 24 는 지금 도는 27B 잡
standardNCADSA100v4Family    24 / -1      ← 제한 없음
TotalDedicatedCores           0 / 1072
```

### 22.3 통제 실험 — SKU 와 tier 를 각각 바꿔봤다

| SKU | tier | 결과 |
|---|---|---|
| `Standard_NC24ads_A100_v4` | LowPriority | ✅ 생성됨 (= 지금 쓰는 `gpu-a100-lp`) |
| `Standard_NC24ads_A100_v4` | **Dedicated** | ❌ `ClusterMinNodesExceedCoreQuota` (**쿼터 0**) |
| `Standard_NV18ads_A10_v5` | Dedicated | ❌ `InvalidPropertyValue` |
| `Standard_NV18ads_A10_v5` | LowPriority | ❌ `InvalidPropertyValue` |

A10 은 **tier 와 무관하게** 거부된다. 흥미로운 건 `compute.list_sizes()` 에는
16개 GPU SKU 가 A10 6종을 포함해 전부 나온다는 점이다 —
**카탈로그에 보이는 것과 실제로 만들 수 있는 것은 다르다.**
`InvalidPropertyValue` 가 돌려주는 "지원 VM 목록"은 D/DS/F/NC/NCv2/NCv3/ND/NV
같은 **구형 SKU 뿐**이라 더 헷갈린다 — 우리가 지금 잘 쓰고 있는
`NC24ads_A100_v4` 조차 그 목록에 없다. **저 메시지는 믿을 게 못 된다.**

### 22.4 결론 — 왜 매니지드 온라인 엔드포인트가 불가능한가

세 사실을 곱하면 끝이다.

1. 매니지드 온라인 엔드포인트는 **항상 전용**이다. 저우선순위 옵션이 없다.
2. 이 구독에서 만들 수 있는 유일한 최신 GPU 패밀리는 `NCADSA100v4` 인데,
   그 **전용 쿼터가 0** 이다.
3. 쿼터가 72 로 남아있는 `NVADSA10v5` 는 tier 를 뭘 주든 생성이 거부된다.

⇒ **이 구독·리전에서 GPU 매니지드 온라인 엔드포인트는 만들 수 없다.**
권한도, 이미지도, 스토리지도, vLLM 설정도 원인이 아니었다. §15 에서 A10
엔드포인트가 65~114분 `Creating` 에 머물다 죽은 것도 이걸로 설명된다 —
엔드포인트는 SKU 를 받아줬지만 노드는 영원히 오지 않았다.

### 22.5 그래서 학습은 왜 되나

`gpu-a100-lp` 가 **LowPriority** 이기 때문이다. 저우선순위 A100 코어는
300개가 열려 있고 패밀리 제한은 아예 없다(`-1`). 학습은 잡이라 저우선순위를
고를 수 있고, 추론(매니지드 엔드포인트)은 고를 수 없다. **차이는 그 한 줄이다.**

### 22.6 열어둘 길

쿼터 0 은 코드로 못 넘는다. 남은 선택지는 셋이고, 전부 이 저장소의 코드가
이미 지원하거나 작은 변경으로 닿는다.

| 선택지 | 필요한 것 | 비고 |
|---|---|---|
| **전용 A100 쿼터 상향 요청** | 지원 티켓 | 가장 곧은 길. 승인되면 `ffsft-lifecycle up` 이 그대로 동작한다 |
| **배치 엔드포인트** | 코드 소폭 추가 | `gpu-a100-lp` 를 그대로 쓴다 — **저우선순위라 쿼터 문제가 없다** |
| **AKS 연결** | AKS 클러스터 | GPU 노드풀도 같은 전용 쿼터를 먹는다. 같은 벽일 가능성이 높다 |

실시간 온라인 서빙이 목표라면 **1번 외에는 우회로가 없다.**
처리량 위주라면 **2번이 오늘 당장 가능하다.**

---

## 23. ✅ 27B 학습 + 평가 완주 — `heroic_fennel_085y2rwm3s`

이 자산이 목표로 삼은 것 전부가 **한 잡 안에서** 끝났다.
Qwen3.8-27B 를 QLoRA 로 학습하고, 그 어댑터로 base/tuned 를 같은 조건에서
평가해 델타까지 냈다.

```
설정: qwen3.8-27b / ko_commercial_safe / 30스텝 / seq 1024 / rank 16
      grad_accum 16 / eval_suite=ko_fast / eval_limit=25
이미지: ffsft-train:9   컴퓨트: gpu-a100-lp (A100 80GB, LowPriority)
```

### 23.1 학습 — 재현된다

```
train.train_loss          = 1.2638      ← 직전 27B 런 1.2637
train.wall_seconds        = 2540.2      (42.3분, 30스텝)
train.vram_peak_gb        = 28.19
setup.vram_after_load_gb  = 17.67       (bf16 이면 ~54 GB)
train.trainable_params_m  = 116.73      (0.7867%)
setup.examples            = 340
```

`olden_bean_302vkc7nbz`(§20) 의 `1.2637` 과 **소수 넷째 자리까지 일치**한다.
같은 데이터·같은 시드·같은 이미지 계열에서 학습이 결정적이라는 뜻이고,
이건 우연히 한 번 된 게 아니라는 가장 값싼 증거다.

### 23.2 평가 — base 대 tuned

`eval_limit=25` 이므로 태스크당 **n=25**, 눈금은 0.04 다.

| 태스크 | base | tuned | delta |
|---|---|---|---|
| `kobest_boolq` | 0.72 | **0.88** | **+0.16** |
| `kobest_sentineg` | 0.96 | **1.00** | **+0.04** |
| `kobest_copa` | 0.84 | 0.84 | 0.00 |
| `kobest_hellaswag` | 0.80 | 0.80 | 0.00 |
| `kobest_wic` | 0.48 | 0.48 | 0.00 |
| `kobest` (그룹) | 0.80 | 0.80 | 0.00 |

### 23.3 ⚠️ 이 숫자로 "성능이 좋아졌다"고 말하면 안 된다

**n=25 에서 +0.16 은 노이즈와 구분되지 않는다.**

```
p≈0.8, n=25 → 표준오차 = sqrt(0.8*0.2/25) = 0.08
95% 신뢰구간 ≈ ±0.157
관측된 최대 델타 = +0.16   ← 신뢰구간 경계에 걸쳐 있다
```

`boolq` 의 +0.16 은 **25문항 중 4문항** 더 맞힌 것이고, `sentineg` 의 +0.04 는
**1문항**이다. 340개 예제로 30스텝 돌린 LoRA 가 실제로 무언가를 바꿨을 수도
있지만, **이 실험은 그걸 증명하지 못한다.**

주장을 하려면 `eval_limit` 을 없애고 전체 테스트 스플릿으로 돌려야 한다.
`ko_fast`(kobest) 전체는 수천 문항이고, 27B 를 두 번 적재해 채점하면
A100 LowPriority 로 대략 2~4시간이다.

**이 잡이 증명하는 것은 점수가 아니라 측정 장치다** — base 와 tuned 가
동일한 양자화·동일한 하네스·동일한 노드에서 채점되고, 델타가 MLflow 로
자동 기록되며, 27B 두 벌을 순차 적재해도 85 GB 카드에서 OOM 이 없다.
표본만 늘리면 그대로 유효한 실험이 된다.

### 23.4 왜 그룹 점수는 안 움직였나

`kobest` 는 그룹이라 하위 5개와 별도로 자기 표본을 채점한다. 하위에서
`boolq` 만 움직였으므로 그룹 평균이 눈금(0.04) 아래로 희석된 것으로 보인다.
표본을 늘리기 전에는 이 이상 말할 근거가 없다.

### 23.5 비용

| 항목 | 실측 |
|---|---|
| 학습 | 2540.2초 (42.3분) |
| 전체 잡 | 54 GB 다운로드 + 학습 + 27B 2회 적재·채점 |
| 요금 | A100 LowPriority, 대략 **$1.5** |

평가를 별도 잡으로 쪼갰다면 54 GB 다운로드가 한 번 더 일어나고
어댑터가 `workspaceblobstore` 를 거쳐야 했다 — §17 에서 실패가 증명된 경로다.
`train && eval` 체이닝(§21.6)이 그 둘을 동시에 없앴다.

---

## 24. ⛔ 스토리지 — 사용자가 계속 물어본 그 문제의 정체

> "스토리지 이슈가 뭔지 이해를 못했어. 디플로이가 안 되는 거야?
> 내 로컬로 뭘 갖고 올 필요가 없잖아. 모델 웨이트나 학습 코드 추론 코드는
> 다 블롭에 있으면 되는 거 아니야? 뭐가 문제였어?"

맞는 지적이었다. 블롭에 있으면 된다. **문제는 블롭에 아무것도 넣을 수 없고,
넣을 수 없다는 사실이 지금까지 증상으로만 보였다는 것이다.**

### 24.1 한 줄 원인

```
mlwffsftstorage8cb451dd1
  publicNetworkAccess       = Disabled
  privateEndpointConnections = []          ← 0개
  networkAcls.defaultAction  = Allow       ← 무의미
  networkAcls.bypass         = AzureServices ← 무의미
```

공용 엔드포인트가 꺼져 있고 프라이빗 엔드포인트도 없다.
`defaultAction=Allow` 는 **공용 엔드포인트가 켜져 있을 때** 누구를 들일지
정하는 규칙이라, 엔드포인트 자체가 꺼져 있으면 아무 효과가 없다.
이 계정은 지금 **어디에서도 접근할 수 없다** — 내 노트북도, Azure ML 컴퓨트
노드도.

### 24.2 그래서 지금까지 본 증상 전부가 이거 하나였다

| 증상 | 기록 위치 |
|---|---|
| `code=` 클라이언트 업로드 거부 → 소스를 이미지에 굽게 됨 | §17, §21.2 |
| `mount_outputs=True` 가 노드 셋업에서 실패 | `aml_job.py` 주석 |
| `./outputs` 아티팩트 업로드가 조용히 0건 | 아래 |
| 잡 출력으로 모델 등록 불가 | 아래 |
| 배치 엔드포인트 배포 불가 | §24.5 |

아티팩트 API 로 완료된 런 3개를 직접 조회했다:

```
heroic_fennel_085y2rwm3s: HTTP 200 artifacts=0
hungry_bell_lpf45kx8kv  : HTTP 200 artifacts=0
olden_bean_302vkc7nbz   : HTTP 200 artifacts=0
```

**학습된 어댑터는 노드 로컬 디스크에서 태어나 노드와 함께 사라진다.**
그래서 모델 등록이 이렇게 끝난다:

```
azureml://jobs/heroic_fennel_085y2rwm3s/outputs/artifacts/paths/outputs/
  → (NoMatchingArtifactsFoundFromJob) No artifacts matching outputs found from Job
azureml://jobs/heroic_fennel_085y2rwm3s/outputs/default/paths/outputs/
  → (NoMatchingOutputFoundFromJob) Job output default not found
```

### 24.3 고치려고 했고, 고칠 수 없다

```bash
az storage account update -n mlwffsftstorage8cb451dd1 -g rg-ffsft-kc \
    --public-network-access Enabled
# exit=0  →  publicNetworkAccess: Disabled     (그대로)

az rest --method patch --url ".../storageAccounts/mlwffsftstorage8cb451dd1" \
    --body '{"properties":{"publicNetworkAccess":"Enabled"}}'
# HTTP 200, provisioningState: Succeeded
#   "publicNetworkAccess": "Disabled"          (그대로)
```

ARM 이 요청을 받아들이고 성공을 반환한 뒤 **값을 바꾸지 않는다.**

결정적 확인 — 아예 새 스토리지 계정을 만들어 봤다:

```bash
az storage account create -n stffsftserve01 -g rg-ffsft-kc \
    --public-network-access Enabled ...
# → publicNetworkAccess: Disabled
```

**명시적으로 Enabled 를 요구하며 만든 계정이 Disabled 로 태어난다.**
(확인 후 즉시 삭제했다.)

### 24.4 누가 강제하는가 — 조회되지 않는다

| 확인한 것 | 결과 |
|---|---|
| 리소스그룹 deny assignment | **0개** |
| 구독 스코프 정책 중 network/storage/public 관련 | 없음 |
| 관리그룹 스코프 (Tenant Root Group) 동일 검색 | 없음 |
| 이 계정에 걸린 비준수 정책 5건 | 전부 *audit* — "Storage accounts should use private link" 류 |

audit 정책은 막지 않는다. 즉 **내 자격 증명으로 열거할 수 없는 상위 계층에서
강제된다.** §22 의 A100 전용 쿼터 0 과 §15 의 A10 SKU 거부와 같은 성격의
테넌트 통제로 보이며, 셋 다 코드로 넘을 수 없다.

### 24.5 결론 — 온라인뿐 아니라 배치도 막혀 있다

이전까지 `ffsft-deploy check` 는 배치 패턴을 `ok` 로 표시했다.
쿼터만 봤기 때문이다. **AML 배포는 온라인이든 배치든 등록된 모델 자산을
입력으로 받는다.** 모델 자산은 데이터스토어 안의 경로이고, 접근 가능한
데이터스토어가 없으면 LowPriority 쿼터가 아무리 많아도 배포할 수 없다.

`check_pattern` 에 데이터스토어 검사를 추가한 뒤의 실측:

```
  datastore  UNREACHABLE  mlwffsftstorage8cb451dd1 (publicNetworkAccess=Disabled, 0 private endpoints)

  aks_vllm         BLOCKED   no reachable datastore: ...
  aml_batch        BLOCKED   no reachable datastore: ...
  aml_batch_vllm   BLOCKED   no reachable datastore: ...
  aml_online_vllm  BLOCKED   no reachable datastore: ...
  local_vllm       n/a       (local, no Azure quota involved)
```

**이 구독에서 서빙을 막는 것은 두 개의 독립된 벽이다.**

| 벽 | 무엇을 막나 | 코드로 우회 가능? |
|---|---|---|
| 전용 GPU 쿼터 = 0 (§22) | 관리형 온라인 엔드포인트 | ✗ |
| 데이터스토어 도달 불가 (§24) | 온라인·배치·AKS **전부** | ✗ |

### 24.6 그럼 학습은 왜 되나

**학습 잡은 스토리지를 한 번도 건드리지 않기 때문이다.**

```
소스   → 이미지에 구움 (COPY . /opt/ffsft)     — 업로드 없음
데이터 → 노드에서 HF Hub 로 직접 다운로드       — 스토리지 무관
가중치 → 노드에서 HF Hub 로 직접 다운로드       — 스토리지 무관
어댑터 → 노드 로컬 디스크                       — 업로드 없음
평가   → 같은 잡에서 그 로컬 디스크를 읽음      — 업로드 없음
지표   → MLflow 런히스토리 (별개 서비스)        — 스토리지 무관
```

§21.6 에서 학습과 평가를 한 잡으로 묶은 것은 시간을 아끼려던 결정이었는데,
**결과적으로 이 구독에서 파이프라인이 동작하는 유일한 이유가 됐다.**
두 잡으로 나눴다면 두 번째 잡이 어댑터를 스토리지에서 읽어야 했고,
그 경로는 존재하지 않는다.

### 24.7 실제 해결책 (미검증 — 이 구독에서 실행하지 않음)

강제를 이길 수 없으므로 **강제를 만족시키는 방향**이 유일한 길이다:

1. VNet + 서브넷 생성
2. 스토리지 계정에 **프라이빗 엔드포인트** 연결 (그러면 `Disabled` 가 정상 posture 가 된다)
3. AML 컴퓨트를 그 VNet 에 주입
4. 그 위에서 배치 엔드포인트 배포 — LowPriority 쿼터는 이미 있다

온라인 엔드포인트는 이걸 해도 **여전히 막힌다** (§22, 전용 쿼터 0).
이 절차는 이 문서의 다른 내용과 달리 **실행해서 확인하지 않았다.**

---

## 25. ✅ 서빙 + 로드테스트 — 유일하게 열린 경로에서 실제로 돌렸다

§22(전용 쿼터 0)와 §24(데이터스토어 도달 불가)로 Azure 위의 서빙 패턴 4개가
전부 막혔다. 남은 것은 `local` 하나였고, 그마저 vLLM 은 GPU 를 요구하는데
이 머신에는 GPU 가 없다. 그래서 **CPU `transformers` 엔진을 로컬 패턴의
형제로 추가**했다 — `src/ffsft/serve/local.py`.

빠르라고 만든 게 아니다. **로드테스트 하네스가 한 번도 살아있는 엔드포인트에
붙어본 적이 없었고**, 붙어보지 않은 하네스는 동작을 모르는 하네스다.

### 25.1 첫 시도 — HTTP 200 열두 번, 실패 열두 번

```
INFO  httpx | POST /v1/chat/completions "HTTP/1.1 200 OK"   ← ×12
INFO  ffsft.loadtest |   ok=0 fail=4 | TTFT p50 0.000s | 0.0 tok/s
WARNING ffsft.loadtest |   errors: {'no tokens streamed': 4}
```

서버는 정상 응답했고 하네스는 전부 실패로 셌다. 이유는 하네스가 옳다:

```python
# loadtest._one_request
"stream": True,
"stream_options": {"include_usage": True},
...
if tokens == 0:
    return RequestResult(ok=False, error="no tokens streamed", ...)
```

**TTFT 는 스트리밍 없이 측정할 수 없다.** 통짜 JSON 응답에서 "첫 토큰까지의
시간"은 곧 전체 지연이고, 그건 TTFT 가 아니라 다른 숫자다. 하네스가
비스트리밍 엔드포인트를 실패로 처리하는 것은 버그가 아니라 정의다.

→ `TextIteratorStreamer` + `StreamingResponse` 로 SSE 를 구현했다.
프레임 모양은 순수 함수(`sse_chunk` / `sse_usage` / `SSE_DONE`)로 분리해
모델을 적재하지 않고 테스트한다(테스트 6개).

`sse_usage` 의 `choices` 가 빈 리스트인 것은 실수가 아니다. 클라이언트는
delta 가 있는 프레임마다 토큰을 하나씩 세므로, usage 프레임에 delta 를 넣으면
토큰이 이중 계산된다.

### 25.2 실측 — Qwen3.5-0.8B, CPU 8코어, float32

```
ffsft-loadtest --base-url http://127.0.0.1:8011/v1 \
  --model Qwen/Qwen3.5-0.8B --concurrency 1,2,4 \
  --requests-per-level 4 --max-tokens 16 --ttft-slo 5.0
```

```
 conc    ok  fail  TTFT p50  TTFT p95  TPOT p50   e2e p95     tok/s    req/s
----------------------------------------------------------------------------
    1     4     0     1.396     2.639    0.4837     4.924       1.7     0.27
    2     4     0     1.692     2.052    0.5495     4.281       2.9     0.47
    4     4     0     2.863     3.545    0.8904     7.114       3.5     0.56

Max concurrency meeting p95 TTFT <= 5.0s: 4 (3.5 output tok/s, 0.56 req/s)
```

**12/12 성공.** 그리고 모양이 교과서적이다:

| 동시성 | 처리량 배수 | TPOT |
|---|---|---|
| 1 → 2 | ×1.71 | 0.484 → 0.550 (+14%) |
| 2 → 4 | ×1.21 | 0.550 → 0.890 (+62%) |

동시성을 2배 올릴 때 처리량은 1.71배 → 1.21배로 체감하고 토큰당 시간은
14% → 62% 로 악화된다. **포화가 1과 4 사이에서 일어난다**는 뜻이고,
CPU 스레드 경합이 원인이다. GPU 서빙이라면 같은 곡선을 훨씬 오른쪽에서
그리겠지만, 곡선을 읽는 방법과 하네스는 동일하다.

### 25.3 품질에 대한 정직한 관찰

비스트리밍 경로로 같은 모델에 한국어를 물었다:

```
Q: 한국의 수도는 어디이고 왜 그곳이 수도가 되었는지 두 문장으로 설명해줘.
A: 한국의 수도는 서울입니다. 이 도시가 수도로 된 이유는 1946년 1월 1일,
   일본군 12만 명이 유입된 후, 당시 일본군 12만 명이 유...
   (48 tokens, 14.9s ≈ 3.2 tok/s)
```

첫 문장은 맞고 그다음부터 환각에 반복까지 붙는다. 이건 **0.8B 베이스
모델에게 기대할 만한 결과**이고, 이 저장소가 존재하는 이유를 한 화면에
보여준다 — 한국어 파인튜닝과 벤치마크가 필요한 이유가 바로 이것이다.

### 25.4 이 절이 증명하는 것과 증명하지 못하는 것

| | |
|---|---|
| ✅ 로드테스트 하네스가 실제로 동작한다 | 12/12, 포화 곡선, SLO 무릎 탐지 |
| ✅ 서빙 패턴이 실행 가능한 코드다 | 문서가 아니라 `ffsft-serve-local` |
| ✅ OpenAI 와이어 프로토콜 호환 | SSE, `delta.content`, `[DONE]`, usage |
| ⛔ 파인튜닝된 27B 를 서빙한 것은 아니다 | 어댑터가 노드를 못 벗어난다(§24.2) |
| ⛔ 프로덕션 처리량 수치가 아니다 | CPU, 배치 없음, 페이지드 어텐션 없음 |

비용 **$0** — Azure 리소스를 하나도 쓰지 않았다.

---

## 26. ✅ 서빙 벽이 뚫렸다 — A10 온라인 엔드포인트 + Hub 직접 로드

§22는 "전용 A100 쿼터 0"을, §24는 "스토리지 도달 불가"를 서빙 블로커로 확정했다.
둘 다 사실이었지만, **둘 다 우회 가능했다**. 이 절은 그 우회로를 실제로 뚫은 기록이다.

### 26.1 벽 두 개, 우회로 두 개

| 벽 | §에서 확정한 내용 | 우회로 |
|---|---|---|
| 전용 GPU 쿼터 | A100 = 0 코어 | **A10 = 36 코어로 승인됨.** 요청이 나중에 통과했다 |
| 스토리지 | 모델 자산 등록 불가 | **vLLM `--model <hf repo>`** — 컨테이너가 Hub에서 직접 받는다 |

두 번째가 핵심이다. Azure ML 배포는 보통 모델 자산을 이름으로 지정하고 플랫폼이
마운트한다. 그러면 데이터스토어를 반드시 탄다. 그런데 vLLM은 **서빙 프로세스가
스스로 가중치를 해결**한다. `--model Qwen/Qwen3.5-0.8B` 를 주면 컨테이너가 뜨면서
Hub에서 내려받는다. 데이터스토어는 아예 등장하지 않는다.

이건 학습이 이 구독에서 돌아간 이유(§24.6)와 정확히 같은 원리다. 스토리지를
안 타면 스토리지 벽에 안 막힌다.

### 26.2 AmlCompute와 온라인 엔드포인트는 SKU 카탈로그가 다르다

§22에서 A10 v5는 **AmlCompute 클러스터** 생성 시 두 티어 모두 `InvalidPropertyValue`
로 거부됐다. 그래서 A10은 못 쓰는 걸로 정리했다. 그건 **클러스터에 한정된 사실**이었다.

관리형 온라인 엔드포인트는 별개 카탈로그를 쓴다. 측정 (2026-08-21):

```
엔드포인트  ffsft-a10                 provisioningState = Succeeded
배포        blue                      instanceType      = Standard_NV12ads_A10_v5
                                      → 수락됨
```

같은 SKU 계열이 한쪽에서는 거부되고 한쪽에서는 수락된다. **표면이 다르면 다시
측정해야 한다**는 교훈.

### 26.3 쿼터 산수 — 기본 SKU로는 못 올린다

온라인 엔드포인트는 롤링 업데이트분까지 잡으므로 **요청 코어의 2배**가 필요하다.

| SKU | 코어 | 실제 필요 | A10 36코어에서 |
|---|---|---|---|
| `Standard_NV6ads_A10_v5` | 6 | 12 | 가능 |
| `Standard_NV12ads_A10_v5` | 12 | 24 | **가능 — 사용한 것** |
| `Standard_NV18ads_A10_v5` | 18 | 36 | 딱 맞음 |
| `Standard_NV36ads_A10_v5` | 36 | 72 | **불가 — 그런데 이게 기본값이었다** |

`configs/serving.yaml` 의 `aml_online_vllm.default_sku` 가 NV36이라, 아무 것도
안 바꾸고 배포하면 승인된 쿼터를 갖고도 막힌다.

### 26.4 첫 배포는 AcrPull로 죽었다 — 그리고 프리체크는 침묵했다

첫 시도는 약 10분 뒤 이렇게 끝났다:

```
(BadArgument) Endpoint identity does not have pull permission on the registry.
```

측정한 엔드포인트 ID 권한:

```
principal fbd167d1-f592-470d-8d57-25ff85790033  (SystemAssigned)
  AzureML Metrics Writer (preview)  on workspace
  Storage Blob Data Reader          on mlwffsftstorage8cb451dd1
  (ACR 권한 없음)
```

Azure는 **워크스페이스 연결 ACR에만** AcrPull을 자동 부여한다. 이 워크스페이스는
`properties.containerRegistry` 가 비어 있어서 `acrffsftkc` 는 커스텀 레지스트리다.
그래서 아무도 권한을 안 준다.

**진짜 문제는 이걸 잡으라고 쓴 프리체크가 침묵했다는 것이다.**
`read_identity_grants` 는 엔드포인트가 없으면(404) `None` 을 돌려준다 — "모르는
것으로 막지 않는다"는 원칙상 맞다. 그런데 `deploy_online` 이 그 검사를
**엔드포인트 생성보다 먼저** 호출하고 있었다. 그래서 신규 엔드포인트에서는 항상
404 → 항상 `None` → 절대 안 막힌다. **권한이 없는 게 확실한 유일한 경우에
구조적으로 눈이 멀어 있었다.**

고친 방식:

- 검사를 **엔드포인트 생성 이후**로 옮겼다. 엔드포인트 리소스 자체는 무료고,
  GPU를 잡는 건 배포뿐이다. 그 사이가 정확히 검사할 자리다.
- `ensure_acr_pull()` 이 권한을 **직접 부여**한다. 이미 인증된 도구가 결정론적인
  명령 하나를 사람에게 시키는 건 개선이라 부르기 어렵다.
- RBAC 쓰기 권한이 없으면 예외를 던지지 않고 실행할 `az` 명령을 출력한다.
  잠긴 구독에서는 그게 정상 상황이다.

### 26.5 CLI에 구멍이 있었다

`deploy_online()` 은 `hf_model=` 을 받은 지 오래였는데, 인자 파서가
`--model-uri` 를 **필수**로 강제하고 있었다. 스토리지 벽을 넘는 유일한 경로가
코드에는 있는데 명령줄에서는 못 쓰는 상태였다.

`--hf-model` 을 추가하고 `--model-uri` 를 선택으로 바꿨다. 둘 다 없거나 둘 다
주면 에러다 — 배포가 알아서 하나를 고르게 두지 않는다.

### 26.6 `ffsft-deploy check` 가 과잉 차단하고 있었다

§24 이후 `requires_model_asset` 이 LOCAL 아닌 모든 표면에 True 였다. 그래서
Hub로 서빙 가능한 온라인 패턴까지 BLOCKED 로 보고했다. 실제로는 배포된다.

`ServingSpec.can_serve_from_hub` 로 "서버가 스스로 가중치를 해결하는가"를
구분하고, `check_pattern(from_hub=True)` 에서 데이터스토어 요구를 뺐다.

라이브 결과:

```
  datastore  UNREACHABLE  mlwffsftstorage8cb451dd1 (publicNetworkAccess=Disabled, 0 private endpoints)

  aks_vllm         ok        via --hf-model (no model asset, storage not involved)
  aml_batch        BLOCKED   no reachable datastore: ...
  aml_batch_vllm   BLOCKED   no reachable datastore: ...
  aml_online_vllm  ok        via --hf-model (no model asset, storage not involved)
  local_vllm       n/a       (local, no Azure quota involved)
```

배치는 여전히 막혀 있다. 배치 배포는 모델 자산을 리소스에 이름으로 박아야 하고
우리 코드가 실행되기 전에 플랫폼이 마운트하므로, 우회할 자리가 없다.

### 26.7 정정

§24와 README는 "모든 호스팅 패턴이 스토리지 벽에 막혔다"고 썼다. **너무 강한
주장이었다.** 정확히는 **모델 자산을 요구하는 패턴만** 막힌다. 온라인 vLLM은
Hub 경로로 우회 가능하고, 실제로 우회했다.

### 26.8 결말 — 롤아웃은 완료되지 않았다

정직하게 적는다. **배포는 성공하지 않았다.**

```
18:31  엔드포인트 ffsft-a10 생성           → Succeeded
18:31  배포 blue 생성 (NV12ads_A10_v5)
18:40  실패: Endpoint identity does not have pull permission   (약 10분)
18:41  AcrPull 수동 부여 → 확인됨
18:50  배포 blue 재생성
19:53  여전히 Creating       (63분, percentComplete 0.0)
20:15  여전히 Creating       (85분, percentComplete 0.0)
20:16  비용 때문에 중단, 삭제
```

두 번째 시도 시점의 상태는 전부 정상으로 측정됐다:

```
provisioningState  Creating          (85분간)
percentComplete    0.0
instanceType       Standard_NV12ads_A10_v5      ← 수락됨
model              null                          ← Hub 경로, 정상
MODEL_PATH         Qwen/Qwen3.5-0.8B
엔드포인트 ID 권한  AzureML Metrics Writer / Storage Blob Data Reader / AcrPull
```

즉 **쿼터·SKU·이미지 권한·모델 경로가 모두 맞는데 롤아웃이 진행되지 않았다.**
`percentComplete` 가 85분간 0.0 이었다는 것은 노드 할당 단계에서 더 나아가지
못했다는 뜻이다. 승인된 쿼터(36코어)와 요청량(24코어)은 맞지만, **쿼터가 있다는
것과 그 리전에 실제 A10 용량이 있다는 것은 다른 문제다.** §22에서 배운 교훈이
여기서도 반복된다 — 쿼터는 필요조건이지 충분조건이 아니다.

컨테이너 로그는 끝내 못 봤다: `Deployment is in deleting or creating state so
logs can't be retrieved`. 컨테이너가 시작조차 안 했으므로 볼 로그가 없다.

**그래서 무엇이 남았나:**

| 항목 | 상태 |
|---|---|
| A10 SKU 를 온라인 엔드포인트가 수락 | ✅ 실증 (§26.2) |
| Hub 경로가 스토리지 벽을 우회 | ✅ 실증 — 배포 리소스에 `model: null` |
| AcrPull 근본 원인과 자동 부여 | ✅ 실증 + 코드로 고침 (§26.4) |
| 프리체크가 못 잡던 구조적 결함 | ✅ 고침 |
| **실제로 서빙되는 GPU 엔드포인트** | ❌ **미완** — 85분간 노드 미할당 |

실제 추론 성능 수치(TTFT/TPOT)는 여전히 §25의 로컬 CPU 측정치뿐이다.
GPU 수치는 없다. **없는 것을 있는 것처럼 적지 않는다.**

다음에 재시도할 때 바꿔볼 것:
1. 더 작은 SKU (`Standard_NV6ads_A10_v5`, 12코어) — 용량 확보 가능성이 높다
2. 다른 리전 (A10 쿼터가 있는 곳)
3. 같은 명령을 시간대를 바꿔서 — 용량 문제라면 시점 의존적이다

명령은 `docs/RUNBOOK.md` §3 에 그대로 있다.

---

## 27. ✅ 기본 SKU 결함 + AcrPull 자동 부여 실증 — `ffsft-nv6`

§26 은 A10 온라인 엔드포인트로 벽을 뚫었지만 롤아웃이 완료되지 않은 채 끝났다.
이번에는 **§26.8 이 남긴 가설 1번(더 작은 SKU)** 을 실제로 시도했고, 그 과정에서
자산 자체의 결함 하나를 더 찾아 고쳤다.

### 27.1 ⛔ 출하된 기본값이 배포 불가능한 값이었다

`configs/serving.yaml` 의 `aml_online_vllm.default_sku` 는
`Standard_NV36ads_A10_v5` 였다.

```
NV36 = 36 코어
온라인 엔드포인트는 롤링 업데이트분을 예약 → x2
필요 = 72 코어      승인 = 36 코어
```

즉 **`--sku` 를 명시하지 않은 모든 `ffsft-deploy online` 은 반드시 실패한다.**
약 20분을 쓴 뒤 `quota requested is 72` 를 받는다. 이건 환경 문제가 아니라
**우리가 출하한 기본값이 우리 구독에서 동작하지 않는다**는 자산의 결함이다.

§26.3 에서 이 산술을 이미 측정해놓고도 설정 파일에 반영하지 않았다.
문서에만 적힌 발견은 다음 사람을 구해주지 못한다.

**TDD 로 고쳤다.** 테스트 3개를 먼저 쓰고 RED 를 확인했다:

```
FAILED test_online_pattern_default_sku_fits_the_granted_quota
FAILED test_every_online_default_sku_is_within_reach_of_its_quota_family
  AssertionError: pattern 'aml_online_vllm' defaults to
  Standard_NV36ads_A10_v5, which asks Azure for 72 dedicated cores
  against a 36-core grant
  assert 72 <= 36
```

기본값을 `Standard_NV12ads_A10_v5`(24 코어 필요, 12 여유)로 바꿔 GREEN.
테스트가 **설정 파일 자체를 실측 쿼터에 고정**하므로 파일이 다시 종이 위에서만
동작하는 값으로 되돌아갈 수 없다.

### 27.2 ✅ AcrPull 자동 부여가 무인 실행으로 증명됐다

§26.4 에서 첫 배포는 `Endpoint identity does not have pull permission` 으로
죽었고, 그때는 **손으로** 역할을 부여해서 넘어갔다. 이번 실행에서는 아무것도
손대지 않았는데 로그가 이렇게 남았다:

```
INFO ffsft.deploy.identity: granted AcrPull to
     30b28994-8c16-4ab7-babb-0a785bd546b8 on acrffsftkc
INFO ffsft.deploy.endpoint: granted AcrPull to the endpoint identity;
     waiting 60s to propagate
```

principal id 가 §26.4 의 `fbd167d1-...` 과 다르다는 점이 중요하다. 엔드포인트를
새로 만들면 시스템 할당 ID 도 새로 생기므로, 이건 **이전 수동 부여가 남아서**
통과한 게 아니라 코드가 새 ID 를 보고 새로 부여한 것이다.

§26.4 에서 고친 두 가지가 모두 실동작으로 확인됐다:

| 고친 것 | 이번 실행에서의 증거 |
|---|---|
| 프리체크를 엔드포인트 생성 **이후**로 이동 | 신규 엔드포인트인데도 ID 를 읽어냈다 (404 로 침묵하지 않음) |
| `ensure_acr_pull()` 로 직접 부여 | 새 principal 에 역할이 자동 생성됨 |

### 27.3 `percentComplete` 는 진행률 신호가 아니다

§26.8 은 85분간 `percentComplete: 0.0` 을 관측했다. 이번 롤아웃에서 같은 필드는
아예 `None` 이었다.

```
10:39:01  Creating None Default
```

**같은 API 가 같은 상태에서 두 가지 다른 값을 준다.** 이 필드로 "얼마나 남았나"를
판단하면 안 된다. 판단 가능한 것은 `provisioningState` 의 전이뿐이다.
### 27.4 ⛔ 가장 작은 A10 SKU 도 똑같이 정지했다 — 용량 가설 확정

§26.8 이 남긴 1번 가설, **"더 작은 SKU 면 용량을 잡을 수 있다"** 를 실제로 시험했다.
`Standard_NV6ads_A10_v5` 는 A10 계열 중 가장 작다.

```
2026-08-22 10:38  ffsft-nv6 / blue / Standard_NV6ads_A10_v5 생성 시작
                  6 코어 x 2(롤링) = 12 코어 필요, 승인 36 코어 → 여유 24
```

배포 리소스는 전부 정상이었다:

```
provisioningState  : Creating
instanceType       : Standard_NV6ads_A10_v5     <- 최소 SKU
model              : None                       <- Hub 경로, 스토리지 미사용
MODEL_PATH         : Qwen/Qwen3.5-0.8B
readinessProbe     : initialDelay PT2M20S, period PT30S, failureThreshold 10
egress             : Enabled
provisioningDetails: null
```

**50분간 `Creating` 에서 한 번도 움직이지 않았다.** 컨테이너 로그는 끝까지
`Deployment is in deleting or creating state so logs can't be retrieved.`

readiness probe 는 최대 `2분20초 + 30초x10 = 7분20초` 만에 판정이 난다.
**컨테이너가 떴다면 늦어도 ~8분 안에 `Failed` 나 `Succeeded` 중 하나가 나와야 한다.**
50분째 `Creating` 이고 로그가 없다는 것은 컨테이너가 뜬 적이 없다는 뜻이고,
컨테이너가 뜨려면 노드가 먼저 있어야 하므로 — **노드가 배정되지 않았다.**

### 27.5 실험 결과: SKU 크기는 변수가 아니었다

| 시도 | SKU | 코어(필요) | 결과 | 정지 시간 |
|---|---|---|---|---|
| §26.8 | `Standard_NV12ads_A10_v5` | 12 (24) | `Creating` 고착 | 85분 |
| §27.4 | `Standard_NV6ads_A10_v5` | 6 (**12**) | `Creating` 고착 | 50분 |

필요 코어를 **24 → 12 로 절반**으로 줄였는데 신호가 완전히 동일했다.
바꾼 변수가 결과를 전혀 바꾸지 못했으므로 **원인은 SKU 크기가 아니다.**

남는 설명은 하나다: **koreacentral 에 managed online endpoint 용 A10 물리 용량이
없다.** 그리고 이것이 이 자산 전체에서 가장 값비싼 교훈이다:

> **쿼터 승인은 용량 보장이 아니다.**
> 쿼터는 "이만큼까지 요청해도 된다"는 *허가*이고,
> 용량은 "지금 그 리전에 실제 GPU 가 놀고 있는가"라는 *사실*이다.
> 전자는 티켓으로 얻지만 후자는 얻을 수 없다.

Azure 는 이 구분을 API 로 알려주지 않는다. `Creating` 은 "곧 됩니다"와
"영원히 안 됩니다"를 같은 문자열로 표현한다.

### 27.6 다음에 시도할 것 — 순서를 바꾼다

SKU 축은 소진됐다. 남은 축은 **리전**과 **호스팅 표면**이다.

1. **다른 리전** — `japaneast`, `southeastasia` 등에 A10/T4 쿼터를 요청.
   워크스페이스가 리전에 묶이므로 새 워크스페이스가 필요하다.
2. **AKS + Spot (`aks_vllm` 패턴)** — 노드 풀은 관리형 엔드포인트와 다른
   용량 풀에서 할당된다. §26.2 에서 AmlCompute 와 온라인 엔드포인트가 서로 다른
   SKU 카탈로그를 갖는 것을 이미 봤으므로, 표면을 바꾸면 결과가 달라질 수 있다.
3. **배치 엔드포인트 + LowPriority A100** — 이 구독에서 **학습이 실제로 도는**
   경로다(§23). 대화형은 아니지만 GPU 추론 자체는 가능하다. 스토리지 벽(§24)만
   풀면 된다.

`--sku` 를 계속 바꿔보는 것은 이제 근거가 없다. 두 번의 실측이 그 축을 닫았다.
---

## 28. 🔑 진짜 원인 — 쿼터가 아니라 `restrictions` 였다

§27.5 는 "koreacentral 에 A10 물리 용량이 없다"로 결론냈다. **절반만 맞았다.**
정확한 원인은 용량 부족이 아니라 **구독이 그 리전에서 그 SKU 를 쓸 수 없도록
막혀 있었다**는 것이고, 이건 추측이 아니라 API 로 읽을 수 있는 값이다.

### 28.1 우리가 한 번도 안 물어본 API

```
GET /subscriptions/{sub}/providers/Microsoft.Compute/skus?$filter=location eq '{region}'
```

각 SKU 마다 `restrictions` 배열이 있다. koreacentral 의 A10 은 이렇게 나온다:

```json
"name": "Standard_NV12ads_A10_v5",
"restrictions": [
  { "type": "Zone", "reasonCode": "NotAvailableForSubscription",
    "restrictionInfo": { "zones": ["1","2","3"] } }
]
```

**zone 1, 2, 3 전부 차단.** koreacentral 에는 zone 이 3개뿐이므로 이건
"놓을 자리가 한 곳도 없다"는 뜻이다. 쿼터는 36코어 승인이었지만
**승인받은 것을 놓을 곳이 없었다.**

이것이 `Creating` 에서 50분·85분을 멈춘 이유다. 스케줄러는 배치할 존을
찾지 못했고, Azure 는 그 상태를 `Creating` 이라는 낙관적인 문자열로 표현한다.

### 28.2 쿼터와 restrictions 는 다른 것이다

| | 쿼터 (`limit`) | 제한 (`restrictions`) |
|---|---|---|
| 뜻 | **얼마나** 요청해도 되는가 | **애초에** 요청이 가능한가 |
| 얻는 법 | 티켓/Quota API 로 요청 | 요청 불가 — Azure 가 정한다 |
| 우리 상태 | koreacentral A10 = **36** | koreacentral A10 = **전 zone 차단** |

**쿼터를 먼저 확인한 것이 실수였다.** 순서가 거꾸로였다.
`restrictions` 가 비어 있지 않으면 쿼터는 몇이든 의미가 없다.

### 28.3 그래서 학습은 왜 됐나 — 이제 완전히 설명된다

koreacentral 의 A100 도 `NotAvailableForSubscription` 이다(리전 레벨).
그런데 `gpu-a100-lp` 클러스터는 A100 으로 **실제로 학습을 돌린다**(§23).

`restrictions` 는 **전용(dedicated) 할당**에만 적용되고,
**LowPriority/Spot 은 별도 풀**에서 배정되기 때문이다.

```
학습  = AmlCompute + LowPriority  → restrictions 우회 → 동작 ✅
서빙  = 매니지드 온라인 엔드포인트 (LowPriority 불가) → restrictions 직격 → 불가 ❌
```

§22.5 가 "그래서 학습은 왜 되나"를 쿼터 종류(dedicated vs low-priority)로
설명했는데, 진짜 층위는 하나 더 아래였다. **결론은 같지만 이유가 다르다.**

### 28.4 실측 — 이 구독에서 제한 없는 GPU 계열 (2026-08-22)

19개 리전을 훑었다. 값은 `restrictions == []` 인 SKU 만 센 것이다.

| 리전 | 제한 없는 GPU 계열 |
|---|---|
| **koreacentral** | **없음** ← 현재 워크스페이스 |
| koreasouth / japaneast / eastasia | 없음 |
| eastus / centralus / australiaeast / uksouth | 없음 |
| northeurope / canadacentral / centralindia | 없음 |
| japanwest | T4 |
| **westus2** | **A10** ← 이 자산의 기본 계열 |
| southcentralus | A10 |
| southeastasia / westus3 | A100 |
| eastus2 | A100, H100 |
| swedencentral | A100, T4 |
| westeurope | H100 |

**koreacentral 은 제한 없는 GPU 계열이 하나도 없다.** 이 구독으로
koreacentral 에서 GPU 온라인 엔드포인트를 띄우는 것은 불가능하다.
쿼터를 얼마를 받든, 어떤 SKU 를 고르든, 얼마를 기다리든 안 된다.

### 28.5 측정 방법에 대한 경고 — 한 번 읽은 값을 믿지 마라

이 표를 처음 만들었을 때 westus3 가 **A10·A100·H100·T4 전부 제한 없음**으로
나왔다. 상세 조회로 교차 확인하니 westus3 의 A10 은 `Location` + `Zone`
양쪽으로 차단돼 있었다. 같은 URL, 같은 필터인데 두 결과가 달랐다.

그래서 **같은 SKU 를 3회 연속 재조회**했다:

```
westus3  read1/2/3: restricted=True  [Location, Zone]   ← 3회 모두 동일
westus2  read1/2/3: restricted=False []                 ← 3회 모두 동일
```

API 는 결정적이었다. 즉 틀린 것은 첫 스캔 스크립트였다.
**리전을 고르는 근거가 되는 측정은 반드시 교차 검증한다.** 이 표는
리전별로 응답을 개별 파일에 저장한 뒤 각각 파싱해 다시 만든 것이다.

### 28.6 다음 행동 — westus2 + A10

`westus2` 를 골랐다. A10 전 계열이 제한 없고 **zone 1·2·3 모두** 사용 가능하다
(southcentralus 는 A10 이 제한 없지만 zone 이 1·3 둘뿐이다).

쿼터는 0 이므로 요청했다:

```
PUT .../locations/westus2/providers/Microsoft.Quota/quotas/StandardNVADSA10v5Family
    { "properties": { "limit": { "limitObjectType": "LimitValue", "value": 24 } } }

→ quotaRequests/c3e20a74-9d6c-46ac-902f-655837bed549
  provisioningState: InProgress
```

24 코어 = `Standard_NV12ads_A10_v5`(12코어 x 2 롤링) 정확히 한 대.

**이번에는 순서가 맞다:** 제한 없음을 먼저 확인했고, 그 다음에 쿼터를 요청했다.
---

## 29. The quota requests failed for a reason worth recording (2026-08-22)

### 29.1 The error code, once asked for properly

`quotaRequests` list returns only `"Request failed."` in `message`. The `error`
object underneath carries the real thing:

```json
{"code": "QuotaNotAvailableForResource", "message": "Request failed."}
```

Both westus2 attempts (24 cores, then 12) and southcentralus (24) failed with
this same code. `Request processing` was not progress -- it was the state
before the failure landed.

**This is a capacity refusal, not a policy refusal.** Azure is not saying "you
may not have this"; it is saying "there is none here to give". No ticket
changes that, and a smaller ask does not either -- 12 failed exactly like 24.

Subscription context: `quotaId: Internal_2014-09-01`, spending limit Off.

### 29.2 What quota this subscription actually has, everywhere

Dedicated (non-LowPriority) GPU families with a non-zero limit, across all 8
regions that have any unrestricted GPU family:

| family | limit | generation |
|---|---|---|
| `standardNCFamily` | 48 | K80, Kepler, CC 3.7 |
| `standardNCPromoFamily` | 48 | K80 |
| `standardNVFamily` | 24 | M60, Maxwell, CC 5.2 |
| `standardNVPromoFamily` | 24 | M60 |
| `internalNDMSv1Family` | 100 | ND v1 |

Identical in all 8 regions, which is the signature of an untouched default
rather than anything granted.

Every modern family -- `StandardNVADSA10v5Family`, `standardNCADSA100v4Family`,
`standardNCadsH100v5Family` -- is **0 in every region except koreacentral**,
and koreacentral is the region §28 proved is restricted in all zones.

The legacy quota is not a workaround. vLLM requires compute capability 7.0+;
K80 is 3.7 and M60 is 5.2, and neither has bf16. The quota that exists cannot
run the workload, and the quota that could run it does not exist.

### 29.3 So the managed online GPU endpoint is unreachable here

Three independent proofs, each measured rather than argued:

1. **Restrictions** (§28) -- koreacentral A10 is `NotAvailableForSubscription`
   in zones 1, 2 and 3. The 36-core grant cannot be placed.
2. **Behaviour** (§27) -- the largest and the smallest A10 stalled identically
   in `Creating`, so SKU size was never the variable.
3. **Capacity** (§29.1) -- every attempt to obtain modern GPU quota in an
   unrestricted region is refused for lack of capacity.

Managed online endpoints cannot use LowPriority. LowPriority is the only GPU
tier this subscription can actually allocate. The two facts do not intersect,
and no amount of retrying makes them.

**This is a subscription limitation, not a defect in the asset.** The same code
against a subscription with dedicated A10 capacity in an unrestricted region
has no known blocker -- which is precisely why the preflight below reports the
condition instead of the asset pretending it cannot happen.

### 29.4 The finding now lives in code, not just in this file

`ffsft.deploy.preflight.sku_blocker` (15 tests, `tests/test_preflight_sku.py`)
reads `Microsoft.Compute/skus` and refuses a deployment that cannot be placed,
before the endpoint is created. Verified live against real ARM:

```
koreacentral  Standard_NV12ads_A10_v5  zones offered ['2','3']  blocked ['1','2','3']  -> BLOCKED
westus2       Standard_NV12ads_A10_v5  zones offered ['1','2','3']  blocked []          -> DEPLOYABLE
```

Note koreacentral offers the SKU in zones 2 and 3 yet restricts 1, 2 and 3;
the restriction set is not a subset of the offered set, so the check subtracts
rather than compares lengths.

The message names the region, the SKU, the zones, and says plainly that more
quota will not help -- because the obvious next move after a placement failure
is to ask for quota, and here that wastes days.

Cost of not having this check: three deployments, roughly three hours of
`Creating`, and two teardowns of 32 and 64 minutes. Cost of running it: one ARM
read, about a second.
---

## 30. Correction: section 28's root cause was wrong (2026-08-22)

**Sections 28 and 29.3 claim `restrictions` explains the serving failures. That
claim is false and is retracted here.** The sections are left in place because
the measurements in them are sound; only the conclusion drawn from them was not.

### 30.1 The test that falsified it

Before letting the new preflight refuse anything, it was pointed at a SKU whose
behaviour is not in question -- the training cluster's own:

```
Standard_NC24ads_A100_v4   koreacentral
  restrictions:
    type=Location  reason=NotAvailableForSubscription  locations=[KoreaCentral]
```

That is a stricter restriction than the A10's: the whole region, not a zone.
And `gpu-a100-lp` is that SKU, in that region, and it fine-tuned a 27B model
for 42 minutes at `train_loss 1.2638` (section 23).

Five ordinary CPU SKUs -- `Standard_F8s_v2`, `Standard_F4s_v2`,
`Standard_DS3_v2`, `Standard_E4s_v3`, `Standard_D4as_v4` -- carry both a
`Location` and an all-zone restriction in koreacentral, in a region that
plainly runs CPU workloads (`cores 4/136` in use).

**So `restrictions` does not predict placement.** It describes on-demand
dedicated purchase eligibility. LowPriority/Spot allocates from a separate pool
and ignores it, which is why training works. It is not the mechanism behind the
A10 stalls; the correlation in section 28 was confounded by priority tier, and
the sample was three failures of one family in one region.

### 30.2 What the check does now

`sku_blocker` is gone. `sku_advisory` reports the same facts, never enforces,
and says in the message itself that the signal is not conclusive. Had the
original shipped, `ffsft-deploy` would have refused `Standard_NC24ads_A100_v4`
in koreacentral -- the one GPU configuration this subscription is proven able
to run.

`tests/test_preflight_sku.py::test_the_training_cluster_sku_is_never_refused`
pins the falsifying case, and `test_module_exposes_no_blocker_for_skus` stops
the hard block returning under its old name.

### 30.3 What is still true

- Section 29.1 stands: quota requests fail with `QuotaNotAvailableForResource`,
  a capacity refusal, at 24 and at 12 cores.
- Section 29.2 stands: the only non-zero dedicated GPU quota this subscription
  has outside koreacentral is K80/M60 era, below vLLM's compute-capability
  floor of 7.0.
- Section 27.5 stands: the largest and smallest A10 stalled identically, so SKU
  size is not the variable.

**Honest status of the serving failure: still unexplained at the mechanism
level.** Regional capacity for dedicated A10 remains the best hypothesis, and
`QuotaNotAvailableForResource` is consistent with it, but nothing measured so
far proves it. What is established is narrower and sufficient for planning: no
managed online GPU endpoint has ever reached a node on this subscription, and
the dedicated capacity such an endpoint requires cannot currently be obtained
in any region -- while LowPriority, which online endpoints cannot use, works.

### 30.4 The lesson worth keeping

The preflight was verified against a case where the right answer was already
known, and that is the only reason a false positive did not ship. A check is
not validated by the failures it explains; it is validated by the successes it
leaves alone.
---

## 31. The storage wall came down — managed VNet, not public access (2026-08-24)

Every previous attempt to get bytes out of a training node failed, and the
working theory was that the workspace storage account had been locked in a way
nothing could work around. That theory was half right. The lock is real and
permanent; the conclusion drawn from it was wrong.

### 31.1 The lock cannot be lifted, and that is now proven twice over

`az storage account update` and a direct ARM `PATCH` both return success and
leave `publicNetworkAccess: Disabled`. That was already known. What settles it
is the second measurement: a **brand-new storage account**, created with
`publicNetworkAccess: Enabled` and `allowSharedKeyAccess: true` explicitly in
the request body, came back

```
publicNetworkAccess  = Disabled
allowSharedKeyAccess = False
```

A management-group `Modify` policy rewrites every storage account in this
subscription at write time. Creating a fresh workspace in a fresh resource
group would have produced exactly the same locked account, so that plan --
which looked like the obvious escape -- was never going to work.

### 31.2 What actually fixed it

`managedNetwork.isolationMode` was `Disabled`. The compute cluster therefore
sat outside any network that had a private route to the storage account, and a
storage account with public access disabled has no other route. Logs still
arrived because the run-history service uploads those and is covered by
`bypass: AzureServices`; the node's own writes had nowhere to go.

Setting `isolationMode: AllowInternetOutbound` and provisioning the network
gives the workspace a managed VNet with private endpoints into its own
storage. The storage account now shows two **Approved** private endpoint
connections, and none of them live in this resource group -- they are managed
by the service, which is why `Microsoft.Network/privateEndpoints` in the RG
still lists zero.

The policy was never an obstacle to this. It locks public access; it has no
objection to the private path, which is the path it exists to force.

Two operational notes, both learned the hard way:

- Managed VNet cannot be enabled while any compute exists
  (`Managed network cannot be enabled when active computes exist`), so the
  cluster has to be deleted first and recreated afterwards.
- `provisionManagedNetwork` requires a body. With none it returns
  `Request body could not be read`; `{"includeSpark": false}` works.

### 31.3 Recreating the cluster silently revoked its permissions

The first job after the rebuild failed in under two minutes:

```
Failed to pull Docker image `acrffsftkc.azurecr.io/ffsft-train:9`
  401 error from registry: authentication required
```

A cluster's system-assigned identity is new every time the cluster is created.
The old principal had `AcrPull`; the new one, `61683516-…`, had nothing. This
has nothing to do with networking and would have been misread as another
storage failure if the error had not been legible. `AcrPull` on the registry
and `Storage Blob Data Contributor` on the account, granted to the new
principal, cleared it.

**Any teardown that deletes the cluster must re-grant both roles on rebuild.**

### 31.4 `jobs.stream()` reads what the artifact API will not

Section 15 recorded that job logs could not be read: `/contentinfo` returns
`{"value": []}`, there is no `contentUri`, and `jobs.download()` fails with
`AuthorizationFailure` because it goes to blob directly.

`MLClient.jobs.stream()` does not. It routes through the service and prints the
`Execution Summary`, which carries the real error text. Three earlier probe
failures were written off as unreadable; all three were readable this whole
time. The ACR 401 above was found this way in one call.

The log *body* still fails to stream, with the same `AuthorizationFailure`, and
that is expected -- the body does come from blob, and this workstation has no
private route. The summary is what matters.

### 31.5 End-to-end proof, entirely inside Azure

Two jobs, no local filesystem involved at any point.

A writer declaring `model_dir` as an ordinary `upload` output:

```
mkdir -p ${{outputs.model_dir}}
echo persisted-by-managed-vnet > ${{outputs.model_dir}}/proof.txt
dd if=/dev/urandom of=${{outputs.model_dir}}/blob.bin bs=1M count=8
```

`gray_feijoa_zlqglq32xh` — **Completed**.

A reader mounting that folder back out of blob, with the exit code as the
verdict because the log body is unreadable from here:

```
test -f ${{inputs.prev}}/proof.txt
grep -q persisted-by-managed-vnet ${{inputs.prev}}/proof.txt
test "$(stat -c%s ${{inputs.prev}}/blob.bin)" = "8388608"
```

reading
`azureml://datastores/workspaceblobstore/paths/azureml/gray_feijoa_zlqglq32xh/model_dir/`
at `ro_mount`. `sad_foot_spqk91m47n` — **Completed**. Any missing file, wrong
content or wrong size exits non-zero and fails the job.

Note what the reader did: it opened a **FUSE mount session against the locked
storage account** and it worked. That is the exact operation whose failure
forced `mount_outputs=False`, and the constraint behind that default no longer
holds.

### 31.6 What this changes

- Section 9's storage finding stands as a description of the lock, but its
  conclusion -- that no route exists -- is retracted. The private route exists.
- `mount_outputs=False` is no longer load-bearing. `upload` remains a
  reasonable default, but `rw_mount` is now available rather than impossible.
- The reason for chaining evaluation into the training job (JobSpec.eval_suite:
  "a second job would have to read the adapter back … and the node cannot open
  a session against that account at all") is obsolete. A second job can.
- `azureml://jobs/{name}/outputs/{output}` is **not** accepted as an input URI.
  The service requires `azureml://datastores/{store}/paths/…`; a declared
  output with no explicit path lands at
  `azureml://datastores/workspaceblobstore/paths/azureml/{job}/{output}/`.

None of this touches the GPU capacity finding in section 29. Training persists
now; dedicated GPU for online serving is still unobtainable.

## 32. Reading a job's log without leaving Azure (2026-08-24)

Section 31.4 established that `jobs.stream()` surfaces the *Execution Summary*.
That is enough to identify an infrastructure failure and not enough for
anything else: a user command that exits non-zero reports

```
ExecutionFailed: [REDACTED]
	exit_codes: 1
```

and the traceback lives only in `user_logs/std_log.txt`, which is in blob.

Once mounts worked, the log became reachable the same way any other blob is —
from inside a job. A diagnostic job mounts the failed run's artifact folder,
reads the log, and writes the tail back as MLflow tags, which are served by the
tracking service rather than blob and so are readable from anywhere:

```python
Input(path=f"azureml://datastores/workspaceartifactstore/paths/ExperimentRun/dcid.{run}/",
      mode=InputOutputModes.RO_MOUNT)
...
[mlflow.set_tag("diag_%02d" % i, tail[j:j+1800]) for ...]
```

Read back with `client.jobs.get(name).tags`. This recovered the full traceback
for `frank_cushion_b725dqgkyf` in a single four-minute job, and it works for
any failed run on this workspace.

The general shape is worth remembering: **anything unreachable from the
workstation is reachable from a job, and MLflow is the return channel.**

## 33. `trust_remote_code` — the registry's promise was narrower than it looked

`frank_cushion_b725dqgkyf` died before the first training step:

```
ValueError: The repository kakaocorp/kanana-2-1.3b-instruct contains custom
code which must be executed to correctly load the model.
Please pass the argument `trust_remote_code=True` to allow custom code to be run.
```

The traceback arrives doubled, and the first half is misleading:

```
Do you wish to run the custom code? [y/N]
  File ".../dynamic_module_utils.py", line 764, in resolve_trust_remote_code
    answer = input(
EOFError: EOF when reading a line
```

transformers tries to *ask*, stdin on a compute node is closed, and the
`EOFError` is what a reader sees first.

`configs/models.yaml` advertises every entry as a swappable target, and
`ffsft train --model <key>` as the whole interface. That held only for
architectures already merged into transformers. Kanana 2, Mi:dm, HyperCLOVA X
and EXAONE are all recent Korean-native releases and several ship their own
modelling code — which is to say the models this asset exists for were the
ones most likely to be unloadable.

The fix is a per-model `trust_remote_code`, default false, read identically by
training, evaluation and merge. Not a global default: the flag executes
arbitrary Python from a third-party repo at load time, and putting the decision
in the registry means enabling it for a model is a reviewable line in a diff
rather than an invisible property of the loader. 11 tests, written first.

Only `kanana2-1.3b` is marked so far, because it is the only one measured. The
others are unverified either way; the honest state is "not yet run", not "does
not need it".

---

## 34. The chain closed — train → persist → register → verify (2026-08-24)

The asset finally produced a registered model. It had never done so before, and
the reason it could not was three independent faults that each looked like the
whole problem while the other two were hidden behind it.

### 34.1 The last fault: the image was never the one running

`TRAIN_IMAGE` was bumped to `ffsft-train:10` carrying the `trust_remote_code`
fix from §33, and the job failed with the *identical* `ValueError` that fix had
already repaired. The fix was correct. It simply never reached the node.

```
version 9 -> acrffsftkc.azurecr.io/ffsft-train:9      <- what actually ran
version 8 -> acrffsftkc.azurecr.io/ffsft-train:8
```

`ENVIRONMENT_VERSION` was still `"9"`. An Azure ML environment is **immutable
per version**, so `ensure_environment` found version 9 registered, returned it,
and submitted against the pre-fix image. `create_or_update` over an existing
version is a silent no-op, not a correction.

Cost: `plum_station_dxwtzlz94q` allocated an A100, pulled nine gigabytes, and
proved nothing.

Two constants that must move together were coupled only by a comment saying so.
Now `image_tag()` derives the version from the tag, and `ensure_environment()`
compares the image inside an existing registration before reusing it — a
mismatch raises where it is free instead of after a node is allocated.

**And an image can be inspected without a GPU at all.** This costs cents and
takes four minutes:

```bash
az acr run --registry acrffsftkc -g rg-ffsft-kc --cmd \
  "acrffsftkc.azurecr.io/ffsft-train:10 python -c '...'" /dev/null
# HAS_BASE_LOAD_KWARGS True
# HAS_TRC_FIELD True
```

That check ran *before* resubmitting, and is the reason the next run was worth
paying for. Verify the artifact, then spend the GPU — not the reverse.

### 34.2 Training completed

`helpful_sand_971pqxtj0l` — `kanana2-1.3b`, `ko_smoke`, 30 steps, A100
LowPriority, `environment: ffsft-train:10`. Queued 06:02, running 06:10,
Completed 06:15.

### 34.3 Registration — two shapes the service insists on

Asset names allow alphanumerics, dashes and underscores. Nothing else:

```
(RequestInvalid) Resource name is invalid. Resource name can only contain
alphanumeric characters, dashes, and underscores, with a limit of 255 characters.
```

Almost every registry key has a dot in it — `kanana2-1.3b`, `qwen3.8-27b`,
`qwen3.5-0.8b` — so this is the default case, not an edge case. `asset_name()`
sanitises, and because that is lossy (`kanana2-1_3b` cannot be looked up in
`configs/models.yaml`) the original key is preserved in a `model_key` tag.

The path must be a datastore URI. The intuitive spelling is rejected:

```
azureml://jobs/{job}/outputs/{name}      -> NoMatchingArtifactsFoundFromJob
azureml://datastores/workspaceblobstore/paths/azureml/{job}/model_dir/   -> works
```

`job.outputs[name].path` reports `null` even for an upload that demonstrably
succeeded, so it cannot be used to discover this and must not be read as
evidence of failure.

### 34.4 Registration is not evidence — the mount is

The service accepts a URI pointing at a folder that does not exist. A successful
`create_or_update` therefore says nothing about whether a model was trained.

`hungry_apple_n455nrpngf` mounted `kanana2-1_3b-ko-lora:2` read-only and counted
what was actually there:

| | |
|---|---|
| files | 19 |
| total | 133,476,918 bytes |
| `adapter_model.safetensors` | 37,415,384 bytes |
| `adapter_config.json` | 1,165 bytes |
| `checkpoint-30/` | present, with optimizer + scheduler state |
| `run_summary.json` | present |

`checkpoint-30` matches the 30 steps requested, which is what makes this a
trained adapter rather than a folder of the right shape.

### 34.5 What this retracts

§24 and the `mount_outputs` docstring both recorded the node's inability to open
a session against `workspaceblobstore` as a permanent property of this
subscription. §31 fixed it; this section is the proof that the fix carries all
the way through to a registered, mountable, verified model asset.

`test_model_store.py` still opens by describing an unreachable datastore. Its
assertions are about `classify_store`, which was written correctly and handles
the private-endpoint case — the live probe now returns:

```
mlwffsftstorage8cb451dd1: public access off, reached over 2 private endpoint(s)
```

and all five serving patterns report deployable. The code did not need changing.
The world did.

### 34.6 An adapter is not servable

The registered folder has no `config.json` and no base weights — 37 MB of
low-rank deltas and a pointer. vLLM cannot open it. Serving requires either
`ffsft.deploy.merge` to fold the adapter into the base and register *that*, or
the `runtime_adapter` mode where vLLM loads the adapter beside a shared base.
Merged is the default for the reason `merge.py` documents: it is engine-agnostic
and carries no assumption about which modules the adapter targets.

## 35. Sizing 27B onto a 24 GB A10 — measured, not estimated (2026-08-24)

§34.6 ends at "serving requires a merge". This section is the arithmetic that
decides whether the merged artifact fits the only SKU quota allows, and the
image probes that confirm every flag it depends on. All of it was established
before any endpoint was created, on an ACR build agent costing cents — see
`az acr run --entrypoint bash` below.

### 35.1 The training run that produced the numbers

`jovial_quill_gk4l78b515`, Qwen3.8-27B QLoRA r16, `ko_smoke`, 20 steps,
`Standard_NC24ads_A100_v4` LowPriority. MLflow (the only readable channel, §32):

```
setup.vram_after_load_gb = 17.67     train.vram_peak_gb  = 28.19
train.trainable_params_m = 116.73    train.vram_card_gb  = 85.1
train.trainable_pct      = 0.7867    train.train_loss    = 1.0974
train.steps              = 20        train.wall_seconds  = 1878.4
```

Two of these are load-bearing:

* **`trainable_pct = 0.7867`** is the evidence that the explicit
  `lora_target_modules` did their job. PEFT's default `{q,k,v,o}_proj` set
  reaches only the 16 full-attention layers; the registry's 12-module list
  reaches the 48 Gated DeltaNet layers as well.
* **`vram_after_load_gb = 17.67`** is the NF4 weight footprint measured on a
  real card. Everything in §35.3 is built on it rather than on an estimate.

`publish(summary, prefix="train.")` runs *after* `trainer.save_model()` in
`qlora.py`, so the presence of the `train.*` metrics is itself proof the adapter
was written before the process exited. This matters because the submitter cannot
list the blob: `workspaceblobstore` answers `AuthorizationFailure` to a client
outside its network, so "did the save happen" cannot be answered by looking.

### 35.2 What the config actually says

From `Qwen/Qwen3.8-27B` `config.json` → `text_config`:

```
num_hidden_layers 64      full_attention_interval 4    -> 16 full / 48 linear
head_dim 256              num_key_value_heads 4
linear_num_value_heads 48 linear_key_head_dim 128      linear_value_head_dim 128
mamba_ssm_dtype float32   tie_word_embeddings false    vocab_size 248320
```

### 35.3 Per-sequence cost, and why `--max-num-seqs` is the flag that matters

| quantity | derivation | value |
| --- | --- | --- |
| KV per token | 16 full layers x 2 x 4 kv-heads x 256 head_dim x 2 B | 64 KB |
| KV per sequence @4096 | 64 KB x 4096 | 256 MB |
| GDN state per layer | 48 x 128 x 128 x 4 B (**float32**, not bf16) | 3 MB |
| GDN state per sequence | x 48 linear layers, plus conv state | ~152 MB |
| **total per concurrent sequence** | | **~408 MB** |

The KV cache is paged and sized from whatever memory is left. **The GDN state is
not** — it is allocated per sequence *slot*, up front, from `max_num_seqs`.
vLLM's default is `DEFAULT_MAX_NUM_SEQS`, which is three orders of magnitude more
slots than this card can hold: at 152 MB each, the default would demand well over
100 GB of state cache on a 24 GB A10 and the server would exit before serving a
single token. On a dense model this flag is a throughput knob. On a hybrid model
it is a fit-or-die knob, and nothing in the deployment path defaults it sanely.

Weights, for the same budget:

```
embed_tokens 248320 x 5120 = 1.27 B, lm_head untied = another 1.27 B
  -> 2.54 B params that bitsandbytes leaves in bf16      = 5.1 GB
body 26.9 B - 2.54 B = 24.4 B params in NF4              = 13.6 GB
                                                   total ~ 18.7 GB
```

which agrees with the 17.67 GB measured in §35.1.

Budget on `Standard_NV36ads_A10_v5` (24 GB) at `--gpu-memory-utilization 0.95`:

```
22.8 budget - 18.7 weights = 4.1 GB
  - GDN state   8 slots x 152 MB = 1.22 GB
  - KV cache    8 x 4096 tokens  = 2.10 GB
                                 = 3.32 GB used, ~0.8 GB margin
```

### 35.4 Flags confirmed present in `ffsft-serve:3` (vLLM 0.27.1)

Probed by scanning `vllm/engine` and `vllm/entrypoints` sources inside the image,
the same technique `docker/verify_serve.py` uses and for the same reason: a
CPU-only agent cannot run `make_arg_parser()` (`DeviceConfig` raises "Failed to
infer device type").

```
--max-num-seqs OK   --enforce-eager OK        --mamba-ssm-cache-dtype OK
--quantization OK   --mamba-cache-mode OK     --gpu-memory-utilization OK
--cuda-graph-sizes MISSING
```

`--cuda-graph-sizes` not existing is why `--enforce-eager` is in the first
rollout: with ~0.8 GB of margin there is no way to *bound* graph capture, only to
switch it off. Defaults worth knowing: `mamba_cache_mode` defaults to `"none"`
(so the registry's `align` is doing real work) and `mamba_ssm_cache_dtype`
defaults to `"auto"`, which follows the config's `float32` — forcing `bfloat16`
would halve the GDN state and is the next lever if the margin proves too thin.

### 35.5 Three deployment risks that turned out to be nothing

Each of these would have surfaced as an unhealthy rollout ~20 minutes and real
money later. All were closed on the build agent instead.

* **`Qwen3_5ForCausalLM` is bitsandbytes-loadable.** `AutoModelForCausalLM`
  resolves the multimodal checkpoint to `Qwen3_5ForCausalLM`, so *that* is the
  architecture the merged `config.json` will name — not the
  `Qwen3_5ForConditionalGeneration` that was verified when the image was built.
  It is registered, and it carries both `load_weights` and a
  `packed_modules_mapping` of `['gate_up_proj', 'in_proj_ba', 'in_proj_qkvz',
  'qkv_proj']`. Those two attributes are exactly what vLLM's bitsandbytes loader
  refuses on, and the mapping already covers the hybrid `in_proj_*` projections.
  **In-flight 4-bit needs no new code, no calibration job and no image rebuild.**
* **`LANGUAGE_MODEL_ONLY=1` on a text-only merge is inert.** `serving_env()`
  emits it from the *base* spec's `multimodal: true`, so the merged text-only
  checkpoint receives a flag telling it to skip a vision tower it does not have.
  `config/model.py:453` guards the only dereference with
  `if self.multimodal_config:`, and a text-only model has none. Nothing to fix.
* **`--reasoning-parser qwen3` resolves.** `ReasoningParserManager.reasoning_parsers`
  reads as `{}` on import, which looks like the parser is absent; registration is
  lazy. `get_reasoning_parser("qwen3")` succeeds, and a bogus name raises
  `KeyError` listing the real ones. An empty registry dict is not evidence.

### 35.6 The `SupportsLoRA` finding, which removes a choice

`Qwen3_5ForCausalLM`'s MRO is `[Qwen3_5ForCausalLM, Qwen3_5ForCausalLMBase,
Module, HasInnerState, IsHybrid, SupportsEagle3]`. There is **no `SupportsLoRA`**.
The `runtime_adapter` mode §34.6 offers as an alternative to merging does not
exist for this model in this vLLM build, and `serve_entrypoint.sh`'s caution that
adapters targeting the Gated DeltaNet projections are "not documented as tested"
resolves to something stronger: they cannot be loaded at all. Merging is the only
path, not the preferred one.

### 35.7 `ffsft-lifecycle up` could not reach either knob

The serve image has always read `GPU_MEMORY_UTILIZATION` and `EXTRA_ARGS`, and
`deploy_online()` has always accepted both — but `cmd_up`'s parser offered
neither, so on the only CLI that exposes `--quantization` they were unreachable
without an image rebuild. Given §35.3, that made the deployment unshippable on
the SKU quota allows. Both are now flags on `up`, pinned by tests.

`--extra-args` must be spelled with an attached `=`: every value worth passing
starts with a dash, and argparse reads a dash-leading token as the next option,
so `--extra-args --enforce-eager` exits 2 with "expected one argument".

## 36. Verifying a merged checkpoint you cannot read (2026-08-24)

### 36.1 What was actually unknown

Job `khaki_energy_wfxwtw3l23` merged the LoRA adapter into bf16 and reported
`Completed`. That word carries less than it looks like it does here. The merge
writes to `workspaceblobstore`, which answers `AuthorizationFailure` to a client
outside the workspace network (§31), so from the submitter's side a merge that
produced a correct 54 GB checkpoint and one that produced an empty directory are
the same three characters of status.

One field decided whether the rollout was worth paying for.
`AutoModelForCausalLM.from_pretrained` resolves a multimodal checkpoint to its
text-only class, so Qwen3.8-27B goes into the merge as
`Qwen3_5ForConditionalGeneration` and should come out as `Qwen3_5ForCausalLM` —
the name vLLM has registered, and the one that carries `load_weights` and
`packed_modules_mapping` (§35.6). If the merge had written the multimodal name
instead, vLLM would exit at container start, and on an NV36 that fact costs
about 30 minutes of rollout and real money before it surfaces.

### 36.2 The cheap way to look

A second command job, on the same LowPriority cluster whose node was still warm:
RO_MOUNT `azureml://datastores/workspaceblobstore/paths/azureml/{job}/merged/`,
walk the tree, and write the findings out as MLflow tags — the one channel that
reads back from outside (§32). `gray_knot_ywz6jg5cvw`, submitted 22:29:26,
`Completed` 22:30:29. **63 seconds**, LowPriority, on a warm node with the image
already cached. That is the whole cost of not guessing.

The tags it returned:

| tag | value |
| --- | --- |
| `diag_architectures` | `["Qwen3_5ForCausalLM"]` |
| `diag_model_type` | `qwen3_5_text` |
| `diag_has_vision_config` / `diag_has_text_config` | `False` / `False` |
| `diag_total_gb` | 53.81 |
| `diag_file_count` | 21 (14 safetensors shards + index) |
| `diag_has_tokenizer_config` / `generation_config` / `chat_template` | all `True` |

`merge_summary.json`, recovered from the same mount, confirmed the adapter
covered all twelve declared modules — `in_proj_a`, `in_proj_b`, `in_proj_qkv`,
`in_proj_z` among them, which are the GDN projections that exist only on the 48
linear-attention layers. Merge wall time 420.1 s; the rest of the job's 13
minutes was image pull and the base-model download.

The config also re-confirms the shape §35 sized against, from the merged file
rather than from the hub: `num_hidden_layers: 64`, `full_attention_interval: 4`,
`head_dim: 256`, `num_key_value_heads: 4`, `linear_key_head_dim: 128`,
`linear_num_value_heads: 48`, `mamba_ssm_dtype: float32`,
`tie_word_embeddings: false` (which is why 1.27 B embedding parameters and 1.27 B
lm_head parameters both stay in bf16 under bitsandbytes and the weights land near
18.7 GB rather than 13.6).

### 36.3 The gap that made the diagnostic job necessary

`qlora.py` calls `publish(summary, prefix="train.")` after `save_model()`, which
is why gate 1 could be verified without reading blob at all — the presence of the
metrics *is* the proof the adapter was written. `merge.py` wrote
`merge_summary.json` to `output_dir` and called `log.info`, and stopped there.
Both destinations are blob. The merge path had simply never learned what the
training path already knew.

Fixed: `deploy/merge.py` now calls `publish(summary, prefix="merge.")`, and the
summary carries three new fields — `architectures`, `model_type` and the
safetensors `files` count — chosen because they are exactly what the diagnostic
job had to be written to recover. **This does not affect the current rollout:**
the code is baked into the training image, so it takes effect only after a
`TRAIN_IMAGE` tag bump and an `az acr build`. `TRAIN_IMAGE` is deliberately left
at `:10` until that build runs, because bumping the tag without building points
`ENVIRONMENT_VERSION` at an image that does not exist and every submission fails.

### 36.4 Tenant pinning earned its keep, and could not finish the job

Mid-session, every Azure SDK call started failing:

```
ClientAuthenticationError: ChainedTokenCredential failed ...
AADSTS90072: User account '{EUII Hidden}' from identity provider
'https://sts.windows.net/2573db8c-.../' does not exist in tenant 'Contoso'
```

The Azure CLI's default subscription had moved underneath the session to a second
directory (`ME-M365CPI74210306-eonlee-1`, tenant `2573db8c-…`, a different signed-in
user). `az account list --all` showed both.

This is the exact drift `build_credential`'s docstring describes, and pinning
`FFSFT_TENANT_ID` did what it was built to do: the run stopped with an
authentication error naming the tenant instead of silently querying the wrong
directory and reporting that the workspace did not exist. What pinning cannot do
is fix it — `AzureCliCredential` serves the token for whichever account the CLI
has active, so a foreign active account fails no matter which tenant is pinned.
The repair is one command, and it is worth knowing before losing twenty minutes
to it:

```bash
az account set --subscription "$FFSFT_SUBSCRIPTION_ID"
```

No re-login was needed; the cached token for the correct account was still valid.

## 37. `containerType` has two spellings, not one (2026-08-24)

Measured against `ffsft-qwen38/blue` while it was in `Creating`, on
azure-ai-ml **1.34.1**:

```python
c.online_deployments.get_logs(..., container_type="StorageInitializer")
# ValidationException: Invalid container type 'StorageInitializer'.
# Supported container types are inference-server and storage-initializer

c.online_deployments.get_logs(..., container_type="storage-initializer")
# 200, body: "Deployment is in deleting or creating state so logs can't be retrieved."
```

§14's finding — that the raw ARM enum rejects lowercase with `RequestInvalid` —
is not retracted. Both are true on different call paths: ARM's enum is
PascalCase, and the SDK runs its own client-side validator that accepts only the
hyphenated lowercase form and never sends a request at all. This repo reaches
Azure through the SDK, so `CONTAINER_TYPES` now carries the SDK spelling and
`REST_CONTAINER_TYPES` keeps the other. The rejection is a `ValidationException`
raised locally, so it never reaches `classify_log_response` — which is why the
wrong constant sat in the module unnoticed: nothing passed it to the SDK, it
only appeared in an error hint that would have sent the reader the wrong way.

The same call confirmed `classify_log_response` works as designed. Azure
declines logs during `Creating` with **HTTP 200 and a 71-character prose body**,
not an error — exactly the shape that produced the 33-minute misdiagnosis — and
the "can't be retrieved" branch classifies it `WITHHELD`. During a rollout there
is therefore no log-based signal at all; elapsed time is the only one.

### 37.1 Open: the serving environment version is not derived

`aml_job.ENVIRONMENT_VERSION = image_tag(TRAIN_IMAGE)` exists so an environment
version can never drift from the image it names (§ the `plum_station` incident).
`endpoint.py` registers `ffsft-serve` with no explicit version, so Azure
auto-increments: today `ffsft-serve:2` points at `acrffsftkc.azurecr.io/ffsft-serve:3`,
an off-by-one that happens to be correct and is not protected by anything. The
training path's immutability check would catch a re-point; the serving path has
no equivalent. Not fixed here — the endpoint was mid-rollout.

## 38. The startup budget was in the wrong probe field (2026-08-24)

> **RETRACTED IN PART, 2026-08-24 (same day).** The probe shape described below
> was genuinely wrong and the fix is kept. The *causal claim* -- that it explains
> the 72-minute rollout of §38.1 -- is withdrawn. A rollout with the corrected
> probes (budget 33.2 min) ran for **113 minutes** and then failed the same way,
> and the operation status reported `percentComplete: 0.0` for its entire life.
> No container was ever created, so no probe of any shape could have mattered.
> The real cause is in §40. §38.2's measurements and §38.6's undocumented ceiling
> stand as measured; only the attribution in §38.3 is wrong.


### 38.1 What happened

`ffsft-qwen38/blue` sat in `Creating` for 72 minutes and never reached a terminal
state. Quota showed `StandardNVADSA10v5Family: used=72 limit=72` throughout, so
the node was allocated and this was not a capacity problem (§26.8). Container
logs were unavailable the entire time — confirmed again here with the *correct*
SDK spelling from §37, so the withholding in §14 is real and not an artefact of
the constant bug:

    get_logs(container_type="storage-initializer") -> 200,
      "Deployment is in deleting or creating state so logs can't be retrieved."
    get_logs(container_type="inference-server")    -> same 71-char body

### 38.2 The measurement

Azure's own defaults, from the [managed online deployment YAML
schema](https://learn.microsoft.com/en-us/azure/machine-learning/reference-yaml-deployment-managed-online?view=azureml-api-2)
`ProbeSettings` table, against what this repo was sending:

| field               | Azure default | published vLLM sample | this repo (before) |
| ------------------- | ------------: | --------------------: | -----------------: |
| `initial_delay`     |          `10` |                 `120` |             `1370` |
| `period`            |          `10` |                  `10` |               `30` |
| `failure_threshold` |          `30` |                  `30` |               `10` |

Two separate errors, in opposite directions:

1. The whole budget was in `initial_delay`. That field is dead time — the
   deployment cannot be reported healthy before it elapses, no matter how fast
   the container actually came up. A failed probe costs nothing; Azure just
   retries `period` seconds later. So `failure_threshold` buys the same patience
   for free in the success case, and `initial_delay` buys none of it.
2. `failure_threshold` was `10`, a *third* of the platform default. A vLLM
   container holding tens of gigabytes is strictly harder to start than the
   generic deployment that default was chosen for.

### 38.3 Why it plausibly caused the 72 minutes

Liveness gave up at `1370 + 30 x 10` = 1670 s (27.8 min) after container start,
and giving up on a liveness probe means the container is restarted. The elapsed
time fits two restarts almost exactly:

    72 min  ~  storage-init (54 GB) ~13 min  +  27.8  +  27.8

Each restart repeats the bitsandbytes NF4 in-flight quantisation from scratch.
From outside, a restart loop and healthy progress are indistinguishable: quota
stays at 72/72 either way and the logs stay shut. The troubleshooting page names
this failure directly under `ResourceNotReady` — *"Container initialization takes
too long, so the readiness or liveness probe fails beyond the failure
threshold."* This is consistent with the evidence, not proven by it; the logs
that would have proven it are the ones Azure withheld.

### 38.4 The fix

`startup_grace_for` now returns a *budget* and `probe_settings_for` spends it on
retries: `initial_delay` is a fixed 120 s, `period` 15, and `failure_threshold`
is derived, floored at Azure's own 30. Total wait is unchanged or larger; the
success case returns as soon as the container does.

Two sizing corrections came with it. The cap moved 1800 -> 3600, and
`IN_FLIGHT_QUANTIZATION_FACTOR = 2.5` was added, because the per-billion estimate
models a *download* and in-flight quantisation is compute on the serving card,
proportional to the unquantised checkpoint. For qwen3.8-27b under bitsandbytes
the budget is now 1981 s (33 min) with the first probe at 2 min, against 1670 s
with the first probe at 23 min.

### 38.5 Open

In-flight quantisation is paid again on every container start, restart and
scale-out. Pre-quantising to NF4 at merge time would remove the step entirely and
cut the storage-initializer download from 54 GB to ~19 GB. Not done — it needs a
re-merge, and the probe shape had to be fixed first either way.

## 39. Tenant pinning is not enough: the CLI profile drifts too (2026-08-24)

### 39.1 What was missed twice

`AADSTS90072` struck three times in one session. The first two were repaired with
`az account set --subscription $FFSFT_SUBSCRIPTION_ID` and written off as
unexplained (§36.4). The third came with timestamps that identified it:

    15:36:41  az account set --subscription <ffsft>      (gate chain starts)
    15:36:48  STEP 1 completes -- auth working
    15:37:40  ~/.azure/azureProfile.json rewritten
    15:37:44  ClientAuthenticationError, AADSTS90072

The workstation is signed in to two directories at once:

    ME-MngEnvMCAP277524-eonlee-1  4510ec63-...  eonlee@MngEnvMCAP277524
    ME-M365CPI74210306-eonlee-1   2573db8c-...  admin@m365cpi74210306

The Azure CLI keeps the active account in a single global file,
`$AZURE_CONFIG_DIR/azureProfile.json`, defaulting to `~/.azure`. Any `az`
invocation anywhere on the machine may rewrite it. `az account set` therefore
cannot be relied on: it is a write to shared mutable state that the next writer
wins, and with background pollers and parallel shells there is always a next
writer.

### 39.2 Why `build_credential` did not cover this

`azure_ml.build_credential` pins `tenant_id` on `AzureCliCredential`, and that is
correct as far as it goes -- it decides *which directory a token is requested
for*. It does not decide *which user asks*. That comes from the CLI's active
account, and when that account is the m365cpi user, requesting a token for tenant
`4510ec63` produces exactly this error: an identity from one directory asking for
another it has no guest entry in. The error names a tenant, so it reads as a
tenant problem, and the tenant was already pinned -- which is why it survived two
repairs.

### 39.3 The fix

Give the session its own CLI profile, so nothing outside it can move the account:

    cp -a ~/.azure ~/.azure-ffsft && chmod 700 ~/.azure-ffsft
    export AZURE_CONFIG_DIR=~/.azure-ffsft
    az account set --subscription "$FFSFT_SUBSCRIPTION_ID"

Verified by deliberately drifting the default profile to the other directory and
confirming the isolated one held, with an SDK call succeeding through it:

    default profile : admin@m365cpi74210306.onmicrosoft.com
    isolated profile: eonlee@MngEnvMCAP277524.onmicrosoft.com

Long-running scripts now assert the identity before spending anything, rather
than discovering it wrong partway into a rollout -- which is what the 15:36 run
did, dying 71 seconds in with a 33-minute deployment budget already committed.

### 39.4 What this does not fix

`AZURE_CONFIG_DIR` is session configuration, not code, so it protects this
workstation and not a teammate's. The durable version is a credential that has no
shared mutable state at all -- a service principal in the environment, which
`DefaultAzureCredential` already picks up behind the CLI credential in the chain.
Not done here; the chain was mid-flight.

### 38.6 The budget did not fit in the field (2026-08-24, same day)

The fix in §38.4 put the whole startup budget into `failure_threshold`. For the
27B model with in-flight quantisation that is 125 retries, and Azure rejected the
deployment 61 seconds after the request:

    (BadRequest) The request is invalid.
    (InferencingClientCallFailed) Validation:
      LivenessProbe.FailureThreshold: Invalid value provided for Failure
      Threshold for Probe: <125>. The value should be less than 120.
      ReadinessProbe.FailureThreshold: (same)

The YAML schema reference documents this field as "Minimum value is `1`", default
`30`, and states **no maximum**. The ceiling is real, server-side, and absent from
the reference -- so it was found by hitting it. 119 is the largest accepted value.

Cheap, as failures go: HTTP 400 at request-validation time, before a node was
allocated, with the offending value quoted back. The endpoint had already been
created (201) so nothing had to be rebuilt. Compare §38.1, where the misconfigured
field was one Azure accepts and then honours for 72 minutes.

`probe_settings_for` now clamps to `AZURE_MAX_FAILURE_THRESHOLD = 119` and lets
`period` absorb the remainder:

    params_b=26.9 q=bitsandbytes  budget=1981s -> initial_delay 120, period 16,
                                                  failure_threshold 117 = 33.2 min

Period is the right place for the overflow. It is how late readiness is *noticed*
after it becomes true, so 15 s -> 16 s buys half an hour of patience for one
second of detection latency. `initial_delay` would buy the same patience by making
every start slower by the worst case, which is precisely what §38 was about.

A test sweeps budgets from 0 to 7200 s and asserts no input can produce a value
Azure would reject, because the next model added to the registry will be larger
than this one and the ceiling is not in the docs to be re-read.

## 40. No GPU can be placed for a managed online endpoint here (2026-08-24)

### 40.1 The measurement that ended the guessing

Two rollouts on 2026-08-24, one with the old probe shape and one with the new,
behaved identically:

    attempt 1  13:39:09Z -> 15:31:33Z   112 min   InternalServerError
    attempt 3  15:47:26Z -> 17:39:35Z   112 min   InternalServerError

The operations status for the second reads:

    "startTime": "2026-08-24T15:47:26Z",
    "endTime":   "2026-08-24T17:39:35Z",
    "percentComplete": 0.0,
    "status": "Failed",
    "error": { "code": "InternalServerError" }

**Zero percent, for 112 minutes.** Not a container that started and failed a
probe -- nothing was ever created. That also explains why `getLogs` answered
`GONE` both times (§38, `LogStatus.GONE`): the logs were not reclaimed with a
node, there was never a container to write any.

### 40.2 Why nothing could be placed

Every GPU SKU offered in koreacentral is refused to this subscription:

    koreacentral, 20 GPU SKUs, Microsoft.Compute/skus
      NC/ND family (T4, A100, H100), 14 SKUs   Location  / NotAvailableForSubscription
      NVadsA10v5 family (A10 24 GB), 6 SKUs    Zone      / NotAvailableForSubscription
      -> unrestricted GPU SKUs: 0

And the A10 restriction covers every zone the SKU is offered in:

    Standard_NV36ads_A10_v5, koreacentral
      locationInfo.zones            : ["2", "3"]      <- where it exists
      restrictions[0].type          : "Zone"
      restrictions[0].reasonCode    : "NotAvailableForSubscription"
      restrictions[0].restrictionInfo.zones : ["1", "2", "3"]   <- all refused

Meanwhile the Azure ML online-endpoint quota says the opposite:

    StandardNVADSA10v5Family   dedicatedCores limit = 72

72 cores is exactly one NV36 at the 2x online-endpoint multiplier. The grant is
real and it is unusable. Quota answers "how much may I ask for"; `restrictions`
answers "may I ask at all", and only the first is visible on the portal quota
page. This is the trap `preflight.py` was written for, and it was still walked
into twice because the check only warned.

### 40.3 Why training works and serving does not

AmlCompute defaults to LowPriority, and Spot allocates from a pool that ignores
`NotAvailableForSubscription` -- this subscription fine-tunes a 27B model on an
A100 carrying a stricter `Location` restriction than the A10 does.

**Managed online endpoints reject LowPriority outright.** Every node they get is
on-demand dedicated, which is precisely what the restriction describes. There is
no escape hatch on this path. The advisory text in `sku_advisory` says the field
is "not conclusive ... LowPriority/Spot allocates from a separate pool that
ignores it" -- true of the training path, false of this one, and collapsing the
two is what made a decisive signal look like one hint among several.

### 40.4 Other regions are strictly worse

    region           Standard_NV36ads_A10_v5      online-endpoint A10 quota
    koreacentral     Zone blocked                 72
    japaneast        Location + Zone blocked       0
    japanwest        not offered                   0
    southeastasia    Location + Zone blocked       0
    eastasia         Location + Zone blocked       0
    australiaeast    Location + Zone blocked       0
    eastus           Location + Zone blocked       0
    eastus2          Location + Zone blocked       0
    westus3          Location + Zone blocked       0
    westeurope       Location + Zone blocked       0

koreacentral is the only region with any online-endpoint A10 grant at all, and
the only one blocked at zone rather than location scope. Moving the workspace
loses the quota and gains a stricter restriction.

### 40.5 The fix

`preflight.online_endpoint_blocker` refuses the deployment before
`begin_create_or_update` is called, for this caller only. `sku_advisory` still
merely reports, because enforcing it globally would refuse the A100 cluster that
trains here -- the retraction recorded at the top of `tests/test_preflight_sku.py`
and pinned by a test that must keep passing.

    RestrictedSkuError: 'Standard_NV36ads_A10_v5' is marked
    NotAvailableForSubscription in every zone of 'koreacentral' (1, 2, 3), and a
    managed online endpoint cannot use LowPriority/Spot -- so unlike an
    AmlCompute cluster it has no pool that ignores this. [...] Choose a SKU with
    an unrestricted zone, or pass force=True to spend the two hours anyway.

Cost of the field being advisory rather than enforced, total: five rollouts,
none of which ever created a container.

### 40.6 What this does not decide

It rules out a managed online endpoint on GPU in this subscription as it stands.
It does not rule out serving the model on Azure. Untested options, in rough order
of effort: vLLM inside an AmlCompute LowPriority job with the load generator in
the same job (uses the exact allocation path that already works, yields real
TTFT/TPOT, yields no public URL); a Kubernetes online endpoint on an AKS Spot GPU
node pool (a real endpoint, Spot satisfies the tenant `priority notEquals "Spot"`
deny policy, but eviction mid-test is a live risk); or lifting the SKU
restriction, which is a support request and was ruled out by the operator.

## 41. The load test moved to where the GPU is (2026-08-25)

§40 closed the endpoint path: every GPU SKU koreacentral offers is
`NotAvailableForSubscription`, and a managed online deployment has no
LowPriority pool to fall back to. The remaining gate — a load test — was defined
in terms of that endpoint, so it had to be re-planned rather than retried.

The operator chose: run vLLM inside an AmlCompute LowPriority job on
`gpu-a100-lp` with the load generator in the same job. That is the allocation
path that already works — it trained the adapter — and it is the only one that
puts this model on a GPU in this subscription today.

### 41.1 What the numbers do and do not cover

Kept, because they are properties of the model and the card: TTFT, TPOT, the
p50/p95/p99 spread, and the concurrency at which throughput stops scaling. The
27B is served in **bf16, unquantized** — 53.8 GB of weights on an 80 GB A100,
against the 24 GB A10 that forced `--quantization bitsandbytes` and the
in-flight quantization problem of §38.5. That problem is now moot on this path,
not solved.

Lost, and not to be presented otherwise: TLS termination, endpoint auth, one WAN
hop, Azure's routing layer, and autoscaling. The client is in the same container
as the server. A report from this job is a model benchmark, not an endpoint SLO.

### 41.2 One image, because two would have measured different servers

The serving image has vLLM and no `ffsft`; the training image has `ffsft` and no
vLLM. `docker/Dockerfile.bench` is `FROM` the serve image and adds only the
package, so `docker/bench_entrypoint.sh` starts the **real**
`serve_entrypoint.sh` as a subprocess rather than reimplementing its flags.

That matters more than it saves. Every architecture flag Qwen3.8 needs is
derived from `configs/models.yaml` by `deploy.endpoint.serving_env`, which
`bench_job.bench_env` reuses verbatim: `--mamba-cache-mode align` (48 of 64
layers are Gated DeltaNet, and vLLM raises `NotImplementedError` without it),
`--language-model-only` (the vision tower would cost VRAM the KV cache needs),
`--reasoning-parser qwen3` (without it the `<think>` block lands in `content`
and inflates every measured output-token count). A second copy of that logic
would eventually disagree with the first, and the bench would then report
latencies for a server nobody ships.

### 41.3 Three defects the node would have found expensively

Each was caught on a laptop, against `scripts/mock_vllm_server.py`, before an
A100 was allocated. A LowPriority allocation plus a 54 GB weight load is tens of
minutes per attempt.

**The mislabeled sweep.** `loadtest.run_level` sends `requests_per_level`
requests through a semaphore of `concurrency`. With 16 requests at concurrency
32, only 16 are ever in flight and the row is still printed as 32 — no error,
a complete-looking table, and the top of the sweep measuring a different load
than its label claims. `BenchSpec.__post_init__` now refuses
`requests_per_level < max(levels)`, and the default is 2x the top level so the
last level runs two full waves.

**The unsubstituted binding.** Azure ML expands `${{inputs.*}}` and
`${{outputs.*}}` in the command string and nowhere else. `MODEL_PATH` set in
`environment_variables` would arrive at the node spelled `${{inputs.model}}`,
and vLLM would look for a model in a directory of that name. Both paths are set
in the command; a test asserts no environment value contains `${{`.

**The silent default.** `bench_entrypoint.sh` reads its configuration from the
environment. A knob renamed on one side runs the default on the other, and the
sweep completes and reports numbers for a configuration nobody asked for. A test
parses the shell script and requires every `BENCH_*` it reads to be one the job
emits — except `BENCH_SERVE_CMD`, the hook that lets the script be exercised
without a GPU, which is asserted **absent** from the job so a run can never be
pointed off vLLM.

### 41.4 Mount vs download for 54 GB

The input is `mode="download"`, not `ro_mount`. vLLM reads safetensors through
`mmap`, and `mmap` over a blobfuse mount turns one sequential 54 GB read into
random page faults against blob storage. `Standard_NC24ads_A100_v4` carries a
local NVMe temp disk, so downloading first is one predictable sequential copy.
Not measured against the alternative — the alternative was not worth a node.

## 42. Nothing leaves the node except MLflow (2026-08-25)

### 42.1 The bench would have run correctly and measured nothing

`ffsft-bench:4` was correct: the model path resolved, vLLM would have loaded
54 GB of bf16 weights, and the sweep would have written `loadtest.json`. It
would still have reported nothing, because every path that carries a number off
a compute node here is backed by the workspace's storage account, and that
account is unreachable from outside the managed VNet (§31):

    jobs.download(...)          -> 403, blob
    SAS link from the artifact  -> 403, blob
    artifact `content` proxy    -> 403, blob
    ./outputs uploads           -> written, then unreadable
    jobs.stream(...)            -> reads std_log.txt, also blob

The last one is the correction. I had told the operator the live stdout stream
was the one working path. It is not: `jobs.stream` reads `std_log.txt` from the
same blocked container. Job `helpful_jelly_gndv8d135q` ran to **Completed** and
its stream delivered three lines -- RunId, Web View, Execution Summary. A sweep
that prints a beautiful table to stdout produces exactly those three lines here.

What does work is the MLflow *tracking* service. It is a separate service, it is
not blob-backed, and it answers an ARM token. `src/ffsft/train/report.py`
already used it for training; §42.2 gives serving the same channel.

### 42.2 `--target` because vLLM's site-packages is not ours to edit

`Dockerfile.bench` installs the client into a directory of its own:

    RUN python3 -m pip install --no-cache-dir --target=/opt/mlflowlib \
            "mlflow-skinny>=2.16" "azureml-mlflow>=1.57"

and `enable_mlflow_lib()` **appends** it to `sys.path`. Both halves matter. The
vLLM base image pins its own `pydantic`, `httpx` and `protobuf`; a plain
`pip install` resolves against those pins and can move them, and a moved pin
breaks the server we are trying to measure. `--target` keeps the two sets apart,
appending (never prepending, never `PYTHONPATH`) means the base image's copy
still wins every import both need, and `bench_report` is the only thing that
reaches into the directory at all.

A build-time guard imports the whole chain, so a broken install fails the build
rather than the node:

    report OK: mlflow 3.13.0 / httpx 0.28.1

### 42.3 Reporting is set up before anything can fail

The reporting block in `bench_entrypoint.sh` sits **above** model resolution,
and `trap cleanup EXIT` is armed there. A job that dies resolving its model is
exactly the job whose reason for dying cannot otherwise be read. `RUN_PHASE`
names the phase in progress -- `resolving_model`, `model_not_found`,
`waiting_for_health`, `server_exited_${RC}`, `health_timeout`, `smoke`,
`sweeping`, `swept` -- so whatever it holds at exit is the phase that failed,
and `bench.status` carries it out. The last 12 lines of `vllm.log` ride along as
numbered tag chunks, but only when the status is not `swept`: a good run should
carry measurements, not chatter.

`RUN_PHASE` is deliberately not `BENCH_*`. That namespace is the job's
configuration knobs, every one of which the job must set -- and the repo's own
drift test enforces it, which is how the first name was caught.

### 42.4 Two shapes MLflow will not carry

Tags cap at 250 characters and `report.publish()` sends all of them inside one
`try`, so **one over-long tag silently drops every tag behind it**. `_tag()`
truncates to 240 and drops empties before they are queued.

A missing knee is a finding, not a zero. When no concurrency level meets the
p95 TTFT SLO, the result is the tag

    bench.knee_concurrency_none = "no level met the p95 TTFT SLO"

never the metric `bench.knee_concurrency = 0.0`, which reads as a measurement of
zero rather than an absence of one.

### 42.5 `az acr build` cannot read a continuation inside a quoted string

The build guard was first written across three lines with `\` continuations.
Build `dey` failed in 3 seconds, before Docker ran at all:

    unable to understand line from ffsft.serve.bench_report import
    enable_mlflow_lib; \

ACR's dependency scanner parses the Dockerfile itself and does not track quoting
when it joins continuations. Flattening the guard to a single long line built
fine. The two remaining `\` continuations in the file sit outside quotes -- the
shape that already built for `:4`.

Related: `az acr build` snapshots the build context at submission, not at
execution. Editing a file after the upload starts changes nothing in the image.

## 43. It was never a quota problem (2026-08-25)

### 43.1 The correction

§40 established that no GPU can be placed for a managed online endpoint here,
and I summarized that to the operator as a quota problem. That was wrong, and
they caught it. Quota is granted:

    AML  StandardNVADSA10v5Family     used=0  limit=72
    AML  TotalLowPriorityCores        used=0  limit=300
    Compute Total Regional Low-pri    used=0  limit=100
    Compute Standard NVADSA10v5       used=0  limit=36

Seventy-two AML A10 cores is exactly one `NV36ads_A10_v5` at the online-endpoint
2x multiplier. The grant is sized for the deployment that cannot be made. Asking
for more of a thing that is already unusable would not have moved anything --
which is a second reason, independent of cost, that the operator's "proceed with
the quota we have" was the right call.

Quota answers *how much may I ask for*. Two other gates answer *may I ask at
all*, and neither appears on the portal's quota page.

### 43.2 Wall A: the SKU restriction (platform)

`Microsoft.Compute/skus` in koreacentral marks all 20 GPU VM SKUs
`NotAvailableForSubscription` (§40.3). In AWS terms this is closer to an
instance family not being offered to the account in a region than to a service
quota ceiling.

### 43.3 Wall B: a tenant deny policy whose only exception is Spot

Not previously enumerated -- a keyword filter over the subscription's six
assignments matched nothing, because the assignment is inherited from the
tenant root management group. The subscription sits under
`MngEnvMCAP277524`, and the root MG carries `MCAPSGov Deny Policies`. Its
`BlockVMSKUs_N` reference resolves to the definition `VirtualMachine_SKU_Deny`:

    if  type in [virtualMachines, virtualMachineScaleSets]
        AND sku.name in BlockedSKUs
        AND priority != "Spot"
    then Deny

`BlockedSKUs` is supplied by the initiative, not defaulted, and lists both SKUs
this project touches: `standard_nc24ads_a100_v4` and `standard_nv36ads_a10_v5`.
So the tenant does not forbid GPU. **It requires that GPU be Spot.**

The other five MG assignments are Azure Security Baseline, MCAPSGov Audit,
MCAPSGov Deploy and Modify, `Block Azure RM Resource Creation`, and a West
Europe region restriction.

### 43.4 Which is why training works and serving does not

AmlCompute defaults to LowPriority. Spot allocates from a pool that ignores
`NotAvailableForSubscription`, and `priority == "Spot"` is the one exception
wall B carves out -- so the training path clears both walls.

The obvious reply is "then run the endpoint on Spot too". You cannot ask. The
constructors say so:

    AmlCompute                 priority/tier params: ['tier']
    ManagedOnlineDeployment    priority/tier params: NONE
    KubernetesOnlineDeployment priority/tier params: NONE

`AmlCompute.tier` is dedicated-or-lowpriority, which is the knob training turns.
`ManagedOnlineDeployment` has no such field in the schema at all: every node it
gets is on-demand dedicated, which is precisely what both walls describe. The
tenant requires that GPU be Spot; the resource cannot request Spot. The two
conditions do not intersect, so this is not a workaround that has not been found
yet. The server states the outcome directly (§40.5, `RestrictedSkuError`).

`KubernetesOnlineDeployment` has no field either, and for the opposite reason:
it does not need one. Priority is a property of the AKS node pool, which is
created and owned by this subscription and does take `--priority Spot`. That is
the whole reason §43.6 is still open rather than closed with the rest.

### 43.5 Azure Container Apps serverless GPU: measured, and closed

Listed here because it was the one option §40.6 did not consider.
`availableManagedEnvironmentsWorkloadProfileTypes` in koreacentral returns 17
profiles and **not one has a GPU**. The feature exists elsewhere --
`Consumption-GPU-NC24-A100` in swedencentral, and westus3 additionally offers
dedicated `NC24-A100` / `NC48-A100` / `NC96-A100` -- but in both of those
regions this subscription reads

    Subscription Dedicated NCA 100 Gpus   used=0  limit=0

Opening it is a quota request, which is the path the operator ruled out. Closed
on the same grounds as lifting the SKU restriction, and it would also have meant
moving the workload out of Korea.

### 43.6 What remains open

An AKS Spot GPU node pool with a Kubernetes online endpoint is the one path that
would still yield a real scoring URL here, because AKS node pools are VMSS in
this subscription -- wall B applies to them and Spot is its stated exception,
and 100 regional low-priority Compute cores are granted and unused. Untested,
and three things could still stop it:

  - Spot eviction mid-load-test, which is the whole point of the test
  - `NV36ads_A10_v5` is one A10, 24 GB. 27B at bf16 is 53.8 GB (§35), so this
    path needs an AWQ/GPTQ 4-bit build (~15 GB) or an A100 Spot pool
    (`NC24ads`, 24 vCPU, within the 100)
  - whether a Spot VMSS clears wall A. Weaker than it looked: §2.1's correction
    records that `vm-a10-ffsft`, an ordinary `Microsoft.Compute/virtualMachines`
    on a GPU SKU at Spot priority, **allocated successfully in this
    subscription** and failed only on its GRID driver extension. So Spot
    clearing wall A is not an AmlCompute-only property. A VMSS is still a
    different resource type, so this is likely rather than proven

Recorded as the honest state, not as a plan. Gate 3 runs on the path that is
known to work first.

## 44. The channel worked; the window was wrong (2026-08-25)

### 44.1 First live proof that a failure can be read from outside

`careful_door_6fqvn7v4x4`, the first bench job on `ffsft-bench:5`, failed. That
is the good news: it failed and **said so**, over MLflow, from a network this
account cannot otherwise reach into. Read with nothing but an ARM token:

    status : Failed
    phase  : server_exited_1

The `RUN_PHASE`/EXIT-trap design of §42.3 did its job on its first real
exercise. The model resolved, the weights downloaded, vLLM started and then the
process exited 1 — and every one of those facts crossed the VNet boundary.

### 44.2 What it could not say

`bench.vllm_tail` arrived complete and useless:

    RuntimeError: Engine core initialization failed. See root cause above.
    Failed core proc(s)

vLLM reports a startup failure **twice**. The EngineCore subprocess prints the
real exception; the API server then prints its own traceback, which ends by
pointing back at the first one. The last twelve lines of the log are always the
second of those. A tail is structurally the wrong window: it is guaranteed to
capture the symptom and to drop the cause.

`bench_entrypoint.sh` already echoes 120 lines to stdout on this path. That
changes nothing — stdout is `std_log.txt`, which is blob (§42.1).

### 44.3 The fix: open the window at the failure, not at the end

`error_excerpt(lines, window=32)` scans **forward** from the top of the log for
the first line carrying an `ERROR_MARKS` signature and returns a window from
there. `build_report` now publishes both, and `_publish_report` gives the cause
the larger share of chunks:

    bench.vllm_cause.01 .. .12    first failure, ~32 lines
    bench.vllm_tail.01  .. .08    last 40 lines, unchanged in kind

It returns `""` when nothing matches, so a log with no error signature falls
back to the tail rather than publishing an empty tag.

Chunk indices are now zero-padded. Tags sort as strings and the Studio UI gives
no way to re-sort them, so unpadded `.10` lands between `.1` and `.2` and
reassembles a traceback scrambled. This was latent at four chunks and would have
become visible at twelve — found by writing the test, not by reading it back
wrong in the UI.

### 44.4 What is still unknown

Why vLLM's engine core failed on `Qwen3.8-27B` merged bf16 on one A100 80 GB.

The first theory was that the Gated DeltaNet state did not fit. From the
published `config.json` -- 48 of 64 layers `linear_attention`,
`linear_num_value_heads` 48, `linear_key_head_dim` and `linear_value_head_dim`
128, `linear_conv_kernel_dim` 4, `mamba_ssm_dtype` float32 -- one sequence slot
costs

    conv      (16*128*2 + 48*128) * 4          =    40,960 elems
    recurrent  48 * 128 * 128                  =   786,432 elems
    per layer  (40,960 + 786,432) * 4 bytes    =      3.2 MiB
    per seq    x 48 linear_attention layers    =    151.5 MiB

At vLLM's default `max_num_seqs` that is 37.9 GiB and would indeed overflow the
18.2 GiB left. **But the job does not use the default.** `BenchSpec.max_num_seqs`
is 16, set deliberately against this exact cost (§35.3), which comes to 2.4 GiB
-- comfortably inside the budget, leaving ~15.8 GiB for KV, enough for 258k
tokens of the 16 full-attention layers at 64 KiB each against a demand of
16 x 4096 = 65k. **The arithmetic does not support the theory.** Recorded
because a plausible cause that the numbers refute is worth as much as one they
confirm, and because the next reader will otherwise recompute it.

So the cause is genuinely unknown. Remaining candidates, unranked: the vision
tower (`vision_config` is present and `language_model_only` is false in the
config, so a multimodal profiling pass runs), CUDA graph capture across a
64-layer hybrid, `--mamba-cache-mode align` page padding, or the merged
checkpoint itself (§36 verified what it could without being able to read it).

**Not guessed at in code.** Changing a serving flag on a hunch would also change
what the benchmark measures, and the next run reports the cause directly.
`ffsft-bench:6` carries the reporting fix and nothing else.

## 45. The window faced the wrong way (2026-08-25)

### 45.1 What `:6` bought

`quirky_bee_4yh061560n`, submitted 02:01:57 UTC on `gpu-a100-lp`, `Failed` at
02:12. Read from outside the VNet with nothing but an ARM token:

```
status  : Failed
phase   : server_exited_1
```

and, for the first time, a `bench.vllm_cause` window that opened at the
EngineCore subprocess rather than at the API server's re-raise. It resolved the
call stack all the way down:

```
run_engine_core -> EngineCoreProc.__init__ -> EngineCore.__init__
  -> executor_class(vllm_config) -> uniproc_executor._init_executor
  -> driver_worker.load_model() -> model_runner.load_model(...)
```

**The failure is inside `load_model`.** That is a real narrowing, and it
eliminates three of the four candidates §44.4 left open: memory profiling, CUDA
graph capture and mamba page padding all happen *after* weights are read. It
dies reading the checkpoint.

### 45.2 Why it stopped one line short, and what that says about the fix

The window was 32 lines wide and the exception was not in it. Two defects, both
mine, both in `error_excerpt`:

**A traceback names its failure on the LAST line.** `lines[index:index + window]`
keeps the head. For an exception the head is the least informative part — it is
the same six frames of vLLM startup every time. §44.3 moved the window to the
right *place* and left it facing the wrong *direction*.

**The speaker stamp is charged to the tag budget on every line.** Every line
carried `(EngineCore pid=287) ERROR 08-25 02:11:16 [core.py:1349] ` — 57
characters, against `TAG_LIMIT` 240. Measured: 159-character lines with a
102-character payload, so `chunks=12` bought 18 lines of traceback where
stripping buys 28.

Both fixed in `ffsft-bench:7`. The block is now bounded by its speaker — the
`(EngineCore pid=287)` stamp — because everything after it belongs to the
re-raise; the stamp is stripped on the way out; and when the block does not fit,
**both ends** are kept with a `... N lines omitted ...` marker between them.
Pinned by three tests (`tests/test_bench_report.py`), one per defect.

### 45.3 Correction to §44.4's candidate list

§44.4 named "the vision tower (`vision_config` is present and
`language_model_only` is false in the config)" as a candidate. That describes
the **base** model on the Hub. It does not describe the artifact being served.

§36.2 verified the merged checkpoint directly: `architectures:
["Qwen3_5ForCausalLM"]`, `model_type: qwen3_5_text`, `diag_has_vision_config:
False`. There is no vision tower in what vLLM is being asked to load.

Meanwhile `serving_env` (`deploy/endpoint.py:495`) emits
`LANGUAGE_MODEL_ONLY = "1" if spec.multimodal else "0"`, and `configs/models.yaml`
marks `qwen3.8-27b` as `multimodal: true` — which is true of the base model and
false of the merge. So the bench launches a text-only checkpoint with
`--language-model-only`. `bench_env` reaches this through the same shared
`serving_env` the deployment uses, so the bench and the deployment agree with
each other and both disagree with the artifact.

That is a registry-versus-artifact mismatch on a flag that acts during weight
loading, which is where the failure now provably is. **It is the leading
candidate and it is still a candidate** — `:7` reports the exception rather than
assuming it, for the reason §44.4 already gave: changing a serving flag on a
hunch changes what the benchmark measures.

## 46. The flag asked the registry what only the artifact knew (2026-08-25)

### 46.1 The exception, in full

`purple_wolf_g3hhc4q5qj` on `ffsft-bench:7`, read from outside the VNet:

```
ValueError: There is no module or parameter named 'language_model' in
Qwen3_5Model. The available parameters belonging to (Qwen3_5Model) are:
{'layers.44.mlp.down_proj.weight', 'layers.42.linear_attn.out_proj.weight',
 'layers.29.linear_attn.in_proj_qkvz.weight', 'layers.62.linear_attn.A_log', ...}
```

raised in `models/utils.py:395` `_load_module`, reached through
`qwen3_5.py:283 load_weights`. The `:7` window did its job: head, then
`... 108 lines omitted ...`, then the tail that names the failure.

### 46.2 Why it happened

`--language-model-only` tells vLLM to load the language stack of a multimodal
wrapper. The flag's value comes from `serving_env`
(`deploy/endpoint.py:495`): `"1" if spec.multimodal else "0"`, and
`configs/models.yaml:53` marks `qwen3.8-27b` `multimodal: true` with the comment
"measured from the real config.json".

It was. From the **base** model's config.json. The artifact being served is the
merge, and §36.2 had already measured that directly: `Qwen3_5ForCausalLM`,
`model_type: qwen3_5_text`, `diag_has_vision_config: False`. A bare
`Qwen3_5Model` with no `language_model` wrapper to select. The registry
described the model that went in; nothing consulted the model that came out.

Note the near miss: `merge.py` records `architectures` and `model_type` in
`merge_summary.json` precisely because "vLLM has to have that name registered or
the server exits at startup". The right fields were being written and nothing
downstream read them.

### 46.3 The fix, and where it belongs

In `serve_entrypoint.sh`, not in the registry. `resolve_model()` already holds
the path to the artifact's own `config.json`, so the flag is now conditioned on
whether that file contains a `vision_config`. Serving the base model still gets
`--language-model-only`; serving the merge does not. `models.yaml` keeps
`multimodal: true`, which remains true of what it describes.

`ffsft-serve:4` (`de13`), `ffsft-bench:8`.

### 46.4 Three cycles, and what each one bought

| image | what it added | what it returned |
| --- | --- | --- |
| `:5` | MLflow reporting at all | `status: Failed`, `phase: server_exited_1` |
| `:6` | window opens at the first error, not the last | the failure is inside `load_model` |
| `:7` | window keeps both ends; speaker stamp stripped | the exception, verbatim |

Each cycle cost ~10 minutes on a LowPriority A100 and returned exactly one fact.
None of them guessed. The alternative -- changing a serving flag on a hunch --
would have changed what the benchmark measures, and with four candidates open
(§44.4) the odds of picking right were poor.

## 47. 정정 — §43의 정책 주장은 관리형 엔드포인트까지 확장되지 않는다 (2026-08-25)

운영자가 포털에서 직접 조회한 결과를 근거로 반박했고, **반박이 맞다.**

### 47.1 무엇이 틀렸나

§43.3은 테넌트 루트 MG의 `VirtualMachine_SKU_Deny`(Spot이 아닌 GPU SKU를 거부)를
관리형 온라인 엔드포인트가 막히는 이유로 제시했다. 그 정책의 대상은
`Microsoft.Compute/virtualMachines` · `virtualMachineScaleSets` 다.

**관리형 온라인 엔드포인트는 Microsoft 소유 구독의 관리형 컴퓨트에서 돈다.**
고객 구독에 위 리소스로 나타나지 않는다. 따라서 그 정책이 물릴 근거가 없다.
AmlCompute(학습 경로)는 고객 구독에 VMSS를 실제로 만들므로 §43.3은 **그 경로에
한해서만** 유효하다. 학습이 LowPriority로만 도는 이유가 그것이다.

운영자 조회 결과(`Microsoft.MachineLearningServices/*` 정책 평가): deny 0건,
audit 3건. 관리형 엔드포인트 생성을 차단하는 정책 할당은 없다.

### 47.2 실제 병목 — 쿼터

`Microsoft.MachineLearningServices/locations/koreacentral/quotas`, 실측:

| 패밀리 | limit |
| --- | --- |
| `standardNCADSA100v4Family` (A100) | **0** |
| `standardNCADSH100v5Family` (H100) | **0** |
| ND\*A100 / ND\*H100 / H200 / MI300X 전 계열 | **0** |
| `StandardNVADSA10v5Family` (A10 v5) | **72** |

27B bf16(53.8 GB)에 필요한 A100은 쿼터 0. 정책 이전에 쿼터에서 끝난다.

### 47.3 그리고 A10 쿼터는 §40 기록 이후 두 배가 됐다

§5는 `A10 쿼터 36`을 기준으로 `Standard_NV36ads_A10_v5`(36코어 × 롤링 예약 2배
= 72 요구)를 **불가**로 기록했다. 오늘 측정값은 구독·워크스페이스 양쪽 모두
**72 dedicated cores**:

```
/subscriptions/{sub}/quotas/StandardNVADSA10v5Family                        72
/subscriptions/{sub}/.../workspaces/mlw-ffsft/quotas/StandardNVADSA10v5Family 72
type: Microsoft.MachineLearningServices/vmFamily/dedicatedCores/quotas
```

72 요구 / 72 한도 — 정확히 들어간다. **§5의 불가 판정은 더 이상 현재 상태가
아니다.** LowPriority가 아니라 dedicated 쿼터라는 점도 §47.1과 일관된다:
관리형 엔드포인트에는 테넌트의 Spot 강제가 적용되지 않는다.

### 47.4 남는 제약

A10은 24 GB다. 27B bf16 53.8 GB는 들어가지 않는다. 4-bit(약 14 GB)이면 들어간다.
그리고 §26.4가 기록한 AcrPull 역할 전파 문제 — 부여 후 최소 5분 대기 — 는
그대로 유효하다. 그 두 가지가 관리형 엔드포인트 재시도의 조건이다.

## 48. A10 쿼터 72코어는 관리형 엔드포인트 전용이다 (2026-08-25)

§47에서 A10 v5 쿼터가 36이 아니라 72라는 것을 확인했다. 왜 하필 72인지,
그리고 왜 그 쿼터로 AmlCompute 클러스터를 만들 수 없는지는 별도의 API가
답한다.

### 48.1 어떤 SKU가 관리형 엔드포인트를 지원하는가

`Microsoft.MachineLearningServices/locations/koreacentral/vmSizes` 는
SKU마다 `supportedComputeTypes` 를 돌려준다. `MIR`(Managed Inference
Resource)이 관리형 온라인 엔드포인트다. koreacentral GPU SKU 16종:

| SKU | GPU | vCPU | supportedComputeTypes |
|---|---|---|---|
| Standard_NC24ads_A100_v4 | 1×A100 80GB | 24 | AmlCompute, ComputeInstance, MIR |
| Standard_NC40ads_H100_v5 | 1×H100 | 40 | AmlCompute, ComputeInstance, MIR |
| Standard_NV6ads_A10_v5 | 1/6 A10 | 6 | **MIR 전용** |
| Standard_NV12ads_A10_v5 | 1/3 A10 | 12 | **MIR 전용** |
| Standard_NV18ads_A10_v5 | 1/2 A10 | 18 | **MIR 전용** |
| Standard_NV36ads_A10_v5 | 1×A10 24GB | 36 | **MIR 전용** |
| Standard_NV36adms_A10_v5 | 1×A10 24GB | 36 | **MIR 전용** |
| Standard_NV72ads_A10_v5 | 2×A10 48GB | 72 | **MIR 전용** |

A10 v5 계열은 전부 MIR 전용이다. 이 쿼터로는 학습용 클러스터를 만들 수
없고, 오직 관리형 온라인 엔드포인트만 세울 수 있다.

### 48.2 72라는 숫자의 의미

관리형 엔드포인트는 롤링 업데이트를 위해 요청 코어의 2배를 잡는다
(§5, `ONLINE_ENDPOINT_CORE_MULTIPLIER = 2`).

- `Standard_NV36ads_A10_v5` × 1 인스턴스 = 36 × 2 = **72 코어**
- 쿼터 = **72**

즉 이 구독은 A10 관리형 엔드포인트 **정확히 한 인스턴스**만큼만 열려 있다.
여유는 0이다. `Standard_NV72ads_A10_v5`(2×A10, 48GB)는 144가 필요해서
들어가지 않는다.

### 48.3 전용 코어 쿼터 실측 (양쪽 스코프 동일)

| 패밀리 | 구독 | 워크스페이스 |
|---|---|---|
| StandardNVADSA10v5Family | **72** | **72** |
| standardNCADSA100v4Family | 0 | 0 |
| standardNCADSH100v5Family | 0 | 0 |
| Standard NCASv3_T4 Family | 0 | 0 |
| Standard NDASv4_A100 Family | 0 | 0 |
| standardNDv5H100Family | 0 | 0 |

A100은 **전용** 코어가 0이다. 학습이 도는 이유는 LowPriority 쿼터가
따로 있기 때문이고, 관리형 엔드포인트는 LowPriority를 받지 않는다
(§40). 그래서 A100 관리형 엔드포인트는 여전히 불가능하고, A10만 남는다.

### 48.4 남은 조건

A10 24 GB에 27B를 올리려면 4-bit이어야 한다 (bf16 53.8 GB → 불가,
nf4 ≈ 14~15 GB). `docker/serve_entrypoint.sh:102` 가 이미
`QUANTIZATION` 을 `--quantization` 으로 넘긴다. 다만 vLLM이 하이브리드
GDN 아키텍처(`Qwen3_5ForCausalLM`)를 4-bit으로 로드할 수 있는지는
검증되지 않았다. 엔드포인트 롤아웃은 과거 50~113분씩 걸렸으므로,
먼저 A100 잡 안에서 `--gpu-memory-utilization` 을 24 GB 상당으로 묶어
로드 가능 여부만 확인하는 것이 싸다.

## 49. 병합 산출물의 텐서 이름이 그 옆의 config와 다르다 (2026-08-25)

### 49.1 §46은 `:7`에 대해 맞았고 `:8`을 설명하지 못한다

`:8`에서 다시 돌린 `brave_bone_2kbcknyrgr`이 `purple_wolf_g3hhc4q5qj`과
**같은 예외로, 같은 줄에서** 죽었다. 다섯 시간 간격, 문구는 한 글자도
다르지 않다.

빌드 순서·캐시·이미지 내용을 먼저 의심했고 셋 다 증거로 배제했다.
`az acr run` 으로 `ffsft-bench:8` 안의 `serve_entrypoint.sh` 를 직접 읽어
가드가 들어 있음을 확인했다 (md5 `06864ea575f5446cc8b19ec819590358`).

그래서 진단 잡 `silly_ocean_n4k5szy7gj` 이 가드의 **판정 자체**를 태그로
내보내게 했다:

```
diag.guard_decision            = DROP
diag.architectures             = Qwen3_5ForCausalLM
diag.model_type                = qwen3_5_text
diag.vision_config_in_text     = False
diag.has_language_model_prefix = True
diag.has_visual_prefix         = False
weight_prefixes:  850  model.language_model  /  1  lm_head.weight
```

`DROP` — 플래그는 이미 빠지고 있었다. §46의 수정은 실제로 동작했다.
그것이 원인이 아니었을 뿐이다. §46.3은 `:7`에 대한 설명으로는 유효하고,
`:8`의 실패에는 적용되지 않는다.

### 49.2 진짜 원인

병합 산출물 안에서 **config와 텐서 이름이 서로 다른 모델을 가리킨다.**

| | 값 |
|---|---|
| config `architectures` | `Qwen3_5ForCausalLM` (텍스트 전용) |
| config `model_type` | `qwen3_5_text` |
| config `vision_config` | 없음 |
| 텐서 접두사 | `model.language_model.*` 850개 |
| `visual.*` | **0개** |

`AutoModelForCausalLM` 은 멀티모달 체크포인트를 텍스트 전용 클래스로
해석하면서 비전 타워를 실제로 버린다 — 850개, `visual.*` 은 하나도 없다.
하지만 `save_pretrained` 가 쓰는 모듈 트리의 루트는 여전히
`model.language_model.` 이다. transformers 가 멀티모달 래퍼 **안쪽**에
디코더를 두기 때문이다.

vLLM은 config를 믿는다. `Qwen3_5Model` 을 `layers.*` 로 바로 짓고, 들어오는
키에서 앞의 `model.` 만 떼어낸 뒤, 자기 트리에 없는 `language_model`
서브모듈을 찾다가 §46.1의 예외를 던진다.

베이스 모델(`Qwen/Qwen3.8-27B`)과 비교하면 어긋난 지점이 분명하다:
`architectures: ["Qwen3_5ForConditionalGeneration"]`, `model_type: qwen3_5`,
가중치 1199개 (`model.language_model` 850 / `model.visual` 333 /
`mtp.*` 15 / `lm_head` 1). 베이스는 이름과 config가 서로 맞는다.
병합본만 반쪽씩 다른 곳에서 왔다.

### 49.3 고칠 곳은 두 군데다

**저장 시점** — `deploy/merge.py` 에 `text_only_state_dict()` 를 두고
`save_pretrained(state_dict=...)` 로 넘긴다. `model.language_model.` →
`model.`. 접두사가 없던 모델에는 no-op이라 Qwen 특수 분기가 필요 없고,
결과적으로 이름이 **이미 쓰이고 있던 config** 와 일치하게 된다.

**이미 저장된 애셋** — `qwen3_8-27b-ko-merged:1` 은 수정 전에 쓰인
54 GB다. 샤드를 하나씩 읽어 키만 바꿔 다시 쓰는 수밖에 없다. 모델 로딩도
PEFT도 GPU도 없어서 재병합보다 훨씬 싸다.

리네임 잡과 벤치 잡을 나누면 LowPriority 할당을 두 번 기다린다. 할당이
비싼 부분이지 작업이 비싼 부분이 아니므로 한 잡에 합쳤다 —
리네임 결과를 선언된 출력 `fixed` 에 쓰고, 같은 컨테이너에서 그 디렉터리를
평소의 `bench_entrypoint.sh` 에 넘긴다. 출력은 어차피 업로드되므로 끝나면
교정된 애셋으로 등록할 수 있고, **그것이 관리형 엔드포인트에 필요하다**:
엔드포인트는 애셋을 직접 마운트해서 리네임 단계를 끼울 자리가 없다.

잡 `dynamic_ship_yj1dmrfdlp`.

### 49.4 이 사이클이 산 것

| 이미지 / 잡 | 물은 것 | 답 |
|---|---|---|
| `:7` | 예외가 무엇인가 | `language_model` 서브모듈 없음 |
| `:8` | §46 수정이 먹혔나 | 먹혔다. 그리고 여전히 죽는다 |
| `silly_ocean_n4k5szy7gj` | 가드는 뭐라고 판정했나 | `DROP` — 플래그는 원인이 아니다 |
| 〃 | 그럼 무엇이 다른가 | 텐서 이름 850개가 config와 불일치 |

`:8` 이 `:7` 과 똑같이 실패했을 때 플래그를 한 번 더 만지는 선택지가
있었다. 가드의 판정을 직접 물어본 것이 그 길을 닫았다 — 수정이 동작했다는
사실과 실패가 계속된다는 사실이 동시에 참이면, 원인은 다른 곳이다.

## 50. 잡 노드의 로컬 디스크는 64 GB다 — SKU 스펙이 아니라 (2026-08-25)

### 50.1 증상

§49.3 이 설계한 합본 잡(리네임 → 벤치, 한 컨테이너) `dynamic_ship_yj1dmrfdlp`
가 9.5분 만에 죽었다. MLflow 에는 `rename.alive`, `rename.src`,
`rename.src_files = 21` 만 있고 트레이스백이 없다 — 프로세스가 예외를 던진
게 아니라 **kill 됐기 때문**이다. 잡이 자기 실패 이유를 쓸 기회를 못 얻는
경우가 있다는 뜻이고, MLflow 만으로는 여기서 막힌다.

### 50.2 이유를 받아낸 경로 — Run History REST API

블랙박스가 아니었다. 아래는 블롭을 거치지 않는다:

```
GET https://koreacentral.api.azureml.ms/history/v1.0/subscriptions/{sub}
    /resourceGroups/{rg}/providers/Microsoft.MachineLearningServices
    /workspaces/{ws}/runs/{runId}
Authorization: Bearer <token for https://ml.azure.com>
```

`error` / `warnings` 필드에 진짜 이유가 담겨 있었다:

```
DiskFullError: Disk full while running job. Please consider reducing amount of
data accessed, or upgrading VM SKU. Total space: 64197 MB, available space:
1332 MB (under AZ_BATCH_NODE_ROOT_DIR).
```

**이것은 새로 확인된 진단 채널이다.** 이 워크스페이스에서 노드 바깥으로
나오는 경로는 지금까지 MLflow 하나뿐이라고 §42 에 적혀 있었다. 정확히는
*잡이 스스로 쓰는* 채널이 MLflow 하나이고, **플랫폼이 쓰는 채널은 Run
History API 로 따로 읽을 수 있다.** 잡이 kill 되는 종류의 실패는 후자로만
보인다.

### 50.3 사실

| | |
|---|---|
| SKU | `Standard_NC24ads_A100_v4` |
| `AZ_BATCH_NODE_ROOT_DIR` 총량 | **64197 MB (≈64 GB)** |
| 잡 시작 시 남은 양 | 이미지 ~9 GB 차감 후 |
| 54 GB 다운로드 1개 후 | **1332 MB** |

`bench_job.py` 주석은 이 SKU 에 "~1 TB" 로컬 디스크가 있다고 적고 있었다.
SKU 카탈로그는 맞다. **Azure ML 이 잡을 그 디스크 위에서 돌리지 않는다.**
주석은 측정값으로 교체했다.

### 50.4 따라오는 제약

- **다운로드된 모델은 노드당 한 개만 들어간다.** 27B bf16 = 54 GB.
- **54 GB급 산출물을 로컬에 쓸 수 없다.** 선언된 출력을 `rw_mount` 로 잡아
  블롭에 직접 스트리밍해야 한다.
- 따라서 §49.3 의 합본 설계(입력 54 GB 다운로드 + 출력 54 GB 로컬)는
  **처음부터 불가능했다.** 할당 대기를 아끼려고 한 잡에 합쳤는데, 아끼려던
  대상이 아니라 아무도 재보지 않은 한도에 부딪혔다.

### 50.5 판단 오류

이 실패의 원인은 Azure 가 아니라 설계다. 리포지터리 주석 하나("~1 TB")를
검증 없이 믿고 그 위에 54+54 GB 잡을 얹었다. 더 큰 문제는 그 잡이 **왜
존재했는가**다 — `merge.py` 수정을 배송하려면 훈련 이미지 재빌드 11분이
필요했고, 그 11분을 피하려고 일회성 잡(진단 → 리네임 → 헤더 수술)을
쌓아 올렸다. **이미 한 번 성공한 적 있는 경로**(이미지 태그 올리고 병합 잡
재실행, `khaki_energy_wfxwtw3l23`)가 내내 있었다. 새 잡 모양을 발명하는
비용이 재빌드보다 쌀 것이라는 가정이 틀렸다.

## 51. 정정 — 관리형 엔드포인트는 막혀 있지 않다. 프로브가 틀린 질문을 했다 (2026-08-25)

### 51.1 무엇이 뒤집혔나

§43 은 "이 구독에서 모든 GPU SKU 가 `NotAvailableForSubscription`" 이라고
결론지었다. §47 이 그중 정책 주장을 이미 철회했고, **이 절은 나머지를
철회한다.**

| §43 의 주장 | 실제 |
|---|---|
| GPU SKU 배포 불가 | 엔드포인트 생성 **69초에 성공** |
| 프로브가 그것을 증명했다 | 프로브는 **다른 리소스**를 테스트했다 |

측정값:

```
existing endpoints: (none)
ENDPOINT CREATED in 69s
  name        : ffsft-ep-probe
  scoring_uri : https://ffsft-ep-probe.koreacentral.inference.ml.azure.com/score
  state       : Succeeded
```

### 51.2 프로브가 왜 뒤집힌 답을 냈나

`endpoint.py::probe_sku()` 는 `AmlCompute` **클러스터**를 만들어 본다.
관리형 온라인 엔드포인트는 다른 리소스 타입이고 다른 컨트롤 플레인이다.

koreacentral 의 A10 v5 SKU 6종은 전부 `supportedComputeTypes = MIR` 이다 —
AmlCompute 가 목록에 없다. 그러므로:

> **엔드포인트가 받아주는 SKU에 대해서만 정확히 이 프로브가 "불가"를
> 반환한다.**

우연한 실패가 아니라 구조적 반전이다. 프로브의 답을 배포 가능성으로 읽는
한 A10 은 영원히 불가로 나온다. docstring 을 스코프 명시로 교체했다.

### 51.3 방법론

이 프로젝트에서 사용자의 반박이 내 증거 정리를 이긴 것이 이번이 두 번째다
(첫 번째는 §47 의 Azure Policy). 두 경우 모두 같은 모양이다:

- 나: 표(쿼터 표, 프로브 출력)를 읽고 결론을 냈다
- 실제: **그 작업을 한 번도 시도하지 않았다**

`probe_sku` 의 원래 docstring 은 "이것이 배포 가능한지에 대한 유일하게
정직한 답"이라고 적고 있었다. 그 문장이 재확인을 막았다 — 리포지터리 자신의
주석이 검증을 대신하는 자리에 앉으면, 그 주석의 스코프가 틀렸을 때 틀린
결론이 근거를 갖춘 것처럼 보인다. **VERIFIED 의 항목은 무엇을 쟀는지와 함께
무엇을 재지 않았는지도 말해야 한다.**

### 51.4 여전히 열려 있는 것

엔드포인트 생성이 되는 것과 **GPU 배포가 롤아웃되는 것**은 다른 질문이다.
첫 배포 시도는 11초 만에 반려됐는데 쿼터가 아니라 validation 이었다:

```
(InferencingClientCallFailed) "No Environment was provided.
 Environment is required for custom model formats."
```

커스텀 모델 배포는 `environment` 를 명시해야 한다. `ffsft-serve:4` 를
`inference_config`(liveness/readiness `/health`, scoring
`/v1/chat/completions`, 포트 8000)와 함께 등록해 재시도했고,
`StartCreateDeploymentAsync` 가 실제로 시작됐다. 프로비저닝 결과는 별도
항목으로 기록한다 — MIR 은 쿼터를 비동기로 거절하는 경우가 있어
**작업 접수는 쿼터 통과의 증거가 아니다.**

### 51.5 §37.1 파생 갭의 실물

`SERVE_IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:3"` 인데 AML 환경으로
등록된 `ffsft-serve` 는 1, 2 뿐이었다. ACR 에는 3 과 4 가 다 있다. 이미지
태그에서 환경 버전을 파생시키는 `ENVIRONMENT_VERSION = image_tag(...)` 규칙이
훈련 쪽에만 있고 서빙 쪽에는 없어서, **빌드된 이미지가 배포에서 보이지
않는 상태**가 두 태그 동안 유지됐다. 배포가 `environment` 를 요구하는 순간
표면화됐다.

## 52. 존 제한이 진짜 벽이다 — 쿼터도 정책도 아니다 (2026-08-25)

§51 이 "엔드포인트는 생성된다"까지 밝혔다. 이 절은 **그럼 무엇이 막는가**에
대한 측정이다.

### 52.1 층별 실측

| 층 | 결과 | 근거 |
|---|---|---|
| Azure Policy | 통과 | §47 |
| RBAC / 엔드포인트 생성 | **성공 69초** | §51 |
| A10 v5 쿼터 (koreacentral) | **72코어** (sub·ws 양쪽) | quotas API |
| 배포 요청 접수 | **성공** | `StartCreateDeploymentAsync` |
| **GPU 노드 할당** | **실패** | 아래 |

### 52.2 제한의 정체

```
Standard_NV36ads_A10_v5   (A10 v5 6종 전부 동일)
  locationInfo.zones                 = ['2', '3']
  restrictions[].type                = Zone
  restrictions[].reasonCode          = NotAvailableForSubscription
  restrictions[].restrictionInfo.zones = ['1', '2', '3']
```

**제공되는 존이 2·3인데 제한 목록이 1·2·3이다. 남는 존이 0이다.**

쿼터 숫자(72)와 사용 가능성이 분리돼 있다는 것이 이 건의 핵심이다. 쿼터
표만 읽으면 "NV36 한 대 들어감"으로 보이고, 실제로는 노드가 영원히 안 나온다.
관리형 엔드포인트는 LowPriority 를 거부하므로 AmlCompute 처럼 제한을 무시하는
별도 풀로 도망갈 수 없다. §40 의 롤아웃 5회 × 50~113분 × `percentComplete 0.0`
이 전부 이 한 줄이다.

### 52.3 리전 우회는 없다

A10 ML 쿼터 (`StandardNVADSA10v5Family`):

| 리전 | limit |
|---|---|
| koreacentral | 72 (단, 전 존 제한) |
| koreasouth / japaneast / southeastasia | 0 |
| eastus / westus3 / westeurope | 0 |

워크스페이스를 옮기면 *제한된 grant* 를 *grant 없음* 으로 바꾸는 것이다.

### 52.4 koreacentral 에 남은 GPU 쿼터

| 패밀리 | limit | 쓸 수 있나 |
|---|---|---|
| `StandardNVADSA10v5Family` | 72 | 존 제한으로 불가 |
| `standardNCFamily` | 100 | K80, CC 3.7 — vLLM 최소 7.0 미만 |
| `standardNVFamily` | 100 | M60, CC 5.2 — 동일. 메모리도 부족 |
| A100 / H100 / T4 / ND* | 0 | — |

CPU 쿼터는 넉넉하다 (`standardESv3Family` 1000, D/E 계열 다수 100).
**1.3B 급 모델을 CPU 관리형 엔드포인트에 올려 엔드포인트 표면 자체 -- scoring
URI, 키 인증, Azure 라우팅 왕복 -- 를 실증하는 것은 가능하다.** 27B 성능
수치는 A100 잡 경로가 담당한다.

### 52.5 남는 결론

GPU 관리형 엔드포인트는 **엔타이틀먼트 요청 없이 도달할 수 없다.** 이번
프로젝트에서는 그 요청을 하지 않기로 했으므로, 27B 로드테스트는 A100
LowPriority 잡 안에서 vLLM 과 클라이언트를 같은 컨테이너에 두고 수행한다
(`ffsft.serve.bench_job`). 잃는 것은 TLS·엔드포인트 인증·WAN 1홉이고,
남는 것은 실제 A100 위 실제 27B 의 TTFT/TPOT/p50·p95·p99 다.

---

## 53. 텐서 이름 수정은 되돌려지고 있었다 — `save_pretrained` 가 (2026-08-25)

### 53.1 세 번째 같은 실패

`ffsft-train:11` 을 새로 빌드하고(14분 47초), 병합 잡
`mighty_pin_ll2vg38n1k` 을 A100 LowPriority 에서 12분 53초에 **Completed**
로 끝내고, `qwen3_8-27b-ko-merged:2` 로 등록하고, 벤치 잡
`nifty_neck_d3b9x8z5x9` 를 41분 돌린 끝에 나온 것은 앞의 두 번과 **글자
그대로 같은 에러**였다:

```
ValueError: There is no module or parameter named 'language_model' in
Qwen3_5Model
```

`purple_wolf_g3hhc4q5qj`, `brave_bone_2kbcknyrgr` 에 이어 세 번째다. 다른
점은 이번엔 **고쳤다고 믿는 코드가 이미지 안에 들어 있었다**는 것이다.

### 53.2 이미지는 결백했다

가설 1은 "빌드에 수정이 안 들어갔다"였다. ACR 안에서 이미지를 직접 실행해
확인했다 (`az acr run --registry acrffsftkc /dev/null --cmd "<image> python -c ..."`,
4분 31초):

```
TEXT_PREFIX_FIX ('model.language_model.', 'model.')
has_fn True
```

수정은 들어 있었다. **이미지 문제가 아니다.** 이 확인 자체가 재사용 가능한
도구다 — 배포된 이미지 내용을 노드를 잡지 않고 4분에 검사할 수 있다.

### 53.3 진짜 원인 — 저장 시 역변환

`transformers/modeling_utils.py:3497`:

```python
if save_original_format and not is_offloaded and not _hf_peft_config_loaded:
    state_dict = revert_weight_conversion(model_to_save, state_dict)
```

`from_pretrained` 는 **체크포인트 이름 → 런타임 이름** 변환을 적용하면서 그
매핑을 `model._weight_conversions` 에 기록해 둔다. `save_pretrained` 는
기본값 `save_original_format=True` 로 그 매핑을 **역방향으로 재생**한다.
원본 체크포인트와 같은 이름 규약으로 저장하기 위해서다.

따라서:

| | |
|---|---|
| 허브 체크포인트 이름 | `model.language_model.layers.*` |
| `from_pretrained` 후 런타임 이름 | `model.layers.*` |
| `model.state_dict()` 가 주는 것 | `model.layers.*` — **이미 맞는 이름** |
| `text_only_state_dict` 가 리네임한 개수 | **850 중 0개** |
| `save_pretrained` 가 디스크에 쓴 것 | `model.language_model.layers.*` |

`text_only_state_dict` 는 잘못된 지점에 있었다. 고치려던 대상은 저장 *후*
파일의 이름인데, 손을 댄 곳은 저장 *전* 런타임 딕셔너리다. 리네임은 매칭이
0건이었고, 설령 매칭됐더라도 그 뒤 역변환이 되돌렸을 것이다.

### 53.4 수정

`save_original_format=False` 가 디스크상의 이름을 결정하는 스위치다.

### 53.5 그런데 그것만 믿지 않는다

`save_pretrained` 는 버전에 따라 이 인자를 `**kwargs` 로 받는다. 모르는
이름은 **거부가 아니라 무시**된다. 즉 "플래그를 넘겼다"와 "디스크의 이름이
바뀌었다"는 여전히 서로 독립적인 두 사실이다. 이번 회차의 교훈이 정확히
그것이므로, `assert_servable_names()` 가 저장 직후 실제 파일을 읽어 검증한다:

- 샤딩됐으면 `model.safetensors.index.json` 의 `weight_map` 키,
- 단일 파일이면 safetensors 헤더(리틀엔디언 8바이트 길이 + JSON)를 직접 파싱.
  54 GB 를 읽지 않고 수 KB 로 끝난다.
- `config.json` 에 `vision_config` 가 없는데 텐서 이름에 `language_model.`
  이 있으면 **거부**한다. 이름 블랙리스트가 아니라 **설정과 이름의 정합성
  검사**다 — 진짜 멀티모달 체크포인트에서는 그 이름이 옳다.
- `docker/serve_entrypoint.sh:88` 의 가드가 같은 `config.json` 에 같은 질문을
  던진다. 핸드오프 양쪽에서 같은 것을 본다.

비용 대비: 이 검사는 병합 잡 끝에서 밀리초가 든다. 이것이 없어서 든 비용은
`nifty_neck_d3b9x8z5x9` 의 41분 — A100 할당, 9 GB 이미지 풀, 54 GB 다운로드
끝의 같은 ValueError.

### 53.6 판단 오류

§50.5 에 이어 두 번째로 기록한다. 이번 오류는 **검증 없이 고쳤다고 선언한
것**이다. `text_only_state_dict` 에는 "Renaming on the way out costs nothing
and makes the names agree with the config" 라는 주석이 달려 있었다. 그 문장은
검증된 적이 없었고, 그 상태로 이미지 빌드 15분 + 병합 13분 + 벤치 41분을
소비했다.

단위 테스트는 통과하고 있었다. 함수가 하는 일 자체는 맞았기 때문이다 —
틀린 것은 **그 함수가 문제를 푼다는 전제**였고, 그건 순수 함수 테스트로는
잡히지 않는다. 그래서 이번엔 테스트를 산출물 쪽으로 옮겼다: 디스크에 쓰인
이름을 읽는 테스트가 실제 보호막이고, 기존 리네임 테스트는 남기되 무엇을
지키는 테스트가 아닌지 모듈 독스트링에 명시했다.

### 53.7 부수 발견 — `SERVE_IMAGE` 가 `:3` 에 멈춰 있었다

같은 회차에 확인한 별건. `deploy/endpoint.py` 의 `SERVE_IMAGE` 는 `:3` 인데,
텍스트 전용 체크포인트에 `--language-model-only` 를 넘기지 않도록 막는
가드(`serve_entrypoint.sh:88`)는 `:4` 에만 있다. 벤치 경로는
`ffsft-bench:8` 을 만들면서 `:4` 로 옮겼지만 이 상수는 따라오지 않았다.

존 제한이 풀려 노드가 할당되는 순간, 관리형 배포는 이 프로젝트가 만드는 바로
그 모델로 `purple_wolf_g3hhc4q5qj` 와 같은 에러를 냈을 것이다. `:4` 로 올리고,
§51.5 가 지적한 버전 파생 누락도 같이 닫았다 — `SERVE_ENVIRONMENT_VERSION =
image_tag(SERVE_IMAGE)`, 그리고 `serve_environment()` 가 등록된 버전이 다른
이미지를 들고 있으면 거부한다. 세 등록 지점(train / bench / endpoint)이 이제
같은 모양이다.

### 53.8 디스크 예방

§50 의 64 GB 측정에 따라 벤치 잡의 캐시를 배치 루트 밖으로 뺐다
(`VLLM_CACHE_ROOT`, `TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR`,
`XDG_CACHE_HOME` → `/mnt/*`). 벤치 이미지 9.2 GB + 27B 다운로드 54 GB 면
배치 루트에 1.3 GB 밖에 안 남고, 64층 모델의 CUDA 그래프 캡처와 인덕터
컴파일은 거기 못 들어간다. 이번 벤치가 디스크로 죽지 않고 모델 로드까지
도달한 것은 이 조치 덕분이다 — 실패 지점이 앞당겨지지 않았다.

### 53.9 이번에는 고치기 전에 확인했다 — 오프로드 경로까지

§53.6 의 교훈이 "검증 없이 수정을 선언했다" 였으므로, `save_original_format`
쪽은 잡을 돌리기 전에 세 가지를 먼저 확인했다.

첫째, 이미지 안의 transformers 가 그 인자를 실제로 받는가. `az acr run` 으로
`ffsft-train:11` 을 열어 확인했다 (Run de19, 4분 30초):

```
TFVER 5.15.1
HAS_SOF True
HAS_KWARGS True
```

`HAS_KWARGS True` 가 중요한 이유는, `**kwargs` 를 받는 시그니처였다면 모르는
인자가 조용히 무시됐을 수 있기 때문이다. `HAS_SOF True` 이므로 이름 있는
인자로 도달한다.

둘째, 로컬 transformers 도 같은 5.15.1 이다. 따라서 로컬에서 읽은
`modeling_utils.py` 소스는 이미지의 소스와 같은 파일이고, 소스 확인이 곧
이미지 확인이다.

셋째 — 그리고 이게 하마터면 놓칠 뻔한 부분 — 되돌리기 경로는 하나가 아니라
둘이다.

```
219  if save_original_format and not is_offloaded and not _hf_peft_config_loaded:
220      state_dict = revert_weight_conversion(model_to_save, state_dict)
...
289  if is_offloaded and save_original_format and not _hf_peft_config_loaded:
291      shard_state_dict = revert_weight_conversion(model_to_save, shard_state_dict)
```

219 는 메모리에 다 올라온 경우 state_dict 전체를 한 번에 되돌리고, 289 는
오프로드된 경우 meta 텐서를 디스크에서 되읽은 뒤 **샤드마다** 되돌린다.
`is_offloaded` 는 `hf_device_map` 에 `cpu` 나 `disk` 가 하나라도 있으면 참이고
(184-187), 이 프로젝트의 머지는 `--device-map auto` 로 27B 를 GPU 와 호스트
RAM 에 걸쳐 올린다. 즉 우리가 실제로 타는 경로는 289 쪽일 가능성이 높다.

두 분기 모두 `save_original_format` 하나에 걸려 있으므로 `False` 면 둘 다
꺼진다. 만약 219 만 보고 "오프로드가 아니면 안 되돌린다" 로 읽었다면, 정작
우리가 타는 경로는 안 막힌 채로 또 한 번 41분짜리 벤치를 태웠을 것이다.

그래도 `assert_servable_names` 는 남겨둔다. 위 세 가지는 "이 인자가 되돌리기를
끈다" 까지만 보장하고, "디스크에 실제로 쓰인 이름" 은 보장하지 않는다. 후자는
파일을 읽어야만 알 수 있는 사실이고, 그 확인은 머지 끝에서 13분 만에 나온다 —
벤치까지 41분을 더 태우고 알게 되는 것과 다르다.

## 54. 관리형 엔드포인트가 막힌 진짜 축 — 쿼터가 아니라 dedicated 엔티틀먼트

### 54.1 프로브의 종료 상태

`ffsft-ep-probe/probe` (`Standard_NV36ads_A10_v5`, 1대)를 끝까지 돌려
종료 상태를 받았다. ARM 비동기 오퍼레이션:

```
startTime       2026-08-25T04:36:57Z
endTime         2026-08-25T06:29:45Z
status          Failed
percentComplete 0.0
error.code      InternalServerError
error.message   Internal error. Please see troubleshooting guide ...
```

112분 동안 진행률 0%. 컨테이너 로그는 `inference-server` 와
`storage-initializer` 양쪽 모두 "There are no logs for this deployment" —
컨테이너가 뜬 적이 없다. 즉 앱 실패가 아니라 노드가 끝내 안 붙은 것이다.

주의할 점: **이 에러 메시지는 존 제한이라고 말해주지 않는다.** 제네릭한
플랫폼 오류다. 이 실패가 확정해 주는 사실은 "요청은 수락됐고, 노드는 할당되지
않았고, 플랫폼이 112분 뒤 포기했다" 까지다. 왜 할당이 안 됐는지는 아래의
별개 조회가 답한다.

### 54.2 `restrictions` 는 하드웨어 가용성이 아니라 dedicated 엔티틀먼트다

`Microsoft.Compute/skus` 를 koreacentral 로 필터해 다시 읽었다:

```
Standard_NC24ads_A100_v4    offered=['3']      restricted=ALL-LOCATION   NotAvailableForSubscription
Standard_NC48ads_A100_v4    offered=['3']      restricted=ALL-LOCATION   NotAvailableForSubscription
Standard_NC96ads_A100_v4    offered=['3']      restricted=ALL-LOCATION   NotAvailableForSubscription
Standard_NV6ads_A10_v5      offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
Standard_NV12ads_A10_v5     offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
Standard_NV18ads_A10_v5     offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
Standard_NV36ads_A10_v5     offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
Standard_NV36adms_A10_v5    offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
Standard_NV72ads_A10_v5     offered=['2','3']  restricted=['1','2','3']  NotAvailableForSubscription
```

여기서 §51 의 해석을 정정해야 한다. **A100 도 제한 목록에 있다.** 그런데 이
프로젝트는 바로 그 `Standard_NC24ads_A100_v4` 에서 학습(`jovial_quill_gk4l78b515`),
머지(`magenta_machine_32zzg0fv0r`), 벤치를 실제로 돌리고 있다. 제한이 걸린
SKU 에서 잡이 도는 것이다.

따라서 이 `restrictions` 목록은 "이 하드웨어를 쓸 수 없다" 가 아니라
"**dedicated(온디맨드)로 살 수 없다**" 를 뜻한다. AmlCompute 의
**LowPriority(스팟)** 는 다른 할당 경로를 타므로 이 제한에 걸리지 않는다.
§47 에서 사용자가 지적했던 "GPU 를 Spot 으로만 쓰게 강제한다" 가 정확히
이것이었고, 그 관측이 옳았다.

### 54.3 그래서 관리형 엔드포인트만 걸린다

| 경로 | 스팟 선택지 | 이 구독 | 결과 |
|---|---|---|---|
| AmlCompute 잡 (A100) | 있음 (`LowPriority`) | 스팟 가능 | 동작 |
| 관리형 온라인 엔드포인트 (A10) | **없음** | dedicated 불가 | 수락되나 미할당 |

관리형 온라인 엔드포인트에는 우선순위/스팟 옵션이 없다. dedicated 로만 노드를
잡는다. 그래서 A10 쿼터가 72 코어로 **넉넉히 있어도** 소용이 없다 — 쿼터는
"얼마나" 를 재는 축이고, 엔티틀먼트는 "살 수 있느냐" 를 가르는 별개 축이다.
쿼터 숫자만 보고 "쿼터는 있는데 왜 안 되지" 를 반복한 회차들이 이 구분을
놓쳤던 것이다.

### 54.4 사용자의 문제 제기가 다시 맞았다

"매니지드 엔드포인트가 안대ㅗ >> 왜안되는건데 내부문서에서 된다는데" —
내부 문서가 맞다. 기능은 있고, Azure Policy 도 막고 있지 않고(§47), 권한도
막혀 있지 않고, 엔드포인트 생성 자체는 69초에 성공한다. 막힌 것은 구독의
dedicated GPU 엔티틀먼트 하나이고, 관리형 엔드포인트는 스팟으로 내려갈
방법이 없어서 그 하나에 걸린다.

세 번째다 — Azure Policy(§47), 관리형 엔드포인트 가능 여부(§51), 그리고
이번. 매번 "증거상 막혀 있다" 는 내 결론보다 "된다는데" 라는 사용자의
문제 제기가 옳았고, 매번 원인은 내가 보던 축이 아니라 옆 축에 있었다.

## 55. 벤치는 학습과 다른 모드의 모델을 재고 있었다 — 게다가 그 눈금자는 (2026-08-25)
   추론 토큰을 못 봤다

`plum_wall_318nsvlvt6` 은 **서빙에는 성공했다.** vLLM 이 뜨고, 스모크가
통과하고, 6개 동시성 레벨 스윕이 끝까지 돌았다. §53 의 텐서 이름 블로커는
사라졌다. 그런데 잡은 Failed 로 끝났다 — 매 레벨마다 64개 중 40개가
`"no tokens streamed"` 로 집계됐기 때문이다.

### 55.1 평평함이 단서였다

| 동시성 | ok | no tokens streamed |
|---|---|---|
| 1 | 24 | 40 |
| 2 | 24 | 40 |
| 4 | 24 | 40 |
| 8 | 24 | 40 |
| 16 | 22 | 42 |
| 32 | 24 | 40 |

부하 때문에 생기는 실패는 동시성에 따라 **움직인다.** 동시성 1과 32가
똑같이 40이면 그건 큐의 성질이 아니라 **입력의 성질**이다. `loadtest.py` 는
`prompts[i % len(prompts)]` 로 8개 프롬프트를 순환하고 레벨당 64요청이므로
프롬프트 하나당 정확히 8회다. 40 = 5 × 8, 24 = 3 × 8. 즉 8개 중 5개
프롬프트가 **항상** 실패하고 3개가 **항상** 성공했다. 부하 문제가 아니다.

### 55.2 결함은 두 개였고, 서로 독립이다

**(a) 눈금자가 추론 토큰을 못 봤다.** 서버는 `--reasoning-parser qwen3` 로
뜬다(`serve_entrypoint.sh`). 이 플래그가 켜지면 Qwen3 의 `<think>` 블록은
`delta["content"]` 가 아니라 `delta["reasoning_content"]` 로 나간다.
`loadtest.py:169` 는 `delta.get("content")` 만 셌다. 그래서 `max_tokens=128`
안에 사고가 안 끝난 요청은 **GPU 가 128 토큰을 실제로 디코딩했는데도**
"토큰 하나도 못 받음" 으로 채점됐다.

**(b) 클라이언트가 학습과 다른 모드를 요청했다.** 레지스트리는
`chat_template_kwargs = {'enable_thinking': False}` 를 선언하고
`bench_job.serving_env` 는 그걸 **서버 쪽에만** 반영했다. 요청 `body` 에는
`chat_template_kwargs` 가 아예 **없었다.**

(b)가 단순한 벤치 편의 문제가 아닌 이유: `train/qlora.py:154-155` 가
`enable_thinking=false` 로 학습 프롬프트를 렌더링했고 "추론이 이것과 반드시
동일해야 한다" 고 명시해 뒀다. 사고 모드를 켠 채 잰 숫자는 **느린 모델의
숫자가 아니라 다른 모델의 숫자**다. 비교 가능한 성능 수치로 발표할 수 없다.

### 55.3 세 파일을 고쳤다

- `serve/loadtest.py` — `_one_request` 가 `chat_template_kwargs` 를 받아
  `body` 에 싣는다. 토큰 카운터는 `content` **또는** `reasoning_content` 를
  센다. TTFT 도 둘 중 먼저 온 토큰에서 잰다. `--chat-template-kwargs` CLI
  플래그를 추가하고, 파싱 실패는 무시가 아니라 `SystemExit` 이다 — 조용히
  버리면 스윕 전체가 잘못된 모드로 돌고 그 숫자가 비교 가능한 것처럼
  발표된다. 리포트 dict 에 실제 사용값을 기록한다.
- `serve/bench_job.py` — `bench_env` 가 레지스트리 값을
  `BENCH_CHAT_TEMPLATE_KWARGS` 로 내보낸다. 레지스트리가 아무것도 선언하지
  않으면 `"{}"` 가 아니라 **변수 자체를 안 만든다** — 서버에 도달하는
  방식이 다르다.
- `docker/bench_entrypoint.sh` — 스윕과 **스모크 둘 다** 같은 kwargs 로
  질문한다. 다른 모드로 묻는 스모크는 아무도 안 한 질문에 답한다.

### 55.4 이번에도 배선을 고정하는 테스트를 먼저 붙였다

`tests/test_bench_reasoning_mode.py` 12개. 그중 두 개가 방향을 양쪽으로
잡는다: `test_a_response_that_never_leaves_thinking_is_still_a_success`
(눈금자가 좁아지는 것을 막고), `test_a_stream_with_no_deltas_at_all_is_still_a_failure`
(눈금자가 **너무 넓어져서** 진짜 실패까지 성공으로 세는 것을 막는다).
계측 버그를 고칠 때 후자를 빼먹으면 다음 회차에 조용히 전부 통과한다.

전체 스위트 588 통과, ruff 클린. `ffsft-bench:9` 에 실린다.

## 56. §54 정정 — 쿼터 표를 두 개 봐야 했다. 그리고 그 쿼터는 (2026-08-25)
   실패한 배포가 물고 있었다

§54 는 "쿼터는 있는데(A10 72코어) 엔티틀먼트가 없다" 로 끝냈다. 72 라는
숫자는 맞았지만 **"있다" 가 틀렸다.** 72 는 한도였고 사용량도 72 였다.

### 56.1 쿼터 네임스페이스가 두 개다

| API | 항목 | koreacentral |
|---|---|---|
| `Microsoft.Compute` `usage.list` | `Standard NVADSA10v5 Family vCPUs` | 0/36 |
| `Microsoft.MachineLearningServices` `usages.list` | `Standard NVADSA10v5 Family Cluster Dedicated vCPUs` | **72/72** |

관리형 온라인 엔드포인트와 AmlCompute 는 **ML 네임스페이스**에서 끌어 쓴다.
§54 까지 나는 Compute 네임스페이스만 보고 있었다 — 숫자도 다르고(36 vs 72)
사용량도 안 보이는 표였다. 틀린 표를 정확하게 읽고 있었던 셈이다.

### 56.2 72 중 36 만 설명된다 — 나머지는 누수다

삭제 직전 워크스페이스에 남은 배포는 `ffsft-ep-probe/probe` 하나,
`instance_type=Standard_NV36ads_A10_v5`, `instance_count=1` = **36 vCPU**.
구독 내 ML 워크스페이스는 이 하나뿐이다(`az resource list` 로 확인).
그런데 사용량은 72 였다. 나머지 36 은 §40 의 실패한 롤아웃들이 **반납하지
않고 잡고 있던 예약분**이다. 즉 두 번째 배포 시도부터는 쿼터가 이미 0 이었고,
그 상태로 5회를 더 던졌다.

`Failed` 상태의 배포는 삭제하기 전까지 쿼터를 계속 점유한다. 실패한 배포를
"증거로 남겨두는" 행위 자체가 다음 시도를 막고 있었다.

### 56.3 삭제하니 전부 풀렸다

```
                                        삭제 전    삭제 후
Standard NVADSA10v5 ... Cluster Dedicated  72/72  ->   0/72
Total Cluster Dedicated Regional vCPUs   72/1072  -> 0/1072
```

엔드포인트 삭제는 159초. 누수분 36 까지 같이 반납됐다.

### 56.4 LowPriority 는 한도가 -1 이다

```
Total Cluster Low Priority Regional vCPUs:        0/300
Standard NVADSA10v5 Family Cluster LowPriority:   0/-1
Standard NCADSA100v4 Family Cluster LowPriority:  0/-1
```

`-1` = 무제한. 학습·병합·벤치가 A100 LowPriority 에서 아무 저항 없이 돈 이유가
여기 있다. dedicated 만 0 이고 spot 은 사실상 안 잠겨 있다.

### 56.5 리전을 옮겨도 안 된다 — 두 축이 서로 어긋나 있다

전 리전 SKU 제한을 훑으니 koreacentral 은 GPU SKU 11개 중 **0개** 열림,
반면 13개 리전이 열려 있다(southeastasia, eastus2, westus3, swedencentral,
francecentral, …). 그런데 그 리전들의 ML dedicated GPU 쿼터는 **전부 0**이다.

| 리전 | SKU 엔티틀먼트 | ML dedicated 쿼터 |
|---|---|---|
| koreacentral | 막힘 (11개 중 0개) | 72 (삭제 후 가용) |
| southeastasia / eastus2 / westus3 | 열림 | **0** |

두 조건을 동시에 만족하는 리전이 없다. 리전 이전은 결국 쿼터 증설 요청을
부르고, 그건 사용자가 명시적으로 배제한 경로다("있는 쿼터로 진행").
그래서 남은 유일한 무료 실험은 **koreacentral 에서 쿼터를 비우고 다시
던져보는 것**이다.

### 56.6 restriction 필드는 여전히 신뢰할 수 없다

koreacentral 제한 레코드:

| SKU | type | zones | reason |
|---|---|---|---|
| `Standard_NC24ads_A100_v4` | Location | – | NotAvailableForSubscription |
| `Standard_NV36ads_A10_v5` | **Zone** | 1,2,3 (전부) | NotAvailableForSubscription |

A100 은 리전 전체 차단으로 뜨는데 **LowPriority 로는 실제로 돌아간다**(지금
이 프로젝트의 학습·벤치 전부). 그러므로 이 필드는 "할당 가능 여부" 가 아니라
dedicated 구매 자격만 기술한다 — §54.2 의 결론은 유지된다. A10 은 type 이
Zone 이고 세 존이 모두 걸려 있어서, 쿼터를 비워도 벽이 남아 있을 수 있다.
그래서 이번 재시도는 결론이 아니라 **판별 실험**이다: 붙으면 벽은 쿼터였고,
같은 방식으로 또 실패하면 벽은 엔티틀먼트다.

### 56.7 교훈

사용자의 "이거 못풀어??" 가 네 번째로 옳았다. §47(Policy), §51(엔드포인트
가능 여부), §54(엔티틀먼트), 그리고 이번 쿼터 네임스페이스. 매번 나는
"증거상 막혔다" 로 닫았고, 매번 내가 안 본 표가 하나 더 있었다.

이번의 구체적 형태: **숫자를 확인했다는 것과 올바른 숫자를 확인했다는 것은
다르다.** 나는 A10 쿼터를 실제로 조회했고 72 를 읽었고 그걸 근거로 "쿼터는
문제가 아니다" 라고 두 회차에 걸쳐 단언했다. 조회한 표가 그 리소스가 쓰는
표가 아니었다는 점만 확인하지 않았다.

### 56.8 판별 실험의 답 — 벽은 쿼터가 아니었다

쿼터를 `0/72` 로 완전히 비운 직후 `ffsft-deploy check --probe` 로 **실제 create
호출**을 던졌다(수락돼도 min=0 으로 만들고 즉시 삭제하는 무료 프로브):

```
aks_vllm         BLOCKED  InvalidPropertyValue. Standard_NV36ads_A10_v5 cannot be
                          created in this workspace at either tier, however many
                          cores the catalogue and the usage APIs advertise.
aml_batch        ok       LowPriority Standard_NC24ads_A100_v4 (create accepted)
aml_batch_vllm   ok       LowPriority Standard_NC24ads_A100_v4 (create accepted)
aml_online_vllm  BLOCKED  InvalidPropertyValue. Standard_NV12ads_A10_v5 cannot be
                          created in this workspace at either tier ...
local_vllm       n/a
```

쿼터가 72코어 비어 있는 상태에서도 A10 은 **가장 작은 NV12ads 조차** 거부된다.
`at either tier` — dedicated 뿐 아니라 **LowPriority 로도** 거부다. 반면 A100 은
같은 순간에 LowPriority create 가 수락된다.

따라서 벽은 A10 패밀리에 대한 구독 엔티틀먼트다. §54 의 결론이 최종적으로
맞았고, §56.2 의 쿼터 누수는 **두 번째 독립 결함**이었을 뿐 원인은 아니었다.
쿼터를 비운 것은 헛수고가 아니라 이 판별을 가능하게 한 전제였다 — 꽉 찬
상태에서 거부됐다면 두 원인을 구분할 수 없었다.

### 56.9 남은 경로

| 경로 | 상태 | 비고 |
|---|---|---|
| 관리형 **온라인** 엔드포인트 (A10) | ❌ | 엔티틀먼트. 지원 요청 필요 |
| 관리형 **온라인** 엔드포인트 (A100) | ❌ | dedicated 쿼터 limit=0. 증설 요청 필요 |
| **배치** 엔드포인트 (A100 LowPriority) | ✅ | create 수락 확인됨 |
| AmlCompute 잡 + vLLM (현재 방식) | ✅ | 게이트 3 완주 |

즉 "AML 엔드포인트" 를 쓰고 싶다면 **배치 엔드포인트는 지금 당장 만들 수
있다.** 실시간 HTTPS 스코어링은 아니지만 SageMaker Batch Transform 에 대응하는
진짜 엔드포인트 리소스다. 실시간 온라인 엔드포인트만 요청 없이는 불가능하다.

## 57. 축은 리전이었다 — 관리형 온라인 엔드포인트가 실제로 떴다 (2026-08-26)

§54 는 벽이 dedicated 엔티틀먼트라고 결론냈고 §56.8 은 그것을 판별 실험으로
확정했다. 둘 다 맞았지만 둘 다 **koreacentral 안에서만** 측정한 결과였다.
남은 축은 리전이었다.

### 57.1 같은 구독, 다른 리전, 정반대의 답

`Microsoft.Compute/skus` 의 `restrictions` 를 두 리전에서 나란히 읽으면:

| SKU | koreacentral | polandcentral |
|---|---|---|
| `Standard_NC24ads_A100_v4` | BLOCKED (Location) | **FREE** |
| `Standard_NC48ads_A100_v4` | BLOCKED (Location) | **FREE** |
| `Standard_NC*as_T4_v3` | BLOCKED (Location) | **FREE** |
| `Standard_ND96isr_H200_v5` | (없음) | **FREE** |
| `Standard_NV*ads_A10_v5` | BLOCKED (Zone) | BLOCKED (Location,Zone) |
| `Standard_ND96isr_H100_v5` | BLOCKED | BLOCKED |

koreacentral 은 **전용 GPU SKU 가 하나도 없다.** A10 이 특별히 막힌 게 아니라
그 리전에서 이 구독이 dedicated GPU 를 아예 살 수 없다. LowPriority 로는
A100 이 되기 때문에 학습만 되고 관리형 엔드포인트는 안 되는 비대칭이 생겼다
— 관리형 온라인 엔드포인트에는 스팟 옵션이 없다.

polandcentral 로 옮기자 A100 계열이 전부 `restrictions: []` 로 열린다.
ML 쿼터도 `standardNCADSA100v4Family 0/48` 로 잡혀 있었다.

**교훈:** §54·§56 은 "이 구독은 dedicated 를 못 산다" 로 읽혔지만 정확히는
"이 구독은 **koreacentral 에서** dedicated 를 못 산다" 였다. 구독 단위로
보이는 제약이 실제로는 구독×리전 단위였다.

### 57.2 스토리지 사전점검은 관리형 VNet 으로 푼다

`endpoint.py` 의 도달성 규칙은 `public_access != "Disabled"` 이거나
`private_endpoints > 0` 이면 통과다. 이 테넌트는 모든 스토리지 계정의
`publicNetworkAccess` 를 상위에서 `Disabled` 로 강제한다 — `--public-network-access
Enabled` 로 **직접 만든 빈 계정조차** `Disabled` 로 돌아오는 것으로 재확인했다.
따라서 첫 번째 조건은 이 테넌트에서 영원히 거짓이다.

두 번째 조건은 만들 수 있다. 워크스페이스를
`managedNetwork.isolationMode = AllowInternetOutbound` 로 PATCH 하고
`provisionManagedNetwork` 를 호출하면 AML 이 자기 스토리지/키볼트로 가는
private endpoint 를 **자동으로** 만든다. 약 3 분 뒤 PE 가 나타나고 7 분 뒤
`Active` 가 된다. 그 시점에 사전점검이 통과한다.

### 57.3 노드 존재 증명은 로그가 아니라 메트릭이다

`get_logs` 는 배포가 `Creating` 인 **동안 내내** 같은 문자열을 돌려준다:

```
Deployment is in deleting or creating state so logs can't be retrieved.
```

노드가 없어서 못 읽는 것과 노드가 있는데 아직 준비 중이라 못 읽는 것을
구분하지 못한다. 컨테이너 로그 폴링은 노드 할당 탐지기로 쓸 수 없다.

**배포 리소스**(엔드포인트 리소스가 아니다)의 Azure Monitor 메트릭은
인스턴스 위에서 도는 에이전트만 내보낼 수 있으므로 독립적인 축이 된다.
같은 순간 두 배포를 나란히 재면:

| | provisioningState | DeploymentCapacity | GpuUtilization |
|---|---|---|---|
| polandcentral A100 | Creating | 포인트 7개 | 포인트 7개 |
| japaneast A10 (73분 경과) | Creating | **0개** | **0개** |

둘 다 `Creating` 이고 둘 다 같은 로그 플레이스홀더를 준다. 메트릭만 갈린다.

### 57.4 실측 기동 타임라인 — 프로브 예산 산정식의 근거

`GpuMemoryUtilizationPercentage` 를 1 분 간격으로 읽어 얻은 첫 실측값:

```
16:05  0%   노드 할당 (메트릭 방출 시작)
16:05-16:16 0%   이미지 풀 + HF 에서 54 GB 다운로드   ← 11분
16:17  34%  가중치 GPU 적재 시작
16:18  66%  적재 완료 (54 GB / 80 GB)
16:21  88%  KV 캐시 할당 완료, 서빙 시작
```

배포 생성(15:58)부터 `Succeeded`(16:21)까지 **23 분**.

`startup_grace_for(27)` 는 `120 + 27×25 = 795 초` 를 준다. 이 예산은 초과되지
**않았다** — 이미지 풀이 프로브 시계 밖에 있기 때문이다. liveness 의
`initial_delay` 는 컨테이너 시작 시점부터 세고, 이미지 풀은 그 이전이다.
"다운로드가 11 분이니 795 초 예산이 모자란다" 는 추론은 틀렸다.

### 57.5 `--mamba-cache-mode` 는 생략해도 떴다

이 배포는 `MAMBA_CACHE_MODE=""`, `REASONING_PARSER=""` 로 나갔다(§57.7 의
버그 3 때문에). 그럼에도 컨테이너는 정상 기동했고 한국어 응답도 정확했다.

`docs/SERVING.md` 는 이 플래그를 "`align` 필수" 로 적고 있지만, 실제로 참인
명제는 "**모드 `all` 이 `NotImplementedError` 를 낸다**" 이고 vLLM 의 기본값은
`all` 이 아니다. 생략이 곧 실패는 아니다. 다만 측정해서 고른 값이므로
정상 경로에서는 여전히 `align` 을 보내야 한다.

`--reasoning-parser` 쪽은 다르다. 생략하면 **조용히 틀린 응답을 준다.**
같은 엔드포인트에 "대한민국의 수도는?" 을 보내면 `content` 가 이렇게 온다:

```
'The user is asking in Korean: "What is the capital of South Korea? ..."
 ... I need to answer in one sentence in Korean.
</think>

대한민국의 수도는 서울입니다.'
```

추론 트레이스와 `</think>` 가 사용자에게 나갈 본문에 그대로 섞인다.
`--reasoning-parser qwen3` 를 주면 vLLM 이 이 앞부분을 `reasoning_content`
필드로 떼어내고 `content` 에는 답만 남긴다. 200 OK 이고 한국어도 맞아서
스모크 테스트는 통과하지만 출력은 틀렸다 — **기동 성공은 서빙 플래그가
맞다는 증거가 아니다.** 응답 본문을 봐야 잡힌다.

### 57.6 로드테스트 — 게이트 3 통과

polandcentral A100 NC24ads 1 인스턴스, `Qwen/Qwen3.8-27B` bf16, `max_model_len 8192`:

```
 conc    ok  fail  TTFT p50  TTFT p95  TPOT p50   e2e p95     tok/s    req/s
    1    20     0     1.142     1.190    0.0364     5.779      22.0     0.18
    2    20     0     1.141     1.238    0.0364     5.813      43.4     0.36
    4    20     0     1.136     1.329    0.0371     6.055      83.0     0.68
    8    20     0     1.275     1.563    0.0382     6.364     131.6     1.08
   16    20     0     1.523     1.855    0.0407     6.987     204.3     1.68
```

100/100 성공, 실패 0. p95 TTFT 2.0 초 SLO 를 만족하는 최대 동시성 **16**.
동시성 16 배에 TPOT 는 12 % 만 나빠지고 처리량은 9.3 배가 된다 — 연속 배칭이
의도대로 동작한다는 뜻이다. SSE 스트리밍도 토큰 단위로 확인했다.

### 57.7 이 회차에서 고친 리포 버그 4 개

1. **쿼터 패밀리를 SKU 가 아니라 패턴에서 읽었다.** `--sku` 오버라이드가
   패밀리를 넘어갈 수 있는데 사전점검은 패턴의 *기본* SKU 패밀리를 읽었다.
   A100 배포가 A10 풀(0 코어)을 보고 거부됐다. `quota_family_for()` 추가,
   `blocked_reason(..., quota_family=)` 로 실제 패밀리를 전달.

2. **ACR 스코프를 배포 리소스그룹으로 가정했다.** `acrffsftkc` 는
   `rg-ffsft-kc` 에 있는데 polandcentral 배포는 `rg-ffsft-plc` 로 id 를 만들어
   ARM 404 → AcrPull 부여 실패. `acr_id_for_image` 를 구독 전체 이름 조회로
   바꾸고 실패 시에만 기존 가정으로 폴백.

3. **측정한 서빙 플래그가 통째로 누락됐다.** `--hf-model` 은 Hub id 만 주므로
   `model_spec` 이 None 이 되고 `mamba_cache_mode`/`reasoning_parser` 가 빈
   문자열로 나갔다. `registry.by_hf_id()` 추가로 id → 스펙 복구.

4. (1) 의 부수 효과로 `spec.blocked_reason` 의 메시지가 잘못된 패밀리 이름을
   출력하던 것도 함께 수정.

### 57.8 남은 격차

이 회차에 뜬 것은 **베이스** `Qwen/Qwen3.8-27B` 다. 파인튜닝 산출물
`qwen3_8-27b-ko-merged:3` 은 koreacentral 워크스페이스 스토리지에 있고 그
스토리지는 §57.2 의 테넌트 강제 때문에 polandcentral 에서 읽을 수 없다.

교차 리전 전송 대신 **polandcentral 에서 학습을 다시 돌리는** 쪽이 낫다:
학습 데이터는 HF 에서 받으므로(`configs/datasets.yaml`) 리전에 묶여 있지
않고, LowPriority A100 은 polandcentral 에서 `TotalLowPriorityCores 0/300`
으로 열려 있으며, 산출물이 처음부터 plc 스토리지에 생겨 전송 문제가 사라진다.
학습 이미지가 koreacentral ACR 에 있는 것은 문제가 아니다 — §57.6 의 배포가
`acrffsftkc.azurecr.io/ffsft-serve:4` 를 교차 리전으로 이미 당겨왔다.

---

## 58. 도달성과 자격증명은 다른 축이다 — PE 2개로 통과한 사전점검이 쓰기를 전부 놓쳤다 (2026-08-26)

### 58.1 증상은 §24 와 똑같았고, 원인은 달랐다

polandcentral 에서 학습을 돌리자 §24 와 동일한 증상이 나왔다 — 완료된 런의
`artifacts=0`, 로그 파일 0개, `jobs.download()` 실패. §24 의 결론은 "스토리지가
아무에게도 도달 불가"였고 리포의 `classify_store` 가 그걸 그대로 구현하고 있다.

그런데 plc 는 그 점검을 **통과**한다:

    mlwffsftstorage09dd66111  publicNetworkAccess=Disabled  privateEndpoints=2
    -> classify_store: reachable=True

PE 2개가 살아 있고 실제로 도달도 된다. 네트워크 판정은 맞았다. 그런데 쓰기는
전부 실패했다. 결정적 오류 코드:

    (KeyBasedAuthenticationNotPermitted) Key based authentication is not
    permitted on this storage account.

### 58.2 진짜 축 — 데이터스토어가 제시하는 자격증명

AML 워크스페이스의 데이터스토어 4개는 각각 `credentialsType` 을 가진다.
같은 구독, 같은 방식으로 만든 두 워크스페이스가 정반대로 나왔다:

| | kc `mlw-ffsft` | plc `mlw-ffsft-plc` | jpe `mlw-ffsft-jpe` |
|---|---|---|---|
| `workspaceblobstore` | `None` | **`AccountKey`** | **`AccountKey`** |
| `workspaceartifactstore` | `None` | **`AccountKey`** | **`AccountKey`** |
| `workspaceworkingdirectory` | `None` | **`AccountKey`** | **`AccountKey`** |
| `workspacefilestore` | `None` | **`AccountKey`** | **`AccountKey`** |
| 스토리지 `allowSharedKeyAccess` | False | False | False |
| 스토리지 `publicNetworkAccess` | Disabled | Disabled | Disabled |
| private endpoints | 2 | 2 | 0 |

스토리지가 `allowSharedKeyAccess=false` 인데 데이터스토어는 계정 키를 내민다.
계정이 그 키를 거부하므로 **모든 쓰기 경로가 같은 이유로 죽는다** — 잡 로그
업로드, 아티팩트 업로드, 출력 마운트, 클라이언트 `jobs.download()` 까지.

**PE 와 RBAC 로는 못 고친다.** 네트워크 경로와 무관하게 거부되기 때문이다.
공개 엔드포인트가 켜져 있어도 똑같이 거부된다 — 두 축은 직교한다.

kc 가 `None` 으로 나온 이유는 모른다. 배포 경로로부터 추론할 수 없다는 것만
확인했다. 그러니 **읽어서 확인하는 것 외에 방법이 없다.**

### 58.3 수정

데이터스토어 4개를 전부 `credentialsType: "None"`(ID 기반)으로 PUT 하고,
워크스페이스 MSI / 클러스터 ID / 내 사용자 계정에 대상 스토리지의
Storage Blob Data Contributor 를 부여했다.

`workspaceblobstore` PUT 이 한 번 거부됐다:

    The datastore workspaceblobstore is currently the workspace default,
    so it cannot be updated with isDefault set to false.

`isDefault` 를 페이로드에서 빼면 ARM 이 `false` 로 간주한다. **기본
데이터스토어를 PUT 할 때는 `isDefault: true` 를 명시적으로 유지해야 한다.**

인증 경로가 실제로 바뀐 것은 오류 코드가 이동한 것으로 확인했다:
`KeyBasedAuthenticationNotPermitted` → `AuthorizationPermissionMismatch`
(AAD 경로로 넘어갔다는 증거) → 역할 부여 후 해소.

### 58.4 사전점검이 이 축을 못 봤다 — 고쳤다

`classify_store` 는 도달성을 두 가지로만 모델링했다: 공개 접근 켜짐, 또는
PE 존재. plc 는 후자로 통과했다. 세 번째 실패 방식이 빠져 있었다.

`src/ffsft/deploy/endpoint.py` 를 고쳤다:

* `classify_store(..., allow_shared_key=None, key_based_datastores=())` —
  `allowSharedKeyAccess=false` 와 `AccountKey` 데이터스토어가 **둘 다 측정된**
  경우에만 차단한다.
* `probe_model_store` 가 데이터스토어 목록을 추가로 읽는다(ARM GET 3회로 증가,
  여전히 읽기 전용).
* `_key_based_datastores` 는 목록을 못 읽으면 `[]` 를 돌려준다 — 이 모듈의
  기존 규칙("못 보는 프로브 ≠ 고장난 리소스")을 자격증명 축에도 그대로 적용.
  `allow_shared_key=None`(안 읽힘)은 절대 차단하지 않는다.
* 두 축이 동시에 고장이면 **둘 다 보고한다.** jpe 가 그 경우다(PE 0 + AccountKey 4).
  하나만 알려주면 고치고 다시 막히는 왕복이 생긴다.

살아있는 3개 워크스페이스에 실제로 돌린 결과:

    OK      mlw-ffsft        keyed=0 PEs=2
    OK      mlw-ffsft-plc    keyed=0 PEs=2
    BLOCKED mlw-ffsft-jpe    keyed=4 PEs=0

`(none)` 이 JSON 경로 오류로 인한 거짓 음성이 아님은 raw `credentialsType`
덤프와 대조해 확인했다(위 표). jpe 가 양성 대조군 역할을 한다 — 실제 결함
워크스페이스에서 발화하므로 이 점검은 절대 안 켜지는 장식이 아니다.

### 58.5 마운트 실패는 내가 만든 경합이었다

데이터스토어를 고치는 도중 제출한 `cyan_sail_2x88dpvrn6` 이 1분 만에 죽었다:

    Failed to mount URI azureml://.../datastores/workspaceblobstore/paths/...
    at mount point /mnt/azureml/cr/j/.../model_dir

`workspaceblobstore` 는 내가 마지막으로 고친 데이터스토어였고, 잡이 마운트할
때는 아직 `AccountKey` 였다. **가설로만 두고** 전부 고친 뒤 재제출
(`boring_arm_y1kt6q4vr2`) 해서 판별했다 — 같은 1분 구간을 통과했으므로 구조적
차단이 아니라 경합이었다.

### 58.6 §24 정정

§24 는 "완료된 런 3개가 전부 `artifacts=0`" 을 스토리지 도달성 **하나의**
원인으로 돌렸다. 그 결론은 koreacentral 에 대해서는 맞았을 수 있지만
**증상에서 원인으로 가는 추론이 유일하지 않다.** 같은 증상을 내는 원인이
최소 둘이고, 리포의 점검은 그중 하나만 봤다. 도달성이 초록이라고 해서
쓰기가 된다는 뜻이 아니다.

### 58.7 잡 로그 본문은 이 머신에서 읽을 방법이 없다 — 메트릭이 유일한 채널

데이터스토어를 고쳐도 **로그 본문 읽기는 여전히 안 된다.** 별개 문제다.
쓰기는 노드가 하고(고쳐졌다), 읽기는 내 노트북이 한다(막혀 있다).

측정한 것:

| 경로 | 결과 |
|---|---|
| `az storage blob list --auth-mode login` | 네트워크 규칙 차단 |
| 런히스토리 `/details` 의 SAS URI 를 curl | `403 AuthorizationFailure` |
| 아티팩트 프록시 `/artifact/v2.0/.../artifacts/content/...` | `400` — **"APIs that proxy blob retrieval have been removed. Request a SAS link instead."** |
| `/history/v1.0/.../runs/{run}/metrics` | `404` |
| `POST /metric/v2.0/.../runs/{run}/lastvalues` | **`200`** |

프록시 API 가 제거됐으므로 우회로가 없다. 스토리지
`publicNetworkAccess=Disabled` 인 한 SAS 는 VNet 밖에서 항상 403 이다.
`/details` 가 돌려주는 **파일 이름 목록은** 제어 평면이라 읽히므로, 로그
업로드가 되는지 여부는 확인할 수 있다 — 내용만 못 읽는다.

`src/ffsft/train/report.py` 는 이미 이 사실 위에 설계돼 있다. HF Trainer 는
`report_to=[]` 로 꺼져 있고(`qlora.py:246`, `preflight.py:162`), 대신
`publish()` 가 MLflow 로 두 번 보낸다:

* `setup.*` — `trainer.train()` **전에**. 로우프라이오리티 노드가 중간에
  선점되면 "모델을 못 올린 잡"과 구별이 안 되기 때문이다.
* `train.*` — 완료 후. `train_loss`, `steps`, `wall_seconds`, `vram_peak_gb`.

**따라서 진행 상황 감시기는 로그가 아니라 메트릭을 폴링해야 한다.** 로그
SAS 를 긁는 감시기는 조용히 아무것도 못 뱉고, 그건 "학습이 멈췄다"와 구별이
안 된다 — 이 회차에 그런 감시기를 하나 만들었다가 kc 대조군으로 403 을
확인하고 버렸다.


---

## 59. 재배포가 서빙 중인 배포를 덮어쓰고 있었다 (2026-08-26)

`deploy_online` 은 배포 이름 `"blue"` 를 하드코딩하고 트래픽을
`{"blue": 100}` 으로 무조건 설정했다. 엔드포인트 리소스는 처음부터 여러 개의
이름 있는 배포를 지원하는데 **호출자가 이름을 정할 수 없었다.**

결과: 모든 재배포가 현재 트래픽을 받고 있는 배포를 제자리에서 덮어쓴다.
기동에 실패하는 롤아웃은 엔드포인트를 같이 죽이고, 복구 방법은 20분짜리
배포를 한 번 더 도는 것뿐이다. §57.4 에서 이 배포가 23분 걸린 것을 측정했다.

`--deployment` 와 `--traffic` 을 추가했다:

    ffsft-deploy deploy-online --endpoint ffsft-plc \
      --deployment green --traffic 0 ...

`--traffic 0` 은 트래픽 맵을 **건드리지 않고** 새 배포를 기존 배포 옆에
올린다. 정상 응답을 확인한 뒤 두 번째 호출로 트래픽을 옮긴다. 기본값은
`blue` / `100` 이라 기존 호출자의 동작은 그대로다.

`--traffic` 이 100 미만이면 나머지를 받을 배포가 정확히 하나여야 한다.
Azure 는 트래픽 맵의 합이 100 이기를 요구하므로, 다른 배포가 0개거나 2개
이상이면 남는 퍼센트를 어디에 줄지 결정할 수 없다 — 조용히 추측하지 않고
거부한다.

테스트에 `main` 이 두 플래그를 실제로 전달하는지 확인하는 항목을 넣었다.
파서만 받고 `main` 이 흘리는 플래그는 없느니만 못하다 — 성공을 보고하면서
`blue` 를 덮어쓰기 때문이다.


---

## 60. 새로 만든 클러스터는 권한이 하나가 아니라 둘이다 (2026-08-26)

### 60.1 선점은 가설이 아니라 측정된 사건이다

`boring_arm_y1kt6q4vr2` (LowPriority, `gpu-a100-lp`) 는 13분을 돌다가 죽었다.
런 히스토리에 남은 문구:

    Low-Priority compute preemption warning: node
    tvmps_d903baf8acc7f70607c727c33fb31f1f6af6128b0e5eb6ef2720964f691cc8de_p
    has been preempted.

**재시작은 이어받기가 아니라 처음부터다.** 27B 가중치 ~54GB 를 다시 받는다.
그리고 재큐잉된 잡은 노드를 다시 받는다는 보장이 없다 — 이 회차에 실제로
`nodeStateCounts` 가 전부 0 인 채 `targetNodeCount=1` 로 7분 넘게 멈춰 있었다.
스팟은 "싸게 되는 것"이지 "되는 것"이 아니다.

그래서 Dedicated 클러스터 `gpu-a100-ded` 를 따로 만들어 같은 잡을 병렬로
넣었다. 두 풀은 서로를 굶기지 않는다 — 별개의 쿼터이기 때문이다:

| 쿼터 | 사용/한도 |
|---|---|
| `standardNCADSA100v4Family` (dedicated) | 24 / 48 |
| `TotalLowPriorityCores` | 24 / 300 |
| `TotalDedicatedCores` | 24 / 148 |

dedicated 24 코어 + 엔드포인트가 잡고 있는 24 = 48/48. NC24ads 가 **정확히
하나** 더 들어간다. 이 회차의 여유는 0이다.

### 60.2 새 클러스터에 필요한 부여는 두 개다. 나는 하나만 줬다

`submit_training.py` 는 클러스터를 만들지 않는다 (`Unknown compute target`).
SDK 로 `gpu-a100-lp` 를 그대로 본떠 `gpu-a100-ded` 를 만들었다.

여기서 **AmlCompute 클러스터는 새로 만들 때마다 새 시스템 할당 identity 를
받는다.** 이전 클러스터의 권한은 하나도 따라오지 않는다.

이걸 알고 있었기 때문에 §58 에서 데이터스토어를 identity 기반으로 바꾼 것을
떠올려, 잡을 넣기 **전에** 새 principal 에 Storage Blob Data Contributor 를
먼저 줬다. 그건 맞았다.

그리고 거기서 멈췄다. 잡은 75초 만에 죽었다:

    Failed to pull Docker image `acrffsftkc.azurecr.io/ffsft-train:12` due to:
    DockerResponseServerError { status_code: 401,
      message: "error from registry: authentication required" }

학습 이미지는 워크스페이스의 ACR 이 아니라 **koreacentral 의
`acrffsftkc`** 에 있다. 다른 리소스 그룹, 다른 리전이다. 그래서 이미지 풀은
컴퓨트 identity 의 `AcrPull` 을 탄다. ACR 스코프의 롤 할당을 뽑아보면
답이 그대로 나와 있었다:

    AcrPull  69f8e424-8d42-49ac-acc0-a05532a81331   <- gpu-a100-lp   (있음)
    (없음)   a66cfbc0-d797-4d55-8720-e626b3e48b1b   <- gpu-a100-ded

`gpu-a100-lp` 는 만들 때 부여받은 적이 있어서 잘 돌았고, 나는 그 사실을
"클러스터를 만들면 되더라"로 기억하고 있었다.

**부여는 이 저장소 구성에서 최소 두 개다:**

    # 1. 데이터스토어가 identity 기반이므로 (§58)
    az role assignment create --assignee-object-id $PID \
      --assignee-principal-type ServicePrincipal \
      --role "Storage Blob Data Contributor" --scope $STORAGE_ID

    # 2. 학습 이미지가 워크스페이스 밖 ACR 에 있으므로
    az role assignment create --assignee-object-id $PID \
      --assignee-principal-type ServicePrincipal \
      --role AcrPull --scope $ACR_ID

principal id 는 `computes/<name>` 의 `identity.principalId` 다.

### 60.3 왜 이게 §58 과 같은 실수인가

§58 은 "도달성을 쟀는데 자격증명 축을 안 쟀다"였다. 이건 "identity 축을
알고 있었는데 그 축 위의 항목을 하나만 셌다"이다. 같은 모양이다: **한
축에서 한 항목을 확인한 것이 그 축을 확인한 것이 아니다.**

새 컴퓨트 identity 를 만들었으면 물어야 할 것은 "무슨 권한을 줘야 하지"가
아니라 **"이전 identity 가 가지고 있던 롤 할당이 전부 뭐였지"** 다. 후자는
조회로 답이 나오고 (`az role assignment list --assignee <old-pid> --all`),
전자는 기억에 의존한다.

### 60.4 실패 이유를 감시기가 직접 말하게 했다

이 실패를 감시기는 `DED [4min] Failed` 라고만 뱉었다. 원인은 내가 런 히스토리
`/details` 를 손으로 뒤져서 찾았다. 상태 전이가 `Failed` 일 때 `error.error.message`
를 같이 뽑아 `DED-REASON [4min] ...` 으로 찍도록 감시기를 고쳤다. 상태만
알려주는 감시기는 사람을 부르기만 하고 답은 안 주는 감시기다.

한 가지 주의: 이 `/details` 호출은 `--resource https://ml.azure.com/` 토큰이
필요하다. ARM 토큰으로 부르면 `401 Bearer token not provided` 가 나오고,
이건 "권한 없음"처럼 보이지만 실제로는 **잘못된 audience** 다.

### 60.5 §58.4 의 나머지 절반 — `preflight.py`

§58.4 에서 `classify_store` 에 자격증명 축을 넣었다. 그런데 배포 preflight 에
**실제로 연결되어 있는 것은** `endpoint.py:787` 이 부르는
`preflight.storage_blocker` 쪽이었다. 이쪽은 여전히 같은 사각지대였다.

`StorageReachability` 에 `allow_shared_key` 와 `key_based_datastores` 를
추가하고, 기존 `storage_blocker` 를 `_network_blocker` 로 이름만 바꾼 뒤
(메시지 문구는 한 글자도 안 바꿔서 기존 테스트 17개가 그대로 통과한다)
`_credential_blocker` 를 새로 만들어 둘을 합치는 `storage_blocker` 를 씌웠다.

두 구현 모두 같은 규칙을 지킨다:

* **못 본 것은 고장난 것이 아니다.** `allow_shared_key=None` 은 "안 읽었다"
  이고 절대로 차단하지 않는다. 측정된 `False` 와 측정된 `AccountKey`
  데이터스토어가 **같이** 있을 때만 차단한다. 데이터스토어 목록을 못 읽으면
  `[]` 로 내려간다.
* **두 차단 사유가 동시에 있으면 둘 다 보고한다.** 하나만 말하면 호출자가
  고치고-다시확인하고-또고치는 왕복을 한다. jpe 가 실제로 그 경우다 (PE 0개
  **그리고** AccountKey 4개).

라이브 3개 워크스페이스에 돌린 결과:

    OK      mlw-ffsft        keyed=0 PEs=2
    OK      mlw-ffsft-plc    keyed=0 PEs=2
    BLOCKED mlw-ffsft-jpe    keyed=4 PEs=0

jpe 가 양성 대조군 역할을 했다. kc/plc 가 둘 다 `(none)` 이라 처음에는
**점검이 아예 안 터지는 것**과 구별이 안 됐다 — 원시 `credentialsType` 덤프로
확인하고 나서야 진짜 음성이라는 걸 알았다. 아무것도 안 잡는 점검과 잡을 게
없는 점검은 출력이 같다.

전체 스위트 610개 통과.

### 60.6 주석으로 적어둔 부여를 코드가 하게 했다

`ensure_compute` 는 클러스터를 system-assigned identity 로 만들고 나서 이렇게
적어두고 있었다:

    # The identity still needs data-plane roles
    # (Storage Blob Data Contributor, and AcrPull for a custom image).

**둘 다 안 만든다.** 그리고 나는 이 주석을 읽은 상태에서 하나만 줬다. 기억해야
할 항목의 개수가 바로 틀린 그 항목이므로, "다음엔 둘 다 기억하자"는 고침이
아니다. identity 를 만드는 쪽이 부여도 하게 옮겼다.

과정에서 나온 별개의 결함 하나: `ArmRoleAuth.create_role(scope, principal, role)`
은 `role` 을 인자로 받고 **본문에서 무시한 채 항상 AcrPull GUID 를 썼다.**
스토리지 롤을 요청한 호출자는 레지스트리 롤을 받고 성공했다는 답을 듣는다 —
검증 단계가 통과해버리기 때문에 여기서 가능한 가장 비싼 모양의 버그다. 이름→GUID
역맵을 만들고 모르는 롤은 `ValueError` 로 거부하게 했다.

그리고 등가 롤 판정을 롤마다 나눴다. `Contributor` 는 AcrPull 을 함의하지만
**블롭 데이터 평면 권한은 전혀 주지 않는다.** 그게 주는 건 계정 키를 읽을
권리이고, 그건 `allowSharedKeyAccess=false` 가 닫는 바로 그 문이다. 하나의
등가 집합을 두 롤에 같이 쓰면, 이 점검이 존재 이유인 구성에서 통과한다.

`ensure_compute` 는 생성할 때만이 아니라 매 호출마다 부여를 시도한다.
`ensure_role` 은 이미 가진 롤이면 아무것도 쓰지 않으므로, 이렇게 해야 손으로
만들어둔 클러스터도 고쳐진다 — `gpu-a100-ded` 가 정확히 그 경우였다.

### 60.7 감시기가 메트릭이 도착하는 순간 조용해질 뻔했다

`lastvalues` 는 메트릭 이름은 등록됐고 값은 아직 안 온 구간에서 `value: [null]`
을 준다:

    {"name": "setup.examples", "columns": {"setup.examples": "Double"},
     "value": [null]}

감시기 파서는 그 `None` 에 `.get()` 을 했다. 죽고, 아무것도 안 찍고, stderr 는
이벤트가 아니라 출력 파일로 간다. 즉 **메트릭이 나오기 시작하는 바로 그 순간
감시기가 조용해지고, 그건 "학습이 멈췄다"와 구별이 안 된다.** §58.7 에서 버린
SAS 감시기와 같은 실패 모양을 다른 원인으로 다시 만든 것이다.

이름이 등록된 것 자체가 신호다 — `setup.*` 는 스크립트가 설정 블록에 도달했다는
뜻이고, `train.*` 는 학습이 반환했다는 뜻이다. 값이 없어도 찍게 고쳤고, 실제
`[null]` 페이로드로 파서를 따로 돌려 확인한 뒤 붙였다.

### 60.8 학습 클러스터와 엔드포인트는 같은 dedicated 풀을 먹는다

green 을 blue 옆에 올리려면(§59) NC24ads 24코어가 더 필요하다. 배포를 시작하기
전에 재봤다:

    vmFamily/dedicatedCores   standardNCADSA100v4Family   48/48
    vmFamily/lowPriorityCores standardNCADSA100v4Family   24/무제한

**dedicated 여유 0.** blue 엔드포인트 24 + `gpu-a100-ded` 24 = 48. 이름이 같은
행이 두 개 나오는데 구별은 `type` 에 있다 — `dedicatedCores` 와
`lowPriorityCores` 는 다른 풀이고, LowPriority 쪽은 한도가 `-1`(무제한)이라
`gpu-a100-lp` 는 이 계산에 안 들어온다.

그래서 배포 게이트의 순서가 강제된다:

1. 학습 완료
2. 병합 잡은 **LowPriority** 클러스터로 (dedicated 를 안 먹는다)
3. green 을 올리기 **전에** `gpu-a100-ded` 가 0노드로 내려가야 한다
   (`min_instances=0`, `idle_time_before_scale_down=900` 이라 잡 종료 15분 뒤
   자동으로 내려간다)
4. 그때 blue 24 + green 24 = 48/48 로 정확히 들어맞는다. 여유는 여전히 0이므로
   트래픽을 옮기고 blue 를 지우기 전까지 다른 dedicated 작업은 못 넣는다

이걸 배포 시작 전에 재두지 않았으면, 23분짜리 롤아웃(§57.4)을 돌린 뒤에
쿼터로 실패하는 걸 봤을 것이다.

---

## 61. 배포 게이트의 스토리지 축은 열려 있다 — 추론이 아니라 측정 (2026-08-26)

학습이 도는 동안 **다음 게이트가 실패할 수 있는 이유를 미리 측정**했다. 23분짜리
롤아웃 도중에 알아내는 것보다 지금 아는 편이 싸다.

### 61.1 `--hf-model`이 성공했다는 사실은 `--model-uri`에 대해 아무것도 증명하지 않는다

`blue`는 살아 있고 A100 위에서 서빙 중이다. 그러나 배포 본문을 읽어보면:

```
provisioningState        : Succeeded
egressPublicNetworkAccess: Enabled
model                    : None          <-- 모델 자산이 아예 없다
instanceType             : Standard_NC24ads_A100_v4
```

`model: None`. `--hf-model`은 컨테이너가 시작할 때 vLLM이 HuggingFace에서 직접
받는 경로라 **워크스페이스 스토리지를 한 번도 건드리지 않는다**(§24). 즉 blue의
성공은 스토리지 축을 한 번도 시험한 적이 없다. `green`은 `--model-uri
azureml:qwen3_8-27b-merged:1`로 뜨므로 storage-initializer가 블롭에서 모델을
가져와야 하고, 여기서 처음으로 그 축을 밟는다.

**이것이 §58·§60과 같은 모양의 함정이다.** "지금 잘 돌아가고 있다"가 "내가
바꾸려는 축도 잘 돌아간다"를 뜻하지 않는다.

### 61.2 측정한 실제 상태

plc 워크스페이스 스토리지 `mlwffsftstorage09dd66111`:

| 항목 | 값 |
|---|---|
| `publicNetworkAccess` | **Disabled** |
| `allowSharedKeyAccess` | **False** |
| `networkAcls.bypass` | `None` (AzureServices 아님) |
| 승인된 private endpoint | **2개** |
| 워크스페이스 `managedNetwork.isolationMode` | `AllowInternetOutbound` (status Active) |
| AccountKey 기반 데이터스토어 | **0개** (4개 전부 identity 기반) |

리포 자신의 프리플라이트를 살아 있는 타깃에 그대로 돌린 결과:

```
ISOLATED_MODES = {'allowinternetoutbound', 'allowonlyapprovedoutbound'}
  public_access_off: True
  trusted          : False
  private_endpoints: 2
  isolated         : True        <-- AllowInternetOutbound는 격리로 친다
  key_auth_refused : False
VERDICT: CLEAR
```

`_network_blocker`의 세 번째 통과 조건(private endpoint **그리고** 워크스페이스
격리)에 걸려 None을 반환한다. `_credential_blocker`는 키 기반 데이터스토어가
0개라 발동하지 않는다 — §58에서 전부 identity로 바꾼 것이 여기서 값을 한다.

### 61.3 프리플라이트의 의견이 아니라 관측으로 뒷받침된다

프리플라이트가 "열렸다"고 말하는 것만으로는 §60.3의 교훈을 반복하는 셈이다.
독립적인 관측이 하나 더 있다: **지금 이 순간 학습 잡이 같은 계정에 같은 private
endpoint를 통해 FUSE 세션을 열고 있다.** 두 런 모두 `setup.examples` 메트릭을
찍었는데, `rw_mount` 모드에서는 라이프사이클러가 마운트를 **사용자 커맨드보다
먼저** 연다. 커맨드가 시작했다는 것은 마운트가 이미 성공했다는 뜻이다.

### 61.4 `mount_outputs`의 기본값은 두 곳에서 다르다

`aml_job.py:134`는 `mount_outputs: bool = False`이고 docstring도 "Still off by
default"라고 적혀 있다. 그런데 `scripts/submit_training.py:66`은

```python
mount_outputs=not args.no_outputs,
```

즉 스크립트를 통해 제출하면 **기본이 True**다. 살아 있는 잡을 읽어 확인:

```
model_dir: type=uri_folder mode=rw_mount path=None
report   : type=uri_folder mode=rw_mount path=None
```

둘 다 참인 진술이지만 dataclass만 읽으면 반대로 이해하게 된다. 실제로 어느
모드로 도는지는 **제출된 잡을 읽어야** 알 수 있다.

`path=None`이므로 출력은 기본 데이터스토어의 관례 경로로 간다.
`job_output_uri`가 만드는 것과 대조:

```
실제 default 출력: azureml://datastores/workspaceartifactstore/ExperimentRun/dcid.<run>
job_output_uri() : azureml://datastores/workspaceblobstore/paths/azureml/<run>/model_dir/
```

`default`(런 히스토리 아티팩트)와 선언된 출력은 **다른 데이터스토어**에 간다.
`workspaceblobstore`가 기본(`isDefault=True`)이고 선언된 출력의 목적지다.
다만 이것은 아직 관례에 대한 추론이고 측정이 아니다 — 그래서 등록 다음에
마운트 검증이 온다(§61.5).

### 61.5 등록은 증거가 아니다

`register_adapter`는 존재하지 않는 폴더를 상대로도 성공한다(모듈 docstring).
그래서 순서는 등록 → **마운트 검증** → 병합이다. 검증 잡은 자산을 RO로 마운트한
뒤 결과를 stdout이 아니라 **MLflow 메트릭**으로 돌려보낸다:

- `mount.file_count`, `mount.total_bytes`, `mount.largest_bytes`
- `mount.largest_name`, `mount.has_adapter_weights` (불리언 → 태그)

stdout으로 보내면 안 되는 이유는 §58.7이다. 이 랩톱에서 잡 로그는 읽을 수 없다
(SAS 403, 아티팩트 프록시 제거됨). **읽을 수 없는 검증은 검증이 아니다.**
`publish`는 이미지에 구워진 패키지에서 임포트하므로 잡 커맨드는 인라인
`python -c`여야 한다 — 오늘 리포에 추가한 모듈은 어제 빌드된 태그에 없다.

### 61.6 데드 쿼터가 순서를 강제한다 (§60.8 재확인)

`Microsoft.MachineLearningServices` `usages` (polandcentral):

| type | name | 값 |
|---|---|---|
| dedicated | `standardNCADSA100v4Family` | **48/48** |
| lowPriority | `standardNCADSA100v4Family` | 24/**-1** (무제한) |
| — | `TotalDedicatedCores` | 48/148 |
| — | `TotalLowPriorityCores` | 24/300 |

48의 내역: `blue` 24 + `gpu-a100-ded` 노드 1개 24. `green`은 24가 더 필요하다.

`gpu-a100-ded`의 스케일 설정은 `min=0, max=1, idle=PT15M`. 따라서 DED 학습이
끝나고 **15분 뒤** 자동으로 0으로 내려가며 24코어가 풀린다. 더 빨리 풀어야 하면
`maxNodeCount=0`으로 리사이즈하는 방법이 있으나 클러스터를 지울 필요는 없다.

병합 잡을 LowPriority(`gpu-a100-lp`)에 두는 이유가 이것이다: 여기서 쓰는 데디
코어 하나하나가 `green`이 못 받는 코어다.

### 61.7 학습 경로에도 스토리지 가드를 달았다

`scripts/submit_training.py`는 §58 이후로도 도달 불가 스토리지를 그냥 제출했다.
배포 경로만 막혀 있었는데, **정작 낭비가 큰 쪽은 학습 경로다**: `rw_mount`에서는
노드 할당과 9GB 이미지 풀을 다 지불한 뒤 라이프사이클러에서 죽고, `upload`에서는
한 시간을 학습한 뒤 어댑터를 놓을 곳이 없다.

`endpoint.py:786`과 같은 모양으로 배선했다 — 읽고, 막히면 거부하고, `--force`면
경고만 하고 진행. `read_storage_reachability`는 모든 실패 경로에서 None을 돌려주므로
**읽지 못한 구독은 "막힘"이 아니라 "모름"으로 강등**된다.

`tests/test_submit_training_guard.py` 7개 추가 (전체 621 → **628 passed**, ruff clean).
그중 핵심은 가드가 *발화할 수 있음*을 증명하는 것이다. 항상 통과하는 프리플라이트는
프리플라이트가 없는 것과 구별되지 않고, 그 고장은 프로덕션에서 보이지 않는다 —
프로덕션이 바로 정상 통과하는 경우이기 때문이다. 합성 상태로 확인:

```
synthetic blocked state -> REFUSES
  workspace storage account '...8cb451dd1' is unreachable: publicNetworkAccess=Disabled
  there is no private endpoint and no public path, so nothing can reach it.
```

살아 있는 두 타깃(plc, kc)은 둘 다 CLEAR — 즉 이 가드는 지금 내 재제출을 막지 않는다.

### 61.8 `azureml-model-deployment` 헤더는 실제로 라우팅한다 (측정함)

`--traffic 0`으로 뜬 `green`은 엔드포인트 URL만으로는 절대 닿지 않는다. 한 배포를
직접 지목하는 것은 `azureml-model-deployment` 헤더이고, `ffsft-loadtest`에는 이
헤더 플래그가 **없다**. 그래서 순서가 이렇게 된다:

1. green 검증 = curl + 헤더 (트래픽 0인 채로)
2. 트래픽 전환
3. 로드테스트 = 엔드포인트 URL (헤더 불필요)

헤더가 진짜 라우팅하는지 지금 측정했다. 이걸 확인하지 않으면 검증 스크립트가
조용히 blue를 두 번 때리고 "green 정상"이라고 보고할 수 있다:

```
(no header)                -> HTTP 200
header=blue                -> HTTP 200
header=green               -> HTTP 404  Specified deployment could not be found
header=no-such-deployment  -> HTTP 404
```

`green`이 아직 없으므로 404 — 헤더가 무시된다면 200이 나왔을 것이다.

### 61.9 blue의 추론 트레이스 유출은 추측이 아니라 관측이다

green이 고치려는 대상이 무엇인지 숫자로 박아둔다. blue에 한국어 한 문장을 요청한
응답:

```
content len              : 380
reasoning_content len    : 0
trace leaked into content: True
content head: We need answer user's request: "한국어로 한 문장만: 서울은 ..."
```

모델의 영어 사고 과정이 `content` 안에 `<think>` 태그째로 들어 있고
`reasoning_content`는 비어 있다. blue의 `REASONING_PARSER`가 비었기 때문이다.

따라서 green 검증 기준은 "200 OK"가 아니다. **green도 트레이스를 `content`에
넣는다면, 배포는 떴지만 존재 이유를 달성하지 못한 것**이고 트래픽을 옮기는 것은
아무도 기록하지 않은 회귀가 된다.

### 61.10 MLflow는 **이름만** 오고 **값은 오지 않는다** — 검증을 종료코드로 옮겼다

§60.7에서 `lastvalues`가 `"value": [null]`을 주는 것을 "이름은 등록됐고 값은 아직"
이라고 읽었다. 28분 뒤에도 여전히 null이라 더 팠다.

**요청 본문 문제가 아니다.** 값을 달라고 이름을 실어 보내도 동일:

```
{}                                  -> value: [null]
{"metricNames":["setup.examples"]}  -> value: [null]
{"names":["setup.examples"]}        -> value: [null]
["setup.examples"]                  -> UserError: unable to deserialize
```

**독립적인 세 뷰가 일치한다:**

| 엔드포인트 | 결과 |
|---|---|
| `metric/v2.0/.../lastvalues` | 이름 1개, `value: [null]` |
| `mlflow/.../runs/get` | `metrics: null`, `tags: {}` |
| `mlflow/.../metrics/get-history` | `{ }` (빈 객체) |

**결정적 단서는 개수다.** `qlora.py:366`의 setup 리포트는 `split_metrics_and_tags`를
거치면 **메트릭 4개 + 태그 3개**가 되어야 한다:

```
metrics: setup.examples, setup.trainable_params_m, setup.trainable_pct, setup.vram_after_load_gb
tags   : setup.model, setup.hf_id, setup.mix
```

실제로 등록된 것은 **`setup.examples` 하나뿐**이고 값은 없다. 즉 `publish`가
**첫 `mlflow.log_metric` 호출에서 죽었다** — 이름은 서버에 등록됐고, 값 커밋이
실패했고, 루프의 나머지는 실행되지 않았다. `publish`는 설계상 절대 raise하지
않으므로(리포팅 때문에 학습을 죽이면 안 되니까) 경고 한 줄을 stdout에 찍고
False를 반환했다 — 그리고 stdout은 이 랩톱에서 읽을 수 없다(§58.7).

**따라서 §61.5에서 만든 마운트 검증은 그대로 두면 읽을 수 없다.** 오늘 이미 두 번
저지른 실수 — 절대 발화할 수 없는 감시자 — 를 세 번째로 저지를 뻔했다.

**수정: 단언을 종료코드에 넣는다.**

```python
sys.exit(0 if (n>0 and tot>0 and w) else 1)
```

`publish`는 남겨두되(우연히 되면 보너스) 판정은 **잡 상태**가 진다:

- `Completed` = 마운트에 파일·바이트·어댑터 가중치가 있었다
- `Failed` = 없었다. 등록이 빈 곳을 가리키고 있었다

잡 상태는 오늘 하루 종일 읽히는 것이 측정된 유일한 채널이다.

로컬에서 양쪽 결과를 실제로 실행해 확인:

```
empty mount        exit=1  COUNT 0 BYTES 0 ADAPTER False
adapter present    exit=0  COUNT 1 BYTES 1234 ADAPTER True
```

**게이트 1에 대한 함의:** 진행 신호로서 메트릭 *이름*은 여전히 유효하다
(`setup.*` 등장 = 스크립트가 setup 도달, `train.*` 등장 = 학습 반환). 그러나
합격 판정은 `Completed` 상태여야 하고, 손실·스텝 수 같은 숫자는 이 채널로는
못 읽는다.

---

## 62. 구독이 막힌 게 맞았다 — 단 `deny`가 아니라 `modify`였다 (2026-08-26)

### 62.1 왜 권한 점검이 전부 깨끗하게 나왔나

운영자가 여러 번 물었다: "기능이 있는데 구독이 막혀있다?", "보안적으로 막힌 건
없어?". 그때마다 **거부(deny) 정책을 찾았고, 하나도 없었다**. 역할 할당도 전부
정상이었다. 그래서 "정책으로는 막혀있지 않습니다"라고 답했다. 그 답은 **틀렸다**.

진짜 축은 관리 그룹 스코프의 정책 할당이다:

```
MCAPSGovDeployPolicies
  scope: /providers/Microsoft.Management/managementGroups/4510ec63-...-2dc7de6cecec
```

스토리지 계정에 걸리는 정책 셋:

| 정책 | effect | 결과 |
|---|---|---|
| `StorageAccount_DisableLocalAuth_Modify` | **modify** | `allowSharedKeyAccess=false` |
| `StorageAccount_PublicNetwork_Modify` | **modify** | `publicNetworkAccess=Disabled` |
| `StorageAccount_BlobAnonymousAccess_Modify` | **modify** | 익명 blob 접근 차단 |

`effect`가 `deny`가 아니라 `modify`다. 요청을 **거절하지 않고 속성을 조용히 고쳐
쓴다**. 그래서:

- 거부 정책 조회 → 0건 (실제로 0건이 맞다)
- 역할 할당 조회 → 전부 정상 (실제로 정상이 맞다)
- 리소스는 **만들어지고**, 다만 다른 속성값을 갖고 태어난다

증거가 전부 진짜였고, 전부 **다른 축을 재고 있었다**. `deny` 축을 아무리 다시
확인해도 `modify` 축은 보이지 않는다.

### 62.2 AAD는 열려 있고 계정 키만 닫혀 있다 (측정)

"스토리지가 막혔다"가 아니다. **키를 쓰는 경로만** 막혔다:

| 동작 | 인증 수단 | 결과 |
|---|---|---|
| 잡 데이터스토어 마운트 (`rw_mount`/`ro_mount`) | 컴퓨트 MSI (AAD) | ✅ 됨 |
| 데이터 자산 등록 | AAD | ✅ 됨 (`probe-qwen38-data:1`) |
| **모델 자산 등록** | 계정 키 blob 열거 | ❌ `KeyBasedAuthenticationNotPermitted` |
| 코드 스냅샷 업로드 (`create_or_get_start_pending_upload`) | SAS 발급(키 필요) | ❌ "Workspace MSI doesn't have appropriate permissions" |

마지막 줄이 오늘 하루를 태운 함정이다. 실패 원인은 **키 금지**인데 AML은 이걸
**MSI 권한 부족**으로 보고한다. 그 메시지를 믿고 역할을 계속 추가하는 것은
없는 문제를 고치는 일이다. 워크스페이스 MSI에는 이미 Azure AI Administrator +
Storage Blob Data Contributor + Storage File Data Privileged Contributor가 있다.

### 62.3 모델 레지스트리는 서버 사이드에서 키를 쓴다 — 클라이언트로 못 바꾼다

실패 스택:

```
Microsoft.MachineLearning.ModelRegistry.Services.Common.Services.Services
  .BlobContainerClient.EnumerateBlobPathsUnderPrefix
```

구형 `Microsoft.Azure.Storage` SDK다. **서비스 내부** 동작이라 클라이언트에서
자격증명을 바꿀 방법이 없다. `model_asset.py`의 docstring은 "The service does not
check either"라고 적혀 있었는데 **틀렸다**: 서비스는 실제로 blob을 열거하고,
열거 단계에서 죽는다.

등록 경로를 **전부 측정**했다. 추정이 아니다:

| 시도 | 결과 |
|---|---|
| 로컬 폴더 업로드 | ❌ 키 필요 |
| 데이터스토어 경로 URI | ❌ 동일 스택에서 실패 |
| `azureml://jobs/.../outputs/...` 참조 | ❌ 동일 |
| `runs:/` 참조 | ❌ 동일 |
| VNet **안**(컴퓨트)에서 등록 | ❌ 동일 — 네트워크 축이 아님을 확인 |
| 데이터 자산으로 등록 | ✅ 되지만 배포가 안 받음 (62.4) |

VNet 안에서도 똑같이 실패한다는 것이 중요하다. 이 워크스페이스에서 모델 자산은
**어떤 방법으로도 만들 수 없다**.

### 62.4 매니지드 배포는 등록된 자산 ID만 받는다

`deploy-online --model-uri`에 자산이 아닌 것을 넘기면 파싱 단계에서 거절한다:

```
데이터스토어 경로 : Could not parse azureml://... If providing an ARM id,
                    it should start with a '/'
데이터 자산(단축) : (InferencingClientCallFailed) The request is invalid.
데이터 자산(ARM)  : Failed to extract version when parsing asset /subscriptions/.../rg
```

**결론: 이 테넌트에서 `deploy-online --model-uri`는 구조적으로 사용 불가다.**
파인튜닝된 가중치가 매니지드 엔드포인트에 도달하는 길은 하나뿐 —
**컨테이너가 시작할 때 스스로 받아오는 것**.

### 62.5 그래서 컨테이너가 자기 신원으로 가중치를 받는다

`ffsft-serve:5`에 추가된 것:

- `docker/fetch_model.py` — `ManagedIdentityCredential`로 blob에서 체크포인트
  다운로드 (AAD만 사용, 키 없음)
- `serve_entrypoint.sh`에 fetch 단계 — `MODEL_BLOB_URI`가 있을 때만 동작
- `serving_env()`가 `MODEL_BLOB_URI`를 **비어 있어도 항상** 방출 (이미지 기본값
  상속 사고 방지 — 아키텍처 플래그와 같은 이유)
- CLI: `--model-blob-uri` + `--model-key`

**실패는 치명적이어야 한다.** `resolve_model()`은 로컬 체크포인트가 없으면
`MODEL_PATH`를 HF repo id로 취급한다. 베이스 모델 배포에는 옳은 동작이지만,
파인튜닝 fetch 실패를 이 폴백이 받으면 **엔드포인트는 건강하게 뜨고 튜닝 안 된
베이스 모델을 서빙한다**. 헬스 프로브도, 로드테스트도 전부 통과한다 — 틀린
가중치에 대해서. 그래서 fetch는 `resolve_model` **밖**, 최상위에서 돈다:

- `resolve_model`은 명령 치환 `$(...)`에서 호출되고, 거기서는 bash가 `set -e`를
  지키지 않는다 → 실패가 무시된다
- fetch는 진행률을 stdout에 쓴다 → `$(...)` 안이면 그게 모델 경로가 된다

`tests/test_serve_entrypoint.py`에 세 개 추가: fetch 미요청 시 미실행, 성공 시
받은 디렉터리 서빙, **실패 시 컨테이너 사망**.

### 62.6 선행 권한은 이미 다 있었다 (측정)

엔드포인트 MSI `c36d952c-4dbc-4b21-8c43-11e4a117c8c7`:

```
Storage Blob Data Reader          → mlwffsftstorage09dd66111   ✅ 이미 있음
AcrPull                           → acrffsftkc                 ✅ 이미 있음
AzureML Metrics Writer (preview)  → mlw-ffsft-plc              ✅ 이미 있음
```

새로 부여할 역할이 없다. 그리고 `/tmp` 용량은 **추정이 아니라 측정**이다:
현재 `blue`가 Qwen3.8-27B(54 GB)를 `HF_HOME=/tmp/hf`로 받아 서빙 중이다.

### 62.7 `systemDatastoresAuthMode`는 안정 API에 안 보인다

api-version `2024-10-01`은 이 필드를 **반환하지 않는다**. PATCH는 202를 주는데
GET으로는 빈 값이라 "적용 안 됐다"고 6번 오판했다. 프리뷰 버전
(`2024-10-01-preview` 등)으로 읽으면 `'identity'`로 바뀌어 있었다 — PATCH는
처음부터 성공했다.

같은 실수의 반복이다: **같은 축을 다시 확인해도 다른 축은 안 보인다.**

그리고 `identity`로 바꾼 뒤에도 코드 업로드는 동일하게 실패했다. 코드 업로드의
원인은 데이터스토어 자격증명 종류가 아니라 SAS 발급에 계정 키가 필요하다는
것이었다. 그래서 `accesskey`로 되돌렸다.

> **정정 (63장).** 여기서 "이 변경은 아무것도 얻지 못했다"고 적었던 것은
> **틀렸다.** 코드 업로드 축에서 아무것도 얻지 못한 것은 맞지만, 그 되돌림이
> **데이터스토어 마운트를 깨뜨렸다.** 되돌림의 효과를 코드 업로드 축에서만
> 재측정하고 마운트 축은 확인하지 않은 채 "무해하다"고 결론지었다. 63장 참조.

## 63. 마운트를 깨뜨린 건 정책이 아니라 나였다 (2026-08-26)

### 63.1 증상

학습 산출물을 읽는 머지 잡이 1분 만에 죽었다. 이유는 한 줄뿐이었다.

```
Failed to mount URI azureml://.../datastores/workspaceblobstore/paths/
azureml/sweet_malanga_5ql2n3cy7q/model_dir/ at mount point .../INPUT_adapter
```

리트라이 7회 전부 같은 문구. 밑에 깔린 원인은 표시되지 않는다.

이상한 점: **11분 전에 똑같은 경로를 똑같은 모드로 마운트한 잡이 성공했다**
(`loyal_beard_p3mfd8bztj`, exit 0). 입력 스펙을 ARM에서 뽑아 바이트 단위로
비교했더니 동일했다. 같은 클러스터, 같은 이미지, 같은 `RO_MOUNT`.

### 63.2 그래서 추측 대신 A/B를 돌렸다

두 잡의 유일한 구조적 차이는 머지 쪽에만 `merged` 출력이
`ReadWriteMount`로 붙어 있다는 것이었다. 그게 원인인지 아닌지를 **한 변수만
다른 두 잡**으로 물었다.

| | 입력 마운트 | 추가 출력 | 결과 |
|---|---|---|---|
| A | `RO_MOUNT` 동일 | 없음 | **Failed** |
| B | `RO_MOUNT` 동일 | `rw_mount` | **Failed** |

제출 **전에** 적어둔 판정표대로 읽으면: `A Failed` → 출력 모드는 무관하고,
마운트가 워크스페이스 전체에서 깨져 있다.

### 63.3 원인: `systemDatastoresAuthMode`를 되돌린 것

`accesskey` 모드는 데이터스토어 4개의 `credentialsType`을 전부 `AccountKey`로
**다시 쓴다**. 그런데 이 스토리지 계정은 `MCAPSGovDeployPolicies`의 `modify`
정책으로 공유 키가 꺼져 있다(62.1). 즉 마운트 드라이버는 **계정이 거부하는 키를
받아 든다.**

트랜스크립트 줄 번호로 시간순이 그대로 나온다.

| 시점 | 사건 | 결과 |
|---|---|---|
| — | `identity` (데이터스토어 = `None`) | verify `loyal_beard` **Completed** |
| 되돌림 | **→ `accesskey`** (데이터스토어 = `AccountKey`) | — |
| 이후 | 머지 `epic_nose` | **Failed** |
| 이후 | A/B 두 잡 | **Failed / Failed** |
| 되돌림 | **→ `identity`** (데이터스토어 = `None`) | — |
| 이후 | 재마운트 `quirky_glove`, **A와 바이트 동일** | **Completed (1분)** |

같은 잡 정의가 한 축을 껐다 켜는 것만으로 실패 → 성공으로 뒤집힌다.
상관이 아니라 인과다.

### 63.4 왜 못 봤나

되돌릴 때 나는 그 효과를 **코드 업로드 축에서만** 재측정했다. 코드 업로드는
`identity`에서도 똑같이 실패했으므로 "이 변경은 무해하다"고 적고 되돌렸다.
마운트 축은 확인하지 않았다.

**한 축에서 무해한 변경이 다른 축에서 무해하다는 근거는 어디에도 없다.**
62.7에 "같은 축을 다시 확인해도 다른 축은 안 보인다"고 직접 써놓고, 바로 그
문장 아래에서 같은 실수를 저질렀다.

### 63.5 그래서 `identity`가 유일하게 옳은 설정이다

두 모드를 축별로 측정하면 이렇다.

| | `identity` | `accesskey` |
|---|---|---|
| 데이터스토어 마운트 | **동작** | 실패 (계정이 키 거부) |
| 코드 스냅샷 업로드 | 실패 | 실패 (SAS 발급에 키 필요) |

코드 업로드는 **양쪽 다 안 된다.** 이 리포는 애초에 코드를 이미지에 구워서
`code=` 없이 제출하므로(머지·학습 모두) 잃는 게 없다. 반면 마운트는 `identity`
에서만 동작한다. 따라서 `identity`가 엄격히 우월하다.

읽고 쓰는 법은 62.7대로 **프리뷰 api-version**이어야 한다. 안정 버전은 이
필드를 반환조차 하지 않으므로, 안정 버전으로 확인하면 영원히 `None`이다.

## 64. blob 가중치와 네트워크 — 내가 만든 손잡이는 애저가 거부했다 (2026-08-26)

> **정정.** 이 장은 원래 "`egressPublicNetworkAccess=disabled`로 두면 된다"고
>썼다. **틀렸다.** 매니지드 VNet이 걸린 워크스페이스에서 애저는 이 값을 아예
> 받지 않는다 — 제출 즉시 400이다(64.2). 64.1의 **측정은 그대로 유효**하지만,
> 거기서 끌어낸 결론이 틀렸다. 왜 틀렸는지는 64.3에 적었다.

### 64.1 배포 전에 잡은 블로커

62장의 경로(컨테이너가 자기 신원으로 blob에서 체크포인트를 받는다)를 그대로
띄웠다면 A100 노드를 잡고 20 GB 이미지를 받은 **다음에** 죽었을 것이다. 실패
메시지는 네트워크 얘기를 한 글자도 하지 않는다.

먼저 스토리지 계정의 자세를 읽었다.

| 속성 | 값 |
|---|---|
| `publicNetworkAccess` | **Disabled** |
| `networkAcls.bypass` | `None` (신뢰 서비스 우회 **없음**) |
| `ipRules` | `[]` |
| `privateEndpointConnections` | 2 |

그리고 **데이터 플레인**을 직접 때려봤다. VNet 밖(이 워크스테이션)에서 유효한
AAD 토큰으로:

```
REFUSED: HttpResponseError
This request is not authorized to perform this operation.
```

컨트롤 플레인(ARM)은 되는데 데이터 플레인은 안 된다. **두 축이 다르다** —
데이터 자산 등록이 됐다고 blob 읽기가 된다는 뜻이 아니었다.

### 64.2 애저는 이 손잡이를 거부한다 (측정)

`egress_public_network_access="disabled"`로 배포를 던졌다. 노드를 잡기도 전에,
**15초 만에** 400이 돌아왔다.

```
(InferencingClientCallFailed) Validation:
 "Deployments with enabled private networking require a premium tier ACR
  which supports private networking capabilities."
 "The EgressPublicNetworkAccess under online deployment is no longer
  supported when your workspace is secured with managed virtual network.
  Please avoid setting EgressPublicNetworkAccess on the deployment in this case."
```

두 절이 각각 다른 얘기를 한다.

| 절 | 뜻 | 이 환경의 사실 |
|---|---|---|
| Premium ACR | 프라이빗 네트워킹은 Premium 레지스트리가 필요 | `acrffsftkc`는 **Basic** |
| EgressPublicNetworkAccess | 매니지드 VNet 워크스페이스에선 **지원 안 함** | `isolationMode: AllowInternetOutbound`, `status: Active` |

두 번째 절이 결정적이다. **"워크스페이스가 매니지드 VNet으로 보호되면 이 설정을
배포에 걸지 말라"** — 즉 그 경우 배포의 아웃바운드는 워크스페이스 매니지드 VNet이
**이미** 관장한다. 배포별로 고를 것이 남아 있지 않다.

### 64.3 왜 틀렸나 — `Enabled`는 설정값이 아니라 읽기 기본값이었다

원래 추론은 이랬다: blue를 읽으니 `egressPublicNetworkAccess: Enabled`다 → blue는
공용 인터넷으로 나간다 → 그러면 blob이 공용 IP로 풀려 거부당한다 → `disabled`로
바꿔야 한다.

첫 단계가 틀렸다. **blue는 이 값을 설정한 적이 없다.** 당시 코드는 허브 소스
배포에 `None`을 넘겼고, 그래서 요청 본문에 이 필드가 실리지 않았다. ARM이 읽기
응답에 채워 넣는 **기본값**을 나는 "blue가 고른 값"으로 읽었다.

| | blue가 보낸 값 | ARM이 읽어주는 값 | 검증기에 도달 |
|---|---|---|---|
| blue (동작 중) | 없음 | `Enabled` | 아니오 |
| green (거부됨) | `disabled` | — | **예 → 400** |

blue가 `Enabled`로 읽히면서 `Succeeded`라는 사실이 함정이었다. 동작하는 배포에
붙어 있는 값처럼 보이니 "따라 써도 되는 값"으로 보인다. 실제로는 아무도 보낸 적
없는 값이고, **명시적으로 보낸 값만 검증기까지 간다.**

이건 추론이 아니라 그 자리에서 확인됐다. 고친 코드로 다시 띄운 green은 이 필드를
**보내지 않았는데**, 생성 직후 ARM이 읽어주는 값은 그대로 `Enabled`였다.

```
provisioningState  : Creating
egressPublicNetwork: Enabled   <- 우리가 보낸 값은 없다
```

즉 `Enabled`는 "이 배포가 공용으로 나간다"는 기록이 아니라 **필드가 비어 있을 때
ARM이 채워 넣는 값**이다. blue에서 이걸 읽고 세운 인과 전체가 여기서 무너진다.

63장과 같은 실수다. 거기서는 되돌리기를 *코드 업로드* 축에서만 재보고 *마운트*
축에 일반화했다. 여기서는 **읽기 응답**을 *설정 기록*으로 읽었다. 둘 다 "본 것"과
"그것이 뜻하는 것" 사이를 건너뛴 것이다.

### 64.4 그래서 아무것도 보내지 않는다

`egress_for(explicit, reachability)` — 판단 근거를 가중치 위치가 아니라
**워크스페이스의 격리 모드**로 바꿨다. 그게 실제로 결정하는 축이다.

- 워크스페이스가 매니지드 VNet이면 → `None` (**명시값이 있어도 버린다**, 보내봐야
  400이다. 버릴 때는 로그로 알린다)
- 아니면 → 명시값 그대로
- `reachability`를 못 읽었으면 → `None`

격리 모드는 `deploy_online`이 **이미 돌리는 프리플라이트**(`reachability`)에서
그대로 꺼낸다. API 호출이 늘지 않고, `ISOLATED_MODES` 판정을 두 벌로 만들지도
않는다.

`tests/test_deploy_egress.py` 6개로 고정했다. 모듈 독스트링에 400 원문을 그대로
박아뒀고, **`Enabled`가 읽기 기본값이라는 함정**을 테스트 이름으로 남겼다 —
다음 사람이 blue를 읽고 같은 결론을 내리지 않도록.

### 64.5 닿는다 — 측정 완료 (2026-08-26)

애저 말대로라면 매니지드 VNet이 관장하니 컨테이너는 이미 그 망 안에 있고,
학습 클러스터가 이 계정을 마운트하는 바로 그 경로를 쓴다. 이건 **문서에서
끌어낸 기대였고**, 64.1의 거부는 *VNet 밖에서* 잰 것이라 답이 되지 못했다.

green이 뜨면서 답이 나왔다. 추론 컨테이너 로그 원문:

```
[serve] fetching model from blob: https://mlwffsftstorage09dd66111.blob.core.windows.net/.../merged/
[fetch] 21 blobs, 50.1 GiB
[fetch]   4.8%     33.0 MiB/s  .../model-00014-of-00014.safetensors
...
[fetch] 100.0%    460.4 MiB/s  .../model-00002-of-00014.safetensors
[fetch] complete: 50.1 GiB in 111s
[fetch] OK /tmp/ffsft-model
```

**닿는다.** 엔드포인트 시스템 할당 MSI(`c36d952c-…`, `Storage Blob Data Reader`)로
프라이빗 엔드포인트 전용 계정에서 50.1 GiB를 111초에 받았다. 아래에서 계산한
13.25분 예산의 **7분의 1**이다 — 아슬아슬하게 통과한 게 아니라 여유가 크다.

기대가 아니라 측정이다.

측정 지점은 green의 **`inference-server` 컨테이너 로그**다. 처음엔
`storage-initializer`라고 적었는데 틀렸다 — 이 배포는 `model` 자산이 `None`이라
(가중치를 이미지가 아니라 컨테이너가 직접 받는 구조) AML의 storage-initializer가
아예 뜨지 않는다. `fetch_model.py`는 추론 컨테이너 안에서 돈다.

그래서 다운로드가 **레디니스 예산 안에서** 벌어진다. ARM에서 읽은 값:

| | green | blue (성공, 지금 서빙 중) |
| --- | --- | --- |
| `model` 자산 | `None` | `None` |
| `initialDelay` | `PT2M` | `PT2M` |
| `failureThreshold` × `period` | 45 × `PT15S` | 45 × `PT15S` |
| **총 예산** | **13.25분** | 13.25분 |
| 가중치 출처 | `MODEL_BLOB_URI` (blob, 프라이빗 엔드포인트) | `MODEL_PATH=Qwen/Qwen3.8-27B` (HF, 인터넷) |

blue가 같은 예산으로 성공했다는 건 **"27B를 받아서 vLLM에 올리는 일" 자체는
13.25분 안에 끝난다**는 뜻이다. 다만 blue는 인터넷으로 HF에서 받았다. 남은 변수는
정확히 하나 — 출처가 blob 프라이빗 엔드포인트로 바뀌었을 때도 닿느냐. 같은 리전
같은 계정이라 더 빠를 것으로 기대하지만, 그건 여전히 기대다.

실패한다면 컨테이너 시작 후 13.25분쯤에 레디니스 타임아웃으로 갈리고, 로그가
`fetch_model.py`가 blob에 닿았는지 아닌지를 말해준다. 그 전까지는 노드 할당과
커스텀 이미지 풀이라 예산 시계가 아직 돌지도 않는다.

---

## 65. 배포가 엔드포인트를 조용히 죽였다 — `traffic: {}` (2026-08-26)

green을 띄워두고 3번 게이트(로드테스트)를 준비하다가, 엔드포인트를 읽었더니
트래픽 맵이 비어 있었다.

```
$ az rest --method get --url ".../onlineEndpoints/ffsft-plc?api-version=2024-04-01" \
    --query "{state:properties.provisioningState, traffic:properties.traffic}"
{
  "state": "Succeeded",
  "traffic": {}
}
```

이 엔드포인트는 **같은 URI로 100요청짜리 로드테스트를 이미 통과했다**
(§59 blue 베이스라인: knee 16, peak 204.29 tok/s, 20/20 성공). 그 사이에 일어난
쓰기는 배포뿐이다.

### 65.1 왜 아무도 알려주지 않았나

애저가 보고하는 모든 상태가 정상이다.

| 읽은 것 | 값 |
| --- | --- |
| 엔드포인트 `provisioningState` | `Succeeded` |
| blue 배포 `provisioningState` | `Succeeded` |
| 배포 인스턴스 | 살아 있음, 과금 중 |
| 스코어링 URI | **죽음** |

죽은 건 라우팅뿐이라 배포 상태로는 절대 안 잡힌다. 요청을 실제로 쏴봐야 안다.

### 65.2 범인 — "엔드포인트 보장" 단계 (측정)

`deploy_online`은 배포 전에 엔드포인트를 무조건 `begin_create_or_update` 했다.
이건 **덮어쓰는 PUT**이고, 넘긴 엔티티는 애저에서 읽어온 게 아니라 그 자리에서
새로 만든 것이다. 로컬에서 직렬화 결과를 그대로 찍었다.

```
entity.traffic: {}          <- None이 아니라 빈 맵

=== 읽지 않고 만든 엔티티가 PUT하는 것 ===
  properties.traffic      : {}
  properties.auth_mode    : <EndpointAuthMode.KEY: 'Key'>

=== 트래픽을 먼저 읽어서 채운 엔티티 ===
  properties.traffic      : {'blue': 100}
```

`{}`는 **생략된 필드가 아니다.** ARM이 병합해줄 여지가 없는, "모든 배포에 0%를
보내라"는 명시적 지시다. 배포를 한 번 더 돌릴 때마다 살아 있던 엔드포인트가
말없이 내려간다.

### 65.3 덤으로 드러난 거짓말 — `--traffic 0`

`deploy_online`의 `--traffic 0` 분기는 이렇게 약속한다: *"트래픽 맵을 건드리지
않는다. 그래서 잘못된 롤아웃이 엔드포인트를 내리지 못한다."* 그리고 현재
트래픽을 로그로 찍는다.

실제로는 **100줄 위에서 이미 내려놨고**, 그 다음에 자기가 방금 만든 `{}`를
"찾아낸 값"인 양 찍는다. 안심시키는 문장의 형태로 피해를 보고하고 있었다.

### 65.4 고침

`ensure_endpoint`를 모듈 레벨 함수로 뽑았다. 규칙은 한 줄이다 — **없으면 만들고,
있으면 손대지 않는다.** `get`이 `ResourceNotFoundError`를 던질 때만 PUT한다.

`auth_mode`가 `key`가 아니면 경고만 하고 그대로 둔다. 고쳐주는 게 친절해 보이지만
기존 클라이언트 전부의 인증 방식을 갈아치우는 짓이라, 조용히 할 일이 아니다.

`tests/test_endpoint_traffic_preserved.py` 6개로 고정했다. 그중 하나는
**SDK 동작 자체를 못 박는다** — 새로 만든 `ManagedOnlineEndpoint`의
`_to_rest_online_endpoint().properties.traffic`이 `{}`인지. 나중에 SDK가 이 필드를
생략하도록 바뀌면 그 테스트가 깨지고, 다음 사람은 "이 가드가 아직 필요한가"를
물려받는 대신 다시 재본다.

### 65.5 트래픽 복구 — 그리고 같은 함정을 한 번 더 밟았다

green 검증 뒤 트래픽을 세우려고 `/tmp/plc_shift.sh`를 돌렸더니 이렇게 찍혔다.

```
traffic before: {}
traffic after : {}
```

에러 없이, 성공한 것처럼. 스크립트는 ARM `PATCH`를 쓰면서 `-o none 2>/dev/null`로
응답을 버리고 있었다. 그걸 벗기니 400이 나왔다.

```
Could not find member 'properties' on object of type
'PartialMinimalTrackedResourceWithIdentity'
```

`onlineEndpoints`의 `PATCH`는 **태그와 identity만** 받는다. `properties`는 애초에
멤버가 아니라서 트래픽은 PATCH로 못 바꾼다. 조용히 삼킨 400이 "before와 after가
같네" 하는 무해한 출력으로 둔갑해 있었다 — **65절이 다루는 것과 같은 종류의
버그를, 그 65절을 쓰는 도중에 한 번 더 밟은 것이다.**

고친 방식은 `endpoint.py:1081`이 이미 하던 그대로다. **엔드포인트를 읽어와서**
`.traffic`만 갈아끼우고 PUT한다. 새로 만든 엔티티를 쓰면 65.2의 `{}` 사고가 그대로
재현되므로, 읽어오는 것이 핵심이다. 실패하면 종료 코드로 죽는다.

```
traffic before: {'blue': 0, 'green': 0}
traffic after : {'green': 100, 'blue': 0}
OK: the endpoint URL now routes to green
```

`before`가 `{}`가 아니라 `{'blue': 0, 'green': 0}`인 데 주목할 것. green 배포가
자기 이름을 0%로 끼워 넣은 것뿐이고, 합이 0이라 **엔드포인트는 그때까지도 아무것도
서빙하지 않고 있었다.** 배포 상태만 보면 둘 다 `Succeeded`였다.

배포와 트래픽 이동은 일부러 다른 스크립트로 갈라놨다 — 이번 일이 정확히 그 둘이 한
단계에 묶여 있어서 생겼다.

---

## 66. 3개 게이트 전부 통과 — 학습 → 배포 → 로드테스트 (2026-08-26)

세 게이트를 순서대로, 각각 독립된 측정으로 통과했다.

| 게이트 | 결과 | 무엇으로 증명했나 |
| --- | --- | --- |
| 1. 학습 | 통과 | 머지 산출물을 **별도 잡**이 blob에서 마운트해 종료 코드로 검증 (39초, exit 0) |
| 2. 배포 | 통과 | green `Succeeded` (24분) + **추론 트레이스 누출 축**으로 검증 |
| 3. 로드테스트 | 통과 | 매니지드 엔드포인트 URL에 100요청, **실패 0** |

### 66.1 배포 검증은 200 OK가 아니었다

같은 프롬프트를 `azureml-model-deployment` 헤더로 각 배포에 직접 쏴서 비교했다.

| | `content` 길이 | 트레이스 누출 | 내용 |
| --- | --- | --- | --- |
| blue (베이스) | 380 | **누출** | `We need answer user's request: "한국어로 한 문장만…"` (영어 사고과정) |
| green (파인튜닝) | 44 | 없음 | `서울은 한국의 수도로, 현대적인 도시와 전통적인 문화가 공존하는 도시입니다.` |

200 OK만 봤으면 **같은 결함을 그대로 실은 배포가 게이트를 통과했을 것이다.**

### 66.2 로드테스트 — tok/s만 보면 오독한다

```
            blue (베이스)                   green (파인튜닝)
conc   tok/req  TPOT    tok/s  req/s |  tok/req  TPOT    tok/s  req/s
   1    121.8  0.0364    22.0  0.18  |   110.6  0.0363    21.3  0.19
   2    121.8  0.0364    43.4  0.36  |   110.6  0.0363    41.1  0.37
   4    121.8  0.0371    83.0  0.68  |   110.7  0.0370    75.0  0.68
   8    121.8  0.0382   131.6  1.08  |   110.6  0.0383   119.8  1.08
  16    121.8  0.0407   204.3  1.68  |   110.6  0.0407   189.0  1.71

blue  knee=16  peak=204.29 tok/s  실패 0/100
green knee=16  peak=189.01 tok/s  실패 0/100
```

peak tok/s가 204.3 → 189.0으로 **7.5% 낮다.** 이걸 성능 저하로 적으면 틀린다.

- **TPOT는 소수점 넷째 자리까지 같다** (0.0363 / 0.0364 … 0.0407 / 0.0407).
  토큰 하나 뽑는 비용은 변하지 않았다.
- **req/s는 green이 오히려 높다** (c=16에서 1.71 vs 1.68).
- 차이는 전부 **응답 길이**다. blue 121.8 tok/req vs green 110.6 — 9.2% 짧다.

blue는 영어 사고과정을 `content`에 쏟아내서 토큰 수가 부풀어 있었다. green은
`REASONING_PARSER=qwen3`로 그걸 `reasoning_content`로 빼낸다. **tok/s는 같은 일을
할 때만 비교 가능한 지표인데, 두 배포는 같은 일을 하고 있지 않다.** 서빙 속도로
읽어야 할 값은 TPOT와 req/s이고, 그 둘로 보면 green은 동등하거나 근소하게 낫다.

### 66.3 최종 형상

- **매니지드 온라인 엔드포인트** — 잡도, 배치도, EC2 대체물도 아니다.
- 가중치는 이미지에 굽지 않고 컨테이너가 blob에서 직접 받는다 (MSI, 111초, §64.5).
- SSE 스트리밍 동작 확인 (`data: {...}` 프레임 수신).
- 트래픽 100% green, 실패 0/100.

---

## 67. thinking 을 켜면 스트림이 침묵한다 (2026-08-26)

> **[68 절에서 정정됨]** 이 절의 핵심 주장 — *"thinking ON 이면 청크가 0 개"*,
> *"TTFT 12.94 초"* — 은 **틀렸다.** 이 이미지는 사고 델타를 `reasoning_content`
> 가 아니라 **`reasoning`** 으로 보내는데 내가 옛 이름만 세고 있었다. 실제로는
> 4920 델타가 도착했고 첫 사고 토큰까지 1.04 초다. 아래 표의 "0" 은 서버의
> 침묵이 아니라 내 계량기의 눈금이다. 무엇이 참인지는 68 절에 있다.

로컬 뷰어(`scripts/token_viewer.py`)를 실제 배포(green)에 물리다가 두 가지가
드러났다. 둘 다 mock 에서는 안 보이는 것들이다.

### 67.1 페이지가 보낸 모델명이 mock 것이었다

```
{"error": "upstream 424: {\"error\":{\"message\":\"The model `local` does not exist.\",
 \"type\":\"NotFoundError\",\"param\":\"model\",\"code\":404}}"}
```

페이지에 `model: 'local'` 이 박혀 있었다. 번들 mock 이 서빙하는 이름이고, 실제
배포는 `SERVED_MODEL_NAME=ffsft` 다. **한 단어 불일치인데 424 프록시 에러처럼
보인다.** 이제 서버가 기동 시 업스트림 `/v1/models` 에 물어서 정하고
(`--model` 로 덮어쓰기 가능), `/upstream` 으로 페이지에 알려주고, 화면 상단에
그 이름을 띄운다. 어느 쪽에 물리든 하드코딩된 이름이 없다.

### 67.2 thinking ON 은 "주황색"이 아니라 "무(無)"다 — 측정

페이지 안내문은 *"thinking 을 ON 으로 두고 max_tokens 를 12 이하로 하면 응답이
전부 주황색(=`reasoning_content`)이 되고 old meter 가 0 을 가리킨다"* 고 적혀
있었다. **실제 배포에서는 틀린 설명이었다.**

`max_tokens=12`, 같은 프롬프트로 세 모드를 실측:

| thinking | content 청크 | reasoning_content 청크 | 화면 |
| --- | --- | --- | --- |
| `true` | **0** | **0** | 아무것도 안 옴 |
| 안 보냄 (서버 기본) | **0** | **0** | 아무것도 안 옴 |
| `false` | 12 | 0 | `세종대왕은 조선의 네 번째 왕으로,` |

vLLM 0.27.1 의 `reasoning_parser=qwen3` 는 `<think>` 블록을 **스트림에 흘리지
않고 `</think>` 까지 버퍼링한다.** 그래서 `reasoning_content` 델타가 아예 발생하지
않는다. `max_tokens=12` 는 `</think>` 에 닿기 전에 소진되므로 스트림이 통째로 빈다.

mock 은 `reasoning_content` 를 그대로 흘리기 때문에 "전부 주황색"이 맞다.
**안내문은 mock 의 동작을 설명하면서 실제 배포를 설명하는 척하고 있었다** — 두
업스트림을 구분해 다시 적었다.

### 67.3 이건 55절보다 나쁜 증상이다

55 절은 *"토큰이 눈앞에 도착하는데 계량기만 0"* 이었다. 여기서는 **도착하는 것
자체가 없다.** 모델이 느린 건지, 멈춘 건지, 끝난 건지 화면으로 구분할 수 없다.

`max_tokens` 를 크게 줘도 첫 글자까지 침묵한다:

| | 첫 토큰 | 총 시간 | 생성 속도 |
| --- | --- | --- | --- |
| thinking OFF | **1.13 초** | 15.5 초 (394 토큰) | 27.5 tok/s |
| thinking ON | **12.94 초** | 19.6 초 (185 토큰) | 27.7 tok/s |

**11 배 차이다.** 생성 속도(27.5 vs 27.7 tok/s)는 같으니 모델이 느려진 게 아니라,
사고 토큰이 스트림에 안 실려서 사용자가 13 초를 빈 화면으로 기다리는 것이다.
챗봇 UX 로 쓸 거면 이건 취향이 아니라 설계 결정이다.

## 68. 사고 토큰은 사라진 적이 없다 — 필드 이름이 `reasoning` 이었다 (2026-08-26)

67 절의 결론은 *"thinking 을 켜면 스트림이 침묵한다"* 였다. **틀렸다.**
스트림은 한 번도 침묵한 적이 없다. 내가 잘못된 키를 읽고 있었다.

이 절은 그 오독이 어디까지 번졌는지, 그리고 무엇이 실제로 참인지를 적는다.

### 68.1 실제 와이어

`ffsft-plc/green` 의 SSE 를 파싱하지 말고 **그대로 받아서 델타 키를 세어 봤다**
(thinking ON, `max_tokens=6000`):

```
SSE 프레임 수: 4921
delta 키별 등장 횟수: {'role': 1, 'reasoning': 4920}
```

`reasoning_content` 는 **단 한 프레임도 없다.** 이 이미지는 사고 블록을
`delta.reasoning` 으로 흘린다. 비스트리밍도 같다 — `message` 의 키는
`['annotations', 'audio', 'content', 'function_call', 'reasoning', 'refusal', 'role']`
이고, 사고는 `message.reasoning` 에 들어 있다.

### 68.2 그래서 무엇이 거짓이었나

| 앞서 기록한 것 | 실제 |
| --- | --- |
| 67.2 "thinking ON 이면 청크가 0 개" | 4920 델타가 도착했다. 세는 키가 틀렸다 |
| 67.3 "thinking ON 은 TTFT 12.94 초" | **첫 사고 토큰까지 1.04 초.** 12.94 초는 `content` 첫 글자까지였다 |
| "700 토큰이 생성·청구되고 전량 폐기" | 폐기 아님. `message.reasoning` 에 **1737 자**가 담겨 왔다 |
| "이 모델은 사고를 안 한다 (빈 `<think>`)" | 손으로 만든 프레임에 시스템 프롬프트와 `<think>` 프리필이 빠졌던 것 (68.4) |

thinking ON 을 정상 예산으로 실측하면 이렇다 (`max_tokens=200`, 짧은 질문):

```
사고 델타 37 (첫 1.04s) 82자 | 답 델타 26 (첫 2.42s) 57자
finish=stop  usage={'prompt_tokens': 62, 'completion_tokens': 65}
```

사고도 답도 한국어로 정상이고 `finish=stop` 으로 스스로 끝냈다.
**thinking 은 고장난 적이 없다.**

### 68.3 왜 테스트는 전부 통과하고 있었나

`scripts/mock_vllm_server.py` 가 `reasoning_content` 를 내보내고 있었다.
`src/ffsft/serve/loadtest.py` 는 `content` 와 `reasoning_content` 만 셌다.
**모의 서버와 클라이언트가 같은 오타를 공유하니 스위트는 초록이었고, 실제
배포에서는 0 을 세고 있었다.** 67.2 에서 *"안내문이 mock 의 동작을 설명하면서
실제 배포를 설명하는 척한다"* 고 적었는데, 같은 함정에 코드가 먼저 빠져 있었다.

고친 것:

- `loadtest.py` 의 계량기가 `reasoning` 도 센다.
- 모의 서버 기본 필드명을 `reasoning` 으로 바꿨다 (`MOCK_THINK_FIELD` 로 옛 이름도
  재현 가능 — 두 와이어를 다 시험할 수 있어야 한다).
- 회귀 테스트 2 개: 실제 서버가 보내는 철자를 세는지, 한 스트림에 두 철자가
  섞여도 중복 집계하지 않는지.

전체 **652 테스트 통과**, `ruff` 클린.

### 68.4 내가 중간에 만든 가짜 증거

파서를 우회하려고 `/v1/completions` 에 ChatML 프레임을 **손으로** 만들어 넣었다:

```
<|im_start|>user\n{질문}<|im_end|>\n<|im_start|>assistant\n
```

green 도 blue 도 `<think>\n\n</think>` — 빈 사고 블록을 내고 바로 답했다. 여기서
*"파인튜닝된 모델이 사고를 안 한다"* 고 읽었다. 추측을 멈추고 서버에 렌더된
프롬프트를 물었다 (`POST /tokenize`, `return_token_strs: true`, 질문 = `"안녕"`):

| thinking | count | `<\|im_start\|>assistant` 뒤 | 시스템 프롬프트 |
| --- | --- | --- | --- |
| `false` | **14** | `\n<think>\n\n</think>\n\n` | 없음 |
| `true` | **54** | `\n<think>\n` | **있음** |

ON 일 때 템플릿이 주입하는 40 토큰짜리 시스템 프롬프트:

> Reasoning effort is set to xhigh. Please think carefully through the task,
> validate key assumptions, consider plausible alternatives, and prioritize
> correctness, consistency, and clarity in the final answer.

내 프레임에는 이 40 토큰도, 열린 `<think>` 도 없었다. **모델은 시키지 않은 사고를
하지 않았을 뿐이다.** 손으로 만든 프레임으로 모델의 성향을 판정하면 안 된다 —
`/tokenize` 가 정답을 그냥 알려준다.

### 68.5 파인튜닝은 무죄 — 순정 베이스로 대조

"파인튜닝하면서 씽킹이 고장난 것 같다" 는 합리적인 의심이었고, 대조군이 이미 떠
있었다:

| | MODEL_PATH | REASONING_PARSER |
| --- | --- | --- |
| blue | `Qwen/Qwen3.8-27B` (순정, HF 직접) | *(빈 값)* |
| green | blob 의 merged 파인튜닝 가중치 | `qwen3` |

68.4 가 뱉은 프레임을 바이트 그대로 재조립해 `/completions` 로 양쪽에 던졌다
(`max_tokens=2048`, `temperature=0`):

| | 소요 | finish | completion_tokens | `</think>` 도달 |
| --- | --- | --- | --- | --- |
| blue (순정) | 75.8 초 | length | 2048 | False |
| green (파인튜닝) | 75.7 초 | length | 2048 | False |

둘 다 2048 토큰으로는 사고를 못 닫았고, 사고 첫 문장까지 거의 같다 — 양쪽 모두
`We need answer in Korean likely. User: "12명이 3대의 차에…"` 로 시작하는 **영어**
사고다. **순정 모델을 다시 배포해 확인할 필요가 없다** — 이미 blue 로 떠 있고,
방금 그걸로 대조했다.

### 68.6 진짜 제약은 사고 예산이다

2048 로는 못 닫았다는 게 고장의 증거가 아니다. **예산 문제였다.**
`max_tokens=7500` 으로 다시:

| 질문 | 소요 | finish | completion_tokens | 사고 / 답 |
| --- | --- | --- | --- | --- |
| 12명·3대 조합론 | 180.6 초 | **stop** | **4908** | 사고 12,238 자 / 답 593 자 |
| "서울은 어떤 도시야?" | 4.2 초 | **stop** | **85** | 사고 124 자 / 답 44 자 |

어려운 문제는 사고에만 4908 토큰을 쓴다. `max_tokens` 를 120·700 으로 잡으면
`finish=length` 로 잘려 답이 시작조차 못 한다 — 이건 파서 탓도 파인튜닝 탓도
아니고 **상한을 사고 길이보다 작게 잡은 것**이다. `MAX_MODEL_LEN=8192` 이므로
thinking ON 을 실제로 쓰려면 예산을 그 수준으로 잡아야 한다.

### 68.7 그래서 `enable_thinking: false` 는 여전히 옳다

`configs/models.yaml` 의

```yaml
  - key: qwen3.8-27b
    chat_template_kwargs:
      enable_thinking: false
```

는 버그 우회가 아니라 **비용 결정**으로서 유효하다. 프롬프트 40 토큰 + 사고
수천 토큰을 매 요청에 지불할 것인가의 문제다. 학습도 같은 프레임을 쓰고
(`src/ffsft/train/qlora.py::apply_chat_template`, *"This must be identical to what
inference does"*), 로드테스트도 스펙에서 받아 그대로 보내므로 세 지점이 일치한다.
게이트 3 의 189 tok/s 는 thinking OFF 로 측정한 값이라 그대로 유효하다.

다만 근거는 바로잡아야 한다 — *"켜면 스트림이 죽는다"* 가 아니라
*"켜면 요청당 수천 토큰을 더 쓴다"* 이다.

### 68.8 뷰어에 넣은 것

- **두 철자 모두 읽는다** — `d.reasoning ?? d.reasoning_content`. `||` 가 아니라
  `??` 인 이유는 빈 문자열 델타도 델타이기 때문이다.
- **route 선택** — `/chat/completions`(파서 통과) vs `/completions`(파서 우회,
  원문). 후자는 68.4 의 프레임을 **서버가 알려준 대로** 재조립한다. 손으로 만들면
  68.4 를 반복한다.
- **deployment 선택** — `X-Deployment` → `azureml-model-deployment`. 트래픽 분배를
  건드리지 않고 blue/green 에 같은 질문을 던져 정성 비교한다.
- **max_tokens 사용 카드** — 청크 수가 아니라 `usage.completion_tokens`
  (`stream_options.include_usage`). 청크는 토큰이 아니다. `finish_reason=length`
  면 빨갛게 칠한다 — 68.6 이 바로 그 카드가 잡아야 할 상태다.
