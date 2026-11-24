"""Offline result write-off ledger (结果核销账本).

Multiple field groups inspect the same ticket offline.  Upload order must not
make one group overwrite another, so this module implements an append-only
causal ledger:

* every result record has a unique id, a ticket identity, a status
  (``已查验`` / ``未到场``) and zero or more parent result ids it continues;
* parents must belong to the same ticket, may come from an old batch or the
  current batch, and the whole graph must be acyclic;
* a record can be withdrawn by id -- withdrawal never deletes the record or
  its causal edges and never withdraws descendants;
* candidates are non-withdrawn records with no other *non-withdrawn*
  descendant.  Descendant closure walks straight through withdrawn records;
* zero candidates means ``未查验``, exactly one adopts its status, several
  mean ``冲突待核`` (even when their status text is identical);
* batches are idempotent by ``batch_id`` + order-independent content hash;
  same id/different content, record-id reuse, cross-ticket/missing
  references and cycles reject the whole batch.

Persistence is a ledger directory containing JSONL files (``records.jsonl`` is
the canonical one-record-per-line store), ``withdrawals.jsonl``,
``batches.jsonl`` and an immutable ``bindings.json`` of ticket->receipt
snapshots.  Reopening the directory or replaying the JSONL from scratch yields
the same reports.
"""

from __future__ import annotations

import heapq
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .core import (
    RoadToolError,
    atomic_write_json,
    canonical_hash,
    stable_json_dumps,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LedgerError(RoadToolError):
    """Base class for ledger errors."""

    code = "ledger_error"


class BadBatch(LedgerError):
    code = "bad_batch"


class BatchContentConflict(LedgerError):
    code = "batch_content_conflict"


class RecordIdReused(LedgerError):
    code = "record_id_reused"


class MissingParent(LedgerError):
    code = "missing_parent"


class CrossTicketParent(LedgerError):
    code = "cross_ticket_parent"


class LedgerCycle(LedgerError):
    code = "cycle_detected"


class RecordNotFound(LedgerError):
    code = "record_not_found"


class ReceiptBindingConflict(LedgerError):
    code = "receipt_binding_conflict"


class CorruptLedger(LedgerError):
    code = "corrupt_ledger"


# Plausible English names for the same concepts are accepted on input; the
# Chinese domain wording from the specification is always preserved in output.
STATUS_INSPECTED = "inspected"
STATUS_NOT_PRESENT = "not_present"

STATUS_ZH: Dict[str, str] = {
    STATUS_INSPECTED: "已查验",
    STATUS_NOT_PRESENT: "未到场",
}

_STATUS_ALIASES: Dict[str, str] = {
    # 已查验
    "已查验": STATUS_INSPECTED,
    "查验": STATUS_INSPECTED,
    "已检查": STATUS_INSPECTED,
    "已到场": STATUS_INSPECTED,
    "inspected": STATUS_INSPECTED,
    "present": STATUS_INSPECTED,
    "found": STATUS_INSPECTED,
    "done": STATUS_INSPECTED,
    "pass": STATUS_INSPECTED,
    "passed": STATUS_INSPECTED,
    "ok": STATUS_INSPECTED,
    "yes": STATUS_INSPECTED,
    # 未到场
    "未到场": STATUS_NOT_PRESENT,
    "未到": STATUS_NOT_PRESENT,
    "缺席": STATUS_NOT_PRESENT,
    "不在场": STATUS_NOT_PRESENT,
    "not_present": STATUS_NOT_PRESENT,
    "not-present": STATUS_NOT_PRESENT,
    "absent": STATUS_NOT_PRESENT,
    "missed": STATUS_NOT_PRESENT,
    "missing": STATUS_NOT_PRESENT,
    "no": STATUS_NOT_PRESENT,
}

STATE_NO_CANDIDATE = "no_candidate"
STATE_UNIQUE = "unique"
STATE_CONFLICT = "conflict"

STATE_ZH: Dict[str, str] = {
    STATE_NO_CANDIDATE: "未查验",
    STATE_UNIQUE: "唯一候选",
    STATE_CONFLICT: "冲突待核",
}

SCHEMA_RECORD = "road-result-record/1"
SCHEMA_WITHDRAWAL = "road-withdrawal/1"
SCHEMA_BATCH = "road-batch-marker/1"
SCHEMA_BINDINGS = "road-receipt-bindings/1"


# ---------------------------------------------------------------------------
# Small normalization helpers
# ---------------------------------------------------------------------------


def _nonempty_id(value: Any, what: str, bad_cls: type[LedgerError] = BadBatch) -> str:
    if isinstance(value, bool) or value is None:
        raise bad_cls(f"{what}必须是字符串或整数")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise bad_cls(f"{what}不能为空")
        return text
    raise bad_cls(f"{what}必须是字符串或整数，收到 {type(value).__name__}")


def normalize_status(raw: Any) -> Tuple[str, str, str]:
    """Return ``(verbatim_status_text, canonical_code, chinese_label)``.

    The caller's status text is preserved verbatim so that "adopting the
    record's status" and "same status text" stay literal.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise BadBatch(f"结果状态必须是 '已查验' 或 '未到场'，收到 {raw!r}")
    verbatim = raw.strip()
    code = _STATUS_ALIASES.get(verbatim) or _STATUS_ALIASES.get(
        verbatim.lower()
    )
    if code is None:
        raise BadBatch(
            f"未知结果状态 {verbatim!r}；只接受 已查验/未到场 "
            "（以及 inspected/not_present 等英文别名）"
        )
    return verbatim, code, STATUS_ZH[code]


_PARENT_KEYS = (
    "parents",
    "parent_ids",
    "parent",
    "prev",
    "previous",
    "succeeds",
    "continues",
    "continues_from",
    "based_on",
)
_TICKET_KEYS = ("ticket_id", "ticket", "ticket_no", "lot_id", "lottery_id")
_ID_KEYS = ("id", "record_id", "result_id", "uuid")
_STATUS_KEYS = ("status", "result", "conclusion", "verdict_status")


def _first_present(item: Dict[str, Any], keys: Sequence[str]) -> Tuple[Optional[str], Any]:
    for key in keys:
        if key in item and item[key] is not None:
            return key, item[key]
    return None, None


def normalize_record(raw: Any, *, index: Optional[int] = None) -> Dict[str, Any]:
    """Validate and canonicalize one result record (no graph/state checks)."""

    where = f"第 {index} 条记录" if index is not None else "记录"
    if not isinstance(raw, dict):
        raise BadBatch(f"{where}必须是 JSON 对象")

    _, id_raw = _first_present(raw, _ID_KEYS)
    if id_raw is None:
        raise BadBatch(f"{where}缺少唯一 id")
    record_id = _nonempty_id(id_raw, f"{where} id")

    _, ticket_raw = _first_present(raw, _TICKET_KEYS)
    if ticket_raw is None:
        raise BadBatch(f"记录 {record_id} 缺少 ticket_id（签身份）")
    ticket_id = _nonempty_id(ticket_raw, f"记录 {record_id} 的 ticket_id")

    _, status_raw = _first_present(raw, _STATUS_KEYS)
    if status_raw is None:
        raise BadBatch(f"记录 {record_id} 缺少 status（已查验/未到场）")
    status_text, status_code, status_zh = normalize_status(status_raw)

    parents_raw: Any = None
    for key in _PARENT_KEYS:
        if key in raw:
            parents_raw = raw[key]
            break
    if parents_raw is None:
        parents: List[str] = []
    elif isinstance(parents_raw, (list, tuple)):
        parents = [
            _nonempty_id(p, f"记录 {record_id} 的父记录 id") for p in parents_raw
        ]
    else:
        # A single parent id given as scalar.
        parents = [_nonempty_id(parents_raw, f"记录 {record_id} 的父记录 id")]
    # Parent order carries no meaning (set of branches); dedupe + sort so
    # persistence and hashing are order-independent.
    parents = sorted(set(parents))
    if record_id in parents:
        raise LedgerCycle(f"记录 {record_id} 不能以自身为父")

    consumed = set(_PARENT_KEYS) | set(_TICKET_KEYS) | set(_ID_KEYS) | set(_STATUS_KEYS)
    extras = {k: v for k, v in raw.items() if k not in consumed}

    record: Dict[str, Any] = {
        "schema": SCHEMA_RECORD,
        "id": record_id,
        "ticket_id": ticket_id,
        "status": status_text,
        "status_code": status_code,
        "status_zh": status_zh,
        "parents": parents,
    }
    record.update(extras)
    return record


def parse_batch_raw(
    raw: Any, *, batch_id: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """Extract ``(batch_id, raw_record_list)`` from a batch JSON value."""

    bid: Optional[Any] = batch_id
    records_raw: Any = None

    if isinstance(raw, list):
        records_raw = raw
    elif isinstance(raw, dict):
        if bid is None:
            bid = raw.get("batch_id") or raw.get("batch") or raw.get("id")
        records_raw = raw.get(
            "records",
            raw.get("results", raw.get("items", raw.get("entries"))),
        )
        # A bare single record is accepted as a one-record batch.
        if records_raw is None and ("id" in raw or "record_id" in raw):
            records_raw = [raw]
    else:
        raise BadBatch("批次必须是 JSON 对象 {batch_id, records:[...]} 或记录列表")

    if bid is None:
        raise BadBatch("导入批次缺少 batch_id")
    bid_text = _nonempty_id(bid, "batch_id")
    if records_raw is None:
        raise BadBatch(f"批次 {bid_text} 缺少 records/results 列表")
    if not isinstance(records_raw, list):
        raise BadBatch(f"批次 {bid_text} 的 records 必须是列表")
    return bid_text, records_raw


def _fingerprint_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project a normalized record onto its batch-content identity.

    ``batch_id`` is assignment metadata, not batch content, so it is excluded
    to keep import-time and replay-time fingerprints identical.
    """

    return {k: v for k, v in record.items() if k != "batch_id"}


def batch_content_hash(records: Iterable[Dict[str, Any]]) -> str:
    """Order-independent fingerprint of a batch's normalized records."""

    ordered = sorted(
        (_fingerprint_record(r) for r in records), key=lambda r: r["id"]
    )
    return canonical_hash({"schema": "road-ledger-batch-content/1", "records": ordered})


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def build_children_map(records: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    children: Dict[str, List[str]] = {rid: [] for rid in records}
    for rid, record in records.items():
        for parent in record.get("parents", []):
            children.setdefault(parent, []).append(rid)
    return children


def ancestor_closure(record_id: str, parents_of: Dict[str, Sequence[str]]) -> Set[str]:
    seen = {record_id}
    stack: List[str] = list(parents_of.get(record_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(parents_of.get(current, ()))
    return seen


def descendant_closure(record_id: str, children_of: Dict[str, Sequence[str]]) -> Set[str]:
    seen: Set[str] = set()
    stack: List[str] = [record_id]
    while stack:
        current = stack.pop()
        for child in children_of.get(current, ()):  # type: ignore[arg-type]
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def stable_topo_order(
    node_ids: Set[str],
    parents_of: Dict[str, Sequence[str]],
) -> List[str]:
    """Kahn topological order; ready ties broken by lexicographic id.

    Gives a deterministic parents-before-children ordering that is stable
    across reopen/replay and independent of input line order.
    """

    nodes = set(node_ids)
    children: Dict[str, List[str]] = {n: [] for n in nodes}
    indegree: Dict[str, int] = {n: 0 for n in nodes}
    for node in nodes:
        for parent in parents_of.get(node, ()):  # type: ignore[arg-type]
            if parent in nodes:
                indegree[node] += 1
                children.setdefault(parent, []).append(node)

    ready: List[str] = [n for n, d in indegree.items() if d == 0]
    heapq.heapify(ready)
    order: List[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for child in children.get(node, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)

    if len(order) != len(nodes):
        remaining = sorted(n for n, d in indegree.items() if d > 0)
        raise LedgerCycle(f"结果因果图存在环，涉及记录: {remaining}")
    return order


def detect_cycle(records_by_id: Dict[str, Dict[str, Any]]) -> Optional[List[str]]:
    """Return a stable ordering iff acyclic, otherwise ``None``."""

    parents_of = {rid: tuple(r.get("parents", ())) for rid, r in records_by_id.items()}
    try:
        return stable_topo_order(set(records_by_id), parents_of)
    except LedgerCycle:
        return None


# ---------------------------------------------------------------------------
# In-memory state + JSONL persistence
# ---------------------------------------------------------------------------


class LedgerState:
    def __init__(self) -> None:
        self.records: Dict[str, Dict[str, Any]] = {}
        self.record_order: List[str] = []
        self.withdrawn: Dict[str, Dict[str, Any]] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.bindings: Dict[str, Dict[str, Any]] = {}

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> "LedgerState":
        """Load from a ledger directory or a single ``*.jsonl`` journal."""

        if root.is_file() or root.suffix == ".jsonl":
            return cls.from_jsonl_file(root)
        return cls.from_directory(root)

    @classmethod
    def from_jsonl_file(cls, path: Path) -> "LedgerState":
        state = cls()
        if not path.is_file():
            return state
        for line_no, event in enumerate(_read_jsonl(path), 1):
            if not isinstance(event, dict) or "kind" not in event:
                raise CorruptLedger(
                    f"{path.name} 第 {line_no} 行不是账本事件（缺少 kind）"
                )
            kind = event["kind"]
            obj = {k: v for k, v in event.items() if k != "kind"}
            if kind == "record":
                rid = _nonempty_id(obj.get("id"), "记录 id", CorruptLedger)
                if rid in state.records:
                    raise CorruptLedger(f"账本事件中记录 id 重复: {rid}")
                state.records[rid] = obj
                state.record_order.append(rid)
            elif kind == "withdrawal":
                rid = _nonempty_id(
                    obj.get("record_id"), "撤回 record_id", CorruptLedger
                )
                if rid in state.withdrawn:
                    raise CorruptLedger(f"账本事件中撤回重复: {rid}")
                state.withdrawn[rid] = obj
            elif kind == "batch":
                bid = _nonempty_id(obj.get("batch_id"), "batch_id", CorruptLedger)
                if bid in state.batches:
                    raise CorruptLedger(f"账本事件中批次标记重复: {bid}")
                state.batches[bid] = obj
            elif kind == "binding":
                tid = _nonempty_id(
                    obj.get("ticket_id"), "绑定 ticket_id", CorruptLedger
                )
                state.bindings[tid] = obj
            else:
                raise CorruptLedger(
                    f"{path.name} 第 {line_no} 行含未知事件类型 {kind!r}"
                )
        return state

    @classmethod
    def from_directory(cls, root: Path) -> "LedgerState":
        state = cls()
        records_path = root / "records.jsonl"
        if records_path.is_file():
            for line_no, obj in enumerate(_read_jsonl(records_path), 1):
                if not isinstance(obj, dict) or "id" not in obj:
                    raise CorruptLedger(f"records.jsonl 第 {line_no} 行不是结果记录")
                rid = _nonempty_id(obj["id"], "记录 id", CorruptLedger)
                if rid in state.records:
                    raise CorruptLedger(f"records.jsonl 中记录 id 重复: {rid}")
                state.records[rid] = obj
                state.record_order.append(rid)

        withdrawals_path = root / "withdrawals.jsonl"
        if withdrawals_path.is_file():
            for line_no, obj in enumerate(_read_jsonl(withdrawals_path), 1):
                rid = obj.get("record_id") if isinstance(obj, dict) else None
                if not rid:
                    raise CorruptLedger(
                        f"withdrawals.jsonl 第 {line_no} 行缺少 record_id"
                    )
                rid = _nonempty_id(rid, "撤回 record_id", CorruptLedger)
                if rid in state.withdrawn:
                    raise CorruptLedger(f"withdrawals.jsonl 中撤回重复: {rid}")
                state.withdrawn[rid] = obj

        batches_path = root / "batches.jsonl"
        if batches_path.is_file():
            for line_no, obj in enumerate(_read_jsonl(batches_path), 1):
                bid = obj.get("batch_id") if isinstance(obj, dict) else None
                if not bid:
                    raise CorruptLedger(f"batches.jsonl 第 {line_no} 行缺少 batch_id")
                bid = _nonempty_id(bid, "batch_id", CorruptLedger)
                if bid in state.batches:
                    raise CorruptLedger(f"batches.jsonl 中批次标记重复: {bid}")
                state.batches[bid] = obj

        bindings_path = root / "bindings.json"
        if bindings_path.is_file():
            doc = json.loads(bindings_path.read_text(encoding="utf-8"))
            bindings = doc.get("tickets", {}) if isinstance(doc, dict) else {}
            if not isinstance(bindings, dict):
                raise CorruptLedger("bindings.json 的 tickets 必须是对象")
            state.bindings = dict(bindings)

        return state


def _read_jsonl(path: Path) -> List[Any]:
    objects: List[Any] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptLedger(f"无法读取 {path}: {exc}") from exc
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CorruptLedger(f"{path.name} 第 {line_no} 行不是合法 JSON: {exc}") from exc
    return objects


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(stable_json_dumps(obj))
        handle.write("\n")
        handle.flush()


class ResultLedger:
    """Append-only result write-off ledger rooted at a directory."""

    def __init__(self, root: "str | Path | ResultLedger | LedgerState") -> None:
        if isinstance(root, ResultLedger):
            root = root.root
        self.root = Path(root)

        # A ledger may be either a directory of focused JSONL/JSON files or a
        # single *.jsonl event journal.  An existing file always means journal;
        # a not-yet-existing path ending in .jsonl is created as a journal.
        self.single_file = self.root.is_file() or self.root.suffix == ".jsonl"
        if self.single_file:
            self.journal_path = self.root
            self.records_path = self.root
            self.withdrawals_path = self.root
            self.batches_path = self.root
            self.bindings_path = self.root
            self.root.parent.mkdir(parents=True, exist_ok=True)
            if not self.root.exists():
                self.root.touch()
        else:
            self.journal_path = self.root / "ledger.jsonl"
            self.records_path = self.root / "records.jsonl"
            self.withdrawals_path = self.root / "withdrawals.jsonl"
            self.batches_path = self.root / "batches.jsonl"
            self.bindings_path = self.root / "bindings.json"
            self.root.mkdir(parents=True, exist_ok=True)
        self.state = LedgerState.load(self.root)

    def _append_event(
        self, kind: str, directory_file: str, obj: Dict[str, Any]
    ) -> None:
        """Persist one event in the active layout."""

        if self.single_file:
            _append_jsonl(self.journal_path, {"kind": kind, **obj})
        else:
            _append_jsonl(self.root / directory_file, obj)

    def _persist_new_bindings(
        self, pending: Dict[str, Dict[str, Any]], merged: Dict[str, Dict[str, Any]]
    ) -> None:
        if self.single_file:
            for tid in sorted(pending):
                self._append_event("binding", "bindings.json", pending[tid])
        else:
            atomic_write_json(
                self.bindings_path,
                {"schema": SCHEMA_BINDINGS, "tickets": merged},
            )

    # Convenience constructors / aliases ------------------------------------

    @classmethod
    def open(cls, root: "str | Path") -> "ResultLedger":
        return cls(root)

    @classmethod
    def initialize(cls, root: "str | Path") -> "ResultLedger":
        """Create an empty ledger (idempotent) and return it.

        A path ending in ``.jsonl`` is a single-file event journal; any other
        path is treated as a ledger directory.
        """

        return cls(root)

    # -- receipt binding ----------------------------------------------------

    def bind_receipts_doc(self, doc: Any) -> Dict[str, Any]:
        """Bind issued-ticket receipts.

        The first binding of a ticket is immutable: later route versions may
        move the point, but they can never rewrite the original ticketing
        point/evidence stored here.  Identical redelivery is a no-op; a
        different receipt for an already bound ticket rejects the whole call.
        """

        receipts = list(_iter_receipts(doc))
        if not receipts:
            raise LedgerError("回执文件中没有 receipts 记录可绑定")

        pending: Dict[str, Dict[str, Any]] = {}
        unchanged: List[str] = []
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ReceiptBindingConflict("回执条目必须是 JSON 对象")
            tid_raw = receipt.get("ticket_id") or receipt.get("ticket")
            if tid_raw is None:
                raise ReceiptBindingConflict("回执条目缺少 ticket_id")
            tid = _nonempty_id(tid_raw, "回执 ticket_id", ReceiptBindingConflict)
            receipt_hash = canonical_hash(receipt)

            existing = self.state.bindings.get(tid)
            if existing is not None:
                if existing.get("receipt_hash") != receipt_hash:
                    raise ReceiptBindingConflict(
                        f"签 {tid} 已绑定原出票回执 "
                        f"{existing.get('receipt_hash', '')[:12]}…；"
                        "原出票点与依据不可被后续换版回执改写"
                    )
                unchanged.append(tid)
                continue

            if tid in pending and pending[tid]["receipt_hash"] != receipt_hash:
                raise ReceiptBindingConflict(
                    f"同一份绑定材料中签 {tid} 出现两份不同回执"
                )

            pending[tid] = {
                "schema": "road-ticket-binding/1",
                "ticket_id": tid,
                "receipt_hash": receipt_hash,
                "receipt_schema": receipt.get("schema"),
                "version_id": receipt.get("version_id"),
                "ticket_index": receipt.get("ticket_index"),
                "r": receipt.get("r"),
                "receipt_status": receipt.get("status"),
                "point": receipt.get("point"),
                "evidence": receipt.get("evidence"),
                "receipt": receipt,
            }

        merged = dict(self.state.bindings)
        merged.update(pending)
        self._persist_new_bindings(pending, merged)
        self.state.bindings = merged
        return {
            "ok": True,
            "bound": len(pending),
            "unchanged": len(set(unchanged)),
            "ticket_ids": sorted(merged),
            "newly_bound_ticket_ids": sorted(pending),
        }

    # -- batch import -------------------------------------------------------

    def import_batch(
        self,
        raw: Any,
        *,
        batch_id: Optional[str] = None,
        require_bound_receipt: bool = False,
    ) -> Dict[str, Any]:
        bid, raw_items = parse_batch_raw(raw, batch_id=batch_id)

        # Structural normalization first, independent of current state.
        incoming: List[Dict[str, Any]] = [
            normalize_record(item, index=i) for i, item in enumerate(raw_items)
        ]

        seen_ids: Set[str] = set()
        for record in incoming:
            rid = record["id"]
            if rid in seen_ids:
                raise RecordIdReused(
                    f"批次 {bid} 内记录 id 重复（记录 ID 必须全局唯一）: {rid}"
                )
            seen_ids.add(rid)

        content_hash = batch_content_hash(incoming)

        # Idempotent redelivery of the exact same batch.  Row order does not
        # matter because the fingerprint sorts by record id.
        existing_batch = self.state.batches.get(bid)
        if existing_batch is not None:
            if existing_batch.get("content_hash") == content_hash:
                return {
                    "ok": True,
                    "noop": True,
                    "batch_id": bid,
                    "imported": 0,
                    "record_ids": list(existing_batch.get("record_ids", [])),
                    "content_hash": content_hash,
                }
            raise BatchContentConflict(
                f"批次号 {bid} 已导入但内容不同；同号异内容整批拒绝"
                f"（原指纹 {str(existing_batch.get('content_hash'))[:12]}…，"
                f"新指纹 {content_hash[:12]}…）"
            )

        # Record ids are globally unique: any clash with committed records is
        # ID reuse (exact batch redelivery was handled above).
        for record in incoming:
            rid = record["id"]
            if rid in self.state.records:
                raise RecordIdReused(
                    f"记录 ID {rid} 已在批次 "
                    f"{self.state.records[rid].get('batch_id')} 中使用；"
                    "记录 ID 重用整批拒绝"
                )

        pending_by_id = {r["id"]: r for r in incoming}

        if require_bound_receipt:
            for record in incoming:
                if record["ticket_id"] not in self.state.bindings:
                    raise MissingParent(
                        f"签 {record['ticket_id']} 尚未绑定已出票回执，"
                        "而本导入要求先绑定回执"
                    )

        # Reference + same-ticket validation against committed state and the
        # rest of this batch (forward references allowed: line order free).
        for record in incoming:
            tid = record["ticket_id"]
            for parent_id in record["parents"]:
                pending_parent = pending_by_id.get(parent_id)
                if pending_parent is not None:
                    parent_ticket = pending_parent["ticket_id"]
                else:
                    old_parent = self.state.records.get(parent_id)
                    if old_parent is None:
                        raise MissingParent(
                            f"记录 {record['id']} 接续的父记录 {parent_id} "
                            "不存在（缺失引用）；整批拒绝"
                        )
                    parent_ticket = old_parent["ticket_id"]
                if parent_ticket != tid:
                    raise CrossTicketParent(
                        f"记录 {record['id']} 属于签 {tid}，但其父记录 "
                        f"{parent_id} 属于另一签 {parent_ticket}；跨签引用整批拒绝"
                    )

        # Acyclicity over the union of committed graph + incoming records.
        merged_records = dict(self.state.records)
        for record in incoming:
            merged_records[record["id"]] = record
        cycle_order = detect_cycle(merged_records)
        if cycle_order is None:
            # Re-run stable topo on just the union to obtain involved nodes.
            parents_of = {
                rid: tuple(r.get("parents", ())) for rid, r in merged_records.items()
            }
            try:
                stable_topo_order(set(merged_records), parents_of)
            except LedgerCycle as exc:
                raise LedgerCycle(f"{exc}；批次 {bid} 整批拒绝") from exc
            raise LedgerCycle(f"批次 {bid} 存在环；整批拒绝")

        # Commit: all validation is done, so append the canonical record
        # lines in one write, then the batch marker.
        for record in incoming:
            record["batch_id"] = bid
        if self.single_file:
            blob = "".join(
                stable_json_dumps({"kind": "record", **r}) + "\n"
                for r in incoming
            )
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(blob)
                handle.flush()
        else:
            blob = "".join(stable_json_dumps(r) + "\n" for r in incoming)
            self.records_path.parent.mkdir(parents=True, exist_ok=True)
            with self.records_path.open("a", encoding="utf-8") as handle:
                handle.write(blob)
                handle.flush()

        marker = {
            "schema": SCHEMA_BATCH,
            "batch_id": bid,
            "content_hash": content_hash,
            "record_count": len(incoming),
            "record_ids": [r["id"] for r in incoming],
        }
        self._append_event("batch", "batches.jsonl", marker)

        for record in incoming:
            self.state.records[record["id"]] = record
            self.state.record_order.append(record["id"])
        self.state.batches[bid] = marker

        return {
            "ok": True,
            "noop": False,
            "batch_id": bid,
            "imported": len(incoming),
            "record_ids": [r["id"] for r in incoming],
            "content_hash": content_hash,
        }

    # -- withdrawal ---------------------------------------------------------

    def add_record(
        self, record: Dict[str, Any], *, batch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Convenience: append one result record without authoring a batch.

        A deterministic internal batch id is allocated unless one is supplied
        or present on the record.  Re-adding an existing record id then hits
        the normal global record-id reuse rejection.
        """

        rid: Any = None
        if isinstance(record, dict):
            rid = record.get("id") or record.get("record_id")
            if batch_id is None and record.get("batch_id"):
                batch_id = _nonempty_id(record["batch_id"], "batch_id")
        if batch_id is None:
            rid_text = _nonempty_id(rid, "记录 id") if rid is not None else "x"
            batch_id = f"direct-{len(self.state.batches) + 1:06d}-{rid_text}"
        return self.import_batch(
            {"batch_id": batch_id, "records": [record]}, batch_id=batch_id
        )

    def withdraw(
        self,
        record_id: Any,
        *,
        reason: Optional[str] = None,
        by: Optional[str] = None,
        withdrawal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = _nonempty_id(record_id, "撤回目标 record_id", RecordNotFound)
        if rid not in self.state.records:
            raise RecordNotFound(f"不能撤回不存在的结果记录: {rid}")

        if rid in self.state.withdrawn:
            return {
                "ok": True,
                "noop": True,
                "already_withdrawn": True,
                "record_id": rid,
                "withdrawal": self.state.withdrawn[rid],
            }

        event: Dict[str, Any] = {
            "schema": SCHEMA_WITHDRAWAL,
            "record_id": rid,
        }
        if withdrawal_id is not None:
            event["withdrawal_id"] = _nonempty_id(
                withdrawal_id, "withdrawal_id", LedgerError
            )
        if reason is not None:
            event["reason"] = reason
        if by is not None:
            event["by"] = by

        self._append_event("withdrawal", "withdrawals.jsonl", event)
        self.state.withdrawn[rid] = event
        return {
            "ok": True,
            "withdrawn": True,
            "noop": False,
            "record_id": rid,
            "withdrawal": event,
        }

    def withdraw_many(self, raw: Any) -> Dict[str, Any]:
        """Withdraw several records from a list/doc.

        Accepted shapes:

        * ``["R1", "R2"]``
        * ``[{"id": "R1", "reason": "..."}, ...]``
        * ``{"withdrawals": [...]}`` / ``{"records": [...]}`` / ``{"ids": [...]}``

        Unknown ids raise ``RecordNotFound`` before any withdrawal is appended
        (whole call validated first).  Repeated withdrawal stays idempotent.
        """

        if isinstance(raw, dict):
            items = raw.get(
                "withdrawals",
                raw.get("records", raw.get("ids", raw.get("items"))),
            )
        else:
            items = raw
        if not isinstance(items, list) or not items:
            raise LedgerError("撤回材料必须是非空列表或 {withdrawals:[...]}")

        planned: List[Tuple[str, Dict[str, Any]]] = []
        for i, item in enumerate(items):
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                rid = _nonempty_id(item, "撤回 record_id", RecordNotFound)
                kwargs: Dict[str, Any] = {}
            elif isinstance(item, dict):
                id_raw = item.get("record_id", item.get("id"))
                rid = _nonempty_id(id_raw, f"第 {i} 项撤回 id", RecordNotFound)
                kwargs = {
                    "reason": item.get("reason"),
                    "by": item.get("by"),
                    "withdrawal_id": item.get("withdrawal_id"),
                }
            else:
                raise LedgerError(f"第 {i} 项撤回必须是记录 ID 或对象")
            if rid not in self.state.records:
                raise RecordNotFound(f"不能撤回不存在的结果记录: {rid}")
            planned.append((rid, kwargs))

        done: List[str] = []
        noop: List[str] = []
        for rid, kwargs in planned:
            result = self.withdraw(rid, **kwargs)
            (noop if result.get("noop") else done).append(rid)
        return {
            "ok": True,
            "withdrawn_ids": done,
            "noop_ids": noop,
            "withdrawn": len(done),
            "noop": len(noop),
        }

    # -- reporting ----------------------------------------------------------

    def ticket_report(self, ticket_id: Any) -> Dict[str, Any]:
        tid = _nonempty_id(ticket_id, "ticket_id", LedgerError)
        return build_ticket_report(self.state, tid)

    def report(self) -> Dict[str, Any]:
        return build_report(self.state)

    # -- integrity / replay -------------------------------------------------

    def check(self) -> Dict[str, Any]:
        return check_state(self.state)


# Common alternative class names used in conversation.
Ledger = ResultLedger
LedgerStore = ResultLedger
LedgerBook = ResultLedger
ResultWriteoffLedger = ResultLedger
ResultWriteOffLedger = ResultLedger
WriteoffLedger = ResultLedger
WriteOffLedger = ResultLedger
VerificationLedger = ResultLedger


# Method-level aliases for the most common alternative vocabularies.
def _alias(name: str, target: str) -> None:
    setattr(ResultLedger, name, getattr(ResultLedger, target))


for _alias_name, _target in {
    "apply_batch": "import_batch",
    "commit_batch": "import_batch",
    "load_batch": "import_batch",
    "add_records": "import_batch",
    "import_results": "import_batch",
    "add": "add_record",
    "append": "add_record",
    "record": "add_record",
    "withdraw_result": "withdraw",
    "withdraw_results": "withdraw_many",
    "revoke": "withdraw",
    "get_ticket_report": "ticket_report",
    "ticket": "ticket_report",
    "status": "ticket_report",
    "get_status": "ticket_report",
    "snapshot": "report",
    "current_results": "report",
    "all_status": "report",
}.items():
    _alias(_alias_name, _target)


# ---------------------------------------------------------------------------
# Receipt extraction
# ---------------------------------------------------------------------------


def _iter_receipts(doc: Any) -> Iterable[Dict[str, Any]]:
    if doc is None:
        return []  # type: ignore[return-value]
    if isinstance(doc, dict):
        if isinstance(doc.get("receipts"), list):
            return [r for r in doc["receipts"] if isinstance(r, dict)]
        if isinstance(doc.get("receipt"), dict):
            return [doc["receipt"]]
        # A bare receipt itself.
        if "ticket_id" in doc and ("point" in doc or "status" in doc):
            return [doc]
        return []
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    return []


# ---------------------------------------------------------------------------
# Candidate evaluation and reports (pure over LedgerState)
# ---------------------------------------------------------------------------


def _record_view(
    state: LedgerState,
    record_id: str,
    *,
    chain: bool = False,
) -> Dict[str, Any]:
    record = state.records[record_id]
    code = record.get("status_code") or _STATUS_ALIASES.get(
        str(record.get("status", "")).strip(), STATUS_INSPECTED
    )
    view: Dict[str, Any] = dict(record)
    view.update(
        {
            "record_id": record_id,
            "parent_ids": list(record.get("parents", [])),
            "status_code": code,
            "status_zh": record.get("status_zh") or STATUS_ZH.get(code, ""),
            "withdrawn": record_id in state.withdrawn,
            "withdrawal": state.withdrawn.get(record_id),
        }
    )
    if chain:
        parents_of = {
            rid: tuple(r.get("parents", ())) for rid, r in state.records.items()
        }
        closure = ancestor_closure(record_id, parents_of)
        order = stable_topo_order(closure, parents_of)
        chain_views = [_record_view(state, rid) for rid in order]
        view["source_chain_ids"] = order
        view["source_chain"] = chain_views
    return view


def evaluate_candidates(
    state: LedgerState, ticket_id: str
) -> Tuple[List[str], List[str]]:
    """Return ``(candidate_ids, ticket_topo_order)`` for one ticket."""

    ticket_ids = {
        rid for rid, r in state.records.items() if r.get("ticket_id") == ticket_id
    }
    parents_of = {
        rid: tuple(
            p
            for p in r.get("parents", ())
            # only same-ticket parents can exist for a valid ledger
            if p in state.records
            and state.records[p].get("ticket_id") == ticket_id
        )
        for rid, r in state.records.items()
        if rid in ticket_ids
    }
    topo = stable_topo_order(set(ticket_ids), parents_of) if ticket_ids else []

    children_of = build_children_map(state.records)
    active = {rid for rid in ticket_ids if rid not in state.withdrawn}

    candidates: List[str] = []
    for rid in topo:
        if rid in state.withdrawn:
            continue
        descendants = descendant_closure(rid, children_of)
        has_active_descendant = any(
            d in active and state.records.get(d, {}).get("ticket_id") == ticket_id
            for d in descendants
        )
        if not has_active_descendant:
            candidates.append(rid)
    return candidates, topo


def build_ticket_report(state: LedgerState, ticket_id: str) -> Dict[str, Any]:
    candidates, topo = evaluate_candidates(state, ticket_id)
    candidate_views = [_record_view(state, rid, chain=True) for rid in candidates]

    if len(candidates) == 0:
        state_code = STATE_NO_CANDIDATE
        current_status = STATE_ZH[STATE_NO_CANDIDATE]
        current_result: Optional[Dict[str, Any]] = None
    elif len(candidates) == 1:
        state_code = STATE_UNIQUE
        adopted = candidate_views[0]
        current_status = adopted["status"]  # adopt the record's own status text
        current_result = adopted
    else:
        state_code = STATE_CONFLICT
        current_status = STATE_ZH[STATE_CONFLICT]
        current_result = None

    binding = state.bindings.get(ticket_id)
    point = binding.get("point") if binding else None
    evidence = binding.get("evidence") if binding else None
    receipt = binding.get("receipt") if binding else None

    ticket_records = [
        rid for rid in state.record_order
        if state.records[rid].get("ticket_id") == ticket_id
    ]
    withdrawn_count = sum(1 for rid in ticket_records if rid in state.withdrawn)

    report: Dict[str, Any] = {
        "schema": "road-ledger-ticket-report/1",
        "ticket_id": ticket_id,
        # State machine
        "state": state_code,
        "state_code": state_code,
        "state_zh": STATE_ZH[state_code],
        "status": current_status,
        "current_status": current_status,
        "current_result": current_result,
        "adopted": current_result,
        # Candidates + complete causal source chains
        "candidates": candidate_views,
        "candidate_ids": list(candidates),
        "candidate_count": len(candidates),
        "has_candidate": bool(candidates),
        "conflict": state_code == STATE_CONFLICT,
        "pending_review": state_code == STATE_CONFLICT,
        # Explicit decision basis: which rule produced the verdict and the
        # candidate chains it was derived from.
        "decision_basis": {
            "rule": (
                "候选=未撤回且不存在其他未撤回后代的结果；"
                "后代闭包穿过已撤回记录仍然有效；"
                "0 候选为未查验，1 候选采用其状态，多候选为冲突待核"
            ),
            "state": state_code,
            "candidate_ids": list(candidates),
            "adopted_record_id": (
                candidate_views[0]["id"] if state_code == STATE_UNIQUE else None
            ),
            "candidate_source_chain_ids": [
                list(c["source_chain_ids"]) for c in candidate_views
            ],
        },
        # Convenience tri-state inspection flag
        "inspected": (
            None
            if state_code != STATE_UNIQUE
            else current_result["status_code"] == STATUS_INSPECTED  # type: ignore[index]
        ),
        # Original ticketing point + basis, frozen from the bound receipt
        "binding": binding,
        "bound": binding is not None,
        "receipt": receipt,
        "receipt_hash": binding.get("receipt_hash") if binding else None,
        "bound_version_id": binding.get("version_id") if binding else None,
        "original_point": point,
        "origin_point": point,
        "point": point,
        "basis": evidence,
        "evidence": evidence,
        # Bookkeeping
        "known": bool(ticket_records or binding is not None),
        "record_count": len(ticket_records),
        "active_record_count": len(ticket_records) - withdrawn_count,
        "withdrawn_count": withdrawn_count,
        "topo_order": topo,
    }
    return report


def build_report(state: LedgerState) -> Dict[str, Any]:
    ticket_ids = sorted(
        {r.get("ticket_id") for r in state.records.values() if r.get("ticket_id")}
        | set(state.bindings.keys())
    )
    tickets = [build_ticket_report(state, tid) for tid in ticket_ids]

    state_counts = {STATE_NO_CANDIDATE: 0, STATE_UNIQUE: 0, STATE_CONFLICT: 0}
    for ticket in tickets:
        state_counts[ticket["state"]] += 1

    return {
        "schema": "road-ledger-report/1",
        "ticket_count": len(tickets),
        "state_counts": state_counts,
        "state_counts_zh": {
            STATE_ZH[k]: v for k, v in state_counts.items()
        },
        "conflict_count": state_counts[STATE_CONFLICT],
        "uninspected_count": state_counts[STATE_NO_CANDIDATE],
        "unique_count": state_counts[STATE_UNIQUE],
        "batch_count": len(state.batches),
        "record_count": len(state.records),
        "withdrawn_count": len(state.withdrawn),
        "bound_receipt_count": len(state.bindings),
        "tickets": tickets,
    }


def check_state(state: LedgerState) -> Dict[str, Any]:
    issues: List[str] = []

    for rid, record in state.records.items():
        if record.get("id") != rid:
            issues.append(f"记录行键与 id 不符: {rid}")
        tid = record.get("ticket_id")
        for pid in record.get("parents", []):
            parent = state.records.get(pid)
            if parent is None:
                issues.append(f"记录 {rid} 缺失父引用 {pid}")
            elif parent.get("ticket_id") != tid:
                issues.append(f"记录 {rid} 跨签父引用 {pid}")

    if detect_cycle(state.records) is None:
        issues.append("结果因果图存在环")

    for rid in state.withdrawn:
        if rid not in state.records:
            issues.append(f"撤回事件指向不存在的记录 {rid}")

    for bid, marker in state.batches.items():
        ids = marker.get("record_ids", [])
        if marker.get("record_count") != len(ids):
            issues.append(f"批次 {bid} record_count 与 record_ids 不符")
        normalized = []
        for rid in ids:
            record = state.records.get(rid)
            if record is None:
                issues.append(f"批次 {bid} 缺少记录 {rid}")
                continue
            if record.get("batch_id") != bid:
                issues.append(f"记录 {rid} 的 batch_id 与批次 {bid} 不符")
            normalized.append(record)
        if normalized and batch_content_hash(normalized) != marker.get("content_hash"):
            issues.append(f"批次 {bid} 内容指纹不符")

    for tid, binding in state.bindings.items():
        receipt = binding.get("receipt")
        if not isinstance(receipt, dict):
            issues.append(f"签 {tid} 绑定缺少完整回执")
        elif canonical_hash(receipt) != binding.get("receipt_hash"):
            issues.append(f"签 {tid} 回执哈希不符")

    return {
        "schema": "road-ledger-check/1",
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "record_count": len(state.records),
        "batch_count": len(state.batches),
        "withdrawn_count": len(state.withdrawn),
        "bound_receipt_count": len(state.bindings),
    }


# ---------------------------------------------------------------------------
# Functional helpers (handy API + replay entry points)
# ---------------------------------------------------------------------------


def replay_ledger(root: "str | Path") -> Dict[str, Any]:
    """Replay a ledger (directory or ``*.jsonl``) and report."""

    state = LedgerState.load(Path(root))
    return build_report(state)


def load_ledger(root: "str | Path") -> ResultLedger:
    return ResultLedger(root)


def open_ledger(root: "str | Path") -> ResultLedger:
    return ResultLedger(root)


def create_ledger(root: "str | Path") -> ResultLedger:
    return ResultLedger.initialize(root)


def import_batch(
    root_or_ledger: "str | Path | ResultLedger",
    raw: Any,
    *,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One-shot functional wrapper."""

    ledger = (
        root_or_ledger
        if isinstance(root_or_ledger, ResultLedger)
        else ResultLedger(root_or_ledger)
    )
    return ledger.import_batch(raw, batch_id=batch_id)


def add_record(
    root_or_ledger: "str | Path | ResultLedger",
    record: Dict[str, Any],
    *,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    ledger = (
        root_or_ledger
        if isinstance(root_or_ledger, ResultLedger)
        else ResultLedger(root_or_ledger)
    )
    return ledger.add_record(record, batch_id=batch_id)


def withdraw_result(
    root_or_ledger: "str | Path | ResultLedger", record_id: Any, **kwargs: Any
) -> Dict[str, Any]:
    ledger = (
        root_or_ledger
        if isinstance(root_or_ledger, ResultLedger)
        else ResultLedger(root_or_ledger)
    )
    return ledger.withdraw(record_id, **kwargs)


def ticket_status(
    root_or_ledger: "str | Path | ResultLedger", ticket_id: Any
) -> Dict[str, Any]:
    ledger = (
        root_or_ledger
        if isinstance(root_or_ledger, ResultLedger)
        else ResultLedger(root_or_ledger)
    )
    return ledger.ticket_report(ticket_id)


def ledger_report(root_or_ledger: "str | Path | ResultLedger") -> Dict[str, Any]:
    ledger = (
        root_or_ledger
        if isinstance(root_or_ledger, ResultLedger)
        else ResultLedger(root_or_ledger)
    )
    return ledger.report()


def candidates_for_ticket(
    root_or_state: "str | Path | ResultLedger | LedgerState", ticket_id: str
) -> List[Dict[str, Any]]:
    if isinstance(root_or_state, ResultLedger):
        state = root_or_state.state
    elif isinstance(root_or_state, LedgerState):
        state = root_or_state
    else:
        state = LedgerState.load(Path(root_or_state))
    ids, _ = evaluate_candidates(state, ticket_id)
    return [_record_view(state, rid, chain=True) for rid in ids]


# Alternative functional vocabulary.
evaluate_ticket = ticket_status
compute_ticket_report = ticket_status
compute_candidates = candidates_for_ticket


def evaluate(root_or_ledger: "str | Path | ResultLedger") -> Dict[str, Any]:
    return ledger_report(root_or_ledger)
