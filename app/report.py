"""学习日报邮件（wordslearn report.py 同模式）：完成当日学习 → 聚合 → HTML → SMTP。

触发：前端 finishSession → POST /api/report/daily（learn/weekly 完成都触发）。
幂等：daily_mail(user_id, date) 唯一，发送成功才置 sent=1；SMTP 未配置/失败可重试。
配置：app/data/smtp.json（gitignored）：
  {"host": "smtp.126.com", "port": 465, "user": "...", "pass": "授权码",
   "to": "a@x.com,b@x.com"}        ← to 逗号分隔即多收件人（家长双方）
无学习不送；test/mido 测试账号不发。
"""
import json
import smtplib
from datetime import date, timedelta
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from database import get_db, DB_PATH

SMTP_CONF = DB_PATH.parent / "smtp.json"
SMTP_SKIP_USERS = ("mido",)                   # 仅 e2e 账号不发信（test 是孩子真机账号，必须发！）
QT_LABEL = {"listen": "听音题", "pin": "拼音题", "mean": "组词题", "sent": "句子题",
            "find": "找一找", "rev": "听词题", "lsent": "听句题", "defi": "字义题",
            "stroke": "写字", "card": "新字卡"}
PHASE_LABEL = {"learn": "新学", "review": "复习", "weekly": "周测", "audit": "抽检"}


def _today() -> str:
    return date.today().isoformat()


def _mean_of(db, hanzi_id):
    r = db.execute("""SELECT c.mean, c.words, h.char FROM hanzi h
                      LEFT JOIN zi_content c ON c.hanzi_id=h.id WHERE h.id=?""",
                   (hanzi_id,)).fetchone()
    if not r:
        return "", ""
    if r["mean"]:
        return r["mean"], r["char"]
    try:
        w = json.loads(r["words"]) if r["words"] else []
    except ValueError:
        w = []
    return (f"组词：{'、'.join(w[:2])}" if w else ""), r["char"]


def build_daily_report(user_id, day=None):
    db = get_db()
    try:
        day = day or _today()
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        uname = (user["display_name"] or user["username"]) if user else "?"
        qrows = db.execute("""SELECT q.*, h.char FROM quiz_log q JOIN hanzi h ON h.id=q.hanzi_id
                              WHERE q.user_id=? AND q.ts=? ORDER BY q.id""",
                           (user_id, day)).fetchall()
        new_chars = [dict(r) for r in db.execute(
            """SELECT DISTINCT h.id, h.char, h.pinyin, c.mean, c.words FROM user_hanzi uh
               JOIN hanzi h ON h.id=uh.hanzi_id LEFT JOIN zi_content c ON c.hanzi_id=h.id
               WHERE uh.user_id=? AND uh.first_date=? AND uh.status!='skipped' ORDER BY h.seq""",
            (user_id, day))]
        # 首答口径：card（新字卡提交）不算题；同字同题型重做（重进）只记首条。
        # 同字当日多题是常态（六关每关一题），去重键必须是 字×phase×题型
        first, seen = [], set()
        for r in qrows:
            if r["quiz_type"] == "card":
                continue
            k = (r["hanzi_id"], r["phase"], r["quiz_type"])
            if k in seen:
                continue
            seen.add(k)
            first.append(r)
        n_first = len(first)
        ok_first = sum(r["correct"] for r in first)
        # 分轮表现（phase × 题型聚合）
        rounds = {}
        for r in first:
            if r["quiz_type"] == "card":
                continue
            key = f"{PHASE_LABEL.get(r['phase'], r['phase'])}·{QT_LABEL.get(r['quiz_type'], r['quiz_type'])}"
            s = rounds.setdefault(key, {"n": 0, "ok": 0})
            s["n"] += 1
            s["ok"] += r["correct"]
        # 错字清单（附释义，家长辅导用）
        wrong = {}
        for r in first:
            if r["correct"]:
                continue
            mean, ch = _mean_of(db, r["hanzi_id"])
            wrong.setdefault(ch, {"mean": mean, "rounds": set()})
            wrong[ch]["rounds"].add(PHASE_LABEL.get(r["phase"], r["phase"]))
        # 跟读开口（当日）：heard=读到 / timeout / na
        fl = db.execute("""SELECT COUNT(*) n, COALESCE(SUM(heard=1),0) heard,
                                  COALESCE(SUM(heard=0),0) timeout,
                                  COALESCE(SUM(heard IS NULL),0) na
                           FROM follow_log WHERE user_id=? AND ts>=?""", (user_id, day)).fetchone()
        follow = dict(fl) if fl["n"] else None
        # 近 7 日趋势
        days = [(date.fromisoformat(day) - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        trend = []
        for d in days:
            q = db.execute("""SELECT COUNT(*) n, SUM(correct) ok FROM quiz_log
                              WHERE user_id=? AND ts=? AND quiz_type!='card'""", (user_id, d)).fetchone()
            nw = db.execute("""SELECT COUNT(*) n FROM user_hanzi
                               WHERE user_id=? AND first_date=? AND status!='skipped'""",
                            (user_id, d)).fetchone()["n"]
            trend.append({"date": d, "new": nw, "n": q["n"],
                          "acc": round(q["ok"] / q["n"] * 100) if q["n"] else None})
        # 连续学习天数（当日有学则从当日起算）
        streak, d = 0, date.fromisoformat(day)
        while True:
            r = db.execute("SELECT 1 FROM quiz_log WHERE user_id=? AND ts=? LIMIT 1",
                           (user_id, d.isoformat())).fetchone()
            if r:
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        # 家长建议（规则化，可执行）
        tips = []
        if wrong:
            tips.append(f"今日错字 {len(wrong)} 个，睡前口头过一遍效果最好：" + "、".join(list(wrong)[:8]))
        weak = [(k, s) for k, s in rounds.items() if s["n"] >= 2 and s["ok"] / s["n"] < 0.7]
        if weak:
            k, s = sorted(weak, key=lambda x: x[1]["ok"] / x[1]["n"])[0]
            tips.append(f"「{k.split('·')[-1]}」较弱（{s['ok']}/{s['n']}），明天重点陪一陪这类题")
        if follow and follow["n"] >= 3 and follow["heard"] / follow["n"] < 0.5:
            tips.append("跟读开口率偏低：检查麦克风权限，或陪读时先示范一遍再让孩子跟")
        if not tips:
            tips.append("全部顺利——今天可以只夸不加压，保持节奏最重要")
        return {"user": uname, "date": day, "streak": streak, "n_first": n_first,
                "ok_first": ok_first, "rounds": rounds, "new_chars": new_chars,
                "wrong": [{"char": c, "mean": v["mean"], "rounds": "、".join(v["rounds"])}
                          for c, v in sorted(wrong.items())],
                "follow": follow, "trend": trend, "tips": tips}
    finally:
        db.close()


def render_html(rep):
    def acc(a):
        return f"{a}%" if a is not None else "—"

    rounds_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center'>{s['n']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;font-weight:600;"
        f"color:{'#34b37a' if s['ok']/s['n'] >= .8 else '#f5a623' if s['ok']/s['n'] >= .6 else '#e05252'}'>"
        f"{round(s['ok']/s['n']*100)}%</td></tr>"
        for k, s in sorted(rep["rounds"].items())) or "<tr><td colspan='3' style='padding:10px;color:#999'>今天没有答题</td></tr>"
    new_html = "、".join(
        f"<b>{c['char']}</b>({(c['mean'] or '')[:14]})" if c.get("mean")
        else f"<b>{c['char']}</b>({c['pinyin']})" for c in rep["new_chars"]) or "—"
    wrong_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'><b>{w['char']}</b></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{w['mean']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{w['rounds']}</td></tr>"
        for w in rep["wrong"]) or "<tr><td colspan='3' style='padding:10px;color:#999'>今天没有错字 🎉</td></tr>"
    trend_html = "".join(
        f"<td style='padding:6px 4px;text-align:center'>{t['date'][5:]}<br><b>{t['new']}</b>字<br>{acc(t['acc'])}</td>"
        for t in rep["trend"])
    tips_html = "".join(f"<li style='margin:4px 0'>{t}</li>" for t in rep["tips"])
    follow = rep["follow"]
    follow_html = (f"<b>{round(follow['heard']/follow['n']*100)}%</b>"
                   f"<span style='font-size:12px;color:#888'>（读到 {follow['heard']}/{follow['n']}"
                   f"·超时 {follow['timeout']}·未开麦 {follow['na']}）</span>") if follow else "今天没有跟读"
    total_acc = round(rep["ok_first"] / rep["n_first"] * 100) if rep["n_first"] else None
    return f"""<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:640px;margin:0 auto;color:#2a2f3a">
<div style="background:linear-gradient(135deg,#ff9800,#ffb74d);border-radius:12px;padding:18px 20px;color:#fff">
<div style="font-size:18px;font-weight:700">字乐 · 每日学习报告</div>
<div style="font-size:14px;margin-top:6px;opacity:.95">{rep['user']} · {rep['date']} · 连续学习 {rep['streak']} 天 🔥</div></div>
<table style="width:100%;border-collapse:collapse;margin:14px 0;background:#fff;border-radius:10px">
<tr><td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{len(rep['new_chars'])}</b><br><span style="font-size:12px;color:#888">新学字</span></td>
<td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{rep['n_first']}</b><br><span style="font-size:12px;color:#888">答题数</span></td>
<td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{acc(total_acc)}</b><br><span style="font-size:12px;color:#888">首答正确率</span></td>
<td style="padding:12px 10px;text-align:center">{follow_html}<br><span style="font-size:12px;color:#888">跟读开口率</span></td></tr></table>
<h3 style="font-size:15px;margin:16px 0 6px">✨ 今日新字（{len(rep['new_chars'])} 个）</h3>
<div style="font-size:13px;line-height:1.9;background:#fff;border-radius:10px;padding:10px 14px">{new_html}</div>
<h3 style="font-size:15px;margin:16px 0 6px">📊 各类题表现（首答正确率）</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:13px">
<tr style="color:#888;font-size:12px"><th style="padding:8px 10px;text-align:left">题型</th><th>题数</th><th>正确率</th></tr>{rounds_html}</table>
<h3 style="font-size:15px;margin:16px 0 6px">🔍 错字清单（今晚重点）</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:13px">
<tr style="color:#888;font-size:12px"><th style="padding:8px 10px;text-align:left">字</th><th style="text-align:left">意思</th><th style="text-align:left">错在</th></tr>{wrong_html}</table>
<h3 style="font-size:15px;margin:16px 0 6px">📈 近 7 日</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:12px"><tr>{trend_html}</tr></table>
<h3 style="font-size:15px;margin:16px 0 6px">💡 给家长的建议</h3>
<ul style="font-size:13px;line-height:1.8;background:#fff8e1;border-radius:10px;padding:12px 14px 12px 30px;margin:0">{tips_html}</ul>
<div style="text-align:center;color:#aaa;font-size:11px;margin-top:16px">字乐 zilearn · 每日完成学习后自动发送</div></div>"""


def _smtp_conf():
    if not SMTP_CONF.exists():
        return None
    try:
        cfg = json.loads(SMTP_CONF.read_text())
    except (ValueError, OSError):
        return None
    if not (cfg.get("host") and cfg.get("user") and cfg.get("pass")):
        return None
    return cfg


def send_daily_report(user_id):
    """完成事件入口：当日无学习不送；已送幂等跳过；失败不置位可重试。"""
    day = _today()
    db = get_db()
    try:
        user = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"sent": False, "reason": "用户不存在"}
        uname = user["username"]
        n_learn = db.execute("""SELECT COUNT(*) n FROM quiz_log WHERE user_id=? AND ts=?""",
                             (user_id, day)).fetchone()["n"]
        if not n_learn:
            return {"sent": False, "reason": "当日无学习记录，不发送"}
        sent = db.execute("SELECT sent FROM daily_mail WHERE user_id=? AND date=?",
                          (user_id, day)).fetchone()
    finally:
        db.close()
    if sent and sent["sent"]:
        return {"sent": False, "reason": "今日已发送"}
    rep = build_daily_report(user_id, day)
    if uname in SMTP_SKIP_USERS:
        _mark_sent(user_id, day, sent_ok=False)          # 记录但不发（测试账号）
        return {"sent": False, "reason": "测试账号不发信", "summary": {"new": len(rep["new_chars"])}}
    cfg = _smtp_conf()
    if not cfg:
        print(f"[report] SMTP 未配置（app/data/smtp.json），{uname} {day} 日报未发送", flush=True)
        return {"sent": False, "reason": "SMTP 未配置"}
    subject = (f"【字乐】{rep['user']} {day[5:]} 学习日报 · "
               f"新学{len(rep['new_chars'])}字 错{len(rep['wrong'])} 🔥{rep['streak']}天")
    try:
        msg = MIMEText(render_html(rep), "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = f"字乐 <{cfg['user']}>"
        to_list = [x.strip() for x in str(cfg.get("to") or cfg["user"]).split(",") if x.strip()]
        msg["To"] = ",".join(to_list)
        with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), timeout=15) as s:
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(cfg["user"], to_list, msg.as_string())
    except Exception as e:
        print(f"[report] 日报发送失败（{uname} {day}）: {e}", flush=True)
        return {"sent": False, "reason": f"发送失败: {e}"}
    _mark_sent(user_id, day, sent_ok=True)
    return {"sent": True, "reason": "ok"}


def _mark_sent(user_id, day, sent_ok):
    db = get_db()
    try:
        db.execute("""INSERT INTO daily_mail(user_id, date, sent) VALUES(?,?,?)
                      ON CONFLICT(user_id, date) DO UPDATE SET sent=?""",
                   (user_id, day, 1 if sent_ok else 0, 1 if sent_ok else 0))
        db.commit()
    finally:
        db.close()
