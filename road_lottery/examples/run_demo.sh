#!/usr/bin/env bash
# End-to-end offline demo.  It never generates random numbers; all rational r
# values are supplied in the ticket JSON files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT="$SCRIPT_DIR/output"
PY="${PYTHON:-python3}"
mkdir -p "$OUT"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REPO="$WORK/repo"
EMPTY_REPO="$WORK/empty-repo"

road() {
  "$PY" -I "$ROOT/road.py" "$@"
}

echo "== 1. 期初已验版 v0 =="
road init --repo "$REPO" --input "$SCRIPT_DIR/frame_v0.json" --version-id v0 >/dev/null
road list-versions --repo "$REPO"

echo "== 2. 临时版同一批签号在 v0 出票 =="
road draw --repo "$REPO" --version v0 \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/receipts_v0.json" >/dev/null
road draw --repo "$REPO" --version v0 \
  --tickets "$SCRIPT_DIR/tickets_common_r.json" \
  --out "$OUT/receipts_common_r_v0.json" >/dev/null

echo "== 3. 整段退役 A-01，产生 v1；旧版 v0 不改 =="
road correct --repo "$REPO" \
  --input "$SCRIPT_DIR/correction_retire_a01.json" \
  --base v0 --version-id v1 >/dev/null
road draw --repo "$REPO" --version v1 \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/receipts_v1_after_retire.json" >/dev/null
road pair --repo "$REPO" --old v0 --new v1 \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/pair_v0_v1_retire.json" \
  --rearrange-out "$OUT/rearrange_orders_v0_v1.json" >/dev/null

echo "== 4. 直接配对两份已保存回执（证据内嵌，不反查老段） =="
road pair-receipts \
  --old-receipts "$OUT/receipts_v0.json" \
  --new-receipts "$OUT/receipts_v1_after_retire.json" \
  --out "$OUT/pair_from_saved_receipts.json" >/dev/null

echo "== 5. 指定老版重验某签 =="
road query --repo "$REPO" --version v0 \
  --ticket-id LOT-2026-0005 --receipts "$OUT/receipts_v0.json" \
  > "$OUT/query_old_ticket.json"
road verify-receipts --repo "$REPO" \
  --receipts "$OUT/receipts_v0.json" \
  --out "$OUT/verify_v0_receipts.json" >/dev/null

echo "== 6. 仅换路线归属：物理点不变，只发路线通知 =="
road correct --repo "$REPO" \
  --input "$SCRIPT_DIR/correction_route_only.json" \
  --base v0 --version-id v-route >/dev/null
road pair --repo "$REPO" --old v0 --new v-route \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/pair_route_only.json" >/dev/null

echo "== 7. 段内禁入导致段内移动 =="
road correct --repo "$REPO" \
  --input "$SCRIPT_DIR/correction_ban_first_meter.json" \
  --base v0 --version-id v-ban >/dev/null
road pair --repo "$REPO" --old v0 --new v-ban \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/pair_intra_move.json" >/dev/null

echo "== 8. 空框恢复有点，再失去点 =="
road init --repo "$EMPTY_REPO" --input "$SCRIPT_DIR/frame_empty.json" >/dev/null
road draw --repo "$EMPTY_REPO" --version v0 \
  --tickets "$SCRIPT_DIR/tickets_empty.json" \
  --out "$OUT/receipts_empty_v0.json" >/dev/null
road correct --repo "$EMPTY_REPO" \
  --input "$SCRIPT_DIR/correction_empty_add.json" \
  --base v0 --version-id v1 >/dev/null
road draw --repo "$EMPTY_REPO" --version v1 \
  --tickets "$SCRIPT_DIR/tickets_empty.json" \
  --out "$OUT/receipts_empty_v1.json" >/dev/null
road pair-receipts \
  --old-receipts "$OUT/receipts_empty_v0.json" \
  --new-receipts "$OUT/receipts_empty_v1.json" \
  --out "$OUT/pair_empty_recovery.json" >/dev/null
road correct --repo "$EMPTY_REPO" \
  --input "$SCRIPT_DIR/correction_empty_retire.json" \
  --base v1 --version-id v2 >/dev/null
road draw --repo "$EMPTY_REPO" --version v2 \
  --tickets "$SCRIPT_DIR/tickets_empty.json" \
  --out "$OUT/receipts_empty_v2.json" >/dev/null
road pair-receipts \
  --old-receipts "$OUT/receipts_empty_v1.json" \
  --new-receipts "$OUT/receipts_empty_v2.json" \
  --out "$OUT/pair_empty_loss.json" >/dev/null

echo "== 9. 导出自包含离线核查包并独立核验 =="
road audit --repo "$REPO" --version v0 \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/audit_bundle_v0.json" >/dev/null
road verify-audit --input "$OUT/audit_bundle_v0.json" \
  --out "$OUT/audit_verification.json" >/dev/null
road audit --repo "$REPO" --version v1 \
  --tickets "$SCRIPT_DIR/tickets_varied.json" \
  --out "$OUT/audit_bundle_v1_retire.json" >/dev/null
road verify-audit --input "$OUT/audit_bundle_v1_retire.json" \
  --out "$OUT/audit_verification_v1.json" >/dev/null

echo "完成。关键输出："
echo "  $OUT/pair_v0_v1_retire.json"
echo "  $OUT/rearrange_orders_v0_v1.json"
echo "  $OUT/pair_route_only.json"
echo "  $OUT/pair_empty_recovery.json"
echo "  $OUT/pair_empty_loss.json"
echo "  $OUT/audit_verification.json"
