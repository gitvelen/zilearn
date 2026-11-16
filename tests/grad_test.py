#!/usr/bin/env python3
"""本期毕业测（v60）后端验收：挂旗跨越事件 / scope / submit 毕业测语义 / wf 生成校验 / 多期链

v60 改动验收：
① 挂旗=跨越事件：learn 逐字 +1，base+target 恰好命中那次置位（"="而非"<="——达成是
   持续状态，<= 会在毕业后每次提交重复挂旗 → 无限毕业测循环）
② create_plan（续订）保留 grad_pending/grad_anchor（mido 11-10 实况：达成当日续订）
③ 毕业测 scope=锚点起本期全部新字（旧字/skipped 排除；不含旧错字/到期字）
④ wf 找朋友判错版生成：干扰字与目标字两序不成词（判错版防双解）、tiles 互异、
   无二字词伙伴退化 mean
⑤ submit 毕业测语义「对只算一次，错永远回炉」：
   - slot0 对 → stage+1（与普通周测同权）
   - slot1/2 对（同日 replayed 或跨日）→ 只记流水不推进（防 stage+3 失衡/D+1 被覆盖）
   - slot1/2 错（replayed 也不跳过）→ 回炉（v56 语义，F1 缺陷修复的核心断言）
⑥ 多期链：A 挂旗→续订 B→B 跨越 anchor 保持 A（合并考 A+B）→ 结算清零→C 期重挂
⑦ 跨日续做：毕业测开头未结算 → 次日 build_today 仍 weekly+grad

载体：test 账号直调 scheduler + monkey-patch today_str，线上库可直接跑（同
     wrong_priority_test.py 惯例）。只动 test 账号——绝不触碰 mido。

用法: python3 tests/grad_test.py
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
    """清 test 账号学习状态（mido 零接触），保证测试幂等；v60 加 grad 两列复位"""
    db = database.get_db()
    db.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL, plan_start=NULL,
                  plan_base_learned=0, confirm_upto=0, onboarded=0, day_offset=0,
                  grad_pending=0, grad_anchor=NULL WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log", "daily_mail"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.commit()
    db.close()


def ready_chars(db, n, book="1a"):
    return [r["id"] for r in db.execute(
        f"""SELECT h.id FROM hanzi h WHERE h.book='{book}' AND {scheduler.CONTENT_READY}
            ORDER BY h.seq LIMIT {n}""")]


def grad_flags(db, uid):
    r = db.execute("SELECT grad_pending, grad_anchor FROM users WHERE id=?", (uid,)).fetchone()
    return r["grad_pending"], r["grad_anchor"]


def flag_tests(uid):
    """①挂旗跨越事件 + ②create_plan 保留。target=3：第 2 字不挂、第 3 字挂且 anchor=plan_start；
    续订（create_plan）后 flag/anchor 原值不动。"""
    print("== ① 挂旗跨越事件 / ② 续订保留 ==")
    day0 = date(2026, 1, 5)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    db = database.get_db()
    chars = ready_chars(db, 3)
    db.execute("""UPDATE users SET grade='1A', target_chars=3, plan_days=7,
                  plan_start=?, plan_base_learned=0, onboarded=1 WHERE id=?""", (D(0), uid))
    db.commit()
    db.close()
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: D(0)
        scheduler.submit(uid, chars[0], "learn", "card", True)
        scheduler.submit(uid, chars[1], "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("第 2 字不挂旗（= 未命中）", p == 0 and a is None, (p, a))
        scheduler.submit(uid, chars[2], "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("第 3 字恰好命中 → 挂旗 anchor=plan_start", p == 1 and a == D(0), (p, a))
        # 越过阈值后的后续 learn 不重复挂/不覆盖 anchor
        scheduler.today_str = lambda: D(1)
        scheduler.submit(uid, ready_chars(database.get_db(), 4)[3], "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("越过阈值后不重复挂（> 不命中）", p == 1 and a == D(0), (p, a))
        # ② 续订：create_plan 只动自己的列
        scheduler.create_plan(uid, 5, 7)
        p, a = grad_flags(database.get_db(), uid)
        check("续订后 flag/anchor 原值保留", p == 1 and a == D(0), (p, a))
    finally:
        scheduler.today_str = real_today  # noqa


def scope_tests(uid):
    """③毕业测 scope：锚点起本期新字恰 N 个；锚点前旧字/skipped 排除；mode=weekly+grad。"""
    print("== ③ 毕业测 scope ==")
    day0 = date(2026, 1, 5)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    db = database.get_db()
    chars = ready_chars(db, 9)
    db.close()
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: D(0)
        db = database.get_db()
        db.execute("""UPDATE users SET grade='1A', target_chars=6, plan_days=7,
                      plan_start=?, plan_base_learned=0, onboarded=1, grad_pending=1, grad_anchor=?
                      WHERE id=?""",
                   (D(0), D(0), uid))
        # 锚点前旧字（上期遗留，直插模拟）
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
                   "VALUES(?,?,'learning',1,'2026-01-20',?)", (uid, chars[7], "2026-01-04"))
        # 本期 skipped 字（first_date>=anchor 但 status=skipped）
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
                   "VALUES(?,?,'skipped',1,NULL,?)", (uid, chars[8], D(2)))
        db.commit()
        db.close()
        # 本期 6 字：D0..D5 每日 learn 一字（同时构成 6 个活动日 → D6 为第 7 活动日=周测日）
        for i in range(6):
            scheduler.today_str = lambda i=i: D(i)
            scheduler.submit(uid, chars[i], "learn", "card", True)
        scheduler.today_str = lambda: D(6)
        t = scheduler.build_today(uid)
        ids = [x["id"] for x in t.get("scope", [])]
        check("D6 周测槽位判定", t["mode"] == "weekly", t.get("mode"))
        check("毕业测标志 grad=True", t.get("grad") is True, t.get("grad"))
        check("scope 恰含本期 6 字（旧字/skipped 排除）",
              set(ids) == set(chars[0:6]), (sorted(ids), sorted(chars[0:6])))
        # ⑦ 跨日续做：D6 开头一题未结算 → D7 仍毕业测日
        scheduler.submit(uid, chars[0], "weekly", "py", True, grad_slot=0)
        scheduler.today_str = lambda: D(7)
        t2 = scheduler.build_today(uid)
        ids2 = [x["id"] for x in t2.get("scope", [])]
        check("跨日续做：D7 仍 weekly+grad、scope 不变",
              t2["mode"] == "weekly" and t2.get("grad") is True and set(ids2) == set(chars[0:6]),
              (t2.get("mode"), t2.get("grad")))
    finally:
        scheduler.today_str = real_today  # noqa


def submit_semantics_tests(uid):
    """⑤ submit 毕业测语义：对只算一次（slot0），错永远回炉（replayed 不跳过）；
    普通周测（不传 slot）行为不回归。"""
    print("== ⑤ submit 毕业测语义 ==")
    day0 = date(2026, 2, 2)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    db = database.get_db()
    chars = ready_chars(db, 5)
    db.close()
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: D(0)
        db = database.get_db()
        db.execute("""UPDATE users SET grade='1A', target_chars=99, plan_days=30,
                      plan_start=?, onboarded=1 WHERE id=?""", (D(0), uid))
        db.commit()
        db.close()
        for hid in chars:
            scheduler.submit(uid, hid, "learn", "card", True)   # stage0 / next D1

        # A. 同日三 slot 全对：仅 slot0 推进 stage+1；slot1/2 只记流水
        scheduler.today_str = lambda: D(1)
        r = scheduler.submit(uid, chars[0], "weekly", "py", True, grad_slot=0)
        check("slot0 对 → stage+1（D+3）", r["stage"] == 1 and r["next_date"] == D(4), r)
        r = scheduler.submit(uid, chars[0], "weekly", "wf", True, grad_slot=1)
        check("slot1 同日对（replayed）→ 不推进", r["stage"] == 1 and r["next_date"] == D(4), r)
        r = scheduler.submit(uid, chars[0], "weekly", "defi", True, grad_slot=2)
        check("slot2 同日对 → 不推进（全对字不 stage+3）", r["stage"] == 1 and r["next_date"] == D(4), r)

        # B. 同日 slot2 错：replayed 也回炉（F1 缺陷修复核心断言）
        r = scheduler.submit(uid, chars[1], "weekly", "py", True, grad_slot=0)
        r = scheduler.submit(uid, chars[1], "weekly", "defi", False, grad_slot=2)
        check("slot2 同日错 → 回炉（replayed 不吞错）",
              r["status"] == "learning" and r["stage"] == 1 and r["next_date"] == D(2), r)
        wc = database.get_db().execute(
            "SELECT wrong_count FROM user_hanzi WHERE user_id=? AND hanzi_id=?",
            (uid, chars[1])).fetchone()["wrong_count"]
        check("slot2 错 → wrong_count+1", wc == 1, wc)

        # C. 跨日 slot1 对：非 replayed 但 slot>0 → 不推进（防 D+1 回炉被覆盖成 D+14）
        r = scheduler.submit(uid, chars[2], "weekly", "py", True, grad_slot=0)   # D1 stage1 next D4
        scheduler.today_str = lambda: D(2)
        r = scheduler.submit(uid, chars[2], "weekly", "wf", True, grad_slot=1)
        check("跨日 slot1 对 → 不推进（回炉保护）", r["stage"] == 1 and r["next_date"] == D(4), r)
        r = scheduler.submit(uid, chars[2], "weekly", "defi", False, grad_slot=2)
        check("跨日 slot2 错 → 回炉 D+1", r["stage"] == 1 and r["next_date"] == D(3), r)

        # D. 普通周测（不传 slot）不回归：对 stage+1 / 错回炉
        r = scheduler.submit(uid, chars[3], "weekly", "listen", True)
        check("普通周测对 → stage+1（无回归）", r["stage"] == 1 and r["next_date"] == D(5), r)
        r = scheduler.submit(uid, chars[4], "weekly", "listen", False)
        check("普通周测错 → 回炉（无回归）", r["stage"] == 1 and r["next_date"] == D(3), r)

        # E. 流水完整：同字三 slot 三行 quiz_log 全在
        n = database.get_db().execute(
            "SELECT COUNT(*) FROM quiz_log WHERE user_id=? AND hanzi_id=? AND phase='weekly'",
            (uid, chars[0])).fetchone()[0]
        check("毕业测流水完整（同字 3 行）", n == 3, n)
    finally:
        scheduler.today_str = real_today  # noqa


def wf_gen_tests():
    """④ wf 生成：干扰不成词/tiles 互异/slots 规则/退化链。"""
    print("== ④ wf 找朋友判错版生成 ==")
    db = database.get_db()
    valid = scheduler._valid_words(db)
    chars = db.execute(
        f"""SELECT h.id, h.char FROM hanzi h WHERE h.book='1b' AND {scheduler.CONTENT_READY}
            ORDER BY h.seq LIMIT 60""").fetchall()
    db.close()
    bad, direct = [], 0
    for h in chars:
        q = scheduler.gen_quiz(h["id"], "wf", book="1b")
        if q["quiz_type"] != "wf":
            continue
        direct += 1
        ch = q["hanzi"]
        if len(set(q["tiles"])) != len(q["tiles"]):
            bad.append((ch, "tiles 重复"))
        if set(q["answer"]) != {s["partner"] for s in q["slots"]}:
            bad.append((ch, "answer/slots 不一致"))
        for s in q["slots"]:
            if ch not in s["word"] or s["partner"] not in s["word"]:
                bad.append((ch, "slot 词不含目标/伙伴"))
        for t in q["tiles"]:
            if t not in q["answer"] and ((ch + t) in valid or (t + ch) in valid):
                bad.append((ch, f"干扰 {t} 与目标成词=双解"))
    check(f"1b 前 60 字 wf 生成零违例（直接生成 {direct}）", not bad, bad[:3])
    # 退化：无二字词伙伴的字 → mean
    q = scheduler.gen_quiz(db_hanzi_id("胖"), "wf", book="1b")
    check("「胖」无二字词伙伴 → 退化 mean", q and q["quiz_type"] == "mean", q and q["quiz_type"])
    # 音频字段：槽位带 n_ 单词音
    q = scheduler.gen_quiz(chars[0]["id"], "wf", book="1b")
    check("slots 附 n_ 单词音", all(s.get("audio", "").startswith("n_") for s in q["slots"]),
          [s.get("audio") for s in q["slots"]])


def db_hanzi_id(ch):
    db = database.get_db()
    hid = db.execute("SELECT id FROM hanzi WHERE char=?", (ch,)).fetchone()["id"]
    db.close()
    return hid


def chain_tests(uid):
    """⑥ 多期链：A 挂旗→续订 B→B 跨越 anchor 保持 A→毕业测结算清零→C 期重新挂旗。"""
    print("== ⑥ 多期链 ==")
    day0 = date(2026, 3, 2)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    db = database.get_db()
    chars = ready_chars(db, 25)   # A3 + B10 + C10：B/C 走 create_plan，target 受 v61 下限
    db.close()                    # MIN_TARGET_CHARS=10 钳制，须学满 10 字才真实跨越
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: D(0)
        db = database.get_db()
        db.execute("""UPDATE users SET grade='1A', target_chars=3, plan_days=7,
                      plan_start=?, plan_base_learned=0, onboarded=1 WHERE id=?""", (D(0), uid))
        db.commit()
        db.close()
        for hid in chars[0:3]:                     # A 期 3 字 → 挂旗 anchor=D0
            scheduler.submit(uid, hid, "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("A 期达成挂旗", p == 1 and a == D(0), (p, a))
        # 当日续订 B 期（create_plan 记新 base=3、新 plan_start=D1；target 2→钳 10，v61 下限）
        scheduler.today_str = lambda: D(1)
        scheduler.create_plan(uid, 2, 7)
        for hid in chars[3:13]:                    # B 期 10 字 → 跨越再命中（base3+10=13）
            scheduler.submit(uid, hid, "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("B 期跨越：flag 保持、anchor 仍=A（合并考 A+B）", p == 1 and a == D(0), (p, a))
        # 毕业测结算 → 清零
        scheduler.weekly_finish(uid, 1, 10, 15, [chars[0]])
        p, a = grad_flags(database.get_db(), uid)
        check("毕业测结算 → 清旗", p == 0 and a is None, (p, a))
        # C 期再达成 → 重新挂旗（新 anchor）
        scheduler.create_plan(uid, 2, 7)
        for hid in chars[13:23]:                   # C 期 10 字（base13+10=23 跨越）
            scheduler.submit(uid, hid, "learn", "card", True)
        p, a = grad_flags(database.get_db(), uid)
        check("C 期跨越 → 重挂新 anchor", p == 1 and a == D(1), (p, a))
    finally:
        scheduler.today_str = real_today  # noqa


if __name__ == "__main__":
    database.init_db()   # 迁移 grad 两列（幂等；与容器启动路径一致——旧容器 SELECT * 不受新增列影响）
    db = database.get_db()
    mido_n = db.execute("SELECT COUNT(*) FROM users WHERE username='mido'").fetchone()[0]
    db.close()
    if not mido_n:
        print("⛔ 缺 mido 账号，库异常，拒绝运行")
        sys.exit(2)
    uid = test_uid(database.get_db())
    clean_test_data(uid)
    try:
        flag_tests(uid)
        clean_test_data(uid)
        scope_tests(uid)
        clean_test_data(uid)
        submit_semantics_tests(uid)
        clean_test_data(uid)
        wf_gen_tests()
        chain_tests(uid)
    finally:
        clean_test_data(uid)   # teardown：只清 test，mido 零接触
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
