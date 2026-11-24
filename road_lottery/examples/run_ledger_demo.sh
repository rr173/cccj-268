#!/usr/bin/env bash
# End-to-end ledger demo with tiny receipts:
# fork -> same-text conflict -> multi-parent merge -> withdrawals,
# frozen original ticketing point across a route revision,
# whole-batch rejection cases, and order-independent independent replay.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LEDGER_EX="$SCRIPT_DIR/ledger"
OUT="$SCRIPT_DIR/output/ledger"
PY="${PYTHON:-python3}"

road() {
  "$PY" -I "$ROOT/road.py" "$@"
}

mkdir -p "$OUT"
rm -rf "$OUT/demo_ledger"
LED="$OUT/demo_ledger"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REPO="$WORK/repo"

echo "== 1. 极小框 v0 出三张小小回执 =="
road init --repo "$REPO" --input "$SCRIPT_DIR/ledger_frame.json" --version-id v0 >/dev/null
road draw --repo "$REPO" --version v0 \
  --tickets "$SCRIPT_DIR/ledger_tickets.json" \
  --out "$OUT/receipts_v0_small.json" >/dev/null

echo "== 2. 账本绑定回执（原出票点/依据快照，不可改写） =="
road ledger-bind --ledger "$LED" \
  --receipts "$OUT/receipts_v0_small.json" \
  --out "$OUT/01_bind.json" >/dev/null

echo "== 3. 导入根批次 B1，同内容重送为 no-op =="
road ledger-import --ledger "$LED" \
  --input "$LEDGER_EX/batch_1_base.json" \
  --out "$OUT/02_import_b1.json" >/dev/null
road ledger-import --ledger "$LED" \
  --input "$LEDGER_EX/batch_1_base.json" \
  --out "$OUT/03_redeliver_noop.json" >/dev/null

echo "== 4. 两个离线分支都接续 R1（状态文字相同）-> 冲突待核 =="
road ledger-import --ledger "$LED" --input "$LEDGER_EX/batch_2_fork_left.json" >/dev/null
road ledger-import --ledger "$LED" --input "$LEDGER_EX/batch_3_fork_right.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/04_fork_same_status_conflict.json" >/dev/null

echo "== 5. 多父合并 R4 -> 唯一候选并采用其状态，附完整来源链 =="
road ledger-import --ledger "$LED" --input "$LEDGER_EX/batch_4_merge.json" \
  --out "$OUT/05_import_merge.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/06_merge_adopted.json" >/dev/null

echo "== 6. 撤回合并末端 R4 -> 两个分支重新成为候选 =="
road ledger-withdraw --ledger "$LED" --record R4 \
  --reason "合并依据不足，撤回复核结果" --by "核销员" \
  --out "$OUT/07_withdraw_r4.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/08_after_merge_withdrawn.json" >/dev/null

echo "== 7. 撤回分支 R3 -> 另一分支 R2 不丢失，成为唯一候选 =="
road ledger-withdraw --ledger "$LED" --record R3 \
  --reason "丙组回执串签，撤回" \
  --out "$OUT/09_withdraw_r3.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/10_r2_unique_other_branch_kept.json" >/dev/null

echo "== 8. 再撤回末端 R2 -> 前序 R1 恢复 =="
road ledger-withdraw --ledger "$LED" --record R2 \
  --out "$OUT/11_withdraw_r2.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/12_r1_restored.json" >/dev/null

echo "== 9. T2 未到场 -> 唯一采用；撤回后无候选 -> 未查验 =="
road ledger-import --ledger "$LED" --input "$LEDGER_EX/batch_5_t2.json" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T2 \
  --out "$OUT/13_t2_not_present.json" >/dev/null
road ledger-withdraw --ledger "$LED" --record R5 \
  --reason "记录作废" >/dev/null
road ledger-ticket --ledger "$LED" --ticket T2 \
  --out "$OUT/14_t2_all_withdrawn_uninspected.json" >/dev/null

echo "== 10. T3 从未有结果，但绑定了回执：未查验且仍展示原出票点/依据 =="
road ledger-ticket --ledger "$LED" --ticket T3 \
  --out "$OUT/15_t3_bound_but_uninspected.json" >/dev/null

echo "== 11. 路线换版 v1 移动出票点；新回执不能改写账本原出票点 =="
road correct --repo "$REPO" --base v0 --version-id v1 \
  --input "$SCRIPT_DIR/ledger_correction_move.json" >/dev/null
road draw --repo "$REPO" --version v1 \
  --tickets "$SCRIPT_DIR/ledger_tickets.json" \
  --out "$OUT/receipts_v1_small_moved.json" >/dev/null
if road ledger-bind --ledger "$LED" \
    --receipts "$OUT/receipts_v1_small_moved.json" \
    >/dev/null 2> "$OUT/16_rebind_rejected.json"; then
  echo "ERROR: 换版回执竟然覆盖了原出票绑定" >&2
  exit 1
fi
road ledger-ticket --ledger "$LED" --ticket T1 \
  --out "$OUT/17_origin_still_v0_point.json" >/dev/null

echo "== 12. 失败批次：全部整批拒绝，错误码落盘 =="
reject() {
  local title="$1" input="$2" outfile="$3"
  if road ledger-import --ledger "$LED" --input "$input" \
      >/dev/null 2> "$OUT/$outfile"; then
    echo "ERROR: 坏批次未被拒绝: $title" >&2
    exit 1
  fi
  echo "  rejected: $title -> $outfile"
}
reject "同号异内容"   "$LEDGER_EX/bad_same_id_diff_content.json" "18_reject_same_id_diff_content.json"
reject "记录ID重用"   "$LEDGER_EX/bad_id_reuse.json"             "19_reject_id_reuse.json"
reject "跨签引用"     "$LEDGER_EX/bad_cross_ticket.json"         "20_reject_cross_ticket.json"
reject "缺失引用"     "$LEDGER_EX/bad_missing_ref.json"          "21_reject_missing_ref.json"
reject "因果环"       "$LEDGER_EX/bad_cycle.json"                "22_reject_cycle.json"

echo "== 13. 最终全量报告 + JSONL 账本自检 =="
road ledger-report --ledger "$LED" --out "$OUT/23_final_report.json" >/dev/null
road ledger-check  --ledger "$LED" --out "$OUT/24_ledger_check.json" >/dev/null

echo "== 14. 行顺序无关 + 独立重放一致 =="
LA="$WORK/replay_a"
LB="$WORK/replay_b"
road ledger-import --ledger "$LA" --input "$LEDGER_EX/batch_1_base.json" >/dev/null
road ledger-import --ledger "$LB" --input "$LEDGER_EX/batch_1_base.json" >/dev/null
road ledger-import --ledger "$LA" --input "$LEDGER_EX/replay_order_a.json" >/dev/null
road ledger-import --ledger "$LB" --input "$LEDGER_EX/replay_order_b.json" >/dev/null
road ledger-report --ledger "$LA" --out "$OUT/25_replay_report_a.json" >/dev/null
road ledger-report --ledger "$LB" --out "$OUT/26_replay_report_b.json" >/dev/null
if diff -u "$OUT/25_replay_report_a.json" "$OUT/26_replay_report_b.json" \
    > "$OUT/27_replay_diff.txt"; then
  echo "MATCH: 两种行顺序的独立账本报告完全一致" | tee "$OUT/28_replay_consistency.txt"
else
  echo "ERROR: 独立重放报告不一致，见 27_replay_diff.txt" >&2
  exit 1
fi

echo
echo "完成。账本 JSONL 原文件："
echo "  $LED/records.jsonl"
echo "  $LED/withdrawals.jsonl"
echo "  $LED/batches.jsonl"
echo "  $LED/bindings.json"
echo "演示输出目录：$OUT"
