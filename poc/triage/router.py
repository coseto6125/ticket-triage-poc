"""分流規則。

規則刻意只有六條，而且全部是程式判斷，不是模型判斷（見 poc/README.md 的取捨）。
任何一條成立就轉真人。金額不在這六條裡面，理由見 docs/adr/0002。

回覆型態與分流是兩個正交欄位（見 docs/adr/0004）：一封工單可以既送出範本回覆、
又同時進人工佇列。
"""

from typing import Final, NamedTuple

from triage import catalog
from triage.gemini import CONFIDENCE_FLOOR
from triage.validate import Answer

# 分流
AUTO: Final = "auto"
HUMAN: Final = "human"

# 回覆型態
NO_REPLY: Final = "none"
TEMPLATE: Final = "template"
ACK_ONLY: Final = "ack_only"

# 優先級
P_URGENT: Final = "P1"
P_HUMAN: Final = "P2"
P_ROUTINE: Final = "P3"

_SCENARIO_HUMAN: Final = "情境需人工"
_COMPLAINT: Final = "客訴性質"
_REFERRAL: Final = "非客服來信"
_IRREVERSIBLE: Final = "不可逆動作"
_RESIDUAL_PII: Final = "殘留個資"
_LOW_CONFIDENCE: Final = "信心不足"
DEGRADED: Final = "分類失敗"


class Decision(NamedTuple):
    """一封工單的分流結果。"""

    route: str
    reply_mode: str
    priority: str
    triggers: tuple[str, ...]
    reason: str


def _priority(answer: Answer, route: str) -> str:
    if answer.is_complaint or any(catalog.BY_NAME[n].urgent for n in answer.scenarios):
        return P_URGENT
    return P_HUMAN if route == HUMAN else P_ROUTINE


def decide(answer: Answer, residual_pii: tuple[str, ...]) -> Decision:
    """依六條規則決定分流與回覆型態。"""
    triggers: list[str] = []

    if any(catalog.BY_NAME[n].requires_human for n in answer.scenarios):
        triggers.append(_SCENARIO_HUMAN)
    if answer.is_complaint:
        triggers.append(_COMPLAINT)
    if answer.assignment == catalog.REFERRAL:
        triggers.append(_REFERRAL)
    if answer.is_irreversible or any(catalog.BY_NAME[n].irreversible for n in answer.scenarios):
        triggers.append(_IRREVERSIBLE)
    if residual_pii:
        triggers.append(_RESIDUAL_PII)
    if answer.confidence < CONFIDENCE_FLOOR:
        triggers.append(_LOW_CONFIDENCE)

    if answer.disposition == catalog.REJECT:
        # 自動關單是這條管線唯一會讓工單消失的動作，所以只在完全沒有疑慮時才做。
        # 只要有任何一條規則成立（典型是信心不足或殘留個資），就改成不回覆但進人工佇列，
        # 讓人確認它真的是廣告，而不是一封寫得很糟的真實客訴。
        if triggers:
            reason = f"疑似不受理但有疑慮（{'、'.join(triggers)}），不回覆，交人工確認。{answer.reason}"
            return Decision(HUMAN, NO_REPLY, P_HUMAN, tuple(triggers), reason)
        return Decision(AUTO, NO_REPLY, P_ROUTINE, (), f"不受理，自動關單。{answer.reason}")

    route = HUMAN if triggers else AUTO
    # 帶客訴、或主情境本身就不該用範本碰內容的，只回收件確認
    reply_mode = ACK_ONLY if _COMPLAINT in triggers or answer.primary.ack_only else TEMPLATE

    if triggers:
        reason = f"轉真人（{'、'.join(triggers)}）。{answer.reason}"
    else:
        reason = f"可自動處理。{answer.reason}"
    return Decision(route, reply_mode, _priority(answer, route), tuple(triggers), reason)


def degraded(detail: str) -> Decision:
    """分類失敗時的降級決策：不猜、不放行，一律轉真人。"""
    return Decision(HUMAN, ACK_ONLY, P_HUMAN, (DEGRADED,), f"轉真人（{DEGRADED}）。{detail}")
