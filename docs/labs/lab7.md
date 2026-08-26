# Lab 7 — 반드시 내리기

> **Track B · 선행: 아무거나 GPU 를 켠 Lab**
> **이 Lab 을 건너뛰면 워크샵이 끝난 뒤에도 계속 청구됩니다.**

> ## 💰 이 Lab 은 돈을 **줄이는** 단계입니다
> 아무것도 안 하면 A100 엔드포인트 하나가 하루 **$119** 입니다.

## 목표

- **어떤 리소스가 유휴 상태에서도 돈을 쓰는지** 비대칭을 외운다
- `BILLING NOW: nothing` 을 실제로 본다
- 자동으로 안 지워지는 것들(고아 디스크·IP)을 스스로 판단한다

## 소요·비용

**15분**. 다만 실패한 배포를 내리면 40분까지 걸릴 수 있습니다 (§3).

---

## 1. 비대칭을 먼저 외운다

| 리소스 | 유휴 비용 | 내려야 하나 |
|---|---|---|
| **관리형 온라인 엔드포인트** | **전액** | **예 — 항상** |
| 배치 엔드포인트 | 없음 | 아니오 (클러스터가 0 으로 축소) |
| AmlCompute `min_instances=0` | 없음 | 아니오 |
| AmlCompute `min_instances>0` | 전액 | 예 |
| ACR 이미지 스토리지 | ~$0.10/GB/월 | 선택 |
| blob 에 등록된 모델 | ~$0.02/GB/월 | 선택 |

**관리형 온라인 엔드포인트에는 scale-to-zero 가 없습니다.** 요청이 0 이어도 24시간
전액입니다. 그리고 **찾아보지 않으면 보이지 않습니다** — 이 리포 최대의 비용 리스크입니다.

## 2. 지금 뭐가 돈을 쓰고 있나

```bash
uv run ffsft-lifecycle status
```

```
KIND                 NAME              SKU                          $/hr  NOTE
  compute-cluster    gpu-a100-lp       Standard_NC24ads_A100_v4        -  min_instances=0: idle costs nothing
BILLING NOW: nothing. No always-on compute in this workspace.
```

**`BILLING NOW: nothing` 이 목표 상태입니다.**

> 가격을 모르는 SKU 는 **추측하지 않고 `unknown` 으로 보고**합니다.
> 비용 리포트의 지어낸 숫자는 없는 것보다 나쁩니다 — 믿어버리니까요.

## 3. 내리기

```bash
uv run ffsft-lifecycle down --endpoint ffsft-lab --yes    # 하나만
uv run ffsft-lifecycle down --all --yes                   # 과금되는 것 전부
```

`--yes` 없이는 계획만 출력합니다.

> ### 실패한 배포를 내리면 오래 걸립니다 — 정상입니다
> §13 실측: DELETE 요청 18:07:57 → 완료 18:48:20, **40분 24초**.
> 그런데 그중 34분은 **아직 돌고 있는 create 오퍼레이션을 기다린 시간**이었습니다.
> create 가 풀리고 6분 만에 삭제가 끝났습니다.
>
> **원래 배포 오퍼레이션이 끝나야 삭제가 시작되고, 그동안 GPU 는 계속 과금됩니다.**
> 프리플라이트가 사후 정리보다 훨씬 싼 또 하나의 이유입니다. → [GOTCHAS #18](../GOTCHAS.md#18)

`up` 과 `down` 은 **역함수**로 설계됐습니다. 다시 만들기 비싼 것(ACR 이미지, 등록된
모델, 학습 클러스터 정의)은 `down` 이 안 지웁니다. **계량되는 컴퓨트만** 사라집니다.

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

```bash
az resource list -g $FFSFT_RESOURCE_GROUP \
   --query "[?type=='Microsoft.Compute/disks' || type=='Microsoft.Network/publicIPAddresses'].{n:name,t:type}" -o table
```

## 5. 확인 — "없음"과 "못 봤음"은 다르다

```
RESULT Microsoft.Compute/disks:            http=200 count=0
RESULT Microsoft.Network/publicIPAddresses: http=200 count=0
```

**`http=200 count=0` 이 중요합니다.** 조회 함수는 어떤 실패에도 `[]` 를 돌려주므로
"고아 없음"이 "인증 실패"를 가리고 있을 수 있습니다. 200/0 이면 **진짜로 없는 것**입니다.

> 같은 원리가 리포 전체에 적용됩니다: `classify_log_response` 가 `LogStatus` 를
> 돌려주는 이유도 **"못 봤다"가 "봤는데 없었다"로 보고되지 않게** 하기 위해서입니다.

## 6. 워크샵 종료 체크리스트

```bash
uv run ffsft-lifecycle status              # BILLING NOW: nothing 인가
az ml online-endpoint list -o table        # 0개인가
az ml compute list -o table                # min_instances 가 전부 0인가
```

- [ ] 온라인 엔드포인트 0개
- [ ] 클러스터 `min_instances=0`
- [ ] 고아 디스크 / 공인 IP 없음
- [ ] **실패한 배포도 지웠나** — 실패해도 과금됩니다 ([#18](../GOTCHAS.md#18))
- [ ] (선택) ACR 에서 안 쓰는 태그 삭제. Basic 포함 용량은 10 GB

---

## 막히면

| 증상 | 항목 |
|---|---|
| `down` 이 40분째 안 끝남 | §3 — create 가 끝나야 delete 가 시작됩니다 |
| `status` 는 깨끗한데 청구서가 나옴 | §4 — 고아 디스크·IP 는 status 사각지대 |
| 지운 VM 요금이 계속 나옴 | §4 — OS 디스크가 Unattached 로 남습니다 |
| 실패한 배포를 안 지웠나 | [#18](../GOTCHAS.md#18) |

**다음**: Track B 는 여기서 끝납니다. 파인튜닝한 모델을 실제로 서빙하려면
[Lab 8 — 풀사이클](lab8.md).
