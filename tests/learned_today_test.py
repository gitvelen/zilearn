#!/usr/bin/env python3
"""learned_today 只认新字卡（v68 后端验收，2026-11-23 mido「今日新学 0」事故回归）

事故：隔夜陈旧包重考前一天的六关 → 关题流水 ts=当天 → build_today 的 learned_today
（原 phase='learn' 不分 card/关题）= 前一天的 13 字 → 新字额度被占满（new=[]）+ 前一天的字进关池。
同一放大器也吃「会话跨午夜、关题落到次日」这条合法路径。

验收（52 字/30 天 → daily_new=2）：
A. 前一天的字当天只有关题流水（含同关重测重复行）→ 不进 learned_today、不占额度：
   new=daily_new 个池内新字；该字调度状态不被关题流水改动、照常进 D+1 复习
B. 当日新字卡计入额度与关池：card 完成 1 字（同日再过一关）→ new=daily_new−1，learned_today 恰为该字
C. gate_done 仍按关题流水统计（分关断点续做口径不变）

载体：test 账号直调 scheduler + monkey-patch today_str，无需起服务，线上库可直跑。
     只动 test 账号——绝不触碰 mido/mily（clean_test_data 同 wrong_priority_test）。
用法: python3 tests/learned_today_test.py
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import database                     # noqa: E402
import scheduler                    # noqa: E402
from wrong_priority_test import check, test_uid, clean_test_data, ready_chars  # noqa: E402

TARGET, DAYS = 52, 30               # daily_new = ceil(52/26) = 2
DAILY_NEW = math.ceil(TARGET / (DAYS - DAYS // 7))


def clean(uid):
    """clean_test_data 不清毕业测挂旗（历史坑）——本测试不会触发，仍一并清零防泄漏给后续套件"""
    clean_test_data(uid)
    db = database.get_db()
    db.execute("UPDATE users SET grad_pending=0, grad_anchor=NULL WHERE id=?", (uid,))
    db.commit()
    db.close()


def set_plan(uid, plan_start):
    db = database.get_db()
    db.execute("""UPDATE users SET grade='1A', target_chars=?, plan_days=?,
                  plan_start=?, onboarded=1 WHERE id=?""", (TARGET, DAYS, plan_start, uid))
    db.commit()
    db.close()


def regate(uid, hid):
    """重考前一天的关：对错都有 + 同关重测重复行（v40 关题重测照记流水）"""
    for qt, ok in (("listen", True), ("py", False), ("py", True), ("lsent", True)):
        scheduler.submit(uid, hid, "learn", qt, ok)


def learned_today_tests(uid):
    day0 = date(2026, 1, 5)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    real_today = scheduler.today_str
    try:
        db = database.get_db()
        x = ready_chars(db, 12)[0]                   # 「昨天」学的字
        db.close()

        print("== A. 前一天的字当天只有关题流水 ==")
        scheduler.today_str = lambda: D(0)
        set_plan(uid, D(0))
        scheduler.submit(uid, x, "learn", "card", True)
        scheduler.today_str = lambda: D(1)
        regate(uid, x)
        t = scheduler.build_today(uid)
        new_ids = [h["id"] for h in t["new"]]
        lt_ids = [h["id"] for h in t["learned_today"]]
        check("A 学习日包", t["mode"] == "learn", t["mode"])
        check("A 不进 learned_today（不进关池）", x not in lt_ids, lt_ids)
        check("A 不占新字额度：new = daily_new", len(new_ids) == DAILY_NEW, new_ids)
        check("A new 不含前一天的字", x not in new_ids, new_ids)
        db = database.get_db()
        ph = ",".join("?" * len(new_ids))
        touched = (db.execute(f"SELECT COUNT(*) n FROM user_hanzi WHERE user_id=? AND hanzi_id IN ({ph})",
                              (uid, *new_ids)).fetchone()["n"],
                   db.execute(f"SELECT COUNT(*) n FROM quiz_log WHERE user_id=? AND hanzi_id IN ({ph})",
                              (uid, *new_ids)).fetchone()["n"])
        row = db.execute("SELECT stage, next_date, first_date FROM user_hanzi WHERE user_id=? AND hanzi_id=?",
                         (uid, x)).fetchone()
        db.close()
        check("A new 全是池内新字（无调度行、无流水）", touched == (0, 0), touched)
        check("A 关题流水不改调度状态（stage0 / next=D+1 / first_date 不变）",
              (row["stage"], row["next_date"], row["first_date"]) == (0, D(1), D(0)), dict(row))
        check("A 前一天的字照常进 D+1 复习", x in [r["id"] for r in t["review"]],
              [r["id"] for r in t["review"]])

        print("== B. 当日新字卡计入额度与关池 ==")
        y = new_ids[0]
        scheduler.submit(uid, y, "learn", "card", True)     # 当日第 1 张新字卡
        scheduler.submit(uid, y, "learn", "mean", False)    # 当天正常过六关
        t = scheduler.build_today(uid)
        new_ids2 = [h["id"] for h in t["new"]]
        lt_ids2 = [h["id"] for h in t["learned_today"]]
        check("B 当日新字卡占 1 个额度：new = daily_new − 1（补差额）",
              new_ids2 == new_ids[1:], (new_ids2, new_ids[1:]))
        check("B learned_today 恰为当日新学之字", lt_ids2 == [y], lt_ids2)

        print("== C. 关进度口径不变 ==")
        check("C gate_done 仍按关题流水统计（含前一天的字的重考行）",
              t["gate_done"].get("listen") == [x] and t["gate_done"].get("py") == [x]
              and t["gate_done"].get("mean") == [y], t["gate_done"])
    finally:
        scheduler.today_str = real_today  # noqa


if __name__ == "__main__":
    db = database.get_db()
    ok = db.execute("SELECT COUNT(*) FROM users WHERE username IN ('mido','mily')").fetchone()[0]
    db.close()
    if ok != 2:
        print("⛔ 缺 mido/mily 账号，库异常，拒绝运行")
        sys.exit(2)
    uid = test_uid(database.get_db())
    clean(uid)
    try:
        learned_today_tests(uid)
    finally:
        clean(uid)   # teardown：只清 test，mido/mily 零接触
    from wrong_priority_test import PASS_N, FAIL_N     # 跑完再取（顶层按值导入会拿到 0）
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
