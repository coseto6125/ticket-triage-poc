"""服務情境目錄。

情境是資料不是程式碼：新增一個情境只需要在 SCENARIOS 加一列，送給模型的
判斷說明、JSON schema 的 enum、下游工具綁定、回覆範本全部由這份定義生成，
沒有第二個地方需要同步。

三層判定裡，受理判定與承辦歸屬登記在情境上，用途是核對模型自己填的那兩欄
（見 docs/adr/0003），不是拿來直接填值。
"""

from dataclasses import dataclass
from typing import Final

# 受理判定
ACCEPT: Final = "accept"
REJECT: Final = "reject"

# 承辦歸屬
SUPPORT: Final = "support"
REFERRAL: Final = "referral"
NOT_APPLICABLE: Final = "not_applicable"


@dataclass(frozen=True, slots=True)
class ScenarioGroup:
    """一組共用同一個後端查詢工具的服務情境。"""

    name: str
    label: str
    tools: tuple[str, ...]
    note: str


@dataclass(frozen=True, slots=True)
class Scenario:
    """一個服務情境。

    Attributes:
        name: 程式用的穩定識別字，同時是 JSON schema enum 的值。
        label: 中文名，出現在分流結果與執行摘要。
        description: 給模型看的觸發說明，決定這封信算不算這個情境。
        group: 所屬情境群組，決定下游查什麼系統。
        disposition: 這個情境對應的受理判定，用於與模型輸出對帳。
        assignment: 這個情境對應的承辦歸屬，用於與模型輸出對帳。
        requires_human: 這個情境本身不可自動結案。
        irreversible: 這個情境的訴求涉及不可逆動作。
        reply: 模擬的回覆範本，不含任何承諾。
    """

    name: str
    label: str
    description: str
    group: str
    disposition: str
    assignment: str
    requires_human: bool
    irreversible: bool
    reply: str


GROUPS: Final[tuple[ScenarioGroup, ...]] = (
    ScenarioGroup(
        name="order_lookup",
        label="訂單查詢",
        tools=("order_api",),
        note="需要訂單系統 REST API，權限尚未核准，QPS 限制低",
    ),
    ScenarioGroup(
        name="knowledge_lookup",
        label="知識查詢",
        tools=("kb_search",),
        note="需要知識庫檢索，文件散落且版本混亂",
    ),
    ScenarioGroup(
        name="account_ops",
        label="會員系統",
        tools=("account_api",),
        note="需要會員系統，涉及帳號異動",
    ),
    ScenarioGroup(
        name="no_lookup",
        label="不查系統",
        tools=(),
        note="不需查任何系統，依 SOP 由人工處理",
    ),
    ScenarioGroup(
        name="referral",
        label="轉其他單位",
        tools=("ticket_transfer",),
        note="非客服案件，於工單系統轉單",
    ),
    ScenarioGroup(
        name="drop",
        label="不受理",
        tools=(),
        note="不進入客服流程，自動關單",
    ),
)


SCENARIOS: Final[tuple[Scenario, ...]] = (
    # ---- order_lookup：需要查訂單系統 ----
    Scenario(
        name="order_status",
        label="訂單狀態確認",
        description="客戶想確認訂單是否成立、款項是否入帳、電子票券或確認信是否已寄出。訴求是查詢現況，不要求變更任何資料。",
        group="order_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="關於您的訂單狀態，我們會以訂單編號查詢系統紀錄後回覆確認結果。",
    ),
    Scenario(
        name="booking_change",
        label="訂單變更",
        description="客戶要求變更已成立的訂單內容：改期、改梯次、更正姓名或證件拼音、變更接送地點、加購品項、變更人數。",
        group="order_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=True,
        reply="您要求的訂單變更需要確認可行性與費用差額，我們會核對訂單後與您聯繫。",
    ),
    Scenario(
        name="cancel_refund",
        label="取消與退費",
        description="客戶要求取消行程、申請退費，或詢問已提出的退費何時入帳。包含因個人因素、健康因素或人數變動而產生的退費請求。",
        group="order_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=True,
        reply="取消與退費需依訂單狀態與契約級距試算，我們會核對後提供試算結果。",
    ),
    Scenario(
        name="payment_billing",
        label="付款與發票",
        description="客戶反映扣款異常、重複扣款、金額與網頁不符，或要求開立、更正發票與收據（統編、抬頭、二聯改三聯、英文收據）。",
        group="order_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=True,
        reply="關於款項或單據的處理，我們會調閱交易與開立紀錄後回覆。",
    ),
    # ---- knowledge_lookup：需要查知識庫 ----
    Scenario(
        name="product_info",
        label="產品內容",
        description=(
            "客戶詢問產品本身對所有人都一樣的內容：行程包含哪些項目、售價是否含稅含小費、"
            "房型與艙等條件、成團人數。問的如果是他個人的特殊需求能不能被滿足，"
            "那屬於 special_request，不是這裡。"
        ),
        group="knowledge_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="您詢問的產品內容與費用包含範圍，我們會依該商品的最新版說明回覆。",
    ),
    Scenario(
        name="policy_terms",
        label="條款與規定",
        description="客戶詢問公司或法規層級的規則本身：定型化契約的退費級距、取消手續費比例、不可抗力的處置原則、點數折抵規則。訴求是知道規則，不是處理個案。",
        group="knowledge_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="您詢問的規則適用條件，我們會依現行契約條款說明。",
    ),
    Scenario(
        name="travel_docs",
        label="證件與簽證",
        description="客戶詢問旅行證件相關規定：護照效期要求、簽證種類與辦理方式、落地簽或電子簽的最新規定、入境條件。",
        group="knowledge_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="關於證件與簽證規定，我們會依目的地現行規定說明應備條件。",
    ),
    Scenario(
        name="pre_trip_info",
        label="行前資訊",
        description="客戶詢問出發前會收到什麼、何時收到：行程表、集合時間地點、出發通知、登機資訊、票券兌換方式。",
        group="knowledge_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="行前資料的寄送時程與內容，我們會依該團的作業時程回覆。",
    ),
    Scenario(
        name="special_request",
        label="特殊需求",
        description=(
            "客戶問他個人的特殊需求能不能被滿足：素食與飲食限制、無障礙與輪椅、"
            "嬰幼兒與孩童的收費方式、指定房型或艙位、行李額度。訴求是問可行性與條件。"
            "如果他要的是在已經成立的訂單上加購或變更，那屬於 booking_change。"
        ),
        group="knowledge_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=False,
        irreversible=False,
        reply="您提出的特殊需求，我們會確認該行程可提供的安排方式後回覆。",
    ),
    # ---- account_ops：需要查會員系統 ----
    Scenario(
        name="member_account",
        label="會員帳號",
        description="客戶的會員帳號本身出問題：無法登入、帳號被鎖、收不到驗證碼、點數餘額與折抵結果不符。",
        group="account_ops",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=True,
        reply="關於會員帳號的狀態，我們會核對帳號紀錄後協助處理。",
    ),
    # ---- no_lookup：不查系統，走 SOP ----
    Scenario(
        name="service_issue",
        label="服務品質與求償",
        description="問題出在已經交付的服務上：領隊或導遊的表現、實際行程與網頁不符、住宿環境與描述不符、公司先前給錯資訊。客戶要求道歉、補償、賠償或重新安排。",
        group="no_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=False,
        reply="您反映的狀況我們已收到，將由專人調閱該團紀錄後與您聯繫。",
    ),
    Scenario(
        name="force_majeure",
        label="不可抗力",
        description="因颱風、豪雪、天災或其他不可抗力，客戶詢問行程是否照常、會不會延期，以及取消的處置方式。這類工單在事件發生時會整批同時湧入。",
        group="no_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=False,
        reply="關於天候與不可抗力因素的行程異動，我們會依最新官方資訊與統一處置原則回覆。",
    ),
    Scenario(
        name="ask_clarification",
        label="資訊不足",
        description="信件內容過短或過於含糊，無法判斷客戶要處理哪一筆訂單或要什麼結果。這是保底情境，只在真的判斷不出訴求時使用。",
        group="no_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=False,
        reply="您的來信我們已收到，需要再向您確認幾項資訊才能協助處理。",
    ),
    Scenario(
        name="out_of_scope",
        label="範圍外",
        description="是真實客戶的來信，但訴求不屬於任何已定義的服務情境。這是保底情境，不要用它來裝不想分類的信。",
        group="no_lookup",
        disposition=ACCEPT,
        assignment=SUPPORT,
        requires_human=True,
        irreversible=False,
        reply="您的來信我們已收到，將轉由適當窗口確認後回覆。",
    ),
    # ---- referral：轉其他單位 ----
    Scenario(
        name="non_support",
        label="非客服來信",
        description="寄件者不是在尋求客戶服務：企業包團或團體業務詢價、網紅與媒體的合作或業配提案、供應商的報價與對帳往來、應徵求職、媒體採訪邀約。",
        group="referral",
        disposition=ACCEPT,
        assignment=REFERRAL,
        requires_human=True,
        irreversible=False,
        reply="您的來信我們已收到，將轉交對應窗口與您聯繫。",
    ),
    # ---- drop：不受理 ----
    Scenario(
        name="spam_fraud",
        label="廣告與詐騙",
        description="與本公司業務無關的推銷、博弈或投資廣告、釣魚與詐騙訊息。判準是寄件者的意圖，不是信裡出現了什麼字：一封抱怨「你們跟博弈網站一樣」的客訴信不屬於這裡。",
        group="drop",
        disposition=REJECT,
        assignment=NOT_APPLICABLE,
        requires_human=False,
        irreversible=False,
        reply="",
    ),
    Scenario(
        name="test_noise",
        label="測試與無效內容",
        description="系統測試留下的資料、空白或無意義內容，不是真實客戶的來信。",
        group="drop",
        disposition=REJECT,
        assignment=NOT_APPLICABLE,
        requires_human=False,
        irreversible=False,
        reply="",
    ),
)

BY_NAME: Final[dict[str, Scenario]] = {s.name: s for s in SCENARIOS}
GROUP_BY_NAME: Final[dict[str, ScenarioGroup]] = {g.name: g for g in GROUPS}
SCENARIO_NAMES: Final[tuple[str, ...]] = tuple(s.name for s in SCENARIOS)


def tools_for(names: list[str]) -> tuple[str, ...]:
    """回傳這組情境在下游需要呼叫的工具，依情境目錄的順序去重。"""
    tools = [t for n in names for t in GROUP_BY_NAME[BY_NAME[n].group].tools]
    return tuple(dict.fromkeys(tools))
