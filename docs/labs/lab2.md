# Lab 2 — QLoRA 학습 + 평가, 한 잡으로

> **Track A · 선행: [Lab 0](lab0.md)**

> ## [Lab 1](lab1.md) 을 했든 안 했든 이 Lab 은 똑같이 돕니다
> Lab 1 이 등록하는 `azureml:ko-sft:1` 을 **이 Lab 은 읽지 않습니다.** 데이터 관련
> 플래그는 `--mix` 하나뿐이고 (`submit_training.py --help`), 잡은
> `configs/datasets.yaml` 의 믹스를 **노드 안에서 HF 로부터 직접** 받습니다.
> 등록된 자산을 가리킬 입력 플래그가 없습니다 — **없는 걸 못 쓰는 게 아니라 애초에 없습니다.**
>
> 그래서 Lab 1 의 산출물이 **워크샵 안에서 쓰이는 곳은 없습니다.** Lab 1 은
> Fabric Spark → OneLake → Azure ML 데이터 자산까지의 **준비 경로를 실증하는 Lab**
> 이고, 그 자산은 여러분의 운영 파이프라인이 쓸 물건입니다. 워크샵의 Track A 는
> 믹스 경로로 갑니다. Lab 1 을 건너뛰어도 Lab 2·3 은 완주됩니다 — **40분을 쓰기 전에
> 어느 쪽인지 정하세요.**

> ## 💸 이 Lab 은 GPU 과금이 발생합니다
> 27B **학습 + 채점을 한 잡**으로 완주한 실측 요금 **약 $1.5** (§23.5, A100 LowPriority).
> 27B 30스텝 학습 실측 **41.6분**(§20) / 채점까지 붙인 런은 **42.3분**(§23.1).
>
> 시간당 요율은 티어를 구분해서 읽으세요 — **위 $1.5 는 잡 1회의 총액이지 요율이 아닙니다.**
> | 티어 | $/시 | 출처 |
> |---|---|---|
> | Dedicated (PAYG) | **$4.959** | `SKU_HOURLY_PAYG` — **상한**으로만 쓰세요 |
> | Spot | **$0.916** | Retail Prices API, koreacentral, 2026-08-27 조회. **Spot ≠ LowPriority** |
> | **LowPriority — 이 클러스터가 쓰는 티어** | **$0.992** | Retail Prices API, koreacentral, 2026-08-27 조회. `SKU_HOURLY_PAYG` 에는 **없습니다** (그 표는 PAYG 전용) |
>
> **세 값을 서로에게서 유도하지 마세요.** 이 SKU 에서 LowPriority 가 PAYG 의 정확히
> 0.20 배인 것은 **조회 결과이지 규칙이 아닙니다** — 같은 리전의
> `Standard_ND96isr_H100_v5` 는 LowPriority 미터가 **아예 없는데도**
> `azure_ml.py` 의 `GPU_SKUS` 가 그 SKU 에 `low_priority: True` 를 선언합니다.
> Spot 과 LowPriority 도 **서로 다른 미터**고 어느 쪽이 싼지는 계열마다 뒤집힙니다.
> 표에 없는 티어·SKU 는 직접 조회해서 채우고, **못 찾으면 비워 두세요.**
>
> 요율 × 학습 벽시계(42.3분)는 약 $0.70 입니다. 실측 총액 $1.5 와의 차이는 **채점 구간과
> 노드 점유 시간**이고 — 이건 두 숫자를 뺀 산수지 측정이 아닙니다. 그 구간의 벽시계는
> 이 리포에 기록이 없으므로 여기서 역산해 "채점은 몇 분" 이라고 적지 마세요.
>
> 끝나면 클러스터 `min_nodes=0` 을 확인하세요.

## 목표

- 27B 모델에 **NF4 QLoRA** 로 실제 학습 스텝을 돌린다
- 학습과 채점을 **한 노드 안에서** 끝내, 어댑터가 스토리지를 안 거치게 한다 (§21.6)
- 잡이 `Completed` 인 것과 **어댑터가 남은 것이 다른 일**임을 확인한다

## 선행조건

- Lab 0 완료, A100 (40GB 이상) 을 빌릴 수 있는 리전
- **첫 줄은 프로필을 통째로 읽는 것입니다.** `AZURE_CONFIG_DIR` 만 다시 export 하면
  `az` 프로필은 맞는데 **워크스페이스 변수가 하나도 없는 셸**이 됩니다 — 그런 셸에서는
  아래 명령들이 여러분이 정한 워크스페이스로 가지 않습니다.

```bash
source ~/.ffsft-env
```

```
profile: ffsft  rg=rg-ffsft-<본인>  ws=mlw-<본인>  loc=<region>
```

  배너가 찍혀야 이 Lab 의 명령이 여러분의 워크스페이스로 갑니다
  ([Lab 0 §4](lab0.md)). **배너가 아예 안 찍히거나 `rg=`/`ws=`/`loc=` 뒤가 비어 있으면**
  `source` 가 안 됐거나 `infra up` 이 env 를 못 쓴 것입니다 → [Lab 0 §4.2](lab0.md) 의
  네 값 확인부터 다시 하세요. 이 Lab 의 명령들은 그 값을 그대로 읽습니다.

- 클러스터 하나 (`gpu-a100-lp`, LowPriority). 없으면 지금 만듭니다 —
  Lab 0 은 `--dry-run` 으로 가드만 돌렸고, **`--dry-run` 은 아무것도 만들지 않습니다**.
  **배너를 확인한 그 셸에서** 돌리세요:

```bash
uv run python scripts/provision_azure.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --compute-name gpu-a100-lp \
   --sku Standard_NC24ads_A100_v4 \
   --priority LowPriority
```

## 소요·비용

**70분** — 프리플라이트·스모크 15분 + 학습·채점 잡 + 확인 5분. 잡 요금 **약 $1.5** (§23.5).

> 잡 안의 학습 구간은 **42.3분 실측**(§23.1)입니다. 채점 구간의 벽시계는
> 이 리포에 기록이 없습니다 — 위 70분에서 그 부분은 추정입니다.

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

> ### 이 잡은 **채널 점검이기도 합니다** — 지금 붙여 보세요
> 위 세 줄은 잡 stdout 이고, 이 워크스테이션에서 **stdout 은 안 읽힙니다** (§19.1).
> 그래서 `preflight.publish()` 가 같은 리포트를 **MLflow 로** 올립니다. 즉 이 잡은
> 워크샵에서 **MLflow 가 읽히는지 확인할 수 있는 가장 싼 자리**입니다 — 몇 분짜리
> LowPriority 잡 하나.
>
> ```bash
> uv run bash scripts/watch_jobs.sh PREFLIGHT:<run-name>     # 인자 모양은 아래 3번
> ```
>
> `preflight.*` 메트릭이 **한 줄이라도** 뜨면 [Lab 3](lab3.md) 이 읽을 채널이 살아
> 있는 것입니다. 상태만 바뀌고 `preflight.*` 가 끝까지 안 오면 **여기서 멈추세요.**
> 그대로 4번의 42분·약 $1.5 짜리 잡을 내면 델타는 만들어지고 **읽을 방법이 없습니다.**
> → [GOTCHAS #9](../GOTCHAS.md#9), [Lab 0 §5.1](lab0.md)

## 2. 스모크 런 — 27B 가 아니라 0.8B 로, 채점 경로까지

**규칙: 27B 를 돌리기 전에 0.8B 를 돌린다** (§18.1). 파이프라인 버그는 모델 크기와
무관하게 같은 자리에서 터지고, 0.8B 는 5분·$0.10 에 그걸 알려줍니다.

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --model qwen3.5-0.8b --mix ko_smoke \
   --max-steps 10 --max-seq-length 512 --rank 8 \
   --eval-suite ko_fast --eval-limit 5
```

**`--eval-suite` 를 스모크에도 붙이는 이유**: 평가는 학습이 **성공한 뒤에야** 실행됩니다.
평가 코드의 버그 하나 값 = 이미지 빌드(7~13분) + 27B 학습 완주(42분). 세 번 그렇게 냈습니다 (§21).

> 💡 이 스모크 런이 실제로 27B 한 시간을 세 번 아꼈습니다 (§18, §19.2):
> `warmup_ratio` 제거(transformers v5), trl 의 `_is_vlm` 오판, blob 판독 불가 —
> 셋 다 5분 안에 드러났습니다.

기대 출력 — 측정된 런 **두 개**입니다. 같은 런이 아니니 섞어 읽지 마세요.

**(a) 학습만 (§19 실측, `olive_machine_58qllrq6y9`, 5분 17초)**

| 지표 | 값 |
|---|---|
| `train.train_loss` | 1.601 |
| `train.steps` | 10 |
| `train.wall_seconds` | 276.3 |
| `train.examples` | 128 |
| `train.trainable_pct` | **1.0631** — 1%만 학습 = QLoRA 정상 |
| `train.vram_peak_gb` | 2.79 (카드 85.1) |

**(b) 학습 → 어댑터 → base 채점 → tuned 채점 → 델타 (§21.5 실측, `hungry_bell_lpf45kx8kv`)**

```
train.train_loss = 1.6009
eval.kobest.base = 0.4    eval.kobest.tuned = 0.4    eval.kobest.delta = 0.0
eval.kobest_boolq      0.8 / 0.8 / 0.0
eval.kobest_copa       0.4 / 0.4 / 0.0
eval.kobest_hellaswag  0.4 / 0.4 / 0.0
eval.kobest_sentineg   0.6 / 0.6 / 0.0
eval.kobest_wic        0.6 / 0.6 / 0.0
```

**델타 0 은 여기서 정상입니다.** `--eval-limit 5` 면 눈금이 0.2 이고, 128샘플 10스텝
LoRA 가 그 눈금을 움직일 리 없습니다. 이 런이 증명하는 것은 점수가 아니라 **배관**입니다.
(b) 런의 벽시계는 기록이 없습니다 — (a) 의 5분 17초는 채점이 없는 런의 값입니다.

## 3. 진행 상황 보기

잡 stdout 은 이 워크스테이션에서 **안 읽힙니다.** 네트워크 격리 워크스페이스에서
뚫려 있는 채널은 MLflow 뿐입니다.

```bash
uv run bash scripts/watch_jobs.sh SMOKE:hungry_bell_lpf45kx8kv
```

ARM 상태 + MLflow `lastvalues` 를 함께 폴링합니다. 메트릭 이름이 등록되고 값이
아직 안 왔으면 `(logged, no value yet)` 으로 나옵니다 — 정상입니다.

### 인자 모양은 `[라벨:]<잡이름>` 이고, 여러 개 줄 수 있습니다

이 스크립트는 워크샵의 세 Lab 이 같이 쓰는데 인자 모양을 어디에도 안 적어놨습니다.
`scripts/watch_jobs.sh:4-7` 의 사용법이 전부입니다:

| 조각 | 무엇 | 예 |
|---|---|---|
| **라벨** (선택) | **여러분이 정하는 출력 표식.** 정해진 값이 없습니다 — `SMOKE`/`LAB2`/`TRAIN`/`MERGE` 는 이 문서들의 관용어일 뿐이고 스크립트는 아무 문자열이나 받습니다 | `SMOKE` |
| **잡이름** (필수) | **제출이 돌려준 런 이름.** `submit_training.py` 가 찍는 JSON 의 `name` 필드입니다 | `hungry_bell_lpf45kx8kv` |

콜론을 빼고 이름만 주면 **이름이 곧 라벨**이 됩니다. 두 잡을 한 화면에 붙이려면
그냥 나열합니다 — 스크립트 머리말(`watch_jobs.sh:5`)의 예가 그대로 이 모양입니다:

```bash
uv run bash scripts/watch_jobs.sh \
   TRAIN:olden_bean_302vkc7nbz MERGE:loving_pumpkin_h0slhvf2l6
```

한 줄의 **모양**은 `라벨 [경과분] 상태` 이고 (`watch_jobs.sh:48`), **바뀔 때만**
찍습니다 — 조용한 화면은 멈춘 게 아니라 상태가 그대로인 것입니다.

- 라벨에도 잡 이름에도 **콜론을 넣지 마세요.** 라벨은 **첫** 콜론 앞, 잡 이름은
  **마지막** 콜론 뒤로 잘립니다 (`${PAIR%%:*}` / `${PAIR##*:}`).
- **리포 루트에서 돌리세요.** 안의 파이썬을 `.venv/bin/python` 이라는 **상대 경로**로
  찾습니다 (`FFSFT_PYTHON` 으로 덮을 수 있습니다).
- 프로필이 실려 있어야 합니다. `_common.sh` 가 `$FFSFT_SUBSCRIPTION_ID` /
  `$FFSFT_RESOURCE_GROUP` / `$FFSFT_WORKSPACE` 로 조회 URI 를 조립하므로,
  **프로필이 틀리면 "잡이 없다" 가 아니라 상태가 `?` 로 나옵니다.**

> `submit_training.py --wait` 는 `MLClient.jobs.stream()` 을 씁니다. 이 워크스페이스에서는
> 성공한 런에서도 `AuthorizationFailure` 로 끝납니다 — 잡은 정상인데 로그만 못 읽는
> 것입니다 (§19.1). 그래서 이 Lab 은 `--wait` 대신 `watch_jobs.sh` 를 씁니다.
> → [GOTCHAS #9](../GOTCHAS.md#9)

## 4. 본 잡 — 27B 30스텝 학습 + `ko_fast` 채점

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --model qwen3.8-27b --mix ko_commercial_safe \
   --max-steps 30 --max-seq-length 1024 --batch-size 1 --grad-accum 16 --rank 16 \
   --eval-suite ko_fast --eval-limit 25
```

**이 두 플래그가 이 Lab 의 설계입니다.** `build_command` 가 `train && eval` 로 이어 붙여
**노드 할당 1회, 이미지 풀 1회, 54 GB 다운로드 1회**로 끝냅니다. `;` 가 아니라 `&&` 인 이유:
학습이 실패하면 어댑터가 없고, 그 상태로 base 를 "tuned" 라벨로 채점하면 아무것도
보고 안 하느니만 못하기 때문입니다.

> **평가를 별도 잡으로 쪼갤 수 없는 이유** (§21.6): 어댑터를 다른 잡으로 넘기려면
> `workspaceblobstore` 를 거쳐야 하고, 그 경로가 §17 에서 실패가 증명된 바로 그 경로입니다.
> 쪼개면 54 GB 다운로드도 한 번 더 냅니다.

`--eval-suite` 를 빼면 학습만 돕니다. 그러면 **Lab 3 에서 읽을 델타가 없고**, 나중에
붙이려면 학습부터 다시 도는 잡을 또 내야 합니다.

기대 출력 (§23 실측, `heroic_fennel_085y2rwm3s`):

| 지표 | 값 |
|---|---|
| 데이터 | `ko_commercial_safe` 340건 |
| 스텝 | 30 (유효배치 16, seq 1024) |
| **학습 벽시계** | **2540.2초 = 42.3분** |
| `train.train_loss` | 1.2638 |
| 적재 후 VRAM | 17.67 GB |
| **VRAM 피크** | **28.19 GB** |
| 학습 파라미터 | 116.73M (**0.7867%**) |

직전 **학습 전용** 런 (§20 실측, `olden_bean_302vkc7nbz`) 과 나란히 두면:

| | §20 (학습만) | §23 (학습+채점) |
|---|---|---|
| 벽시계 | **2496초 = 41.6분** (83.2초/스텝) | 2540.2초 = 42.3분 |
| `train_loss` | **1.2637** | **1.2638** |

소수 넷째 자리까지 일치 — **한 번 우연히 된 게 아니라는 가장 싼 증거**입니다.
채점 플래그를 붙여도 학습은 그대로 재현됩니다.

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

채점 리포트는 같은 출력 안 `model_dir/eval/eval_report.json` 에 있고, `eval.*` 메트릭은
MLflow 로 올라옵니다. **읽는 법은 [Lab 3](lab3.md) 입니다** — 이제 GPU 는 안 씁니다.

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
| 학습은 `Completed` 인데 `eval.*` 이 하나도 없다 | `--eval-suite` 를 안 준 경우. 기본은 학습만 |

> **LowPriority 노드는 선점됩니다.** 체크포인트 없이 긴 런을 돌리지 마세요.
> LowPriority 가 기본인 이유: 전용 GPU 쿼터는 계열별이고 기본값이 0 이며,
> 테넌트 N-시리즈 거부 정책이 허용하는 유일한 티어입니다.

## 정리

```bash
uv run ffsft lifecycle status         # 클러스터가 0 노드로 내려갔나
```

AmlCompute 는 `min_nodes=0` 이면 유휴 시 과금되지 않습니다. 잡이 끝나면 자동으로 내려갑니다.

**다음**: [Lab 3 — 델타 읽기](lab3.md) (GPU 과금 없음) · 배포까지 가려면 [Lab 4](lab4.md)
