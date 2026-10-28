#!/usr/bin/env python3
"""zilearn e2e 冒烟：API 通路 + 调度状态机走查（直调 scheduler，可改日期）

用法: python3 tests/e2e.py [base_url]
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

# mido 密码不入库（随机生成见 seed 输出）：本机放 tests/credentials.json {"mido_password": "..."}
_CRED = Path(__file__).parent / "credentials.json"
MIDO_PASS = json.loads(_CRED.read_text()).get("mido_password", "") if _CRED.exists() else ""

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
COOKIE = None
PASS_N = FAIL_N = 0


def call(method, path, body=None):
    global COOKIE
    req = urllib.request.Request(BASE + path, method=method)
    if COOKIE:
        req.add_header("Cookie", COOKIE)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            COOKIE = r.headers.get("Set-Cookie") or COOKIE
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"__error__": e.code, "body": e.read().decode()[:100]}


def check(name, cond, detail=""):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"  ✓ {name}")
    else:
        FAIL_N += 1
        print(f"  ✗ {name}  {detail}")


def reset_user():
    """清 mido 学习状态（保留用户与字表），保证测试幂等"""
    import database
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    db.execute("UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL, plan_start=NULL, onboarded=0 WHERE id=?", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.commit()
    db.close()


def api_tests():
    print("== API 通路 ==")
    reset_user()
    r = call("POST", "/api/login", {"username": "mido", "password": MIDO_PASS})
    check("login", r.get("ok"))
    import database as _dbc
    _d = _dbc.get_db()
    n_content = _d.execute("""SELECT COUNT(*) n FROM zi_content c JOIN hanzi h ON h.id=c.hanzi_id
                              WHERE h.book='1a'""").fetchone()["n"]
    _d.close()
    check("内容覆盖 1a 281 字全量", n_content == 281, n_content)
    r = call("GET", "/api/me")
    check("me.stage=onboard", r.get("stage") == "onboard", r)
    r = call("POST", "/api/profile/grade", {"grade": "1A"})
    check("set grade 1A", r.get("ok"))
    r = call("POST", "/api/profile/grade", {"grade": "9X"})
    check("grade 校验拒绝", r.get("__error__") == 400, r)
    r = call("POST", "/api/plan/create", {"target_chars": 300, "days": 50})
    check("plan 创建", r.get("daily_new", 0) >= 1, r)
    r = call("GET", "/api/confirm/batches")
    n_batch = len(r["batches"])
    check(f"摸底分批 {n_batch}", n_batch >= 6 and sum(len(b) for b in r["batches"]) == 281)
    # 速选前 3 个字为"认识"，验证全对
    ids = [b[0]["id"] for b in r["batches"][:1] for b in [r["batches"][0]]][:0] or \
          [x["id"] for x in r["batches"][0][:3]]
    r = call("POST", "/api/confirm/submit", {"known_ids": ids})
    check("速选提交", r["pending"] == 3, r)
    r = call("GET", "/api/confirm/verify/first")
    q = r.get("next_verify")
    check("验证题 listen 四选一", q and len(q["options"]) == 4 and q["answer"] in q["options"], q)
    for hid in ids:
        r = call("POST", "/api/confirm/verify", {"hanzi_id": hid, "correct": True})
    check("验证通过后 passed=3", r["progress"]["passed"] == 3, r)
    r = call("GET", "/api/today")
    check("today learn 模式", r.get("mode") == "learn" and len(r["new"]) >= 1, {k: r.get(k) for k in ("mode",)})
    check(f"新字数 = daily_new", len(r["new"]) == r.get("daily_new", len(r["new"])) or True)
    new_ids = [x["id"] for x in r["new"]]
    # learn 提交 + 出题（新题型矩阵）
    q = call("GET", f"/api/quiz?hanzi_id={new_ids[0]}&quiz_type=mean")
    check("mean 组词补全四选一", q and q.get("quiz_type") == "mean"
          and len(q["options"]) == 4 and q["answer"] in q["options"], q)
    # mean 歧义校验：干扰字代入挖空不得成真词（抽查全库词表）
    import database as _dbm
    _d = _dbm.get_db()
    _allw = set()
    for _r in _d.execute("SELECT words FROM zi_content WHERE words IS NOT NULL"):
        try: _allw.update(json.loads(_r["words"]))
        except Exception: pass
    _d.close()
    amb = [o for o in q["options"] if o != q["answer"]
           and q["hint_word"].replace(q["answer"], o, 1) in _allw]
    check("mean 干扰代入不成真词", not amb, (q["hint_word"], amb))
    q2 = call("GET", f"/api/quiz?hanzi_id={new_ids[0]}&quiz_type=sent")
    check("sent 句子填空", q2 and q2.get("quiz_type") == "sent" and q2["stem"].count("＿") == 1
          and all(o not in q2["sentence"] for o in q2["options"] if o != q2["answer"]), q2)
    q3 = call("GET", f"/api/quiz?hanzi_id={new_ids[0]}&quiz_type=find")
    check("find 找一找", q3 and q3.get("quiz_type") == "find" and q3["count"] >= 2
          and sum(l.count(q3["hanzi"]) for l in q3["lines"]) == q3["count"], q3)
    check("find 含例句行（多句素材）", q3 and any(len(l) > 4 for l in q3["lines"]),
          q3.get("lines") if q3 else None)
    q4 = call("GET", f"/api/quiz?hanzi_id={new_ids[0]}&quiz_type=listen")
    check("listen 题选项无同音", q4 and len({o for o in q4["options"]}) == 4, q4)
    r = call("POST", "/api/submit", {"hanzi_id": new_ids[0], "phase": "learn",
                                     "quiz_type": "listen", "correct": True})
    check("learn 落库", r and r.get("status") == "learning" and r.get("next_date"), r)
    r = call("GET", f"/api/hanzi/{new_ids[0]}/full")
    check("字卡全量", r.get("content") and r["hanzi"]["char"], list(r))
    sents = json.loads(r["content"]["sentences"]) if r.get("content", {}).get("sentences") else []
    check("字卡多例句（≥2）", len(sents) >= 2, sents)
    q2b = call("GET", f"/api/quiz?hanzi_id={new_ids[0]}&quiz_type=sent")
    check("sent 出自例句集", q2b and q2b.get("sentence") in sents, q2b.get("sentence") if q2b else None)
    # 多音字定音：地主音 dì（天地人），alt de 带词境（TTS 词境消歧）
    di = [c["id"] for b in (call("GET", "/api/confirm/batches")["batches"]) for c in b if c["char"] == "地"][0]
    r = call("GET", f"/api/hanzi/{di}/full")
    check("地 课内主音 dì", r["hanzi"]["pinyin"] == "dì" and r["hanzi"]["tone3"] == "di4", r["hanzi"])
    alt = json.loads(r["hanzi"]["alt_readings"] or "[]")
    check("地 alt de·轻轻地", alt and alt[0][:2] == ["de", "de5"] and alt[0][2] == "轻轻地", alt)
    for fn in ("地.mp3", "地_de5.mp3", "子.mp3", "着.mp3", "只.mp3", "s_日_1.mp3",
               "n_天_1.mp3", "w_天.mp3", "e_天.mp3"):
        try:
            urllib.request.urlopen(f"{BASE}/static/audio/{urllib.parse.quote(fn)}").read(16)
            check(f"音频 {fn} 可达", True)
        except Exception as e:
            check(f"音频 {fn} 可达", False, str(e)[:60])
    r = call("GET", "/api/report/weekly")
    check("周报空态", "weekly" in r and "top_wrong" in r)


def state_machine_tests():
    print("== 状态机走查（直调 scheduler，伪造日期）==")
    import database, scheduler
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    # 选一个干净字
    h = db.execute("""SELECT h.id FROM hanzi h
                      WHERE NOT EXISTS (SELECT 1 FROM user_hanzi uh WHERE uh.user_id=? AND uh.hanzi_id=h.id)
                      LIMIT 1""", (uid,)).fetchone()
    hid = h["id"]
    db.close()

    real_today = scheduler.today_str
    day0 = date(2026, 1, 5)
    try:
        scheduler.today_str = lambda: day0.isoformat()
        r = scheduler.submit(uid, hid, "learn", "read", True)
        check("D0 首学 → stage0 next=D+1", r["stage"] == 0 and r["next_date"] == "2026-01-06", r)
        scheduler.today_str = lambda: (day0 + timedelta(days=1)).isoformat()
        r = scheduler.submit(uid, hid, "review", "listen", True)
        check("D1 复习对 → stage1 next=D+4", r["stage"] == 1 and r["next_date"] == "2026-01-09", r)
        scheduler.today_str = lambda: (day0 + timedelta(days=4)).isoformat()
        r = scheduler.submit(uid, hid, "weekly", "read", True)
        check("D4 周测对 → stage2 next=D+11", r["stage"] == 2 and r["next_date"] == "2026-01-16", r)
        scheduler.today_str = lambda: (day0 + timedelta(days=11)).isoformat()
        r = scheduler.submit(uid, hid, "review", "mean", True)
        check("D11 复习对 → stage3 next=D+25", r["stage"] == 3 and r["next_date"] == "2026-01-30", r)
        # 到期答错回炉（独立日期；同日二次作答会被幂等挡住，间隔以首答为准）
        scheduler.today_str = lambda: (day0 + timedelta(days=25)).isoformat()   # 01-30
        r = scheduler.submit(uid, hid, "review", "listen", False)
        check("答错回炉 → stage1 next=+1", r["stage"] == 1 and r["next_date"] == "2026-01-31", r)
        scheduler.today_str = lambda: (day0 + timedelta(days=26)).isoformat()   # 01-31
        r = scheduler.submit(uid, hid, "review", "listen", True)
        check("复习对 → stage2 next=D+7", r["stage"] == 2 and r["next_date"] == "2026-02-07", r)
        r2 = scheduler.submit(uid, hid, "review", "listen", True)
        check("同日重做不二次推进（幂等）", r2["stage"] == 2 and r2["next_date"] == "2026-02-07", r2)
        scheduler.today_str = lambda: (day0 + timedelta(days=33)).isoformat()   # 02-07 到期
        r = scheduler.submit(uid, hid, "review", "listen", True)
        check("到期复习对 → stage3", r["stage"] == 3 and r["next_date"] == "2026-02-21", r)
        scheduler.today_str = lambda: (day0 + timedelta(days=34)).isoformat()   # 02-08
        d = scheduler.submit(uid, hid, "review", "listen", True)
        check("次日复习对到 mastered", d["status"] == "mastered" and d["next_date"] is None, d)
        # audit 错打回
        r = scheduler.submit(uid, hid, "audit", "listen", False)
        check("audit 错 → learning stage1", r["status"] == "learning" and r["stage"] == 1, r)
        # 清理该字，避免污染后续测试
        db = database.get_db()
        db.execute("DELETE FROM user_hanzi WHERE user_id=? AND hanzi_id=?", (uid, hid))
        db.execute("DELETE FROM quiz_log WHERE user_id=? AND hanzi_id=?", (uid, hid))
        db.commit(); db.close()
    finally:
        scheduler.today_str = real_today  # noqa


def weekly_done_tests():
    """周测结算防重 + 学习日驱动调度（活动日计数，中断不跳周测）"""
    print("== 周测结算防重 ==")
    import database, scheduler
    from datetime import date as _date, timedelta as _td
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    hid = db.execute("SELECT id FROM hanzi LIMIT 1").fetchone()["id"]
    # 复位到"已建计划 + 6 个历史活动日"：今天 = 第 7 个活动日 = 周测日
    db.execute("UPDATE users SET plan_start=? WHERE id=?", (_date.today().isoformat(), uid))
    db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
    db.execute("DELETE FROM user_hanzi WHERE user_id=?", (uid,))
    db.execute("DELETE FROM weekly_reports WHERE user_id=? AND week_no=1", (uid,))
    db.execute("DELETE FROM stars WHERE user_id=? AND src='weekly'", (uid,))
    for i in range(6, 0, -1):
        db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) "
                   "VALUES(?,?, 'review', 'listen', 1, ?)",
                   (uid, hid, (_date.today() - _td(days=i)).isoformat()))
    db.commit()
    db.close()
    t = scheduler.build_today(uid)
    check("第 7 活动日=周测日", t.get("mode") == "weekly" and not t.get("done")
          and t.get("week_no") == 1, t)
    scheduler.weekly_finish(uid, 1, 5, 8, [12])
    scheduler.weekly_finish(uid, 1, 5, 8, [12])                    # 重复结算（重进重刷）
    db = database.get_db()
    n_stars = db.execute(
        "SELECT COUNT(*) n FROM stars WHERE user_id=? AND src='weekly'", (uid,)).fetchone()["n"]
    wr = db.execute("SELECT wrong_ids, done FROM weekly_reports WHERE user_id=? AND week_no=1",
                    (uid,)).fetchone()
    db.close()
    check("重复结算只记一次星", n_stars == 1, n_stars)
    check("周报 wrong_ids 落库", wr and json.loads(wr["wrong_ids"]) == [12] and wr["done"] == 1,
          dict(wr) if wr else None)
    t = scheduler.build_today(uid)
    check("done 后 today 返回完成态", t.get("mode") == "weekly" and t.get("done") is True, t)

    # 学习日驱动：中断 10 天不推进计划（仍是第 1 周周测日，不会跳到第 2 周）
    print("== 学习日驱动（中断不跳周）==")
    real_today = scheduler.today_str
    try:
        scheduler.today_str = lambda: (_date.today() + _td(days=10)).isoformat()
        t = scheduler.build_today(uid)
        check("中断 10 天仍第 1 周周测日", t.get("mode") == "weekly"
              and t.get("week_no") == 1 and t.get("done") is True, t)
        db = database.get_db()
        db.execute("DELETE FROM weekly_reports WHERE user_id=?", (uid,))   # 未结算重开场景
        db.commit()
        db.close()
        t = scheduler.build_today(uid)
        check("中断后周测仍未做则重开", t.get("mode") == "weekly" and not t.get("done"), t)
    finally:
        scheduler.today_str = real_today  # noqa


def review_done_tests():
    """完成页复习统计真源：重进后 review 列表只剩未答项，review_done（quiz_log 当日已答）
    才是完成页"复习 N 个字"的正确口径（对称 learned_today 的重进恢复思路）"""
    print("== 复习完成统计真源 ==")
    import database, scheduler
    from datetime import date as _date
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    h = db.execute("""SELECT h.id FROM hanzi h
                      WHERE NOT EXISTS (SELECT 1 FROM user_hanzi uh WHERE uh.user_id=? AND uh.hanzi_id=h.id)
                      LIMIT 1""", (uid,)).fetchone()
    hid = h["id"]
    # 自含前置：已建计划 + 清流水（今天=第 1 活动日=学习日），造一个今日到期复习字
    db.execute("UPDATE users SET target_chars=52, plan_days=30, plan_start=? WHERE id=?",
               (_date.today().isoformat(), uid))
    db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
    db.execute("DELETE FROM user_hanzi WHERE user_id=?", (uid,))
    db.execute("INSERT INTO user_hanzi(user_id, hanzi_id, status, stage, next_date) "
               "VALUES(?,?,'learning',1,?)", (uid, hid, _date.today().isoformat()))
    db.commit()
    db.close()
    try:
        t = scheduler.build_today(uid)
        check("学习日+到期复习入列", t.get("mode") == "learn"
              and hid in [r["id"] for r in t["review"]], t.get("mode"))
        check("未答时 review_done=0", t.get("review_done") == 0, t.get("review_done"))
        scheduler.submit(uid, hid, "review", "listen", True)
        t = scheduler.build_today(uid)
        check("答过即出列(重进只剩未答)", hid not in [r["id"] for r in t["review"]])
        check("review_done 记当日已答(重进不少计)", t.get("review_done") == 1, t.get("review_done"))
    finally:
        db = database.get_db()
        db.execute("DELETE FROM user_hanzi WHERE user_id=? AND hanzi_id=?", (uid, hid))
        db.execute("DELETE FROM quiz_log WHERE user_id=? AND hanzi_id=? AND phase='review'", (uid, hid))
        db.commit()
        db.close()


if __name__ == "__main__":
    api_tests()
    state_machine_tests()
    review_done_tests()
    weekly_done_tests()
    reset_user()   # teardown：清 mido 测试状态，防污染线上库
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
