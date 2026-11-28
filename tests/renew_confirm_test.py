#!/usr/bin/env python3
"""续订重摸底 + 摸底游标（v69 后端验收，2026-11-28 拍板）

背景：续订路径自 v29 起跳过摸底（「续订：不重走摸底，直接回今日」），续订后的新字从未
经过摸底，已认识的字被当成新字学。拍板：摸底是每个计划的组成部分——create_plan 开启一轮摸底，confirm_submit
结束它；摸底位置改用游标（users.confirm_book + confirm_seq），修掉补摸批跳字。

验收（test 账号直调 scheduler，只动 test）：
A. 续订重开摸底：create_plan 置 onboarded=0、游标清零；首批 = 池头 ceil(目标×1.3)，
   status='new' 的字（看过笔顺没写完卡）可摸；旧设计残留水位不干扰
B. 补摸不跳字：首批 65 点亮 40 → 补摸批紧接首批之后的 50 字（旧码 OFFSET 漂移 → 跳过 40 字）
C. 跨册不重复扣减：目标 280、1a 全册点亮 260 → 1b 补摸 6 批即够（旧码 raw 少算 260 → 11 批）
D. 本册摸尽：起始册 = 池头所在册（旧码恒取年级册 → 空表，摸底被静默跳过）
E. 刷新重进：摸底中途重调 confirm_batches → 游标回到当前候选第 n 个；再点亮后生词够数即结束
F. upsert 守卫：已学字被提交为「认识」不被覆盖成 skipped
G/H. 供给约束：摸底没做完不发新字；做完自动关轮次；有游标只从已摸底范围出，范围内学完即
   暂停；旧残留游标（无 confirm_seq）不限
I. 滞后到达的摸底提交：没有有效游标 / 轮次已关 → 既不供应也不关轮次、不动游标

载体：test 账号直调 scheduler + monkey-patch today_str，无需起服务（线上库可直跑，只动 test）。
     绝不触碰 mido/mily（clean_test_data 同 wrong_priority_test）。
用法: python3 tests/renew_confirm_test.py
"""
import sys
from datetime import date

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import database                     # noqa: E402
import scheduler                    # noqa: E402
from wrong_priority_test import check, test_uid, clean_test_data  # noqa: E402


def clean(uid):
    """clean_test_data 不清毕业测挂旗/模式——本测试会触发挂旗，一并清零防泄漏给后续套件"""
    clean_test_data(uid)
    db = database.get_db()
    db.execute("UPDATE users SET grad_pending=0, grad_anchor=NULL, mode=NULL WHERE id=?", (uid,))
    db.commit()
    db.close()


def urow(uid):
    db = database.get_db()
    r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    db.close()
    return r


def seq_chars(db, n=None):
    """1a 内容就绪字（照教材序）前 n 个：[(id, seq)]"""
    lim = f"LIMIT {n}" if n else ""
    return [(r["id"], r["seq"]) for r in db.execute(
        f"""SELECT h.id, h.seq FROM hanzi h WHERE h.book='1a' AND {scheduler.CONTENT_READY}
            ORDER BY h.seq {lim}""")]


def fresh_1a(uid):
    """复位 + 选册 1A（book_of 口径）；返回 1a 字表 [(id, seq)]"""
    clean(uid)
    db = database.get_db()
    db.execute("UPDATE users SET grade='1A' WHERE id=?", (uid,))
    db.commit()
    c = seq_chars(db)
    db.close()
    return c


def flat(batches):
    return [x for b in batches for x in b]


def cursor(uid):
    u = urow(uid)
    return u["confirm_book"], u["confirm_seq"]


def case_renew_reopen(uid):
    print("== A. 续订重开摸底 ==")
    c = fresh_1a(uid)
    db = database.get_db()
    # 造「学过一阵、引导早已完成」的用户：12 learning + 3 skipped + 池头一个 'new' 行
    for hid, _ in c[:12]:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,first_date) "
                   "VALUES(?,?,'learning',0,'2026-01-01')", (uid, hid))
    for hid, _ in c[12:15]:
        db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status) VALUES(?,?,'skipped')", (uid, hid))
    db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status) VALUES(?,?,'new')", (uid, c[15][0]))
    # 旧设计残留水位（线上 test 账号实况同款：confirm_book='2b'、confirm_upto=300）
    db.execute("UPDATE users SET onboarded=1, confirm_book='2b', confirm_upto=300 WHERE id=?", (uid,))
    db.commit()
    db.close()
    scheduler.create_plan(uid, 30, 7)
    u = urow(uid)
    check("A 续订后 onboarded=0（摸底重开）", u["onboarded"] == 0, u["onboarded"])
    check("A 续订后游标清零", u["confirm_book"] is None and u["confirm_seq"] is None,
          (u["confirm_book"], u["confirm_seq"]))
    rows = flat(scheduler.confirm_batches(urow(uid)))
    ids = [x["id"] for x in rows]
    check("A 首批 = 池头 39 字（ceil(30×1.3)），不含已学/已点亮",
          ids == [h for h, _ in c[15:54]], (len(ids), ids[:4]))
    check("A 'new' 行的字可摸且排首位", bool(ids) and ids[0] == c[15][0], ids[:2])
    check("A 游标 = 首批末行", cursor(uid) == ("1a", c[53][1]), cursor(uid))


def case_no_gap(uid):
    print("== B. 补摸不跳字 ==")
    c = fresh_1a(uid)
    scheduler.create_plan(uid, 50, 7)
    first = flat(scheduler.confirm_batches(urow(uid)))
    check("B 首批 65 字", len(first) == 65, len(first))
    scheduler.confirm_submit(uid, [x["id"] for x in first[:40]])
    more = scheduler.confirm_more(urow(uid)) or []
    mseq = [x["seq"] for x in more]
    check("B 补摸批 = 首批之后 50 字（seq 66–115），不跳字",
          mseq == [s for _, s in c[65:115]], (mseq[:3], mseq[-3:]))
    check("B 游标推进到补摸批末行", cursor(uid) == ("1a", c[114][1]), cursor(uid))


def case_cross_book(uid):
    print("== C. 跨册不重复扣减 ==")
    fresh_1a(uid)
    scheduler.create_plan(uid, 280, 30)
    first = flat(scheduler.confirm_batches(urow(uid)))
    check("C 首批 = 1a 全册 281 字", len(first) == 281, len(first))
    scheduler.confirm_submit(uid, [x["id"] for x in first[:260]])     # 1a 剩 21 个生词
    batches = []
    while True:                                     # 一个不点亮，摸到够数为止
        more = scheduler.confirm_more(urow(uid))
        if not more:
            break
        batches.append(more)
    n = sum(len(b) for b in batches)
    check("C 1b 补摸 6 批共 300 字即够（生词 21+300≥280）",
          len(batches) == 6 and n == 300 and all(x.get("book") == "1b" for b in batches for x in b),
          (len(batches), n))
    check("C 目标未被压没", urow(uid)["target_chars"] == 280, urow(uid)["target_chars"])


def case_book_exhausted(uid):
    print("== D. 本册摸尽：起始册 = 池头所在册 ==")
    fresh_1a(uid)
    db = database.get_db()
    db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status) "
               "SELECT ?, h.id, 'skipped' FROM hanzi h WHERE h.book='1a'", (uid,))
    db.execute("UPDATE users SET onboarded=1 WHERE id=?", (uid,))
    db.commit()
    db.close()
    scheduler.create_plan(uid, 30, 7)
    first = flat(scheduler.confirm_batches(urow(uid)))
    check("D 首批 39 字全在下一册 1b（旧码返回空表）",
          len(first) == 39 and all(x.get("book") == "1b" for x in first),
          (len(first), [x.get("book") for x in first[:3]]))
    check("D 游标落在 1b 首批末行", cursor(uid) == ("1b", first[-1]["seq"]) if first else False,
          cursor(uid))
    scheduler.confirm_submit(uid, [x["id"] for x in first[:10]])
    more = scheduler.confirm_more(urow(uid)) or []
    check("D 生词 29<30 → 从 1b 游标之后补摸 50 字，与首批不重叠",
          len(more) == 50 and not {x["id"] for x in more} & {x["id"] for x in first}
          and all(x.get("book") == "1b" for x in more), len(more))
    check("D 目标不变", urow(uid)["target_chars"] == 30, urow(uid)["target_chars"])


def case_refresh(uid):
    print("== E. 摸底中途刷新重进 ==")
    c = fresh_1a(uid)
    scheduler.create_plan(uid, 50, 7)
    first = flat(scheduler.confirm_batches(urow(uid)))
    scheduler.confirm_submit(uid, [x["id"] for x in first[:40]])
    scheduler.confirm_more(urow(uid))               # 补摸批 seq 66–115，游标 115
    again = flat(scheduler.confirm_batches(urow(uid)))     # 刷新：boot 重出首批
    check("E 重列批 = 当前候选前 65 字（seq 41–105），不含已点亮",
          [x["seq"] for x in again] == [s for _, s in c[40:105]], (len(again), again[:1]))
    check("E 游标回到重列批末行", cursor(uid) == ("1a", c[104][1]), cursor(uid))
    scheduler.confirm_submit(uid, [x["id"] for x in again[:10]])   # 再点亮 10 → 生词 55≥50
    check("E 生词够数 → 不再补摸", scheduler.confirm_more(urow(uid)) is None)


def case_upsert_guard(uid):
    print("== F. upsert 守卫 ==")
    c = fresh_1a(uid)
    db = database.get_db()
    db.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,first_date) "
               "VALUES(?,?,'learning',2,'2026-01-01')", (uid, c[0][0]))
    db.commit()
    db.close()
    scheduler.confirm_submit(uid, [c[0][0], c[1][0]])        # 已学字 + 生字一起提交为「认识」
    db = database.get_db()
    st = [db.execute("SELECT status FROM user_hanzi WHERE user_id=? AND hanzi_id=?",
                     (uid, h)).fetchone()["status"] for h in (c[0][0], c[1][0])]
    db.close()
    check("F 已学字（learning）不被覆盖成 skipped", st[0] == "learning", st)
    check("F 无记录的字正常落 skipped", st[1] == "skipped", st)


def case_supply(uid):
    print("== G/H. 供给约束 ==")
    c = fresh_1a(uid)
    day0 = date(2026, 1, 5)
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: day0.isoformat()
        scheduler.create_plan(uid, 20, 7)            # daily_new = ceil(20/6) = 4
        t = scheduler.build_today(uid)
        check("G 摸底没做完不发新字", t["mode"] == "learn" and t["new"] == [] and t.get("new_paused") is False,
              (t.get("new"), t.get("new_paused")))
        first = flat(scheduler.confirm_batches(urow(uid)))
        check("G 首批 26 字", len(first) == 26, len(first))
        scheduler.confirm_submit(uid, [first[0]["id"]])        # 点亮 1 个 → 生词 25≥20，摸底结束
        check("G 摸底做完自动关轮次（onboarded=1）",
              scheduler.confirm_more(urow(uid)) is None and urow(uid)["onboarded"] == 1,
              urow(uid)["onboarded"])
        screened = {x["id"] for x in first} - {first[0]["id"]}
        t = scheduler.build_today(uid)
        new_ids = [h["id"] for h in t["new"]]
        check("H 新字只从已摸底范围出（4 个，不含已点亮）",
              len(new_ids) == 4 and set(new_ids) <= screened, new_ids)
        check("H 范围内还有生词 → 不算暂停", t.get("new_paused") is False, t.get("new_paused"))
        for hid in screened:                        # 范围内生词全部学完（1a 池里还剩 250+ 未学字）
            scheduler.submit(uid, hid, "learn", "card", True)
        scheduler.today_str = lambda: (day0.replace(day=6)).isoformat()
        t = scheduler.build_today(uid)
        check("H 范围学完 → 新字暂停（等续订重新摸底）",
              t["mode"] == "learn" and t["new"] == [] and t.get("new_paused") is True,
              ([h["char"] for h in t["new"]], t.get("new_paused")))
        db = database.get_db()                      # v69 前旧账号的残留形态：confirm_book 有值、
        db.execute("UPDATE users SET confirm_book='2b', confirm_upto=300, confirm_seq=NULL WHERE id=?",
                   (uid,))                          # confirm_seq 为空 → 按无游标，不限范围
        db.commit()
        db.close()
        t = scheduler.build_today(uid)
        check("H 旧残留游标（无 confirm_seq）不限范围、不暂停",
              len(t["new"]) == 4 and t.get("new_paused") is False, (len(t["new"]), t.get("new_paused")))
    finally:
        scheduler.today_str = real_today  # noqa


def case_stale_submit(uid):
    print("== I. 滞后到达的摸底提交：无有效游标 / 轮次已关 ==")
    fresh_1a(uid)
    scheduler.create_plan(uid, 20, 7)
    scheduler.confirm_submit(uid, [])               # 轮次已开但还没出过表（无游标）
    check("I 无有效游标的提交：不供应", scheduler.confirm_more(urow(uid)) is None)
    check("I 无有效游标的提交：不关轮次（新字仍被挡）", urow(uid)["onboarded"] == 0,
          urow(uid)["onboarded"])
    first = flat(scheduler.confirm_batches(urow(uid)))
    scheduler.confirm_submit(uid, [first[0]["id"]])
    scheduler.confirm_more(urow(uid))               # 正常做完：关轮次
    for x in first[1:11]:                           # 学掉 10 个 → 范围内生词 15<20
        scheduler.submit(uid, x["id"], "learn", "card", True)
    cur0 = cursor(uid)
    scheduler.confirm_submit(uid, [])               # 另一台设备上残留的摸底页滞后提交
    check("I 轮次已关的滞后提交：不供应、游标不动",
          scheduler.confirm_more(urow(uid)) is None and cursor(uid) == cur0, cursor(uid))


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
        case_renew_reopen(uid)
        case_no_gap(uid)
        case_cross_book(uid)
        case_book_exhausted(uid)
        case_refresh(uid)
        case_upsert_guard(uid)
        case_supply(uid)
        case_stale_submit(uid)
    finally:
        clean(uid)   # teardown：只清 test，mido/mily 零接触
    from wrong_priority_test import PASS_N, FAIL_N     # 跑完再取（顶层按值导入会拿到 0）
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
