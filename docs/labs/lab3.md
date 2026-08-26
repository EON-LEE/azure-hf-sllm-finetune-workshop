# Lab 3 — 평가: base 대 tuned

> **Track A · 선행: [Lab 2](lab2.md)**

> ## 💸 이 Lab 은 GPU 과금이 발생합니다
> 학습과 **같은 잡 안에서** 채점합니다. §23 실측 기준 학습+평가 합쳐 약 **$1.5**,
> 약 **50분**. 평가만 따로 돌리면 54 GB 다운로드가 한 번 더 발생합니다.

## 목표

- **델타가 작업 단위**임을 이해한다 — 단일 절대 점수는 아무 말도 하지 않는다
- 이 리포가 만든 것이 **점수가 아니라 측정 장치**라는 것을 확인한다
- n=25 의 +0.16 을 "성능 향상"이라 부르면 안 되는 이유를 계산으로 안다

## 선행조건

- Lab 2 의 학습이 `Completed` 이고 어댑터가 실재함이 확인됨

## 소요·비용

**50분** (학습 42분 + 채점 8분), 약 $1.5.

---

## 1. 학습과 평가를 한 잡으로

```bash
uv run python scripts/submit_training.py \
   --subscription $FFSFT_SUBSCRIPTION_ID \
   --resource-group $FFSFT_RESOURCE_GROUP \
   --workspace $FFSFT_WORKSPACE \
   --location $FFSFT_LOCATION \
   --model qwen3.8-27b --mix ko_commercial_safe \
   --max-steps 30 --max-seq-length 1024 --rank 16 \
   --eval-suite ko_fast --eval-limit 25
```

`build_command` 가 `train && eval` 로 이어 붙입니다. `;` 가 아니라 `&&` 인 이유:
**학습이 실패하면 어댑터가 없고, 그 상태로 base 를 "tuned" 라벨로 채점하면
아무것도 보고 안 하느니만 못하기 때문**입니다.

한 잡으로 묶으면 노드 할당 1회, 이미지 풀 1회, 54 GB 다운로드 1회로 끝나고
어댑터가 로컬 디스크를 벗어나지 않습니다.

## 2. 스위트 고르기

```bash
uv run ffsft bench suites
uv run ffsft bench list
```

| 스위트 | 벤치마크 | 쓰임 |
|---|---|---|
| `ko_fast` | kobest | 학습 반복 중 회귀 확인 — **이 Lab** |
| `ko_core` | kmmlu, hae_rae_1_1, ifeval_ko, logickor | 기본 4축 |
| `ko_full` | + kmmlu_hard, kobest, hrm8k | 전면 측정 |

> `configs/benchmarks.yaml` 의 모든 항목은 `eval_only: true` 입니다. **벤치마크 id 는
> 학습 믹스에 절대 못 들어갑니다** — 테스트로 고정돼 있습니다. LogicKor 심사 문항은
> 라이선스가 없어 벤더링하지 않았습니다.

## 3. 기대 출력 (§23 실측, `heroic_fennel_085y2rwm3s`)

학습이 재현됩니다:

```
train.train_loss  = 1.2638      ← 직전 27B 런은 1.2637
train.wall_seconds = 2540.2     (42.3분)
train.vram_peak_gb = 28.19
```

소수 넷째 자리까지 일치 — **한 번 우연히 된 게 아니라는 가장 싼 증거**입니다.

채점 결과 (태스크당 n=25, 눈금 0.04):

| 태스크 | base | tuned | delta |
|---|---|---|---|
| `kobest_boolq` | 0.72 | **0.88** | **+0.16** |
| `kobest_sentineg` | 0.96 | **1.00** | +0.04 |
| `kobest_copa` | 0.84 | 0.84 | 0.00 |
| `kobest_hellaswag` | 0.80 | 0.80 | 0.00 |
| `kobest_wic` | 0.48 | 0.48 | 0.00 |

## 4. ⚠️ 이 숫자로 "좋아졌다"고 말하면 안 된다

```
p ≈ 0.8, n = 25  →  표준오차 = sqrt(0.8 × 0.2 / 25) = 0.08
95% 신뢰구간 ≈ ±0.157
관측된 최대 델타 = +0.16          ← 신뢰구간 경계에 걸쳐 있다
```

- `boolq` 의 +0.16 = **25문항 중 4문항** 더 맞힌 것
- `sentineg` 의 +0.04 = **1문항**

340개 예제로 30스텝 돌린 LoRA 가 정말 뭔가 바꿨을 수도 있습니다. **다만 이 실험은
그것을 증명하지 못합니다.** 주장을 하려면 `--eval-limit` 을 빼고 전체 스플릿으로
돌려야 하고, 27B 를 두 번 적재해 채점하면 A100 LowPriority 로 2~4시간입니다.

**이 Lab 이 실제로 증명하는 것**: base 와 tuned 가 동일한 양자화·동일한 하네스·
동일한 노드에서 채점되고, 델타가 MLflow 로 자동 기록되며, 27B 두 벌을 순차 적재해도
85 GB 카드에서 OOM 이 없다는 것. **표본만 늘리면 그대로 유효한 실험이 됩니다.**

## 5. 결과 읽기

```bash
uv run bash scripts/watch_jobs.sh EVAL:<run-name>
```

`eval.*` 메트릭이 MLflow 로 옵니다. 잡 stdout 은 여전히 안 읽힙니다
→ [GOTCHAS #9](../GOTCHAS.md#9)

---

## 막히면

| 증상 | 항목 |
|---|---|
| 평가만 `HFLM.__init__` 에서 터진다 | §21 — lm-eval 로더를 이 리포가 직접 감쌌습니다 |
| 코드를 고쳤는데 옛 트레이스백 줄번호 | [#12](../GOTCHAS.md#12) — 소스는 이미지에 구워져 있습니다 |
| `train_loss` 는 정상인데 평가가 없다 | `--eval-suite` 를 안 준 경우. 기본은 학습만 |
| 델타가 전부 0.00 | 정상일 수 있습니다. §4 를 읽으세요 |

> **§21 이 비싼 이유**: 평가는 학습이 **성공한 뒤에야** 실행됩니다. 평가 버그 하나의
> 값 = 이미지 빌드(7~13분) + 학습 완주(27B 42분). 세 번 그렇게 냈습니다.
> 그래서 이 리포는 **평가 경로도 0.8B 스모크로 먼저 밟습니다.**

## 정리

학습 잡은 끝나면 노드를 반납합니다. 클러스터 `min_nodes=0` 만 확인하세요.

```bash
uv run ffsft-lifecycle status
```

**다음**: Track A 는 여기서 끝납니다. 배포까지 가려면 [Lab 4 — vLLM 이미지](lab4.md),
학습한 어댑터를 실제로 서빙하려면 [Lab 8 — 풀사이클](lab8.md).
