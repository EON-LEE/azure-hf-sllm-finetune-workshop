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

참고로 `Microsoft.Compute` 쪽 `Total Regional Low-priority vCPUs`는 확인한 6개
지역 전부 **0/100**이다. 즉 **일반 VM으로는 Spot GPU도 못 띄우고**, AML의 별도
저우선순위 풀로만 GPU에 접근할 수 있다. (A10 Spot VM은 한 번 떴었지만 GRID
드라이버 확장이 exit 14로 실패했고 — `ubuntu-hpc` 이미지가 apt로 이미
`nvidia-driver-580-open`을 깔아둬서 `.run` 설치기가 스스로 중단한다 — AML
경로를 찾은 뒤 삭제했다.)

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
| `tests/test_startup_grace.py` | 프로브 예산은 모델 크기에 비례 | 10 |
| `tests/test_model_size_inference.py` | 레지스트리에 없어도 repo id에서 크기 복원 | 18 |
| `tests/test_serve_entrypoint.py` | HF repo id로도 엔트리포인트가 죽지 않음 | 8 |
| `tests/test_preflight_storage.py` | 스토리지 도달 불가면 즉시 거부 | 12 |
| `tests/test_loadtest_e2e.py` | 로드테스트 측정 수식이 정답과 일치 | 5 |

전체 **237 테스트 통과**, `ruff` 클린.

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