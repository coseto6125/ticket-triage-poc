"""分流分類器：對外呼叫 Gemini，並把每次的原始回應落地。

三件事在這裡：
- 節流。免費額度是每分鐘 15 次，超過就會被擋，所以本機自己先守住。
- 落地。每封工單的原始回應存成一個 JSON，包含模型代號與 prompt 版本，之後要追
  某一筆為什麼被這樣分，直接看那個檔案。
- 重跑。沒有金鑰時改讀既有的回應檔，任何人都能重現同一份分流結果。缺檔就抛例外，
  由上層轉真人，不會拿舊資料或空值假裝跑過。
"""

import json
import re
import time
from collections import deque
from typing import Any, Final

from triage import prompt as prompt_module
from triage import schema
from triage.bootloader import API_KEY, MODEL, RESPONSE_DIR, RPM

PROMPT_VERSION: Final = "v6"
_MAX_OUTPUT_TOKENS: Final = 800
CONFIDENCE_FLOOR: Final = 0.6
_MAX_ATTEMPTS: Final = 3
_RETRY_WAITS: Final = (1.0, 3.0)
_MAX_QUOTA_WAITS: Final = 2
_RETRY_IN_RE: Final = re.compile(r"retry in ([\d.]+)s")
_RETRY_DELAY_RE: Final = re.compile(r"retryDelay['\"]?:\s*['\"]?(\d+)s")


def _quota_delay(exc: Exception) -> float | None:
    """額度用完時，回傳 API 要求的等待秒數；不是額度問題就回 None。

    節流器只看得到自己這個行程送出去的請求，換一個行程重跑就會撞牆。API 已經在
    錯誤訊息裡講了要等多久，照它說的等，比自己猜一個退避秒數可靠。
    """
    text = str(exc)
    if "RESOURCE_EXHAUSTED" not in text and "429" not in text:
        return None
    if match := _RETRY_IN_RE.search(text):
        return float(match.group(1))
    if match := _RETRY_DELAY_RE.search(text):
        return float(match.group(1))
    return 60.0


class ClassifierUnavailable(RuntimeError):
    """分類服務無法給出可用的答案。上層必須降級轉真人。"""


class _RateLimiter:
    """滑動視窗節流：任意 60 秒內不超過 limit 次。"""

    __slots__ = ("_calls", "_limit")

    def __init__(self, limit: int) -> None:
        self._limit = limit
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

    def _record_path(self, ticket_id: str):
        return RESPONSE_DIR / f"{ticket_id}.json"

    def _call(self, text: str) -> tuple[dict[str, Any], dict[str, int]]:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(api_key=API_KEY)
        config = types.GenerateContentConfig(
            system_instruction=self._prompt,
            response_mime_type="application/json",
            response_schema=self._schema,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            temperature=0.0,
            # 實際踩過：模型陷入重複輸出，回了 65KB 的壞 JSON。正常回應約 130 token，
            # 給硬上限讓失控的那一次快速失敗，而不是拖著等它自己停。
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
        last: Exception | None = None
        attempt = quota_waits = 0
        while attempt < _MAX_ATTEMPTS:
            try:
                self._limiter.acquire()
                response = self._client.models.generate_content(model=MODEL, contents=text, config=config)
                usage = response.usage_metadata
                return json.loads(response.text), {
                    "input_tokens": usage.prompt_token_count,
                    "output_tokens": usage.candidates_token_count,
                }
            except json.JSONDecodeError as exc:
                # 回應不是合法 JSON 屬於永久性錯誤，重試沒有意義
                raise ClassifierUnavailable(f"回應不是合法 JSON：{exc}") from exc
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

    def classify(self, ticket_id: str, text: str) -> dict[str, Any]:
        """回傳這封工單的分類結果。text 必須是假名化之後的內容。"""
        path = self._record_path(ticket_id)
        if not self.live or (path.is_file() and not self.refresh):
            if not path.is_file():
                raise ClassifierUnavailable(f"{ticket_id} 沒有既有回應可重跑，且未設定金鑰")
            return json.loads(path.read_text(encoding="utf-8"))["answer"]

        answer, usage = self._call(text)
        path.write_text(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "model": MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "usage": usage,
                    "answer": answer,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return answer
