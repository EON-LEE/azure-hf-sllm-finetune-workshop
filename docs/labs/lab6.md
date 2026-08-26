# Lab 6 — 로드테스트와 토큰 뷰어

> **Track B · 선행: [Lab 5](lab5.md) 의 엔드포인트가 `Succeeded`**

> ## 🚨 Lab 5 의 엔드포인트가 계속 과금 중입니다
> $4.959/시. 이 Lab 을 끝내면 바로 [Lab 7](lab7.md) 로 가세요.

## 목표

- TTFT / TPOT / knee point 를 실측한다
- **tok/s 만 보면 오독한다**는 것을 두 배포 비교로 확인한다
- 사고 토큰이 실제로 어떤 필드로 오는지 눈으로 본다

## 선행조건

- 살아 있는 엔드포인트 하나 (Lab 5)
- 또는 **Azure 없이**: `scripts/mock_vllm_server.py`

## 소요·비용

**40분**. 로드테스트 자체는 몇 분입니다.

---

## 0. Azure 없이 먼저 해보기 (공짜)

```bash
uv run python scripts/mock_vllm_server.py &          # 127.0.0.1:8111
uv run ffsft-loadtest --base-url http://127.0.0.1:8111/v1 --model ffsft
```

모의 서버는 SSE 프레임 모양·필드명·`usage` 까지 실제 vLLM 과 같게 흉내냅니다.
**계량기 자체를 검증하는 용도**이지 성능 숫자는 의미가 없습니다.

> `MOCK_THINK_FIELD` 로 사고 델타의 필드명을 바꿀 수 있습니다. 기본은 `reasoning`
> (실제 와이어), 옛 이름 `reasoning_content` 도 재현 가능 — **두 와이어를 다
> 시험할 수 있어야 하기 때문**입니다. 이유는 아래 §3.

## 1. 실제 엔드포인트에 걸기

```bash
uv run bash scripts/verify_deployment.sh ffsft-lab blue     # 먼저 서빙되는지
```

로드테스트는 엔드포인트 URL 로 갑니다:

```bash
BASE="$(az ml online-endpoint show -n ffsft-lab --query scoring_uri -o tsv)"
uv run ffsft-loadtest \
   --base-url "${BASE%/chat/completions}" \
   --model ffsft \
   --concurrency 1,2,4,8,16 \
   --requests-per-level 20 \
   --ttft-slo 2.0
```

> `--api-key` 는 기본값이 `$FFSFT_ENDPOINT_KEY` 입니다. **키를 명령줄에 적지 마세요.**

## 2. 기대 출력 (실측 — A100 NC24ads 1 인스턴스, 27B bf16, max_model_len 8192)

아래는 `blue` 5레벨 중 요약 컬럼입니다. **p99·e2e p50 까지 포함한 전체 표와
`--output` 원자료 JSON 은 [`docs/RESULTS.md` §2](../RESULTS.md)** 에 있습니다.

```
 conc    ok  fail  TTFT p50  TTFT p95  TPOT p50   e2e p95     tok/s    req/s
    1    20     0     1.142     1.190    0.0364     5.779      22.0     0.18
    2    20     0     1.141     1.238    0.0364     5.813      43.4     0.36
    4    20     0     1.136     1.329    0.0371     6.055      83.0     0.68
    8    20     0     1.275     1.563    0.0382     6.364     131.6     1.08
   16    20     0     1.523     1.855    0.0407     6.987     204.3     1.68
```

- 100/100 성공, **실패 0**
- p95 TTFT 2.0초 SLO 를 만족하는 **최대 동시성 16**
- 동시성이 16배가 되는데 TPOT 는 **12% 만** 나빠지고 처리량은 **9.3배**
  → 연속 배칭(continuous batching)이 의도대로 동작한다는 뜻

## 3. ⚠️ tok/s 로 두 모델을 비교하면 틀린다 (§66.2)

같은 엔드포인트의 두 배포를 나란히 재면 (양 끝 레벨만; 5레벨 전체는 [RESULTS §3](../RESULTS.md)):

```
            blue (베이스)                   green (파인튜닝)
conc   tok/req  TPOT    tok/s  req/s |  tok/req  TPOT    tok/s  req/s
   1    121.8  0.0364    22.0  0.18  |   110.6  0.0363    21.3  0.19
  16    121.8  0.0407   204.3  1.68  |   110.6  0.0407   189.0  1.71
```

peak tok/s 가 204.3 → 189.0 으로 **7.5% 낮습니다.** 이걸 성능 저하로 적으면 **틀립니다.**

- **TPOT 는 소수점 넷째 자리까지 같습니다** (0.0364 / 0.0363 … 0.0407 / 0.0407).
  토큰 하나 뽑는 비용은 안 변했습니다.
- **req/s 는 green 이 오히려 높습니다** (c=16 에서 1.71 vs 1.68).
- 차이는 전부 **응답 길이**입니다. blue 121.8 tok/req vs green 110.6 — 9.2% 짧음.

blue 는 **영어 사고과정을 `content` 에 쏟아내서** 토큰 수가 부풀어 있었습니다.
green 은 `REASONING_PARSER=qwen3` 로 그걸 분리합니다.

> **tok/s 는 같은 일을 할 때만 비교 가능한 지표인데, 두 배포는 같은 일을 하고 있지
> 않습니다.** 서빙 속도로 읽어야 할 값은 **TPOT 와 req/s** 입니다.

원가로 환산하면 knee 기준 **100만 출력 토큰당 $7.29**, 동시성 1 로 쓰면 **$62.5** —
9.3배입니다. 배칭이 원가의 대부분입니다 ([RESULTS §4](../RESULTS.md)).

## 4. 토큰 뷰어 — 정성 평가

```bash
uv run bash scripts/run_token_viewer.sh ffsft-lab       # 기본 127.0.0.1:8112
```

브라우저에서 `http://127.0.0.1:8112`.

- 키는 이 프로세스의 환경변수로만 들어가고 **디스크에 안 씁니다.**
  브라우저도 키를 못 봅니다 — 페이지와 프록시가 같은 오리진이라 Authorization 헤더가
  서버 사이드에서 붙습니다. 뷰어는 `127.0.0.1` 에만 바인딩합니다.

뷰어가 보여주는 것:

| 요소 | 왜 있나 |
|---|---|
| **두 철자 모두 읽기** | `d.reasoning ?? d.reasoning_content`. `\|\|` 가 아니라 `??` 인 이유는 **빈 문자열 델타도 델타**이기 때문 |
| **route 선택** | `/chat/completions`(파서 통과) vs `/completions`(파서 우회, 원문) |
| **deployment 선택** | `azureml-model-deployment` 헤더 — 트래픽 분배를 안 건드리고 blue/green 정성 비교 |
| **max_tokens 사용 카드** | 청크 수가 아니라 `usage.completion_tokens`. **청크는 토큰이 아닙니다.** `finish_reason=length` 면 빨갛게 칠합니다 |

## 5. 사고 토큰 — 필드 이름이 `reasoning` 이다

`ffsft-plc/green` 의 SSE 를 파싱하지 말고 그대로 받아 델타 키를 세면 (§68.1):

```
SSE 프레임 수: 4921
delta 키별 등장 횟수: {'role': 1, 'reasoning': 4920}
```

**`reasoning_content` 는 단 한 프레임도 없습니다.**

> ### 이 버그가 왜 안 잡혔나 — 워크샵의 진짜 교훈
> 모의 서버가 `reasoning_content` 를 내보내고, 클라이언트도 `reasoning_content` 만
> 셌습니다. **모의 서버와 클라이언트가 같은 오타를 공유하니 테스트 스위트는 초록이었고,
> 실제 배포에서는 0 을 세고 있었습니다.**
> → [GOTCHAS #15](../GOTCHAS.md#15)

## 6. thinking 예산 (§68.6)

`max_tokens=7500` 으로 실측:

| 질문 | 소요 | finish | completion_tokens | 사고 / 답 |
|---|---|---|---|---|
| 12명·3대 조합론 | 180.6초 | **stop** | **4908** | 사고 12,238자 / 답 593자 |
| "서울은 어떤 도시야?" | 4.2초 | **stop** | **85** | 사고 124자 / 답 44자 |

어려운 문제는 **사고에만 4908 토큰**을 씁니다. `max_tokens` 를 120·700 으로 잡으면
`finish=length` 로 잘려서 **답이 시작조차 못 합니다.** 파서 탓도 파인튜닝 탓도 아니고
**상한을 사고 길이보다 작게 잡은 것**입니다. → [GOTCHAS #16](../GOTCHAS.md#16)

그래서 `configs/models.yaml` 의 `enable_thinking: false` 는 버그 우회가 아니라
**비용 결정**입니다 — 프롬프트 40토큰 + 사고 수천 토큰을 매 요청에 낼 것인가.
학습·서빙·로드테스트가 같은 프레임을 쓰므로 세 지점이 일치합니다.

---

## 막히면

| 증상 | 항목 |
|---|---|
| 사고 토큰이 0 으로 세어짐 | [#15](../GOTCHAS.md#15) — 필드는 `reasoning` |
| 답이 안 나오고 잘림 | [#16](../GOTCHAS.md#16) — `max_tokens` 가 사고보다 작음 |
| 빈 응답처럼 보임 | [#14](../GOTCHAS.md#14) — scoringUri 모양 |
| 파인튜닝했더니 tok/s 가 떨어짐 | §3 — TPOT 와 req/s 를 보세요 |
| 내 숫자가 표와 다름 | [RESULTS](../RESULTS.md) — ±20% 는 정상, 그 밖이면 여기 |
| 손으로 만든 프롬프트로 판정하고 싶을 때 | §68.4 — `POST /tokenize` 가 정답을 알려줍니다 |

## 정리 — 반드시

```bash
uv run ffsft-lifecycle status
```

**다음**: [Lab 7 — 내리기](lab7.md). 건너뛰지 마세요.
