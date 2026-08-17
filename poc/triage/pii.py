"""個資抽取與假名化。

個資邊界在這個檔案裡（見 docs/adr/0001）：抽取永遠在本機完成，抽出來的原值換成
同型別的假值之後才允許離開本機，回覆送出前再由程式換回來。假名對照表是
Pseudonymizer 的實例屬性，不寫檔、不進日誌、不進送出的請求。

抽取層的介面是「呼叫一個本機模型服務」。PoC 沒有本機模型，MockTransport 用人工
標註的結果回應同一個介面；正式環境把 transport 換成真的 HTTP 呼叫，其餘程式不動。
標註沒有涵蓋的工單一律抛例外，由上層轉真人，不猜也不放行。
"""

import json
import zlib
from pathlib import Path
from typing import Final, NamedTuple, Protocol

from triage.bootloader import DATA_DIR


class ExtractionUnavailable(RuntimeError):
    """本機抽取層無法回答這封工單。上層必須轉真人。"""


class Entity(NamedTuple):
    """一筆個資：型別與在原文裡的字面值。"""

    type: str
    value: str


class Transport(Protocol):
    """本機個資抽取服務的傳輸層。"""

    def extract(self, ticket_id: str, fields: dict[str, str]) -> list[Entity]:
        """回傳這封工單裡的個資。無法回答時抛 ExtractionUnavailable。"""
        ...


class MockTransport:
    """以人工標註結果模擬本機模型的回應。"""

    def __init__(self, path: Path | None = None) -> None:
        source = path if path is not None else DATA_DIR / "pii_annotations.json"
        raw: dict[str, list[dict[str, str]]] = json.loads(source.read_text(encoding="utf-8"))
        self._by_ticket = {
            tid: [Entity(e["type"], e["value"]) for e in items]
            for tid, items in raw.items()
            if not tid.startswith("_")
        }

    def extract(self, ticket_id: str, fields: dict[str, str]) -> list[Entity]:
        if (found := self._by_ticket.get(ticket_id)) is None:
            raise ExtractionUnavailable(f"{ticket_id} 不在本機抽取層的涵蓋範圍")
        # 只回報真的出現在這封工單文字裡的項目，標註與資料不一致時寧可少報也不誤報
        blob = "\n".join(fields.values())
        return [e for e in found if e.value in blob]


# 假值池。同型別的假值必須和原值一樣自然，模型才不會因為看到怪字串而改變判斷。
_POOLS: Final[dict[str, tuple[str, ...]]] = {
    "person_name": (
        "沈柏睿",
        "藍映竹",
        "涂宥安",
        "簡雅琳",
        "闕思賢",
        "衛可庭",
        "戚宗翰",
        "郝欣妍",
        "宮子墨",
        "麥筱涵",
        "步鎮宇",
        "祁沛慈",
        "尚文彥",
        "冉靖翔",
        "扈晴薇",
        "詹又寧",
    ),
    "person_name_en": ("Emily Kao", "Daniel Hsu", "Grace Yeh"),
    "passport_name": ("CHIU YA WEN", "CHIU YA WENN", "LOU CHIH HAO", "PIEN SHU CHEN"),
    "company": ("澄川物流有限公司", "岱耘生活誌", "翊笙科技股份有限公司", "曜嵐工程行"),
    "national_id": ("B234567891", "C123456780", "D198765432"),
    "phone": ("0987654321", "0933112244", "0955667788"),
    "bank_account": ("013-987654321098", "812-556677889900", "700-112233445566"),
    "tax_id": ("87654321", "24681357", "13570246"),
    "email": ("ping.chen@example.com", "wei.lin@example.com"),
    "address": ("桃園市中壢區民安路 12 號", "臺中市西屯區文華路 88 號"),
}


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

    __slots__ = ("_backward", "_forward", "_map")

    def __init__(self, entities: list[Entity]) -> None:
        self._map: list[tuple[str, str, str]] = []  # (型別, 原值, 假值)
        # 短的先配，長的才有機會沿用短的假值把「只差一個字」的關係保留下來
        for entity in sorted(entities, key=lambda e: len(e.value)):
            if any(real == entity.value for _, real, _ in self._map):
                continue
            self._map.append((entity.type, entity.value, self._assign(entity)))
        # 換值時一律長的先換，否則短的原值會先把長的原值切壞
        self._forward = sorted(((r, f) for _, r, f in self._map), key=lambda p: -len(p[0]))
        self._backward = sorted(((f, r) for _, r, f in self._map), key=lambda p: -len(p[0]))

    def _assign(self, entity: Entity) -> str:
        return self._near(entity) or self._pick(entity)

    def _near(self, entity: Entity) -> str | None:
        """原值之間只差幾個字時（打錯字的情境），假值也必須只差幾個字。

        TK-1028 的兩個護照英文名、TK-1010 的錯誤與正確統編都屬於這一類：換成兩個
        毫不相干的假值，客戶真正的問題就在假名化的時候被消滅了。
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
            if all(candidate != other for _, _, other in self._map):
                return candidate
        return None

    def _pick(self, entity: Entity) -> str:
        """依原值的雜湊挑一個同型別假值，同一個原值永遠得到同一個假值。"""
        if not (pool := _POOLS.get(entity.type)):
            return f"[{entity.type}]"
        start = zlib.crc32(entity.value.encode()) % len(pool)
        for offset in range(len(pool)):
            candidate = pool[(start + offset) % len(pool)]
            if all(candidate != other for _, _, other in self._map):
                return candidate
        return f"[{entity.type}]"

    @property
    def entity_count(self) -> int:
        return len(self._map)

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
        """
        return any(value in fake or fake in value for fake, _ in self._backward)

    def leaked(self, text: str) -> list[str]:
        """回報假名化之後仍然看得到的原值。正常情況應為空。"""
        return [real for _, real, _ in self._map if real in text]
