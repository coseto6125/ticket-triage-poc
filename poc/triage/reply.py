"""草稿產生。

草稿一律用情境自帶的範本組出來，不再呼叫模型：這個 PoC 要證明的是分流，不是
生成品質。範本內容是模擬的，不是任何一家旅行社的真實規定。

寫出去之前有兩道關卡：
1. 禁用詞掃描。掃到金額或承諾句式就降級成只回收件確認。這條放在程式而不是
   prompt 裡，因為「不亂承諾」的失敗成本是客訴（見 docs/adr/0004）。
2. 還原。草稿是用假名化後的值組出來的，送出前才換回原值。
"""

import re
from typing import Final

from triage import catalog, router
from triage.pii import Pseudonymizer
from triage.validate import Answer

# 金額，以及任何把未確認的事講成已確認的句式
_BANNED: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\d[\d,]{2,}\s*元"),
    re.compile(r"NT\$"),
    re.compile(r"已為您(辦理|處理|完成|退款|取消)"),
    re.compile(r"將於\s*\d"),
    re.compile(r"(同意|核准|批准)(退款|退費|取消|變更)"),
    re.compile(r"(保證|一定|必定|絕對)(可以|能|會)"),
)

_GREETING: Final = "{name} 您好，感謝您的來信。"
_HANDOFF: Final = "以上為初步說明，尚未確認的部分我們不會先做承諾，將由專人接手後與您聯繫。"
_ACK: Final = "{name} 您好，您的來信我們已經收到，將由專人確認後盡快與您聯繫。"


def banned_hits(text: str) -> tuple[str, ...]:
    """回傳文字裡命中的禁用詞，正常情況應為空。"""
    return tuple(m.group(0) for pattern in _BANNED if (m := pattern.search(text)))


def render(
    answer: Answer | None,
    decision: router.Decision,
    sender: str,
    pseudonymizer: Pseudonymizer,
) -> tuple[str, str, tuple[str, ...]]:
    """組出草稿。

    Args:
        answer: 通過驗證的分類結果，降級時為 None。
        decision: 分流結果。
        sender: 假名化之後的寄件者名稱。
        pseudonymizer: 這封工單的假名對照表，用於送出前還原。

    Returns:
        (最終回覆型態, 已還原的草稿文字, 命中的禁用詞)
    """
    if decision.reply_mode == router.NO_REPLY:
        return router.NO_REPLY, "", ()

    if decision.reply_mode == router.ACK_ONLY or answer is None:
        return router.ACK_ONLY, pseudonymizer.restore(_ACK.format(name=sender)), ()

    parts = [_GREETING.format(name=sender)]
    parts.extend(catalog.BY_NAME[n].reply for n in answer.scenarios if catalog.BY_NAME[n].reply)
    if decision.route == router.HUMAN:
        parts.append(_HANDOFF)
    draft = pseudonymizer.restore("\n".join(parts))

    if hits := banned_hits(draft):
        return router.ACK_ONLY, pseudonymizer.restore(_ACK.format(name=sender)), hits
    return router.TEMPLATE, draft, ()
