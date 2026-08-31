# Lab 7 — 반드시 내리기

> **Track B 의 마지막 · 풀사이클의 마지막 · 선행: GPU 를 켠 아무 Lab**
> **이 Lab 을 건너뛰면 워크샵이 끝난 뒤에도 계속 청구됩니다.**

> ## 🧭 이 Lab 은 두 단계입니다 — **끄기**, 그다음 **없애기**
> | 단계 | 명령 | 무엇이 사라지나 |
> |---|---|---|
> | §3 끄기 | `ffsft lifecycle down` | 엔드포인트·배포. 그룹·스토리지·ACR 은 **남습니다** |
> | §7 없애기 | `ffsft infra down` | **리소스 그룹째** — 워크샵이 만든 전부 |
>
> 다음 회차에 이어서 할 거면 §3 에서 멈추세요 — ACR 이미지 빌드 25분을 아낍니다.
> **워크샵을 끝낼 거면 §7 까지 가야 합니다.** §3 만 하고 끝내면 스토리지·Key Vault·
> ACR·Log Analytics 가 남고, 그건 시간당 몇 센트라서 **아무도 눈치채지 못한 채로
> 몇 달** 갑니다.
>
> **프로필은 하나입니다.** [Lab 0 §4](lab0.md) 의 `ffsft infra up` 이 리소스 그룹 하나에
> 전부 넣었고, `~/.ffsft-env` 가 그걸 가리킵니다. 이 Lab 의 모든 명령을 돌리기 전에:
>
> ```bash
> source ~/.ffsft-env
> ```
>
> `ffsft lifecycle status` 는 **플래그가 하나도 없습니다** (`status --help` → `[-h]` 뿐).
> 워크스페이스는 오직 환경변수로 정해집니다 (`AzureTarget.from_env()`). 출력은 표 위에
> `LOOKED IN: workspace … / resource group … / subscription …` 헤더를 찍으므로 (§73.3),
> **어느 워크스페이스 얘기인지는 화면에 적혀 있습니다** — 다만 **조회가 끝난 뒤에**
> 적힙니다. 프로필을 먼저 싣는 이유입니다.
>
> **왜 그룹이 하나여야 했는지가 여기서 드러납니다.** 이 리포는 한때 학습을
> koreacentral 그룹에, 서빙을 polandcentral 그룹에 뒀습니다. 그러면 이 Lab 이
> "두 프로필을 도는 루프"가 되고, 루프가 한 바퀴만 돌면 화면에는 그쪽의
> `BILLING NOW: nothing` 만 남습니다 — **반대편에서 $4.959/시가 돌고 있어도 이 화면에
> 등장조차 안 합니다.** 그룹을 하나로 만든 것은 그 루프를 없애기 위해서입니다.

> ## 🚦 먼저 순서 — 여기 올 차례가 맞습니까
> [Lab 6](lab6.md) 은 **갈림길 표**로 끝납니다 — "여기서 끝"이면 Lab 7, "파인튜닝한 가중치까지
> 서빙"이면 [Lab 8](lab8.md). 그 아래 두 줄이 **"전체 순서는 0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → 7"**
> 과 **"어느 쪽이든 마지막은 Lab 7"** 입니다. 그 "마지막"은 **끝나는 지점**이지 **지금 차례**가
> 아닙니다. 표에서 "여기서 끝"을 고른 경우에만 Lab 6 다음이 여기입니다.
> 풀사이클이면 **Lab 6 다음은 [Lab 8](lab8.md)** 이고, Lab 7 은 그 뒤입니다.
>
> | 트랙 | 순서 | Lab 7 은 |
> |---|---|---|
> | Track A (파인튜닝) | 0 → 1 → 2 → 3 | GPU 를 켠 적이 있으면 그 직후 |
> | Track B (배포) | 0 → 4 → 5 → 6 → **7** | Lab 6 직후, **즉시** |
> | 풀사이클 | 0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → **7** | **맨 마지막** |
>
> **풀사이클인데 여기서 먼저 내리면 Lab 8 이 시작을 못 합니다.** Lab 8 은 Lab 5 가 띄운
> `blue` 위에서 blue/green 전환을 하므로, 지우고 나면 24분짜리 배포를 다시 해야 합니다
> ($4.959/시 × 24분 ≈ $2 를 아무 산출물 없이 다시 내는 것).

> ## 💰 이 Lab 은 돈을 **줄이는** 단계입니다
> 아무것도 안 하면 A100 엔드포인트 하나가 하루 **$119** 입니다 ($4.959/시 × 24).

## 목표

- **어떤 리소스가 유휴 상태에서도 돈을 쓰는지** 비대칭을 외운다
- 비용 표에서 **`?` 는 "공짜"가 아니라 "직접 확인하라"** 임을 안다
- `BILLING NOW: nothing` 을 실제로 본다 — 그리고 그게 **그룹이 비었다는 뜻이 아님**을 안다
- 자동으로 안 지워지는 것들(고아 디스크·IP)을 스스로 판단한다
- `ffsft infra down` 으로 **Lab 0 이 만든 그룹을 통째로 되돌리고**, 다음 회차에 같은
  prefix 로 다시 올라오는 것까지 확인한다

## 소요·비용

**15분** (§3~§6) **+ 5분** (§7 그룹 삭제), GPU 과금 없음 — 줄이는 단계입니다.
다만 실패한 배포를 내리면 40분까지 걸릴 수 있고 **그 40분도 과금됩니다** (§3).

---

## 1. 비대칭을 먼저 외운다

| 리소스 | 유휴 비용 | 내려야 하나 |
|---|---|---|
| **관리형 온라인 엔드포인트** | **전액** | **예 — 항상** |
| 배치 엔드포인트 | 없음 | 아니오 (클러스터가 0 으로 축소) |
| AmlCompute `min_instances=0` | 없음 | 아니오 |
| AmlCompute `min_instances>0` | 전액 | 예 |
| ACR 이미지 스토리지 | **`?`** — 이 리포에 요율 없음 (§2.1 의 `?`. 0 이 아닙니다) | 선택 |
| blob 에 등록된 모델 | **`?`** — 이 리포에 요율 없음 (§2.1 의 `?`. 0 이 아닙니다) | 선택 |

**관리형 온라인 엔드포인트에는 scale-to-zero 가 없습니다.** 요청이 0 이어도 24시간
전액입니다. 그리고 **찾아보지 않으면 보이지 않습니다** — 이 리포 최대의 비용 리스크입니다.

**`Failed` 도 마찬가지입니다.** 상태가 실패라고 과금이 멈추지 않습니다 —
이번 회차 테어다운에서 `Failed` 로 남아 있던 `NV36ads_A10_v5` 배포 하나가
**$4.320/시** 로 계속 과금되고 있었습니다 (단가 §6, 쿼터까지 같이 잡고 있던 건 §56.2).
"증거로 남겨두는" 실패한 배포는 **시간당 $4.32 짜리 증거**입니다. → [#18](../GOTCHAS.md#18)

## 2. 지금 뭐가 돈을 쓰고 있나

```bash
source ~/.ffsft-env
uv run ffsft lifecycle status
```

배너가 여러분의 그룹을 말하는지 먼저 보세요 — `profile: ffsft  rg=rg-ffsft-<본인> …`.
배너가 안 찍히면 프로필이 안 실린 셸이고, 그 셸의 `status` 는 저자들의 기본값
(`rg-ffsft-kc` / `mlw-ffsft`)을 조회합니다.

> 아래 블록에서 `==== ` 줄은 저자들 화면의 프로필 배너 자리이고, 그 아래는
> [`PERFORMANCE.md §13`](../PERFORMANCE.md) 의 **실측 `status` 출력을 그대로 잘라 온
> 것**입니다 (기대 출력은 실측에서 잘라 오는 것이지 지어내는 것이 아닙니다). 표
> **위에** `LOOKED IN: workspace … / resource group … / subscription …` 헤더 여섯 줄이
> 오는 것이 지금 코드입니다 (§73.3, `format_inventory` → `scope_lines`). 이름과 가려
> 둔 구독 id 는 **저자들 값**이고 여러분 화면에는 **여러분의 실제 구독 id** 가 찍힙니다.
> `LOOKED IN` 줄이 안 보이면 구판 코드입니다.
>
> 헤더가 붙은 지금은 **확인이 하나 늘었습니다**: `LOOKED IN` 워크스페이스가
> `source` 직후 배너의 `ws=` 와 같아야 합니다. 다르면 프로필에 실린 값과 실제로
> 조회된 곳이 어긋난 것입니다.

```
==== /home/you/.ffsft-env  rg=rg-ffsft-kc  ws=mlw-ffsft  loc=koreacentral

LOOKED IN: workspace mlw-ffsft   resource group rg-ffsft-kc
           subscription <your-subscription-id>
           that triple is what get_ml_client sends, and it scopes every row that came back
           through it. LEFTOVERS does not: it is a separate ARM scan of resource group rg-ffsft-kc,
           same subscription, no workspace. FFSFT_LOCATION=koreacentral is sent by neither, so it
           does not scope this read and cannot explain a missing resource.

KIND                 NAME                               SKU                            $/hr  NOTE
------------------------------------------------------------------------------------------------------------------------------------
  compute-cluster    gpu-a100-lp                        Standard_NC24ads_A100_v4          -  min_instances=0 (low_priority): idle costs nothing
------------------------------------------------------------------------------------------------------------------------------------
BILLING NOW: nothing. No always-on compute in this workspace.
```

**`BILLING NOW: nothing` 이 §3 의 목표 상태입니다.** 그런데 그건 *이 워크스페이스에서*
계량되는 컴퓨트가 없다는 뜻이지 **그룹이 비었다**는 뜻이 아닙니다 — 스토리지·ACR·
Key Vault 는 워크스페이스 리소스가 아니라 이 표에 아예 안 나옵니다. 그룹까지 비우는
것은 §7 입니다.

> 표가 안 보이고 Azure SDK 로그 수백 줄만 스크롤된다면 옛 버전입니다. 지금은 SDK 의
> INFO 를 눌러 표만 남깁니다. 배포가 실패해서 **HTTP 덤프가 필요할 때만**
> `FFSFT_VERBOSE_AZURE=1 uv run ffsft lifecycle status` 로 되돌리세요.
> 끄기만 되는 스위치는, 엔드포인트를 지운 뒤에 로그가 없다는 걸 알게 되는 방식입니다.

### 2.1 `$/hr` 칸의 `?` 는 **공짜가 아니라 미확인**입니다

> ⚠️ **바로 아래 블록은 구판 출력이고, 지금 빌드로 다시 잰 적이 없습니다.**
> `LOOKED IN:` 헤더 여섯 줄이 빠져 있습니다. §2 블록은 이번 라운드에 실측 캡처로
> 교체되어 헤더가 붙었으므로, 두 블록이 달라 보이는 것은 정상입니다 (§75).
> 그 다음 블록들은 합계 줄만 잘라 둔 것이라 헤더와 무관합니다.
>
> **왜 실측으로 못 바꿨나.** [`PERFORMANCE.md §13`](../PERFORMANCE.md) 의 실측은
> **엔드포인트가 하나도 없는** 유휴 워크스페이스를 찍은 것이라, `!!` 두 줄이 도는
> **이 화면의 상태를 잰 것이 아닙니다.** 그 캡처를 여기 붙이면 자르는 게 아니라
> 지어내는 것이라 안 붙였습니다. **이 절에서 읽을 것은 `$/hr` 칸과 합계 줄이고,
> 그 부분은 지금 코드와 같습니다.**

```
KIND                 NAME                               SKU                            $/hr  NOTE
------------------------------------------------------------------------------------------------------------------------------------
!!online-deployment  ffsft-qwen/blue                    Standard_NV6ads_A10_v5        0.613  managed online endpoint: NO scale-to-zero, bills 24/7
!!online-deployment  ffsft-qwen/green                   Standard_D8s_v5                   ?  managed online endpoint: NO scale-to-zero, bills 24/7 (price unknown for this SKU)
------------------------------------------------------------------------------------------------------------------------------------
BILLING NOW: 2 resource(s)  $0.613/hr  ~$447/month if left running
  the total EXCLUDES 1 resource(s) whose rate is unknown: ffsft-qwen/green [Standard_D8s_v5]
```

읽는 법 세 가지.

- **`!!` 는 "지금 과금 중"** 입니다. 가격을 알든 모르든 상관없습니다
- **`?` 는 이 도구가 그 SKU 의 단가를 안 갖고 있다는 뜻**입니다. `-` (진짜 무과금) 와
  다른 글자를 쓰는 이유가 이것입니다
- **합계는 `?` 를 빼고 낸 값**이고, 뺀 것을 **이름으로** 지목합니다. `$0.613/hr` 은
  실제 지출의 **하한**이지 전부가 아닙니다

가격을 아는 리소스가 하나도 없으면 합계 줄 자체가 달라집니다 — 달러 표시가 아예 없습니다.

```
BILLING NOW: 1 resource(s)  cost UNKNOWN -- no rate for any of them, which is not the same as free
  the total EXCLUDES 1 resource(s) whose rate is unknown: ffsft-qwen/blue [Standard_D8s_v5]
```

`down` 쪽도 같은 규칙입니다: `stops an UNKNOWN amount per hour; EXCLUDES 1 resource(s)
whose rate is unknown: ffsft-qwen/blue [Standard_D8s_v5]`.

> ### 이 화면이 왜 이렇게 생겼나
> 예전에는 같은 자리에 **`$0.000/hr ~$0/month`** 가 찍혔습니다. 방금 `BILLING NOW` 라고
> 선언한 리소스의 비용을 0 으로 보고한 것입니다 — 살아 있는 `Standard_NV6ads_A10_v5`
> 위에서 실제로 관측됐습니다. 단가 표에 없는 SKU 라 `0.0` 이 돌아왔고, 그 `0.0` 이
> "무료" 와 구분되지 않았습니다.
>
> **`?` 를 보면 할 일은 "무시" 가 아니라 "확인"입니다.** Azure Portal 의 Cost analysis
> 나 소매 가격 API 로 그 SKU 단가를 직접 보세요. 그 전까지 그 리소스는
> **얼마인지 모르는 채로 24시간 돌고 있는 것**입니다.
>
> 비용 리포트의 지어낸 숫자는 없는 것보다 나쁩니다 — 믿어버리니까요.
> 같은 원칙이 디스크 쪽에도 있습니다 (§4 의 `(price unknown for this SKU)`).

현재 단가 표에는 GPU SKU **16개**(T4·A10·A100·H100/ND)의 PAYG 단가가 들어 있습니다.
CPU SKU 와 그 밖의 SKU 는 `?` 로 나옵니다. **Spot / LowPriority 단가는 일부러 안 넣습니다** —
이 표가 값을 매기는 관리형 온라인 엔드포인트는 LowPriority 를 아예 쓸 수 없어서,
싼 티어 단가를 적으면 24시간 도는 유일한 리소스를 과소보고하게 됩니다.

## 3. 내리기

**`down` 도 `status` 와 같은 환경변수로 워크스페이스를 정합니다.** 그러니 **지우기 전에
프로필을 읽으세요.**

**`status` 와 달리 `down` 은 어디를 봤는지 안 적습니다.** 지울 게 있으면
`format_inventory` 를 아예 안 부르고 `will remove:` 목록 다음이 바로 삭제입니다
(`LOOKED IN:` 헤더가 나오는 건 지울 게 없거나 조회가 실패했을 때뿐입니다). 즉 `status`
는 틀린 셸을 **사후에** 알려주지만 `down` 은 그 전에 이미 지웠습니다. 어느 워크스페이스인지는
**치기 전에** 정해져 있어야 하고, 그걸 정하는 것이 위 `source` 한 줄입니다.

```bash
source ~/.ffsft-env
```

### 스코프는 셋이고, **하나는 반드시 골라야 합니다**

```bash
uv run ffsft lifecycle down --endpoint ffsft-lab --yes                      # 엔드포인트 하나 (배포 전부 포함)
uv run ffsft lifecycle down --endpoint ffsft-lab --deployment blue --yes    # 배포 하나만, 엔드포인트는 유지
uv run ffsft lifecycle down --all --yes                                     # 이 워크스페이스의 과금 리소스 전부
```

`down --help` 가 세 플래그를 그대로 설명합니다.

| 플래그 | `--help` 가 말하는 것 |
|---|---|
| `--endpoint E` | `only this endpoint; one of --endpoint/--all is required` |
| `--deployment D` | `only this deployment of --endpoint; the endpoint and any sibling deployments survive` |
| `--all` | `every billing resource in the workspace. Required when --endpoint is not given` |

### 스코프 없는 `down --yes` 는 **거부됩니다**

```bash
uv run ffsft lifecycle down --yes
```

```
down needs a scope: --endpoint NAME for one endpoint, or --all for everything
refusing to guess: --all deletes every billing resource in this workspace
```

종료코드 **2**, 그리고 **Azure 를 부르기 전에** 멈춥니다 — `cmd_down` 의 이 검사는
`get_ml_client` import 보다 위에 있습니다. `--all` 을 안 준 것을 "전부"로 해석하지 않고
**질문으로 되돌리는** 쪽을 골랐습니다.

`--deployment` 를 `--endpoint` 없이 주면 그보다 먼저 거부됩니다.

```
--deployment needs --endpoint: a deployment name alone is ambiguous
```

이 워크샵의 엔드포인트는 전부 `blue` 라는 배포를 갖고 있어서, 이름만으로는 어느
$4.959/시 를 지우는지 정해지지 않기 때문입니다.
자세한 쓰임은 [Lab 8 §7](lab8.md) — blue/green 전환 뒤 blue 만 지우는 자리입니다.

### `--yes` 없이는 계획만 나옵니다

스코프를 준 뒤 `--yes` 를 빼면 **`--all` 이어도** 아무것도 안 지웁니다. `cmd_down` 은
계획을 언제나 `dry_run=True` 로 먼저 만들고, `--yes` 가 없으면 거기서 멈춥니다.
**종료코드는 0 이 아닐 수도 있습니다** — 이 스코프가 기대는 listing 이 하나라도 실패하면
dry run 도 **1** 을 돌려줍니다 (§74). 계획이 못 본 것을 덮고 있는데 0 을 주면 그 0 이
"이게 전부다" 로 읽히기 때문입니다. 마지막 줄이 이렇게 끝납니다.

```
dry run. re-run with --yes to actually delete.
```

먼저 `--yes` 없이 돌려서 **`will remove:` 목록이 지우려던 그것인지** 보는 게 정석입니다.

> ### 실패한 배포를 내리면 오래 걸립니다 — 정상입니다
> §13 실측: DELETE 요청 18:07:57 → 완료 18:48:20, **40분 24초**.
> 그런데 그중 34분은 **아직 돌고 있는 create 오퍼레이션을 기다린 시간**이었습니다.
> create 가 풀리고 6분 만에 삭제가 끝났습니다.
>
> **원래 배포 오퍼레이션이 끝나야 삭제가 시작되고, 그동안 GPU 는 계속 과금됩니다.**
> NV36 기준 그 40분 24초가 **$2.91** 입니다 ($4.320/시 × 0.673시).
> 프리플라이트가 사후 정리보다 훨씬 싼 또 하나의 이유입니다. → [GOTCHAS #18](../GOTCHAS.md#18)

`lifecycle up` 과 `lifecycle down` 은 **역함수**로 설계됐습니다. 다시 만들기 비싼
것(ACR 이미지, 등록된 모델, 학습 클러스터 정의)은 `down` 이 안 지웁니다.
**계량되는 컴퓨트만** 사라집니다. 그 "비싼 것"들까지 없애는 명령은 §7 입니다 —
거기서는 되돌릴 수 없고, 그래서 별도 단계입니다.

## 4. ⚠️ `down` 이 절대 안 건드리는 것 — 고아 리소스

`status` 는 워크스페이스 클라이언트로 조회하므로 **엔드포인트와 클러스터만** 봅니다.
**디스크와 공인 IP 는 워크스페이스 리소스가 아니라 리소스 그룹 리소스**라 구조적으로
안 보입니다. 실제로 새고 있던 것 (§11):

| 리소스 | 상태 | 요금 |
|---|---|---|
| `vm-a10-ffsft_OsDisk_...` | 256 GB Premium_LRS, **Unattached** | **$38.01/월** |
| `vm-a10-ffsftPublicIP` | Standard static IPv4 | **$3.65/월** |

**합계 $41.66/월** — 아무 일도 안 하는 리소스에.

### 공인 IP 가 함정이었습니다

이 IP 는 `ipConfiguration` 이 **정상적으로 붙어 있었습니다.** 그래서
`ipConfiguration is None` 만 보는 검사는 **정상이라고 판정합니다.** 실제로는 그 NIC 의
`virtualMachine` 이 `null` — **이미 삭제된 VM 의 시체에 붙어 있었던 것**입니다.
판정은 **NIC 를 한 단계 더 따라가야** 합니다.

### 그래서 `down` 은 삭제 명령을 출력만 합니다

고아 리소스는 `bills_when_idle=True` 라 리포트에는 뜨지만, `teardown()` 은 의도적으로
**삭제하지 않고 명령만 인쇄**합니다. 디스크 삭제는 되돌릴 수 없고 `up` 이 다시
만들어주지도 않습니다 — **사람이 판단할 일**입니다.

Premium_LRS 가 아닌 디스크는 여기서도 `(price unknown for this SKU)` 로 나옵니다.
§2.1 과 같은 규칙입니다 — **모르는 건 0 이 아니라 모른다고 씁니다.**

고아 리소스는 **리소스그룹**에 있습니다. 그룹이 하나니 조회도 한 번입니다.

```bash
az resource list -g "$FFSFT_RESOURCE_GROUP" \
  --query "[?type=='Microsoft.Compute/disks' || type=='Microsoft.Network/publicIPAddresses'].{n:name,t:type}" \
  -o table
```

> §11 의 $41.66/월 은 **VM 을 지운 rg 에** 남아 있었습니다. 리소스를 지웠다고 그
> 리소스가 만든 디스크가 같이 가지 않습니다.
>
> **§7 까지 갈 거면 이 절은 확인용입니다** — `ffsft infra down` 은 그룹째 지우므로
> 고아 디스크도 같이 갑니다. §3 에서 멈출 거면 여기 나온 것을 손으로 지우세요.

## 5. 확인 — "없음"과 "못 봤음"은 다르다

```
RESULT Microsoft.Compute/disks:            http=200 count=0
RESULT Microsoft.Network/publicIPAddresses: http=200 count=0
```

**`http=200 count=0` 이 중요합니다.** 조회 함수(`read_orphans`)는 **예외를 던지지
않습니다** — 예외를 던지고 죽는 비용 리포트는 아무도 안 돌립니다. 대신 세 목록
(디스크·공인 IP·NIC)을 **따로** 읽습니다. 한 목록이 실패해도 나머지가 읽어 낸 행은
그대로 나오고, 실패한 목록만 `SectionScan` 으로 기록돼 리포트가 어느 목록을 못 읽었는지
이름을 대고 `LEFTOVERS: UNKNOWN` / `COULD NOT LOOK` 을 찍습니다 (§74, §81). 라운드 9 전에는
셋을 한 번에 읽어서, **이미 읽어 낸 디스크까지 형제 목록의 실패에 같이 버려졌습니다**.
즉 `status` 화면에 `COULD NOT LOOK` 이 아예 없으면 조회는 실제로 성공한 것이고, 아래 `az` 대조는
그것을 **워크스페이스 클라이언트 밖에서 한 번 더** 확인하는 자리입니다.
200/0 이면 **진짜로 없는 것**입니다.

> 같은 원리가 리포 전체에 적용됩니다: `classify_log_response` 가 `LogStatus` 를
> 돌려주는 이유도 **"못 봤다"가 "봤는데 없었다"로 보고되지 않게** 하기 위해서입니다.

## 6. 끄기 확인 — **0 인 것을 눈으로**

**여기는 §3 이 끝났다는 확인이지 워크샵의 마지막이 아닙니다** — 마지막은 §7 입니다.

```bash
source ~/.ffsft-env
uv run ffsft lifecycle status
az ml online-endpoint list -g "$FFSFT_RESOURCE_GROUP" -w "$FFSFT_WORKSPACE" -o table
az ml compute list         -g "$FFSFT_RESOURCE_GROUP" -w "$FFSFT_WORKSPACE" -o table
```

> `az ml` 하위 명령에 `-g` / `-w` 를 **명시적으로** 주는 이유: `az configure` 기본값이
> 잡혀 있으면 프로필과 다른 워크스페이스를 조회하고도 조용히 성공합니다. 루프가 찍은
> `==== ` 줄과 조회 대상이 어긋나면 이 확인은 아무 의미가 없습니다.

이렇게 나와야 합니다.

> ⚠️ **아래 블록은 구판 출력이고, 지금 빌드로 다시 잰 적이 없습니다 — 그런데 이
> 블록이 "0" 의 정의라서 제일 중요합니다.** 지금 `status` 는 `==== ` 줄과 `KIND` 줄
> 사이에 `LOOKED IN: workspace … / resource group … / subscription …` 헤더 여섯 줄을
> 찍습니다 (§73.3, `format_inventory` → `scope_lines`). 아래 블록에는 그 헤더가
> 없으니 **헤더를 뺀 나머지**로 읽으세요. `LOOKED IN` 줄이 안 보이면 구판 코드입니다.
>
> **왜 실측으로 못 바꿨나.** [`PERFORMANCE.md §13`](../PERFORMANCE.md) 에 실측
> `status` 출력이 있지만, 그것은 **테어다운을 한 적이 없는** 유휴 워크스페이스를 찍은
> 것입니다. 여기는 **`down` 을 돌린 뒤**의 화면이고, `down` 뒤에는 §4 의 고아
> 디스크·IP 가 남아 `LEFTOVERS:` 블록이 붙을 수 있습니다. 그 캡처에 `LEFTOVERS:` 가
> 없는 것은 "테어다운이 깨끗하다"는 증거가 **아니라** 거기서 테어다운을 한 적이 없다는
> 뜻입니다. 상태가 다른 캡처를 붙이는 것은 잘라 오는 게 아니라 지어내는 것이라
> 안 붙였습니다. 아래에서 실측으로 고친 것은 `(low_priority)` **철자 하나**뿐입니다.

```
==== /home/you/.ffsft-env  rg=rg-ffsft-kc  ws=mlw-ffsft  loc=koreacentral
KIND                 NAME                               SKU                            $/hr  NOTE
------------------------------------------------------------------------------------------------------------------------------------
  compute-cluster    gpu-a100-lp                        Standard_NC24ads_A100_v4          -  min_instances=0 (low_priority): idle costs nothing
------------------------------------------------------------------------------------------------------------------------------------
BILLING NOW: nothing. No always-on compute in this workspace.
```

**이 화면이 "컴퓨트 0" 의 정의입니다.** 다섯 개가 동시에 참이어야 합니다.

- [ ] `LOOKED IN:` 의 워크스페이스·리소스그룹이 `source` 배너의 `ws=`/`rg=` 와 **같음** —
      다르면 아래 판정은 여러분이 보려던 워크스페이스 얘기가 아닙니다
- [ ] `BILLING NOW: nothing` — 이 문장 그대로. `$0.000/hr` 이나 `cost UNKNOWN` 은 0 이 아닙니다
- [ ] `!!` 로 시작하는 줄이 **하나도 없음**
- [ ] `?` 가 찍힌 줄이 **하나도 없음** — 있다면 아직 얼마인지 모르는 게 돌고 있는 겁니다 (§2.1)
- [ ] `LEFTOVERS:` **블록**이 없음 — 콜론까지 보세요. `LOOKED IN:` 헤더에도 "LEFTOVERS does
      not" 이라는 **단어**가 들어 있습니다(그 블록의 스코프가 다르다는 설명이고, 표 위입니다).
      표 아래에 `LEFTOVERS:` 로 시작하는 블록이 있으면 둘 중 하나입니다. 리소스가 실제로
      남아 있거나(§4), 아니면 `LEFTOVERS: UNKNOWN` — **리소스그룹 조회 자체가 실패한 것**이라
      깨끗한 게 아니라 안 본 것입니다 (§74). 뒤쪽이면 `status` 종료코드도 1 입니다

같이 찍은 `az ml` 두 표도:

- [ ] 온라인 엔드포인트 0개
- [ ] 클러스터 `min_instances=0`
- [ ] **실패한 배포도 지웠나** — `Failed` 도 과금됩니다 ([#18](../GOTCHAS.md#18))
- [ ] (선택) ACR 에서 안 쓰는 태그 삭제. Basic 포함 용량은 10 GB

> 여기서 0 이 되는 것은 **계량되는 컴퓨트**입니다. ACR 이미지(~$0.10/GB/월)와 등록된
> 모델(~$0.02/GB/월), 스토리지·Key Vault·Log Analytics 는 **남아 있습니다.**
> 다음 회차에 이어서 할 거면 그게 의도한 잔액입니다 — 이미지 빌드 25분을 아낍니다.
> **워크샵을 끝낼 거면 §7 로 가세요.**

## 7. 워크샵 종료 — 그룹째 없애기

§6 까지는 **끈 것**이고, 여기가 **없애는 것**입니다. 리소스 그룹은 Azure 의 청구
경계이자 삭제 단위라, 그룹 하나가 사라지면 그 안의 전부가 같이 사라집니다 — Lab 0 이
prefix 하나로 그룹 하나를 만든 이유가 이 한 줄을 성립시키기 위해서였습니다. → [GOTCHAS #19](../GOTCHAS.md#19)

**먼저 예행연습.** `--yes` 없이 부르면 아무것도 안 지우고 **지울 것의 이름을 댑니다.**

```bash
uv run ffsft infra down --prefix <본인>
```

```
rg-ffsft-<본인> holds 11 resource(s)
WOULD DELETE resource group rg-ffsft-<본인>
WOULD PURGE  key vault kv<본인><해시>
dry run. re-run with --yes to actually delete.
```

목록이 여러분 것이 맞으면 진짜로 지웁니다. **되돌릴 수 없습니다.**

```bash
uv run ffsft infra down --prefix <본인> --yes
```

```
deleted resource group rg-ffsft-<본인>
purged  key vault kv<본인><해시>
nothing left under prefix <본인>.
```

### 이 명령이 그룹 삭제 **전에** 리소스를 한 번 읽는 이유

Key Vault 는 그룹이 지워져도 **소프트 삭제 상태로 90일 남습니다.** 이름이 계속
점유되므로, 다음 회차에 같은 prefix 로 `infra up` 을 하면 vault 생성이 거부됩니다.
그래서 그룹을 지우기 전에 `az resource list` 로 **vault 이름을 먼저 읽고**, 그룹을
지운 뒤 그 이름들을 `az keyvault purge` 합니다. ARM 의 `uniqueString` 은 로컬에서
다시 계산할 수 없어서, **읽지 못하면 이름을 알 방법이 없습니다.**

그래서 **그 목록 조회가 실패하면 이 명령은 그룹을 지우지 않고 멈춥니다.** 지우고 나면
무엇이 남았는지 물어볼 곳이 사라지기 때문입니다.

### 종료코드가 세 가지인 이유

| 코드 | 뜻 | 해야 할 일 |
|---|---|---|
| `0` | 지웠고, 지워졌다는 것도 **확인했습니다** | 끝 |
| `3` | 지웠는데 **뭔가 남아 있습니다** (그룹이 아직 보이거나 vault 가 무덤에 있음) | 이름이 화면에 있습니다. 손으로 처리하세요 |
| `1` | **조회를 못 했습니다** — 남았는지 아닌지 모릅니다 | 포털에서 직접 확인하세요 |

**`1` 이 `3` 보다 먼저입니다.** 못 읽은 목록이 하나라도 있으면 "찾은 잔여물" 보다
그쪽을 보고합니다. 안 본 곳은 깨끗한 게 아니라 안 본 것이고, 그건 이 리포가 §5 에서
`http=200 count=0` 을 따지는 것과 같은 규칙입니다 — **부호만 뒤집혔습니다.**
다른 Lab 에서는 "빈 목록이 빈 세상의 증거가 아니다" 이고, 여기서는
**"확인 못 한 삭제는 삭제가 아니다"** 입니다.

> ⚠️ **`az group delete` 를 직접 쳐도 됩니다** — 실제로 이 명령이 안에서 그걸 부릅니다.
> 다르게 하는 것은 딱 두 가지입니다: 지우기 전에 vault 이름을 읽어 두는 것,
> 그리고 지운 뒤에 **정말 없어졌는지 다시 보는 것**. 손으로 칠 거면 그 둘도 같이
> 하세요. 특히 vault purge 를 빼먹으면 **오늘은 아무 일도 안 일어나고**,
> 다음 회차 `infra up` 이 4분 뒤에 실패합니다.

### 다음 회차에 다시 올라오는지

이 워크샵의 정의상 **다시 올라와야 합니다** — 고객 계정으로 로그인해서 돌리고
내리고, 다음에 또 돌립니다. 같은 prefix 로 그냥 다시 부르면 됩니다.

```bash
uv run ffsft infra up --prefix <본인> --location <region>
```

ML 워크스페이스는 이 구독에서 소프트 삭제가 꺼져 있어(`softDeleteRetention=None`)
이름이 즉시 재사용됩니다. Key Vault 는 위에서 purge 했으니 역시 재사용됩니다.
**둘 중 하나라도 안 지워졌으면 여기서 이름 충돌로 막히고**, 그게 §7 을 끝까지 한
것과 안 한 것의 차이입니다.

---

## 막히면

| 증상 | 항목 |
|---|---|
| `down` 이 40분째 안 끝남 | §3 — create 가 끝나야 delete 가 시작됩니다 |
| `status` 는 깨끗한데 청구서가 나옴 | §4 — 고아 디스크·IP 는 status 사각지대 |
| 지운 VM 요금이 계속 나옴 | §4 — OS 디스크가 Unattached 로 남습니다 |
| `$/hr` 이 `?` 로 나옴 | §2.1 — 미확인이지 무료가 아닙니다. 직접 단가를 보세요 |
| 배포 하나만 지우고 싶다 | §3 — `down --endpoint X --deployment blue --yes` |
| 실패한 배포를 안 지웠나 | [#18](../GOTCHAS.md#18) — `Failed` 도 시간당 과금 |
| 표가 SDK 로그에 묻힘 | §2 — 지금은 눌려 있습니다. 되돌리려면 `FFSFT_VERBOSE_AZURE=1` |
| `status` 는 깨끗한데 엔드포인트가 살아 있음 | 프로필이 안 실린 셸입니다 — `source ~/.ffsft-env` 후 배너를 보세요 |
| `infra down` 이 종료코드 1 | 조회를 못 한 겁니다. 남았는지 **모르는** 상태이니 포털에서 직접 보세요 (§7) |
| `infra down` 이 종료코드 3 | 뭔가 남았습니다. 화면에 이름이 있습니다 (§7) |
| 다음 회차 `infra up` 이 vault 이름 충돌 | 지난 회차에 purge 를 안 한 겁니다 — `az keyvault purge -n <이름>` (§7) |
| `down --yes` 가 거부됨 | §3 — 스코프(`--endpoint` 또는 `--all`)가 필요합니다. 종료코드 2, 아무것도 안 지웠습니다 |

---

## 다음

- **Track B 로 왔다면 여기가 끝입니다.** 파인튜닝한 가중치를 실제로 서빙까지 하려면
  [Lab 8 — 풀사이클](lab8.md) 로 가되, **§7 을 돌리기 전에** 가야 합니다 — §7 은
  그룹째 지우므로 Lab 8 이 쓸 워크스페이스도 같이 사라집니다.
- **풀사이클(0→1→2→3→4→5→6→8→7)로 왔다면 여기가 워크샵의 마지막입니다.**
  §6 의 체크박스가 다 참이면 **끈 것**이고, §7 의 `nothing left under prefix <본인>.`
  까지 봐야 **없앤 것**입니다. 워크샵 한 판은 `ffsft infra up` 으로 열리고
  `ffsft infra down` 으로 닫힙니다 — 그 두 줄 사이가 이 리포 전부입니다.
