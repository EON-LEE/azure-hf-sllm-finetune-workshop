# Lab 8 — 풀사이클: 병합 → blue/green → 트래픽 전환

> **Track A+B · 선행: [Lab 2](lab2.md) (학습된 어댑터), [Lab 5](lab5.md) (서빙 중인 blue)**
> 이 Lab 이 두 트랙의 이음매입니다. 여기가 없으면 파인튜닝은 갈 곳 없는 어댑터를 만들고,
> 배포는 남의 모델만 서빙합니다.

> ## 🚦 순서 — **Lab 7 보다 먼저입니다**
> 풀사이클 순서는 **0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → 7** 입니다.
> [Lab 6](lab6.md) 의 마지막은 **갈림길 표**입니다 — "여기서 끝"이면 [Lab 7](lab7.md),
> 파인튜닝한 가중치까지 서빙하면 여기입니다. 같은 자리에 "전체 순서는
> 0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → 7" 과 "**어느 쪽이든 마지막은 Lab 7**" 이 같이
> 적혀 있습니다. **마지막이 Lab 7 인 것과 다음이 Lab 7 인 것은 다릅니다.**
> 풀사이클인데 여기 오기 전에 [Lab 7](lab7.md) 을 돌렸다면 **Lab 5 의 `blue` 가 이미
> 없습니다.** 이 Lab 은 그 위에서 green 을 올려 갈아타는 실습이라, 다시 24분짜리 배포부터
> 해야 합니다 ($4.959/시 × 24분 ≈ **$2** 를 산출물 없이 다시 내는 것).
> **[Lab 7](lab7.md) 은 이 Lab 이 끝난 뒤 워크샵의 마지막 순서입니다.**

> ## 🚨 여기서는 A100 이 **두 개** 돕니다
> blue 가 서빙하는 동안 green 을 올리기 때문입니다 — **$4.959 × 2 = $9.918/시**.
> 병합 잡은 별도로 A100 LowPriority 12분 53초, **$0.992/시** 기준 ≈ **$0.21**
> (요율 출처 §72.1. **이전 판의 "약 $1.5/시" 는 요율이 아니라 §23.5 의 잡 1회
> 총액이었습니다** — 철회, §72.4).
> 전환이 끝나면 **blue 를 지우세요** (§7). 배포 단위 삭제 명령이 있습니다.
> ```bash
> uv run ffsft lifecycle down --endpoint ffsft-lab --deployment blue --yes
> ```
> 이 한 줄이 시간당 $9.918 을 $4.959 로 되돌립니다. 미루면 **1시간에 $4.96,
> 하룻밤(12시간)에 $59.5, 하루에 $119** 가 안 쓰는 배포에 붙습니다.
> 그리고 워크샵이 끝나면 반드시 [Lab 7 (내리기)](lab7.md).

## 목표

- 학습한 LoRA 어댑터가 **실제로 서빙되는 가중치**가 되는 전 구간을 한 번에 통과한다
- blue/green — 서비스를 끊지 않고 새 가중치로 갈아탄다
- **"성공했다"는 상태값이 두 번 거짓말하는 것**을 직접 본다
  (병합 산출물의 텐서 이름, 엔드포인트의 트래픽 맵)

## 선행조건

- Lab 2 의 학습 잡이 `Completed` 이고 `verify_output_path.py` 가 통과한 상태
  — **`Completed` 만으로는 부족합니다** ([GOTCHAS #7](../GOTCHAS.md#7))
- Lab 5 의 엔드포인트(`ffsft-lab`)가 **아직 살아 있고** `blue` 가 서빙 중
  (`--hf-model` 베이스여도 됩니다 — 오히려 좋은 비교 대상입니다).
  `uv run ffsft lifecycle status` 에 `!!online-deployment ffsft-lab/blue` 줄이 보여야
  시작할 수 있습니다. 안 보이면 [Lab 5](lab5.md) 를 다시 하세요
- **Lab 4 의 서빙 이미지가 ACR 에 있고, 그 참조 한 줄을 손에 들고 있음**
  (`$FFSFT_ACR.azurecr.io/ffsft-serve:1`) — green 은 **§3 에서 `--image` 로 그 값을
  받습니다.** 없으면 §3 이 저자들의 사설 ACR 로 내려가 pull 이 실패합니다
- `source ~/.ffsft-env` — **Lab 0 에서 만든 셸 하나로 §1 부터 §7 까지 갑니다.**
  프로필을 바꾸는 단계는 없습니다 (§0)
- 엔드포인트 시스템 할당 MSI 에 **§3 의 가중치가 놓인 스토리지 계정**의
  `Storage Blob Data Reader`. 그룹이 하나면 그건 **자기 워크스페이스**의 계정이라
  보통 이미 붙어 있습니다 — §62.6 이 잰 것이 그 경우이고, **계정 하나**입니다.
  **워크스페이스를 갈라 돌렸다면 그 실측은 이 배포를 안 덮습니다**: §3 의
  「이 URI 를 읽을 권한은 **보통 이미 붙어 있습니다**」를 배포 전에 읽으세요

## 소요·비용

**약 79분** = 등록 2분(**추정**) + 병합 12분 53초(실측) + green 배포 24분(실측)
+ 검증·전환·로드테스트 40분(**추정**).

| 구간 | 시간 | 도는 것 | 시간당 | 소계 |
|---|---|---|---|---|
| 등록 + 병합 대기 | 15분 (등록 2분 **추정** + 병합 13분 실측) | blue 1대 | $4.959 | $1.24 |
| 병합 잡 (별도, 동시에 돎) | 12분 53초 (실측 §66) | A100 LowPriority | $0.992 | 약 $0.21 |
| green 배포 ~ 로드테스트 | 64분 (배포 24분 실측 + 그 뒤 40분 **추정**) | **A100 2대** | $9.918 | $10.58 |
| **합계** | **약 79분** | | | **약 $12** |

> **네 구간 중 둘만 실측입니다.** 병합 12분 53초와 배포 24분은
> [PERFORMANCE.md §9](../PERFORMANCE.md)(§66) 에서 왔습니다. **등록 2분과
> 검증·전환·로드테스트 40분은 이 리포에 잰 기록이 없는 계획용 추정치**이고,
> 여러분의 값은 다를 수 있습니다 — 길어지면 소계도 같이 늘어납니다.
> 요율이 아니라 시간이지만 규칙은 같습니다: **모르는 값은 모른다고 적습니다**
> ([Lab 7 §2.1](lab7.md) 의 `?`).
>
> 병합 잡 줄만 요율이 다릅니다 — **LowPriority $0.992/시**, `Standard_NC24ads_A100_v4`,
> Retail Prices API (koreacentral, 2026-08-27 조회, §72.1). 나머지 두 줄은 관리형
> 온라인 엔드포인트라 **LowPriority 를 쓸 수 없어** PAYG $4.959 입니다 (§71.2).
> 소계: 0.25시간 × $4.959 = $1.24 · 0.2147시간 × $0.992 = $0.21 ·
> 1.0667시간 × $9.918 = $10.58. 합계 $12.03 → 약 $12.

`blue` 는 Lab 5 에서 이미 켜져 있으므로 이 Lab 이 **추가로 켜는 건 green 하나**입니다.
2대 구간을 64분으로 잡은 건 보수적으로 본 값입니다 — green 이 `Succeeded` 가 된 뒤부터
blue 를 지울 때까지를 **40분으로 추정**했고(× $9.918 = $6.61), 그 앞 **24분(실측)**의
배포 구간에도 인스턴스는 여러분 앞으로 할당되어 있습니다. **40분 쪽은 잰 값이 아닙니다** —
청구서 기준으로 계획하세요.

**이 표에 없는 유일한 변수는 §7 을 언제 하느냐입니다.** blue 를 안 지운 채 자고 오면
위 $12 에 하룻밤 $59.5 가 더 붙습니다.

---

## 0. 프로필 — 셸 하나로 끝까지 갑니다

이 Lab 은 두 트랙의 이음매입니다. **그런데 갈아탈 셸이 없습니다** — [Lab 0 §4](lab0.md)
의 `ffsft infra up` 이 학습과 서빙을 **같은 그룹, 같은 리전**에 넣었기 때문입니다.

```bash
source ~/.ffsft-env
```

```
profile: ffsft  rg=rg-ffsft-<본인>  ws=mlw-<본인>  loc=<region>
```

> ⚠️ **배너에 여러분의 prefix 가 안 보이면 이 Lab 의 명령을 하나도 돌리지 마세요.**
> `ffsft lifecycle` 도 `ffsft deploy` 도 워크스페이스를 인자로 받지 않습니다 —
> 환경변수가 유일한 조향 장치입니다 (`AzureTarget.from_env`). 빈 셸에서 `status` 를
> 부르면 저자들의 기본값을 조회하고 **`BILLING NOW: nothing` 을 찍습니다.** 그 문장은
> "아무 데서도 안 돈다" 가 아니라 "**여기서는** 안 돈다" 입니다.

### 이 Lab 이 한 워크스페이스로 끝나야 하는 이유 — 두 축이 걸려 있습니다

이 Lab 의 §1·§2 는 학습 잡의 산출물을 읽고, §3~§7 은 엔드포인트를 만집니다. 그 둘이
다른 워크스페이스에 있으면 **각각 다른 방식으로 깨집니다.**

| 축 | 갈렸을 때 무슨 일이 |
|---|---|
| **경로** | 등록 경로가 `azureml://datastores/workspaceblobstore/paths/azureml/{잡}/model_dir/` 이고, 그 blobstore 는 **학습 잡이 돈 워크스페이스의 것**입니다 (`deploy/model_asset.py::job_output_uri`). 다른 워크스페이스에서 부르면 그 잡도, 그 경로도 없습니다 |
| **시간** | §3 의 `--model-blob-uri` 수신을 이 리포는 **같은 리전에서만** 쟀습니다 — 111초 (§64.5). 리전이 다를 때의 수신 시간은 **아무도 재지 않았습니다.** 레디니스 예산은 **13.25분**이고, 수신이 그걸 넘으면 배포는 24분 뒤에 실패합니다 — **$4.959/시를 쓰면서** |
| **권한** | 엔드포인트 MSI 의 `Storage Blob Data Reader` 가 **가중치가 놓인 스토리지 계정**에 있어야 합니다. 자기 워크스페이스의 계정이면 보통 이미 붙어 있습니다 (§62.6 실측 — **계정 하나** 기준). 갈리면 그 실측이 이 배포를 안 덮습니다 |

그룹이 하나면 세 축이 전부 자동으로 맞습니다. [PERFORMANCE §9](../PERFORMANCE.md) 의
"가중치와 엔드포인트는 같은 리전에 두세요" 가 적힌 자리가 정확히 여기이고,
[Lab 0 §3](lab0.md) 이 리전을 하나만 고르라고 한 이유가 이것입니다.

> **그래도 갈린 상태로 여기 왔다면** — [Lab 0 §3](lab0.md) 의 예외 경로대로 prefix 를
> 둘 만든 경우입니다. 병합 잡을 **엔드포인트 쪽 워크스페이스에서 학습된 어댑터**로
> 돌리거나, 산출물을 그쪽 스토리지로 복사해 그 URL 을 §3 에 주는 수밖에 없습니다.
> **둘 다 이 워크샵이 실측한 적 없는 경로라 여기에 명령을 적지 않습니다.**

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

> ### 이 등록 자체가 거부될 수 있습니다 — `KeyBasedAuthenticationNotPermitted`
> 위 예시는 등록이 성공한다고 가정합니다. **테넌트에 따라서는 그렇지 않습니다.**
> `allowSharedKeyAccess=false` 인 스토리지 계정(관리 그룹 스코프 `modify` 정책 — RG
> 스코프 정책 목록에는 안 보입니다)에서는 `register_adapter` 가 예외 없이 이 에러로
> 죽습니다. 원인은 마운트 크리덴셜(`identity` 모드, 이 프리플라이트가 이미 재는 것)과
> 무관합니다 — Model Registry 서비스가 **레거시 `Microsoft.Azure.Storage` SDK 로 계정
> 키를 써서 서버사이드로 blob 을 나열**하기 때문이고, 여기엔 `identity` 모드에 대응하는
> 것이 없습니다 (JOURNAL §62, §100).
>
> 이 계정에서 등록이 막혀 있는지는 위 `verify_output_path.py` 가 성공했는지로는 알 수
> 없습니다 — 그건 마운트가 되는지만 잽니다. 등록이 이 에러로 죽으면, §2 를
> `--adapter` 대신 `--adapter-uri` 로 건너뜁니다:
> ```bash
> uv run python scripts/submit_merge.py --model qwen3.8-27b \
>    --adapter-uri "azureml://datastores/workspaceblobstore/paths/azureml/<run-name>/model_dir/"
> ```
> 등록된 자산을 아예 거치지 않고 학습 잡의 출력을 `uri_folder` 로 직접 마운트합니다 —
> 실측: `purple_room_j4504v814f`, 10분 완주, `merged_size_gb=53.79`, 배포 경로의
> `--model-blob-uri` 와 같은 우회입니다 (JOURNAL §100).

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
ACCT=<워크스페이스의 스토리지 계정>        # §2 의 병합 잡이 쓴 workspaceblobstore
CONT=<azureml-blobstore-...>
URI="https://$ACCT.blob.core.windows.net/$CONT/azureml/<merge-run-name>/merged/"
```

> ### 이 URI 를 읽을 권한은 **보통 이미 붙어 있습니다** — 그룹이 하나이기 때문입니다
> `$ACCT` 는 §1·§2 가 돈 워크스페이스의 계정이고, 그걸 받는 것은 **같은 워크스페이스의**
> 엔드포인트 MSI 입니다. Azure ML 이 자기 워크스페이스의 스토리지에는 롤을 붙여 줍니다 —
> §62.6 이 잰 것이 정확히 그 경우입니다 (`Storage Blob Data Reader`, **계정 하나**).
>
> **워크스페이스가 갈리면 그 실측이 이 배포를 안 덮습니다.** 계정이 다르고 (rg 가 같아도
> 계정은 다릅니다), Azure ML 이 알아서 붙여주는 롤은 **자기 워크스페이스 것뿐**입니다.
> 배포 경로의 신원 프리플라이트도 안 잡습니다: `read_identity_grants` 는 **엔드포인트
> 워크스페이스**의 `properties.storageAccount` 를 스코프로 롤을 읽으므로, 다른 계정에
> 롤이 없다는 사실은 그 검사에 아예 **안 보입니다.**
>
> **이건 추론이지 실측이 아닙니다.** 이 403 을 본 사람은 없습니다 — §62.6 **측정의 범위**
> 에서 나온 **미확인 권한 공백**이고, 관측된 실패가 아닙니다. 그리고 이 실패는 조용합니다:
> `fetch_model.py` 는 실패하면 컨테이너를 죽이고 (그 폴백이 살아 있으면 튜닝 안 된 베이스를
> **건강하게** 서빙하니까), `Creating` 동안 컨테이너 로그는 못 읽습니다
> ([Lab 5 §4](lab5.md)). 화면에서 403 은 §0 표의 "수신이 느림" 과 똑같이 보입니다 —
> 둘 다 24분짜리 롤아웃 끝의 실패입니다.
>
> **[Lab 0 §3](lab0.md) 대로 prefix 하나로 왔으면 아래는 확인용 한 줄입니다.**
> prefix 를 둘 만든 예외 경로면 부여까지 하세요.
>
> ```bash
> PID=$(az ml online-endpoint show -n ffsft-lab \
>         -g $FFSFT_RESOURCE_GROUP -w $FFSFT_WORKSPACE \
>         --query identity.principalId -o tsv)
>
> SA=$(az storage account show -n "$ACCT" -g $FFSFT_RESOURCE_GROUP --query id -o tsv)
>
> # 먼저 봅니다. Storage Blob Data Reader 가 안 보이면 없는 것입니다.
> az role assignment list --assignee "$PID" --scope "$SA" --include-inherited \
>    --query "[].roleDefinitionName" -o tsv
>
> # 없을 때만:
> az role assignment create --assignee-object-id "$PID" \
>   --assignee-principal-type ServicePrincipal \
>   --role "Storage Blob Data Reader" --scope "$SA"
> ```
>
> 모양은 [RUNBOOK §3.3](../RUNBOOK.md) 의 AcrPull 손 부여와 같습니다. 다른 것은 롤
> 이름뿐입니다. 전파도 같습니다: 부여했으면 1~2분 기다린 뒤 배포하세요.
>
> `$ACCT` 를 `-g` 없이 못 찾겠으면 그것부터가 신호입니다 — 스토리지가 여러분 그룹에
> 없다는 뜻이고, [Lab 0 §4](lab0.md) 를 다시 보세요.

```bash
uv run ffsft deploy deploy-online \
   --endpoint ffsft-lab \
   --deployment green \
   --image "$FFSFT_ACR.azurecr.io/ffsft-serve:1" \
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
  스펙이 비면 `--mamba-cache-mode align` 없이 나갑니다. **그게 곧 실패는 아닙니다** —
  §57.5 는 `MAMBA_CACHE_MODE=""` 로 나간 배포가 정상 기동한 것을 기록하고 있고, 참인
  명제는 "**모드 `all` 이 `NotImplementedError` 를 낸다**" 입니다 (vLLM 기본값은 `all` 이
  아닙니다). `align` 은 측정해서 고른 값이니 정상 경로에서는 보내세요. **그 오류가 몇 분째에
  나는지는 이 리포가 잰 적이 없습니다**
- 가중치 출처는 `--model-uri` / `--hf-model` / `--model-blob-uri` 중 **정확히 하나**.
  0개면 vLLM 이 서빙할 게 없고, 2개면 배포가 그 모순을 조용히 자기 방식대로 풉니다
- `--model-uri`(등록된 자산)는 이 테넌트에서 **구조적으로 안 됩니다** (§62.4).
  파인튜닝 가중치가 관리형 엔드포인트에 닿는 길은 컨테이너가 스스로 받는 것뿐입니다
- `--egress-public-network-access` 는 **넘기지 마세요.** 매니지드 VNet 워크스페이스에서
  애저가 값을 아예 안 받고 15초 만에 400 을 돌려줍니다 (§64.2). 코드가 알아서 버립니다
- **`--image` 는 [Lab 5 §3](lab5.md) 에서 blue 에 준 값과 글자 그대로 같아야 합니다.**
  Lab 5 는 이 플래그에 대해 "**이 플래그가 없으면 이 트랙은 여기서 끝납니다**" 라고
  적었고, 그 문장은 green 에도 그대로 적용됩니다. 빼면 `resolve_serve_image()` 가
  상수 `SERVE_IMAGE` = `acrffsftkc.azurecr.io/ffsft-serve:5`(**저자들의 사설 ACR**)
  까지 내려가고, 여러분 구독에서는 pull 이 안 됩니다. 그 실패는 파싱 오류가 아니라
  **노드가 할당된 뒤**에 나므로, **$4.959/시가 도는 롤아웃 도중**에 만납니다
  (성공한 배포의 실측 소요는 **24분** — [PERFORMANCE.md §9](../PERFORMANCE.md), §66.
  **실패가 몇 분째에 나는지는 이 리포가 잰 적이 없습니다**)

> ### 이미지가 손에 없으면 **여기서 멈추세요**
> 두 경우가 있습니다.
>
> - **blue 를 `--image` 로 올렸다** — 그 값을 그대로 쓰면 됩니다. 기억이 안 나면
>   [Lab 4 §6](lab4.md) 이 만든 참조 한 줄(`$FFSFT_ACR.azurecr.io/ffsft-serve:1`)이고,
>   셸마다 `export FFSFT_SERVE_IMAGE=<그 값>` 을 한 번 해두면 blue/green 이 어긋날 수
>   없습니다 (우선순위 `--image` > `$FFSFT_SERVE_IMAGE` > 상수).
> - **Track A 만 해서 [Lab 4](lab4.md) 를 건너뛰었다** — 여러분에게는 **서빙 이미지가
>   없습니다.** 이 Lab 은 Lab 5 의 blue 위에서 도는 실습이므로 사실 Lab 4·5 를 이미
>   지났어야 합니다. Lab 4 로 돌아가세요 — **약 30분(추정), GPU 는 안 켜고**, ACR 스토리지 요금만
>   듭니다 (**단가는 `?` — 이 리포에 없습니다**, [Lab 4 소요·비용](lab4.md)). 여기서
>   이미지 없이 진행하면 24분(실측)과 $2 를 버리고 같은 자리로 돌아옵니다.
>
> **두 배포의 이미지가 다르면 §6 의 비교가 무의미해집니다.** `PERFORMANCE.md` §1 이
> "두 배포는 같은 SKU, 같은 인스턴스 수, **같은 이미지**" 라고 적은 것이 그 표의 전제이고,
> 이미지가 다르면 차이의 원인 후보가 하나 더 늘어납니다.

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
uv run ffsft deploy shift --endpoint ffsft-lab --to green
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

먼저 **엔드포인트 키**입니다. §4 의 `verify_deployment.sh` 는 **자기가 직접** 키를
가져오므로(`scripts/verify_deployment.sh`), 그게 돌았다고 이 변수가 실려 있는 것은
아닙니다. `ffsft loadtest` 는 `$FFSFT_ENDPOINT_KEY` 를 읽습니다
(`src/ffsft/serve/loadtest.py`).

```bash
# `_common.sh` 를 이 셸에 직접 source 하지 않습니다: 그 파일은 스크립트용이라
# `set -u` 를 이 셸에 걸고, 구독이 어긋나면 `exit 1` -- **터미널이 닫힙니다.**
# 서브셸로 부르면 실패는 서브셸에서 끝나고 stdout 으로는 키만 넘어옵니다.
FFSFT_ENDPOINT_KEY="$(bash -c '. scripts/_common.sh; ffsft_endpoint_key "$1"' _ ffsft-lab)"
export FFSFT_ENDPOINT_KEY
[ -n "$FFSFT_ENDPOINT_KEY" ] || echo "key lookup failed -- 위 stderr 를 읽으세요"
echo "key acquired (length ${#FFSFT_ENDPOINT_KEY})"
```

```
key acquired (length <0 이 아닌 수>)
```

> **길이만 봅니다. 0 이 아니면 성공입니다.** 위 기대 출력에 숫자를 안 적은 이유가
> 그것입니다 — **이 리포에는 실제 키 길이를 잰 기록이 없어서** "32여야 한다" 같은
> 문장을 쓸 근거가 없습니다. 0 이면 위의 stderr 에 이유가 적혀 있습니다
> (구독 불일치·엔드포인트 없음·권한).

> ### 왜 파일이 아니라 환경변수이고, 왜 길이만 찍나
> - `primaryKey` 는 **bearer 자격증명**입니다. 이걸 가진 사람은 여러분의
>   **$4.959/시** 엔드포인트를 키를 돌릴 때까지 마음대로 씁니다.
> - `~/.ffsft-env` 계열에 **넣지 마세요.** [Lab 0](lab0.md) 이 "자격증명은 이 파일에
>   없습니다" 라고 약속한 파일이고, 그 약속이 이 파일을 백업하거나 화면에 띄워도
>   되게 만듭니다. 키를 한 줄 넣는 순간 그 약속이 깨집니다.
> - 변수는 셸과 함께 사라집니다. **새 셸이면 이 블록을 다시 돌리세요.**
> - `echo $FFSFT_ENDPOINT_KEY` 를 치지 마세요. 워크샵은 화면 공유·녹화되고 스크롤백에
>   남습니다. **알아야 할 건 "받았나/못 받았나" 하나뿐이고 길이가 그걸 답합니다.**
> - 명령줄(`--api-key <키>`)도 안 됩니다. 셸 히스토리와 `ps` 에 남습니다.
> - 조회 URL 에는 `$FFSFT_RESOURCE_GROUP` 과 `$FFSFT_WORKSPACE` 가 그대로 들어갑니다
>   (`scripts/_common.sh`). **프로필이 틀리면 키가 아니라 404 가 옵니다** — 조용히
>   틀리는 게 아니라 즉시 틀립니다.
> - `bash -c` 는 리포 루트에서 돌려야 합니다 (`scripts/_common.sh` 상대경로).

그리고 엔드포인트 URL 을 읽는 줄에는 워크스페이스를 명시합니다 — `az ml` 은
`az configure --defaults` 를 보는데 이 워크샵은 그걸 설정한 적이 없습니다:

```bash
BASE="$(az ml online-endpoint show -n ffsft-lab \
        -g "$FFSFT_RESOURCE_GROUP" -w "$FFSFT_WORKSPACE" \
        --query scoring_uri -o tsv)"
uv run ffsft loadtest \
   --base-url "${BASE%/chat/completions}" \
   --model ffsft \
   --concurrency 1,2,4,8,16 \
   --requests-per-level 20
```

> `scoringUri` 는 배포 종류에 따라 `.../score` 로도 옵니다. 그 경우 위의
> `${BASE%/chat/completions}` 는 **아무것도 안 벗겨내고**, 붙여 쓴 URL 이 404 를
> JSON 으로 돌려줘 downstream 에서 **"빈 응답"으로 읽힙니다**
> ([GOTCHAS #14](../GOTCHAS.md#14)). `_common.sh` 에 두 모양을 다 벗기는 함수가
> 이미 있고, `az ml` 확장도 필요 없습니다 — 키와 **같은 서브셸 관용구**입니다:
>
> ```bash
> BASE="$(bash -c '. scripts/_common.sh; ffsft_scoring_base "$1"' _ ffsft-lab)"
> ```

실측 비교 (§66.2, 각 100요청 / 실패 0 — 5레벨 전체와 원자료 JSON 은
[`docs/PERFORMANCE.md`](../PERFORMANCE.md)):

```
            blue (베이스)                   green (파인튜닝)
conc   tok/req  TPOT    tok/s  req/s |  tok/req  TPOT    tok/s  req/s
   1    121.8  0.0364    22.0  0.18  |   110.6  0.0363    21.3  0.19
   4    121.8  0.0371    83.0  0.68  |   110.7  0.0370    75.0  0.68
  16    121.8  0.0407   204.3  1.68  |   110.6  0.0407   189.0  1.71
```

peak tok/s 가 204.3 → 189.0 으로 **7.5% 낮습니다. 이걸 성능 저하로 적으면 틀립니다.**

- **TPOT 는 같거나 0.0001 차이입니다** — 위 표의 c=1 이 0.0364 vs 0.0363 인 것이 그
  0.0001 입니다. 5레벨 중 정확히 같은 건 c=16 하나뿐이고 나머지 넷은 0.0001 씩
  엇갈립니다(세 번은 blue 가, 한 번은 green 이 큽니다). **토큰 하나 뽑는 비용은 안
  변했습니다** — 방향이 일정하지 않은 마지막 자리는 차이가 아니라 잡음입니다
- **req/s 는 green 이 오히려 높습니다** (c=16 에서 1.71 vs 1.68)
- 차이는 전부 **응답 길이**입니다 — blue 121.8 vs green 110.6 tok/req.
  **다만 이 회차는 8개 프롬프트 중 blue 6개·green 6개가 `max_tokens=128` 상한에서
  잘렸습니다** — 양쪽 다 잘린 건 5개, blue 만 1개, green 만 1개, 어느 쪽도 안 잘린 건
  1개입니다. 잘린 응답의 토큰 수는 길이가 아니라 상한이므로, 이 격차를
  "파인튜닝이 응답을 짧게 만들었다" 로 읽으면 안 됩니다
  ([PERFORMANCE §6.1](../PERFORMANCE.md) 이 프롬프트 단위로 분해합니다)

**tok/s 는 두 배포가 같은 일을 할 때만 비교 가능한 지표입니다.** 서빙 속도로 읽어야 할
값은 TPOT 와 req/s 이고, 그 둘로 보면 green 은 동등하거나 근소하게 낫습니다.
e2e p95 도 green 이 짧습니다 — c=16 에서 6.471 vs 6.987초.

> 결과를 파일로 남기려면 `--output my-loadtest.json` 을 붙이세요. 이 리포의
> [`docs/results/`](../results/) 에 있는 두 JSON 이 같은 플래그의 산출물입니다.
> `uv run ffsft plot mine=my-loadtest.json` 이 그 JSON 을 그래프로 그립니다.

---

## 7. 롤백은 한 줄이고, **그다음이 blue 를 지우는 순간입니다**

blue 를 지우기 전까지는 되돌리는 데 재배포가 필요 없습니다.

```bash
uv run ffsft deploy shift --endpoint ffsft-lab --to blue
```

이게 blue 를 **띄워둔 채로** 전환하는 이유입니다. 되돌릴 수 있는 40분과, 24분짜리
재배포를 다시 기다리는 것의 차이입니다.

### 그리고 되돌릴 일이 없다고 판단되면 — blue 를 지웁니다

green 이 §4 의 게이트를 통과했고 §6 의 숫자도 납득됐으면, blue 는 **시간당 $4.959 짜리
보험**입니다. 여기가 A100 2개 구간을 끝내는 지점입니다.

```bash
uv run ffsft lifecycle down --endpoint ffsft-lab --deployment blue          # 먼저 계획만
uv run ffsft lifecycle down --endpoint ffsft-lab --deployment blue --yes    # 실제 삭제
```

`--yes` 없이 돌리면 아무것도 지우지 않고 이렇게 끝납니다.

```
will remove:
  - online-deployment ffsft-lab/blue (endpoint ffsft-lab kept)

stops $4.959/hr (~$3,620/month)

dry run. re-run with --yes to actually delete.
```

`(endpoint ffsft-lab kept)` 가 이 명령의 전부입니다. **`--deployment` 없이 돌리면
엔드포인트가 지워지고 green 도 같이 사라집니다** — 방금 24분 들여 올린 그 green 입니다.
`--deployment` 는 `--endpoint` 없이는 거부합니다 (모든 엔드포인트가 `blue` 를 갖고
있으므로 이름만으로는 어느 $4.959/시 인지 정해지지 않습니다).

삭제 뒤 `status` 는 이렇게 되어야 합니다 — **`!!` 줄이 두 개에서 하나로** 줄어듭니다.

> ⚠️ **아래 블록은 구판 출력이고, 지금 빌드로 다시 잰 적이 없습니다.** 지금 `status`
> 는 표 **위에** `LOOKED IN: workspace … / resource group … / subscription …` 헤더
> 여섯 줄을 먼저 찍습니다 (§73.3, `format_inventory` → `scope_lines`). 아래 블록에는
> 그 헤더가 없습니다 — `LOOKED IN` 줄이 안 보이면 구판 코드입니다.
>
> **왜 실측으로 못 바꿨나.** [`PERFORMANCE.md §13`](../PERFORMANCE.md) 의 실측은
> **엔드포인트가 하나도 없는** 유휴 워크스페이스를 찍은 것이고, 여기는 **green 이
> $4.959/시로 돌고 있는** 화면입니다. 상태가 다른 캡처를 갖다 붙이는 것은 잘라 오는 게
> 아니라 지어내는 것이라 안 붙였습니다. 아래에서 실측으로 고친 것은 `(low_priority)`
> **철자 하나**뿐입니다.

```
KIND                 NAME                               SKU                            $/hr  NOTE
------------------------------------------------------------------------------------------------------------------------------------
!!online-deployment  ffsft-lab/green                    Standard_NC24ads_A100_v4      4.959  managed online endpoint: NO scale-to-zero, bills 24/7
  compute-cluster    gpu-a100-lp                        Standard_NC24ads_A100_v4          -  min_instances=0 (low_priority): idle costs nothing
------------------------------------------------------------------------------------------------------------------------------------
BILLING NOW: 1 resource(s)  $4.959/hr  ~$3,620/month if left running
Run `ffsft lifecycle down --all --yes` to stop the meter.
```

> **미루는 값이 정확히 얼마인지 알고 미루세요.** blue 를 그대로 두면 **$4.959/시**,
> 즉 점심 한 번에 $5, 하룻밤(12시간)에 **$59.5**, 하루에 **$119** 입니다.
> 이 Lab 전체 비용이 약 $12 인데, blue 하나를 이틀 잊으면 $238 — **실습 자체의 약 20배**입니다.
> `Failed` 로 끝난 배포도 똑같이 과금됩니다 ([#18](../GOTCHAS.md#18)).

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
| 실패한 배포가 계속 과금된다 | [#18](../GOTCHAS.md#18) — `Failed` 도 시간당 과금 |
| blue 만 지우고 싶다 (엔드포인트는 유지) | §7 — `down --endpoint X --deployment blue --yes` |
| 시작하려는데 `blue` 가 없다 | Lab 7 을 먼저 돌린 경우입니다. 순서는 0→…→6→**8**→7 |
| green 이 가중치를 못 받고 죽는다 (blue 는 멀쩡) | §3 의 권한 박스 — `$ACCT` 에 `Storage Blob Data Reader` 가 있는지 봅니다. 그룹이 하나면 보통 붙어 있습니다. **관측된 적 없는 미확인 공백**입니다 |
| green 이 이미지를 못 받고 실패한다 | §3 — `--image` 를 뺐습니다. 기본값은 **저자들의 사설 ACR** 이라 다른 구독에서 pull 이 안 됩니다 |
| 로드테스트가 `ok` 0 / `fail` 20 줄로만 끝난다 | §6 — 키 블록을 안 돌린 셸입니다. `ffsft loadtest` 는 키가 없으면 `Authorization` 헤더를 **안 붙이고 그대로 돕니다** (거부하지 않습니다) |
| `compare_deployments.py` 가 `no key: pass --api-key or set FFSFT_ENDPOINT_KEY` 로 exit 2 | 같은 원인. §6 의 키 블록 |
| `status` 가 `BILLING NOW: nothing` 인데 청구는 계속된다 | 그 문장은 "**이 워크스페이스에는**" 없다는 뜻입니다. 그룹이 비었다는 뜻이 아닙니다 — [Lab 7 §7](lab7.md) 의 `ffsft infra down` 이 그룹째 봅니다 |

---

## 정리 — 세 단계입니다

**1) blue 를 지웁니다 (§7).** 전환이 끝나고 green 이 검증됐으면 blue 는 더 이상 필요
없습니다. 이걸로 A100 2개 구간이 끝납니다.

```bash
uv run ffsft lifecycle status                                            # 지금 몇 대가 도나
uv run ffsft lifecycle down --endpoint ffsft-lab --deployment blue --yes # 2대 -> 1대
```

**2) 워크샵이 끝났으면 엔드포인트째 내립니다.**

```bash
uv run ffsft lifecycle down --endpoint ffsft-lab --yes                   # 1대 -> 0
```

> 배포 하나를 지우는 것과 엔드포인트를 지우는 것은 다릅니다. 배포만 지우면 엔드포인트는
> 남지만 **엔드포인트 자체는 과금되지 않습니다** — 돈은 배포 인스턴스가 씁니다.
> 그래도 워크샵 끝에는 엔드포인트까지 지우세요. 남아 있으면 다음 사람이 "이미 있네"
> 하고 재배포해서 §65 의 사고를 재현합니다.

**3) 그룹째 없앱니다 — [Lab 7 §7](lab7.md).** 위 두 단계는 **엔드포인트만** 껐습니다.
워크스페이스·스토리지·ACR·KeyVault 는 그대로 있고, `status` 는 그것들을 못 봅니다
(§11 에서 그렇게 **$41.66/월** 이 새고 있었습니다).

```bash
uv run ffsft infra down --prefix <본인> --yes
```

**다음**: [Lab 7 — 반드시 내리기](lab7.md). 풀사이클에서는 **Lab 7 이 마지막 순서**이고,
거기서 고아 디스크·IP 확인부터 그룹 삭제까지 한 번에 끝냅니다. 위 2단계를 여기서
이미 했더라도 Lab 7 은 건너뛰지 마세요 — 워크샵 한 판은 `ffsft infra up` 으로 열리고
`ffsft infra down` 으로 닫힙니다.
