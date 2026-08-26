# GOTCHAS — 돈과 시간을 실제로 태운 함정들

이 워크샵을 진행하면서 **실제로 밟은** 함정만 모았다. 전부 라이브 Azure 구독에서
측정했고, 각 항목의 `§N` 은 `docs/JOURNAL.md` 의 해당 절을 가리킨다 —
증상만으로는 판단이 안 될 때 그 절에 원본 출력이 있다.

읽는 순서는 실습 순서와 같다. 지금 막힌 곳부터 봐도 된다.

| # | 함정 | 언제 밟나 |
|---|---|---|
| [1](#1) | 오류가 테넌트를 말하는데 테넌트 문제가 아니다 | Lab 0 |
| [2](#2) | 쿼터 승인 ≠ 용량 확보 | Lab 0, 5 |
| [3](#3) | 리전이 진짜 축이다 | Lab 0, 5 |
| [4](#4) | 온라인 엔드포인트는 코어를 2배로 먹는다 | Lab 5 |
| [5](#5) | 27B 는 A10 24GB 에 안 들어간다 | Lab 2 |
| [6](#6) | LoRA 타깃을 안 정하면 조용히 헛학습한다 | Lab 2 |
| [7](#7) | 선언하지 않은 출력은 노드와 함께 죽는다 | Lab 2 |
| [8](#8) | 잡 노드 디스크는 SKU 스펙이 아니라 64 GB 다 | Lab 2, 8 |
| [9](#9) | 잡 안에서는 MLflow 말고 아무것도 못 내보낸다 | Lab 2, 3 |
| [10](#10) | 등록은 증거가 아니다 | Lab 8 |
| [11](#11) | 모델 자산 이름에 점을 못 쓴다 | Lab 8 |
| [12](#12) | 코드는 이미지에 구워져 있다 | Lab 4 |
| [13](#13) | 재배포가 서빙 중인 엔드포인트를 조용히 죽인다 | Lab 8 |
| [14](#14) | scoringUri 는 한 모양이 아니다 | Lab 5, 6 |
| [15](#15) | 사고 토큰 필드는 `reasoning` 이다 | Lab 6 |
| [16](#16) | thinking 을 켜면 토큰 예산이 사라진다 | Lab 6 |
| [17](#17) | 클러스터를 다시 만들면 권한이 **둘 다** 날아간다 | Lab 5 |
| [18](#18) | 실패한 배포도 과금된다 | 전 구간 |

---

## <a id="1"></a>1. 오류가 테넌트를 말하는데 테넌트 문제가 아니다

- 증상: `InvalidAuthenticationTokenTenant — access token is from the wrong issuer`
- 오독하기 쉬운 것: 로그인 만료 / 역할 누락 / 코드 버그 — **셋 다 아니다**
- 실제: 자격증명은 멀쩡하고, **다른 디렉터리 것**이다
- 원인: `FFSFT_SUBSCRIPTION_ID` 는 구독만 정한다. 테넌트는 `az` 의 활성 계정에서 온다
- 함정의 함정: 테넌트를 고정해도 **CLI 전역 프로필이 밑에서 바뀐다.**
  `az` 는 활성 계정을 `$AZURE_CONFIG_DIR/azureProfile.json` 한 곳에 둔다
- 대처:
  ```bash
  export AZURE_CONFIG_DIR=~/.azure-ffsft   # 이 워크샵 전용 프로필로 격리
  export FFSFT_TENANT_ID=<your-tenant-id>
  az account set --subscription $FFSFT_SUBSCRIPTION_ID
  ```
- 근거: §39 (한 세션에 두 번, 실행 도중에 드리프트)

## <a id="2"></a>2. 쿼터 승인 ≠ 용량 확보

- 증상: 쿼터는 넉넉한데 `NotAvailableForSubscription` / 노드가 영원히 대기
- 원인: 쿼터와 `Microsoft.Compute/skus` 의 `restrictions` 는 **다른 축**이다.
  쿼터는 "얼마까지 쓸 수 있나", restrictions 는 "여기서 쓸 수 있나"
- 실측: A10 쿼터 72코어를 받고도 A10 배포는 계속 실패 —
  `restrictions: [Zone]` 이 걸려 있었다
- **세 번째 축**: 같은 SKU 라도 쓸 수 있는 컴퓨트 종류가 다르다.
  `NV*ads_A10_v5` 는 전부 **MIR 전용** — 관리형 엔드포인트에만 쓰고
  **AmlCompute 학습 클러스터로는 못 만든다.** 그런데 거부 메시지는
  `not a supported VM size` 라서 쿼터 문제처럼 읽힌다
- 대처: 쿼터 신청 전에 두 API 를 읽는다
  ```bash
  # 여기서 쓸 수 있나
  az vm list-skus --location <region> --size Standard_NC24ads_A100_v4 \
     --query "[].restrictions" -o json
  # 무엇으로 쓸 수 있나 (AmlCompute / ComputeInstance / MIR)
  az rest --method get --url "https://management.azure.com/subscriptions/\
$FFSFT_SUBSCRIPTION_ID/providers/Microsoft.MachineLearningServices/\
locations/<region>/vmSizes?api-version=2024-04-01" \
     --query "value[?name=='Standard_NV36ads_A10_v5'].supportedComputeTypes"
  ```
- 근거: §28 → §30 정정 → §52 → §54 → §56 (원인 진단을 네 번 갈아엎었다), §48

## <a id="3"></a>3. 리전이 진짜 축이다

- 증상: 구독·쿼터·정책을 다 맞췄는데 GPU 온라인 엔드포인트가 안 뜬다
- 실측 (같은 구독, 같은 날):

  | SKU | koreacentral | polandcentral |
  |---|---|---|
  | `Standard_NC24ads_A100_v4` | BLOCKED (Location) | **FREE** |
  | `Standard_NV*ads_A10_v5` | BLOCKED (Zone) | BLOCKED (Location,Zone) |

- 대처: 막히면 코드를 고치기 전에 **다른 리전에서 같은 질문**을 한다.
  `FFSFT_LOCATION` 하나만 바꾸면 된다
- 근거: §57 — 이 리포의 살아 있는 엔드포인트는 polandcentral 에 있다

## <a id="4"></a>4. 온라인 엔드포인트는 코어를 2배로 먹는다

- 공식: `ceil(1.2 × instances) × cores` — 롤링 업데이트용 예비분
- 결과: **1 인스턴스가 2배**, 2 인스턴스는 3배 (4배가 아니다)
- 실측: A10 36코어 승인 + `Standard_NV36ads_A10_v5`(기본값) = 72코어 필요 → **불가**
- 면제: **A100 / H100 / ND 계열은 예비분이 없다** (문서의 "Skip 20% Reservation").
  A100 24코어는 24코어면 된다
- 대처: `configs/serving.yaml` 의 `default_sku` 를 확인한다. 기본값이 가장 큰 SKU 다
- 근거: §26.3, 코드는 `ONLINE_ENDPOINT_UPGRADE_RESERVATION`

## <a id="5"></a>5. 27B 는 A10 24GB 에 안 들어간다

- 실측 (QLoRA 4bit, seq 1024, 유효 배치 16):

  | 지표 | 값 |
  |---|---|
  | 적재 후 VRAM | 17.67 GB |
  | **학습 피크 VRAM** | **28.19 GB** |
  | 벽시계 | 2496초 (41.6분 / 30스텝) |

- 함정: **적재는 된다.** 24GB 카드에서 모델이 올라가고 첫 스텝에서 OOM 난다
- 대처: 40GB 이상 (A100) 을 쓴다. 24GB 로 실습하려면 더 작은 모델을 고른다
- 근거: §20, §35

## <a id="6"></a>6. LoRA 타깃을 안 정하면 조용히 헛학습한다

- 증상: **없다.** 학습이 정상 종료하고 loss 도 떨어진다. 그런데 성능이 안 는다
- 원인: PEFT 기본 타깃 `{q,k,v,o}_proj` 는 Qwen3.5/3.6/3.8 의
  **4개 중 1개인 full-attention 레이어에만** 존재한다. 나머지 레이어는 어댑터가 안 붙는다
- 대처: 이 리포는 **추측하지 않고 거부한다.** `configs/models.yaml` 에
  `lora_target_modules` 를 명시하거나, 알고서 `--allow-default-lora-targets` 를 준다
- 확인: `uv run python scripts/probe_architecture.py qwen3.8-27b --check`
  (meta 디바이스에 올려서 검사 — 가중치를 안 받는다)

## <a id="7"></a>7. 선언하지 않은 출력은 노드와 함께 죽는다

- 증상: 잡이 `Completed` 인데 어댑터가 없다
- 원인: `JobSpec.declared_outputs()` 가 돌려주는 `{model_dir, report}` 만 살아남는다.
  스크립트가 그 밖에 쓴 것은 노드가 사라질 때 같이 사라진다
- 대가: **27B 완주 2회분의 어댑터를 이렇게 잃었다**
- 관련: `mount_outputs=True` 는 이 워크스페이스에서 항상 실패한다 (§17)
- 대처: 저장 경로는 반드시 선언된 출력 아래. 끝나고 §10 으로 확인

## <a id="8"></a>8. 잡 노드 디스크는 SKU 스펙이 아니라 64 GB 다

- 실측: `AZ_BATCH_NODE_ROOT_DIR` 총량 **64197 MB (≈64 GB)** — SKU 문서의 임시 디스크 값과 무관
- 증상: 27B 가중치 + HF 캐시 + 병합 산출물을 한 노드에서 다루면 중간에 `No space left`
- 대처: HF 캐시를 배치 루트 **밖으로** 뺀다 (`HF_HOME`). 병합 잡은 특히
- 근거: §50

## <a id="9"></a>9. 잡 안에서는 MLflow 말고 아무것도 못 내보낸다

- 증상: 잡 stdout 이 이 워크스테이션에서 안 읽힌다. 로그 SAS URI 는 403
- 원인: 네트워크 격리된 워크스페이스. **MLflow 만 뚫려 있다**
- 대처 3종:
  1. 값은 MLflow 메트릭/태그로 (`ffsft.mlflow_report`)
  2. **참/거짓 판정은 종료 코드로** — `scripts/verify_output_path.py` 가 이 패턴
  3. 상태 추적은 `scripts/watch_jobs.sh` (ARM status + MLflow `lastvalues`)
- 곁가지: `lastvalues` 는 메트릭 이름만 등록되고 값이 아직이면 `[null]` 을 돌려준다.
  여기서 죽는 파서를 만들면 **메트릭이 도착하기 시작하는 바로 그 순간 조용해진다**
- 근거: §42, §32, §58.7

## <a id="10"></a>10. 등록은 증거가 아니다

- 증상: 모델 자산이 등록됐는데 배포하면 가중치가 없다
- 원인: 등록은 **경로를 가리키는 행위**다. 그 경로에 뭐가 있는지 검사하지 않는다
- 대처: 마운트해서 센다
  ```bash
  uv run python scripts/verify_output_path.py <run-name> model_dir
  # Completed = 파일과 바이트와 어댑터 가중치가 있었다
  # Failed    = 없었다. 학습이 쓸 만한 걸 안 남겼다
  ```
- 근거: §34, §36

## <a id="11"></a>11. 모델 자산 이름에 점을 못 쓴다

- 증상: `qwen3.8-27b` 로 등록 시도 → 거부
- 전문: `Resource name can only contain alphanumeric characters, dashes, and underscores`
- 함정: **레지스트리 키는 거의 다 점을 갖고 있다** (`qwen3.8-27b`, `kanana2-1.3b`) —
  예외가 아니라 기본값이다
- 대처: `asset_name()` 이 치환한다. 치환은 **되돌릴 수 없으므로**
  (`kanana2-1_3b` 로는 `configs/models.yaml` 을 못 찾는다) 원래 키를
  `model_key` 태그에 보존한다
- 같이 오는 함정 — **경로 표기도 직관과 다르다**:
  ```
  azureml://jobs/{job}/outputs/{name}                              -> NoMatchingArtifactsFoundFromJob
  azureml://datastores/workspaceblobstore/paths/azureml/{job}/model_dir/   -> 된다
  ```
  `job.outputs[name].path` 는 업로드가 분명히 성공한 뒤에도 `null` 을 돌려준다.
  **실패의 증거로 읽으면 안 된다**
- 근거: §34

## <a id="12"></a>12. 코드는 이미지에 구워져 있다

- 원인: `command(code=...)` 의 업로드를 이 스토리지가 거부한다 → 코드를 이미지에 넣었다
- 따라서: **코드를 고쳤으면 이미지가 바뀐 것이다.** `TRAIN_IMAGE` 의 태그를 올린다
- 왜 치명적인가: Azure ML 환경 버전은 **불변**이다. 태그를 재사용하면
  오류 없이 **옛날 스크립트가 돈다**
- 안전장치: `ENVIRONMENT_VERSION = image_tag(TRAIN_IMAGE)` — 손으로 적지 않고 파생시킨다

## <a id="13"></a>13. 재배포가 서빙 중인 엔드포인트를 조용히 죽인다

- 증상: 새 배포를 올렸더니 **기존 배포의 트래픽이 0 이 됐다.** 엔드포인트 URL 이 아무데도 안 간다
- 원인: 엔드포인트 엔티티를 **새로 만들어서** PUT 하면 `traffic` 이 `{}` 로 직렬화되어 맵을 덮어쓴다
- 규칙 두 개:
  1. **PATCH 는 안 된다.** `onlineEndpoints` 의 PATCH 는
     `PartialMinimalTrackedResourceWithIdentity` 에 바인딩되어 `properties` 를 거부한다
  2. **읽어서 고쳐서 PUT 한다.** 새로 만들지 않는다
- 대처: 손으로 ARM 을 치지 말고
  ```bash
  uv run ffsft-deploy shift --endpoint <ep> --to <deployment>
  ```
- 근거: §65. 첫 버전은 PATCH 를 `-o none 2>/dev/null` 로 보내서
  **400 이 사라지고 no-op 이 정상처럼 보였다**

## <a id="14"></a>14. scoringUri 는 한 모양이 아니다

- 기본 추론 서버 배포: `https://<ep>.<region>.inference.ml.azure.com/score`
- **커스텀 이미지로 OpenAI 라우트를 선언한 배포**: `.../v1/chat/completions`
- 함정: 두 번째에 `/chat/completions` 를 덧붙이면 404 가 나는데,
  그 **본문이 JSON 으로 파싱된다.** 아래 단계에서는 "빈 응답" 으로 읽힌다 —
  **멀쩡한 엔드포인트가 죽은 것처럼 보인다**
- 대처: `scripts/_common.sh` 의 `ffsft_scoring_base` 가 `/score`,
  `/chat/completions`, `/completions` 를 전부 떼어낸다

## <a id="15"></a>15. 사고 토큰 필드는 `reasoning` 이다

- 증상: thinking 을 켰는데 사고 토큰이 0 개로 집계된다
- 실측: SSE 프레임 4921개 중 **4920개가 `reasoning`**, `reasoning_content` 는 **0개**
- 왜 오래 안 잡혔나: **목 서버와 클라이언트가 같은 오타를 공유했다.**
  테스트는 녹색이고 실서버에서만 0 이 나온다 — 이 리포에서 제일 비쌌던 버그 종류
- 대처: `python -m ffsft.serve.smoke` 로 응답을 읽는다.
  `reasoning` 을 먼저 보고 `reasoning_content` 는 구버전 서버용 폴백
- 곁가지: 파서가 없으면(`REASONING_PARSER` 미설정) 사고 흔적이 **`content` 안으로 샌다.**
  200 이고 응답도 긴데 내용이 영어 혼잣말이다
- 근거: §67 → §68

## <a id="16"></a>16. thinking 을 켜면 토큰 예산이 사라진다

- 실측: 프롬프트는 40토큰, 완성은 **4908토큰** (사고 12,238자 / 답 593자)
- 결과: `max_tokens` 를 작게 잡으면 **사고가 예산을 다 먹고 `content` 가 빈 채로 끝난다.**
  200 OK, `finish_reason: length`, 답 0자
- 대처: `enable_thinking` 을 켤 거면 `max_tokens` 를 넉넉히. smoke 가
  `budget used 108/400 (27%)` 처럼 소진율을 찍는다
- 벤치 주의: thinking ON/OFF 는 **다른 눈금자**다. 이 리포의 189 tok/s 는 OFF 측정치
- 근거: §67, §55

## <a id="17"></a>17. 클러스터를 다시 만들면 권한이 둘 다 날아간다

- 증상: 잘 되던 클러스터를 재생성했더니 이미지 pull 또는 스토리지 접근이 깨진다
- 원인: 관리 ID 가 새로 발급된다. 필요한 역할이 **하나가 아니라 둘**이다
  - ACR `AcrPull` — 이미지를 못 받으면 배포가 죽는다
  - 스토리지 `Storage Blob Data Reader/Contributor` — 마운트가 죽는다
- 대처: 재생성 후 두 역할을 **다시** 부여한다. `ffsft-deploy check --probe` 로 확인
- 근거: §60

## <a id="18"></a>18. 실패한 배포도 과금된다

- 실측 누수: 삭제한 VM 이 남긴 디스크·IP 로 **$41.66/월** — 아무 일도 안 하는 리소스에
- 관리형 온라인 엔드포인트는 **놀고 있어도 정가**다 (NV36 기준 ~$103/일)
- **실패한 배포는 지워야 한다.** `Failed` 상태로 남아 있어도 노드는 잡혀 있다
- 대처: 모든 Lab 끝에 정리 단계가 있다. 워크샵이 끝나면
  ```bash
  uv run ffsft-lifecycle status          # 지금 뭐가 돈을 쓰고 있나
  uv run ffsft-lifecycle down --all --yes
  ```
- 주의: `down` 은 고아 디스크·IP 를 **알려주기만 하고 지우지 않는다.**
  그건 사람이 결정할 일이다 — 위 $41.66 이 그 이유
- 근거: §11, §13

---

## 한 줄 요약

- 막히면 **코드보다 축을 의심한다** — 리전, 리전 내 restrictions, 계정 프로필 순으로
- **"성공" 은 증거가 아니다** — 잡이 Completed 인 것, 자산이 등록된 것, 200 이 온 것 전부
- **테스트가 녹색인데 실서버가 0** 이면 목과 클라이언트가 같은 가정을 공유했는지 본다
- **올렸으면 내린다**
