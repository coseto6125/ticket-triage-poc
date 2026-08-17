"""讀取與前處理。

原始附件是 .xlsx，用標準函式庫解開，不引入試算表套件。每一項清理都會記錄在
Ticket.notes 裡，執行摘要直接統計這些註記，README 的「資料發現」就不需要另外
用眼睛數。
"""

import html
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, NamedTuple

TAIPEI: Final = timezone(timedelta(hours=8))
# Excel 1900 日期系統的實際起點：序列值 1 對應 1900-01-01，且保留了 1900 閏年的錯誤
EXCEL_EPOCH: Final = datetime(1899, 12, 30, tzinfo=TAIPEI)

_CELL_RE: Final = re.compile(r"<c[^>]*>.*?</c>|<c[^>]*/>", re.DOTALL)
_ROW_RE: Final = re.compile(r"<row[^>]*>(.*?)</row>", re.DOTALL)
_SI_RE: Final = re.compile(r"<si>(.*?)</si>", re.DOTALL)
_TAG_RE: Final = re.compile(r"<[^>]+>")
_TYPE_RE: Final = re.compile(r't="([^"]+)"')
_VALUE_RE: Final = re.compile(r"<v>(.*?)</v>", re.DOTALL)
_INLINE_RE: Final = re.compile(r"<is>(.*?)</is>", re.DOTALL)

_BR_RE: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE: Final = re.compile(r"<[^>]{1,40}>")
_SIGNATURE_RE: Final = re.compile(r"\n?--\s*從我的\s*\S+\s*傳送\s*$")
_BLANKS_RE: Final = re.compile(r"[ \t]{2,}")
_NEWLINES_RE: Final = re.compile(r"\n{3,}")

CHANNELS: Final[dict[str, str]] = {"email": "email", "表單": "form"}

# 只把全形英數與全形空白轉半形。全形標點在中文裡是正確寫法，不動它。
_WIDTH_MAP: Final = str.maketrans(
    {c: chr(c - 0xFEE0) for c in [*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B), *range(0xFF41, 0xFF5B)]}
    | {0x3000: " "}
)


class Ticket(NamedTuple):
    """一封前處理完成的工單。"""

    ticket_id: str
    created_at: datetime
    channel: str
    customer_name: str
    subject: str
    body: str
    notes: tuple[str, ...]


def _cell(chunk: str, shared: list[str]) -> str:
    if inline := _INLINE_RE.search(chunk):
        return html.unescape(_TAG_RE.sub("", inline.group(1)))
    if not (value := _VALUE_RE.search(chunk)):
        return ""
    kind = _TYPE_RE.search(chunk)
    if kind is not None and kind.group(1) == "s":
        return shared[int(value.group(1))]
    return value.group(1)


def _rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as book:
        shared = [
            html.unescape(_TAG_RE.sub("", chunk))
            for chunk in _SI_RE.findall(book.read("xl/sharedStrings.xml").decode("utf-8"))
        ]
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
    return [[_cell(c, shared) for c in _CELL_RE.findall(row)] for row in _ROW_RE.findall(sheet)]


def _clean(text: str, notes: list[str]) -> str:
    """清理自由文字，把每一項改動記進 notes。"""
    if _BR_RE.search(text):
        text = _BR_RE.sub("\n", text)
        notes.append("HTML 換行標籤")
    if _HTML_TAG_RE.search(text):
        text = _HTML_TAG_RE.sub("", text)
        notes.append("HTML 標籤殘留")
    if _SIGNATURE_RE.search(text):
        text = _SIGNATURE_RE.sub("", text)
        notes.append("郵件簽名檔")
    widened = text.translate(_WIDTH_MAP)
    if widened != text:
        notes.append("全形英數字")
        text = widened
    squeezed = _NEWLINES_RE.sub("\n\n", _BLANKS_RE.sub(" ", text)).strip()
    if squeezed != text:
        notes.append("多餘空白")
    return squeezed


def load(path: Path) -> list[Ticket]:
    """讀進工單並前處理。第一列是欄位名，跳過。"""
    tickets: list[Ticket] = []
    for row in _rows(path)[1:]:
        if len(row) < 6 or not row[0]:
            continue
        notes: list[str] = []
        created = EXCEL_EPOCH + timedelta(days=float(row[1]))
        channel = CHANNELS.get(row[2], row[2])
        if channel != row[2]:
            notes.append("管道名稱正規化")
        subject = _clean(row[4], notes)
        body = _clean(row[5], notes)
        if len(body) < 12:
            notes.append("內文過短")
        tickets.append(
            Ticket(
                ticket_id=row[0],
                created_at=created,
                channel=channel,
                customer_name=row[3].strip(),
                subject=subject,
                body=body,
                notes=tuple(dict.fromkeys(notes)),
            )
        )
    return tickets
