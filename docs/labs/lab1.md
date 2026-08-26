# Lab 1 — 데이터 준비 (Fabric)

> **Track A · 선행: [Lab 0](lab0.md)**

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
from ffsft.data.fabric_prep import hangul_ratio, normalize_text, dedup_key, quality_reasons
print('한글비율      :', hangul_ratio('서울은 한국의 수도입니다'))
print('한글비율(영문):', hangul_ratio('Seoul is the capital'))
print('NFC 정규화    :', normalize_text('한국') == normalize_text('한국'))
"
```

기대 출력:

```
한글비율      : 0.75 근처
한글비율(영문): 0.0
NFC 정규화    : True
```

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

## 4. OneLake 로 내보내기

```
OneLake / <workspace> / <lakehouse> / Files/ ffsft/ train.jsonl
                                                    val.jsonl
                                                    test.jsonl
```

> ⚠️ **`OneLakeDatastore` 는 `Files/` 만 지원하고 `Tables/`(Delta) 는 못 읽습니다.**
> Spark 에서 JSONL 로 내보내는 이 단계가 선택이 아니라 필수인 이유입니다.
>
> 인증 스코프도 다릅니다 — Foundry/AML 은 `https://ai.azure.com/.default`,
> **OneLake 는 `https://storage.azure.com/.default`** (Storage 오디언스).

---

## 기대 최종 상태

- `Files/ffsft/train.jsonl` 에 `{"messages":[...]}` 줄들
- 중복제거 후 건수가 원본보다 **눈에 띄게 줄었다** (안 줄었으면 NFC 를 의심하세요)

## 막히면

| 증상 | 볼 곳 |
|---|---|
| 중복제거가 아무것도 안 지운다 | NFC 정규화 — 위 1번 |
| `OneLakeDatastore` 가 테이블을 못 읽는다 | 위 4번. `Files/` 만 됩니다 |
| 토큰 스코프 오류 | OneLake 는 `storage.azure.com` 오디언스 |

## 정리

Fabric 은 CPU 풀이라 유휴 시 GPU 과금이 없습니다. 세션만 닫으세요.

**다음**: [Lab 2 — QLoRA 학습](lab2.md)
