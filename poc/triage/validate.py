"""回應驗證。任何一項對不上就拋例外，由上層降級轉真人，不修補、不猜。

除了型別檢查以外，這裡做 docs/adr/0003 講的對帳：模型自己填的受理判定與承辦歸屬，
必須和它選中的每一個情境在目錄裡登記的值一致。矛盾代表模型這次沒讀懂，而不是
一個可以直接修好的欄位。
"""

from typing import Any, NamedTuple

from triage import catalog


class InvalidAnswer(ValueError):
    """回應不符合約定。"""


class Answer(NamedTuple):
    """一封工單通過驗證的分類結果。"""

    reason: str
    disposition: str
    assignment: str
    scenarios: tuple[str, ...]
    is_complaint: bool
    is_irreversible: bool
    money_mentioned: str
    residual_pii: tuple[str, ...]
    confidence: float

    @property
    def primary(self) -> catalog.Scenario:
        """最主要的情境，模型被要求放在第一位。"""
        return catalog.BY_NAME[self.scenarios[0]]


def _text(raw: dict[str, Any], key: str, *, required: bool = True) -> str:
    if not isinstance(value := raw.get(key), str):
        raise InvalidAnswer(f"{key} 不是字串")
    if required and not value.strip():
        raise InvalidAnswer(f"{key} 是空的")
    return value.strip()


def _flag(raw: dict[str, Any], key: str) -> bool:
    if not isinstance(value := raw.get(key), bool):
        raise InvalidAnswer(f"{key} 不是布林值")
    return value


def _strings(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    if not isinstance(value := raw.get(key), list) or any(not isinstance(v, str) for v in value):
        raise InvalidAnswer(f"{key} 不是字串陣列")
    return tuple(v.strip() for v in value if v.strip())


def parse(raw: Any) -> Answer:
    """驗證並轉成 Answer，不合約定就拋 InvalidAnswer。"""
    if not isinstance(raw, dict):
        raise InvalidAnswer("回應不是物件")

    disposition = _text(raw, "disposition")
    if disposition not in (catalog.ACCEPT, catalog.REJECT):
        raise InvalidAnswer(f"未知的受理判定：{disposition}")

    assignment = _text(raw, "assignment")
    if assignment not in {catalog.SUPPORT, catalog.REFERRAL, catalog.NOT_APPLICABLE}:
        raise InvalidAnswer(f"未知的承辦歸屬：{assignment}")

    scenarios = _strings(raw, "scenarios")
    if not scenarios:
        raise InvalidAnswer("沒有給任何服務情境")
    if unknown := [s for s in scenarios if s not in catalog.BY_NAME]:
        raise InvalidAnswer(f"未知的服務情境：{'、'.join(unknown)}")
    scenarios = tuple(dict.fromkeys(scenarios))

    # bool 是 int 的子類，先擋掉才不會讓 True 通過信心值檢查
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise InvalidAnswer("confidence 不是數值")
    if not 0.0 <= float(confidence) <= 1.0:
        raise InvalidAnswer(f"confidence 超出範圍：{confidence}")

    is_complaint = _flag(raw, "is_complaint")

    # 與情境目錄對帳
    for name in scenarios:
        scenario = catalog.BY_NAME[name]
        if scenario.disposition != disposition:
            raise InvalidAnswer(f"{name} 的受理判定應為 {scenario.disposition}，模型填了 {disposition}")
        if scenario.assignment != assignment:
            raise InvalidAnswer(f"{name} 的承辦歸屬應為 {scenario.assignment}，模型填了 {assignment}")
    if disposition == catalog.REJECT and is_complaint:
        raise InvalidAnswer("不受理的工單不應同時被判為客訴")

    return Answer(
        reason=_text(raw, "reason"),
        disposition=disposition,
        assignment=assignment,
        scenarios=scenarios,
        is_complaint=is_complaint,
        is_irreversible=_flag(raw, "is_irreversible"),
        money_mentioned=_text(raw, "money_mentioned", required=False),
        residual_pii=_strings(raw, "residual_pii"),
        confidence=float(confidence),
    )
