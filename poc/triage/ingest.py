"""讀取與前處理。

原始附件是 .xlsx，用標準函式庫解開，不引入試算表套件。xlsx 內部就是一包 XML，所以
用 ElementTree 解析結構、用正則只處理自由文字：把「有規範的格式」交給解析器，正則
留給沒有規範的內容。每一項清理都會記錄在 Ticket.notes 裡，執行摘要直接統計這些註記，
README 的「資料發現」就不需要另外用眼睛數。
"""

import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, NamedTuple
from xml.etree import ElementTree

TAIPEI: Final = timezone(timedelta(hours=8))
# Excel 1900 日期系統的實際起點：序列值 1 對應 1900-01-01，且保留了 1900 閏年的錯誤
EXCEL_EPOCH: Final = datetime(1899, 12, 30, tzinfo=TAIPEI)

_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

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


def _joined_text(node: ElementTree.Element) -> str:
    """把一個節點底下的 <t> 串起來。只取直屬的與 <r> 裡的，跳過注音欄位 <rPh>。"""
    parts = [*node.findall(f"{_NS}t"), *node.findall(f"{_NS}r/{_NS}t")]
    return "".join(part.text or "" for part in parts)


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    if (inline := cell.find(f"{_NS}is")) is not None:
        return _joined_text(inline)
    if (value := cell.find(f"{_NS}v")) is None or value.text is None:
        return ""
    return shared[int(value.text)] if cell.get("t") == "s" else value.text


def _column_index(reference: str) -> int:
    """把 A2、AA10 這種儲存格位址換成 0 起算的欄位索引。"""
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _row_values(row: ElementTree.Element, shared: list[str]) -> list[str]:
    """依儲存格自己宣告的位址把值放回它真正的欄位。

    Excel 會省略空白儲存格的 <c> 元素，所以照出現順序讀會讓空格後面的欄位整排左移：
    一封主旨是空的工單，內文會被當成主旨、姓名會被當成內文。這種錯位不會報錯，只會
    安靜地產生錯的分類結果。
    """
    cells: dict[int, str] = {}
    for cell in row.findall(f"{_NS}c"):
        reference = cell.get("r", "")
        index = _column_index(reference) if reference else len(cells)
        cells[index] = _cell_text(cell, shared)
    return [cells.get(i, "") for i in range(max(cells) + 1)] if cells else []


def _rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as book:
        strings = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
        shared = [_joined_text(si) for si in strings.findall(f"{_NS}si")]
        sheet = ElementTree.fromstring(book.read("xl/worksheets/sheet1.xml"))
    return [_row_values(row, shared) for row in sheet.iter(f"{_NS}row")]


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
    """讀進工單並前處理。第一列是欄位名，跳過。

    整列空白就跳過；有內容但缺必要欄位就拋例外。安靜地略過一列，等於有一封工單從此
    不存在，不會被分類也不會被人看到，這是這條管線最不能有的失敗方式。
    """
    tickets: list[Ticket] = []
    for number, row in enumerate(_rows(path)[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        padded = [*row, *[""] * (6 - len(row))]
        if not padded[0] or not padded[1]:
            raise ValueError(f"第 {number} 列缺少工單編號或建立時間，無法處理：{padded[:3]}")
        notes: list[str] = []
        try:
            created = EXCEL_EPOCH + timedelta(days=float(padded[1]))
        except ValueError as exc:
            raise ValueError(f"第 {number} 列的建立時間不是 Excel 序列值：{padded[1]!r}") from exc
        channel = CHANNELS.get(padded[2], padded[2])
        if channel != padded[2]:
            notes.append("管道名稱正規化")
        subject = _clean(padded[4], notes)
        body = _clean(padded[5], notes)
        if len(body) < 12:
            notes.append("內文過短")
        tickets.append(
            Ticket(
                ticket_id=padded[0],
                created_at=created,
                channel=channel,
                customer_name=padded[3].strip(),
                subject=subject,
                body=body,
                notes=tuple(dict.fromkeys(notes)),
            )
        )
    return tickets
