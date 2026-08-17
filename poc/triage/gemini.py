"""分流分類器：對外呼叫 Gemini，並把每次的原始回應存檔。

三件事在這裡：
- 節流。免費額度是每分鐘 15 次，超過就會被擋，所以本機自己先守住。
- 存檔。每封工單的原始回應存成一個 JSON，包含模型代號與 prompt 版本，之後要追
  某一筆為什麼被這樣分，直接看那個檔案。
- 重跑。沒有金鑰時改讀既有的回應檔，任何人都能重現同一份分流結果。缺檔就拋例外，
  由上層轉真人，不會拿舊資料或空值假裝跑過。
"""

import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Final

from google import genai
from google.genai import errors, types

from triage import prompt as prompt_module
from triage import schema, validate
from triage.bootloader import API_KEY, MODEL, RESPONSE_DIR, RPM

PROMPT_VERSION: Final = "v6"
_MAX_OUTPUT_TOKENS: Final = 800
CONFIDENCE_FLOOR: Final = 0.6
_MAX_ATTEMPTS: Final = 3
_RETRY_WAITS: Final = (1.0, 3.0)
_MAX_QUOTA_WAITS: Final = 2
_QUOTA_STATUS: Final = 429
_DEFAULT_QUOTA_WAIT: Final = 60.0
_RETRY_INFO_TYPE: Final = "RetryInfo"
# 抓 3 次：涵蓋「修一次還是壞、再修一次」的情況，又不會讓一個持續壞掉的回應無限吃掉
# 每分鐘 15 次的額度。每一次修復都佔一個名額，所以它不是免費的。
_REPAIR_MAX_ATTEMPTS: Final = 3
# 完全的 greedy decoding 會讓模型有機會鎖進重複輸出的迴圈（實際發生過，一次回了 65KB）。
# 給一點 temperature 就足以跳出來，對分類結果的影響可以忽略。
_TEMPERATURE: Final = 0.1
_REPAIR_PROMPT: Final = (
    "下面這段文字原本應該是一個符合 schema 的 JSON 物件，但它壞掉了：可能被截斷、"
    "重複輸出、或夾雜了多餘的文字。請只根據這段文字能看出來的內容，重新輸出一個合法的 JSON。"
    "看不出來的欄位就給該欄位的保守值：信心給 0，布林給 false，字串給空字串，陣列給空陣列。"
    "不要重新判斷、不要補充你自己的看法，只輸出 JSON。"
)


def _digest(text: str) -> str:
    """送進模型的文字的 digest，用來確認重跑讀到的回應真的對應這段內容。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _local_repair(raw: str) -> dict[str, Any] | None:
    """純程式的修復：切掉結尾多出來的東西，並且驗證切出來的東西真的合約定。

    模型壞掉的典型樣子是「一個完整的物件後面接了垃圾」或「重複輸出到被截斷」。從最右邊
    的 `}` 往左走，每個候選都試著解析成完整物件並過一次 schema 驗證；驗證才是驗收標準，
    只是 json.loads 成功不算數，因為半截的前綴也可能是合法 JSON 但欄位不全。

    修不出合約定的結果就回 None，由呼叫端決定要不要花 LLM 的次數。
    """
    end = raw.rfind("}")
    while end != -1:
        candidate = raw[: end + 1]
        try:
            parsed = json.loads(candidate)
            validate.parse(parsed)
            return parsed
        except (json.JSONDecodeError, validate.InvalidAnswer):
            end = raw.rfind("}", 0, end)
    return None


def _quota_delay(exc: Exception) -> float | None:
    """額度用完時，回傳 API 指定的等待秒數；不是額度問題就回 None。

    判斷依據是回應本身的狀態碼與結構化欄位，不是錯誤訊息的字面內容：訊息的措辭會隨
    版本改，而且 request id 或 token 數裡本來就可能出現 429 這三個數字。

    節流器只看得到自己這個行程送出去的請求，換一個行程重跑就會撞牆。回應的 RetryInfo
    已經寫了要等多久，照它說的等，比自己猜一個退避秒數可靠。
    """
    if not isinstance(exc, errors.APIError) or exc.code != _QUOTA_STATUS:
        return None
    body = exc.details if isinstance(exc.details, dict) else {}
    body = body.get("error", body)
    for item in body.get("details", []) if isinstance(body, dict) else []:
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith(_RETRY_INFO_TYPE):
            continue
        try:
            return float(str(item.get("retryDelay", "")).removesuffix("s"))
        except ValueError:
            break
    return _DEFAULT_QUOTA_WAIT


class ClassifierUnavailable(RuntimeError):
    """分類服務無法給出可用的答案。上層必須降級轉真人。"""


class _RateLimiter:
    """滑動視窗節流：任意 60 秒內不超過 limit 次。"""

    __slots__ = ("_calls", "_limit")

    def __init__(self, limit: int) -> None:
        # 下限 1：設定填 0 會讓 acquire() 永遠等不到名額
        self._limit = max(1, limit)
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            time.sleep(60.0 - (now - self._calls[0]) + 0.1)
            return self.acquire()
        self._calls.append(now)

    def reset(self) -> None:
        """等過額度之後視窗已經清空，重新開始計。"""
        self._calls.clear()


class Classifier:
    """一封工單一次呼叫，回傳符合 schema 的判斷結果。"""

    def __init__(self, *, live: bool | None = None, refresh: bool = False) -> None:
        """
        Args:
            live: 是否實際呼叫 API。預設看有沒有金鑰。
            refresh: 已有回應檔時是否仍然重打一次。
        """
        self.live = bool(API_KEY) if live is None else live
        self.refresh = refresh
        self._limiter = _RateLimiter(RPM)
        self._schema = schema.response_schema()
        self._prompt = prompt_module.build()
        self._client = None
        RESPONSE_DIR.mkdir(parents=True, exist_ok=True)

    def _record_path(self, ticket_id: str) -> Path:
        return RESPONSE_DIR / f"{ticket_id}.json"

    def _configs(self):
        classify = types.GenerateContentConfig(
            system_instruction=self._prompt,
            response_mime_type="application/json",
            response_schema=self._schema,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            temperature=_TEMPERATURE,
            # 實際踩過：模型陷入重複輸出，回了 65KB 的壞 JSON。正常回應約 130 token，
            # 給硬上限讓失控的那一次快速失敗，而不是拖著等它自己停。
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
        # 修復用的設定刻意和分類用的不一樣：換掉 system instruction、把 thinking_level 壓到最低，讓它只做
        # 「把這段壞掉的文字重新輸出成合法 JSON」這一件事，不重走一次會壞掉的判斷。
        repair = types.GenerateContentConfig(
            system_instruction=_REPAIR_PROMPT,
            response_mime_type="application/json",
            response_schema=self._schema,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            temperature=_TEMPERATURE,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
        return classify, repair

    def _client_or_new(self):
        if self._client is None:
            self._client = genai.Client(api_key=API_KEY)
        return self._client

    def _repair(self, broken: str, config: Any) -> dict[str, Any] | None:
        """修復壞掉的回應。便宜的先做：程式修不出來，才花 LLM 的次數。

        兩條路徑共用同一個驗收標準（_local_repair 裡的 schema 驗證），所以模型修完
        還是不合約定時不會被當成修好了。全部失敗就回 None，由呼叫端降級轉真人，
        絕不把原始的壞字串當成答案往下傳。
        """
        if (fixed := _local_repair(broken)) is not None:
            return fixed
        current = broken
        for _ in range(_REPAIR_MAX_ATTEMPTS):
            try:
                self._limiter.acquire()
                current = (
                    self._client_or_new()
                    .models.generate_content(model=MODEL, contents=current, config=config)
                    .text
                )
            except errors.APIError:
                break
            if (fixed := _local_repair(current)) is not None:
                return fixed
        return None

    def _call(self, text: str) -> tuple[dict[str, Any], dict[str, int]]:
        classify, repair = self._configs()
        last: Exception | None = None
        attempt = quota_waits = 0
        while attempt < _MAX_ATTEMPTS:
            try:
                self._limiter.acquire()
                response = self._client_or_new().models.generate_content(
                    model=MODEL, contents=text, config=classify
                )
                usage = response.usage_metadata
                counts = {
                    "input_tokens": usage.prompt_token_count,
                    "output_tokens": usage.candidates_token_count,
                }
                try:
                    return json.loads(response.text), counts
                except json.JSONDecodeError as exc:
                    # 重跑同一個判斷多半會再壞一次，所以不重試，改交給修復設定重出格式
                    if (repaired := self._repair(response.text, repair)) is not None:
                        return repaired, counts
                    raise ClassifierUnavailable(f"回應不是合法 JSON，修復後仍無法解析：{exc}") from exc
            except ClassifierUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 傳輸層錯誤一律視為暫時性
                last = exc
                if (delay := _quota_delay(exc)) is not None and quota_waits < _MAX_QUOTA_WAITS:
                    quota_waits += 1
                    time.sleep(delay + 1.0)
                    self._limiter.reset()
                    continue  # 等額度不算一次重試
                if attempt < len(_RETRY_WAITS):
                    time.sleep(_RETRY_WAITS[attempt])
                attempt += 1
        raise ClassifierUnavailable(f"重試 {_MAX_ATTEMPTS} 次後仍失敗：{last}")

    def _replay(self, ticket_id: str, text: str) -> dict[str, Any]:
        """讀回既有回應。任何一項對不上就拋例外，由上層轉真人。

        重跑的意義是「重現同一份結果」，所以模型、prompt 版本、以及送進去的文字都必須
        和當初錄下來的一致。少了這些檢查，改過 prompt 或改過工單內容之後重跑，會拿舊答案
        當新答案用，而且完全看不出來。
        """
        path = self._record_path(ticket_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            answer = record["answer"]
            recorded = (record["model"], record["prompt_version"], record["input_digest"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ClassifierUnavailable(f"{ticket_id} 沒有可用的既有回應：{exc}") from exc
        if recorded != (MODEL, PROMPT_VERSION, _digest(text)):
            raise ClassifierUnavailable(
                f"{ticket_id} 的既有回應與現在的模型、prompt 版本或工單內容不一致，需要重新呼叫"
            )
        return answer

    def classify(self, ticket_id: str, text: str) -> dict[str, Any]:
        """回傳這封工單的分類結果。text 必須是假名化之後的內容。"""
        path = self._record_path(ticket_id)
        if not self.live or (path.is_file() and not self.refresh):
            return self._replay(ticket_id, text)

        answer, usage = self._call(text)
        payload = json.dumps(
            {
                "ticket_id": ticket_id,
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
                "input_digest": _digest(text),
                "usage": usage,
                "answer": answer,
            },
            ensure_ascii=False,
            indent=2,
        )
        # 先寫暫存再換名。中途中斷只會留下暫存檔，不會留下一個半截的回應檔騙過下次重跑。
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        return answer
