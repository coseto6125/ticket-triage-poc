"""個資抽取與假名化。

個資邊界在這個檔案裡（見 docs/adr/0001）：抽取永遠在本機完成，抽出來的原值換成
同型別的假值之後才允許離開本機，回覆送出前再由程式換回來。假名對照表是
Pseudonymizer 的實例屬性，不寫檔、不進日誌、不進送出的請求。

分工是刻意的：

- **抽取層只說「哪一段是個資、語意上是什麼」**。它的介面是「呼叫一個本機模型服務」。
  PoC 沒有本機模型，MockTransport 用人工標註的結果回應同一個介面；正式環境把
  transport 換成真的 HTTP 呼叫，其餘程式不動。標註沒有涵蓋、或標註對不上內文的
  工單一律抛例外，由上層轉真人，不猜也不放行。
- **換成什麼假值完全由程式決定**，不交給模型。程式看原值的字形挑替換方式：全大寫
  拼音換成護照式拼音、大小寫英文換成英文名、中文帶稱謂只換姓保留稱謂、簡稱保留
  簡稱的形式、公司保留業別與組織型態。拿掉的是身分，留下的是分類需要的線索。

假值一律用「王小明／王小美／範例公司」這種一望即知是佔位的值。自己編一個看起來很真
的名字有機率撞到真實存在的人，而這組示例姓名讀起來自然，又不可能被誤認成某個真人。
"""

import json
import zlib
from pathlib import Path
from typing import Any, Final, NamedTuple, Protocol

from triage.bootloader import DATA_DIR


class ExtractionUnavailable(RuntimeError):
    """本機抽取層無法回答這封工單。上層必須轉真人。"""


class Entity(NamedTuple):
    """一筆個資：語意型別與在原文裡的字面值。"""

    type: str
    value: str


# 本機模型只需要辨認語意型別，寫法上的差別（中文、拼音、簡稱）由程式自己看字形判斷。
PII_TYPES: Final[tuple[str, ...]] = (
    "person_name",
    "company",
    "national_id",
    "phone",
    "bank_account",
    "tax_id",
    "email",
    "address",
)

# 本機抽取層的契約：模型以一次 function call 回報結果，帶語意型別與原文字面值兩個參數。
# 換成真的本機模型時，把這份 schema 當成它的工具定義，其餘程式不動。
EXTRACTION_TOOL: Final[dict[str, Any]] = {
    "name": "report_personal_data",
    "description": (
        "回報這封工單文字裡出現的個人資料。只回報，不要改寫、不要遮罩、不要正規化，"
        "換成什麼由呼叫端決定。找不到就回空陣列。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": list(PII_TYPES),
                            "description": "這筆資料在語意上是什麼。姓名一律填 person_name，不分中英文寫法。",
                        },
                        "value": {
                            "type": "string",
                            "description": "原文裡的字面值，一字不改，包含稱謂與空白。",
                        },
                    },
                    "required": ["type", "value"],
                },
            }
        },
        "required": ["entities"],
    },
}


def parse_tool_call(ticket_id: str, arguments: Any) -> list[Entity]:
    """驗證本機模型的 function call 參數。不合約定就抛例外，由上層轉真人。"""
    if not isinstance(arguments, dict) or not isinstance(items := arguments.get("entities"), list):
        raise ExtractionUnavailable(f"{ticket_id} 的抽取結果不是預期的形狀")
    entities: list[Entity] = []
    for item in items:
        if not isinstance(item, dict):
            raise ExtractionUnavailable(f"{ticket_id} 的抽取結果含有非物件項目")
        kind, value = item.get("type"), item.get("value")
        if kind not in PII_TYPES:
            raise ExtractionUnavailable(f"{ticket_id} 回報了未知的個資型別：{kind}")
        if not isinstance(value, str) or not value.strip():
            raise ExtractionUnavailable(f"{ticket_id} 的 {kind} 沒有給字面值")
        entities.append(Entity(kind, value))
    return entities


class Transport(Protocol):
    """本機個資抽取服務的傳輸層。"""

    def extract(self, ticket_id: str, fields: dict[str, str]) -> list[Entity]:
        """回傳這封工單裡的個資。無法回答時抛 ExtractionUnavailable。"""
        ...


class MockTransport:
    """以人工標註結果模擬本機模型的 function call。

    標註檔存的就是 report_personal_data 的參數，回傳前一樣走 parse_tool_call，
    所以驗證路徑和接上真模型時完全相同。
    """

    def __init__(self, path: Path | None = None) -> None:
        source = path if path is not None else DATA_DIR / "pii_annotations.json"
        raw: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
        self._by_ticket = {tid: items for tid, items in raw.items() if not tid.startswith("_")}

    def extract(self, ticket_id: str, fields: dict[str, str]) -> list[Entity]:
        if (found := self._by_ticket.get(ticket_id)) is None:
            raise ExtractionUnavailable(f"{ticket_id} 不在本機抽取層的涵蓋範圍")
        entities = parse_tool_call(ticket_id, {"entities": found})
        # 字面值必須真的出現在這封工單裡。對不上代表模型回報的不是這段文字裡的東西
        # （幻覺、或標註過期），那就不是「少報一筆」而是整份不可信：抛例外轉真人。
        blob = "\n".join(fields.values())
        if missing := [e.type for e in entities if e.value not in blob]:
            raise ExtractionUnavailable(f"{ticket_id} 回報的 {'、'.join(missing)} 在內文裡找不到")
        return entities


# ---- 依字形挑替換方式 ----

_ZH_SURNAMES: Final = ("王", "李", "陳", "林", "張", "黃", "吳", "劉")
_ZH_GIVEN: Final = ("小明", "小美")
_EN_SURNAMES: Final = ("Wang", "Lee", "Chen", "Lin")
_EN_GIVEN: Final = ("Hsiao-Ming", "Hsiao-Mei")
# 中文姓名後面常見的稱謂與職稱。保留它，因為「陳先生」和「陳經理」對分類是不同的線索。
_ZH_TITLES: Final = ("先生", "小姐", "女士", "太太", "經理", "總監", "主任", "老師", "醫師", "董事長")
# 簡稱的構詞前綴：小陳、老王、阿明。
_ZH_NICKNAME_PREFIXES: Final = ("小", "老", "阿")
# 公司名的尾巴，長的先比對。保留它，因為「巴士公司」和「科技公司」決定這封信該轉給誰。
_ORG_SUFFIXES: Final = (
    "科技股份有限公司",
    "股份有限公司",
    "有限公司",
    "巴士公司",
    "旅行社",
    "工程行",
    "企業社",
    "事務所",
    "生活誌",
    "公司",
)

_VALUE_POOLS: Final[dict[str, tuple[str, ...]]] = {
    "national_id": ("A234567891", "B123456780", "C198765432"),
    "phone": ("0900000001", "0900000002", "0900000003"),
    "bank_account": ("001-000000000001", "001-000000000002", "001-000000000003"),
    "tax_id": ("00000001", "00000002", "00000003"),
    "email": ("hsiaoming@example.com", "hsiaomei@example.com"),
    "address": ("臺北市中正區範例路 1 號", "臺北市中正區範例路 2 號"),
}


def _is_latin(value: str) -> bool:
    return any(c.isascii() and c.isalpha() for c in value) and not any(c > "⿿" for c in value)


def _person_candidates(value: str) -> list[str]:
    """依姓名的寫法挑候選假名。"""
    if _is_latin(value):
        if value.isupper():  # 護照拼音一律全大寫
            return [f"{s} {g}".upper().replace("-", " ") for s in _EN_SURNAMES for g in _EN_GIVEN]
        if " " in value:
            return [f"{s} {g}" for s in _EN_SURNAMES for g in _EN_GIVEN]
        return ["Ming", "Mei", "Hsiaoming", "Hsiaomei"]  # 只有一個字的英文名（暱稱）
    if title := next((t for t in _ZH_TITLES if value.endswith(t)), None):
        return [f"{s}{title}" for s in _ZH_SURNAMES]
    if len(value) == 2 and value[0] in _ZH_NICKNAME_PREFIXES:
        return [f"{value[0]}{s}" for s in _ZH_SURNAMES]
    return [f"{s}{g}" for s in _ZH_SURNAMES for g in _ZH_GIVEN]


def _company_candidates(value: str) -> list[str]:
    """保留業別與組織型態，只換掉可識別的字號。"""
    suffix = next((s for s in _ORG_SUFFIXES if value.endswith(s)), "")
    stem = "範例" if suffix else "範例企業"
    return [f"{stem}{suffix}", f"{stem}二{suffix}", f"{stem}三{suffix}"]


def candidates_for(entity: Entity) -> list[str]:
    """這筆個資可以換成哪些假值，第一順位是首選。

    姓名與識別號依原值的雜湊旋轉，讓同一個原值永遠先拿到同一個假值，不同的人也不會
    全部撞成同一個。公司不旋轉，直接從「範例…」開始，讀起來才不會冒出「範例三」。
    """
    match entity.type:
        case "person_name":
            pool = _person_candidates(entity.value)
        case "company":
            return _company_candidates(entity.value)
        case _:
            pool = list(_VALUE_POOLS.get(entity.type, ()))
    if not pool:
        return []
    start = zlib.crc32(entity.value.encode()) % len(pool)
    return pool[start:] + pool[:start]


def _shift(char: str) -> str:
    """把一個字元換成鄰近的同類字元，用來製造「只差一點點」的假值。"""
    if char.isdigit():
        return str((int(char) + 1) % 10)
    if "A" <= char <= "Z":
        return chr((ord(char) - 65 + 1) % 26 + 65)
    if "a" <= char <= "z":
        return chr((ord(char) - 97 + 1) % 26 + 97)
    return char


class Pseudonymizer:
    """把一封工單的個資換成假值，並在回覆送出前換回來。

    一個實例只服務一封工單。對照表存在實例裡，處理完就隨實例消失。
    """

    __slots__ = ("_backward", "_forward", "_map", "_reals")

    def __init__(self, entities: list[Entity]) -> None:
        self._map: list[tuple[str, str, str]] = []  # (型別, 原值, 假值)
        self._reals = {e.value for e in entities}
        # 短的先配，長的才有機會沿用短的假值把「只差一個字」的關係保留下來
        for entity in sorted(entities, key=lambda e: len(e.value)):
            if any(real == entity.value for _, real, _ in self._map):
                continue
            self._map.append((entity.type, entity.value, self._assign(entity)))
        # 換值時一律長的先換，否則短的原值會先把長的原值切壞
        self._forward = sorted(((r, f) for _, r, f in self._map), key=lambda p: -len(p[0]))
        self._backward = sorted(((f, r) for _, r, f in self._map), key=lambda p: -len(p[0]))

    def _conflicts(self, candidate: str) -> bool:
        """這個假值會不會和這封工單裡的其他值互相吃到。

        apply 與 restore 都是一連串 str.replace，只要假值和任何一個原值或另一個假值
        有包含關係，替換就會接龍：A 的假值先被換成 B 的假值，還原時 B 的原值就會被寫進
        A 的位置。所以候選假值必須和「所有原值」以及「已配出去的假值」都互不包含。
        """
        others = [*self._reals, *(fake for _, _, fake in self._map)]
        return any(candidate in other or other in candidate for other in others)

    def _assign(self, entity: Entity) -> str:
        if (near := self._near(entity)) is not None:
            return near
        for candidate in candidates_for(entity):
            if not self._conflicts(candidate):
                return candidate
        # 候選都撞上時退回一個不可能和任何內容相撞的佔位符。
        # 文字的自然度會掉，但替換與還原的正確性優先。
        return f"[{entity.type}#{len(self._map)}]"

    def _near(self, entity: Entity) -> str | None:
        """原值之間只差幾個字時（打錯字的情境），假值也必須只差幾個字。

        TK-1028 的兩個護照英文名、TK-1010 的錯誤與正確統編都屬於這一類：換成兩個
        毫不相干的假值，客戶真正的問題就在假名化的時候被消滅了。保留的是「共用長前綴、
        尾端不同」這個關係，不是原本那個編輯動作的種類。
        """
        for kind, real, fake in self._map:
            if kind != entity.type:
                continue
            shared = 0
            for a, b in zip(real, entity.value):
                if a != b:
                    break
                shared += 1
            if shared < max(3, len(real) * 0.6) or len(fake) < shared:
                continue
            candidate = fake[:shared] + "".join(_shift(c) for c in entity.value[shared:])
            if not self._conflicts(candidate):
                return candidate
        return None

    @property
    def entity_count(self) -> int:
        return len(self._map)

    def types(self) -> tuple[str, ...]:
        """這封工單被換掉的個資型別，供只需要說明「有幾筆什麼」的地方使用。"""
        return tuple(kind for kind, _, _ in self._map)

    def apply(self, text: str) -> str:
        """把原值換成假值。送出本機的文字必須先經過這裡。"""
        for real, fake in self._forward:
            text = text.replace(real, fake)
        return text

    def restore(self, text: str) -> str:
        """把假值換回原值。只在回覆寫出前呼叫。"""
        for fake, real in self._backward:
            text = text.replace(fake, real)
        return text

    def covers(self, value: str) -> bool:
        """這個字串是不是我們自己放進去的假值。

        模型看到的是假名化之後的文字，它回報的「殘留個資」多半就是我們放的假名。
        真正的殘留是抽取層漏掉的東西，所以要先把自己的假值扣掉。

        只認「回報的片段包含我們的假值」這個方向。反過來認（片段是假值的一部分）會把
        真的殘留一起吃掉：假值裡面本來就含有一段數字，而那段數字可能正好是一組沒被抽到
        的統編。寧可多轉幾封人工，也不要把漏抓的個資判成自己人。
        """
        return any(fake in value for fake, _ in self._backward)

    def leaked(self, text: str) -> list[str]:
        """回報假名化之後仍然看得到的原值的型別。不回傳原值本身，避免它被寫進日誌或報表。"""
        return [kind for kind, real, _ in self._map if real in text]
