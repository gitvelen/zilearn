#!/usr/bin/env python3
"""应用人工审查 verdict 到 questions 表

输入：verdict 列表（本文件 VERDICTS 常量，Claude 逐题审查结论）
格式：(id, 'P'|'F'|'W', note)  P=通过 W=通过但弱题(待variant替换) F=打回(待重新生成)
用法：python3 review_apply.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "app/data/zilearn.db"

# mean 批次（281 题全审，2026-10-27）
VERDICTS = [
    # F 打回
    (452, "F", "零语义线索"),
    (889, "F", "＿的多解：它的/狗的皆通"),
    (921, "F", "＿是多解：也是/他是皆通"),
    (929, "F", "零语义线索"),
    (957, "F", "大伞儿童语感成立，真歧义"),
    # W 弱题通过（后续 variant=2 换词替换）
    (70, "W", "十足超纲"), (94, "W", "山川书面"), (200, "W", "小王边缘"),
    (408, "W", "＿个弱线索"), (428, "W", "好了非典型组词"), (448, "W", "从来超纲"),
    (496, "W", "好的非典型组词"), (567, "W", "看着非典型组词"), (575, "W", "就是弱"),
    (599, "W", "反面偏书面"), (611, "W", "内外书面"), (659, "W", "指尖书面"),
    (666, "W", "众人书面"), (746, "W", "选项含题干字"), (933, "W", "又是弱"),
    (941, "W", "才能超纲"), (964, "W", "比高待核内容词"),
]


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    n = {"P": 0, "W": 0, "F": 0}
    for qid, v, note in VERDICTS:
        verified = 1 if v in ("P", "W") else -1
        tag = "P" if v == "P" else v
        db.execute("UPDATE questions SET verified=?, verdict=? WHERE id=?",
                   (verified, f"{tag}:{note}", qid))
        n[v] += 1
    db.commit()
    # 其余未判 mean 题全部 P（已逐题过目无问题）
    cur = db.execute("""UPDATE questions SET verified=1, verdict='P'
                        WHERE quiz_type='mean' AND verified=0""")
    n["P"] += cur.rowcount
    db.commit()
    print(f"mean 批次落库：F={n['F']} W={n['W']} P={n['P']}")
    left = db.execute("SELECT COUNT(*) n FROM questions WHERE verified=0").fetchone()["n"]
    print(f"待审剩余：{left}")
    db.close()


if __name__ == "__main__":
    main()
