#!/usr/bin/env python3
"""stroke_only（mily 纯写字模式，2026-11-19 拍板）后端验收

改动验收：
① 供给结构：恒 learn（stroke=True）、daily_new 不扣周测日、review/audit/gate 恒空
② D+1 复习注册被供给层置空：submit 照常注册 next_date=D+1（learned 真源），
   但 stroke 供给恒不消费复习池
③ 第 7 个活动日（normal 周测日）仍 learn：周测/毕业测/跨日续做对 stroke_only 永不触发
④ 写满 target：grad_pending 恒 0（不挂毕业测旗）、当日三空 learn（完成态）
⑤ 幂等：同字重提 learn/stroke 不重复计数
⑥ 续订：plan_base_learned 前移、新字供给排除已学
⑦ normal 隔离（用户两次强调的红线）：同账号翻回 normal——build_today 无 stroke 键、
   daily_new 恢复扣周测口径、遗留 next_date 的字回到复习池
   （=「将来转 normal 复习链自动接管」的证明，亦是 mode 隔离的直接断言）

载体：test 账号临时翻 mode + monkey-patch today_str（同 grad_test.py 惯例），
     线上库可直接跑。只动 test 账号——绝不触碰 mido/mily。

用法: python3 tests/stroke_mode_test.py
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
    """清 test 账号学习状态（mido/mily 零接触）；mode 由调用方管理（本测试翻/复原）"""
    db = database.get_db()
    db.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL, plan_start=NULL,
                  plan_base_learned=0, confirm_upto=0, onboarded=0, day_offset=0,
                  grad_pending=0, grad_anchor=NULL WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log", "daily_mail"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.commit()
    db.close()


def set_mode(uid, mode):
    db = database.get_db()
    db.execute("UPDATE users SET mode=? WHERE id=?", (mode, uid))
    db.commit()
    db.close()


def grad_flag(db, uid):
    return db.execute("SELECT grad_pending p FROM users WHERE id=?", (uid,)).fetchone()["p"]


def stroke_supply_tests(uid):
    """①-⑥：stroke_only 全链（供给/D+1 置空/无周测/不挂旗/幂等/续订）"""
    print("== ① 供给结构 / ② D+1 置空 / ③ 无周测 / ④ 满员不挂旗 ==")
    day0 = date(2026, 1, 5)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    db = database.get_db()
    chars = [r["id"] for r in db.execute(
        f"""SELECT h.id FROM hanzi h WHERE h.book='1a' AND {scheduler.CONTENT_READY}
            ORDER BY h.seq LIMIT 20""")]
    db.close()
    real_today = scheduler.today_str
    written = []
    try:
        # D0：建计划（target=14, days=7 → stroke daily_new=2；normal 口径 ceil(14/6)=3）
        scheduler.today_str = lambda: D(0)
        scheduler.create_plan(uid, 14, 7)
        t = scheduler.build_today(uid)
        check("恒 learn + stroke 标志", t["mode"] == "learn" and t.get("stroke") is True, str(t.get("mode")))
        check("daily_new 不扣周测日（2 而非 normal 的 3）", len(t["new"]) == 2, str(len(t["new"])))
        check("无复习/抽检/分关", t["review"] == [] and t["audit"] == [] and t["gate_done"] == {})
        # ② D0 写 2 字 → D1：复习池被供给层置空、新字为下一批
        for h in t["new"]:
            scheduler.submit(uid, h["id"], "learn", "stroke", True)
            written.append(h["id"])
        d0_ids = set(written)
        scheduler.today_str = lambda: D(1)
        t = scheduler.build_today(uid)
        check("D1 复习被置空（next_date=D+1 注册但无消费）", t["review"] == [])
        check("D1 新字=下一批 2 字（不重出昨日字）",
              len(t["new"]) == 2 and all(h["id"] not in d0_ids for h in t["new"]))
        # ③ 铺活动日到第 7 天（di=6 → (di+1)%7==0 = normal 周测日）→ 仍 learn
        for i in range(1, 7):                      # D1..D6，每天写当日 2 字
            scheduler.today_str = lambda i=i: D(i)
            t = scheduler.build_today(uid)
            if t["mode"] != "learn":
                check(f"D{i} 仍 learn（无周测）", False, t["mode"])
                break
            for h in t["new"]:
                scheduler.submit(uid, h["id"], "learn", "stroke", True)
                written.append(h["id"])
        scheduler.today_str = lambda: D(6)
        t = scheduler.build_today(uid)
        check("第 7 个活动日仍 learn（normal 周测日被绕过）",
              t["mode"] == "learn" and t.get("stroke") is True, str(t.get("mode")))
        # ④ 14 字写满（D0..D6 每天 2 字）→ 不挂毕业测旗 + 当日完成态
        check("已写满 14 字", len(written) == 14, str(len(written)))
        db = database.get_db()
        check("写满达成后 grad_pending==0（不挂毕业测旗）", grad_flag(db, uid) == 0)
        db.close()
        t = scheduler.build_today(uid)   # D6 已写完当日 2 字：当日三空（learned_today 满员剔除）
        check("当日写完三空 learn（完成态，learned_today=当日 2 字）",
              t["mode"] == "learn" and t["new"] == [] and t["review"] == [] and t["audit"] == []
              and len(t["learned_today"]) == 2)
        # ⑤ 幂等：同字重提不重复计数
        db = database.get_db()
        n_before = db.execute(
            "SELECT COUNT(DISTINCT hanzi_id) n FROM quiz_log WHERE user_id=? AND phase='learn'", (uid,)).fetchone()["n"]
        db.close()
        scheduler.submit(uid, written[0], "learn", "stroke", True)
        db = database.get_db()
        n_after = db.execute(
            "SELECT COUNT(DISTINCT hanzi_id) n FROM quiz_log WHERE user_id=? AND phase='learn'", (uid,)).fetchone()["n"]
        db.close()
        check("同字重提 learned_today 不重复计数", n_before == n_after == 14, f"{n_before}->{n_after}")
        # 写满次日：达成≠供给停止（达成由前端 planDone 判定等续订），仍 learn+新字
        scheduler.today_str = lambda: D(7)
        t = scheduler.build_today(uid)
        check("写满次日仍 learn 且继续供新字 2 个",
              t["mode"] == "learn" and len(t["new"]) == 2, f"new={len(t['new'])}")
        # ⑥ 续订：base 前移到 14、新字排除已学（target=13 避开 MIN_TARGET_CHARS=10 钳制；
        # D8：避开 D7 已提交流水）
        scheduler.create_plan(uid, 13, 7)
        db = database.get_db()
        row = db.execute("SELECT plan_base_learned FROM users WHERE id=?", (uid,)).fetchone()
        db.close()
        check("续订 base 前移到 14", row["plan_base_learned"] == 14, str(row["plan_base_learned"]))
        scheduler.today_str = lambda: D(8)
        t = scheduler.build_today(uid)
        check("续订后新字=2（ceil(13/7)）且排除已学",
              len(t["new"]) == 2 and all(h["id"] not in set(written) for h in t["new"]),
              f"new={len(t['new'])}")
    finally:
        scheduler.today_str = real_today  # noqa


def normal_isolation_tests(uid):
    """⑦ 同账号翻回 normal：无 stroke 键 + daily_new 恢复扣周测 + 复习池接管遗留 next_date"""
    print("== ⑦ normal 隔离（mode 翻回后行为与改动前一致）==")
    set_mode(uid, None)                           # 确保从 normal 起跑（main 里翻的 stroke 在此复位）
    day0 = date(2026, 2, 2)
    D = lambda n: (day0 + timedelta(days=n)).isoformat()  # noqa: E731
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: D(0)
        scheduler.create_plan(uid, 14, 7)
        t = scheduler.build_today(uid)
        check("normal 无 stroke 键（响应与改动前一致）", "stroke" not in t)
        check("normal daily_new 恢复扣周测口径（3 而非 2）", len(t["new"]) == 3, str(len(t["new"])))
        ids = []
        for h in t["new"]:
            scheduler.submit(uid, h["id"], "learn", "card", True)
            ids.append(h["id"])
        # 次日翻 stroke 写 2 字（next_date=D+2 注册；当日 normal 字 D+1 已可回炉），再翻回 normal
        scheduler.today_str = lambda: D(1)
        set_mode(uid, "stroke_only")
        t = scheduler.build_today(uid)
        for h in t["new"][:2]:
            scheduler.submit(uid, h["id"], "learn", "stroke", True)
            ids.append(h["id"])
        set_mode(uid, None)
        scheduler.today_str = lambda: D(2)
        t = scheduler.build_today(uid)
        review_ids = {h["id"] for h in t["review"]}
        check("翻回 normal 后复习池接管全部写过字（含 stroke 写的）", set(ids) <= review_ids,
              f"review={len(review_ids)}")
        check("normal 供给无 stroke 键（再验）", "stroke" not in t)
    finally:
        scheduler.today_str = real_today  # noqa


def main():
    database.init_db()
    db = database.get_db()
    uid = test_uid(db)
    db.close()
    clean_test_data(uid)
    set_mode(uid, "stroke_only")
    try:
        stroke_supply_tests(uid)
        clean_test_data(uid)
        normal_isolation_tests(uid)
    finally:
        clean_test_data(uid)
        set_mode(uid, None)                       # 复原：test 账号回 normal
    print(f"\n{'='*40}\nstroke_mode_test: {PASS_N} 通过, {FAIL_N} 失败")
    sys.exit(1 if FAIL_N else 0)


if __name__ == "__main__":
    main()
