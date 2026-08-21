# fabric-foundry-sllm-finetune

**Microsoft Fabric + Azure AI Foundry 를 결합한 한국어 sLLM 파인튜닝 코드 에셋.**

데이터 준비는 Fabric(Spark/OneLake), GPU 학습·배포는 Foundry/Azure ML,
학습 루프는 Hugging Face(`transformers` / `trl` / `peft`) 로 구성됩니다.
**모델은 코드 수정 없이 YAML 설정만으로 교체**할 수 있습니다.

기본 타깃 모델: **`Qwen/Qwen3.8-27B`** (Apache-2.0)

> 상세 설계·리서치 근거는 [`docs/PLAN.md`](docs/PLAN.md) 를 참고하세요.
> 모델/데이터셋 후보 비교, 라이선스 검증, 학습 경로 A/B/C/D, 리스크가 모두 정리되어 있습니다.

---

## 왜 이 에셋이 필요한가

Fabric 과 Foundry 를 **파인튜닝** 관점에서 묶은 공식 Microsoft 샘플이 없습니다.
그래서 실무에서 매번 다음 세 가지를 다시 만듭니다.

1. Fabric Lakehouse(Delta) → 학습용 JSONL 로 내보내는 경로
2. 한국어 데이터셋의 **라이선스 판별** (상당수가 NC/ND 라 상용 학습에 쓸 수 없음)
3. 한국어 벤치마크 평가 파이프라인

이 리포는 이 세 가지를 **설정 파일로 고정**하고, 모델만 갈아끼우면 되게 만듭니다.

### 역할 분담 (중요)

**Fabric Spark 는 CPU 전용이라 GPU 학습이 불가능합니다.** 따라서 이렇게 나눕니다.

```mermaid
flowchart LR
    subgraph FABRIC["Microsoft Fabric (CPU)"]
        A[Lakehouse Delta Tables] --> B[Spark 전처리<br/>중복제거·PII·품질필터]
        B --> C[chat template 적용<br/>train/val/test 분할]
        C --> D[(OneLake<br/>Files/ JSONL)]
    end
    subgraph FOUNDRY["Azure AI Foundry / Azure ML (GPU)"]
        D --> E[OneLakeDatastore]
        E --> F[QLoRA SFT<br/>trl + peft]
        F --> G[한국어 벤치마크 평가]
        G --> H[vLLM Managed Endpoint]
    end
```

> `OneLakeDatastore` (AML SDK v2, Preview) 는 **`Files/` 폴더만** 지원하고
> `Tables/`(Delta) 는 읽지 못합니다. 그래서 Spark 에서 JSONL 로 내보내는 단계가 필수입니다.

---

## 빠른 시작

`uv` 로 관리합니다. (`export PATH="$HOME/.local/bin:$PATH"`)

```bash
uv sync --extra dev          # 개발 의존성 (Azure/GPU 없이 동작)
uv run pytest                # 전체 테스트 (네트워크·과금 없음)
uv run ffsft models list     # 교체 가능한 모델 목록
```

GPU 학습 의존성은 무겁기 때문에 별도 extra 로 분리했습니다.

```bash
uv sync --extra train        # transformers>=5.8, trl, peft, torch, bitsandbytes
uv sync --extra azure        # azure-ai-ml, azure-identity, mlflow
uv sync --extra data         # datasets, huggingface-hub
```

---

## 모델 교체 방법

모든 학습/평가 코드는 모델 ID 를 하드코딩하지 않고 **레지스트리에서 `ModelSpec` 을 조회**합니다.

```bash
uv run ffsft models list                        # 전체
uv run ffsft models list --max-params-b 5       # 5B 이하만
uv run ffsft models list --commercial-only      # 상용 가능 라이선스만
uv run ffsft models list --provider hf
uv run ffsft models show qwen3.8-27b            # 단일 모델 상세
uv run ffsft models trainable                   # 파인튜닝 가능 여부 + 불가 사유
```

새 모델을 추가하려면 [`configs/models.yaml`](configs/models.yaml) 에 항목만 추가하면 됩니다.
**Python 코드 수정은 필요 없습니다.**

```yaml
- key: my-model
  display_name: My Model
  provider: hf                 # hf | foundry_managed | foundry_serverless
                               # | azure_openai | inference_only
  hf_id: org/my-model
  supports: [qlora, lora, full]   # 첫 항목이 기본 레시피
  params_b: 7.0
  license: apache-2.0
  commercial_use: true
  korean_tier: strong             # native | strong | moderate | weak | unknown
  vram_gb: { qlora: 12, lora: 24, full: 120 }
  recommended_sku: Standard_NC24ads_A100_v4
```

다른 레지스트리 파일로 통째로 교체할 수도 있습니다.

```bash
FFSFT_MODEL_REGISTRY=/path/to/my_models.yaml uv run ffsft models list
```

### 현재 등록된 모델 (21개)

| 계열 | 키 | 크기 | 비고 |
|---|---|---|---|
| **Qwen (기본)** | `qwen3.8-27b` ⭐ | 26.9B | Apache-2.0, 기본 타깃 |
| | `qwen3.8-27b-fp8` | 26.9B | FP8 양자화 |
| | `qwen3.6-27b` | 26.9B | 폴백 후보 |
| | `qwen3.5-9b` / `-4b` / `-2b` / `-0.8b` | 0.9~9.7B | 가장 최신 **소형** 티어 |
| **한국어 네이티브** | `midm-2.0-mini` | 2.3B | KT, **MIT** (가장 안전) |
| | `midm-2.0-base` | 11.5B | KT, MIT |
| | `kanana2-3b` / `-1.3b` | 1.3~3.5B | 카카오 |
| | `hyperclovax-seed-1.5b` | 1.6B | 네이버 |
| | `exaone-4.0-1.2b` | 1.3B | LG, 비상용 |
| **Microsoft** | `phi4-mini` | 3.8B | MIT, Foundry 서버리스 튜닝 가능 |
| **Foundry 서버리스** | `foundry-qwen-32b` 외 | - | JSONL 제출형 (블랙박스) |
| **MAI** | `mai-thinking-1`, `mai-ds-r1` | - | **파인튜닝 불가** (아래 참조) |

---

## MAI 모델 검토 결과 (결론: 파인튜닝 불가)

요청하신 Microsoft MAI 자체 모델을 조사한 결과입니다.

- `microsoft/MAI-DS-R1` 이 **유일한 오픈 웨이트** MAI 모델이지만 **671B** (DeepSeek-R1 포스트트레이닝) 로 sLLM 범위를 완전히 벗어납니다.
- `MAI-Thinking-1`, `MAI-Code-1`, `MAI-Image`, `MAI-Voice`, `MAI-Transcribe` 는 모두 **API 전용 독점 모델**로 웨이트가 공개되지 않습니다.
- "Frontier Tuning"(RL 기반 커스터마이징)은 선별 프리뷰이며 Hugging Face 기반이 아닙니다.
- 공개된 **한국어 벤치마크 수치가 없습니다.**

그래서 삭제하지 않고 `provider: inference_only` 로 **레지스트리에 남겨** 두었습니다.
검토 결과가 코드에 근거로 남고, LLM-as-judge / 합성 데이터 생성 용도로는 활용할 수 있습니다.

```bash
uv run ffsft models trainable   # 파인튜닝 불가 모델과 그 사유를 출력
```

> ⚠️ Hugging Face 의 `Tongyi-MAI/*` 는 **알리바바** 조직이며 Microsoft MAI 와 무관합니다.

---

## 한국어 데이터셋 · 벤치마크

라이선스가 이 도메인의 가장 큰 함정입니다. 모든 항목에 `license` / `commercial_use` 를 명시했습니다.

- [`configs/datasets.yaml`](configs/datasets.yaml) — 학습용. 기본 믹스 `ko_commercial_safe` 는 **MIT / Apache-2.0 만** 포함합니다. NC 데이터는 명시적 opt-in 이어야 합니다.
- [`configs/benchmarks.yaml`](configs/benchmarks.yaml) — 평가용. KMMLU, HAE-RAE, IFEval-Ko, LogicKor 등 10종. 기본 스위트는 `ko_core`.

**모든 벤치마크는 `eval_only: true` 로 강제**됩니다. 한국어 벤치마크 상당수가 CC-BY-**ND** / **NC** 라
학습에 쓰면 라이선스 위반인 동시에 테스트셋 오염이 됩니다. 이 불변식은 테스트로 검증합니다.

```python
def test_no_benchmark_id_appears_in_the_training_datasets(): ...
def test_default_mix_contains_only_commercially_safe_datasets(): ...
```

---

## ID 검증

리서치로 얻은 모델/데이터셋 ID 는 **실제로 틀린 경우가 많습니다.**
(예: `Qwen/Qwen3.5-4B-Instruct` 는 존재하지 않음 — 실제 ID 에는 `-Instruct` 접미사가 없습니다.)

그래서 설정 파일의 모든 ID 를 Hugging Face API 로 직접 검증하는 스크립트를 포함했습니다.

```bash
uv run python scripts/verify_hf_ids.py                      # 전체 검증
uv run python scripts/verify_hf_ids.py --models             # 모델만
uv run python scripts/verify_hf_ids.py --spec Qwen/Qwen3.8-27B   # 실제 아키텍처 확인
```

일부 항목(`HAERAE-HUB/qarv-instruct-ko`, `HAERAE-HUB/CLIcK`)은 **게이트 리포**라
접근 권한을 신청해야 하며, 401 은 정상입니다.

---

## Qwen3.8-27B 사용 시 반드시 알아야 할 점

실제 `config.json` 을 확인한 결과, 이름만 보고 넘기면 실패하는 요소가 3가지 있습니다.

1. **멀티모달 체크포인트입니다.** `Qwen3_5ForConditionalGeneration` 이며 vision/video 토큰을 가집니다.
   한국어 텍스트 전용 SFT 에서는 `language_model_only: true` 로 비전 타워를 로드/학습하지 않아야 합니다.
2. **하이브리드 아키텍처입니다.** 64 레이어 중 4번째마다 full attention, 나머지는 linear/SSM 계열입니다.
   → **QLoRA(bitsandbytes 4-bit)가 이 레이어들과 호환되는지는 아직 검증되지 않았고, 본 프로젝트 최대 리스크입니다.**
   실패 시 폴백은 `qwen3.6-27b` 또는 `qwen3.5-9b` 입니다.
3. **`transformers>=5.8` 이 필수**입니다. (`transformers_version: 5.8.0.dev0`)

또한 vocab 이 **248,320** 토큰(Qwen3 는 152K)이라 한글 토크나이징 효율이 크게 개선됩니다.

### VRAM 추정치 (26.9B 기준)

| 방식 | 추정 peak VRAM | 권장 SKU |
|---|---|---|
| QLoRA 4-bit ⭐ | ~26 GB | `Standard_NC24ads_A100_v4` (1×A100 80GB) |
| BF16 LoRA | ~76 GB | 2×80GB 권장 (여유 없음) |
| Full FT | ~460 GB | `Standard_ND96isr_H100_v5` (8×H100) + ZeRO-3 |

> 계산 추정치이며 실측값이 아닙니다.
> **A100/H100 쿼터는 기본 0인 경우가 많으니 몇 주 전에 증설 신청하세요.**

---

## 프로젝트 구조

```
configs/            # 교체 지점: 모델·데이터셋·벤치마크 레지스트리
  models.yaml
  datasets.yaml
  benchmarks.yaml
src/ffsft/
  models/           # ModelSpec + 레지스트리
  data/             # 데이터 로딩 · 라이선스 게이팅 · fabric_prep
  fabric/           # OneLake / Lakehouse 연동
  train/            # qlora.py (QLoRA) · aml.py (Azure ML 잡 제출)
  eval/             # run.py (벤치마크) · judge.py (LLM-as-judge)
  deploy/           # spec / endpoint / lifecycle (올리기·내리기)
  loadtest/         # 스트리밍 부하 테스트 (TTFT · TPOT · knee)
  cli.py            # ffsft CLI
docker/
  Dockerfile.train  # ACPT 기반 학습 이미지
  Dockerfile.serve  # vLLM 기반 서빙 이미지
  verify_serve.py   # 빌드 타임 게이트 (아키텍처·플래그 검증)
notebooks/fabric/   # Fabric Spark 데이터 준비 노트북 (subtree 루트)
scripts/            # verify_hf_ids.py 등 유틸
docs/
  PLAN.md           # 전체 설계 문서 (리서치 근거 포함)
  SERVING.md        # 서빙 패턴 · 쿼터 규칙 · 실측 제약
  VERIFIED.md       # 실제로 실행해서 확인된 것만 기록
tests/              # 167+ 테스트 (Azure 호출 없음, 무과금)
```

---

## 엔드포인트 올리기 / 내리기

> **⚠️ 온라인 배포 전에 엔드포인트 ID 권한부터 확인하세요.**
> 이 워크스페이스는 **연결된 ACR 이 없습니다**(`properties.containerRegistry` 가 빈 값).
> 그래서 `acrffsftkc` 는 고객 소유 레지스트리이고, **온라인 엔드포인트의 시스템 할당
> 관리 ID 에 `AcrPull` 이 자동으로 부여되지 않습니다.** 권한 없이 배포하면 컨테이너가
> 이미지를 못 받아 시작조차 못 하고, 롤아웃이 **한 시간 넘게 `Creating`** 에 머물다
> **로그 한 줄 없이** `InternalServerError` 로 죽습니다. 이 함정에 두 번 당했습니다
> (약 $8). 워크스페이스 ID 에 권한이 있다는 사실은 **엔드포인트 ID 와 무관합니다** —
> 서로 다른 principal 입니다. 전체 진단은 `docs/VERIFIED.md` §0.
>
> 그래서 `ffsft-lifecycle up` 은 **배포 전에 엔드포인트 ID 의 실제 role 을 읽어
> 검사하고 거부**합니다(`src/ffsft/deploy/identity.py`). 한 시간을 기다린 뒤 실패하는
> 대신 **2초 만에** 그대로 붙여 넣을 수 있는 `az role assignment create` 명령을 줍니다.

Managed Online Endpoint 는 **scale-to-zero 가 없어서 idle 상태에서도 24시간 과금**됩니다.
실험이 끝나면 반드시 내려야 하고, 나중에 같은 명령으로 다시 올릴 수 있습니다.

```bash
# 이 CLI 는 azure 엑스트라와 환경변수 3개가 있어야 동작합니다
uv sync --extra dev --extra azure
export FFSFT_SUBSCRIPTION_ID=...
export FFSFT_RESOURCE_GROUP=rg-ffsft-kc
export FFSFT_WORKSPACE=mlw-ffsft

ffsft-lifecycle status              # 지금 과금 중인 리소스 + 시간당/월 비용
ffsft-lifecycle up   --endpoint ffsft-smoke --model qwen3.8-27b
ffsft-lifecycle up   --endpoint ffsft-smoke --hf-model Qwen/Qwen3-0.6B
ffsft-lifecycle down --endpoint ffsft-smoke --yes
ffsft-lifecycle down --all --yes    # 과금되는 것 전부 정리
```

`--model` 은 `configs/models.yaml` 의 키다. 이걸 주면 해당 모델의
**아키텍처 플래그**(`--mamba-cache-mode` / `--language-model-only` /
`--reasoning-parser`)와 **프로브 예산**이 자동으로 결정된다.
레지스트리에 없는 `--hf-model` 이라도 repo id 에서 크기를 되찾아 쓰고
(`Qwen/Qwen3-0.6B` → 0.6B), 정 안 되면 `--params-b` 로 직접 줄 수 있다.
플래그를 이미지에 굽지 않는 이유는 `docs/VERIFIED.md` 5.2 참고.

`down` 은 **엔드포인트만 삭제**하고 **컴퓨트 클러스터는 0 노드로 스케일 다운**합니다.
클러스터 정의는 유지 비용이 없고 재생성에 수 분이 걸리므로 남겨 두는 편이 낫습니다.
엔드포인트 삭제는 **20~40분** 걸리니 `status` 로 `BILLING NOW: nothing` 을 확인하세요.

### `status` 는 삭제된 VM의 잔해도 찾습니다

VM 을 지워도 **OS 디스크와 공인 IP 는 남아서 계속 과금**됩니다. 이 프로젝트에서
실제로 스팟 A10 VM 의 잔해가 **$41.66/월**(256 GB Premium 디스크 $38.01 +
Standard 공인 IP $3.65) 을 축내고 있었고, 워크스페이스 클라이언트로는 보이지
않아 테어다운이 매번 놓쳤습니다. 이제 `status` 가 리소스 그룹을 직접 훑습니다.

`down` 은 이것들을 **건드리지 않고 삭제 명령만 출력**합니다 — 디스크 삭제는
되돌릴 수 없고 `up` 이 복구해주지도 않으니 사람이 판단할 일입니다.
자세한 내용과 함정(붙어 있는 것처럼 보이는 공인 IP)은 `docs/VERIFIED.md` §11.

## 서브트리 워크플로

Fabric 노트북은 Fabric 워크스페이스에 따로 연결해야 하므로
`notebooks/fabric` 을 별도 리포로 분리해 `git subtree` 로 동기화합니다.

```bash
# 최초 1회
git remote add fabric https://github.com/EON-LEE/ffsft-fabric.git

# 이 리포 → 위성 리포 (Fabric 워크스페이스가 당겨감)
git subtree push --prefix=notebooks/fabric fabric main

# 위성 리포 → 이 리포 (Fabric 에서 편집한 내용 회수)
git subtree pull --prefix=notebooks/fabric fabric main --squash
```

`notebooks/fabric` 안의 코드는 **로직을 직접 담지 않고** `src/ffsft/data/fabric_prep.py`
를 호출하는 얇은 래퍼입니다. 그래야 순수 함수 부분을 Spark 없이 테스트할 수 있습니다.

## 현재 상태

- [x] 리서치 (Qwen 계열 / 한국어 데이터셋·벤치마크 / Fabric·Foundry / MAI)
- [x] 모든 HF ID 실제 API 검증
- [x] 모델 추상화 레이어 + 레지스트리 + CLI
- [x] 3종 설정 레지스트리 (모델·데이터셋·벤치마크)
- [x] 설계 문서 `docs/PLAN.md`
- [x] Fabric Spark 데이터 준비 노트북 + `fabric_prep` 순수 함수 (TDD)
- [x] 학습 경로: ACPT 커스텀 이미지 빌드 · A100 프리플라이트 통과
- [x] 서빙 경로: vLLM 이미지 빌드 · 아키텍처 등록 검증
- [x] 평가: 벤치마크 러너 + LLM-as-judge (TDD)
- [x] 부하 테스트: TTFT / TPOT / knee 측정기 — **모의 서버로 측정 정확도 실검증**
- [x] 라이프사이클: `up` / `down` / `status` — **실제 테어다운 검증 완료**
- [x] 비용 누수 탐지: 삭제된 VM 잔해 스캔 (TDD) — **실제 $41.66/월 발견·제거**
- [x] 배포 프리플라이트: 엔드포인트 ID 권한 부족 시 2초 만에 거부 (TDD)
- [x] **학습 경로 실검증** — A100 LowPriority 에서 preflight 잡 2회 `Completed`,
      노드 실측 `nf4_matmul_ok: True` / `transformers 5.15.1` / A100 80GB,
      QLoRA 실제 학습 스텝 성공 (`docs/VERIFIED.md` §16)
- [x] **QLoRA 학습 엔드투엔드 `Completed`** — `olive_machine_58qllrq6y9`,
      `train_loss 1.601` / 10 스텝 / 276초 / 학습 파라미터 **1.06%** /
      VRAM 피크 2.79 GB (§19). 성공 런의 stdout 은 블롭 권한 때문에 읽을 수
      없으므로 수치는 전부 `ffsft/train/report.py` → MLflow 로 회수했다.
- [x] 잡 제출 가드: 모델이 `lora_target_modules` 를 선언 안 하면 **GPU 를 빌리기 전에**
      거부 (TDD, `tests/test_aml_job.py`)
- [x] 라이브러리 rename 내성: transformers v5 의 `warmup_ratio` 제거 등을
      런타임에 해석 (TDD, `tests/test_qlora_config.py`, §18)
- [ ] **온라인 배포 ⛔ 5회 전부 실패 — 남은 블로커는 GPU 노드 할당.**
      `AcrPull` 문제는 해결됐고 CPU SKU 에서 이미지 pull 성공을 실증했지만
      (§14), 같은 엔드포인트·같은 이미지로 SKU 만 A10 으로 바꾸면 65분간
      `Creating` 에서 못 나온다(§15). 테넌트 정책이 **전용** N-series 를 막고
      매니지드 엔드포인트에는 저우선순위 옵션이 없다는 것이 유력한 가설이다.
      학습이 되는 이유도 같다 — 학습만 LowPriority 를 쓸 수 있다(§16.1).
- [ ] 27B 실학습 · 튜닝 전후 벤치마크 비교

## 사전 요구사항

- Azure 구독 + **Foundry User / Foundry Owner** 롤
  (2025~26년에 `Azure AI User` → `Foundry User` 로 이름이 변경되었습니다.)
- Azure ML 워크스페이스 + **A100/H100 쿼터**
- Microsoft Fabric 워크스페이스 + Lakehouse
  (자동화용 서비스 주체는 워크스페이스 **Viewer** 롤 필요)
- 인증 스코프: Foundry/AML 은 `https://ai.azure.com/.default`,
  **OneLake 는 `https://storage.azure.com/.default`** (Storage 오디언스)

## 라이선스

MIT (본 리포 코드 기준). 각 모델·데이터셋은 **개별 라이선스를 따릅니다** —
`configs/*.yaml` 의 `license` / `commercial_use` 필드를 반드시 확인하세요.
