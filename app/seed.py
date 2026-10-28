"""seed：导入字表 CSV + 内容 JSON + 创建 mido 用户（幂等）

用法:
  python seed.py hanzi    # 字表 + 内容
  python seed.py users    # mido（密码仅首次生成时打印）
"""
import csv
import json
import secrets
import sys
from pathlib import Path

import database
from auth import hash_password

BASE = Path(__file__).parent
CSV = BASE.parent / "data/parsed/hanzi_books.csv"
CONTENT = BASE.parent / "data/parsed/zi_content.json"


def py_to_tone3(py: str) -> str:
    VOWELS = {"ā": ("a", 1), "ē": ("e", 1), "ī": ("i", 1), "ō": ("o", 1), "ū": ("u", 1), "ǖ": ("v", 1),
              "á": ("a", 2), "é": ("e", 2), "í": ("i", 2), "ó": ("o", 2), "ú": ("u", 2), "ǘ": ("v", 2),
              "ǎ": ("a", 3), "ě": ("e", 3), "ǐ": ("i", 3), "ǒ": ("o", 3), "ǔ": ("u", 3), "ǚ": ("v", 3),
              "à": ("a", 4), "è": ("e", 4), "ì": ("i", 4), "ò": ("o", 4), "ù": ("u", 4), "ǜ": ("v", 4),
              "ü": ("v", 0)}
    tone, out = 5, []
    for c in py:
        if c in VOWELS:
            out.append(VOWELS[c][0])
            if VOWELS[c][1]:
                tone = VOWELS[c][1]
        else:
            out.append(c)
    return "".join(out) + str(tone)


def seed_hanzi():
    db = database.get_db()
    try:
        rows = list(csv.DictReader(CSV.open()))
        content = json.loads(CONTENT.read_text()) if CONTENT.exists() else {}
        for t in ("zi_content", "user_hanzi", "quiz_log", "confirm_pick"):
            db.execute(f"DELETE FROM {t}")
        db.execute("DELETE FROM hanzi")
        n = 0
        for r in rows:
            db.execute("INSERT OR IGNORE INTO hanzi(char, pinyin, tone3, alt_readings, unit, seq, book)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (r["char"], r["pinyin"], r["tone3"], r["alt_readings"],
                        r["unit"], int(r["seq"]), r["book"]))
            n += 1
        # 内容按字键 upsert（book=1a 范围内）。sentence 恒取 sentences[0]（单句引用兼容）
        n_content = 0
        for ch, c in content.items():
            row = db.execute("SELECT id, book FROM hanzi WHERE char=?", (ch,)).fetchone()
            if not row:
                continue
            sents = c.get("sentences") or ([c["sentence"]] if c.get("sentence") else [])
            db.execute("""INSERT INTO zi_content(hanzi_id, words, sentence, sentences, etym_type, etym, emoji)
                          VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(hanzi_id) DO UPDATE SET
                            words=excluded.words, sentence=excluded.sentence,
                            sentences=excluded.sentences,
                            etym_type=excluded.etym_type, etym=excluded.etym, emoji=excluded.emoji""",
                       (row["id"], json.dumps(c["words"], ensure_ascii=False),
                        sents[0] if sents else None, json.dumps(sents, ensure_ascii=False),
                        c["etym_type"], c["etym"], c.get("emoji", "")))
            n_content += 1
        db.commit()
        books = db.execute("SELECT book, COUNT(*) n FROM hanzi GROUP BY book").fetchall()
        print(f"hanzi {n} 字 / 内容 {n_content} 字 / 册分布 {[(b['book'], b['n']) for b in books]}")
    finally:
        db.close()


def seed_users():
    """mido（随机密码，仅首次打印）+ test（固定密码 test123）。幂等。"""
    db = database.get_db()
    try:
        row = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()
        if row:
            print("mido 已存在")
        else:
            pw = secrets.token_urlsafe(4)
            db.execute("INSERT INTO users(username, password_hash, display_name) VALUES(?,?, 'Mido')",
                       ("mido", hash_password(pw)))
            db.commit()
            print(f"mido 创建成功，密码: {pw}")
        row = db.execute("SELECT id FROM users WHERE username='test'").fetchone()
        if row:
            print("test 已存在")
        else:
            db.execute("INSERT INTO users(username, password_hash, display_name) VALUES(?,?, 'Test')",
                       ("test", hash_password("test123")))
            db.commit()
            print("test 创建成功，密码: test123")
    finally:
        db.close()


if __name__ == "__main__":
    database.init_db()
    what = sys.argv[1] if len(sys.argv) > 1 else "hanzi"
    if what == "hanzi":
        seed_hanzi()
    elif what == "users":
        seed_users()
