# Lab 6 — 로드테스트와 토큰 뷰어

> **Track B · 선행: [Lab 5](lab5.md) 의 엔드포인트가 `Succeeded`**

> ## 🚨 Lab 5 의 엔드포인트가 계속 과금 중입니다
> $4.959/시. 이 Lab 40분이면 **약 $3.3** 입니다.
> 끝나면 [Lab 8](lab8.md)(green 까지) 또는 바로 [Lab 7](lab7.md) — **순서는 아래 「정리」**.
> **Lab 8 은 살아 있는 blue 를 필요로 합니다.** 거기까지 갈 거면 여기서 내리지 마세요.

## 목표

- TTFT / TPOT / knee point 를 실측한다
- **tok/s 만 보면 오독한다**는 것을 확인한다 — 내 회차가 상한에 눌렸는지로(배포 하나),
  그리고 기준 회차의 두 배포 비교를 읽어서
- 사고 토큰이 실제로 어떤 필드로 오는지 눈으로 본다

## 선행조건

- 살아 있는 엔드포인트 하나 (Lab 5) — **배포는 `blue` 하나면 됩니다**
- 또는 **Azure 없이**: `scripts/mock_vllm_server.py`

## 소요·비용

**40분**. 로드테스트 자체는 몇 분이고 나머지는 뷰어와 해석입니다. 그동안 Lab 5 의
엔드포인트가 계속 돕니다 — **$4.959/시 × 40분 ≈ $3.3**.
§0 의 모의 서버 경로만 밟으면 **$0** 입니다.

---

## 시작 전 — 셸에 프로필을 싣습니다

```bash
source ~/.ffsft-env
```

```
profile: ffsft  rg=rg-ffsft-<본인>  ws=mlw-<본인>  loc=<region>
```

> ⚠️ **배너에 여러분의 prefix 가 안 보이면 이 Lab 의 명령을 하나도 돌리지 마세요.**
> `ffsft lifecycle` 도 `ffsft deploy` 도 워크스페이스를 인자로 받지 않습니다 —
> 환경변수가 유일한 조향 장치입니다 (`AzureTarget.from_env`). 빈 셸에서 `status` 를
> 부르면 저자들의 기본값(`rg-ffsft-kc` / `mlw-ffsft`)을 조회하고 **`BILLING NOW:
> nothing` 을 찍습니다.** 그 문장은 "아무 데서도 안 돈다"가 아니라 "**여기서는** 안
> 돈다"입니다. 파일이 없으면 [Lab 0 §4](lab0.md) 를 아직 안 한 것입니다.

**§0 만 예외입니다** — 모의 서버 경로는 Azure 를 아예 안 부릅니다.

## 0. Azure 없이 먼저 해보기 (공짜)

```bash
uv run python scripts/mock_vllm_server.py &          # 127.0.0.1:8111
uv run ffsft loadtest --base-url http://127.0.0.1:8111/v1 --model ffsft
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

### 1.1 엔드포인트 키를 환경변수에 담습니다

`ffsft loadtest` 의 `--api-key` 기본값이 `$FFSFT_ENDPOINT_KEY` 인데, **여기까지 그 변수를
채운 Lab 이 없습니다.** 비운 채로 §1.2 를 돌리면 401 입니다. 방금 통과한
`verify_deployment.sh` 는 증거가 안 됩니다 — 그 스크립트는 키를 **자기 안에서** 가져다
쓰고 (`scripts/verify_deployment.sh:29`) 여러분 셸에는 아무것도 남기지 않습니다.

```bash
# 프로필이 먼저 올라와 있어야 합니다 -- 키는 **그 워크스페이스**에서만 나옵니다.
source ~/.ffsft-env

# `_common.sh` 를 이 셸에 직접 source 하지 않습니다: 그 파일은 스크립트용이라
# `set -u` 를 이 셸에 걸고, 구독이 어긋나면 `exit 1` -- **터미널이 닫힙니다.**
# 서브셸로 부르면 실패는 서브셸에서 끝나고 stdout 으로는 키만 넘어옵니다.
FFSFT_ENDPOINT_KEY="$(bash -c '. scripts/_common.sh; ffsft_endpoint_key "$1"' _ ffsft-lab)"
export FFSFT_ENDPOINT_KEY
[ -n "$FFSFT_ENDPOINT_KEY" ] || echo "key lookup failed -- 위 stderr 를 읽으세요"
echo "key acquired (length ${#FFSFT_ENDPOINT_KEY})"
```

리포 루트에서 돌리세요 (`scripts/...` 상대경로). 기대 출력:

```
key acquired (length <0 이 아닌 수>)
```

> **길이만 봅니다. 0 이 아니면 성공입니다.** 자릿수 자체는 엔드포인트마다 다를 수 있고,
> 이 리포에 실측 기록이 없어 **"몇 자여야 한다"고 못 씁니다.** 0 이면 위의 stderr 에
> 이유가 적혀 있습니다 (구독 불일치·엔드포인트 없음·권한).

> ### 왜 파일이 아니라 환경변수이고, 왜 길이만 찍나
> - `primaryKey` 는 **bearer 자격증명**입니다. 이걸 가진 사람은 여러분의 **$4.959/시**
>   엔드포인트를 키를 돌릴 때까지 마음대로 씁니다.
> - `~/.ffsft-env` 계열에 **넣지 마세요.** [Lab 0](lab0.md) 이 "자격증명은 이 파일에
>   없습니다" 라고 약속한 파일이고, 그 약속이 이 파일을 백업하거나 화면에 띄워도 되게
>   만듭니다. 키를 한 줄 넣는 순간 그 약속이 깨집니다.
> - 변수는 셸과 함께 사라집니다. **새 셸이면 이 블록을 다시 돌리세요** — `source` 와
>   같은 규칙입니다.
> - `echo $FFSFT_ENDPOINT_KEY` 를 치지 마세요. 워크샵은 화면 공유·녹화되고 스크롤백에
>   남습니다. **알아야 할 건 "받았나/못 받았나" 하나뿐이고 길이가 그걸 답합니다** —
>   `scripts/verify_deployment.sh` 도 정확히 같은 줄(`key acquired (length ...)`)을 찍습니다.
> - 명령줄(`--api-key <키>`)도 안 됩니다. 셸 히스토리와 `ps` 에 남습니다. 그래서
>   `ffsft loadtest` 와 `compare_deployments.py` 의 `--api-key` 기본값이
>   `$FFSFT_ENDPOINT_KEY` 입니다.
> - 조회 URL 에는 `$FFSFT_RESOURCE_GROUP` 과 `$FFSFT_WORKSPACE` 가 그대로 들어갑니다
>   (`scripts/_common.sh:53-61`). **프로필이 틀리면 키가 아니라 404 가 옵니다** — 이건
>   좋은 소식입니다. 조용히 틀리는 게 아니라 즉시 틀립니다.
> - `scripts/run_token_viewer.sh` 와 `scripts/verify_deployment.sh` 는 **자기가 직접 키를
>   가져옵니다.** 그 둘이 돌았다고 이 변수가 실려 있는 건 아닙니다.

### 1.2 로드테스트

로드테스트는 엔드포인트 URL 로 갑니다:

```bash
BASE="$(az ml online-endpoint show -n ffsft-lab \
        -g "$FFSFT_RESOURCE_GROUP" -w "$FFSFT_WORKSPACE" \
        --query scoring_uri -o tsv)"
uv run ffsft loadtest \
   --base-url "${BASE%/chat/completions}" \
   --model ffsft \
   --concurrency 1,2,4,8,16 \
   --requests-per-level 20 \
   --ttft-slo 2.0 \
   --output my-loadtest.json
```

> **`-g`/`-w` 를 빼지 마세요.** `az ml` 은 그 둘이 없으면 `az configure --defaults` 를
> 보는데, 이 워크샵은 그걸 설정한 적이 없습니다. 프로필의 `rg=`/`ws=` 와 이 두 인자가
> 같은 값을 말하는 건 의도한 것입니다.

> **`--output` 이 이 명령의 산출물입니다.** §2.1 의 그래프도 §3.1 의 토큰 수 확인도
> `my-loadtest.json` 을 읽습니다. 빼면 표만 화면에 찍히고 **파일은 안 생겨서** 두 절이
> 파일 없음으로 끝납니다. 같은 형태가 [PERFORMANCE §11](../PERFORMANCE.md) 의 재현 절차에
> 있습니다.
> `--max-tokens` 는 안 줬습니다 — argparse 기본값이 **128** 이고
> (`src/ffsft/serve/loadtest.py`), §3.1 의 `of 128` 이 그 값입니다.

> `--api-key` 는 기본값이 `$FFSFT_ENDPOINT_KEY` 입니다 (§1.1). **키를 명령줄에 적지 마세요.**

## 2. 기대 출력 (실측 — A100 NC24ads 1 인스턴스, 27B bf16, max_model_len 8192)

아래는 `blue` 5레벨 중 요약 컬럼입니다. **p99·e2e p50 까지 포함한 전체 표와
`--output` 원자료 JSON 은 [`docs/PERFORMANCE.md` §2](../PERFORMANCE.md)** 에 있습니다.

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

### 2.1 그래프로 보기

숫자 표에서는 knee 가 어디인지, 배칭이 도는지가 잘 안 보입니다. `--output` 으로
남긴 JSON 을 그대로 그래프로 그립니다 (matplotlib 불필요 — 표준 라이브러리로 SVG).

```bash
uv run ffsft plot mine=my-loadtest.json --out-dir .
```

`ttft-vs-concurrency.svg`, `tpot-vs-concurrency.svg`, `throughput-vs-concurrency.svg`,
`tokens-per-request.svg` 4장이 나옵니다. 기준 회차는 이렇게 생겼습니다:

![기준 회차 TTFT — p95 가 SLO 선 아래에 머문다](../results/ttft-vs-concurrency.svg)

- **p95(점선)가 SLO 선을 뚫는 직전이 knee** 입니다.
- 여러분의 곡선이 c=1 부터 SLO 위에 있으면 부하가 아니라 **배포가 문제**입니다.
  [GOTCHAS](../GOTCHAS.md) 로 가세요.

![기준 회차 TPOT — 두 배포가 겹친다](../results/tpot-vs-concurrency.svg)

**동시성을 16배 올리는 동안 TPOT 는 12% 만 나빠집니다.** 이 선이 동시성에 비례해
올라가면 연속 배칭이 안 도는 것입니다.

## 3. ⚠️ tok/s 로 두 모델을 비교하면 틀린다 (§66.2, §70)

> **배포가 `blue` 하나뿐이면 §3.2 는 못 돌립니다.** `compare_deployments.py` 는 배포를
> 둘 미만으로 주면 `name at least two deployments -- one alone has nothing to compare`
> 를 찍고 **exit 2** 로 끝납니다. green 을 만드는 건 [Lab 8](lab8.md) 입니다.
> **§3.1 은 blue 하나로 지금 돌아가고, 이 절의 교훈은 §3.1 에 다 들어 있습니다.**
> 아래 표는 기준 회차의 **실측을 읽는** 부분이지 지금 재는 부분이 아닙니다.

기준 회차에서 같은 엔드포인트의 두 배포를 나란히 쟀습니다 (양 끝 레벨만; 5레벨 전체는
[PERFORMANCE §6](../PERFORMANCE.md)):

```
            blue (베이스)                   green (파인튜닝)
conc   tok/req  TPOT    tok/s  req/s |  tok/req  TPOT    tok/s  req/s
   1    121.8  0.0364    22.0  0.18  |   110.6  0.0363    21.3  0.19
  16    121.8  0.0407   204.3  1.68  |   110.6  0.0407   189.0  1.71
```

peak tok/s 가 204.3 → 189.0 으로 **7.5% 낮습니다.** 이걸 성능 저하로 적으면 **틀립니다.**

- **TPOT 는 5개 레벨 전부에서 소수점 넷째 자리까지 같거나 0.0001 차이입니다**
  (0.0364/0.0363 … 0.0407/0.0407 — [PERFORMANCE §4](../PERFORMANCE.md)).
  토큰 하나 뽑는 비용은 안 변했습니다.
- **req/s 는 green 이 오히려 높습니다** (c=16 에서 1.71 vs 1.68).
- 차이는 전부 **응답 길이**입니다. blue 121.8 tok/req vs green 110.6 — 9.2% 짧음.

**왜 짧은지는 집계값으로 알 수 없습니다.** 상한에서 잘렸을 수도, 사고과정이
`content` 로 샜을 수도, 진짜로 말을 덜 했을 수도 있고 셋의 의미가 전부 다릅니다.
프롬프트 단위로 다시 재보면 이 회차는 8개 중 **5개가 양쪽 다** `max_tokens=128` 상한에서
잘렸습니다 — 배포별로 세면 blue 6/8, green 6/8 인데 **겹치는 건 5개**이고 나머지는
한쪽만 잘렸습니다(blue 만 1개, green 만 1개, 양쪽 다 안 잘린 게 1개). 격차 223토큰은
**프롬프트 한 개**가 혼자 만들었습니다 ([PERFORMANCE §6.1](../PERFORMANCE.md)).
사고과정 누출은 0건이었습니다.

> **tok/s 는 두 배포가 같은 일을 할 때만 비교 가능한 지표입니다.**
> 서빙 속도로 읽어야 할 값은 **TPOT 와 req/s** 이고, 길이 차이가 보이면
> `finish_reason` 을 먼저 확인하세요.

### 3.1 배포가 하나여도 되는 확인 — 내 토큰 수가 길이가 맞나

상한에서 잘린 응답의 토큰 수는 **길이가 아니라 `max_tokens` 그 자체**입니다. 두 배포가
없어도 이건 혼자 확인됩니다. §1.2 에서 `--output` 으로 남긴 JSON 을 보세요:

```bash
uv run python -c "import json;d=json.load(open('my-loadtest.json'));[print(l['concurrency'], round(l['output_tokens']/l['requests'],1), 'of', d['max_tokens']) for l in d['levels']]"
```

기준 회차의 `blue` 로 돌리면 5개 레벨 전부 `121.8 of 128` 입니다 —
**상한의 95%.** 이 값이 상한에 붙어 있으면 그 회차의 평균 토큰 수는 모델의 길이가 아니라
**여러분이 정한 상한**을 재고 있는 것이고, 그대로 두 배포를 비교하면 §3 의 오독이 나옵니다.
프롬프트별 `finish_reason` 은 §4 뷰어의 **max_tokens 사용 카드**가 빨갛게 칠해 줍니다.

**길이를 비교하려면 `--max-tokens` 를 훨씬 크게 잡고 다시 재야 합니다**
([PERFORMANCE §6.2](../PERFORMANCE.md) 가 이 회차의 한계를 그렇게 적어 뒀습니다).

### 3.2 두 배포가 생기면 — [Lab 8](lab8.md) 이후

green 이 blue 옆에 올라온 뒤에야 돌아갑니다:

```bash
uv run python scripts/compare_deployments.py \
   --base-url "${BASE%/chat/completions}" \
   --deployment blue --deployment green --max-tokens 128
```

같은 프롬프트를 두 배포에 보내고 `finish_reason` 과 토큰 수를 나란히 찍습니다.
위 표(`PERFORMANCE §6.1`)가 정확히 이 명령의 출력입니다.

이 스크립트도 `--api-key` 기본값이 `$FFSFT_ENDPOINT_KEY` 입니다
(`scripts/compare_deployments.py`). 안 실려 있으면
`no key: pass --api-key or set FFSFT_ENDPOINT_KEY` 를 찍고 **exit 2** 로 끝납니다 —
**§1.1 을 다시 돌리세요.** 새 셸이면 변수가 없고, Lab 8 을 거쳐서 여기 왔으면 셸이
바뀌었을 가능성이 큽니다. `BASE` 도 §1.2 의 그 변수이므로 새 셸이면 같이 다시 잡아야 합니다.

### 3.3 원가로 환산하면 — **한 배포 안에서**

`blue` 하나의 lineage 입니다 ([PERFORMANCE §7](../PERFORMANCE.md)):

| blue | tok/s | 100만 출력 토큰당 |
|---|---:|---:|
| knee (c=16) | 204.29 | **$6.74** |
| 동시성 1 | 22.04 | **$62.50** |

**9.3배.** 같은 GPU, 같은 시간당 요금($4.959/시), 다른 건 배칭뿐입니다.
**배칭이 원가의 대부분입니다.**

> 여기에 green 의 $7.29 를 끌어오면 안 됩니다. 그건 다른 배포의 값이고, 그 차이는
> 배칭이 아니라 §3 의 **응답 길이**입니다. **비교는 한 lineage 안에서** 하세요 — 그게
> 이 절 전체가 말하는 것과 같은 규칙입니다.

## 4. 토큰 뷰어 — 정성 평가

```bash
uv run bash scripts/run_token_viewer.sh ffsft-lab       # 기본 127.0.0.1:8112
```

브라우저에서 `http://127.0.0.1:8112`.

- 키는 이 프로세스의 환경변수로만 들어가고 **디스크에 안 씁니다.**
  브라우저도 키를 못 봅니다 — 페이지와 프록시가 같은 오리진이라 Authorization 헤더가
  서버 사이드에서 붙습니다. 뷰어는 `127.0.0.1` 에만 바인딩합니다.
- **이 스크립트는 키를 자기가 직접 가져옵니다** (`scripts/run_token_viewer.sh`). 그러니
  **뷰어가 떴다는 것이 §1.1 의 변수가 실려 있다는 증거가 아닙니다.** 뷰어는 되는데
  `ffsft loadtest` 가 401 이면 그게 바로 이 경우입니다.

뷰어가 보여주는 것:

| 요소 | 왜 있나 |
|---|---|
| **두 철자 모두 읽기** | `d.reasoning ?? d.reasoning_content`. `\|\|` 가 아니라 `??` 인 이유는 **빈 문자열 델타도 델타**이기 때문 |
| **route 선택** | `/chat/completions`(파서 통과) vs `/completions`(파서 우회, 원문) |
| **deployment 선택** | `azureml-model-deployment` 헤더 — 트래픽 분배를 안 건드리고 blue/green 정성 비교 |
| **max_tokens 사용 카드** | 청크 수가 아니라 `usage.completion_tokens`. **청크는 토큰이 아닙니다.** `finish_reason=length` 면 빨갛게 칠합니다 |

## 5. 사고 토큰 — 필드 이름이 `reasoning` 이다

아래는 **기준 회차의 기록을 읽는 부분**입니다 (§68.1) — 엔드포인트 `ffsft-plc` 의
`green` 배포, thinking ON, `max_tokens=6000`. 이 Lab 의 `ffsft-lab/blue` 가 아니고,
Track B 는 `green` 을 만들지도 않습니다 ([Lab 8](lab8.md) 이 만듭니다).
**지금 재는 게 아니라 읽는 것입니다.** SSE 를 파싱하지 말고 그대로 받아 델타 키를 세면:

```
SSE 프레임 수: 4921
delta 키별 등장 횟수: {'role': 1, 'reasoning': 4920}
```

**`reasoning_content` 는 단 한 프레임도 없습니다.**

여러분의 배포에서 지금 확인하고 싶으면 §4 의 뷰어가 같은 것을 보여줍니다 — 뷰어는
`d.reasoning ?? d.reasoning_content` 로 **두 철자를 다 읽기** 때문에, 어느 쪽으로 오든
화면에는 뜨고 어느 쪽인지도 구분됩니다.

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
| 내 숫자가 표와 다름 | [PERFORMANCE](../PERFORMANCE.md) — ±20% 는 정상, 그 밖이면 여기 |
| tok/s 가 기대보다 낮음 | 짧은 것과 느린 것은 다릅니다 — 배포가 하나면 §3.1, 둘이면 §3.2 |
| `compare_deployments.py` 가 exit 2 | `name at least two deployments` — green 은 [Lab 8](lab8.md) 이 만듭니다 (§3.2) |
| 손으로 만든 프롬프트로 판정하고 싶을 때 | §68.4 — `POST /tokenize` 가 정답을 알려줍니다 |
| 로드테스트가 401 | `$FFSFT_ENDPOINT_KEY` 가 비었습니다 — §1.1. 뷰어가 뜨는 것은 증거가 아닙니다 |
| `my-loadtest.json` 이 없다고 나옴 | §1.2 에서 `--output` 을 빼고 돌렸습니다. 다시 돌리세요 |
| 키 조회가 404 | 프로필이 안 실린 셸입니다 — 「시작 전」 의 `source ~/.ffsft-env` |
| `az ml online-endpoint show` 가 워크스페이스를 못 찾음 | `-g`/`-w` 를 빠뜨렸습니다 — §1.2 |

## 정리 — 반드시

**프로필이 실린 셸에서** 돌립니다 (「시작 전」의 `source ~/.ffsft-env`). 변수가 없는
셸이면 이 명령은 엉뚱한 워크스페이스를 조회하고, 그 `BILLING NOW: nothing` 은 여기
엔드포인트를 **가려줍니다**:

```bash
uv run ffsft lifecycle status          # 지금 시간당 얼마인가
```

**여기가 갈림길입니다. 순서를 지키세요:**

| 하려는 것 | 다음 | blue 는 |
|---|---|---|
| 파인튜닝한 가중치까지 서빙 — §3.2 의 두 배포 비교 포함 | [Lab 8](lab8.md) | **살려 둡니다.** Lab 8 이 blue 옆에 green 을 올립니다 |
| 여기서 끝 | [Lab 7](lab7.md) | 내립니다 |

Lab 8 을 할 건데 여기서 내리면 **Lab 8 이 필요로 하는 blue 가 없어집니다.**
전체 순서는 **0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → 7**.

**어느 쪽이든 마지막은 [Lab 7 — 내리기](lab7.md) 입니다. 건너뛰지 마세요.**
