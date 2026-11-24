# 路面养护抽签出票与核对工具

一个只依赖 Python 标准库的**离线**工具：负责按已验版资料把给定有理数 `r`
映射成查验点、保留路径证据、在新版资料上回放同一批签号、配对变点签、通知路线变化，并导出自包含核查包。

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
