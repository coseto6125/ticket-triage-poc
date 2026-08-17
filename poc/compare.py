"""把跑出來的分流結果和人工標註比對，列出不一致的地方。

用法：uv run python compare.py [--offset N] [--limit N]

這是開發期的檢查工具，不是管線的一部分。標註在 data/expected.json，寫在看到任何
模型輸出之前；比對出來的差異要逐筆看過，決定是標註錯了還是模型錯了。
"""

import argparse
import json
import sys

from triage import bootloader, ingest, pipeline
from triage.__main__ import DEFAULT_SOURCE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)

    expected = {
        k: v
        for k, v in json.loads((bootloader.DATA_DIR / "expected.json").read_text("utf-8")).items()
        if not k.startswith("_")
    }
    tickets = ingest.load(DEFAULT_SOURCE)[args.offset :]
    if args.limit:
        tickets = tickets[: args.limit]

    engine = pipeline.build(refresh=args.refresh)
    agree = 0
    for ticket in tickets:
        row = engine.run_one(ticket)
        want = expected[ticket.ticket_id]
        ok_primary = row.category == want["primary"] or row.category in want.get("alt_primary", [])
        ok_complaint = row.is_complaint == want["is_complaint"]
        ok_route = row.decision.route == want["route"]
        if ok_primary and ok_complaint and ok_route:
            agree += 1
            continue
        diffs = []
        if not ok_primary:
            diffs.append(f"情境 期望 {want['primary']} 得到 {row.category or '-'}")
        if not ok_complaint:
            diffs.append(f"客訴 期望 {want['is_complaint']} 得到 {row.is_complaint}")
        if not ok_route:
            diffs.append(f"分流 期望 {want['route']} 得到 {row.decision.route}")
        print(f"{ticket.ticket_id}  {' | '.join(diffs)}")
        print(f"           情境集 {'|'.join(row.scenarios) or '-'}  觸發 {'|'.join(row.decision.triggers) or '-'}")
        print(f"           模型理由 {row.decision.reason}")

    total = len(tickets)
    print(f"\n一致 {agree}/{total}（{agree / total:.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
