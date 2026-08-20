# Fabric + Foundry sLLM Fine-tuning — 계획서

> 조사 기준일 **2026-08-20**. 이 문서의 모든 Hugging Face repo ID / 라이선스는
> HF API로 직접 조회해 검증했다. 검증 스크립트: `scripts/verify_hf_ids.py`

---

## 0. 한 줄 요약

Microsoft Fabric에서 한국어 학습 데이터를 만들고, Hugging Face 스택
(`transformers`/`trl`/`peft`)으로 **Qwen3.8-27B**를 QLoRA 파인튜닝한 뒤,
Azure ML managed compute(= Foundry 관리형 컴퓨트)에서 학습하고 Foundry
엔드포인트로 배포·평가하는, **모델 교체 가능한** 코드 에셋.

> **채택 모델: `Qwen/Qwen3.8-27B`** (2026-08-05, Apache-2.0, 27.8B).
> 소형 모델(0.8B~9B)은 레지스트리에 함께 등록해 스모크 테스트·비교군으로 쓴다.

---

## 1. 모델 조사 결과

### 1.1 "최신 작은 Qwen"의 정답 — Qwen3.5 계열

HF API 조회 결과, 2026-08-20 기준 Qwen 계보는 다음과 같다.

| 세대 | 출시 | 공개된 크기 | 소형(≤10B) 존재? |
| --- | --- | --- | --- |
| Qwen3 | 2025-04 | 0.6B / 1.7B / 4B / 8B / 30B-A3B ... | ✅ |
| **Qwen3.5** | **2026-02** | **0.8B / 2B / 4B / 9B** / 27B / 35B-A3B / 122B-A10B / 397B-A17B | ✅ **최신 소형** |
| Qwen3.6 | 2026-04 | 27B, 35B-A3B **뿐** | ❌ |
| Qwen3.8 | 2026-08 | 27B, 2.4T-A95B **뿐** | ❌ |

> **핵심**: Qwen3.6·Qwen3.8은 27B 이상만 공개됐다. 따라서 **소형 모델에서
> 가장 최신은 Qwen3.5 계열**이고, 이게 사용자가 말한 "요즘 작은데 엄청
> 성능 좋다"에 해당한다.

**검증된 repo ID** (모두 Apache-2.0):

| repo ID | 크기 | 다운로드 | 비고 |
| --- | --- | --- | --- |
| **`Qwen/Qwen3.8-27B`** | **27.8B** | **1.0M** | ⭐ **채택** (2026-08-05, 최신) |
| `Qwen/Qwen3.8-27B-FP8` | 27.8B | 1.1M | FP8, 서빙용 |
| `Qwen/Qwen3.6-27B` | 27.8B | 6.7M | 안정성 폴백 |
| `Qwen/Qwen3.5-9B` | 9.7B | 13.6M | 소형 상위 |
| `Qwen/Qwen3.5-4B` | 4.7B | 7.7M | 단일 GPU 실험용 |
| `Qwen/Qwen3.5-2B` | 2.3B | 3.1M | T4급 |
| `Qwen/Qwen3.5-0.8B` | 0.9B | 2.9M | 스모크 테스트 |

`-Base` 접미사 변형이 사전학습 베이스이고, 접미사 없는 쪽이
post-trained(instruct) 모델이다.

> ⚠️ **초기 리서치 오류를 정정함**: `Qwen/Qwen3.5-4B-Instruct` 같은 ID는
> **존재하지 않는다**(HTTP 401). `-Instruct` 접미사를 쓰지 않는다.

### 1.2 Qwen3.8-27B 실측 스펙 — 계획에 영향을 준 3가지

`config.json`을 직접 받아 확인한 결과, 이름만 보고는 알 수 없는 특성이 있다.

| 항목 | 값 | 영향 |
| --- | --- | --- |
| architectures | `Qwen3_5ForConditionalGeneration` | — |
| **모달리티** | **멀티모달 (vision + video)** | ❗ 텍스트 전용 학습 시 `language_model_only=true` 필요 |
| **아키텍처** | **하이브리드** — 64층, 4층마다 full attention, 나머지는 linear/SSM | ❗ QLoRA·flash-attn 호환성 사전 검증 필요 |
| vocab_size | **248,320** | ✅ 한글 토큰 효율 대폭 개선 (Qwen3는 152K) |
| max_position_embeddings | 262,144 | 262K 컨텍스트 |
| hidden_size / layers | 5120 / 64 | GQA 24Q·4KV, head_dim 256 |
| dtype | BF16 | 가중치만 **≈55.6 GB** |
| **transformers** | **5.8.0.dev0로 저장** | ❗ `transformers>=5.8` 하드 요구 (현 stable 5.15) |
| mtp_num_hidden_layers | 1 | multi-token prediction 헤드 존재 |
| eos / pad | `<|im_end|>` / `<|endoftext|>` | 챗 템플릿에 `enable_thinking`·tools 지원 |

> **1M 다운로드에도 불구하고 "27B는 sLLM이 아니다."** BF16 가중치만 55.6GB라
> 단일 80GB 카드에서 bf16 LoRA는 빠듯하다. → **QLoRA를 기본 레시피로 채택**한다.

### 1.3 VRAM 및 SKU 계획 (Qwen3.8-27B)

| 방식 | 가중치 | 총 피크(추정, seq 2048) | 최소 구성 |
| --- | --- | --- | --- |
| **QLoRA (4bit NF4)** | ~14 GB | **~26 GB** | ⭐ 1× A100 80GB (40GB도 가능) |
| LoRA (BF16) | 55.6 GB | ~76 GB | 1× 80GB 빠듯 → 2× A100/H100 80GB 권장 |
| Full FT | 55.6 GB | ~460 GB | 8× H100 80GB + DeepSpeed ZeRO-3 |

### 1.4 MAI (Microsoft 자체 모델) 검토 — 결론: 파인튜닝 불가

| 모델 | 오픈웨이트 | 파인튜닝 | 비고 |
| --- | --- | --- | --- |
| MAI-DS-R1 | ✅ `microsoft/MAI-DS-R1` (MIT) | 이론상 가능 | DeepSeek-R1 671B 기반 — **sLLM 아님** |
| MAI-Thinking-1 | ❌ API 전용 | "Frontier Tuning" 선별 프리뷰 | 가중치 비공개, HF 툴링 불가 |
| MAI-Code-1 / Flash | ❌ API 전용 | ❌ | 주로 GitHub Copilot 내장 |
| MAI-Image / Voice / Transcribe | ❌ API 전용 | ❌ | 추론 전용 |

**판정**: MAI는 이번 에셋의 **파인튜닝 대상이 될 수 없다.** 가중치가 없고
HF `transformers`/`peft`로 만질 수 없다. 다만 두 가지 역할로는 유용하다.

1. **비교 baseline** — 파인튜닝한 sLLM vs MAI/GPT 대형 모델 품질 비교
2. **LLM-as-judge / 합성 데이터 생성기** — 한국어 SFT 데이터 증강

→ 레지스트리에 `provider: inference_only`로 등록해서 "왜 안 되는지"를
코드와 문서에 명시적으로 남긴다.

### 1.5 그 외 후보

| 모델 | repo ID | 라이선스 | 한국어 | 메모 |
| --- | --- | --- | --- | --- |
| Phi-4-mini | `microsoft/Phi-4-mini-instruct` | MIT | 보통 | Foundry **서버리스 SFT 지원**, 200K vocab |
| **Mi:dm 2.0 Mini** | `K-intelligence/Midm-2.0-Mini-Instruct` | **MIT** | **네이티브** | ⭐ KT, 라이선스 가장 안전 |
| Mi:dm 2.0 Base | `K-intelligence/Midm-2.0-Base-Instruct` | **MIT** | **네이티브** | 11.5B |
| Kanana 2 3B | `kakaocorp/kanana-2-3b-instruct` | other | **네이티브** | 2026-07 최신 한국어 소형 |
| Kanana 2 1.3B | `kakaocorp/kanana-2-1.3b-instruct` | other | **네이티브** | 초경량 |
| HyperCLOVA X SEED | `naver-hyperclovax/HyperCLOVAX-SEED-Text-Instruct-1.5B` | other | **네이티브** | Naver |
| EXAONE 4.0 1.2B | `LGAI-EXAONE/EXAONE-4.0-1.2B` | other(비상업) | **네이티브** | LG, 상업이용 제한 |

> Qwen3.8-27B를 기본으로 하되, **한국어 네이티브 모델(Mi:dm 2.0 Mini, MIT)을
> 비교군**으로 같이 돌려서 "범용 대형 + 한국어 파인튜닝" vs "한국어 네이티브
> 소형"을 정량 비교하는 게 이 에셋의 핵심 데모 포인트가 된다.

---

## 2. 한국어 파인튜닝 데이터셋

### 2.1 상업 이용 안전 세트 (권장 학습 믹스)

| dataset ID | 라이선스 | 상업 | 용도 |
| --- | --- | --- | --- |
| `MarkrAI/KOpen-HQ-Hermes-2.5-60K` | MIT | ✅ | 고품질 범용 instruct |
| `maywell/koVast` | MIT | ✅ | 대용량 범용 |
| `MarkrAI/KoCommercial-Dataset` | MIT | ✅ | 상업이용 목적 큐레이션 |
| `CarrotAI/ko-instruction-dataset` | Apache-2.0 | ✅ | 범용 instruct |
| `lemon-mint/smol-koreantalk` | Apache-2.0 | ✅ | 대화체 |
| `coastral/korean-writing-style-instruct` | Apache-2.0 | ✅ | 문체/작문 |
| `heegyu/open-korean-instructions` | MIT | ✅ | 통합 instruct |
| `nlpai-lab/kullm-v2` | Apache-2.0 | ✅ | KULLM |
| `maywell/korean_textbooks` | Apache-2.0 | ✅ | 239K행, 교과서형 |
| `devngho/korean-instruction-mix` | CC-BY-SA-4.0 | ✅(SA) | 위 소스들의 큐레이션 믹스 |
| `beomi/KoAlpaca-RealQA` | CC-BY-SA-4.0 | ✅(SA) | 실제 질문 기반 |

### 2.2 라이선스 지뢰 (기본 제외)

| dataset ID | 라이선스 | 문제 |
| --- | --- | --- |
| `kyujinpy/KOR-OpenOrca-Platypus` | CC-BY-NC-4.0 | ❌ 비상업 |
| `Bingsu/ko_alpaca_data` | CC-BY-NC-4.0 | ❌ 비상업 |
| `beomi/KoAlpaca-v1.1a` | 미표기 | ⚠️ `text-davinci-003` 증류 |
| `HAERAE-HUB/qarv-instruct-ko` | — | 🔒 gated (401) |
| AI Hub 파생 데이터 전반 | 재배포 제한 | ⚠️ 별도 계약 필요 |

> 에셋은 **기본값을 상업 안전 세트로만 구성**하고, NC 데이터셋은
> `commercial_use: false` 플래그를 달아 명시적으로 opt-in 해야 켜지게 한다.

### 2.3 선호도(DPO) 데이터 — 2단계용

- `kuotient/orca-math-korean-dpo-pairs` (CC-BY-SA-4.0)
- `ChuGyouk/argilla-distilabel-math-preference-dpo-korean` (Apache-2.0)

---

## 3. 한국어 벤치마크

| 벤치마크 | dataset ID | 라이선스 | 측정 대상 |
| --- | --- | --- | --- |
| **KMMLU** | `HAERAE-HUB/KMMLU` | CC-BY-ND-4.0 | 한국 특화 지식 45개 과목 |
| KMMLU-HARD | `HAERAE-HUB/KMMLU-HARD` | CC-BY-ND-4.0 | 난이도 상위 |
| KMMLU-Pro | `LGAI-EXAONE/KMMLU-Pro` | CC-BY-NC-ND-4.0 | 전문가 수준 |
| **HAE-RAE 1.1** | `HAERAE-HUB/HAE_RAE_BENCH_1.1` | CC-BY-NC-ND-4.0 | 한국 문화/어휘/역사 |
| KoBEST | `skt/kobest_v1` | CC-BY-SA-4.0 | 기초 NLU 5종 |
| **IFEval-Ko** | `allganize/IFEval-Ko` | Apache-2.0 | 한국어 지시 이행 |
| **LogicKor** | `maywell/LogicKor` | CC-BY-SA-4.0 | LLM-judge 멀티턴 |
| HRM8K | `HAERAE-HUB/HRM8K` | MIT | 한국어 수학 |
| KUDGE | `HAERAE-HUB/KUDGE` | 미표기 | judge 신뢰도 |
| KorNAT | `jiyounglee0523/KorNAT` | CC-BY-NC-2.0 | 국가 정렬성(평가 전용) |

> ⚠️ 벤치마크 대부분이 **ND(변경 금지)/NC** 라서 **평가 전용**이다.
> 절대 학습 데이터에 섞지 않는다. 코드에서 `eval_only: true`로 강제한다.

**기본 평가 세트**: KMMLU + HAE-RAE + IFEval-Ko + LogicKor
(지식 / 문화 / 지시이행 / 생성품질 4축)

---

## 4. 학습 실행 경로 — GPU vs Foundry 내장 서비스

| 경로 | 대상 모델 | HF 툴링 | LoRA 제어 | GPU 쿼터 | 판정 |
| --- | --- | --- | --- | --- | --- |
| **A. 로컬/VM GPU** | 모든 HF 모델 | ✅ | ✅ | 자체 | 개발·디버깅용 |
| **B. Azure ML managed compute** | 모든 HF 모델 | ✅ | ✅ | 필요 | ⭐ **본 에셋 기본** |
| C. Foundry 서버리스 SFT | Qwen-32B, Phi-4-mini, Llama-3.3-70B, gpt-oss-20b, Mistral-3B | ❌ | ❌ | 불필요 | 소형 Qwen **미지원** |
| D. Azure OpenAI SFT/DPO/RFT | gpt-4.1 / 4.1-mini / 4.1-nano / 4o / o4-mini / gpt-5 | ❌ | ❌ | 불필요 | 비교군 |

### 결론

- **Foundry 내장(서버리스) 파인튜닝은 Qwen3.8-27B를 지원하지 않는다.**
  서버리스 목록의 Qwen은 **Qwen-32B 하나뿐**(Public Preview, Global 전용)이고,
  이는 우리가 고른 Qwen3.8-27B와 **다른 모델**이다.
- 따라서 Qwen3.8-27B를 쓰려면 **경로 B(Azure ML managed compute + 자체 학습
  스크립트 + QLoRA)** 가 유일한 정답.
- 단, 에셋은 **A/B/C/D를 모두 추상화**해서 `--backend` 로 갈아끼울 수 있게 만든다.
  그래야 "Phi-4-mini는 서버리스로, Qwen3.8-27B는 GPU로" 같은 비교 데모가 가능하다.

### 권장 GPU SKU

| SKU | GPU | 용도 |
| --- | --- | --- |
| `Standard_NC4as_T4_v3` | 1× T4 16GB | 스모크 테스트 (0.8B~2B QLoRA) |
| `Standard_NC24ads_A100_v4` | 1× A100 80GB | ⭐ **Qwen3.8-27B QLoRA (기본)** |
| `Standard_NC40ads_H100_v5` | 1× H100 80GB | 27B QLoRA 속도 우선 |
| `Standard_ND96amsr_A100_v4` | 8× A100 80GB | 27B BF16 LoRA / 대규모 |
| `Standard_ND96isr_H100_v5` | 8× H100 80GB | 27B 풀 파인튜닝 (ZeRO-3) |

> A100·H100 쿼터는 기본 0인 경우가 많다. **데모 전 쿼터 증설 신청 필수**
> (수일~수주 소요). 대안으로 AML **serverless compute** 사용.

---

## 5. Fabric 통합

### 5.1 Fabric은 GPU 학습을 제공하지 않는다

Fabric Spark는 **CPU 전용**이다. 따라서 역할 분담은 다음과 같다.

```
Fabric  = 데이터 준비 + 카탈로그 + 계보 + MLflow 실험 추적
Foundry = GPU 학습 + 배포 + 평가
```

### 5.2 데이터 전달 경로

```
Fabric Lakehouse (Delta Tables)
  └─ Spark 노트북: dedup / 필터 / PII 마스킹 / 챗 템플릿 / train-val split
       └─ Files/ 에 JSONL 기록
            └─ AML OneLakeDatastore (Preview) 로 마운트
                 └─ AML command job 입력
```

- OneLake ABFS 경로:
  `abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<lakehouse>.Lakehouse/Files/...`
- `OneLakeDatastore`는 **Files/ 폴더만** 지원한다. Delta **Tables/ 직접 마운트 불가**
  → Spark에서 Parquet/JSONL로 내보내는 단계가 반드시 필요하다.
- 자동화 시엔 서비스 주체(SP)에 Fabric 워크스페이스 **Viewer** 권한 부여.

### 5.3 인증

| 대상 | 스코프 |
| --- | --- |
| Foundry / AML | `https://ai.azure.com/.default` (`DefaultAzureCredential`) |
| OneLake | `https://storage.azure.com/.default` (Storage 스코프 전용) |

---

## 6. 에셋 구조

```
fabric-foundry-sllm-finetune/
├─ configs/
│  ├─ models.yaml          # 교체 가능한 모델 레지스트리 (검증된 ID)
│  ├─ datasets.yaml        # 한국어 데이터셋 + 라이선스 플래그
│  ├─ benchmarks.yaml      # 평가 전용 벤치마크
│  └─ recipes/             # LoRA / QLoRA / DPO 하이퍼파라미터
├─ src/ffsft/
│  ├─ models/              # ModelSpec + Registry  ← 교체 가능성의 핵심
│  ├─ data/                # HF 로더, 챗 포맷 변환, 라이선스 게이트
│  ├─ fabric/              # OneLake 읽기/쓰기, Spark 프렙 헬퍼
│  ├─ train/               # backend: local / aml / foundry_serverless / aoai
│  ├─ eval/                # 한국어 벤치마크 + Foundry Evaluation
│  ├─ deploy/              # 모델 등록 + 온라인 엔드포인트(vLLM)
│  └─ cli.py               # ffsft models|data|train|eval|deploy
├─ notebooks/fabric/       # Fabric Spark 노트북
└─ scripts/                # 검증/부트스트랩 스크립트
```

### 모델 교체 방식

```bash
ffsft train --model qwen3.8-27b  --backend aml --recipe qlora   # ⭐ 기본
ffsft train --model qwen3.5-0.8b --backend local --recipe qlora  # 스모크
ffsft train --model midm-2.0-mini --backend aml                  # 한국어 네이티브 비교군
ffsft train --model phi4-mini    --backend foundry_serverless    # Foundry 내장 서비스
ffsft train --model aoai-gpt-4.1-mini --backend aoai             # 상용 비교군
```

코드는 모델 ID를 하드코딩하지 않는다. `configs/models.yaml`에 항목을
추가하면 즉시 새 모델을 쓸 수 있다.

---

## 7. 실행 단계

| 단계 | 산출물 | 상태 |
| --- | --- | --- |
| 0 | 리포·구조·모델/데이터/벤치마크 레지스트리 | ✅ 완료 |
| 1 | Fabric Spark 데이터 프렙 노트북 → OneLake JSONL | |
| 2 | 로컬 스모크 학습 (Qwen3.5-0.8B QLoRA) — 파이프라인 검증 | |
| 3 | **Qwen3.8-27B 호환성 검증** (transformers 5.15 + 4bit + 하이브리드 층) | |
| 4 | AML command job 제출 (Qwen3.8-27B QLoRA, A100 80GB) | |
| 5 | 한국어 벤치마크 before/after 평가 | |
| 6 | Foundry 온라인 엔드포인트 배포(vLLM) | |
| 7 | 비교 실험: Qwen3.8-27B vs Mi:dm vs Phi 서버리스 vs gpt-4.1-mini | |
| 8 | Fabric MLflow에 실험 로깅 + 대시보드 | |

---

## 8. 리스크

| 리스크 | 영향 | 완화 |
| --- | --- | --- |
| **27B QLoRA + 하이브리드(linear/SSM) 층 호환성** | 학습 자체 실패 | 3단계에서 별도 검증, 실패 시 Qwen3.6-27B 또는 Qwen3.5-9B 폴백 |
| **멀티모달 체크포인트를 텍스트로 학습** | 비전 타워 낭비/오류 | `language_model_only=true`, 비전 파라미터 freeze |
| A100/H100 쿼터 0 | 데모 불가 | 사전 쿼터 신청, AML serverless compute, T4+소형 모델 폴백 |
| `transformers>=5.8` 요구 | 환경 파손 | 학습 컨테이너 버전 고정, 커스텀 Docker 이미지 |
| OneLakeDatastore Preview | 파이프라인 파손 | abfss 직접 읽기 폴백 경로 유지 |
| 벤치마크 ND/NC 라이선스 | 컴플라이언스 | `eval_only` 강제, 학습 믹스 차단 |
| 한국어 데이터 품질 편차 | 성능 저하 | dedup + 길이/언어 필터 + 수동 샘플 검수 |
| Kanana/HyperCLOVA/EXAONE `other` 라이선스 | 상업 이용 리스크 | 기본 비교군은 MIT인 Mi:dm 2.0 사용 |
| Fabric↔AML MLflow 연동 비공식 | 추적 단절 | AML 네이티브 추적 기본, Fabric은 선택 |
