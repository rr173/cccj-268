# 路面养护抽签出票与核对工具

一个只依赖 Python 标准库的**离线**工具：负责按已验版资料把给定有理数 `r`
映射成查验点、保留路径证据、在新版资料上回放同一批签号、配对变点签、通知路线变化、
导出自包含核查包；并提供多现场组离线核销同一签的**结果核销账本**
（JSONL 因果记录、分叉/合并/撤回、候选与冲突待核，见第 8 节）。

工具**不产生随机数**。`r` 由抽签单输入；支持 `"p/q"`、十进制精确字符串、整数、
`{"p": ..., "q": ...}` 等形式。所有长度、里程、序号和分母均使用 Python 任意精度整数，
可超过 64 位。

## 1. 快速运行

无需安装依赖：

```bash
cd /workspace/road_lottery
python3 -I road.py --help
```

跑完整示例（会在 `examples/output/` 生成回执、配对单、重排单和审计包）：

```bash
examples/run_demo.sh
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

也可安装后使用 `road-lottery` 命令：

```bash
pip install -e .
road-lottery --help
```

处理不可信目录中的 JSON 时，建议始终使用上面的 `python3 -I /绝对路径/road.py ...`
形式；启动器会自行把项目目录加入 `sys.path`，避免当前目录里的 `json.py` 等文件影响标准库导入。

---

## 2. 输入格式

### 2.1 期初版

示例：`examples/frame_v0.json`

```json
{
  "routes": [
    {"id": "G1", "name": "一号旅游公路"},
    "G2"
  ],
  "segments": [
    {
      "id": "A-01",
      "route": "G1",
      "order": 10,
      "length": 10,
      "forbidden": []
    },
    {
      "id": "B-01",
      "route": "G2",
      "order": 10,
      "length": 10,
      "forbidden": [[2, 4]]
    }
  ]
}
```

规则：

- `routes` 的列表顺序就是固定路线名册与全框路线顺序；期初后不能增删路线或改序。
- 路段字段：
  - `id`：稳定身份，字符串或整数；
  - `route`：必须是名册中的路线；
  - `order`：路线内整数顺位；同顺位按路段 ID 升序；
  - `length`：非负整数，局部整数坐标域为 `[0, length)`；
  - `forbidden`：局部坐标半开区间 `[start, end)`，也可用 `{"start": ..., "end": ...}`。
- 禁入区间可重叠；按并集扣除。相邻整数区间会合并。
- `start < 0` 或 `end > length` 是坏版，直接拒绝，不静默剪裁。
- 期初版路段 ID 重复是坏版，直接拒绝。
- 长度为 0 或被禁入区间全覆盖的“空段”允许存在；其有效权重为 0，但仍保留在登记和排序结构中。

### 2.2 抽签单

支持题意中的“一个公共 `r` + 固定顺序签号”：

```json
{
  "r": "123.../999...",
  "tickets": ["LOT-0001", "LOT-0002"]
}
```

也支持每张签带各自的精确 `r`：

```json
{
  "tickets": [
    {"id": "LOT-0001", "r": "0"},
    {"id": "LOT-0002", "r": "15/28"}
  ]
}
```

两种格式可混用：顶层给公共 `r`，个别签可覆盖。输出严格保持输入顺序。
不同签落到同一点时，回执中仍是两条独立业务签。

设某版有效坐标总数为 `W`，则：

```text
k = floor(r * W) = floor(p * W / q)
```

`k` 是全框从 0 开始的有效坐标序号。全框顺序为：

```text
路线名册顺序 → 路线内 (order, segment_id) → 段内递增有效局部坐标
```

若 `W = 0`，所有签均为 `empty_frame` / 无点；不会回退查询旧版。

### 2.3 更正版

更正只发送增删改项，不发送其余登记：

```json
{
  "base_version": "v0",
  "corrections": [
    {"op": "retire", "id": "A-01"},
    {"op": "add", "segment": { "...完整新登记..." }},
    {"op": "replace", "segment": { "...保持同一 id 的完整新登记..." }}
  ]
}
```

也接受 `update`/`delete`/`remove` 等别名。

- 一次更正中每个路段身份最多一项；重复目标拒绝。
- `add` 要求基准版无此 ID。
- `replace` / `retire` 要求基准版确有此 ID。
- `replace` 可同时改路线、顺位、长度和禁入区间；稳定 ID 必须保持。
- 整批先全部验明，再发布新版本；任何一项不合法都不会产生新版本文件，基准版也不会被修改。
- 可以从任意老版本分叉更正，而不是只能从“最新版”更正。

---

## 3. 常用命令

下面用 `$REPO` 表示离线仓库目录，例如 `/tmp/road-demo-repo`。

### 3.1 初始化、更正、列版本

```bash
python3 -I road.py init \
  --repo "$REPO" \
  --input examples/frame_v0.json \
  --version-id v0

python3 -I road.py correct \
  --repo "$REPO" \
  --base v0 \
  --input examples/correction_retire_a01.json \
  --version-id v1

python3 -I road.py list-versions --repo "$REPO"
```

### 3.2 出票与老版重验

```bash
python3 -I road.py draw \
  --repo "$REPO" --version v0 \
  --tickets examples/tickets_varied.json \
  --out receipts_v0.json

# 按签号从老回执取 r，再指定老版本重算：
python3 -I road.py query \
  --repo "$REPO" --version v0 \
  --ticket-id LOT-2026-0005 \
  --receipts receipts_v0.json

# 整份回执重算：
python3 -I road.py verify-receipts \
  --repo "$REPO" --receipts receipts_v0.json
```

### 3.3 新旧回放与配对

```bash
python3 -I road.py pair \
  --repo "$REPO" \
  --old v0 --new v1 \
  --tickets examples/tickets_varied.json \
  --out pair.json \
  --rearrange-out rearrange_orders.json
```

如果现场已有两版分别保存的回执，也可以完全不访问仓库、直接配对：

```bash
python3 -I road.py pair-receipts \
  --old-receipts examples/output/receipts_v0.json \
  --new-receipts examples/output/receipts_v1_after_retire.json \
  --out pair.json
```

配对单中的每一项都内嵌完整的 `old_receipt` 和 `new_receipt`。
老票证据来自老版本计算或老回执，不会用新版当前段反查补造。

配对类别：

| `change` | 含义 | 现场动作 |
|---|---|---|
| `unchanged` | 段身份与局部坐标均相同 | 不重查 |
| `empty_both` | 两版均为空框 | 无点 |
| `empty_to_point` | 老版空框，新版恢复有点 | 按新点重排/查验 |
| `point_to_empty` | 老版有点，新版失去点 | 撤旧点/按业务规则处理 |
| `segment_changed` | 两版都有点，但稳定段身份改变 | 按新点重排 |
| `intra_segment_moved` | 同一段身份，但局部坐标改变 | 按新点重排 |

若段身份和局部坐标不变，即使 `route_id` 改变，也判定为物理点不变、不要求重查；
配对项中 `route_changed=true`，并进入顶层 `route_only_notices`。
顺位或登记哈希变化分别记录为 `order_changed`、`registration_changed`。

### 3.4 自包含审计包

```bash
python3 -I road.py audit \
  --repo "$REPO" --version v0 \
  --tickets examples/tickets_varied.json \
  --out audit_v0.json

python3 -I road.py verify-audit --input audit_v0.json
```

审计包含有：

- 固定路线名册及哈希；
- 该版 manifest、总有效坐标数 `W`、段数；
- 可达的持久化索引节点；
- 该版引用的全部路段登记与禁入并集/前缀索引（按哈希去重）；
- 可选的签号、`r` 和回执。

`verify-audit` 不访问原仓库，会重算节点哈希、记录哈希、路线/段树总量和全部回执。

---

## 4. 回执与路径证据

每张中签回执包含：

```json
{
  "ticket_id": "LOT-2026-0005",
  "version_id": "v0",
  "r": "22/28",
  "total_effective_coordinates": 28,
  "formula_rank": 22,
  "global_rank": 22,
  "point": {
    "segment_id": "B-01",
    "route_id": "G2",
    "route_index": 1,
    "order": 10,
    "local_coordinate": 4,
    "record_hash": "..."
  },
  "evidence": {
    "valid_before_point_global": 22,
    "route": {
      "before_effective": 20,
      "route_effective_count": 8
    },
    "segment": {
      "before_effective_on_route": 0,
      "before_effective_global": 20,
      "segment_effective_count": 8,
      "intra_rank": 2,
      "local_coordinate": 4,
      "forbidden_before_local": 2
    }
  }
}
```

恒等式为：

```text
global_rank
= route.before_effective
+ segment.before_effective_on_route
+ segment.intra_rank
```

段内用合并后的禁入区间和前缀数组做二分定位，只返回被采中的局部坐标及
`forbidden_before_local` 等核查量，不展开公里格或全部有效坐标。

---

## 5. 跨段 / 整段退役示例

`examples/frame_v0.json` 的总量：

```text
A-01: 10
A-ZERO: 0（全禁入空段）
A-02: 10
B-01: 10 - 2 = 8
W = 28
```

执行 `examples/correction_retire_a01.json` 整段退役 `A-01` 后：

```text
W(v1) = 18
```

同一批签号回放结果（已在 `examples/output/pair_v0_v1_retire.json`）：

```text
LOT-2026-0001  A-01#0 -> A-02#0   segment_changed
LOT-2026-0002  A-01#0 -> A-02#0   segment_changed
LOT-2026-0003  A-02#0 -> A-02#6   intra_segment_moved
LOT-2026-0004  A-02#5 -> A-02#9   intra_segment_moved
LOT-2026-0005  B-01#4 -> B-01#6   intra_segment_moved
```

前两张签覆盖“整段退役后跨到后续段”的情形；它们是两张业务签，即使旧版落同一点也保持两条。

空框恢复/失去点的结果见：

- `examples/output/pair_empty_recovery.json`
- `examples/output/pair_empty_loss.json`

仅换路线归属的结果见：

- `examples/output/pair_route_only.json`

---

## 6. Python API

```python
from roadtool.core import Repository, draw_tickets, pair_receipt_docs

repo = Repository.initialize("/tmp/repo", frame_dict, version_id="v0")
old = draw_tickets(repo, repo.load_version("v0"), ticket_sheet_dict)

v1 = repo.apply_correction("v0", correction_ops, new_version_id="v1")
new = draw_tickets(repo, v1, ticket_sheet_dict)

report = pair_receipt_docs(old, new)
```

---

## 7. 离线增量存储与复杂度

仓库结构：

```text
repo/
  routes.json
  versions/<version_id>.json
  corrections/<correction_hash>.json
  records/<hash前2位>/<record_hash>.json
  nodes/<hash前2位>/<node_hash>.json
```

实现没有为每个版本复制完整路段登记：

- 完整路段登记按内容哈希存一次；未变路段在新版中继续引用原 `record_hash`。
- 全局路段 ID 索引、每条路线的顺位索引使用**持久化 Treap**。
- 固定路线名册上的路线有效里程前缀使用**持久化路线二叉树**。
- 更正 `t` 个路段时，只沿受影响路径创建 `O(t log S + t log R)` 个新节点；
  移动路线/顺位会删除旧键并插入新键，因此旧位置不会被重复计入总量。
- 更正校验通过 ID 树查找基准身份，不需要逐段重读全部登记。
- 未变路段的禁入并集和前缀数组不会在新版重建；只有真正替换/新增的路段才解析禁区。

单次出票复杂度：

```text
O(log R + log S_route + log M_segment)
```

其中 `M_segment` 是命中段的禁入区间数；进程内还有节点和路段索引缓存，
一次提交很多签时不会每签重新读盘或重扫所有禁区。

期初解析仍需对全部输入区间排序/合并一次，这是验证坏版和建立索引所必需的；
工具不会按长度展开坐标空间。审计导出为了形成自包含材料，会显式遍历一次该版引用的对象。

---

## 8. 结果核销账本（多现场组离线核销）

多个现场组会离线查验**同一张签**，不能按上传先后互相覆盖。账本把各组结果
保存为只追加的 JSONL 因果记录，绑定已出票回执，输出每签当前结果、**原出票点**
和**依据**；路线后续换版移动出票点时，账本里的原点与证据快照不会被改写。

跑一个用小回执做的完整演示（分叉、同状态文字冲突、多父合并、撤回、五类失败批次、
独立重放一致性）：

```bash
examples/run_ledger_demo.sh
# 产物见 examples/output/ledger/，JSONL 账本见
# examples/output/ledger/demo_ledger/{records,withdrawals,batches}.jsonl
```

### 8.1 账本目录

```text
my-ledger/
  records.jsonl       # 结果记录，每行一条（主 JSONL 产物）
  withdrawals.jsonl   # 撤回事件（不删记录/边）
  batches.jsonl       # 批次号 -> 内容指纹、记录 ID
  bindings.json       # 签 -> 已出票回执快照（首次绑定不可改写）
```

重开目录或用 `replay_ledger()` / `road ledger-replay` 从 JSONL 独立重放，报告完全一致；
全部输出顺序按 ID/拓扑序确定，不依赖输入行序、时间戳或随机数。

除目录形式外，`--ledger path/to/ledger.jsonl`（或以 `.jsonl` 结尾的路径）使用
**单文件事件日志**：每行一个带 `kind`（`record`/`withdrawal`/`batch`/`binding`）
的 JSON 事件，语义与目录形式完全相同。

### 8.2 绑定已出票回执

```bash
# 先用极小框出一份小回执
python3 -I road.py init --repo "$REPO" --input examples/ledger_frame.json --version-id v0
python3 -I road.py draw --repo "$REPO" --version v0 \
  --tickets examples/ledger_tickets.json --out receipts_v0_small.json

python3 -I road.py ledger-bind --ledger my-ledger \
  --receipts receipts_v0_small.json
```

绑定后每签报告含：

- `original_point` / `origin_point`：绑定时刻回执上的原出票点；
- `basis`：回执的完整路径证据（与第 4 节相同）；
- `receipt` / `receipt_hash` / `bound_version_id`：完整原回执与哈希。

同一回执重复绑定为 no-op；同一签绑定**不同**回执会以
`receipt_binding_conflict` 拒绝——即使路线已出 v1 且点已移动，原出票点也
保持 v0 快照不变（见演示 `16_rebind_rejected.json`、`17_origin_still_v0_point.json`）。

### 8.3 结果记录与批次格式

批次文件：

```json
{
  "batch_id": "B2-fork-left",
  "records": [
    {
      "id": "R2",
      "ticket_id": "T1",
      "status": "已查验",
      "parents": ["R1"],
      "by": "现场乙组",
      "note": "可保留任意附加字段"
    }
  ]
}
```

- `id`：结果记录唯一 ID（字符串或整数），全局不得重用；
- `ticket_id`：签身份；
- `status`：`"已查验"` 或 `"未到场"`（也接受 `inspected`/`not_present`/
  `absent` 等英文别名；输入状态原文保留在 `status`，规范码在 `status_code`）；
- `parents`：它接续的零个或多个旧结果 ID，可来自旧批或本批，允许前向引用，
  因此**本批行顺序不影响结果**；多父即把这些分支合并为一份新结果。

导入：

```bash
python3 -I road.py ledger-import --ledger my-ledger --input batch.json
# 也可：--batch-id B1（输入没写 batch_id 时）、--receipts r.json、
#       --require-bound-receipt
```

整批先验明再追加，下列情况**整批拒绝**，`records.jsonl` 不留任何行：

| 情况 | 错误码 |
|---|---|
| 同批次号但内容不同 | `batch_content_conflict` |
| 记录 ID 重用（批内或跨批） | `record_id_reused` |
| 父记录属于另一签 | `cross_ticket_parent` |
| 父记录不存在 | `missing_parent` |
| 因果图有环（含自父） | `cycle_detected` |

同批次号 + 同内容（记录行重排也算同内容，指纹按 ID 排序计算）重送是
`noop: true`，不重复入账。

### 8.4 候选、冲突与撤回规则

- **候选** = 未撤回，且不存在其他**未撤回后代**的结果；
- 后代闭包沿 `parents` 反向求传递闭包，**穿过已撤回记录仍然有效**
  （`A <- W(撤回) <- C(活跃)` 时，C 仍压制 A）；
- 父记录必须同签；图整体无环；
- 撤回按记录 ID：记录与因果边都保留，也**不自动撤回后代**；撤回事件追加在
  `withdrawals.jsonl`，重复撤回为幂等 no-op。

每签结论：

- 0 个候选 → `state: "no_candidate"`，`status: "未查验"`（包括从未有记录、
  或记录全部被撤回）；
- 恰 1 个候选 → `state: "unique"`，采用该记录自己的状态文字；
- 多个候选 → `state: "conflict"`，`status: "冲突待核"`，**即使状态文字相同
  也不自动合并**。

查询：

```bash
python3 -I road.py ledger-ticket --ledger my-ledger --ticket T1
python3 -I road.py ledger-report --ledger my-ledger --out all.json
python3 -I road.py ledger-withdraw --ledger my-ledger \
  --record R3 --reason "串签" --by 核销员
python3 -I road.py ledger-check --ledger my-ledger
# 同样支持嵌套写法：road ledger import|ticket|report|withdraw|check ...
```

`ticket` 报告的 `candidates[]` 中每个候选都带完整 `source_chain`
（全部祖先，拓扑序，含已撤回节点及其 `withdrawal` 事件）和
`source_chain_ids`；`decision_basis` 概述判定规则与候选链。

典型演化（演示产物编号对应 `examples/output/ledger/`）：

```text
R1(已查验)
 ├─ R2(已查验) ┐
 └─ R3(已查验) ┴─ R4(多父合并, 已查验)
```

1. R2、R3 并存 → 两个候选 → `冲突待核`（状态文字相同也冲突）；
2. 导入多父 R4 → 仅 R4 候选，采用已查验，来源链 `[R1,R2,R3,R4]`；
3. 撤回 R4 → R2、R3 重新成为候选（合并撤回不吞掉分支）；
4. 撤回 R3 → R2 唯一候选（另一分支不丢失，R1 仍被 R2 压制）；
5. 再撤回 R2 → R1 恢复为唯一候选。

### 8.5 Python API

```python
from roadtool.ledger import ResultLedger, replay_ledger

ledger = ResultLedger("/tmp/my-ledger")          # 不存在会自动建空账本
ledger.bind_receipts_doc(receipts_doc)           # 原出票点快照，不可改写
ledger.import_batch({"batch_id": "B1", "records": [...]})
ledger.withdraw("R3", reason="串签")

ticket = ledger.ticket_report("T1")              # 当前结果+候选来源链+原点依据
all_report = ledger.report()
assert ledger.check()["ok"]

same_report = replay_ledger("/tmp/my-ledger")    # 独立 JSONL 重放
```

常用别名：`Ledger`/`WriteOffLedger`/`VerificationLedger`，方法
`apply_batch`、`withdraw_result`、`ticket_status`、`current_results`，
以及函数式 `import_batch(path, batch)`、`withdraw_result(path, rid)`、
`ticket_status(path, tid)`、`ledger_report(path)`。
