# Lab 2 — QLoRA 학습

> **Track A · 선행: [Lab 0](lab0.md) (+ 데이터가 있으면 [Lab 1](lab1.md))**

> ## 💸 이 Lab 은 GPU 과금이 발생합니다
> `Standard_NC24ads_A100_v4` LowPriority 기준 **시간당 약 $1.5** (Dedicated 는 $4.96).
> 27B 30스텝 실측 **41.6분**. 끝나면 클러스터 `min_nodes=0` 을 확인하세요.

## 목표

- 27B 모델에 **NF4 QLoRA** 로 실제 학습 스텝을 돌린다
- 잡이 `Completed` 인 것과 **어댑터가 남은 것이 다른 일**임을 확인한다

## 선행조건

- Lab 0 완료, A100 (40GB 이상) 을 빌릴 수 있는 리전
- 클러스터 하나 (`gpu-a100-lp`, LowPriority)

## 소요·비용

**60분** — 스모크 10분 + 실학습 42분 + 확인 5분.

---

## 1. 먼저 프리플라이트 — GPU 를 빌려서 자기검사만

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --preflight
```

노드에서 실제로 확인하는 것: `nf4_matmul_ok`, transformers 버전, GPU 모델/VRAM.
**클러스터가 살아 있는지 증명하는 가장 싼 방법**입니다.

기대 출력 (§16 실측):

```
nf4_matmul_ok: True
transformers  5.15.1
GPU           A100 80GB
```

## 2. 스모크 런 — 27B 가 아니라 0.8B 로 10스텝

**규칙: 27B 를 돌리기 전에 0.8B 를 돌린다** (§18.1). 파이프라인 버그는 모델 크기와
무관하게 같은 자리에서 터지고, 0.8B 는 5분·$0.10 에 그걸 알려줍니다.

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --model qwen3.5-0.8b --mix ko_smoke \
   --max-steps 10 --max-seq-length 512 --rank 8
```

> 💡 이 스모크 런이 실제로 27B 한 시간을 세 번 아꼈습니다 (§18, §19.2):
> `warmup_ratio` 제거(transformers v5), trl 의 `_is_vlm` 오판, blob 판독 불가 —
> 셋 다 5분 안에 드러났습니다.

기대 출력 (§19 실측, `olive_machine_58qllrq6y9`, 5분 17초):

| 지표 | 값 |
|---|---|
| `train.train_loss` | 1.601 |
| `train.steps` | 10 |
| `train.wall_seconds` | 276.3 |
| `train.examples` | 128 |
| `train.trainable_pct` | **1.0631** — 1%만 학습 = QLoRA 정상 |
| `train.vram_peak_gb` | 2.79 (카드 85.1) |

## 3. 진행 상황 보기

잡 stdout 은 이 워크스테이션에서 **안 읽힙니다.** 네트워크 격리 워크스페이스에서
뚫려 있는 채널은 MLflow 뿐입니다.

```bash
uv run bash scripts/watch_jobs.sh SMOKE:<run-name>
```

ARM 상태 + MLflow `lastvalues` 를 함께 폴링합니다. 메트릭 이름이 등록되고 값이
아직 안 왔으면 `(logged, no value yet)` 으로 나옵니다 — 정상입니다.
`LABEL:run-name` 으로 여러 잡을 한 화면에서 볼 수 있습니다.

> `submit_training.py --wait` 는 `MLClient.jobs.stream()` 을 씁니다. 이 워크스페이스에서는
> 성공한 런에서도 `AuthorizationFailure` 로 끝납니다 — 잡은 정상인데 로그만 못 읽는
> 것입니다 (§19.1). 그래서 이 Lab 은 `--wait` 대신 `watch_jobs.sh` 를 씁니다.
> → [GOTCHAS #9](../GOTCHAS.md#9)

## 4. 본 학습 — 27B, 30스텝

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --model qwen3.8-27b --mix ko_commercial_safe \
   --max-steps 30 --max-seq-length 1024 --batch-size 1 --grad-accum 16
```

기대 출력 (§20 실측, `olden_bean_302vkc7nbz`):

| 지표 | 값 |
|---|---|
| 데이터 | `ko_commercial_safe` 340건 |
| 스텝 | 30 (83.2초/스텝, 유효배치 16) |
| **벽시계** | **2496초 = 41.6분** |
| `train_loss` | 1.2637 |
| 적재 후 VRAM | 17.67 GB |
| **VRAM 피크** | **28.19 GB** |
| 학습 파라미터 | 116.73M (**0.79%**) |

> **28.19 GB 가 이 Lab 의 핵심 숫자입니다.** 24GB A10 에서는 **적재는 되고
> 첫 스텝에서 OOM** 납니다. 40GB 이상이 필요합니다. → [GOTCHAS #5](../GOTCHAS.md#5)
>
> 하이브리드 Gated-DeltaNet 48개 층에도 NF4 QLoRA 가 동작함을 실증한 값입니다.

## 5. ⚠️ 어댑터가 실제로 남았는지 확인

**잡이 `Completed` 인 것은 증거가 아닙니다.** 선언하지 않은 출력은 노드와 함께 사라지고,
이 리포는 그렇게 **27B 완주 2회분의 어댑터를 잃었습니다.**

```bash
uv run python scripts/verify_output_path.py <run-name> --output model_dir
```

- `Completed` = 그 폴더에 파일과 바이트와 어댑터 가중치가 있었다
- `Failed` = 없었다. 학습이 쓸 만한 걸 안 남겼다

판정이 **종료 코드에 있는 이유**: 이 워크스페이스에서 MLflow 메트릭 값도 잡 stdout 도
읽히지 않습니다. 잡 상태가 유일하게 확실히 동작하는 채널입니다. → [GOTCHAS #9](../GOTCHAS.md#9)

---

## 막히면

| 증상 | 항목 |
|---|---|
| 학습은 되는데 성능이 안 는다 | [#6](../GOTCHAS.md#6) — LoRA 타깃 미선언 |
| `Completed` 인데 어댑터가 없다 | [#7](../GOTCHAS.md#7) — 선언 안 된 출력 |
| 24GB 카드에서 OOM | [#5](../GOTCHAS.md#5) — 피크 28.19 GB |
| `No space left on device` | [#8](../GOTCHAS.md#8) — 노드 디스크 64 GB |
| 코드를 고쳤는데 옛 동작 | [#12](../GOTCHAS.md#12) — 이미지 태그를 올리세요 |
| 노드가 영원히 대기 | [#2](../GOTCHAS.md#2), [#3](../GOTCHAS.md#3) |

> **LowPriority 노드는 선점됩니다.** 체크포인트 없이 긴 런을 돌리지 마세요.
> LowPriority 가 기본인 이유: 전용 GPU 쿼터는 계열별이고 기본값이 0 이며,
> 테넌트 N-시리즈 거부 정책이 허용하는 유일한 티어입니다.

## 정리

```bash
uv run ffsft-lifecycle status         # 클러스터가 0 노드로 내려갔나
```

AmlCompute 는 `min_nodes=0` 이면 유휴 시 과금되지 않습니다. 잡이 끝나면 자동으로 내려갑니다.

**다음**: [Lab 3 — 평가](lab3.md) · 배포까지 가려면 [Lab 4](lab4.md)
