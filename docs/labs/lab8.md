# Lab 8 — 풀사이클: 병합 → blue/green → 트래픽 전환

> **Track A+B · 선행: [Lab 2](lab2.md) (학습된 어댑터), [Lab 5](lab5.md) (서빙 중인 blue)**
> 이 Lab 이 두 트랙의 이음매입니다. 여기가 없으면 파인튜닝은 갈 곳 없는 어댑터를 만들고,
> 배포는 남의 모델만 서빙합니다.

> ## 🚨 여기서는 A100 이 **두 개** 돕니다
> blue 가 서빙하는 동안 green 을 올리기 때문입니다 — **$4.96 × 2 = $9.92/시**.
> 병합 잡은 별도로 A100 LowPriority 13분 ≈ **$0.5**.
> 전환이 끝나면 **blue 를 지우세요.** 안 지우면 안 쓰는 배포에 하루 $119 가 붙습니다.
> 끝나면 반드시 [Lab 7 (내리기)](lab7.md).

## 목표

- 학습한 LoRA 어댑터가 **실제로 서빙되는 가중치**가 되는 전 구간을 한 번에 통과한다
- blue/green — 서비스를 끊지 않고 새 가중치로 갈아탄다
- **"성공했다"는 상태값이 두 번 거짓말하는 것**을 직접 본다
  (병합 산출물의 텐서 이름, 엔드포인트의 트래픽 맵)

## 선행조건

- Lab 2 의 학습 잡이 `Completed` 이고 `verify_output_path.py` 가 통과한 상태
  — **`Completed` 만으로는 부족합니다** ([GOTCHAS #7](../GOTCHAS.md#7))
- Lab 5 의 엔드포인트가 살아 있고 `blue` 가 서빙 중
  (`--hf-model` 베이스여도 됩니다 — 오히려 좋은 비교 대상입니다)
- 엔드포인트 시스템 할당 MSI 에 워크스페이스 스토리지의 `Storage Blob Data Reader`
  — Azure ML 이 엔드포인트를 만들 때 보통 이미 붙여줍니다 (§62.6 실측: 새로 줄 역할 없음)

## 소요·비용

**90분** — 등록 2분 + 병합 13분 + green 배포 24분 + 검증·전환·로드테스트 약 40분.
그중 A100 2개가 동시에 도는 구간이 약 **40분**.

---

## 1. 어댑터를 모델 자산으로 등록한다

병합 잡은 경로가 아니라 **등록된 자산**을 마운트합니다. 노드는 URI 만으로
`workspaceblobstore` 에 세션을 못 엽니다.

```bash
uv run python - <<'PY'
from ffsft.azure_ml import AzureTarget, get_ml_client
from ffsft.deploy.model_asset import register_adapter

client = get_ml_client(AzureTarget.from_env())
ref = register_adapter(client, "<run-name>", "qwen3.8-27b",
                       base_model="Qwen/Qwen3.8-27B", mix="ko_commercial_safe")
print(ref)          # -> 'qwen3_8-27b-ko-lora:1'
PY
```

두 가지가 여기서 조용히 어긋납니다.

| | |
|---|---|
| 이름의 점 | Azure ML 자산 이름은 영숫자·대시·언더스코어만 받습니다. **레지스트리 키는 거의 다 점을 갖고 있습니다** (`qwen3.8-27b`). `asset_name()` 이 `qwen3_8-27b` 로 치환하고, **되돌릴 수 없으므로** 원래 키를 `model_key` 태그에 보존합니다 |
| 경로 표기 | `azureml://jobs/{job}/outputs/{name}` 은 직관적이지만 `NoMatchingArtifactsFoundFromJob` 으로 거부됩니다. 되는 것은 데이터스토어 경로 `azureml://datastores/workspaceblobstore/paths/azureml/{job}/model_dir/` |

> `job.outputs[name].path` 는 업로드가 **성공한 뒤에도** `null` 을 돌려줍니다.
> 실패의 증거로 읽지 마세요.

**등록은 증거가 아닙니다.** 서비스는 존재하지 않는 폴더를 가리켜도 등록을 받아줍니다.

```bash
uv run python scripts/verify_output_path.py <run-name> --output model_dir
```

Lab 2 에서 이미 했다면 건너뜁니다. 안 했으면 **여기서 하세요** — 다음 단계가 13분짜리
GPU 잡입니다. 자세히는 [GOTCHAS #10](../GOTCHAS.md#10), [#11](../GOTCHAS.md#11).

---

## 2. 병합 — 어댑터를 베이스 가중치에 접는다

노트북에서 할 일이 아닙니다. 27B 에 어댑터를 접으면 bf16 **54 GB** 를 실체화해야 하고,
`device_map="auto"` 가 A100 과 호스트 RAM 에 걸쳐 흘립니다.

```bash
uv run python scripts/submit_merge.py \
   --model qwen3.8-27b \
   --adapter qwen3_8-27b-ko-lora:1
```

**`name:version` 이 강제입니다.** 맨 이름은 거부합니다 — "latest" 는 움직이는 값이고,
읽어본 것과 병합된 것이 달라지면 그 차이는 **서빙되는 가중치**에서 드러납니다.

제출 전에 두 가지를 공짜로 거절합니다.

- 어댑터 자산의 `model_key` 태그가 `--model` 과 다르면 **거부**.
  잘못된 베이스에 병합하면 **어느 층에서도 에러가 안 납니다** — PEFT 는 이름이 겹치는
  모듈에 델타를 얹고, `save_pretrained` 는 성공하고, 첫 신호는 *유창한 헛소리를 서빙하는
  엔드포인트*입니다
- 스토리지 도달성 프리플라이트. 병합은 **마운트하는 잡**이라서, 마운트가 막히면
  `Failed to mount URI ... at mount point ...` 만 남고 진짜 이유는 어디에도 안 적힙니다 (§63)

기대 출력 — 제출된 잡의 신원. **이 잡 이름을 적어두세요.** 3단계의 blob 경로가 이걸로
만들어집니다.

```json
{
  "name": "mighty_pin_ll2vg38n1k",
  "status": "Starting",
  "studio_url": "https://ml.azure.com/runs/mighty_pin_ll2vg38n1k?wsid=...",
  "compute": "gpu-a100-lp",
  "sku": "Standard_NC24ads_A100_v4",
  "priority": "LowPriority",
  "image": "acrffsftkc.azurecr.io/ffsft-train:12",
  "adapter": "qwen3_8-27b-ko-lora:1",
  "base_model": "Qwen/Qwen3.8-27B"
}
```

진행 상황은 `--wait` 말고 `uv run bash scripts/watch_jobs.sh MERGE:<잡이름>` 으로 봅니다
(`--wait` 는 이 워크스페이스에서 성공한 잡에도 `AuthorizationFailure` 로 끝납니다, §19.1).

선언된 출력은 **`merged` 하나뿐입니다.** 스크립트가 그 밖에 쓰는 건 노드와 함께 죽습니다
([GOTCHAS #7](../GOTCHAS.md#7)). 학습 잡에서 어댑터 두 개를 이렇게 잃었습니다.

실측 소요: A100 LowPriority 에서 **12분 53초** (§53.1).

> ### 이 단계가 세 번 실패한 이유 (§49, §53)
> 병합 잡은 세 번 연속 `Completed` 로 끝났고, 세 번 다 배포에서 **글자 그대로 같은**
> 에러가 났습니다.
> ```
> ValueError: There is no module or parameter named 'language_model' in Qwen3_5Model
> ```
> 산출물의 **텐서 이름이 그 옆 `config.json` 과 안 맞았습니다.** 원인은 저장 쪽이었습니다 —
> `from_pretrained` 가 체크포인트→런타임 이름 변환을 `model._weight_conversions` 에
> 기록해 두고, `save_pretrained` 가 기본값 `save_original_format=True` 로 그걸
> **역방향 재생**합니다. 저장 *전* 딕셔너리를 고치는 코드는 850개 중 0개를 리네임했고,
> 매칭됐더라도 그 뒤에 되돌려졌을 것입니다.
>
> 고침은 `save_original_format=False`. **그런데 그것만 믿지 않습니다** — 이 인자는
> 버전에 따라 `**kwargs` 로 들어가고, 모르는 이름은 **거부가 아니라 무시**됩니다.
> 그래서 `assert_servable_names()` 가 저장 직후 **실제 파일**을 읽습니다
> (`model.safetensors.index.json` 의 `weight_map` 키, 단일 파일이면 safetensors 헤더만
> 파싱 — 54 GB 를 읽지 않고 수 KB). 밀리초가 듭니다.
> **없어서 든 비용은 이미지 빌드 15분 + 병합 13분 + 벤치 41분 × 3회.**

---

## 3. green 을 **트래픽 0** 으로 올린다

가중치는 이미지에 굽지 않습니다. 컨테이너가 시작할 때 자기 MSI 로 blob 에서 받습니다.

```bash
ACCT=<your-workspace-storage-account>
CONT=<azureml-blobstore-...>
URI="https://$ACCT.blob.core.windows.net/$CONT/azureml/<merge-run-name>/merged/"

uv run ffsft-deploy deploy-online \
   --endpoint ffsft-lab \
   --deployment green \
   --traffic 0 \
   --model-blob-uri "$URI" \
   --model-key qwen3.8-27b \
   --sku Standard_NC24ads_A100_v4 \
   --max-model-len 8192
```

- **`--traffic 0`** — blue 가 계속 서빙하는 동안 green 이 54 GB 를 받고 vLLM 을 띄웁니다.
  트래픽은 green 이 **자기 라우팅 헤더로** 대답한 뒤에만 옮깁니다
- **`--model-key` 는 CLI 가 강제합니다** (`--model-blob-uri requires --model-key`).
  `--hf-model` 은 repo id 에서 아키텍처를 추론하지만 blob URI 는 **그냥 경로**라 못 합니다.
  스펙이 비면 `--mamba-cache-mode align` 없이 나가고, Qwen3.5/3.8 체크포인트에서 그건
  기본값이 아니라 20분 뒤 vLLM 안에서 나는 `NotImplementedError` 입니다
- 가중치 출처는 `--model-uri` / `--hf-model` / `--model-blob-uri` 중 **정확히 하나**.
  0개면 vLLM 이 서빙할 게 없고, 2개면 배포가 그 모순을 조용히 자기 방식대로 풉니다
- `--model-uri`(등록된 자산)는 이 테넌트에서 **구조적으로 안 됩니다** (§62.4).
  파인튜닝 가중치가 관리형 엔드포인트에 닿는 길은 컨테이너가 스스로 받는 것뿐입니다
- `--egress-public-network-access` 는 **넘기지 마세요.** 매니지드 VNet 워크스페이스에서
  애저가 값을 아예 안 받고 15초 만에 400 을 돌려줍니다 (§64.2). 코드가 알아서 버립니다

기대 출력 — 추론 컨테이너 로그 (실측, §64.5):

```
[serve] fetching model from blob: https://.../merged/
[fetch] 21 blobs, 50.1 GiB
[fetch] 100.0%    460.4 MiB/s  .../model-00002-of-00014.safetensors
[fetch] complete: 50.1 GiB in 111s
[fetch] OK /tmp/ffsft-model
```

**111초.** 레디니스 예산은 `initialDelay PT2M` + `45 × PT15S` = **13.25분**이므로 7분의 1
입니다. 아슬아슬하게 통과한 게 아니라 여유가 큽니다. 다운로드가 예산 *안에서* 벌어지는
이유는 이 배포의 `model` 자산이 `None` 이라 AML 의 storage-initializer 가 아예 안 뜨고,
`fetch_model.py` 가 추론 컨테이너 안에서 돌기 때문입니다.

전체는 blue 와 비슷하게 **약 24분**입니다 (노드 할당 + 20 GB 이미지 풀이 대부분).

---

## 4. 게이트 2 — 트래픽을 **안 옮긴 채로** 검증한다

트래픽 0 인 배포는 엔드포인트 URL 로는 안 보입니다. `azureml-model-deployment` 헤더가
그걸 직접 지목하는 유일한 수단입니다.

```bash
uv run bash scripts/verify_deployment.sh ffsft-lab blue green
```

두 배포를 같이 적으면 **비교**가 됩니다. 실측 (§66.1):

| | `content` 길이 | 트레이스 누출 | 내용 |
|---|---|---|---|
| blue (베이스) | 380 | **누출** | `We need answer user's request: "한국어로 한 문장만…"` (영어 사고과정) |
| green (파인튜닝) | 44 | 없음 | `서울은 한국의 수도로, 현대적인 도시와 전통적인 문화가 공존하는 도시입니다.` |

**검사는 "200 이 왔나"가 아닙니다.** `ffsft.serve.smoke` 가 본문을 읽고 — `content` 안에
남은 사고 트레이스, 빈 응답, 답이 시작되기 전에 잘린 응답에서 실패합니다.
200 OK 만 봤으면 **같은 결함을 그대로 실은 배포가 게이트를 통과했을 것입니다.**

> 비교 기준으로만 적은 배포(여기서는 blue)가 여기서 실패하는 건 **정상입니다.**
> 종료 코드가 아니라 위의 줄들을 읽으세요.

---

## 5. 전환 — 엔드포인트 URL 을 green 으로

```bash
uv run ffsft-deploy shift --endpoint ffsft-lab --to green
```

기대 출력 (실측):

```
... INFO  ffsft.traffic | traffic before: {'blue': 0, 'green': 0}
... INFO  ffsft.traffic | traffic after : {'green': 100, 'blue': 0}
ffsft-lab now routes to green: {'green': 100, 'blue': 0}
```

`before` 가 `{}` 가 아니라 `{'blue': 0, 'green': 0}` 인 데 주목하세요. green 이 자기
이름을 0% 로 끼워 넣었을 뿐이고, **합이 0 이라 그때까지 엔드포인트는 아무것도 서빙하지
않고 있었습니다.** 두 배포 다 `Succeeded` 인 채로.

> ### 배포와 전환이 왜 다른 명령인가 (§65)
> 그 둘이 한 단계에 묶여 있어서 사고가 났기 때문입니다.
>
> - `deploy_online` 은 배포 전에 엔드포인트를 무조건 `begin_create_or_update` 했습니다.
>   그 엔티티는 **애저에서 읽어온 게 아니라 새로 만든 것**이고, 새 엔티티는
>   `traffic` 을 `{}` 로 직렬화합니다. **`{}` 는 생략된 필드가 아닙니다** — "모든 배포에
>   0% 를 보내라"는 명시적 지시입니다. 재배포할 때마다 살아 있던 엔드포인트가 말없이
>   내려갔습니다
> - 애저가 보고하는 상태는 전부 정상이었습니다: 엔드포인트 `Succeeded`, 배포
>   `Succeeded`, 인스턴스 살아 있음·과금 중. **죽은 건 라우팅뿐**이라 요청을 쏴봐야 압니다
> - 지금은 `ensure_endpoint` 가 **없으면 만들고, 있으면 손대지 않습니다**
>   (`get` 이 `ResourceNotFoundError` 를 던질 때만 PUT)
> - 손으로 ARM 을 치지 마세요. `onlineEndpoints` 의 **PATCH 는 태그와 identity 만** 받고
>   `properties` 는 멤버가 아니라서 거부합니다:
>   `Could not find member 'properties' on object of type 'PartialMinimalTrackedResourceWithIdentity'`.
>   첫 버전은 그 400 을 `-o none 2>/dev/null` 로 삼켜서 `before`/`after` 가 같은 것이
>   **무해한 no-op 처럼** 보였습니다
> - 트래픽은 **엔드포인트를 읽어와서** `.traffic` 만 갈아끼우고 PUT 합니다
>
> [GOTCHAS #13](../GOTCHAS.md#13)

---

## 6. 로드테스트 재측정 — 그리고 오독 주의

[Lab 6](lab6.md) 과 같은 명령입니다. 이번엔 **전환 전후를 비교**하려고 돌립니다.

```bash
BASE="$(az ml online-endpoint show -n ffsft-lab --query scoring_uri -o tsv)"
uv run ffsft-loadtest \
   --base-url "${BASE%/chat/completions}" \
   --model ffsft \
   --concurrency 1,2,4,8,16 \
   --requests-per-level 20
```

실측 비교 (§66.2, 각 100요청 / 실패 0 — 5레벨 전체와 원자료 JSON 은
[`docs/RESULTS.md`](../RESULTS.md)):

```
            blue (베이스)                   green (파인튜닝)
conc   tok/req  TPOT    tok/s  req/s |  tok/req  TPOT    tok/s  req/s
   1    121.8  0.0364    22.0  0.18  |   110.6  0.0363    21.3  0.19
   4    121.8  0.0371    83.0  0.68  |   110.7  0.0370    75.0  0.68
  16    121.8  0.0407   204.3  1.68  |   110.6  0.0407   189.0  1.71
```

peak tok/s 가 204.3 → 189.0 으로 **7.5% 낮습니다. 이걸 성능 저하로 적으면 틀립니다.**

- **TPOT 는 소수점 넷째 자리까지 같습니다.** 토큰 하나 뽑는 비용은 안 변했습니다
- **req/s 는 green 이 오히려 높습니다** (c=16 에서 1.71 vs 1.68)
- 차이는 전부 **응답 길이**입니다 — blue 121.8 vs green 110.6 tok/req.
  blue 는 영어 사고과정을 `content` 에 쏟아내서 토큰 수가 부풀어 있었습니다.
  green 은 `REASONING_PARSER=qwen3` 로 그걸 `reasoning` 필드로 빼냅니다
  ([GOTCHAS #15](../GOTCHAS.md#15))

**tok/s 는 두 배포가 같은 일을 할 때만 비교 가능한 지표입니다.** 서빙 속도로 읽어야 할
값은 TPOT 와 req/s 이고, 그 둘로 보면 green 은 동등하거나 근소하게 낫습니다.
e2e p95 도 green 이 짧습니다 — c=16 에서 6.471 vs 6.987초.

> 결과를 파일로 남기려면 `--output my-loadtest.json` 을 붙이세요. 이 리포의
> [`docs/results/`](../results/) 에 있는 두 JSON 이 같은 플래그의 산출물입니다.

---

## 7. 롤백은 한 줄이다

blue 를 지우기 전까지는 되돌리는 데 재배포가 필요 없습니다.

```bash
uv run ffsft-deploy shift --endpoint ffsft-lab --to blue
```

이게 blue 를 **띄워둔 채로** 전환하는 이유입니다. 되돌릴 수 있는 40분과, 24분짜리
재배포를 다시 기다리는 것의 차이입니다.

---

## 막히면

| 증상 | 항목 |
|---|---|
| 자산 등록이 이름을 거부한다 | [#11](../GOTCHAS.md#11) |
| 등록은 됐는데 마운트하니 비어 있다 | [#10](../GOTCHAS.md#10) |
| 배포에서 `no module or parameter named 'language_model'` | §49, §53 — 병합 산출물의 이름 |
| 새 배포를 올렸더니 기존 트래픽이 0 이 됐다 | [#13](../GOTCHAS.md#13) |
| 응답이 오는데 "빈 응답"으로 읽힌다 | [#14](../GOTCHAS.md#14) — scoringUri 모양 |
| 잘 뜬 것 같은데 답이 영어 사고과정이다 | §66.1 — 게이트 2 를 200 으로 통과시키지 마세요 |
| 사고 토큰이 0 으로 집계된다 | [#15](../GOTCHAS.md#15) |
| 실패한 배포가 계속 과금된다 | [#18](../GOTCHAS.md#18) |

---

## 정리 — **여기서 A100 이 두 개 돌고 있습니다**

전환이 끝나고 green 이 검증됐으면 blue 는 더 이상 필요 없습니다.

```bash
uv run ffsft-lifecycle status         # 지금 뭐가 돈을 쓰고 있나
```

그리고 [Lab 7](lab7.md) 로 내립니다. 워크샵이 끝났다면 엔드포인트 전체:

```bash
uv run ffsft-lifecycle down --endpoint ffsft-lab --yes
```

> 배포 하나를 지우는 것과 엔드포인트를 지우는 것은 다릅니다. 배포만 지우면 엔드포인트는
> 남지만 **엔드포인트 자체는 과금되지 않습니다** — 돈은 배포 인스턴스가 씁니다.
> 그래도 워크샵 끝에는 엔드포인트까지 지우세요. 남아 있으면 다음 사람이 "이미 있네"
> 하고 재배포해서 §65 의 사고를 재현합니다.
