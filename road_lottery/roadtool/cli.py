"""Command line interface for the offline road lottery toolkit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from .core import (
    BadTicketSheet,
    BundleSource,
    Repository,
    RoadToolError,
    _row_from_receipt,
    build_audit_bundle,
    draw_one,
    draw_tickets,
    pair_receipt_docs,
    parse_rational,
    read_json_file,
    safe_version_id,
    verify_audit_bundle,
    verify_receipts_doc,
)


def load_json_input(path: str) -> Any:
    if path == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise RoadToolError(f"标准输入 JSON 解析失败: {exc}") from exc
    p = Path(path)
    if not p.is_file():
        raise RoadToolError(f"输入文件不存在: {path}")
    try:
        return read_json_file(p)
    except json.JSONDecodeError as exc:
        raise RoadToolError(f"JSON 解析失败 {path}: {exc}") from exc


def emit(result: Any, out_path: Optional[str] = None) -> int:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "written": str(path), "bytes": len(text.encode())}))
    else:
        print(text)
    return 0


def open_repo(args: argparse.Namespace) -> Repository:
    if not args.repo:
        raise RoadToolError("请用 --repo 指定离线仓库目录")
    repo = Repository(args.repo)
    repo.require_open()
    return repo


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    frame = load_json_input(args.input)
    repo = Repository.initialize(args.repo, frame, version_id=args.version_id)
    manifest = repo.load_version(args.version_id)
    return emit({"ok": True, "manifest": manifest})


def cmd_correct(args: argparse.Namespace) -> int:
    raw = load_json_input(args.input)
    base = args.base
    if not base and isinstance(raw, dict):
        base = raw.get("base_version")
    if not base:
        raise RoadToolError("请用 --base 或更正文件中的 base_version 指定基准版")
    repo = open_repo(args)
    manifest = repo.apply_correction(
        base, raw, new_version_id=args.version_id
    )
    return emit({"ok": True, "manifest": manifest})


def cmd_draw(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    manifest = repo.load_version(args.version)
    sheet = load_json_input(args.tickets)
    doc = draw_tickets(repo, manifest, sheet)
    return emit(doc, args.out)


def cmd_query(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    manifest = repo.load_version(args.version)

    stored = None
    if args.receipts:
        doc = load_json_input(args.receipts)
        receipts = doc.get("receipts", [])
        if args.ticket_index is not None:
            if not 0 <= args.ticket_index < len(receipts):
                raise RoadToolError("ticket-index 超出回执范围")
            stored = receipts[args.ticket_index]
        elif args.ticket_id:
            matches = [r for r in receipts if r.get("ticket_id") == args.ticket_id]
            if not matches:
                raise RoadToolError(f"回执中找不到签: {args.ticket_id}")
            if len(matches) > 1:
                raise RoadToolError(
                    f"签号 {args.ticket_id} 有重复，请用 --ticket-index 指定；"
                    f"匹配位置: {[r.get('ticket_index') for r in matches]}"
                )
            stored = matches[0]
        else:
            raise RoadToolError("使用 --receipts 时还需指定 --ticket-id 或 --ticket-index")

    if stored is not None:
        row = _row_from_receipt(stored)
    else:
        if not args.ticket_id:
            raise RoadToolError("直接查询需要 --ticket-id 和 --r")
        if args.r is None:
            raise RoadToolError("直接查询需要 --r；或用 --receipts 从原回执取 r")
        num, den = parse_rational(args.r, "查询参数 --r")
        row = {
            "ticket_index": args.ticket_index if args.ticket_index is not None else 0,
            "ticket_id": args.ticket_id,
            "r_num": num,
            "r_den": den,
            "r": f"{num}/{den}",
        }

    if args.r is not None and stored is not None:
        num, den = parse_rational(args.r, "查询参数 --r")
        row.update({"r_num": num, "r_den": den, "r": f"{num}/{den}"})

    receipt = draw_one(repo, manifest, row)
    return emit(receipt)


def cmd_verify_receipts(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    doc = load_json_input(args.receipts)
    report = verify_receipts_doc(repo, doc)
    if args.out:
        emit(report, args.out)
    else:
        emit(report)
    return 0 if report["ok"] else 1


def _pair_docs(
    old_doc: dict[str, Any], new_doc: dict[str, Any], rearrange_out: Optional[str]
) -> dict[str, Any]:
    report = pair_receipt_docs(old_doc, new_doc)
    if rearrange_out:
        orders_doc = {
            "schema": "road-rearrangement-orders/1",
            "old_version_id": report["old_version_id"],
            "new_version_id": report["new_version_id"],
            "order_count": len(report["change_orders"]),
            "orders": report["change_orders"],
            "route_only_notices": report["route_only_notices"],
        }
        path = Path(rearrange_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(orders_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def cmd_pair(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    old_manifest = repo.load_version(args.old)
    new_manifest = repo.load_version(args.new)
    sheet = load_json_input(args.tickets)
    old_doc = draw_tickets(repo, old_manifest, sheet)
    new_doc = draw_tickets(repo, new_manifest, sheet)
    report = _pair_docs(old_doc, new_doc, args.rearrange_out)
    return emit(report, args.out)


def cmd_pair_receipts(args: argparse.Namespace) -> int:
    old_doc = load_json_input(args.old_receipts)
    new_doc = load_json_input(args.new_receipts)
    report = _pair_docs(old_doc, new_doc, args.rearrange_out)
    return emit(report, args.out)


def cmd_audit(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    sheet = load_json_input(args.tickets) if args.tickets else None
    bundle = build_audit_bundle(repo, args.version, sheet)
    return emit(bundle, args.out)


def cmd_verify_audit(args: argparse.Namespace) -> int:
    bundle = load_json_input(args.input)
    report = verify_audit_bundle(bundle)
    if args.out:
        emit(report, args.out)
    else:
        emit(report)
    return 0 if report["ok"] else 1


def cmd_show_version(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    return emit(repo.load_version(args.version))


def cmd_list_versions(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    version_dir = repo.root / "versions"
    items = []
    if version_dir.is_dir():
        for path in sorted(version_dir.glob("*.json")):
            manifest = read_json_file(path)
            items.append(
                {
                    "version_id": manifest.get("version_id"),
                    "parent_id": manifest.get("parent_id"),
                    "total_effective_coordinates": manifest.get(
                        "total_effective_coordinates"
                    ),
                    "segment_count": manifest.get("segment_count"),
                    "op_count": manifest.get("op_count"),
                    "correction_hash": manifest.get("correction_hash"),
                }
            )
    return emit({"versions": items})


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="road_tool",
        description="Python 离线路面养护抽签出票、回放、配对与核查工具（不产生随机数）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    repo_parent = argparse.ArgumentParser(add_help=False)
    repo_parent.add_argument("--repo", required=True, help="离线仓库目录")

    p_init = sub.add_parser("init", parents=[repo_parent], help="建立期初已验版")
    p_init.add_argument("--input", required=True, help="期初路线与路段 JSON")
    p_init.add_argument("--version-id", default="v0", help="期初版本号，默认 v0")
    p_init.add_argument("--out", help="把结果写入文件")
    p_init.set_defaults(handler=cmd_init)

    p_corr = sub.add_parser("correct", parents=[repo_parent], help="从指定已验版递交更正")
    p_corr.add_argument("--base", help="基准版本号；缺省读取输入中的 base_version")
    p_corr.add_argument("--input", required=True, help="只含增删改项的更正 JSON")
    p_corr.add_argument("--version-id", help="新版本号；缺省按内容派生")
    p_corr.add_argument("--out", help="把结果写入文件")
    p_corr.set_defaults(handler=cmd_correct)

    p_draw = sub.add_parser("draw", parents=[repo_parent], help="在指定版本出票")
    p_draw.add_argument("--version", required=True)
    p_draw.add_argument("--tickets", required=True, help="固定顺序签号和 r 的 JSON")
    p_draw.add_argument("--out", help="回执输出 JSON 文件")
    p_draw.set_defaults(handler=cmd_draw)

    p_query = sub.add_parser("query", parents=[repo_parent], help="查询/重验某张签")
    p_query.add_argument("--version", required=True)
    p_query.add_argument("--ticket-id")
    p_query.add_argument("--ticket-index", type=int)
    p_query.add_argument("--r", help="该签的精确有理数，如 1/3 或 0")
    p_query.add_argument("--receipts", help="原出票回执文件，可从中取 r")
    p_query.set_defaults(handler=cmd_query)

    p_verify = sub.add_parser(
        "verify-receipts", parents=[repo_parent], help="在指定仓库重算整份回执"
    )
    p_verify.add_argument("--receipts", required=True)
    p_verify.add_argument("--out")
    p_verify.set_defaults(handler=cmd_verify_receipts)

    p_pair = sub.add_parser("pair", parents=[repo_parent], help="在新旧两版回放并配对")
    p_pair.add_argument("--old", required=True, help="老版本号")
    p_pair.add_argument("--new", required=True, help="新版本号")
    p_pair.add_argument("--tickets", required=True, help="同一批签号 JSON")
    p_pair.add_argument("--out", help="完整配对报告输出文件")
    p_pair.add_argument("--rearrange-out", help="实际变点签的重排单输出文件")
    p_pair.set_defaults(handler=cmd_pair)

    p_pair_r = sub.add_parser(
        "pair-receipts", help="直接配对两份已保存回执（保留两版证据）"
    )
    p_pair_r.add_argument("--old-receipts", required=True)
    p_pair_r.add_argument("--new-receipts", required=True)
    p_pair_r.add_argument("--out")
    p_pair_r.add_argument("--rearrange-out")
    p_pair_r.set_defaults(handler=cmd_pair_receipts)

    p_audit = sub.add_parser("audit", parents=[repo_parent], help="导出自包含核查包")
    p_audit.add_argument("--version", required=True)
    p_audit.add_argument("--tickets", help="可选：同时把签号与回执放入核查包")
    p_audit.add_argument("--out", required=True)
    p_audit.set_defaults(handler=cmd_audit)

    p_vaudit = sub.add_parser("verify-audit", help="不依赖仓库核验自包含核查包")
    p_vaudit.add_argument("--input", required=True)
    p_vaudit.add_argument("--out")
    p_vaudit.set_defaults(handler=cmd_verify_audit)

    p_show = sub.add_parser("show-version", parents=[repo_parent])
    p_show.add_argument("--version", required=True)
    p_show.set_defaults(handler=cmd_show_version)

    p_list = sub.add_parser("list-versions", parents=[repo_parent])
    p_list.set_defaults(handler=cmd_list_versions)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except RoadToolError as exc:
        payload = {"ok": False, "error": str(exc), "code": exc.code}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
        payload = {"ok": False, "error": f"输入/输出或数值错误: {exc}"}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
