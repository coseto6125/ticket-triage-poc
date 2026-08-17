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
            "maxLength": 200,
            "description": "用一句繁體中文說明寄件者是誰、這封信要什麼。只寫一句，這句會被記錄為分流依據。",
        },
        "disposition": {
            "type": "string",
            "enum": [catalog.ACCEPT, catalog.REJECT],
            "description": (
                "第一層：這封信要不要進入客服流程。"
                "reject 只用在不是真實客戶來信的內容：推銷與博弈廣告、詐騙、系統測試資料。"
                "來自真實的人、只是不該由客服回覆的信（業務詢價、合作提案、供應商往來、"
                "應徵、媒體採訪）仍然填 accept，改用承辦歸屬 referral 把它轉出去。"
            ),
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
            "maxItems": 4,
            "items": {"type": "string", "enum": list(catalog.SCENARIO_NAMES)},
            "description": (
                "第三層：這封信屬於哪些服務情境，可複選。一封信同時問兩件不同的事就填兩個。"
                "第一層填 reject 時，只填該封信屬於的 reject 情境。"
            ),
        },
        "is_complaint": {
            "type": "boolean",
            "description": (
                "這封信是否針對本公司已經發生的作為或服務表達不滿、指責，或要求道歉與補償。"
                "判準是有沒有責難本公司，不是語氣強不強、事情急不急："
                "寫「請今天務必回我」、催促進度、抱怨天氣或第三方，都填 false；"
                "「等了 40 分鐘沒人接，這是什麼服務態度」則填 true。"
                "這一欄與服務情境無關，可以掛在任何情境上。第一層填 reject 時填 false。"
            ),
        },
        "is_irreversible": {
            "type": "boolean",
            "description": (
                "客戶要求的動作是否為執行後無法自動撤回的：退款、開立或更正文件、變更帳號，"
                "以及在一筆已經成立的訂單上做任何加購、變更、指定或取消"
                "（加行李、加人、指定艙位或房間、改期、改名都算）。"
                "如果他只是在問通則、問還沒買的產品，填 false。"
            ),
        },
        "money_mentioned": {
            "type": "string",
            "maxLength": 100,
            "description": "信中提到的金額原文，多筆以頓號分隔；沒提到就填空字串。這欄只做記錄，不參與判斷。",
        },
        "residual_pii": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 100},
            "description": (
                "這封信的文字裡仍然看得到的個人資料原文片段（姓名、電話、證號、帳號、地址）。沒有就給空陣列。"
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "對以上分類判斷的信心，介於 0 與 1 之間的小數。"
                "評的是「這封信有沒有被分到對的情境」，不是「這個問題好不好回答」："
                "一封訴求很清楚但需要查很多資料才能回覆的信，分類信心仍然應該很高。"
                "只有在真的看不出客戶要什麼、或兩個情境都說得通時才給低分。"
            ),
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(FIELD_ORDER),
        "propertyOrdering": list(FIELD_ORDER),
    }
