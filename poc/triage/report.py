"""輸出：分流結果 CSV、草稿、執行摘要。"""

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Final, NamedTuple

from triage import catalog, router
from triage.ingest import Ticket

FIELDS: Final[tuple[str, ...]] = (
    "ticket_id",
    "created_at",
    "channel",
    "category",
    "category_label",
    "scenarios",
    "route",
    "reply_mode",
    "priority",
    "is_complaint",
    "confidence",
    "triggers",
    "tools",
    "pii_count",
    "banned_hits",
    "money_mentioned",
    "reason",
)


class Row(NamedTuple):
    """一封工單的分流結果。"""

    ticket: Ticket
    decision: router.Decision
    scenarios: tuple[str, ...]
    is_complaint: bool
    confidence: float
    pii_count: int
    money_mentioned: str
    draft: str
    reply_mode: str
    banned_hits: tuple[str, ...]

    @property
    def primary(self) -> str:
        """主要服務情境，降級時為空字串。"""
        return self.scenarios[0] if self.scenarios else ""

    def as_record(self) -> dict[str, str]:
        # CSV 的 category 欄位沿用題目指定的名稱，內容是主要服務情境
        return {
            "ticket_id": self.ticket.ticket_id,
            "created_at": self.ticket.created_at.isoformat(),
            "channel": self.ticket.channel,
            "category": self.primary,
            "category_label": catalog.BY_NAME[self.primary].label if self.primary else "",
            "scenarios": "|".join(self.scenarios),
            "route": self.decision.route,
            "reply_mode": self.reply_mode,
            "priority": self.decision.priority,
            "is_complaint": "true" if self.is_complaint else "false",
            "confidence": f"{self.confidence:.2f}",
            "triggers": "|".join(self.decision.triggers),
            "tools": "|".join(catalog.tools_for(list(self.scenarios))),
            "pii_count": str(self.pii_count),
            "banned_hits": "|".join(self.banned_hits),
            "money_mentioned": self.money_mentioned,
            "reason": self.decision.reason,
        }


def write_csv(rows: list[Row], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(row.as_record() for row in rows)


def write_drafts(rows: list[Row], path: Path) -> None:
    blocks = [
        f"## {row.ticket.ticket_id}（{row.reply_mode} / {row.decision.route}）\n\n{row.draft}\n"
        for row in rows
        if row.draft
    ]
    header = "# 草稿回覆\n\n由情境範本組出，內容為模擬，非任何旅行社的真實規定。個資已於寫出前還原。\n\n"
    path.write_text(header + "\n".join(blocks), encoding="utf-8")


def _table(title: str, counts: Counter[str], total: int, label: str) -> list[str]:
    lines = [f"### {title}", "", f"| {label} | 件數 | 佔比 |", "| --- | ---: | ---: |"]
    lines.extend(f"| {name} | {n} | {n / total:.0%} |" for name, n in counts.most_common())
    return [*lines, ""]


def build_summary(rows: list[Row], tickets: list[Ticket]) -> str:
    """組出執行摘要。"""
    total = len(rows)
    by_scenario: Counter[str] = Counter()
    for row in rows:
        for name in row.scenarios:
            by_scenario[f"{catalog.BY_NAME[name].label}（{name}）"] += 1
    by_group = Counter(
        catalog.GROUP_BY_NAME[catalog.BY_NAME[row.primary].group].label for row in rows if row.primary
    )
    by_route = Counter(row.decision.route for row in rows)
    by_mode = Counter(row.reply_mode for row in rows)
    by_trigger = Counter(t for row in rows for t in row.decision.triggers)
    by_note = Counter(n for t in tickets for n in t.notes)

    # 用寄件者姓名分組，但只輸出工單編號。這份摘要是統計文件，不需要知道是誰。
    same_sender: defaultdict[str, list[str]] = defaultdict(list)
    for ticket in tickets:
        same_sender[ticket.customer_name].append(ticket.ticket_id)
    repeats = [ids for ids in same_sender.values() if len(ids) > 1]
    guarded = sum(1 for row in rows if row.banned_hits)

    multi = sum(1 for row in rows if len(row.scenarios) > 1)
    lines = [
        "# 執行摘要",
        "",
        f"工單總數 {total} 件。",
        "",
        "## 分類分佈",
        "",
        "一封工單可以同時屬於多個情境，所以情境件數合計會大於工單數。",
        "",
    ]
    lines += _table("依服務情境", by_scenario, total, "情境")
    lines += _table("依情境群組（決定下游查哪個系統）", by_group, total, "群組")
    lines += _table("依分流", by_route, total, "分流")
    lines += _table("依回覆型態", by_mode, total, "回覆型態")
    lines += _table("轉真人的觸發規則", by_trigger, total, "規則") if by_trigger else []

    lines += [
        "## 資料問題",
        "",
        f"同時命中多個情境：{multi} 件。",
        "",
        "### 前處理時修掉的雜訊",
        "",
        "| 項目 | 件數 |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {n} |" for name, n in by_note.most_common()]
    lines += [
        "",
        "### 疑似重複來信",
        "",
        "同一位寄件者的多封工單，未自動合併，理由見 README。此處只列工單編號，不列姓名。",
        "",
    ]
    lines += [f"- {'、'.join(ids)}" for ids in repeats] if repeats else ["- 無"]
    lines += ["", "### 禁用詞攔截", "", f"草稿因命中承諾句式而被降級：{guarded} 件。"]
    return "\n".join(lines) + "\n"
