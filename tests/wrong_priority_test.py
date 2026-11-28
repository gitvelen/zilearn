#!/usr/bin/env python3
"""错题优先出队 + 答错状态回炉测试（2026-11-14 拍板三项的后端验收）

v56 改动验收：
A. 复习出队三级排序：wrong_count>0 DESC（错字插队）→ next_date（组内先到期）
   → wrong_count DESC（同日错得多优先）
B. weekly/review 答错补 status='learning'：mastered 字周测答错次日回复习供给
   （原缝隙：next_date=D+1 但 status='mastered' 被复习池过滤，只能靠抽检随机捞回）
C. 周测 scope 第二路 EXISTS 防御：quiz_log 有错答但 user_hanzi 为 skipped 的脏字
   不进周测（否则答错会把"已认识"标记推翻、激活回复习池）

载体：test 账号直调 scheduler + 直写 sqlite，无需起服务，线上库可直接跑。
     只动 test 账号——绝不触碰 mido（e2e.py 的 reset_user 清 mido，本文件不导入它）。

用法: python3 tests/wrong_priority_test.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import database
import scheduler

PASS_N = FAIL_N = 0


def check(name, cond, detail=""):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"  ✓ {name}")
    else:
        FAIL_N += 1
        print(f"  ✗ {name}  {detail}")


def test_uid(db):
    row = db.execute("SELECT id FROM users WHERE username='test'").fetchone()
    if not row:
        print("⛔ 缺 test 账号（seed 应建），拒绝运行")
        sys.exit(2)
    return row["id"]


def clean_test_data(uid):
    """清 test 账号学习状态（mido 零接触），保证测试幂等"""
    db = database.get_db()
    db.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL, plan_start=NULL,
                  plan_base_learned=0, confirm_upto=0, confirm_book=NULL, confirm_seq=NULL,
                  onboarded=0, day_offset=0 WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log", "daily_mail"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.commit()
    db.close()


def ready_chars(db, n):
    """1a 内容就绪字前 n 个（照 throttle_bank_tests 取字口径）"""
    return [r["id"] for r in db.execute(
        f"""SELECT h.id FROM hanzi h WHERE h.book='1a' AND {scheduler.CONTENT_READY}
            ORDER BY h.seq LIMIT {n}""")]


def wrong_order_tests(uid):
    """A. 出队排序：12 错字插队（前天 5 → 昨天 7，昨天组内 wc3 在 wc1 前），
    名额余 18 恰好给前天干净字，昨天干净字 23 个全被挤出。"""
    print("== A. 复习出队错字优先 ==")
    db = database.get_db()
    db.execute("""UPDATE users SET grade='1A', target_chars=10, plan_days=30,
                  plan_start=?, onboarded=1 WHERE id=?""", (date.today().isoformat(), uid))
    chars = ready_chars(db, 53)
    prev2 = (date.today() - timedelta(days=2)).isoformat()
    prev1 = (date.today() - timedelta(days=1)).isoformat()
    # 错字：前天 5（wc=1）、昨天 7（wc1×3 + wc3×4）
    for hid in chars[0:5]:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,wrong_count,next_date,first_date) "
                   "VALUES(?,?,'learning',1,1,?,?)", (uid, hid, prev2, prev2))
    wc1_late, wc3_late = chars[5:8], chars[8:12]
    for hid in wc1_late:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,wrong_count,next_date,first_date) "
                   "VALUES(?,?,'learning',1,1,?,?)", (uid, hid, prev1, prev1))
    for hid in wc3_late:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,wrong_count,next_date,first_date) "
                   "VALUES(?,?,'learning',1,3,?,?)", (uid, hid, prev1, prev1))
    # 干净字：前天 18（恰好占满余下名额）、昨天 23（应全被挤出）
    for hid in chars[12:30]:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
                   "VALUES(?,?,'learning',1,?,?)", (uid, hid, prev2, prev2))
    for hid in chars[30:53]:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
                   "VALUES(?,?,'learning',1,?,?)", (uid, hid, prev1, prev1))
    db.commit()
    db.close()

    t = scheduler.build_today(uid)
    rev = t["review"]
    ids = [r["id"] for r in rev]
    wrong_all = set(chars[0:12])
    check("供给封顶 30", len(rev) == scheduler.REVIEW_CAP, len(rev))
    check("前 12 全为错字（插队）", set(ids[:12]) == wrong_all, ids[:12])
    pos = {hid: i for i, hid in enumerate(ids)}
    check("错字组内先到期先服务（前天 5 在昨天 7 前）",
          max(pos[h] for h in chars[0:5]) < min(pos[h] for h in chars[5:12]),
          {h: pos.get(h) for h in chars[0:12]})
    check("同日错得多优先（昨天 wc3×4 全在 wc1×3 前）",
          max(pos[h] for h in wc3_late) < min(pos[h] for h in wc1_late),
          {h: pos.get(h) for h in wc1_late + wc3_late})
    check("余下名额给前天干净字", set(ids[12:]) == set(chars[12:30]), ids[12:])
    check("昨天干净字全被挤出（挤最年轻到期者）",
          not (set(chars[30:53]) & set(ids)), "有昨天干净字混入")


def weekly_wrong_status_tests(uid):
    """B. mastered 字周测答错 → 回 learning 次日回供给（原缝隙）。
    断言日必为周测日：D0~D26 累计 7 个活动日，(6+1)%7=0 —— scope 第三路的
    过滤条件（status='learning' AND next_date<=今天）即复习池出队条件，等价证明回池。"""
    print("== B. mastered 字周测答错回炉 ==")
    db = database.get_db()
    hid = ready_chars(db, 1)[0]
    db.close()
    real_today = scheduler.today_str
    day0 = date(2026, 1, 5)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    try:
        scheduler.today_str = lambda: D(0)
        db = database.get_db()
        db.execute("""UPDATE users SET grade='1A', target_chars=10, plan_days=30,
                      plan_start=?, onboarded=1 WHERE id=?""", (D(0), uid))
        db.commit()
        db.close()
        r = scheduler.submit(uid, hid, "learn", "read", True)
        check("D0 首学 stage0", r["stage"] == 0 and r["next_date"] == D(1), r)
        for n, phase in ((1, "review"), (4, "weekly"), (11, "review"), (25, "review")):
            scheduler.today_str = lambda n=n: D(n)
            r = scheduler.submit(uid, hid, phase, "listen", True)
        check("四连对 → mastered", r["status"] == "mastered" and r["next_date"] is None, r)
        scheduler.today_str = lambda: D(26)
        r = scheduler.submit(uid, hid, "weekly", "read", False)
        check("D26 周测答错 → learning 回炉 next=D27（缝隙修复）",
              r["status"] == "learning" and r["stage"] == 1 and r["next_date"] == D(27), r)
        r2 = scheduler.submit(uid, hid, "weekly", "read", False)
        check("同日重做幂等（replayed 不二次推进）",
              r2["status"] == "learning" and r2["next_date"] == D(27), r2)
        scheduler.today_str = lambda: D(27)
        t = scheduler.build_today(uid)
        scope_ids = [x["id"] for x in t.get("scope", [])]
        check("D27 周测日，该字回到供给（scope 含）",
              t["mode"] == "weekly" and hid in scope_ids,
              (t.get("mode"), len(scope_ids)))
    finally:
        scheduler.today_str = real_today  # noqa


def scope_defense_tests(uid):
    """C. 周测 scope 第二路 EXISTS：skipped 脏字（quiz_log 有错答）不进周测；
    对照 learning 错字照常进（防御不误伤）。"""
    print("== C. 周测 scope 脏数据防御 ==")
    db = database.get_db()
    chars = ready_chars(db, 10)
    dirty, normal, filler = chars[0], chars[1], chars[2:9]
    db.execute("""UPDATE users SET grade='1A', target_chars=10, plan_days=30,
                  plan_start=?, onboarded=1 WHERE id=?""", ("2026-01-05", uid))
    # 造 7 个活动日（D0..D6）→ D6 build_today 为周测日（di=6）
    for i, hid in enumerate(filler):
        db.execute("INSERT INTO quiz_log(user_id,hanzi_id,phase,quiz_type,correct,ts) "
                   "VALUES(?,?,'learn','read',1,?)", (uid, hid, (date(2026, 1, 5) + timedelta(days=i)).isoformat()))
    # 脏字：skipped 但 quiz_log 有错答（直插数据库才会出现的脏数据形态）
    db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
               "VALUES(?,?,'skipped',1,NULL,'2026-01-10')", (uid, dirty))
    db.execute("INSERT INTO quiz_log(user_id,hanzi_id,phase,quiz_type,correct,ts) "
               "VALUES(?,?,'weekly','read',0,'2026-01-10')", (uid, dirty))
    # 对照：正常 learning 错答字（本周答错，应照常被圈入）
    db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
               "VALUES(?,?,'learning',1,'2026-01-20','2026-01-10')", (uid, normal))
    db.execute("INSERT INTO quiz_log(user_id,hanzi_id,phase,quiz_type,correct,ts) "
               "VALUES(?,?,'weekly','read',0,'2026-01-10')", (uid, normal))
    db.commit()
    db.close()

    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: "2026-01-11"   # D6：第 7 个活动日 → 周测日
        t = scheduler.build_today(uid)
        scope_ids = [x["id"] for x in t.get("scope", [])]
        check("周测日判定（di=6）", t["mode"] == "weekly", t.get("mode"))
        check("skipped 脏字不进周测", dirty not in scope_ids, scope_ids)
        check("learning 错字照常进（防御不误伤）", normal in scope_ids, scope_ids)
    finally:
        scheduler.today_str = real_today  # noqa


if __name__ == "__main__":
    db = database.get_db()
    mido_n = db.execute("SELECT COUNT(*) FROM users WHERE username='mido'").fetchone()[0]
    db.close()
    if not mido_n:
        print("⛔ 缺 mido 账号，库异常，拒绝运行")
        sys.exit(2)
    uid = test_uid(database.get_db())
    clean_test_data(uid)
    try:
        wrong_order_tests(uid)
        clean_test_data(uid)
        weekly_wrong_status_tests(uid)
        clean_test_data(uid)
        scope_defense_tests(uid)
    finally:
        clean_test_data(uid)   # teardown：只清 test，mido 零接触
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
