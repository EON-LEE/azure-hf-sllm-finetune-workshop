# Lab 1 — 데이터 준비 (Fabric)

> **Track A · 선행: [Lab 0](lab0.md)** · 새 셸이면 `source ~/.ffsft-env` 부터

## 목표

- 한국어 인스트럭션 코퍼스를 필터·중복제거해서 **학습이 바로 먹는 JSONL** 로 만든다
- 그 필터 로직이 **왜 Spark 밖에 있는지** 이해한다

## 선행조건

- Lab 0 완료
- Microsoft Fabric 워크스페이스 + Lakehouse
  (자동화용 서비스 주체는 워크스페이스 **Viewer** 롤 필요)

## 소요·비용

**40분 / GPU 과금 없음.** Fabric Spark 는 CPU 풀입니다.

> 💡 **왜 학습 잡 안에서 안 하나.** 코퍼스 필터링은 pandas 일입니다. 학습 스크립트
> 안에서 하면 시간당 **$4.96 짜리 `NC24ads_A100_v4`** 가 그 일을 하고,
> 재시작할 때마다·하이퍼파라미터 스윕할 때마다 다시 합니다.

---

## 1. 로컬에서 먼저 — Spark 없이

필터 로직은 전부 `ffsft.data.fabric_prep` 의 **순수 함수**입니다. Spark import 없이 돕니다.

```bash
uv run pytest tests/test_fabric_prep.py -q
uv run python -c "
from ffsft.data.fabric_prep import hangul_ratio, normalize_text, DEFAULT_MIN_HANGUL_RATIO
print('한글비율      :', hangul_ratio('서울은 한국의 수도입니다'))
print('한글비율(혼합):', hangul_ratio('Azure ML 로 파인튜닝합니다'))
print('한글비율(영문):', hangul_ratio('Seoul is the capital'))
print('컷오프        :', DEFAULT_MIN_HANGUL_RATIO)
print('NFC 정규화    :', normalize_text('한국') == normalize_text('한국'))
"
```

기대 출력:

```
한글비율      : 1.0
한글비율(혼합): 0.5333333333333333
한글비율(영문): 0.0
컷오프        : 0.3
NFC 정규화    : True
```

> **`1.0` 은 오타가 아닙니다.** 분모가 글자 수가 아니라 **letter 수**입니다 — 공백·
> 문장부호·숫자는 세지 않습니다. 그래서 순한국어 문장은 0.9x 가 아니라 정확히 `1.0`
> 이 나옵니다. 컷오프 `0.3` 은 한국어 **순도**를 재라는 값이 아니라,
> `Azure ML 로 파인튜닝합니다`(0.53) 같은 정상적인 기술 한국어를 살리면서 영어
> 문서(0.0)를 떨어뜨리라는 값입니다. 판정은 instruction 과 output 을 **합쳐서**
> 합니다 (`quality_reasons` → `not_korean`).

> ⚠️ **NFC 정규화가 없으면 중복제거가 조용히 아무것도 안 합니다.**
> NFD 로 쓰인 `한국` 과 NFC 로 쓰인 `한국` 은 **화면에 똑같이 보이고 해시가 다릅니다.**
> 한국어 데이터에서는 예외가 아니라 기본값입니다.

## 2. 믹스 고르기

```bash
uv run python -c "
import yaml; d=yaml.safe_load(open('configs/datasets.yaml'))
for k,v in d['mixes'].items():
    print(f'{k:22s} {len(v[\"datasets\"])}개  {v[\"description\"].strip()[:60]}')
"
```

| 믹스 | 용도 |
|---|---|
| `ko_smoke` | 파이프라인 연기 테스트. 빠르고 쌉니다. **Lab 2 는 이걸로 시작하세요** |
| `ko_commercial_safe` | MIT / Apache-2.0 / CC-BY-SA 만. **상용 데모에 쓸 수 있는 것** |
| `ko_broad` | 더 크고, 문체·교과서형 데이터 추가 |

> ⚠️ **한국어 데이터셋 상당수가 NC/ND 라 상용 학습에 못 씁니다.**
> `configs/datasets.yaml` 의 `license` / `commercial_use` 를 반드시 보세요.
> 이 판별을 매번 다시 하지 않으려고 YAML 에 고정해 둔 것입니다.

## 3. Fabric 에서 실행

`notebooks/fabric/01_prepare_korean_corpus.py` 를 Fabric 노트북으로 가져가 실행합니다.
노트북에는 **로직이 없습니다** — 전부 `ffsft.data.fabric_prep` 호출입니다.

출력 계약: **한 줄에 JSON 하나**, `{"messages": [...]}` —
`trl` 의 `SFTTrainer` 가 그대로 먹는 형식입니다.

> 렌더링된 프롬프트가 아니라 `messages` 를 내보내는 이유: chat template 을
> **모델의 속성**으로 남겨두려는 것입니다. 모델을 바꿔도 데이터를 다시 안 만듭니다.

첫 셀(PARAMETERS)에서 건드릴 것:

| 파라미터 | 기본값 | 언제 |
|---|---|---|
| `HF_DATASETS` | `nlpai-lab/kullm-v2` | 소스 교체. `SOURCE_TABLE` 을 채우면 Lakehouse Delta 를 대신 읽습니다 |
| `MAX_ROWS` | `0` (제한 없음) | **처음엔 작게.** 전량을 돌리기 전에 파이프라인만 확인 |
| `MIN_HANGUL_RATIO` | `0.3` | 위 1번의 컷오프 |
| `OUTPUT_PATH` / `REJECTED_PATH` | `Files/ffsft/ko_sft` / `Files/ffsft/ko_rejected` | 바꾸면 4번의 등록 경로도 같이 바꾸세요 |

찍히는 순서 (숫자는 소스·믹스마다 다릅니다):
`loaded … rows` → `rejected … of …` → `rejection reasons:` 표 →
`kept … after dedup (… duplicates)`.

`quality_reasons` 는 boolean 이 아니라 **떨어진 이유 전부**를 돌려줍니다. 그래서
행의 80% 가 날아가도 추측이 아니라 표로 진단됩니다:

| `reason` | 뜻 |
|---|---|
| `not_korean` | instruction+output 을 합친 한글비율 < `MIN_HANGUL_RATIO` |
| `output_too_short` | 정규화 후 output 이 `MIN_OUTPUT_CHARS` 미만 |
| `output_echoes_instruction` | output 이 instruction 을 그대로 되돌려줍니다. 학습하면 프롬프트를 따라 읽는 모델이 됩니다 |
| `repetitive` | output 반복 비율 > 0.5 |
| `empty_instruction` / `empty_output` | 빈 칸 |

## 4. OneLake 에 남는 것 — 파일 하나가 아니라 **디렉터리 둘**

Spark writer 의 산출물입니다. `train.jsonl` 같은 파일은 만들어지지 않습니다.

```
OneLake / <workspace> / <lakehouse> / Files/ ffsft/ ko_sft/        <- 디렉터리
                                                      _SUCCESS
                                                      part-00000-…   한 줄에 JSON 하나
                                             ffsft/ ko_rejected/   <- 디렉터리
                                                      part-…        떨어진 행 + reason
```

- `ko_sft` 는 `.coalesce(1)` 이라 part 파일이 **하나**입니다. 학습 잡은 JSONL 하나만
  읽으면 되고, 채팅 행 수십만 개는 분산 읽기 문제가 아니라 수십 MB 이기 때문입니다.
- 확장자가 `.jsonl` 이 아닙니다. `write.text()` 의 산출물이고 **내용**이 한 줄에 JSON
  하나입니다. 계약은 이름이 아니라 내용입니다.
- `ko_rejected` 는 coalesce 하지 않아 part 가 여러 개입니다. 진단용이지 학습용이 아닙니다.

Azure ML 에는 파일이 아니라 **폴더**로 등록합니다 (노트북 마지막 셀):

```bash
az ml data create --name ko-sft --version 1 \
  --path abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lakehouse>/Files/ffsft/ko_sft \
  --type uri_folder
```

> ⚠️ **`OneLakeDatastore` 는 `Files/` 만 지원하고 `Tables/`(Delta) 는 못 읽습니다.**
> Spark 에서 JSONL 로 내보내는 이 단계가 선택이 아니라 필수인 이유입니다.
>
> 인증 스코프도 다릅니다 — Foundry/AML 은 `https://ai.azure.com/.default`,
> **OneLake 는 `https://storage.azure.com/.default`** (Storage 오디언스).

> ⚠️ **여기서 등록한 자산을 Lab 2 는 아직 안 씁니다.** `ffsft train submit --help` 를
> 보면 데이터 관련 플래그는 `--mix` 하나뿐이고, 잡은 `configs/datasets.yaml` 의 믹스를
> **잡 안에서 HF 로부터 직접** 받습니다. `azureml:ko-sft:1` 을 가리킬 입력 플래그는
> 현재 없습니다. 이 Lab 의 산출물은 운영 경로용이고, 워크샵의 Lab 2 는 믹스 경로로
> 갑니다 — 없는 플래그를 적어두면 Lab 2 에서 막히므로 그대로 적어둡니다.

---

## 기대 최종 상태

- `Files/ffsft/ko_sft/` 에 part 파일 하나 + `_SUCCESS`, 각 줄이 `{"messages":[...]}`
- `Files/ffsft/ko_rejected/` 에 떨어진 행들 — `reason` 분포가 납득이 되는지 보세요
- 노트북 마지막 두 줄이 `wrote … rows to Files/ffsft/ko_sft` /
  `wrote … rejects to Files/ffsft/ko_rejected`
- 중복제거 후 건수가 원본보다 **눈에 띄게 줄었다** (안 줄었으면 NFC 를 의심하세요)

## 막히면

| 증상 | 볼 곳 |
|---|---|
| 중복제거가 아무것도 안 지운다 | NFC 정규화 — 위 1번 |
| `train.jsonl` 이 안 보인다 | 그런 파일은 안 만듭니다. `ko_sft/` 디렉터리입니다 — 위 4번 |
| 한글비율이 1.0 이라 이상하다 | 분모가 letter 수입니다 — 위 1번 |
| `OneLakeDatastore` 가 테이블을 못 읽는다 | 위 4번. `Files/` 만 됩니다 |
| 토큰 스코프 오류 | OneLake 는 `storage.azure.com` 오디언스 |

## 정리

Fabric 은 CPU 풀이라 유휴 시 GPU 과금이 없습니다. 세션만 닫으세요.

**다음**: [Lab 2 — QLoRA 학습](lab2.md)
