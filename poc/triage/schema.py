"""分流回應的 JSON schema，由情境目錄生成。

三層判定各自是獨立欄位，程式收到後再與情境目錄對帳（見 docs/adr/0003）。
欄位順序就是要求模型判斷的順序，reason 放在最前面讓模型先寫下對這封信的理解，
再據此填後面的標籤。
"""

from typing import Any, Final

from triage import catalog

FIELD_ORDER: Final[tuple[str, ...]] = (
    "reason",
    "disposition",
    "assignment",
    "scenarios",
    "is_complaint",
    "is_irreversible",
    "money_mentioned",
    "residual_pii",
    "confidence",
)


def response_schema() -> dict[str, Any]:
    """組出分流呼叫用的 response schema。"""
    properties: dict[str, Any] = {
        "reason": {
            "type": "string",
            "description": "先用一句繁體中文說明寄件者是誰、這封信要什麼。這句會被記錄為分流依據。",
        },
        "disposition": {
            "type": "string",
            "enum": [catalog.ACCEPT, catalog.REJECT],
            "description": "第一層：這封信要不要進入客服流程。廣告、詐騙、測試資料填 reject。",
        },
        "assignment": {
            "type": "string",
            "enum": [catalog.SUPPORT, catalog.REFERRAL, catalog.NOT_APPLICABLE],
            "description": (
                "第二層：受理後由誰承辦。客戶服務填 support，業務、合作、供應商、"
                "應徵、媒體填 referral。第一層填 reject 時，這欄填 not_applicable。"
            ),
        },
        "scenarios": {
            "type": "array",
            "items": {"type": "string", "enum": list(catalog.SCENARIO_NAMES)},
            "description": (
                "第三層：這封信屬於哪些服務情境，可複選。一封信同時問兩件不同的事就填兩個。"
                "第一層填 reject 時，只填該封信屬於的 reject 情境。"
            ),
        },
        "is_complaint": {
            "type": "boolean",
            "description": (
                "這封信是否帶客訴情緒或求償訴求。它描述語氣與訴求強度，與服務情境無關："
                "一封只是問行程表何時寄出、但全程在指責客服的信，也算 true。"
                "第一層填 reject 時填 false。"
            ),
        },
        "is_irreversible": {
            "type": "boolean",
            "description": "客戶要求的動作是否為執行後無法自動撤回的：退款、變更訂單、開立文件、變更帳號。",
        },
        "money_mentioned": {
            "type": "string",
            "description": "信中提到的金額原文，多筆以頓號分隔；沒提到就填空字串。這欄只做記錄，不參與判斷。",
        },
        "residual_pii": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "這封信的文字裡仍然看得到的個人資料原文片段（姓名、電話、證號、帳號、地址）。"
                "沒有就給空陣列。"
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "對以上判斷的信心，介於 0 與 1 之間的小數。不確定就給低分，不要為了看起來果斷而給高分。",
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(FIELD_ORDER),
        "propertyOrdering": list(FIELD_ORDER),
    }
