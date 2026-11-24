"""Offline road-maintenance lottery ticketing and replay core.

The module is intentionally dependency-free.  It uses arbitrary precision
Python integers so lengths and rational denominators above 64 bits work.

Persistence is copy-on-write:

* segment registrations are content-addressed and stored once;
* ordered segment indexes and the global ID index are persistent treaps;
* route totals live in a persistent binary route tree;
* publishing a correction creates only new tree nodes on touched paths and a
  new version manifest.  Old manifests/nodes are never overwritten.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Errors and small primitives
# ---------------------------------------------------------------------------


class RoadToolError(Exception):
    """Base class for all expected tool errors."""

    code = "error"


class BadVersion(RoadToolError):
    code = "bad_version"


class BadCorrection(RoadToolError):
    code = "bad_correction"


class BadTicketSheet(RoadToolError):
    code = "bad_ticket_sheet"


class VersionNotFound(RoadToolError):
    code = "version_not_found"


class Conflict(RoadToolError):
    code = "conflict"


class CorruptRepository(RoadToolError):
    code = "corrupt_repository"


_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def stable_json_dumps(obj: Any) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(stable_json_dumps(obj).encode("utf-8")).hexdigest()


def safe_version_id(version_id: Any) -> str:
    if not isinstance(version_id, str) or not _VERSION_ID_RE.match(version_id):
        raise RoadToolError(
            "版本号只能包含字母、数字、点、下划线、连字符，且以字母或数字开头"
        )
    if version_id in {".", ".."} or "/" in version_id or "\\" in version_id:
        raise RoadToolError("非法版本号")
    return version_id


def scalar_id(value: Any, what: str = "身份") -> str:
    """Normalize route/segment/ticket IDs to stable strings."""

    if isinstance(value, bool) or value is None:
        raise BadVersion(f"{what}必须是字符串或整数")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if value == "":
            raise BadVersion(f"{what}不能为空字符串")
        return value
    raise BadVersion(f"{what}必须是字符串或整数，收到 {type(value).__name__}")


def parse_bigint(
    value: Any,
    what: str,
    *,
    minimum: Optional[int] = None,
    bad_cls: type[RoadToolError] = BadVersion,
) -> int:
    if isinstance(value, bool):
        raise bad_cls(f"{what}不能是布尔值")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        number = int(value.strip())
    else:
        raise bad_cls(f"{what}必须是整数（也接受整数字符串），收到 {value!r}")
    if minimum is not None and number < minimum:
        raise bad_cls(f"{what}必须 >= {minimum}，收到 {number}")
    return number


def parse_rational(
    value: Any,
    what: str = "有理数 r",
    *,
    require_unit_interval: bool = True,
    bad_cls: type[RoadToolError] = BadTicketSheet,
) -> Tuple[int, int]:
    """Parse an exact rational as ``(numerator, denominator)``."""

    from fractions import Fraction

    numerator: int
    denominator: int

    if isinstance(value, bool):
        raise bad_cls(f"{what}不能是布尔值")
    if isinstance(value, Fraction):
        numerator, denominator = value.numerator, value.denominator
    elif isinstance(value, int):
        numerator, denominator = value, 1
    elif isinstance(value, float):
        # JSON floats are binary rationals.  Accept them without rounding or
        # pretending 0.1 is exact; callers needing decimal semantics should
        # pass "1/10" or an exact decimal string.
        frac = Fraction(value)
        numerator, denominator = frac.numerator, frac.denominator
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        numerator = parse_bigint(value[0], f"{what}分子", bad_cls=bad_cls)
        denominator = parse_bigint(value[1], f"{what}分母", minimum=1, bad_cls=bad_cls)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise bad_cls(f"{what}为空")
        if "/" in text:
            left, right = text.split("/", 1)
            if not (_INT_RE.match(left.strip()) and _INT_RE.match(right.strip())):
                raise bad_cls(f"{what}不是精确的 'p/q' 整数分数: {value!r}")
            numerator, denominator = int(left.strip()), int(right.strip())
        else:
            # Fraction accepts exact decimal strings such as "0.125" and
            # ordinary integer strings.
            try:
                frac = Fraction(text)
            except (ValueError, ZeroDivisionError) as exc:
                raise bad_cls(f"{what}无法解析为精确有理数: {value!r}") from exc
            numerator, denominator = frac.numerator, frac.denominator
    elif isinstance(value, dict):
        p = value.get("numerator", value.get("p", value.get("num")))
        q = value.get("denominator", value.get("q", value.get("den")))
        if p is None or q is None:
            raise bad_cls(f"{what}对象需含 numerator/denominator（或 p/q）")
        numerator = parse_bigint(p, f"{what}分子", bad_cls=bad_cls)
        denominator = parse_bigint(q, f"{what}分母", minimum=1, bad_cls=bad_cls)
    else:
        raise bad_cls(f"{what}必须是字符串、整数或 p/q 对象，收到 {type(value).__name__}")

    if denominator == 0:
        raise bad_cls(f"{what}分母不能为 0")
    if require_unit_interval and not (0 <= numerator < denominator):
        raise bad_cls(f"{what}必须满足 0 <= r < 1，收到 {numerator}/{denominator}")
    return numerator, denominator


def rational_text(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator}"


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Segments and forbidden unions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    seg_id: str
    route_id: str
    order: int
    length: int
    forbidden: Tuple[Tuple[int, int], ...]
    merged: Tuple[Tuple[int, int], ...]
    effective_count: int
    record_hash: str
    core: Dict[str, Any]
    forbidden_prefix: Tuple[int, ...]
    allowed_before: Tuple[int, ...]

    def locate(self, intra_rank: int) -> Tuple[int, Dict[str, Any]]:
        """Return the local coordinate for zero-based valid rank in segment."""

        if not (0 <= intra_rank < self.effective_count):
            raise IndexError(
                f"段 {self.seg_id} 内序号 {intra_rank} 超出有效坐标数 "
                f"{self.effective_count}"
            )

        # O(log(number of forbidden intervals)); no interval scan per ticket.
        allowed_before = self.allowed_before
        m = len(self.merged)
        pos = bisect.bisect_right(allowed_before, intra_rank)

        if pos == 0:
            local = intra_rank
        elif pos == m:
            end = self.merged[-1][1]
            local = end + (intra_rank - allowed_before[-1])
        else:
            previous_end = self.merged[pos - 1][1]
            local = previous_end + (intra_rank - allowed_before[pos - 1])

        forbidden_before = local - intra_rank
        evidence = {
            "segment_id": self.seg_id,
            "length": self.length,
            "effective_count": self.effective_count,
            "intra_rank": intra_rank,
            "local_coordinate": local,
            "valid_before_local": intra_rank,
            "forbidden_before_local": forbidden_before,
            "record_hash": self.record_hash,
        }
        return local, evidence

    def envelope(self) -> Dict[str, Any]:
        return {
            "schema": "road-segment-record/1",
            "record_hash": self.record_hash,
            "core": self.core,
            "merged_forbidden": [[a, b] for a, b in self.merged],
            "forbidden_prefix": list(self.forbidden_prefix),
            "allowed_before_start": list(self.allowed_before),
            "effective_count": self.effective_count,
        }

    @classmethod
    def from_envelope(cls, env: Dict[str, Any]) -> "Segment":
        core = env["core"]
        expected_hash = env["record_hash"]
        actual_hash = canonical_hash(core)
        if actual_hash != expected_hash:
            raise CorruptRepository(
                f"路段记录哈希不符: {expected_hash} != {actual_hash}"
            )
        segment = parse_segment(core, {core["route"]}, bad_cls=CorruptRepository)
        if segment.record_hash != expected_hash:
            raise CorruptRepository(f"路段记录规范化哈希不符: {expected_hash}")

        # Use the persisted acceleration arrays after checking them.  This
        # avoids rebuilding/scanning禁区 on every load or ticket.
        prefix = tuple(int(x) for x in env.get("forbidden_prefix", segment.forbidden_prefix))
        allowed = tuple(
            int(x) for x in env.get("allowed_before_start", segment.allowed_before)
        )
        if prefix != segment.forbidden_prefix or allowed != segment.allowed_before:
            raise CorruptRepository(f"路段记录派生索引不符: {expected_hash}")
        return segment


def _forbidden_prefix(merged: Tuple[Tuple[int, int], ...]) -> Tuple[int, ...]:
    prefix = [0]
    for start, end in merged:
        prefix.append(prefix[-1] + end - start)
    return tuple(prefix)


def _allowed_before_start(
    merged: Tuple[Tuple[int, int], ...], prefix: Tuple[int, ...]
) -> Tuple[int, ...]:
    return tuple(start - prefix[i] for i, (start, _end) in enumerate(merged))


def _merge_intervals(intervals: List[Tuple[int, int]]) -> Tuple[Tuple[int, int], ...]:
    if not intervals:
        return ()
    intervals.sort()
    merged: List[List[int]] = []
    for start, end in intervals:
        if start == end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            # Overlap or adjacency.  Adjacent integer half-open intervals are
            # one continuous forbidden integer run.
            if end > merged[-1][1]:
                merged[-1][1] = end
    return tuple((a, b) for a, b in merged)


def _parse_interval(raw: Any, index: int, bad_cls: type[RoadToolError]) -> Tuple[int, int]:
    if isinstance(raw, dict):
        start_raw = raw.get("start", raw.get("begin", raw.get("a")))
        end_raw = raw.get("end", raw.get("finish", raw.get("b")))
        if start_raw is None or end_raw is None:
            raise bad_cls(f"第 {index} 个禁入区间需含 start/end")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start_raw, end_raw = raw
    else:
        raise bad_cls(f"第 {index} 个禁入区间必须是 [start,end) 或 {{start,end}}")

    start = parse_bigint(start_raw, f"第 {index} 个禁入区间起点", bad_cls=bad_cls)
    end = parse_bigint(end_raw, f"第 {index} 个禁入区间终点", bad_cls=bad_cls)
    return start, end


def parse_routes(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, list) or not raw:
        raise BadVersion("routes 必须是非空列表，路线名册及次序在期初固定")

    routes: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw):
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            route_id = scalar_id(item, "路线 ID")
            name = None
        elif isinstance(item, dict):
            id_raw = item.get("id", item.get("route_id"))
            if id_raw is None:
                raise BadVersion(f"第 {index} 条路线缺少 id")
            route_id = scalar_id(id_raw, "路线 ID")
            name = item.get("name")
            if name is not None and not isinstance(name, str):
                raise BadVersion(f"路线 {route_id} 的 name 必须是字符串")
        else:
            raise BadVersion("routes 项必须是路线 ID 字符串或 {id,...}")

        if route_id in seen:
            raise BadVersion(f"路线名册中路线 ID 重复: {route_id}")
        seen.add(route_id)
        entry: Dict[str, Any] = {"id": route_id, "index": index}
        if name is not None:
            entry["name"] = name
        routes.append(entry)

    return {"schema": "road-routes/1", "routes": routes}


def parse_segment(
    raw: Any,
    route_ids: Iterable[str],
    *,
    bad_cls: type[RoadToolError] = BadVersion,
) -> Segment:
    if not isinstance(raw, dict):
        raise bad_cls("路段登记必须是 JSON 对象")

    id_raw = raw.get("id", raw.get("segment_id"))
    if id_raw is None:
        raise bad_cls("路段缺少 id")
    seg_id = scalar_id(id_raw, "路段 ID")

    route_raw = raw.get("route", raw.get("route_id"))
    if route_raw is None:
        raise bad_cls(f"路段 {seg_id} 缺少 route")
    route_id = scalar_id(route_raw, "路线 ID")
    route_id_set = set(route_ids)
    if route_id not in route_id_set:
        raise bad_cls(f"路段 {seg_id} 归属了不存在的路线 {route_id}")

    order = parse_bigint(
        raw.get("order", raw.get("order_in_route", raw.get("seq", 0))),
        f"路段 {seg_id} 的路线内顺位",
        bad_cls=bad_cls,
    )
    length = parse_bigint(
        raw.get("length", raw.get("effective_length_source")),
        f"路段 {seg_id} 的长度",
        minimum=0,
        bad_cls=bad_cls,
    )

    intervals_raw = raw.get(
        "forbidden",
        raw.get("forbidden_intervals", raw.get("excluded", raw.get("blocked", []))),
    )
    if intervals_raw is None:
        intervals_raw = []
    if not isinstance(intervals_raw, list):
        raise bad_cls(f"路段 {seg_id} 的 forbidden 必须是列表")

    intervals: List[Tuple[int, int]] = []
    for i, interval_raw in enumerate(intervals_raw):
        start, end = _parse_interval(interval_raw, i, bad_cls)
        if start > end:
            raise bad_cls(
                f"路段 {seg_id} 第 {i} 个禁入区间起点大于终点: [{start},{end})"
            )
        if start < 0 or end > length:
            raise bad_cls(
                f"路段 {seg_id} 第 {i} 个禁入区间 [{start},{end}) 越界；"
                f"局部坐标框为 [0,{length})，不静默剪裁"
            )
        intervals.append((start, end))

    ordered_for_hash = sorted(intervals)
    merged = _merge_intervals(list(intervals))
    forbidden_total = sum(end - start for start, end in merged)
    effective_count = length - forbidden_total
    if effective_count < 0:  # defensive; bounds validation should prevent this
        raise bad_cls(f"路段 {seg_id} 禁入并集超过长度")

    core = {
        "id": seg_id,
        "route": route_id,
        "order": order,
        "length": length,
        "forbidden": [[a, b] for a, b in ordered_for_hash],
    }
    record_hash = canonical_hash(core)
    prefix = _forbidden_prefix(merged)
    allowed_before = _allowed_before_start(merged, prefix)
    return Segment(
        seg_id=seg_id,
        route_id=route_id,
        order=order,
        length=length,
        forbidden=tuple(ordered_for_hash),
        merged=merged,
        effective_count=effective_count,
        record_hash=record_hash,
        core=core,
        forbidden_prefix=prefix,
        allowed_before=allowed_before,
    )


def parse_frame(raw: Any) -> Tuple[Dict[str, Any], List[Segment]]:
    if not isinstance(raw, dict):
        raise BadVersion("期初版输入必须是 JSON 对象")
    routes_doc = parse_routes(raw.get("routes"))
    route_ids = [r["id"] for r in routes_doc["routes"]]

    segments_raw = raw.get("segments", [])
    if not isinstance(segments_raw, list):
        raise BadVersion("segments 必须是列表")

    segments: List[Segment] = []
    seen = set()
    for item in segments_raw:
        seg = parse_segment(item, route_ids)
        if seg.seg_id in seen:
            raise BadVersion(f"期初版身份重复: 路段 {seg.seg_id}")
        seen.add(seg.seg_id)
        segments.append(seg)
    return routes_doc, segments


# ---------------------------------------------------------------------------
# Persistent treap and route tree
# ---------------------------------------------------------------------------


class NodeSource:
    def get_node(self, node_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def put_node(self, obj: Dict[str, Any]) -> str:
        raise NotImplementedError

    def get_segment(self, record_hash: str) -> Segment:
        raise NotImplementedError


def _aggregate(source: NodeSource, node_id: Optional[str]) -> int:
    if node_id is None:
        return 0
    return source.get_node(node_id)["aggregate"]


def _priority(tree: str, key: Any) -> int:
    return int(canonical_hash({"tree": tree, "key": key}), 16)


def save_treap_node(
    source: NodeSource,
    *,
    tree: str,
    key: Any,
    value: Dict[str, Any],
    priority: int,
    left: Optional[str],
    right: Optional[str],
    leaf_weight: int,
) -> str:
    aggregate = (
        int(leaf_weight)
        + _aggregate(source, left)
        + _aggregate(source, right)
    )
    obj = {
        "kind": "treap",
        "tree": tree,
        "key": key,
        "value": value,
        "priority": priority,
        "left": left,
        "right": right,
        "leaf_weight": leaf_weight,
        "aggregate": aggregate,
    }
    return source.put_node(obj)


_UNSET = object()


def _clone_treap(
    source: NodeSource,
    node: Dict[str, Any],
    *,
    left: Any = _UNSET,
    right: Any = _UNSET,
) -> str:
    return save_treap_node(
        source,
        tree=node["tree"],
        key=node["key"],
        value=node["value"],
        priority=node["priority"],
        left=node["left"] if left is _UNSET else left,
        right=node["right"] if right is _UNSET else right,
        leaf_weight=node["leaf_weight"],
    )


def treap_find_node(
    source: NodeSource, root_id: Optional[str], key: Any
) -> Optional[Dict[str, Any]]:
    current = root_id
    while current is not None:
        node = source.get_node(current)
        if key == node["key"]:
            return node
        if key < node["key"]:
            current = node["left"]
        else:
            current = node["right"]
    return None


def treap_find(source: NodeSource, root_id: Optional[str], key: Any) -> Optional[Any]:
    node = treap_find_node(source, root_id, key)
    return None if node is None else node["value"]


def treap_split_lt(
    source: NodeSource, root_id: Optional[str], key: Any
) -> Tuple[Optional[str], Optional[str]]:
    if root_id is None:
        return None, None
    node = source.get_node(root_id)
    node_key = node["key"]
    if node_key < key:
        left_part, right_part = treap_split_lt(source, node["right"], key)
        cloned = _clone_treap(source, node, right=left_part)
        return cloned, right_part
    left_part, right_part = treap_split_lt(source, node["left"], key)
    cloned = _clone_treap(source, node, left=right_part)
    return left_part, cloned


def treap_merge(
    source: NodeSource, left_id: Optional[str], right_id: Optional[str]
) -> Optional[str]:
    if left_id is None:
        return right_id
    if right_id is None:
        return left_id
    left = source.get_node(left_id)
    right = source.get_node(right_id)
    if left["priority"] >= right["priority"]:
        new_right = treap_merge(source, left["right"], right_id)
        return _clone_treap(source, left, right=new_right)
    new_left = treap_merge(source, left_id, right["left"])
    return _clone_treap(source, right, left=new_left)


def treap_insert_absent(
    source: NodeSource,
    root_id: Optional[str],
    *,
    tree: str,
    key: Any,
    value: Dict[str, Any],
    leaf_weight: int,
) -> str:
    left, right = treap_split_lt(source, root_id, key)
    new_node = save_treap_node(
        source,
        tree=tree,
        key=key,
        value=value,
        priority=_priority(tree, key),
        left=None,
        right=None,
        leaf_weight=leaf_weight,
    )
    return treap_merge(source, treap_merge(source, left, new_node), right)


def treap_erase(
    source: NodeSource, root_id: Optional[str], key: Any
) -> Optional[str]:
    if root_id is None:
        raise KeyError(key)
    node = source.get_node(root_id)
    if key == node["key"]:
        return treap_merge(source, node["left"], node["right"])
    if key < node["key"]:
        new_left = treap_erase(source, node["left"], key)
        return _clone_treap(source, node, left=new_left)
    new_right = treap_erase(source, node["right"], key)
    return _clone_treap(source, node, right=new_right)


def treap_select_weight(
    source: NodeSource, root_id: str, rank: int, before: int = 0
) -> Tuple[Dict[str, Any], int, int]:
    """Select by leaf_weight.

    Returns ``(value, before_weight_in_this_tree, intra_leaf_rank)``.
    Zero-weight leaves are skipped.
    """

    node = source.get_node(root_id)
    left_weight = _aggregate(source, node["left"])
    leaf_weight = int(node["leaf_weight"])

    if rank < left_weight:
        return treap_select_weight(source, node["left"], rank, before)
    if rank < left_weight + leaf_weight:
        return node["value"], before + left_weight, rank - left_weight

    return treap_select_weight(
        source,
        node["right"],
        rank - left_weight - leaf_weight,
        before + left_weight + leaf_weight,
    )


class _TempNode:
    __slots__ = ("key", "value", "leaf_weight", "priority", "left", "right", "saved_id")

    def __init__(self, key: Any, value: Dict[str, Any], leaf_weight: int, priority: int):
        self.key = key
        self.value = value
        self.leaf_weight = leaf_weight
        self.priority = priority
        self.left: Optional["_TempNode"] = None
        self.right: Optional["_TempNode"] = None
        self.saved_id: Optional[str] = None


def build_treap_from_sorted(
    source: NodeSource,
    tree: str,
    items: List[Tuple[Any, Dict[str, Any], int]],
) -> Optional[str]:
    """Build and persist a treap in O(n) from key-sorted items."""

    if not items:
        return None

    stack: List[_TempNode] = []
    for key, value, leaf_weight in items:
        node = _TempNode(key, value, leaf_weight, _priority(tree, key))
        last: Optional[_TempNode] = None
        while stack and stack[-1].priority < node.priority:
            last = stack.pop()
        node.left = last
        if stack:
            stack[-1].right = node
        stack.append(node)

    root = stack[0]
    postorder: List[Tuple[_TempNode, bool]] = [(root, False)]
    while postorder:
        temp, processed = postorder.pop()
        if not processed:
            postorder.append((temp, True))
            if temp.right is not None:
                postorder.append((temp.right, False))
            if temp.left is not None:
                postorder.append((temp.left, False))
            continue

        left_id = temp.left.saved_id if temp.left is not None else None
        right_id = temp.right.saved_id if temp.right is not None else None
        temp.saved_id = save_treap_node(
            source,
            tree=tree,
            key=temp.key,
            value=temp.value,
            priority=temp.priority,
            left=left_id,
            right=right_id,
            leaf_weight=temp.leaf_weight,
        )
    return root.saved_id


def build_route_tree(
    source: NodeSource,
    lo: int,
    hi: int,
    route_leaves: List[Dict[str, Any]],
) -> str:
    if hi - lo == 1:
        leaf = route_leaves[lo]
        seg_root = leaf["segment_root"]
        total = _aggregate(source, seg_root)
        obj = {
            "kind": "route_leaf",
            "index": lo,
            "route_id": leaf["route_id"],
            "segment_root": seg_root,
            "aggregate": total,
        }
        return source.put_node(obj)

    mid = (lo + hi) // 2
    left = build_route_tree(source, lo, mid, route_leaves)
    right = build_route_tree(source, mid, hi, route_leaves)
    obj = {
        "kind": "route_internal",
        "lo": lo,
        "hi": hi,
        "left": left,
        "right": right,
        "aggregate": _aggregate(source, left) + _aggregate(source, right),
    }
    return source.put_node(obj)


def route_get_leaf(source: NodeSource, root_id: str, index: int) -> Dict[str, Any]:
    node = source.get_node(root_id)
    if node["kind"] == "route_leaf":
        if node["index"] != index:
            raise CorruptRepository("路线树叶节点索引不符")
        return node

    mid = (node["lo"] + node["hi"]) // 2
    if index < mid:
        return route_get_leaf(source, node["left"], index)
    return route_get_leaf(source, node["right"], index)


def route_update_leaf(
    source: NodeSource,
    root_id: str,
    index: int,
    new_segment_root: Optional[str],
) -> str:
    node = source.get_node(root_id)
    if node["kind"] == "route_leaf":
        total = _aggregate(source, new_segment_root)
        return source.put_node(
            {
                "kind": "route_leaf",
                "index": index,
                "route_id": node["route_id"],
                "segment_root": new_segment_root,
                "aggregate": total,
            }
        )

    mid = (node["lo"] + node["hi"]) // 2
    if index < mid:
        new_left = route_update_leaf(source, node["left"], index, new_segment_root)
        new_right = node["right"]
    else:
        new_left = node["left"]
        new_right = route_update_leaf(source, node["right"], index, new_segment_root)

    return source.put_node(
        {
            "kind": "route_internal",
            "lo": node["lo"],
            "hi": node["hi"],
            "left": new_left,
            "right": new_right,
            "aggregate": _aggregate(source, new_left) + _aggregate(source, new_right),
        }
    )


def route_select(
    source: NodeSource, root_id: str, rank: int, before: int = 0
) -> Dict[str, Any]:
    node = source.get_node(root_id)
    if node["kind"] == "route_leaf":
        return {
            "route_id": node["route_id"],
            "route_index": node["index"],
            "segment_root": node["segment_root"],
            "route_before": before,
            "route_total": node["aggregate"],
        }

    left_weight = _aggregate(source, node["left"])
    if rank < left_weight:
        return route_select(source, node["left"], rank, before)
    return route_select(
        source,
        node["right"],
        rank - left_weight,
        before + left_weight,
    )


# ---------------------------------------------------------------------------
# Filesystem repository
# ---------------------------------------------------------------------------


class Repository(NodeSource):
    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root)
        self._node_cache: Dict[str, Dict[str, Any]] = {}
        self._segment_cache: Dict[str, Segment] = {}
        self._version_cache: Dict[str, Dict[str, Any]] = {}
        self._routes_doc: Optional[Dict[str, Any]] = None

    # paths / low-level IO -------------------------------------------------

    @property
    def routes_path(self) -> Path:
        return self.root / "routes.json"

    def version_path(self, version_id: str) -> Path:
        vid = safe_version_id(version_id)
        return self.root / "versions" / f"{vid}.json"

    def _node_path(self, node_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", node_id):
            raise CorruptRepository(f"非法节点哈希: {node_id!r}")
        return self.root / "nodes" / node_id[:2] / f"{node_id}.json"

    def _record_path(self, record_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", record_hash):
            raise CorruptRepository(f"非法记录哈希: {record_hash!r}")
        return self.root / "records" / record_hash[:2] / f"{record_hash}.json"

    def require_open(self) -> None:
        if not self.routes_path.is_file():
            raise RoadToolError(f"目录不是已初始化的出票仓库: {self.root}")

    # NodeSource implementation --------------------------------------------

    def put_node(self, obj: Dict[str, Any]) -> str:
        node_id = canonical_hash(obj)
        if node_id in self._node_cache:
            return node_id
        path = self._node_path(node_id)
        if not path.exists():
            atomic_write_json(path, obj)
        self._node_cache[node_id] = obj
        return node_id

    def get_node(self, node_id: str) -> Dict[str, Any]:
        if node_id in self._node_cache:
            return self._node_cache[node_id]
        path = self._node_path(node_id)
        if not path.is_file():
            raise CorruptRepository(f"缺少持久化索引节点: {node_id}")
        obj = read_json_file(path)
        if canonical_hash(obj) != node_id:
            raise CorruptRepository(f"索引节点哈希不符: {node_id}")
        self._node_cache[node_id] = obj
        return obj

    def put_segment(self, segment: Segment) -> str:
        record_hash = segment.record_hash
        path = self._record_path(record_hash)
        if not path.exists():
            atomic_write_json(path, segment.envelope())
        self._segment_cache[record_hash] = segment
        return record_hash

    def get_segment_envelope(self, record_hash: str) -> Dict[str, Any]:
        path = self._record_path(record_hash)
        if not path.is_file():
            raise CorruptRepository(f"缺少路段记录: {record_hash}")
        return read_json_file(path)

    def get_segment(self, record_hash: str) -> Segment:
        if record_hash in self._segment_cache:
            return self._segment_cache[record_hash]
        env = self.get_segment_envelope(record_hash)
        segment = Segment.from_envelope(env)
        self._segment_cache[record_hash] = segment
        return segment

    # routes / versions ------------------------------------------------------

    def routes_doc(self) -> Dict[str, Any]:
        if self._routes_doc is not None:
            return self._routes_doc
        self.require_open()
        doc = read_json_file(self.routes_path)
        routes = parse_routes(doc.get("routes") if isinstance(doc, dict) else None)
        self._routes_doc = routes
        return routes

    def route_index_map(self) -> Dict[str, int]:
        return {r["id"]: r["index"] for r in self.routes_doc()["routes"]}

    def save_version(self, manifest: Dict[str, Any], *, overwrite: bool = False) -> None:
        version_id = safe_version_id(manifest["version_id"])
        path = self.version_path(version_id)
        if path.exists() and not overwrite:
            raise Conflict(f"版本已存在，已出版版本不可覆盖: {version_id}")
        atomic_write_json(path, manifest)
        self._version_cache[version_id] = manifest

    def load_version(self, version_id: str) -> Dict[str, Any]:
        vid = safe_version_id(version_id)
        if vid in self._version_cache:
            return self._version_cache[vid]
        path = self.version_path(vid)
        if not path.is_file():
            raise VersionNotFound(f"找不到已验版本: {vid}")
        manifest = read_json_file(path)
        if manifest.get("version_id") != vid:
            raise CorruptRepository(f"版本文件内 version_id 与文件名不符: {vid}")
        self._version_cache[vid] = manifest
        return manifest

    # initialization and correction -----------------------------------------

    @classmethod
    def initialize(
        cls,
        root: os.PathLike[str] | str,
        raw_frame: Dict[str, Any],
        *,
        version_id: str = "v0",
    ) -> "Repository":
        root_path = Path(root)
        vid = safe_version_id(version_id)

        # Validate the complete input before touching the repository.
        routes_doc, segments = parse_frame(raw_frame)

        if root_path.exists() and any(root_path.iterdir()):
            raise Conflict(f"仓库目录已存在且非空: {root_path}")
        root_path.mkdir(parents=True, exist_ok=True)

        repo = cls(root_path)
        atomic_write_json(repo.routes_path, routes_doc)

        route_count = len(routes_doc["routes"])
        route_items: List[List[Tuple[Any, Dict[str, Any], int]]] = [
            [] for _ in range(route_count)
        ]
        id_items: List[Tuple[Any, Dict[str, Any], int]] = []
        route_index = {r["id"]: r["index"] for r in routes_doc["routes"]}

        for segment in segments:
            record_hash = repo.put_segment(segment)
            placement = {
                "segment_id": segment.seg_id,
                "route_id": segment.route_id,
                "order": segment.order,
                "record_hash": record_hash,
                "effective_count": segment.effective_count,
            }
            key = [segment.order, segment.seg_id]
            route_items[route_index[segment.route_id]].append(
                (key, placement, segment.effective_count)
            )
            id_items.append((segment.seg_id, placement, 1))

        route_leaves: List[Dict[str, Any]] = []
        route_roots: List[Optional[str]] = []
        for index, items in enumerate(route_items):
            items.sort(key=lambda item: (item[0][0], item[0][1]))
            seg_root = build_treap_from_sorted(repo, "segment-order", items)
            route_roots.append(seg_root)
            route_leaves.append(
                {"route_id": routes_doc["routes"][index]["id"], "segment_root": seg_root}
            )

        id_items.sort(key=lambda item: item[0])
        id_root = build_treap_from_sorted(repo, "segment-id", id_items)
        route_tree_root = build_route_tree(repo, 0, route_count, route_leaves)
        total = _aggregate(repo, route_tree_root)
        segment_count = repo.get_node(id_root)["aggregate"] if id_root else 0

        routes_hash = canonical_hash(routes_doc)
        manifest_core = {
            "schema": "road-version/1",
            "parent_id": None,
            "routes_hash": routes_hash,
            "route_tree_root": route_tree_root,
            "id_root": id_root,
            "total_effective_coordinates": total,
            "segment_count": segment_count,
        }
        manifest = dict(manifest_core)
        manifest["version_id"] = vid
        manifest["correction_hash"] = None
        manifest["op_count"] = 0
        repo.save_version(manifest)
        return repo

    def _plan_correction(
        self, base_manifest: Dict[str, Any], raw_ops: Any
    ) -> List[Dict[str, Any]]:
        if isinstance(raw_ops, dict):
            raw_ops = raw_ops.get("corrections", raw_ops.get("ops", raw_ops.get("items")))
        if not isinstance(raw_ops, list):
            raise BadCorrection("更正版需提供 corrections/ops 列表")

        route_ids = set(self.route_index_map())
        planned: List[Dict[str, Any]] = []
        seen_targets = set()
        base_id_root = base_manifest["id_root"]

        for index, raw_op in enumerate(raw_ops):
            if not isinstance(raw_op, dict):
                raise BadCorrection(f"第 {index} 项更正必须是对象")
            op = raw_op.get("op", raw_op.get("action", raw_op.get("type")))
            op = {"update": "replace", "modify": "replace", "delete": "retire", "remove": "retire"}.get(
                op, op
            )
            if op not in {"add", "replace", "retire"}:
                raise BadCorrection(
                    f"第 {index} 项更正 op 必须是 add/replace/retire，收到 {op!r}"
                )

            if op == "retire":
                target = scalar_id(raw_op.get("id", raw_op.get("segment_id")), "更正目标 ID")
                if target in seen_targets:
                    raise BadCorrection(f"一次更正中路段 {target} 出现多于一项")
                seen_targets.add(target)
                old_node = treap_find_node(self, base_id_root, target)
                if old_node is None:
                    raise BadCorrection(f"退役失败：基准版没有路段 {target}")
                planned.append(
                    {"op": "retire", "target": target, "old": old_node["value"], "raw": raw_op}
                )
                continue

            segment_raw = raw_op.get("segment", raw_op.get("registration"))
            if segment_raw is None:
                # Allow a flattened registration with op/id at top level.
                segment_raw = {k: v for k, v in raw_op.items() if k not in {"op", "action", "type"}}
            segment = parse_segment(segment_raw, route_ids, bad_cls=BadCorrection)
            target = segment.seg_id
            explicit_target = raw_op.get("id", raw_op.get("segment_id"))
            if explicit_target is not None:
                explicit_id = scalar_id(explicit_target, "更正目标 ID")
                if explicit_id != target:
                    raise BadCorrection(
                        f"更正目标 ID {explicit_id} 与完整新登记 ID {target} 不一致；"
                        "替代必须保持稳定身份"
                    )

            if target in seen_targets:
                raise BadCorrection(f"一次更正中路段 {target} 出现多于一项")
            seen_targets.add(target)

            old_node = treap_find_node(self, base_id_root, target)
            if op == "add":
                if old_node is not None:
                    raise BadCorrection(f"新增失败：基准版已有路段 {target}")
                planned.append({"op": "add", "target": target, "segment": segment, "raw": raw_op})
            else:  # replace
                if old_node is None:
                    raise BadCorrection(f"替代失败：基准版没有路段 {target}")
                planned.append(
                    {
                        "op": "replace",
                        "target": target,
                        "segment": segment,
                        "old": old_node["value"],
                        "raw": raw_op,
                    }
                )

        return planned

    def apply_correction(
        self,
        base_version_id: str,
        raw_ops: Any,
        *,
        new_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        base = self.load_version(base_version_id)
        # Validation happens entirely before any new version is published.
        planned = self._plan_correction(base, raw_ops)

        requested_vid: Optional[str] = None
        if new_version_id is not None:
            requested_vid = safe_version_id(new_version_id)
            if self.version_path(requested_vid).exists():
                raise Conflict(f"新版本号已存在: {requested_vid}")

        route_index = self.route_index_map()
        route_root = base["route_tree_root"]
        id_root = base["id_root"]
        changed_records: List[str] = []
        normalized_ops: List[Dict[str, Any]] = []

        def upsert_route_placement(
            current_route_root: str,
            placement: Dict[str, Any],
            segment: Segment,
            old: Optional[Dict[str, Any]],
        ) -> str:
            new_key = [segment.order, segment.seg_id]
            new_route_idx = route_index[segment.route_id]

            if old is not None:
                old_route_idx = route_index[old["route_id"]]
                old_key = [old["order"], old["segment_id"]]
                old_leaf = route_get_leaf(self, current_route_root, old_route_idx)
                old_seg_root = treap_erase(self, old_leaf["segment_root"], old_key)
                current_route_root = route_update_leaf(
                    self, current_route_root, old_route_idx, old_seg_root
                )

                same_place = (
                    old["route_id"] == segment.route_id and old["order"] == segment.order
                )
                if same_place:
                    target_route_idx = old_route_idx
                    leaf = route_get_leaf(self, current_route_root, target_route_idx)
                    seg_root = treap_insert_absent(
                        self,
                        leaf["segment_root"],
                        tree="segment-order",
                        key=new_key,
                        value=placement,
                        leaf_weight=segment.effective_count,
                    )
                    return route_update_leaf(
                        self, current_route_root, target_route_idx, seg_root
                    )

                # If route/order changed, old key was removed above; now add
                # the new key in the destination route tree.
                leaf = route_get_leaf(self, current_route_root, new_route_idx)
            else:
                leaf = route_get_leaf(self, current_route_root, new_route_idx)

            seg_root = treap_insert_absent(
                self,
                leaf["segment_root"],
                tree="segment-order",
                key=new_key,
                value=placement,
                leaf_weight=segment.effective_count,
            )
            return route_update_leaf(self, current_route_root, new_route_idx, seg_root)

        for item in planned:
            op = item["op"]
            target = item["target"]

            if op == "retire":
                old = item["old"]
                old_route_idx = route_index[old["route_id"]]
                leaf = route_get_leaf(self, route_root, old_route_idx)
                seg_root = treap_erase(
                    self, leaf["segment_root"], [old["order"], target]
                )
                route_root = route_update_leaf(self, route_root, old_route_idx, seg_root)
                id_root = treap_erase(self, id_root, target)
                normalized_ops.append({"op": "retire", "id": target})
                continue

            segment: Segment = item["segment"]
            record_hash = self.put_segment(segment)
            changed_records.append(record_hash)
            placement = {
                "segment_id": segment.seg_id,
                "route_id": segment.route_id,
                "order": segment.order,
                "record_hash": record_hash,
                "effective_count": segment.effective_count,
            }

            old = item.get("old")
            if old is not None:
                id_root = treap_erase(self, id_root, target)
            id_root = treap_insert_absent(
                self,
                id_root,
                tree="segment-id",
                key=target,
                value=placement,
                leaf_weight=1,
            )
            route_root = upsert_route_placement(route_root, placement, segment, old)

            normalized_ops.append(
                {
                    "op": op,
                    "id": target,
                    "record_hash": record_hash,
                    "route": segment.route_id,
                    "order": segment.order,
                    "effective_count": segment.effective_count,
                }
            )

        total = _aggregate(self, route_root)
        segment_count = _aggregate(self, id_root)

        correction_doc = {
            "base_version": base_version_id,
            "ops": normalized_ops,
        }
        correction_hash = canonical_hash(correction_doc)
        correction_path = self.root / "corrections" / f"{correction_hash}.json"
        if not correction_path.exists():
            atomic_write_json(correction_path, correction_doc)

        manifest_core = {
            "schema": "road-version/1",
            "parent_id": base_version_id,
            "routes_hash": base["routes_hash"],
            "route_tree_root": route_root,
            "id_root": id_root,
            "total_effective_coordinates": total,
            "segment_count": segment_count,
            "correction_hash": correction_hash,
            "op_count": len(planned),
            "changed_record_hashes": changed_records,
        }

        if requested_vid is None:
            vid = "v-" + canonical_hash(manifest_core)[:16]
            if self.version_path(vid).exists():
                raise Conflict(f"新版本号已存在: {vid}")
        else:
            vid = requested_vid

        manifest = dict(manifest_core)
        manifest["version_id"] = vid
        self.save_version(manifest)
        return manifest


# ---------------------------------------------------------------------------
# Ticket sheets, drawing and verification
# ---------------------------------------------------------------------------


def parse_ticket_rows(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        raw = {"tickets": raw}
    if not isinstance(raw, dict):
        raise BadTicketSheet("抽签单必须是 JSON 对象或签号列表")

    common_r = raw.get("r", raw.get("ratio", raw.get("random")))
    common: Optional[Tuple[int, int]] = None
    if common_r is not None:
        common = parse_rational(common_r, "抽签单公共 r")

    tickets_raw = raw.get("tickets", raw.get("ticket_ids", raw.get("ids")))
    if not isinstance(tickets_raw, list):
        raise BadTicketSheet("抽签单需含固定顺序的 tickets/ticket_ids 列表")

    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(tickets_raw):
        r_value: Any = None
        if isinstance(item, dict):
            ticket_raw = item.get("id", item.get("ticket_id", item.get("no")))
            if ticket_raw is None:
                raise BadTicketSheet(f"第 {index} 张签缺少 id/ticket_id")
            ticket_id = scalar_id(ticket_raw, "签号")
            if "r" in item or "ratio" in item or "random" in item:
                r_value = item.get("r", item.get("ratio", item.get("random")))
            elif common is not None:
                r_value = common_r
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            ticket_id = scalar_id(item, "签号")
            if common is None:
                raise BadTicketSheet(
                    f"签 {ticket_id} 未给 r，且抽签单顶层也没有公共 r"
                )
            r_value = common_r
        else:
            raise BadTicketSheet(f"第 {index} 张签必须是签号或 {{id,r}} 对象")

        if r_value is None:
            raise BadTicketSheet(f"签 {ticket_id} 缺少有理数 r")
        numerator, denominator = parse_rational(r_value, f"签 {ticket_id} 的 r")
        rows.append(
            {
                "ticket_index": index,
                "ticket_id": ticket_id,
                "r_num": numerator,
                "r_den": denominator,
                "r": rational_text(numerator, denominator),
            }
        )
    return rows


def _row_from_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    if "r_num" in receipt and "r_den" in receipt:
        num = parse_bigint(receipt["r_num"], "r 分子", bad_cls=BadTicketSheet)
        den = parse_bigint(receipt["r_den"], "r 分母", minimum=1, bad_cls=BadTicketSheet)
    else:
        num, den = parse_rational(receipt.get("r"), "回执行 r")
    return {
        "ticket_index": receipt.get("ticket_index", 0),
        "ticket_id": receipt["ticket_id"],
        "r_num": num,
        "r_den": den,
        "r": rational_text(num, den),
    }


def draw_one(
    source: NodeSource, manifest: Dict[str, Any], row: Dict[str, Any]
) -> Dict[str, Any]:
    version_id = manifest["version_id"]
    total = int(manifest["total_effective_coordinates"])
    numerator = int(row["r_num"])
    denominator = int(row["r_den"])
    formula_rank = (numerator * total) // denominator

    receipt: Dict[str, Any] = {
        "schema": "road-ticket-receipt/1",
        "ticket_index": row["ticket_index"],
        "ticket_id": row["ticket_id"],
        "version_id": version_id,
        "r": row["r"],
        "r_num": str(numerator),
        "r_den": str(denominator),
        "total_effective_coordinates": total,
        "rank_formula": f"floor(({numerator}/{denominator}) * {total})",
        "formula_rank": formula_rank,
    }

    if total == 0:
        receipt.update(
            {
                "status": "empty_frame",
                "global_rank": None,
                "point": None,
                "evidence": {
                    "valid_before_point_global": None,
                    "route": None,
                    "segment": None,
                },
            }
        )
        return receipt

    rank = formula_rank
    if not (0 <= rank < total):
        raise CorruptRepository("计算出的签号全局序号越界")

    route_info = route_select(source, manifest["route_tree_root"], rank)
    route_before = route_info["route_before"]
    route_rank = rank - route_before
    placement, segment_before_on_route, intra_rank = treap_select_weight(
        source, route_info["segment_root"], route_rank
    )
    segment_before_global = route_before + segment_before_on_route
    if segment_before_global + intra_rank != rank:
        raise CorruptRepository("路线/段前缀证据之和与全局序号不符")

    segment = source.get_segment(placement["record_hash"])
    if segment.effective_count != placement["effective_count"]:
        raise CorruptRepository(f"段 {segment.seg_id} 的有效里程缓存与记录不符")
    local, local_evidence = segment.locate(intra_rank)

    route_evidence = {
        "route_id": route_info["route_id"],
        "route_index": route_info["route_index"],
        "before_effective": route_before,
        "route_effective_count": route_info["route_total"],
    }
    segment_evidence = {
        "segment_id": segment.seg_id,
        "route_id": segment.route_id,
        "order": segment.order,
        "before_effective_on_route": segment_before_on_route,
        "before_effective_global": segment_before_global,
        "segment_effective_count": segment.effective_count,
        "intra_rank": intra_rank,
        "local_coordinate": local,
        "valid_before_local": intra_rank,
        "forbidden_before_local": local - intra_rank,
        "length": segment.length,
        "record_hash": segment.record_hash,
    }
    point = {
        "segment_id": segment.seg_id,
        "route_id": segment.route_id,
        "route_index": route_info["route_index"],
        "order": segment.order,
        "local_coordinate": local,
        "record_hash": segment.record_hash,
    }
    receipt.update(
        {
            "status": "located",
            "global_rank": rank,
            "point": point,
            "evidence": {
                "valid_before_point_global": rank,
                "route": route_evidence,
                "segment": segment_evidence,
                "local_selection": local_evidence,
            },
        }
    )
    return receipt


def draw_tickets(
    source: NodeSource,
    manifest: Dict[str, Any],
    raw_sheet: Any,
) -> Dict[str, Any]:
    rows = parse_ticket_rows(raw_sheet)
    receipts = [draw_one(source, manifest, row) for row in rows]
    return {
        "schema": "road-lottery-receipts/1",
        "tool": "road_lottery_tool",
        "version_id": manifest["version_id"],
        "parent_id": manifest.get("parent_id"),
        "total_effective_coordinates": manifest["total_effective_coordinates"],
        "ticket_count": len(receipts),
        "receipts": receipts,
    }


def verify_receipts_doc(
    source: NodeSource,
    receipts_doc: Dict[str, Any],
    manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if manifest is None:
        if not hasattr(source, "load_version"):
            raise RoadToolError("离线回执核对需要显式 manifest")
        manifest = source.load_version(receipts_doc["version_id"])  # type: ignore[attr-defined]

    mismatches_report = []
    checked = 0
    for stored in receipts_doc.get("receipts", []):
        checked += 1
        row = _row_from_receipt(stored)
        recomputed = draw_one(source, manifest, row)
        mismatches: List[str] = []
        for key in [
            "status",
            "total_effective_coordinates",
            "formula_rank",
            "global_rank",
            "point",
            "evidence",
        ]:
            if stored.get(key) != recomputed.get(key):
                mismatches.append(key)
        if mismatches:
            mismatches_report.append(
                {
                    "ticket_index": stored.get("ticket_index"),
                    "ticket_id": stored.get("ticket_id"),
                    "mismatch_fields": mismatches,
                    "expected": {k: recomputed.get(k) for k in mismatches},
                    "stored": {k: stored.get(k) for k in mismatches},
                }
            )

    return {
        "schema": "road-receipt-verification/1",
        "version_id": manifest["version_id"],
        "checked": checked,
        "ok": not mismatches_report,
        "mismatches": mismatches_report,
    }


# ---------------------------------------------------------------------------
# Pairing and change sheets
# ---------------------------------------------------------------------------


def classify_pair(old_receipt: Dict[str, Any], new_receipt: Dict[str, Any]) -> Dict[str, Any]:
    old_point = old_receipt.get("point")
    new_point = new_receipt.get("point")

    if old_point is None and new_point is None:
        change = "empty_both"
        physical_changed = False
        action = "none"
    elif old_point is None and new_point is not None:
        change = "empty_to_point"
        physical_changed = True
        action = "reinspect_new_point"
    elif old_point is not None and new_point is None:
        change = "point_to_empty"
        physical_changed = True
        action = "withdraw_lost_point"
    elif old_point["segment_id"] != new_point["segment_id"]:
        change = "segment_changed"
        physical_changed = True
        action = "reinspect_new_point"
    elif old_point["local_coordinate"] != new_point["local_coordinate"]:
        change = "intra_segment_moved"
        physical_changed = True
        action = "reinspect_new_point"
    else:
        change = "unchanged"
        physical_changed = False
        action = "none"

    route_changed = bool(
        old_point
        and new_point
        and old_point.get("route_id") != new_point.get("route_id")
    )
    order_changed = bool(
        old_point
        and new_point
        and old_point.get("order") != new_point.get("order")
    )
    record_changed = bool(
        old_point
        and new_point
        and old_point.get("record_hash") != new_point.get("record_hash")
    )

    if route_changed and not physical_changed:
        route_action = "notify_route_change"
    elif route_changed:
        route_action = "route_changed_with_physical_change"
    else:
        route_action = "none"

    return {
        "change": change,
        "physical_point_changed": physical_changed,
        "requires_new_inspection": change
        in {"empty_to_point", "segment_changed", "intra_segment_moved"},
        "field_action": action,
        "route_changed": route_changed,
        "order_changed": order_changed,
        "registration_changed": record_changed,
        "route_action": route_action,
    }


def pair_receipt_docs(
    old_doc: Dict[str, Any], new_doc: Dict[str, Any]
) -> Dict[str, Any]:
    old_receipts = old_doc.get("receipts", [])
    new_receipts = new_doc.get("receipts", [])
    if len(old_receipts) != len(new_receipts):
        raise RoadToolError(
            f"两版签数不同，不能按同一批签号配对: {len(old_receipts)} != {len(new_receipts)}"
        )

    pairs: List[Dict[str, Any]] = []
    change_orders: List[Dict[str, Any]] = []
    route_only_notices: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}

    for index, (old_r, new_r) in enumerate(zip(old_receipts, new_receipts)):
        if old_r.get("ticket_index") != new_r.get("ticket_index"):
            raise RoadToolError(f"第 {index} 位签的顺序号不一致")
        if old_r.get("ticket_id") != new_r.get("ticket_id"):
            raise RoadToolError(
                f"第 {index} 位签号不一致: {old_r.get('ticket_id')} != {new_r.get('ticket_id')}"
            )
        if old_r.get("r") != new_r.get("r"):
            raise RoadToolError(f"签 {old_r.get('ticket_id')} 两版使用的 r 不一致")

        classification = classify_pair(old_r, new_r)
        change = classification["change"]
        counts[change] = counts.get(change, 0) + 1

        pair = {
            "ticket_index": index,
            "ticket_id": old_r["ticket_id"],
            "r": old_r.get("r"),
            "old_version_id": old_doc.get("version_id"),
            "new_version_id": new_doc.get("version_id"),
            **classification,
            "old_receipt": old_r,
            "new_receipt": new_r,
        }
        pairs.append(pair)

        if classification["physical_point_changed"]:
            change_orders.append(
                {
                    "ticket_index": index,
                    "ticket_id": old_r["ticket_id"],
                    "change": change,
                    "field_action": classification["field_action"],
                    "requires_new_inspection": classification[
                        "requires_new_inspection"
                    ],
                    "old_point": old_r.get("point"),
                    "new_point": new_r.get("point"),
                    "old_evidence": old_r.get("evidence"),
                    "new_evidence": new_r.get("evidence"),
                }
            )

        if classification["route_changed"] and not classification[
            "physical_point_changed"
        ]:
            route_only_notices.append(
                {
                    "ticket_index": index,
                    "ticket_id": old_r["ticket_id"],
                    "notice": "physical segment and local coordinate unchanged; route attribution changed",
                    "old_route_id": old_r["point"]["route_id"],
                    "new_route_id": new_r["point"]["route_id"],
                    "point": new_r["point"],
                }
            )

    return {
        "schema": "road-pair-report/1",
        "old_version_id": old_doc.get("version_id"),
        "new_version_id": new_doc.get("version_id"),
        "ticket_count": len(pairs),
        "category_counts": counts,
        "physical_change_count": len(change_orders),
        "route_only_notice_count": len(route_only_notices),
        "change_orders": change_orders,
        "route_only_notices": route_only_notices,
        "pairs": pairs,
    }


# ---------------------------------------------------------------------------
# Self-contained audit bundles
# ---------------------------------------------------------------------------


class BundleSource(NodeSource):
    def __init__(self, bundle: Dict[str, Any]):
        self.bundle = bundle
        self.nodes = bundle.get("nodes", {})
        self._segments: Dict[str, Segment] = {}

    def get_node(self, node_id: str) -> Dict[str, Any]:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise CorruptRepository(f"审计包缺少节点 {node_id}") from exc

    def put_node(self, obj: Dict[str, Any]) -> str:
        raise RoadToolError("审计包是只读的")

    def get_segment(self, record_hash: str) -> Segment:
        if record_hash in self._segments:
            return self._segments[record_hash]
        try:
            env = self.bundle["records"][record_hash]
        except KeyError as exc:
            raise CorruptRepository(f"审计包缺少路段记录 {record_hash}") from exc
        segment = Segment.from_envelope(env)
        self._segments[record_hash] = segment
        return segment


def build_audit_bundle(
    repo: Repository,
    version_id: str,
    raw_sheet: Any = None,
) -> Dict[str, Any]:
    manifest = repo.load_version(version_id)
    routes_doc = repo.routes_doc()

    nodes: Dict[str, Dict[str, Any]] = {}
    stack = [manifest["route_tree_root"], manifest["id_root"]]
    while stack:
        node_id = stack.pop()
        if node_id is None or node_id in nodes:
            continue
        node = repo.get_node(node_id)
        nodes[node_id] = node
        if node.get("left"):
            stack.append(node["left"])
        if node.get("right"):
            stack.append(node["right"])
        if node.get("kind") == "route_leaf" and node.get("segment_root"):
            stack.append(node["segment_root"])

    record_hashes = set()
    for node in nodes.values():
        value = node.get("value")
        if isinstance(value, dict) and value.get("record_hash"):
            record_hashes.add(value["record_hash"])

    records = {h: repo.get_segment_envelope(h) for h in sorted(record_hashes)}
    bundle: Dict[str, Any] = {
        "schema": "road-audit-bundle/1",
        "routes_doc": routes_doc,
        "version": manifest,
        "nodes": nodes,
        "records": records,
    }
    if raw_sheet is not None:
        bundle["receipts"] = draw_tickets(repo, manifest, raw_sheet)
    return bundle


def verify_audit_bundle(bundle: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(bundle, dict) or bundle.get("schema") != "road-audit-bundle/1":
        return {"ok": False, "errors": ["不是 road-audit-bundle/1 审计包"]}

    routes_doc = bundle.get("routes_doc")
    version = bundle.get("version")
    nodes = bundle.get("nodes", {})
    records = bundle.get("records", {})

    if not isinstance(version, dict):
        errors.append("缺少 version")
        return {"ok": False, "errors": errors}

    if canonical_hash(routes_doc) != version.get("routes_hash"):
        errors.append("路线名册哈希与版本 manifest 不符")

    for node_id, node in nodes.items():
        if canonical_hash(node) != node_id:
            errors.append(f"节点哈希不符: {node_id}")

    for record_hash, env in records.items():
        try:
            segment = Segment.from_envelope(env)
            if segment.record_hash != record_hash:
                errors.append(f"记录哈希键不符: {record_hash}")
        except Exception as exc:  # report all corruption as audit errors
            errors.append(f"路段记录 {record_hash} 无法核验: {exc}")

    source = BundleSource(bundle)
    for root_key in ["route_tree_root", "id_root"]:
        root_id = version.get(root_key)
        if root_id is not None and root_id not in nodes:
            errors.append(f"版本缺少 {root_key}: {root_id}")

    try:
        route_total = _aggregate(source, version["route_tree_root"])
        if route_total != version["total_effective_coordinates"]:
            errors.append(
                f"路线树总量 {route_total} 与 manifest "
                f"{version['total_effective_coordinates']} 不符"
            )
        id_count = _aggregate(source, version.get("id_root"))
        if id_count != version.get("segment_count"):
            errors.append(
                f"身份树段数 {id_count} 与 manifest {version.get('segment_count')} 不符"
            )
    except Exception as exc:
        errors.append(f"索引总量无法重算: {exc}")

    receipt_report = None
    if "receipts" in bundle:
        try:
            receipt_report = verify_receipts_doc(
                source, bundle["receipts"], manifest=version
            )
            if not receipt_report["ok"]:
                errors.append("审计包内回执重算不一致")
        except Exception as exc:
            errors.append(f"回执重算失败: {exc}")

    return {
        "schema": "road-audit-verification/1",
        "ok": not errors,
        "errors": errors,
        "node_count": len(nodes),
        "record_count": len(records),
        "receipt_verification": receipt_report,
    }


# ---------------------------------------------------------------------------
# Lazy re-exports of the result write-off ledger.
#
# ledger.py imports names from core, so importing it at module bottom would
# create a fragile initialization order.  A module-level __getattr__ gives
# ``from roadtool.core import ResultLedger`` semantics on demand (PEP 562).
# ---------------------------------------------------------------------------

_LEDGER_LAZY_NAMES = {
    "ResultLedger",
    "Ledger",
    "LedgerStore",
    "LedgerState",
    "BadBatch",
    "BatchContentConflict",
    "RecordIdReused",
    "MissingParent",
    "CrossTicketParent",
    "LedgerCycle",
    "RecordNotFound",
    "ReceiptBindingConflict",
    "CorruptLedger",
    "LedgerError",
    "replay_ledger",
    "add_record",
    "import_batch",
    "withdraw_result",
    "ticket_status",
    "ledger_report",
}


def __getattr__(name: str) -> Any:
    if name in _LEDGER_LAZY_NAMES:
        from . import ledger as _ledger

        return getattr(_ledger, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
