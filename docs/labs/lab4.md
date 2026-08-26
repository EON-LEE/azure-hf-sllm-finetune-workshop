# Lab 4 — vLLM 서빙 이미지

> **Track B 시작점 · 선행: [Lab 0](lab0.md)** — **학습 잡을 한 번도 안 돌려도 됩니다**

> ## 💰 GPU 과금은 없습니다
> 빌드는 ACR 빌드 에이전트(CPU)에서 돕니다. 20 GB 이미지가 여러분 회선을 타지 않습니다.
> 대신 **빌드 타임에 GPU 가 없다**는 사실이 이 Lab 의 핵심 제약입니다.

## 목표

- 서빙 이미지가 **왜 학습 이미지와 절대 합쳐질 수 없는지** 이해한다
- vLLM 태그 하나가 "모델이 아예 안 뜨는" 사유가 되는 것을 확인한다
- **빌드가 실패하도록 만드는 것**이 20분짜리 배포 실패보다 싸다는 걸 몸으로 안다

## 선행조건

- Lab 0 완료
- ACR 하나 (Basic 이면 충분). `az acr create -n <이름> -g $FFSFT_RESOURCE_GROUP --sku Basic`

## 소요·비용

**30분** — 대부분 빌드 대기. ACR Basic 은 월 $5 수준.

---

## 1. 이미지가 둘인 이유

| | `Dockerfile.train` | `Dockerfile.serve` |
|---|---|---|
| 베이스 | ACPT (Microsoft 검증 torch) | `vllm/vllm-openai` |
| 필요한 것 | bitsandbytes NF4, PEFT, TRL | vLLM 자체 torch/CUDA 커널 |
| 크기·시간 | ~20 GB / 25분 | 훨씬 작음 |

**vLLM 을 ACPT 위에 설치하면 검증된 torch 가 교체됩니다** — `docker/verify_stack.py`
가 막으려는 바로 그 일입니다. 둘은 베이스를 공유할 수 없고, 합쳐서도 안 됩니다.

## 2. 태그가 `latest` 가 아닌 이유

```dockerfile
ARG VLLM_TAG=v0.27.1
FROM vllm/vllm-openai:${VLLM_TAG}
```

- Qwen3.8-27B 의 `architectures` 는 `Qwen3_5ForConditionalGeneration`
- 그 클래스는 vLLM **v0.27.0** 에 처음 등록됐습니다 (2026-08-10, PR #50210)
- **그보다 낮으면 이 모델은 아예 적재되지 않습니다.** v0.27.1 이 그 라인의 최신 안정 패치

> `vllm/vllm-openai:qwen38` 이라는 특수 태그도 있습니다. NVIDIA/AMD 공동개발
> Gated-DeltaNet·MoE 커널이 들어 있는데 겨냥한 대상은 2.4T MoE 플래그십입니다.
> 우리 목표는 **dense 27B** 라 안정 라인으로 충분합니다.
> A/B 하려면 `--build-arg VLLM_TAG=qwen38`.

## 3. 빌드

```bash
az acr build \
   --registry <your-acr> \
   --image ffsft-serve:1 \
   --file docker/Dockerfile.serve .
```

`az acr build` 는 **제출 시점에 컨텍스트를 스냅샷**합니다. 업로드가 시작된 뒤 파일을
고쳐도 이미지에는 반영되지 않습니다 (§42.5).

## 4. 기대 출력 — 빌드 게이트가 통과해야 끝난다

`docker/verify_serve.py` 가 빌드 중에 돌고, 실패하면 **이미지가 안 만들어집니다.**

```
vllm 0.27.1   torch 2.13.0+cu130   python 3.12
arch Qwen3_5ForConditionalGeneration: registered
flags 12 required: all present   (--language-model-only 포함)
```

### 이 게이트를 만들면서 실제로 밟은 지뢰 4개 (§4.1)

| 회차 | 증상 | 원인 |
|---|---|---|
| 1 | `python: not found` | vLLM 이미지에는 `python3` 만 있습니다 |
| 2 | `cannot import name 'FlexibleArgumentParser'` | 0.27 에서 위치가 바뀜 |
| 3 | `RuntimeError: Failed to infer device type` | `make_arg_parser()` 가 `VllmConfig` 를 만들고, 그게 **GPU 를 요구**합니다. ACR 빌드 에이전트는 CPU 전용 |
| 4 | — | 통과 |

3번 때문에 플래그 검증을 파서 생성이 아니라 **vLLM 소스 2,360개 파일 텍스트 스캔**으로
바꿨습니다. **빌드 타임에 GPU 가 없다** — 이게 설계 제약입니다.

> ⚠️ 빌드 가드를 여러 줄로 쓰지 마세요. ACR 의 의존성 스캐너는 Dockerfile 을 직접
> 파싱하면서 **따옴표 안의 `\` 연속행을 이해하지 못합니다.** 3초 만에
> `unable to understand line from ...` 로 죽습니다 (§42.5). 한 줄로 평평하게 쓰세요.

## 5. ⚠️ 아키텍처 플래그는 중립이 기본이다

```dockerfile
ENV MAMBA_CACHE_MODE="" \
    LANGUAGE_MODEL_ONLY=0 \
    REASONING_PARSER=""
```

예전에는 이 셋이 Qwen3.8-27B 의 값으로 **박혀** 있었습니다. 그 결과 dense·텍스트 전용인
`Qwen3-0.6B` 스모크 배포가 `--language-model-only` 와 `--mamba-cache-mode align` 을 달고
떠서 **끝내 healthy 가 되지 않았습니다.**

**변하는 것은 모델이므로 모델이 정합니다.** `ffsft.models.spec` 이 `multimodal` /
`mamba_cache_mode` / `reasoning_parser` 를 들고 있고, `serving_env()` 가 **중립일 때도
세 키를 전부 내보냅니다** — 이미지 기본값이 다시는 조용히 적용될 수 없게.

```bash
uv run ffsft serving show qwen3.8-27b      # 이 모델이 요구하는 플래그
uv run ffsft serving adapter-modes
```

> Qwen3.8 은 48개 GDN 레이어 때문에 `--mamba-cache-mode align` 이 **필수**입니다.
> `all` 로 두면 `NotImplementedError` 가 납니다.

## 6. 코드 수정 = 이미지 수정

```python
SERVE_IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:5"
SERVE_ENVIRONMENT_VERSION = image_tag(SERVE_IMAGE)   # 파생값, 손으로 안 씁니다
```

Azure ML 환경 버전은 **불변**입니다. 태그를 재사용하면 옛 이미지가 조용히 다시 뜹니다.
그래서 환경 버전을 태그에서 **뽑아냅니다** — 손으로 적으면 어긋날 수 있으니까.
→ [GOTCHAS #12](../GOTCHAS.md#12)

---

## 막히면

| 증상 | 항목 |
|---|---|
| 빌드가 3초 만에 `unable to understand line` | §42.5 — 따옴표 안 `\` 연속행 |
| 빌드 중 `Failed to infer device type` | 빌드 에이전트에 GPU 가 없습니다 (§4.1) |
| 배포는 되는데 healthy 가 안 됨 | 아키텍처 플래그. [#12](../GOTCHAS.md#12), §4.2 |
| 고친 코드가 반영 안 됨 | 태그를 올리세요. [#12](../GOTCHAS.md#12) |

## 정리

빌드된 이미지는 ACR 에 남습니다(스토리지 요금만). GPU 는 아직 안 켰습니다.

**다음**: [Lab 5 — 관리형 온라인 엔드포인트](lab5.md)
