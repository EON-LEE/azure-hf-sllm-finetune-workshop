# 서빙 패턴 (Serving patterns)

파인튜닝한 모델을 어떻게 서빙할지는 **취향이 아니라 쿼터가 결정합니다.**
이 문서는 실제로 배포를 시도하면서 측정한 사실만 담습니다.

---

## 1. 왜 패턴이 여러 개인가

| 패턴 | 표면 | LowPriority | 유휴 비용 | 스트리밍 | 용도 |
|---|---|---|---|---|---|
| `aml_online_vllm` | Managed online endpoint | ❌ 불가 | **24시간 과금** | ✅ | 실시간 챗, 데모, 로드테스트 |
| `aml_batch_vllm` | Batch endpoint (vLLM) | ✅ | **0** | ❌ | 대량 생성, 평가 |
| `aml_batch` | Batch endpoint (transformers) | ✅ | **0** | ❌ | 단순 배치 스코어링 |
| `aks_vllm` | AKS | ✅(노드풀) | 노드 유지 비용 | ✅ | 자체 클러스터 운영 시 |
| `local_vllm` | 로컬/개발 VM | — | — | ✅ | 개발, 디버깅 |

핵심 비대칭:

> **Managed online endpoint는 scale-to-zero가 없습니다.**
> 요청이 0건이어도 인스턴스가 24시간 과금됩니다.
> Batch endpoint는 일반 AmlCompute 위에서 돌기 때문에 `min_instances=0`을
> 그대로 상속합니다. 유휴 시 **0원**입니다.

이 차이가 비용의 대부분을 결정합니다. `NV36ads_A10_v5` 기준으로
$4.320/hr × 730h ≈ **월 $3,154** 가 아무것도 안 해도 나갑니다.

---

## 2. 온라인 엔드포인트는 SKU 코어의 **2배** 쿼터가 필요하다 (실측)

이건 문서에서 찾은 게 아니라 **실제로 막혀서** 알아낸 사실입니다.

```
(OutOfQuota) Not enough subscription CPU quota.
The amount of CPU quota requested is 72 and your maximum amount of quota is [N/A].
```

- 요청한 SKU: `Standard_NV36ads_A10_v5` = **36 코어**
- 승인받은 dedicated 쿼터: **36 코어**
- Azure가 요구한 값: **72 코어**

Managed online endpoint는 새 버전을 띄운 뒤 기존 버전을 내리는
**롤링 업데이트 여유분**으로 요청한 인스턴스보다 20% 많은 인스턴스를 잡되,
**정수 인스턴스 단위로 올림**합니다 — `ceil(1.2 × instances) × 인스턴스 코어`.

인스턴스가 1개면 올림 때문에 정확히 2배가 되므로, 위 실패 로그만 보면
"항상 2배"로 보입니다. 실제로는 2개면 3개분(4개분이 아님), 3개면 4개분입니다.
그리고 **A100/H100/ND 계열은 이 여유분이 아예 면제**됩니다(공식 지원 SKU
문서의 "Skip 20% Reservation" 열). 이 계열까지 2배로 계산하면 Azure가 받아줬을
쿼터를 우리 코드가 먼저 막습니다 — `UPGRADE_RESERVATION_EXEMPT_FAMILIES`.

`ServingSpec.blocked_reason()` 이 이 계산을 배포 **전에** 수행합니다.
예전에는 `쿼터 > 0` 만 확인했고, 그래서 절대 생성될 수 없는 배포를
20분 기다린 뒤에 실패시켰습니다.

```python
required_dedicated_cores("Standard_NV36ads_A10_v5")                # 72
required_dedicated_cores("Standard_NV18ads_A10_v5")                # 36
required_dedicated_cores("Standard_NV18ads_A10_v5", instances=2)   # 54  (3 x 18)
required_dedicated_cores("Standard_NC4as_T4_v3",   instances=10)   # 48  (12 x 4)
required_dedicated_cores("Standard_NC24ads_A100_v4")               # 24  (면제 계열)
```

**실전 우회법**: 쿼터가 36이면 18코어 SKU(`NV18ads_A10_v5`, 12GB)를 쓰면
바로 배포됩니다. 실제로 이 방법으로 검증을 진행했습니다.

---

## 3. 실패한 배포는 반드시 삭제해야 한다 (실측)

쿼터 때문에 실패한 뒤 SKU만 바꿔서 재시도하면 **두 번째 에러**가 납니다.

```
Specified deployment [blue] failed during initial provisioning
and is in an unrecoverable state. Delete and re-create.
```

`deploy_online()` 이 이제 `provisioning_state` 를 확인하고
`failed`/`canceled` 상태면 먼저 삭제한 뒤 재생성합니다.

---

## 4. Qwen3.8-27B 서빙 시 반드시 필요한 것들 (실측)

`Qwen/Qwen3.8-27B` 의 `config.json` 을 직접 읽어 확인한 값:

```
architectures  : ["Qwen3_5ForConditionalGeneration"]
model_type     : qwen3_5   (text: qwen3_5_text)
vision_config  : 존재함  ← 멀티모달입니다
layer_types    : linear_attention 48개 + full_attention 16개 (총 64층)
max_position   : 262,144
vocab_size     : 248,320
```

여기서 나오는 제약 세 가지:

| 항목 | 값 | 이유 |
|---|---|---|
| vLLM 최소 버전 | **v0.27.0+** | `Qwen3_5ForConditionalGeneration` 이 v0.27.0에서 처음 등록됨. 이미지는 `v0.27.1` 고정 |
| `--mamba-cache-mode` | **`align` 필수** | 64층 중 48층이 Gated DeltaNet 선형 어텐션. `all` 모드는 `NotImplementedError` |
| `--language-model-only` | 권장 | 멀티모달이라 비전 타워가 VRAM을 먹는데, 한국어 텍스트 SFT에는 쓰이지 않음 |

빌드 시점에 `docker/verify_serve.py` 가 아키텍처 등록 여부와 플래그 존재를
검사합니다. 20분짜리 롤아웃이 실패하고 나서 알게 되는 것보다 낫습니다.

이 플래그들은 **opt-out** 입니다. Qwen3-0.6B 같은 dense 텍스트 모델은
Mamba 상태도 비전 타워도 없기 때문에, 환경변수를 빈 문자열로 두면
플래그가 빠집니다. 모델 교체가 이 자산의 전제이므로 이렇게 해야 합니다.

---

## 5. VRAM: 27B는 A10 24GB에 bf16으로 안 들어간다

| 정밀도 | 필요 GPU | A10 24GB |
|---|---|---|
| BF16 | H200 1장 또는 H100 2장 | ❌ (가중치만 ~54GB) |
| FP8 | 40GB 1장 | ❌ |
| **Int4** | **24GB 1장** | ✅ |

즉 **A10에서 27B를 서빙하려면 Int4 양자화 체크포인트가 필요합니다.**
`deploy.merge` 가 만드는 bf16 병합본을 그대로 올릴 수 없습니다.

---

## 6. merged vs runtime adapter

| | `merged` | `runtime_adapter` |
|---|---|---|
| 방식 | LoRA를 base에 흡수해 일반 체크포인트로 저장 | base 하나 띄우고 어댑터를 이름으로 선택 |
| 지연 | 가장 낮음 | 약간의 오버헤드 |
| 여러 어댑터 | 각각 별도 배포 | **한 배포에서 다 서빙** |
| 양자화 | 자유 | 제약 있음 |
| Qwen3.8 위험도 | 낮음 | **미검증** |

`runtime_adapter` 주의: vLLM의 LoRA 훅은 `LinearBase` 계열에 붙습니다.
full-attention 16개 층과 MLP는 표준이라 안전하지만,
`configs/models.yaml` 이 Qwen3.8에 지정한 Gated DeltaNet 프로젝션
(`in_proj_qkvz`, `in_proj_ba`, `out_proj`)에 어댑터가 실제로 적용되는지는
**아직 검증되지 않았습니다.** 이 모드를 쓰기 전에 merged 결과와 비교해서
출력이 실제로 달라지는지 확인하세요.

---

## 7. 명령어

```bash
# 지금 뭐가 돈 나가고 있는지
ffsft lifecycle status

# 올리기
ffsft lifecycle up --endpoint ffsft-qwen \
    --hf-model Qwen/Qwen3-0.6B \
    --sku Standard_NV18ads_A10_v5

# 로드테스트
ffsft loadtest --base-url https://<endpoint>/v1 --api-key $KEY \
    --model ffsft --concurrency 1,2,4,8,16

# 내리기 (반드시)
ffsft lifecycle down --endpoint ffsft-qwen --yes
ffsft lifecycle down --all --yes
```

`down` 은 **과금되는 컴퓨트만** 지웁니다. ACR 이미지, 등록된 모델,
클러스터 정의는 남겨서 다음 실험 때 `up` 만으로 그대로 복원됩니다.
클러스터는 삭제하지 않고 `min_instances=0` 으로 내립니다.
