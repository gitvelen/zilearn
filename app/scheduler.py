"""调度核心 — zilearn

周循环 + 日日复习（PRD §4）：
- 间隔序列 INTERVALS=[1,3,7,14]：首学 D0 → 复习1(D+1) → 复习2(D+4) → 复习3(D+11) → 复习4(D+25) → mastered
  （间隔从每次复习当日起算；扩张比≈3）
- 每周 = 6 学习日 + 1 周测日（不投新字）；活动日驱动：中断不推进计划、周测日顺延到
  下次打开（活动日 = quiz_log 有流水的自然日，day_index=今天之前活动日数）
- 周测 = 批量复习事件：每题按 phase='weekly' 提交，对→按当前 stage 正常推进，错→回炉 D+1；
  周测范围 = 本周新字 + 本周答错字 + 当日到期复习（一锅端，同字不重复）
- 复习答错：stage 重置 1、next=D+1 完整回炉
- mastered 每日抽检 3 个（audit）混入复习；答错打回 stage1
- 摸底三步（表格确认）：速选认识 → 1 题四选一验证（对→skipped 剔除，错→回池）→ 建计划按教材序续补
"""
import json
import math
import random
from datetime import date
from pathlib import Path

from database import get_db

INTERVALS = [1, 3, 7, 14]     # stage0→1 / 1→2 / 2→3 / 3→mastered 的间隔天数
AUDIT_N = 3                   # 掌握词每日抽检数
CONFIRM_BATCH = 50            # 摸底速选表格每批字数
MIN_PLAN_DAYS = 7             # 计划天数下限（一个完整周循环）

today_str = lambda: date.today().isoformat()  # noqa: E731


def book_of(user):
    """用户年级 → 字表册（grade '1A' → '1a'）；无年级默认 1a"""
    g = user["grade"] or "1A"
    return g[0].lower() + g[1].lower()


def _d(s):
    return date.fromisoformat(s)


# ---------- 引导流：年级 → 计划 → 摸底 ----------

def create_plan(user_id, target_chars, days):
    """生成计划：池 = 全表 - skipped - 已学；返回预估每日用时（分钟）。
    目标为本期增量（续订场景）：达成判定 learned - plan_base_learned >= target，
    故建计划时记基准线。"""
    db = get_db()
    try:
        days = max(int(days), MIN_PLAN_DAYS)
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        book = book_of(user)
        pool = db.execute("""SELECT COUNT(*) n FROM hanzi h
            WHERE h.book=? AND NOT EXISTS (SELECT 1 FROM user_hanzi uh
                              WHERE uh.user_id=? AND uh.hanzi_id=h.id
                                AND uh.status IN ('skipped','learning','mastered'))""",
                          (book, user_id)).fetchone()["n"]
        base = db.execute("""SELECT COUNT(*) n FROM user_hanzi
                             WHERE user_id=? AND status IN ('learning','mastered')""",
                          (user_id,)).fetchone()["n"]
        target = min(int(target_chars), pool)
        study_days = days - days // 7            # 每周 1 天周测不投新字
        daily_new = max(1, math.ceil(target / study_days))
        db.execute("""UPDATE users SET target_chars=?, plan_days=?, plan_start=?,
                      plan_base_learned=?, onboarded=1 WHERE id=?""",
                   (target, days, today_str(), base, user_id))
        db.commit()
        # 预估：新字 2.5min + 稳态复习约 4×daily_new 题 ×0.2min + 昨日重现 3min
        est = daily_new * 2.5 + daily_new * 4 * 0.2 + 3
        return {"target": target, "days": days, "daily_new": daily_new,
                "est_minutes": round(est, 1), "over_30min": est > 30}
    finally:
        db.close()


def confirm_batches(user):
    """摸底速选：本册字表按教材 seq 分批。"""
    db = get_db()
    try:
        rows = db.execute("SELECT id, char, pinyin, unit, seq FROM hanzi WHERE book=? ORDER BY seq",
                          (book_of(user),)).fetchall()
        out = []
        for i in range(0, len(rows), CONFIRM_BATCH):
            out.append([dict(r) for r in rows[i:i + CONFIRM_BATCH]])
        return out
    finally:
        db.close()


def confirm_submit(user_id, known_ids):
    """速选认识的字 → 待验证。"""
    db = get_db()
    try:
        db.execute("DELETE FROM confirm_pick WHERE user_id=? AND result IS NULL", (user_id,))
        for hid in known_ids:
            db.execute("INSERT OR IGNORE INTO confirm_pick(user_id, hanzi_id) VALUES(?,?)",
                       (user_id, hid))
        db.commit()
        return {"pending": len(known_ids)}
    finally:
        db.close()


def confirm_verify(user_id, hanzi_id, correct):
    """验证 1 题：对 → 整字 skipped 剔除；错 → 回池（标记 fail，不剔除）。"""
    db = get_db()
    try:
        db.execute("UPDATE confirm_pick SET result=? WHERE user_id=? AND hanzi_id=?",
                   ("pass" if correct else "fail", user_id, hanzi_id))
        if correct:
            db.execute("""INSERT INTO user_hanzi(user_id, hanzi_id, status) VALUES(?,?, 'skipped')
                          ON CONFLICT(user_id, hanzi_id) DO UPDATE SET status='skipped'""",
                       (user_id, hanzi_id))
        db.commit()
    finally:
        db.close()


def confirm_progress(user_id):
    db = get_db()
    try:
        pend = db.execute("SELECT COUNT(*) n FROM confirm_pick WHERE user_id=? AND result IS NULL",
                          (user_id,)).fetchone()["n"]
        passed = db.execute("SELECT COUNT(*) n FROM confirm_pick WHERE user_id=? AND result='pass'",
                            (user_id,)).fetchone()["n"]
        return {"pending": pend, "passed": passed}
    finally:
        db.close()


# ---------- 每日队列 ----------

def _active_days(db, user_id):
    """历史活动日日期升序（quiz_log 有流水的自然日去重）。"""
    return [r["ts"] for r in db.execute(
        "SELECT DISTINCT ts FROM quiz_log WHERE user_id=? ORDER BY ts", (user_id,))]


def day_index(user, db=None):
    """今日角色（0 起）= 今天之前的活动日数。学习日驱动：中断不推进、周测日顺延；
    无计划返回 None。"""
    if not user["plan_start"]:
        return None
    own = db is not None
    db = db or get_db()
    try:
        t = today_str()
        return sum(1 for d in _active_days(db, user["id"]) if d < t)
    finally:
        if not own:
            db.close()


def is_weekly_day(user, db=None):
    di = day_index(user, db)
    return di is not None and (di + 1) % 7 == 0


def build_today(user_id):
    """当日任务包：learn-day | weekly-day。"""
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        di = day_index(user, db)
        if di is None:
            return {"mode": "no-plan"}

        if is_weekly_day(user, db):
            week_no = (di + 1) // 7
            # 已结算的周测不重开（防退出重进无限刷星/重复推进状态机）
            done = _row(db, """SELECT score, total FROM weekly_reports
                                WHERE user_id=? AND week_no=? AND done=1""",
                        (user_id, week_no))
            if done:
                return {"mode": "weekly", "week_no": week_no, "scope": [], "done": True,
                        "score": done["score"], "total": done["total"]}
            # 本周窗口 = 本周首个活动日（第 (week_no-1)*7+1 个）的日期
            active = _active_days(db, user_id)
            week_start = active[(week_no - 1) * 7]
            scope = {}
            # 本周新字
            for r in db.execute("""SELECT h.id, h.char FROM user_hanzi uh JOIN hanzi h ON h.id=uh.hanzi_id
                                   WHERE uh.user_id=? AND uh.first_date>=? AND uh.status!='skipped'""",
                                (user_id, week_start)):
                scope[r["id"]] = dict(r)
            # 本周答错过的字（含前几周的旧字本周答错）
            for r in db.execute("""SELECT DISTINCT h.id, h.char FROM quiz_log q JOIN hanzi h ON h.id=q.hanzi_id
                                   WHERE q.user_id=? AND q.correct=0 AND q.ts>=?""",
                                (user_id, week_start)):
                scope[r["id"]] = dict(r)
            # 当日到期复习（并入，避免同字重复出现）
            for r in db.execute("""SELECT h.id, h.char FROM user_hanzi uh JOIN hanzi h ON h.id=uh.hanzi_id
                                   WHERE uh.user_id=? AND uh.status='learning' AND uh.next_date<=?""",
                                (user_id, today_str())):
                scope[r["id"]] = dict(r)
            return {"mode": "weekly", "week_no": week_no, "scope": list(scope.values())}

        # ---- 学习日 ----
        daily_new = math.ceil(user["target_chars"] /
                              (user["plan_days"] - user["plan_days"] // 7))
        # 新字：按 seq 补齐（幂等：当日已 learn 的计入 daily_new）
        learned_today = [r["hanzi_id"] for r in db.execute(
            "SELECT DISTINCT hanzi_id FROM quiz_log WHERE user_id=? AND phase='learn' AND ts>=?",
            (user_id, today_str()))]
        new_ids = list(learned_today)
        if len(new_ids) < daily_new:
            for r in db.execute("""SELECT id, char FROM hanzi h
                WHERE h.book=? AND NOT EXISTS (SELECT 1 FROM user_hanzi uh WHERE uh.user_id=? AND uh.hanzi_id=h.id
                                  AND uh.status IN ('skipped','learning','mastered'))
                AND NOT EXISTS (SELECT 1 FROM quiz_log q WHERE q.user_id=? AND q.hanzi_id=h.id AND q.phase='learn')
                ORDER BY seq LIMIT ?""", (book_of(user), user_id, user_id, daily_new - len(new_ids))):
                new_ids.append(r["id"])
        new_chars = [dict(r) for r in db.execute(
            f"SELECT id, char, pinyin, tone3 FROM hanzi WHERE id IN ({','.join('?'*len(new_ids))}) ORDER BY seq",
            new_ids)] if new_ids else []
        # 到期复习
        reviews = [dict(r) for r in db.execute(
            """SELECT h.id, h.char, h.pinyin, h.tone3, uh.stage FROM user_hanzi uh
               JOIN hanzi h ON h.id=uh.hanzi_id
               WHERE uh.user_id=? AND uh.status='learning' AND uh.next_date<=?
               ORDER BY uh.next_date""", (user_id, today_str()))]  # stage/first_date 已含
        # 掌握词抽检
        audits = [dict(r) for r in db.execute(
            """SELECT h.id, h.char, h.pinyin, h.tone3 FROM user_hanzi uh
               JOIN hanzi h ON h.id=uh.hanzi_id
               WHERE uh.user_id=? AND uh.status='mastered' AND
                     (uh.audit_date IS NULL OR uh.audit_date<?)
               ORDER BY RANDOM() LIMIT ?""", (user_id, today_str(), AUDIT_N))]
        # 当日已学字（分关测验中途退出重进时，前端据此重建关队列）
        learned_today_chars = [dict(r) for r in db.execute(
            """SELECT DISTINCT h.id, h.char, h.pinyin, h.tone3 FROM quiz_log q
               JOIN hanzi h ON h.id=q.hanzi_id
               WHERE q.user_id=? AND q.phase='learn' AND q.ts=? ORDER BY h.seq""",
            (user_id, today_str()))]
        # 当日已答复习/抽检数（完成页统计真源：重进后 review 列表只剩未答项，直接取 length 会少计）
        review_done = db.execute(
            """SELECT COUNT(DISTINCT hanzi_id) n FROM quiz_log
               WHERE user_id=? AND ts>=? AND phase IN ('review','audit')""",
            (user_id, today_str())).fetchone()["n"]
        return {"mode": "learn", "day_index": di, "new": new_chars,
                "review": reviews, "audit": audits,
                "learned_today": learned_today_chars, "review_done": review_done}
    finally:
        db.close()


# ---------- 答题状态机 ----------

def submit(user_id, hanzi_id, phase, quiz_type, correct):
    """phase: learn|review|weekly|audit。返回该字最新状态。

    review/weekly/audit 当日幂等：同字同 phase 当日已提交过（中途退出重进重做）
    只记流水不二次推进状态机——间隔以首答为准。
    """
    db = get_db()
    try:
        t = today_str()
        replayed = phase in ("review", "weekly", "audit") and bool(_row(db,
            """SELECT 1 FROM quiz_log WHERE user_id=? AND hanzi_id=? AND phase=? AND ts>=? LIMIT 1""",
            (user_id, hanzi_id, phase, t)))
        db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) VALUES(?,?,?,?,?,?)",
                   (user_id, hanzi_id, phase, quiz_type, int(correct), t))
        if phase == "learn":
            db.execute("""INSERT INTO user_hanzi(user_id, hanzi_id, status, stage, next_date, first_date, last_date)
                          VALUES(?,?, 'learning', 0, date(?, '+1 day'), ?, ?)
                          ON CONFLICT(user_id, hanzi_id) DO UPDATE SET
                            status='learning', stage=0,
                            next_date=date(?, '+1 day'), first_date=?, last_date=?
                          WHERE user_hanzi.status='new'""",
                       (user_id, hanzi_id, t, t, t, t, t, t))
        elif phase in ("review", "weekly") and not replayed:
            row = db.execute("SELECT stage, wrong_count FROM user_hanzi WHERE user_id=? AND hanzi_id=?",
                             (user_id, hanzi_id)).fetchone()
            if row:
                if correct:
                    stage = row["stage"] + 1
                    if stage >= len(INTERVALS):
                        db.execute("""UPDATE user_hanzi SET status='mastered', stage=?, next_date=NULL,
                                      last_date=? WHERE user_id=? AND hanzi_id=?""",
                                   (stage, t, user_id, hanzi_id))
                    else:
                        db.execute("""UPDATE user_hanzi SET stage=?, next_date=date(?, '+' || ? || ' day'),
                                      last_date=? WHERE user_id=? AND hanzi_id=?""",
                                   (stage, t, INTERVALS[stage], t, user_id, hanzi_id))
                else:
                    db.execute("""UPDATE user_hanzi SET stage=1, wrong_count=wrong_count+1,
                                  next_date=date(?, '+1 day'), last_date=? WHERE user_id=? AND hanzi_id=?""",
                               (t, t, user_id, hanzi_id))
        elif phase == "audit" and not replayed:
            if not correct:
                db.execute("""UPDATE user_hanzi SET status='learning', stage=1, wrong_count=wrong_count+1,
                              next_date=date(?, '+1 day'), last_date=? WHERE user_id=? AND hanzi_id=?""",
                           (t, t, user_id, hanzi_id))
            else:
                db.execute("UPDATE user_hanzi SET audit_date=? WHERE user_id=? AND hanzi_id=?",
                           (t, user_id, hanzi_id))
        db.commit()
        row = db.execute("SELECT status, stage, next_date FROM user_hanzi WHERE user_id=? AND hanzi_id=?",
                         (user_id, hanzi_id)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def weekly_finish(user_id, week_no, score, total, wrong_ids):
    db = get_db()
    try:
        existed = _row(db, "SELECT 1 FROM weekly_reports WHERE user_id=? AND week_no=?",
                       (user_id, week_no))
        db.execute("""INSERT INTO weekly_reports(user_id, week_no, score, total, wrong_ids, done)
                      VALUES(?,?,?,?,?,1)
                      ON CONFLICT(user_id, week_no) DO NOTHING""",
                   (user_id, week_no, score, total, json.dumps(wrong_ids)))
        if not existed:                     # 首次结算才记星，重复结算不刷星
            db.execute("INSERT INTO stars(user_id, date, src, n) VALUES(?,?,'weekly',?)",
                       (user_id, today_str(), score))
        db.commit()
    finally:
        db.close()


# ---------- 进度 ----------

def get_progress(user_id):
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        cnt = db.execute("""SELECT
            SUM(CASE WHEN status IN ('learning','mastered') THEN 1 ELSE 0 END) learned,
            SUM(CASE WHEN status='mastered' THEN 1 ELSE 0 END) mastered,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) skipped
            FROM user_hanzi WHERE user_id=?""", (user_id,)).fetchone()
        stars = db.execute("SELECT COALESCE(SUM(n),0) s FROM stars WHERE user_id=?",
                           (user_id,)).fetchone()["s"]
        n_hanzi = db.execute("SELECT COUNT(*) n FROM hanzi WHERE book=?",
                             (book_of(user),)).fetchone()["n"]
        return {
            "total_hanzi": n_hanzi,
            "learned": cnt["learned"] or 0, "mastered": cnt["mastered"] or 0,
            "skipped": cnt["skipped"] or 0,
            "target_chars": user["target_chars"], "plan_days": user["plan_days"],
            "plan_start": user["plan_start"], "stars": stars,
            "plan_base_learned": user["plan_base_learned"] or 0,
            "pool": max(0, n_hanzi - (cnt["learned"] or 0) - (cnt["skipped"] or 0)),  # 剩余可学池（续订上限参考）
        }
    finally:
        db.close()


# ---------- 出题引擎 ----------

# 一年级形近字混淆组（部件/笔画差异；干扰项形近优先）。键值双向展开。
_CONFUSABLE_PAIRS = {
    "日": "目白田石旦", "目": "日自月", "白": "日百自", "田": "日由甲石",
    "人": "入八个大", "入": "人八", "八": "人入几", "大": "太天夫犬",
    "天": "夫大太无", "土": "士王干工", "士": "土王", "王": "土玉主",
    "工": "土干", "上": "下卡止", "下": "上卡", "口": "中日曰",
    "他": "地池她也", "地": "他池她", "木": "本术禾米才", "本": "木体",
    "禾": "木和秋", "米": "木来类", "水": "永冰", "山": "出凸",
    "石": "右占古", "四": "西匹", "西": "四酉", "儿": "几九七",
    "几": "儿九", "九": "几七", "刀": "力刃方", "力": "刀方刀",
    "马": "鸟乌妈", "鸟": "马乌岛", "乌": "鸟马", "云": "去会",
    "雨": "两雪", "小": "少沙水", "少": "小沙", "牛": "午生半",
    "月": "用肉明", "用": "月甩", "虫": "中丝", "手": "毛看",
    "毛": "手尾", "心": "必", "六": "立只", "半": "羊伴",
    "果": "里棵", "里": "果黑", "东": "车冻", "来": "米未",
    "开": "井并", "关": "天送", "见": "贝视", "贝": "见页",
    "自": "白目己", "己": "已巳自", "子": "了字", "长": "张",
    "方": "万放", "同": "问网", "问": "间闪同", "门": "问闪间",
    "们": "闪润", "我": "找", "水火": "灭", "火": "灭灰",
    "左": "右在", "右": "左石有", "有": "友右", "友": "有反",
    "年": "午生", "生": "牛年先", "午": "牛年", "飞": "气",
    "气": "汽飞", "早": "草", "字": "子学", "学": "字觉",
    "花": "化华", "回": "面", "片": "反", "两": "雨辆",
    "五": "互", "三": "王", "二": "三工", "一": "二七",
    "七": "匕十几", "十": "七土什", "力刀": "为", "为": "办力",
}
CONFUSABLE = {}
for k, v in _CONFUSABLE_PAIRS.items():
    for a in k:
        for b in v:
            if a != b:
                CONFUSABLE.setdefault(a, set()).add(b)
                CONFUSABLE.setdefault(b, set()).add(a)


def _row(db, sql, args=()):
    return db.execute(sql, args).fetchone()


def _all_words(db):
    """全库组词集（mean 歧义校验：干扰字代入挖空处不得构成真词）"""
    ws = set()
    for r in db.execute("SELECT words FROM zi_content WHERE words IS NOT NULL"):
        try:
            ws.update(json.loads(r["words"]))
        except (TypeError, ValueError):
            pass
    return ws


# jieba 盲区补充（2026-10-27 二次全量复审发现：口语/搭配层成词但词典查不到）
_SUPPLEMENT_WORDS = frozenset({
    "一人", "采来", "姐的", "妹的", "下个",                 # mean 干扰代入成真搭配
    "大词", "大棋", "大桌", "小白", "小王", "小我", "小几",  # 大/小+名词短语层多解
    "好问",                                                 # 好 hào 问异读成词
    # 感叹句式"多X"：句中"多+形容词"任何形容代入皆通（731 多少/多小 双解教训）
    "多大", "多小", "多好", "多美", "多高", "多长", "多快", "多甜", "多香", "多远", "多新",
})
_WORDS_CACHE = None


def _valid_words(db):
    """歧义校验统一词集：content 全库词 ∪ jieba dict ∪ 补充词集（惰性加载，进程内缓存）"""
    global _WORDS_CACHE
    if _WORDS_CACHE is None:
        ws = _all_words(db)
        f = Path(__file__).resolve().parent.parent / "data/words_dict.txt"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                w = line.split(" ")[0]
                if 2 <= len(w) <= 4:
                    ws.add(w)
        ws |= _SUPPLEMENT_WORDS
        _WORDS_CACHE = frozenset(ws)
    return _WORDS_CACHE


def _sent_window_bad(sentence, char, d_char, valid):
    """sent 干扰字代入后，与挖空前/后邻字组成的二字窗成真词 → 危险。

    堵"学校就在我家[后]面/青[瓜]/刚[好]"类句面双解（原 sent 只查干扰字不入句，漏窗口成词）。
    """
    i = sentence.find(char)
    if i < 0:
        return True
    if i > 0 and sentence[i - 1] + d_char in valid:
        return True
    return i + 1 < len(sentence) and d_char + sentence[i + 1] in valid


def _distractors(db, h, book, n=12):
    """干扰字池（形近 → 同单元 → 随机），返回 [{char, tone3}]，排除目标本身"""
    chars, seen = [], {h["char"]}
    conf = CONFUSABLE.get(h["char"], set())
    if conf:
        for r in db.execute(f"SELECT char FROM hanzi WHERE char IN ({','.join('?' * len(conf))})",
                            tuple(conf)):
            if r["char"] not in seen:
                chars.append(r["char"]); seen.add(r["char"])
    bk = book or h["book"]
    for sql, args in (
        ("SELECT char FROM hanzi WHERE book=? AND unit=? ORDER BY RANDOM()", (bk, h["unit"])),
        ("SELECT char FROM hanzi WHERE book=? ORDER BY RANDOM()", (bk,)),
    ):
        for r in db.execute(sql, args):
            if r["char"] not in seen:
                chars.append(r["char"]); seen.add(r["char"])
            if len(chars) >= n:
                break
        if len(chars) >= n:
            break
    if not chars:
        return []
    rows = db.execute(f"SELECT char, tone3 FROM hanzi WHERE char IN ({','.join('?' * len(chars))})",
                      tuple(chars)).fetchall()
    by_char = {r["char"]: dict(r) for r in rows}
    return [by_char[ch] for ch in chars if ch in by_char]


# 题库内退化方向（与运行时生成退化链一致；题库缺该题型时降级取）
_QB_DEGRADE = {"find": ("sent", "mean", "listen"),
               "sent": ("mean", "listen"), "mean": ("listen",),
               "pin": ("listen",), "rev": ("listen",)}


def _qbank(db, hanzi_id, quiz_types):
    """从已审查题库（verified=1）按优先序取第一题。

    options 运行时 shuffle：审查过的选项集合不变，仅打乱展示顺序防位置记忆。
    """
    for qt in quiz_types:
        row = _row(db, """SELECT payload FROM questions
                          WHERE hanzi_id=? AND quiz_type=? AND verified=1
                          ORDER BY variant LIMIT 1""", (hanzi_id, qt))
        if row:
            q = json.loads(row["payload"])
            if q.get("options"):
                random.shuffle(q["options"])
            return q
    return None


def gen_quiz(hanzi_id, quiz_type, book=None):
    """出题：listen(听音选字) | mean(组词补全) | sent(句子填空) | find(找一找)。

    题库优先：1a 全量题经离线生成+双重词集校验+逐题人工审查（gen_questions.py /
    review_apply.py，2026-10-27），运行时只从 verified 池取，杜绝语义双解题上线。
    题库无该字/该题型 → 题库内退化 → 仍无（2a+ 册尚未建库）→ 运行时生成兜底。
    运行时生成约束：
    - 干扰项同册字池内 形近优先 → 同单元 → 随机；
    - listen 干扰与目标读音不同，且选项间读音两两不同（防 他/她 同 tā）；
    - mean 目标字在词中恰好出现 1 次（多则挖空后剩余出现泄漏答案），干扰字≠题干字，
      代入挖空不得成真词（统一词集 = content ∪ jieba ∪ 补充词集）；
    - sent 例句中目标字恰好 1 次，干扰字不得在句中出现（防句中现字），代入后与挖空
      前后邻字的二字窗不得成真词（防"后面/青瓜/刚好"句面双解），且题干全库唯一
      （防"太阳升＿来了=上/起"同句异答）；
    - find 词行+句行中目标字复现 ≥2 才有游戏感（多例句素材：词行 ≤2 + 全部例句）。
    """
    db = get_db()
    try:
        q = _qbank(db, hanzi_id, (quiz_type,) + _QB_DEGRADE.get(quiz_type, ()))
        if q:
            return q
        h = _row(db, "SELECT * FROM hanzi WHERE id=?", (hanzi_id,))
        if not h:
            return None
        c = _row(db, "SELECT * FROM zi_content WHERE hanzi_id=?", (hanzi_id,))
        words = json.loads(c["words"]) if c and c["words"] else []
        if c and c["sentences"]:
            sentences = json.loads(c["sentences"])
        else:
            sentences = [c["sentence"]] if c and c["sentence"] else []
        ds = _distractors(db, h, book)

        if quiz_type == "find":
            lines = [w for w in words if h["char"] in w][:2] + sentences
            count = sum(l.count(h["char"]) for l in lines)
            if count >= 2:
                return {"quiz_type": "find", "hanzi_id": hanzi_id, "hanzi": h["char"],
                        "tone3": h["tone3"], "lines": lines, "count": count}
            return gen_quiz(hanzi_id, "sent", book=book)

        if quiz_type == "sent":
            valid = _valid_words(db)
            cands = [s for s in sentences if s.count(h["char"]) == 1]
            random.shuffle(cands)
            for sentence in cands:
                stem = sentence.replace(h["char"], "＿", 1)
                clash = _row(db, """SELECT 1 FROM questions WHERE quiz_type='sent' AND verified=1
                                    AND json_extract(payload, '$.stem')=? LIMIT 1""", (stem,))
                if clash:
                    continue
                dr = [d["char"] for d in ds
                      if d["char"] not in sentence
                      and not _sent_window_bad(sentence, h["char"], d["char"], valid)][:3]
                if len(dr) >= 3:
                    opts = [h["char"]] + dr[:3]
                    random.shuffle(opts)
                    return {"quiz_type": "sent", "hanzi_id": hanzi_id, "hanzi": h["char"],
                            "tone3": h["tone3"], "stem": stem,
                            "sentence": sentence, "options": opts, "answer": h["char"]}
            return gen_quiz(hanzi_id, "mean", book=book)

        if quiz_type == "mean":
            valid = _valid_words(db)
            cands = [w for w in words if w.count(h["char"]) == 1]
            random.shuffle(cands)
            for w in cands:
                dr = [d["char"] for d in ds if d["char"] not in w
                      and w.replace(h["char"], d["char"], 1) not in valid][:3]
                if len(dr) >= 3:
                    opts = [h["char"]] + dr[:3]
                    random.shuffle(opts)
                    return {"quiz_type": "mean", "hanzi_id": hanzi_id, "hanzi": h["char"],
                            "tone3": h["tone3"], "stem": w.replace(h["char"], "＿", 1),
                            "hint_word": w, "options": opts, "answer": h["char"]}
            return gen_quiz(hanzi_id, "listen", book=book)

        # listen：干扰读音必须不同，且选项间两两不同（防 他/她 同 tā）
        seen_t = {h["tone3"]}
        dr = []
        for d in ds:
            if d["tone3"] not in seen_t:
                dr.append(d)
                seen_t.add(d["tone3"])
        if len(dr) < 3:
            for r in db.execute("SELECT char, tone3 FROM hanzi "
                                "WHERE book=? AND char != ? ORDER BY RANDOM() LIMIT 12",
                                (h["book"], h["char"])):
                if r["tone3"] not in seen_t:
                    dr.append(dict(r))
                    seen_t.add(r["tone3"])
                if len(dr) >= 3:
                    break
        if len(dr) < 3:
            return None
        opts = [h["char"]] + [d["char"] for d in dr[:3]]
        random.shuffle(opts)
        return {"quiz_type": "listen", "hanzi_id": hanzi_id, "hanzi": h["char"],
                "tone3": h["tone3"], "stem": "", "options": opts, "answer": h["char"]}
    finally:
        db.close()
