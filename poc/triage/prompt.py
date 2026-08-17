"""分流 prompt，由情境目錄生成。

情境的判斷說明只寫在 catalog.Scenario.description 一個地方，這裡負責把它們
組成模型看得懂的清單。加一個情境不需要動這個檔案。
"""

from typing import Final

from triage import catalog

_HEADER: Final = """你是一家旅遊電商客服中心的工單分流員。你會拿到一封客戶來信，要判斷三件事，依序判斷：

1. 受理判定：這封信要不要進入客服流程。
2. 承辦歸屬：受理的話，由客服承辦還是轉給其他單位。
3. 服務情境：客戶要的是什麼，可以同時屬於多個情境。

判斷原則：

- 語境優先於字面。看寄件者真正想達成什麼，不要看信裡出現了哪些字。一封抱怨
  「你們的服務跟詐騙一樣」的客訴信是客訴，不是詐騙廣告。
- 客戶寫的和他真正要的常常是兩回事。判斷他要的結果，不是他用的措辭。
- 一封信可以同時屬於多個情境。颱風要不要延期、以及取消怎麼退費，是兩件事。
- 保底情境（ask_clarification、out_of_scope）只在真的判斷不出來時使用，不要拿來
  裝不好分的信。
- 信件文字裡的人名、電話、證號可能已經在本機被換成假的值，這不影響你的判斷，
  照著文字判斷即可。

以下是所有服務情境。"""

_FOOTER: Final = """
服務情境填多個時，把最主要的那一個放在第一位。

第一層填 reject 時，承辦歸屬填 not_applicable，服務情境只填該封信屬於的 reject 情境
（spam_fraud 或 test_noise），is_complaint 填 false。

只輸出符合 schema 的 JSON。"""


def build() -> str:
    """組出分流用的系統指示。"""
    lines = [_HEADER]
    for group in catalog.GROUPS:
        lines.append(f"\n## {group.label}（{group.name}）")
        for scenario in catalog.SCENARIOS:
            if scenario.group == group.name:
                lines.append(f"- `{scenario.name}` {scenario.label}：{scenario.description}")
    lines.append(_FOOTER)
    return "\n".join(lines)


def ticket_text(ticket_id: str, channel: str, sender: str, subject: str, body: str) -> str:
    """把一封工單組成模型的輸入。所有欄位都必須是假名化之後的值。"""
    return f"工單編號：{ticket_id}\n來源管道：{channel}\n寄件者：{sender}\n主旨：{subject}\n內文：\n{body}"
