"""設定載入。必須是所有進入點的第一個 import。

只做一件事：把 poc/.env 讀進 os.environ，之後任何模組都可以直接讀環境變數，
不需要把設定當參數一路傳下去。沒有 .env 也不報錯，缺金鑰時管線會走重跑既有回應
的路徑（見 gemini.py）。
"""

import os
from pathlib import Path
from typing import Final

POC_ROOT: Final = Path(__file__).resolve().parent.parent
DATA_DIR: Final = POC_ROOT / "data"
RESPONSE_DIR: Final = DATA_DIR / "responses"


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env(POC_ROOT / ".env")

MODEL: Final = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
RPM: Final = int(os.environ.get("GEMINI_RPM", "15"))
API_KEY: Final = os.environ.get("GEMINI_API_KEY", "")
