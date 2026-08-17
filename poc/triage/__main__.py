"""進入點：讀工單、跑分流、寫結果。

用法：
    uv run python -m triage                     # 全部 48 筆
    uv run python -m triage --offset 0 --limit 15   # 只跑前 15 筆
    uv run python -m triage --refresh           # 忽略既有回應，重新呼叫 API
"""

import argparse
import sys
from pathlib import Path

from triage import bootloader, ingest, pipeline, report, router

DEFAULT_SOURCE = bootloader.POC_ROOT.parent / "requirements" / "sample_tickets.xlsx"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage", description="旅遊電商客服工單分流")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="工單來源 xlsx")
    parser.add_argument("--out", type=Path, default=bootloader.POC_ROOT, help="輸出目錄")
    parser.add_argument("--offset", type=int, default=0, help="從第幾筆開始")
    parser.add_argument("--limit", type=int, default=0, help="最多跑幾筆，0 表示全部")
    parser.add_argument("--refresh", action="store_true", help="忽略既有回應，重新呼叫 API")
    args = parser.parse_args(argv)

    tickets = ingest.load(args.source)
    selected = tickets[args.offset :]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print(f"選取範圍內沒有工單（共 {len(tickets)} 筆，offset={args.offset}）")
        return 1

    engine = pipeline.build(refresh=args.refresh)
    mode = "呼叫 API" if engine.classifier.live else "重跑既有回應"
    print(f"[{mode}] 模型 {bootloader.MODEL}，處理 {len(selected)} / {len(tickets)} 筆", flush=True)

    rows = []
    for index, ticket in enumerate(selected, 1):
        row = engine.run_one(ticket)
        rows.append(row)
        print(
            f"  {index:>2}/{len(selected)} {ticket.ticket_id} "
            f"{row.primary or '-':<16} {row.decision.route:<5} {row.reply_mode}",
            flush=True,
        )

    args.out.mkdir(parents=True, exist_ok=True)
    report.write_csv(rows, args.out / "triage_result.csv")
    report.write_drafts(rows, args.out / "drafts.md")
    (args.out / "SUMMARY.md").write_text(report.build_summary(rows, selected), encoding="utf-8")

    human = sum(1 for r in rows if r.decision.route == router.HUMAN)
    print(f"完成：{len(rows)} 筆，轉真人 {human} 筆（{human / len(rows):.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
