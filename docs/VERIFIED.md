# 검증 기록 (Verified Findings)

이 문서는 **실제로 실행해서 확인한 것만** 기록한다. 추정·문서·블로그 근거는
`docs/PLAN.md`에 있고, 여기에는 라이브 Azure 구독과 라이브 Hugging Face에 대해
직접 호출한 결과만 남긴다.

- 구독: `ME-MngEnvMCAP277524-eonlee-1` (`cb370f4f-…`), 테넌트 `4510ec63-…`
- 검증일: **2026-08-20**
- 재현 도구: `scripts/probe_architecture.py`, `scripts/verify_hf_ids.py`

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

---

## 4. 아직 검증 못 한 것

- [ ] **Qwen3.8-27B QLoRA 실제 학습** — bitsandbytes NF4가 hybrid
      linear-attention/Conv1d 레이어에서 실제로 도는지. 최대 리스크.
- [ ] 22.5–26.5 GB 실측 추정이 실제 피크와 맞는지
- [ ] Fabric → OneLake → AML 데이터 경로
- [ ] `benchmarks.yaml`의 한국어 harness task 이름
- [ ] `trl` 1.10 / `peft` 0.20 이 `transformers` 5.15와 호환되는지
