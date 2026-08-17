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


def _priority(answer: Answer | None, route: str) -> str:
    if answer is not None and (answer.is_complaint or "force_majeure" in answer.scenarios):
        return "P1"
    return "P2" if route == HUMAN else "P3"


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
        # 不受理的工單不回覆也不進人工佇列，直接關單
        return Decision(AUTO, NO_REPLY, "P3", (), f"不受理，自動關單。{answer.reason}")

    route = HUMAN if triggers else AUTO
    if _COMPLAINT in triggers or answer.primary.name == "service_issue":
        # 帶客訴或問題出在已交付的服務上，不用範本碰內容，只回收件確認
        reply_mode = ACK_ONLY
    else:
        reply_mode = TEMPLATE

    if triggers:
        reason = f"轉真人（{'、'.join(triggers)}）。{answer.reason}"
    else:
        reason = f"可自動處理。{answer.reason}"
    return Decision(route, reply_mode, _priority(answer, route), tuple(triggers), reason)


def degraded(detail: str) -> Decision:
    """分類失敗時的降級決策：不猜、不放行，一律轉真人。"""
    return Decision(HUMAN, ACK_ONLY, "P2", (DEGRADED,), f"轉真人（{DEGRADED}）。{detail}")
