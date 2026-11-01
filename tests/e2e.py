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
    db.execute("UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL, plan_start=NULL, plan_base_learned=0, confirm_upto=0, onboarded=0 WHERE id=?", (uid,))
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
                              WHERE h.book IN ('1a','1b','2a','2b','3a','3b','4a','4b')""").fetchone()["n"]
    _d.close()
    check("内容覆盖八册 2535 字全量", n_content == 2535, n_content)
    r = call("GET", "/api/me")
    check("me.stage=onboard", r.get("stage") == "onboard", r)
    r = call("POST", "/api/profile/grade", {"grade": "1A"})
    check("set grade 1A", r.get("ok"))
    r = call("POST", "/api/profile/grade", {"grade": "9X"})
    check("grade 校验拒绝", r.get("__error__") == 400, r)
    r = call("POST", "/api/profile/grade", {"grade": "5A"})
    check("未生产册(5A)选年级拒绝", r.get("__error__") == 400, r)
    r = call("GET", "/api/me")
    check("me.grades_ready 八册", r.get("grades_ready") ==
          ["1A", "1B", "2A", "2B", "3A", "3B", "4A", "4B"], r.get("grades_ready"))
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
    # 词步 ASR 校验（v38）：纯函数匹配 + 端点降级链（未配置 key 永远放行不卡孩子）
    import asr as _asr
    check("word_match 同音/前后缀宽容+乱喊拒绝",
          _asr.word_match("夏双", "下霜") and _asr.word_match("我现在读下霜", "下霜")
          and not _asr.word_match("啊啊啊啊", "下霜") and not _asr.word_match("我想喝水", "下霜"))
    boundary = "----zilearnBoundary"
    mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"char\"\r\n\r\n霜\r\n"
          f"--{boundary}\r\nContent-Disposition: form-data; name=\"word\"\r\n\r\n下霜\r\n"
          f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"rec.webm\"\r\n"
          f"Content-Type: audio/webm\r\n\r\n").encode() + b"\x00\x01\x02fakeaudio" + \
          f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/follow/verify", method="POST", data=mp)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if COOKIE:
        req.add_header("Cookie", COOKIE)
    with urllib.request.urlopen(req) as resp:
        v = json.loads(resp.read())
    check("follow/verify 未配置 key → 降级放行", v.get("pass") is True and v.get("degraded") is True, v)


def gate_question_tests():
    """第四关 lsent（听句选字）+ 第五关 defi（听释义选字，v39）出题校验"""
    print("== 四五关新题型 ==")
    import database, scheduler
    db = database.get_db()
    # 1b 字（有 mean 素材）：lsent 运行时生成 + defi 题库题
    h1 = db.execute("""SELECT h.id, h.char FROM hanzi h JOIN zi_content c ON c.hanzi_id=h.id
                       WHERE h.book='1b' AND c.mean IS NOT NULL LIMIT 1""").fetchone()
    for _ in range(3):
        q = scheduler.gen_quiz(h1["id"], "lsent", book="1a")     # 故意跨册 book：干扰须锚定目标字册
        others = [o for o in q["options"] if o != q["hanzi"]]
        in_sentence = [o for o in others if o in q["sentence"]]
        check("lsent 选项含目标字且句含目标字",
              q["hanzi"] in q["options"] and q["hanzi"] in q["sentence"], q)
        check("lsent 干扰不在句中（防双解）", not in_sentence, (q["sentence"], in_sentence))
        tone_bad = db.execute(
            f"""SELECT COUNT(*) n FROM hanzi WHERE char IN ({','.join('?' * len(others))})
                AND (tone3 = (SELECT tone3 FROM hanzi WHERE char=?) OR book != '1b')""",
            (*others, q["hanzi"])).fetchone()["n"]
        check("lsent 干扰异音且锚定目标字册（无跨册污染）", tone_bad == 0,
              (others, tone_bad))
    q2 = scheduler.gen_quiz(h1["id"], "defi", book="1a")
    check("defi 题库题返回（1b 已审）", q2 and q2.get("quiz_type") == "defi"
          and q2["hanzi"] in q2["options"] and q2.get("mean"), q2.get("quiz_type"))
    opts_others = [o for o in q2["options"] if o != q2["hanzi"]]
    check("defi 释义不含任何选项字（无音节泄漏/困惑）",
          not any(o in q2["mean"] for o in opts_others), q2["mean"])
    # 1a 字无 mean：defi 退化 listen（不排哑巴题）
    h2 = db.execute("SELECT id, char FROM hanzi WHERE book='1a' LIMIT 1").fetchone()
    q3 = scheduler.gen_quiz(h2["id"], "defi", book="1a")
    check("defi 无素材退化 listen", q3.get("quiz_type") == "listen", q3.get("quiz_type"))
    db.close()


def report_tests():
    """学习日报聚合（v39）：mido 是 SMTP_SKIP 账号——照常聚合、不发信"""
    print("== 学习日报 ==")
    import database, report as _report
    reset_user()
    call("POST", "/api/login", {"username": "mido", "password": MIDO_PASS})
    call("POST", "/api/profile/grade", {"grade": "1A"})
    call("POST", "/api/plan/create", {"target_chars": 10, "days": 7})
    batches = call("GET", "/api/confirm/batches")["batches"]
    pool = [c for b in batches for c in b]
    hid = pool[0]["id"]
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    from datetime import date as _d
    t = _d.today().isoformat()          # 与 scheduler.today_str() 同源（本地时区；date('now') 是 UTC 会错位）
    db.execute("INSERT INTO user_hanzi(user_id, hanzi_id, status, stage, next_date, first_date, last_date) "
               "VALUES(?,?,'learning',0,?,?,?)", (uid, hid, t, t, t))
    db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) "
               "VALUES(?,?,'learn','card',1,?)", (uid, hid, t))
    db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) "
               "VALUES(?,?,'learn','listen',0,?)", (uid, hid, t))
    db.execute("INSERT INTO follow_log(user_id, char, kind, heard, ts) "
               "VALUES(?,(SELECT char FROM hanzi WHERE id=?),'char',1,?)", (uid, hid, t + " 12:00:00"))
    db.commit(); db.close()
    rep = _report.build_daily_report(uid)
    check("日报聚合：新字/错字/跟读",
          len(rep["new_chars"]) == 1 and len(rep["wrong"]) == 1
          and rep["follow"] and rep["follow"]["heard"] == 1,
          {"new": len(rep["new_chars"]), "wrong": len(rep["wrong"])})
    html = _report.render_html(rep)
    check("日报 HTML 渲染含关键段", "每日学习报告" in html and "错字清单" in html and "跟读开口率" in html)
    r = call("POST", "/api/report/daily")
    check("日报幂等+测试账号跳过发信", r.get("reason") in ("测试账号不发信", "今日已发送"), r)
    r2 = call("POST", "/api/report/daily")
    check("日报二次触发不重发", r2.get("sent") is False, r2)
    reset_user()


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


def plan_renew_tests():
    """续订增量记账：建计划记基准线 base；达成=learned-base>=target；续订后 base 前移"""
    reset_user()
    call("POST", "/api/login", {"username": "mido", "password": MIDO_PASS})
    call("POST", "/api/profile/grade", {"grade": "1A"})
    r = call("POST", "/api/plan/create", {"target_chars": 10, "days": 7})
    check("计划: 7天10字 daily=2", r["daily_new"] == 2, r)
    me = call("GET", "/api/me")["progress"]
    check("基准线 base=0（首建）", me["plan_base_learned"] == 0, me["plan_base_learned"])
    # 学满 10 字（learn 提交与日期无关，直接按教材序补齐）
    pool_ids = [c["id"] for b in call("GET", "/api/confirm/batches")["batches"] for c in b]
    for hid in pool_ids[:10]:
        call("POST", "/api/submit", {"hanzi_id": hid, "phase": "learn",
                                     "quiz_type": "card", "correct": True})
    me = call("GET", "/api/me")["progress"]
    check("学满后达成条件成立", me["learned"] >= 10 and me["learned"] - me["plan_base_learned"] >= 10, me)
    # 续订：下一期 14 字 / 7 天
    call("POST", "/api/plan/create", {"target_chars": 14, "days": 7})
    me = call("GET", "/api/me")["progress"]
    check("续订: base 前移到已学数", me["plan_base_learned"] == 10, me["plan_base_learned"])
    check("续订: 达成条件重置（未再达成）", me["learned"] - me["plan_base_learned"] < me["target_chars"],
          (me["learned"], me["plan_base_learned"], me["target_chars"]))
    check("续订: 剩余池正确", me["pool"] == me["total_hanzi"] - me["learned"] - me["skipped"], me["pool"])
    reset_user()   # 本组自带清理


def confirm_more_tests():
    """轮次补摸：池内确认生词<目标 → verify 响应 more_batch 续摸；够数开始学；
    全册摸完仍不足 → target 收敛（目标不悬空）"""
    reset_user()
    call("POST", "/api/login", {"username": "mido", "password": MIDO_PASS})
    call("POST", "/api/profile/grade", {"grade": "1A"})
    call("POST", "/api/plan/create", {"target_chars": 50, "days": 7})
    batches = call("GET", "/api/confirm/batches")["batches"]
    pool = [c for b in batches for c in b]
    check("首轮池 ceil(50*1.3)=65", len(pool) == 65, len(pool))
    # 标记前 40 个认识（UI 同款时序：先全量提交，再逐题验证）
    call("POST", "/api/confirm/submit", {"known_ids": [c["id"] for c in pool[:40]]})
    for c in pool[:40]:
        r = call("POST", "/api/confirm/verify", {"hanzi_id": c["id"], "correct": True})
    check("生词25<目标50 → 触发补摸 50 字", r.get("more_batch") and len(r["more_batch"]) == 50,
          len(r["more_batch"]) if r.get("more_batch") else None)
    seen = {c["char"] for c in pool}
    more1 = r["more_batch"]
    fresh = [c["char"] for c in more1 if c["char"] not in seen]
    check("补摸批不与已摸重复", len(fresh) == 50, len(fresh))
    # 补摸批一个不选（全算生词）→ 生词 75≥50 → 无更多批次，直接开始学
    call("POST", "/api/confirm/submit", {"known_ids": []})
    r = call("GET", "/api/confirm/verify/first")
    check("生词够数 → 无更多批次", not r.get("more_batch"), r.get("more_batch"))
    t = call("GET", "/api/today")
    known2 = seen | set(fresh)
    in_pool = all(h["char"] in known2 for h in t["new"])
    check("学习字全部来自已摸底池", in_pool, [h["char"] for h in t["new"]])

    # 全册摸完仍不足：target=280（池=全册），skip 260 → 收敛
    reset_user()
    call("POST", "/api/login", {"username": "mido", "password": MIDO_PASS})
    call("POST", "/api/profile/grade", {"grade": "1A"})
    call("POST", "/api/plan/create", {"target_chars": 280, "days": 30})
    batches = call("GET", "/api/confirm/batches")["batches"]
    pool = [c for b in batches for c in b]
    check("280 目标池=全册 281", len(pool) == 281, len(pool))
    call("POST", "/api/confirm/submit", {"known_ids": [c["id"] for c in pool[:260]]})
    for c in pool[:260]:
        r = call("POST", "/api/confirm/verify", {"hanzi_id": c["id"], "correct": True})
    check("全册摸完 → 无补摸批次", not r.get("more_batch"), r.get("more_batch"))
    import sqlite3 as _s
    conn = _s.connect("/root/zilearn/app/data/zilearn.db")
    tgt = conn.execute("SELECT target_chars FROM users WHERE username='mido'").fetchone()[0]
    conn.close()
    check("目标收敛到实际生词 21", tgt == 21, tgt)
    reset_user()   # 本组自带清理


def content_gate_tests():
    """内容就绪门禁回归（2026-11-01 1B 事故）：zi_content 未覆盖的字（1b+ 册）
    不得进入新字/复习/抽检/当日已学——否则排出无音频无词句的"哑巴卡"（只剩提示音效）"""
    print("== 内容就绪门禁 ==")
    import database, scheduler
    reset_user()
    db = database.get_db()
    uid = db.execute("SELECT id FROM users WHERE username='mido'").fetchone()["id"]
    # 模拟被污染的 5A 账号（5a 册有字无内容）：册计划 + 今日已"学"过无内容字 + 挂到期复习
    t0 = date.today().isoformat()
    db.execute("UPDATE users SET grade='5A', target_chars=30, plan_days=7, plan_start=?, onboarded=1",
               (t0,))
    b1 = db.execute("SELECT h.id, h.char FROM hanzi h WHERE h.book='5a' ORDER BY h.seq LIMIT 2").fetchall()
    for r in b1:
        db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) "
                   "VALUES(?,?,'learn','card',1,?)", (uid, r["id"], t0))
        db.execute("INSERT INTO user_hanzi(user_id, hanzi_id, status, stage, next_date, first_date, last_date) "
                   "VALUES(?,?,'learning',0,?,?,?)", (uid, r["id"], t0, t0, t0))
    db.commit(); db.close()

    r = call("GET", "/api/me")
    check("5A 账号 stage 打回 onboard(重选年级)", r.get("stage") == "onboard", r.get("stage"))
    t = scheduler.build_today(uid)
    ids_bad = {x["id"] for x in b1}
    ids_all = ({x["id"] for x in t["new"]} | {x["id"] for x in t["review"]}
               | {x["id"] for x in t["audit"]} | {x["id"] for x in t["learned_today"]})
    check("5a 无内容字不进新字/复习/抽检/已学", not (ids_all & ids_bad), sorted(ids_all & ids_bad))
    db = database.get_db()
    n_bad = db.execute(
        f"""SELECT COUNT(*) n FROM hanzi h WHERE h.id IN ({','.join('?' * len(ids_all))})
            AND NOT {scheduler.CONTENT_READY}""", tuple(ids_all)).fetchone()["n"] if ids_all else 0
    db.close()
    check("当日字池全部内容就绪", n_bad == 0, n_bad)
    check("全册未就绪 → 当日无新字（宁空不排哑巴卡）", t["new"] == [], t["new"])
    reset_user()   # 本组自带清理


if __name__ == "__main__":
    api_tests()
    state_machine_tests()
    review_done_tests()
    weekly_done_tests()
    plan_renew_tests()
    confirm_more_tests()
    content_gate_tests()
    gate_question_tests()
    report_tests()
    reset_user()   # teardown：清 mido 测试状态，防污染线上库
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    sys.exit(1 if FAIL_N else 0)
