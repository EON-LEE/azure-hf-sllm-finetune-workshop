# Lab 4 — vLLM 서빙 이미지

> **Track B 시작점 · 선행: [Lab 0](lab0.md)** — **학습 잡을 한 번도 안 돌려도 됩니다**

> ## 💰 GPU 과금은 없습니다
> 빌드는 ACR 빌드 에이전트(CPU)에서 돕니다. 20 GB 이미지가 여러분 회선을 타지 않습니다.
> 대신 **빌드 타임에 GPU 가 없다**는 사실이 이 Lab 의 핵심 제약입니다.

## 목표

- 서빙 이미지가 **왜 학습 이미지와 절대 합쳐질 수 없는지** 이해한다
- vLLM 태그 하나가 "모델이 아예 안 뜨는" 사유가 되는 것을 확인한다
- **빌드가 실패하도록 만드는 것**이 배포 실패보다 싸다는 걸 몸으로 안다
  — 이 리포가 실제로 낸 실패 배포 한 번이 **1시간 54분 · 약 $4.1** 이었습니다 (§12)

## 선행조건

- Lab 0 완료
- **새 셸이면 `source ~/.ffsft-env` 부터.** 배너가 `profile: ffsft …` 으로 찍혀야 합니다.
  이건 코드가 아니라 셸 세션 설정이라 Lab 0 에서 한 번 한 걸로 안 따라옵니다 — 이 파일이
  `AZURE_CONFIG_DIR` 까지 같이 잡아줍니다. 안 하면 `az` 가 전역 `~/.azure` 프로필에
  조용히 씁니다. → [GOTCHAS #1](../GOTCHAS.md#1)
- **ACR 은 이미 있습니다** — [Lab 0 §4](lab0.md) 의 `ffsft infra up` 이 워크스페이스와
  같은 그룹에 만들었고, 이름은 `$FFSFT_ACR` 에 실려 있습니다.

  ```bash
  echo "$FFSFT_ACR"          # acr<본인><해시>
  az acr show -n "$FFSFT_ACR" -g "$FFSFT_RESOURCE_GROUP" --query loginServer -o tsv
  ```

  비어 있으면 `source ~/.ffsft-env` 를 안 한 것입니다.

> ### ACR 이 워크스페이스와 **같은 그룹**에 있는 것이 이 워크샵의 전제입니다
>
> `infra/workspace.bicep` 이 ACR 을 그룹 안에 만들고, 워크스페이스의
> `containerRegistry` 로 **연결**합니다. 그래서 [Lab 5](lab5.md) 의 엔드포인트가
> 이미지를 pull 할 때 AcrPull 이 붙는 경로가 하나로 정해집니다.
>
> **왜 이걸 굳이 적어 두나.** 이 리포는 한동안 ACR 을 한쪽 rg 에, 엔드포인트를 다른 rg 에
> 두고 돌렸습니다. 코드가 그걸 견디도록 짜여 있어서 대부분은 돌았고 — 그러다 워크스페이스의
> `properties.containerRegistry` 가 비어 있는 경로에서 **AcrPull 이 엉뚱한 그룹의
> 레지스트리로 나갔습니다** (`src/ffsft/deploy/identity.py:319`, `:411`). 배포는 10분 뒤
> `Endpoint identity does not have pull permission` 으로 죽었습니다.
>
> 코드가 견디는 범위는 그대로 남겨 뒀습니다:
>
> | | |
> |---|---|
> | **리소스그룹**이 달라도 되나 | 됩니다. `deploy-online` 은 ACR 을 **이름으로 구독 전체에서** 찾습니다 (`acr_id_for_image`) |
> | **리전**이 달라도 되나 | 됩니다. 크로스리전 pull 은 실측으로 돌았습니다 — koreacentral ACR → polandcentral 배포 |
> | **구독**이 달라도 되나 | **안 됩니다.** ACR 은 엔드포인트와 **같은 구독**이어야 합니다 |
>
> 이 워크샵에서는 셋 다 같습니다. 위 표는 "여러분이 고를 수 있는 것"이 아니라
> **코드가 무엇을 견디는지**의 기록입니다.

## 소요·비용

**약 30분(계획용 추정)** — 대부분 빌드 대기, GPU 는 안 켭니다.
**서빙 이미지 빌드를 잰 기록은 이 리포에 없습니다.** 훈련 이미지 쪽 실측이 7~15분입니다
(§21 의 7~13분, §53.1 의 14분 47초).

**ACR 요금은 `?` 입니다** — 이 리포는 ACR 단가를 잰 적이 없습니다 (§11.2 의 Retail Prices
조회는 디스크·공인 IP 뿐입니다). 아는 건 용량뿐입니다: Basic 포함 10 GB, 이 리포가 남긴
이미지 두 개가 **19.5 GB** 라 초과분이 과금됩니다 (§11.5). 단가는 Portal 의 Cost analysis
나 소매 가격 API 로 직접 보세요 — `?` 는 공짜가 아니라 미확인입니다 ([Lab 7 §2.1](lab7.md)).

---

## 1. 이미지가 둘인 이유

| | `Dockerfile.train` | `Dockerfile.serve` |
|---|---|---|
| 베이스 | ACPT (Microsoft 검증 torch) | `vllm/vllm-openai` |
| 필요한 것 | bitsandbytes NF4, PEFT, TRL | vLLM 자체 torch/CUDA 커널 |
| 크기·시간 | ~20 GB / **실측 빌드 7~15분** (§21, §53.1) | 훨씬 작음 |

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
   --registry "$FFSFT_ACR" \
   --image ffsft-serve:1 \
   --file docker/Dockerfile.serve .
```

`az acr build` 는 **제출 시점에 컨텍스트를 스냅샷**합니다. 업로드가 시작된 뒤 파일을
고쳐도 이미지에는 반영되지 않습니다 (§42.5).

> ### 이 참조를 적어 두세요 — Lab 5 가 그대로 받습니다
> `$FFSFT_ACR.azurecr.io/ffsft-serve:1`. 이게 **여러분의** 이미지이고,
> [Lab 5](lab5.md) 의 `--image` 에 그대로 들어갑니다:
>
> ```bash
> uv run ffsft deploy deploy-online --image "$FFSFT_ACR.azurecr.io/ffsft-serve:1" ...
> # 셸마다 한 번 내보내고 --image 를 빼도 됩니다
> export FFSFT_SERVE_IMAGE=$FFSFT_ACR.azurecr.io/ffsft-serve:1
> ```
>
> **태그 `1` 은 장식이 아닙니다.** 그게 Azure ML 환경 버전이 됩니다 (§6).

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
uv run ffsft serving list                    # 패턴 5개 — 키를 먼저 확인하세요
uv run ffsft serving show aml_online_vllm    # Lab 5 가 쓰는 패턴
uv run ffsft serving adapter-modes
```

> `serving show` 가 받는 건 **서빙 패턴 키**(`aks_vllm`, `aml_batch`, `aml_batch_vllm`,
> `aml_online_vllm`, `local_vllm`)이지 모델 키가 아닙니다. `serving show qwen3.8-27b`
> 는 `KeyError: unknown serving pattern` 입니다 — **모델과 패턴은 다른 레지스트리**입니다.

모델별 플래그 세 개는 `configs/models.yaml` 의 스펙에 있고 `serving_env()` 가 읽습니다:

```bash
grep -A2 'multimodal:' configs/models.yaml     # multimodal / mamba_cache_mode / reasoning_parser
```

> Qwen3.8 은 48개 GDN 레이어 때문에 `--mamba-cache-mode align` 이 **필수**입니다.
> `all` 로 두면 `NotImplementedError` 가 납니다.

## 6. 코드 수정 = 이미지 수정, 이미지 = 환경 버전

배포할 이미지를 정하는 함수는 **하나**입니다 — `endpoint.resolve_serve_image()`, 배포당 한 번:

| 우선순위 | 출처 |
|---|---|
| 1 | `--image` (또는 `deploy_online(image=…)`) |
| 2 | `$FFSFT_SERVE_IMAGE` |
| 3 | `SERVE_IMAGE` 상수 = `acrffsftkc.azurecr.io/ffsft-serve:5` — **저자들의 사설 ACR** |

3번은 여러분이 pull 할 수 없습니다. 그래서 §3 에서 빌드한 걸 1이나 2로 넣는 겁니다.

1·2 가 비었거나 공백뿐이면 **없는 것으로 치고 다음으로 내려갑니다.**
`export FFSFT_SERVE_IMAGE=` 는 빈 이미지를 배포하라는 요청이 아니라 셸 사고니까요.
`--image` 의 argparse 기본값이 `None` 인 것도 같은 이유입니다 — 파서 기본값을 두면
그게 **매번 환경변수를 가립니다.**

그리고 환경 버전은 **지금 배포하는 그 이미지**의 태그에서 나옵니다:

```python
serve_image = resolve_serve_image(image)     # 배포당 한 번
version     = image_tag(serve_image)         # 파생값, 손으로 안 씁니다
```

Azure ML 환경 버전은 **불변**입니다. 태그를 재사용하면 옛 이미지가 조용히 다시 뜹니다.
그래서 버전을 태그에서 뽑아내는데, **상수가 아니라 인자에서** 뽑는 게 핵심입니다.
상수(`SERVE_ENVIRONMENT_VERSION`, 이제는 호환용 export 로만 남아 있고 배포 경로에서는
아무도 안 읽습니다)에서 뽑으면 여러분의 `ffsft-serve:1` 이 저자들의 `:5` 를 들고 있는
버전 `5` 에 부딪히고, **Azure ML 은 거부가 아니라 저장된 엔티티를 돌려줍니다** — 즉
여러분이 부르지도 않은 이미지가 뜹니다. `serve_environment()` 가 그 자리에서 비교해
멈추고, 에러 메시지가 고치는 법으로 `--image` / `FFSFT_SERVE_IMAGE` 를 지목합니다.

`image_tag` 는 태그 없는 참조와 다이제스트 고정(`@sha256:`)도 거부하고, **그 거부가
`check_pattern` 보다 먼저** — 즉 Azure 를 한 번도 부르기 전에, 노드 할당 15~30분을
태우기 한참 전에 일어납니다. `:latest` 는 같은 버그가 옷만 갈아입은 것입니다.

고친 코드를 반영하려면 **태그를 올리세요.** `ffsft-serve:2` 를 빌드해 `--image` 로 주면
환경 버전도 같이 2 로 갑니다. → [GOTCHAS #12](../GOTCHAS.md#12)

---

## 막히면

| 증상 | 항목 |
|---|---|
| 빌드가 3초 만에 `unable to understand line` | §42.5 — 따옴표 안 `\` 연속행 |
| 빌드 중 `Failed to infer device type` | 빌드 에이전트에 GPU 가 없습니다 (§4.1) |
| 배포는 되는데 healthy 가 안 됨 | 아키텍처 플래그. [#12](../GOTCHAS.md#12), §4.2 |
| 고친 코드가 반영 안 됨 | 태그를 올리세요. [#12](../GOTCHAS.md#12) |
| `image '...' carries no tag` | 태그를 붙이세요. `latest`·다이제스트는 환경 버전이 못 됩니다 (§6) |
| `environment 'ffsft-serve:1' is already registered against ...` | 같은 태그로 다른 이미지를 밀었습니다. 새 태그를 빌드해 `--image` 로 주세요 (§6) |
| 저자들의 ACR 을 pull 하려다 실패 | `--image` 도 `FFSFT_SERVE_IMAGE` 도 안 준 겁니다 (§6 우선순위 3) |

## 정리

빌드된 이미지는 ACR 에 남습니다(스토리지 요금만). GPU 는 아직 안 켰습니다.

손에 남는 것은 **참조 한 줄** — `$FFSFT_ACR.azurecr.io/ffsft-serve:1`. Lab 5 가 그걸 받습니다.

**다음**: [Lab 5 — 관리형 온라인 엔드포인트](lab5.md)
