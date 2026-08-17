"""把 docs/answers/ 的四份分稿組成根目錄的 answers.md。

分稿是唯一的來源，answers.md 是產物。這樣四個章節各自修改時不會和最終交付檔漂移，
也不用擔心有人只改了其中一邊。

用法：uv run --project poc python docs/build_answers.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ("part1.md", "part2.md", "part3.md", "part4.md")

HEADER = """# T 旅遊客服工單分流：書面回答

第三部分的程式與說明在 `poc/`，執行方式見 `poc/README.md`。
語彙定義見 `CONTEXT.md`，幾個不明顯的決策與當初為什麼不選另一條路見 `docs/adr/`。

本文由 `docs/answers/` 的分稿組出，請勿直接編輯本檔。
"""


def build() -> str:
    sections = [(ROOT / "docs" / "answers" / name).read_text(encoding="utf-8").strip() for name in PARTS]
    return HEADER + "\n---\n\n" + "\n\n---\n\n".join(sections) + "\n"


if __name__ == "__main__":
    target = ROOT / "answers.md"
    target.write_text(build(), encoding="utf-8")
    print(f"寫出 {target.relative_to(ROOT)}，{len(build().splitlines())} 行")
