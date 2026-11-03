"""zilearn FastAPI 入口"""
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, File, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import asr
import database
import auth
import report
import scheduler

BASE = Path(__file__).parent
STATIC = BASE / "static"

app = FastAPI(title="zilearn", docs_url=None, redoc_url=None)

_secret_file = BASE / "data" / "secret.key"
_secret_file.parent.mkdir(parents=True, exist_ok=True)
if _secret_file.exists():
    _secret = _secret_file.read_text().strip()
else:
    _secret = secrets.token_urlsafe(32)
    _secret_file.write_text(_secret)
app.add_middleware(SessionMiddleware, secret_key=_secret, max_age=30 * 86400)


@app.middleware("http")
async def no_cache_html(request, call_next):
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _startup():
    database.init_db()


class LoginIn(BaseModel):
    username: str
    password: str

class GradeIn(BaseModel):
    grade: str

class PlanIn(BaseModel):
    target_chars: int
    days: int

class SubmitIn(BaseModel):
    hanzi_id: int
    phase: str          # learn | review | weekly | audit
    quiz_type: str = ""
    correct: bool

class ConfirmSubmitIn(BaseModel):
    known_ids: list

class WeeklyFinishIn(BaseModel):
    week_no: int
    score: int
    total: int
    wrong_ids: list

class FollowLogIn(BaseModel):
    char: str
    kind: str            # char|word|sent|read
    heard: bool | None = None   # None=无麦/拒绝的节奏式降级
    tries: int = 0
    peak_rms: float | None = None    # v19 遥测（旧前端不传，均默认 None）
    noise_base: float | None = None
    thr_used: float | None = None
    voiced_ms: int | None = None
    dur_ms: int | None = None
    played: int | None = None       # 范读播放结果（v21 遥测）
    model_ms: int | None = None
    mic_settings: str | None = None  # gUM 实际生效设置 JSON（Safari 忽略 EC/NS 约束的电平取证）
    ts_ms: int | None = None        # 客户端时刻：区分弱网批量交付
    ua: str | None = None
    asr_text: str | None = None     # 词步 ASR 识别文本（v38：无 ASR/降级为 None）
    asr_pass: int | None = None     # 词步 ASR 匹配结果 1/0（未走 ASR 为 None）


VALID_GRADES = {f"{g}{t}" for g in range(1, 7) for t in ("A", "B")}


def _client_ip(request):
    # XFF 取末段：单层 caddy 反代会把自己的 peer 追加在末尾，首段可被客户端伪造
    # （轮换伪造首段既绕限速又撑大 auth._fail）。多级代理接入时须改为倒数第 N 段
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    user = auth.login(body.username.strip(), body.password, _client_ip(request))
    request.session["uid"] = user["id"]
    return {"ok": True, "username": user["username"]}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = auth.current_user(request)
    progress = scheduler.get_progress(user["id"])
    ready = scheduler.ready_books()
    stage = "onboard"
    if user["grade"]:
        stage = "plan"
    if user["target_chars"]:
        stage = "confirm" if user["onboarded"] == 0 else "study"
    if user["grade"] and scheduler.book_of(user) not in ready:
        stage = "onboard"   # 所选册内容未生产（2026-11-01 1B 事故）：打回年级重选
    return {"user": {"username": user["username"], "display_name": user["display_name"],
                     "grade": user["grade"]},
            "progress": progress, "stage": stage,
            "grades_ready": sorted(b.upper() for b in ready)}


@app.post("/api/profile/grade")
def set_grade(body: GradeIn, request: Request):
    user = auth.current_user(request)
    if body.grade not in VALID_GRADES:
        raise HTTPException(400, "无效年级")
    if body.grade[0].lower() + body.grade[1].lower() not in scheduler.ready_books():
        raise HTTPException(400, "这本教材的字还在制作中，先选已上线的教材吧")
    db = database.get_db()
    try:
        db.execute("UPDATE users SET grade=? WHERE id=?", (body.grade, user["id"]))
        if (user["grade"] or "") != body.grade:   # 换册：旧册摸底未决项与水位作废
            db.execute("DELETE FROM confirm_pick WHERE user_id=? AND result IS NULL", (user["id"],))
            db.execute("UPDATE users SET confirm_upto=0 WHERE id=?", (user["id"],))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.post("/api/plan/create")
def create_plan(body: PlanIn, request: Request):
    user = auth.current_user(request)
    return scheduler.create_plan(user["id"], body.target_chars, body.days)


@app.get("/api/confirm/batches")
def confirm_list(request: Request):
    user = auth.current_user(request)
    return {"batches": scheduler.confirm_batches(user)}


@app.post("/api/confirm/submit")
def confirm_submit(body: ConfirmSubmitIn, request: Request):
    """速选提交（验证轮已砍，2026-11-03 拍板）：已选直接 skipped；
    池内确认生词不足目标 → more_batch 供应下一批（前端回摸底表格页）。
    无补摸批次 = 摸底完成 → 置 onboarded（摸底中途刷新可回摸底页续选，2026-11-03 补洞）"""
    user = auth.current_user(request)
    r = scheduler.confirm_submit(user["id"], body.known_ids)
    more = scheduler.confirm_more(user)
    if not more:
        db = database.get_db()
        try:
            db.execute("UPDATE users SET onboarded=1 WHERE id=?", (user["id"],))
            db.commit()
        finally:
            db.close()
    r["more_batch"] = more
    return r


@app.post("/api/hanzi/{hanzi_id}/stroke_seen")
def stroke_seen(hanzi_id: int, request: Request):
    """写步首见笔顺演示看完标记（服务端持久化跨会话；复见免强制重看，2026-11-03 拍板）。
    INSERT status='new' 行不挡后续 learn 提交（其 upsert 守卫 WHERE status='new' 恰好匹配），
    也不把字踢出学习池（池排除条件只认 skipped/learning/mastered）。"""
    user = auth.current_user(request)
    db = database.get_db()
    try:
        db.execute("""INSERT INTO user_hanzi(user_id, hanzi_id, status, stroke_seen)
                      VALUES(?,?, 'new', 1)
                      ON CONFLICT(user_id, hanzi_id) DO UPDATE SET stroke_seen=1""",
                   (user["id"], hanzi_id))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.get("/api/today")
def today(request: Request):
    user = auth.current_user(request)
    return scheduler.build_today(user["id"])


@app.post("/api/dev/next-day")
def dev_next_day(request: Request):
    """TEST 专属跳日：虚拟日历 +1（同一天连学下一天课程）；非 test 账号 403"""
    user = auth.current_user(request)
    if user["username"].lower() != "test":
        raise HTTPException(403, "仅 test 账号可用")
    db = database.get_db()
    try:
        db.execute("UPDATE users SET day_offset=day_offset+1 WHERE id=?", (user["id"],))
        db.commit()
        off = db.execute("SELECT day_offset FROM users WHERE id=?",
                         (user["id"],)).fetchone()["day_offset"]
    finally:
        db.close()
    return {"ok": True, "day_offset": off}


@app.post("/api/submit")
def submit(body: SubmitIn, request: Request):
    user = auth.current_user(request)
    return scheduler.submit(user["id"], body.hanzi_id, body.phase, body.quiz_type, body.correct)


@app.get("/api/quiz")
def quiz(request: Request, hanzi_id: int, quiz_type: str = "listen"):
    user = auth.current_user(request)
    return scheduler.gen_quiz(hanzi_id, quiz_type, book=scheduler.book_of(user))


@app.get("/api/hanzi/{hanzi_id}/full")
def hanzi_full(hanzi_id: int, request: Request):
    auth.current_user(request)
    db = database.get_db()
    try:
        h = db.execute("SELECT * FROM hanzi WHERE id=?", (hanzi_id,)).fetchone()
        c = db.execute("SELECT * FROM zi_content WHERE hanzi_id=?", (hanzi_id,)).fetchone()
        if not h:
            raise HTTPException(404, "无此字")
        return {"hanzi": dict(h), "content": dict(c) if c else None}
    finally:
        db.close()


@app.post("/api/follow/log")
def follow_log(body: FollowLogIn, request: Request):
    """跟读开口流水（前端 fire-and-forget）：有效性的唯一实证来源"""
    user = auth.current_user(request)
    db = database.get_db()
    try:
        h = db.execute("SELECT id FROM hanzi WHERE char=?", (body.char,)).fetchone()
        db.execute(
            "INSERT INTO follow_log(user_id, hanzi_id, char, kind, heard, tries,"
            " peak_rms, noise_base, thr_used, voiced_ms, dur_ms, played, model_ms, mic_settings, ts_ms, ua,"
            " asr_text, asr_pass)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], h["id"] if h else None, body.char, body.kind,
             None if body.heard is None else int(body.heard), body.tries,
             body.peak_rms, body.noise_base, body.thr_used, body.voiced_ms,
             body.dur_ms, body.played, body.model_ms,
             body.mic_settings[:120] if body.mic_settings else None, body.ts_ms,
             body.ua[:160] if body.ua else None,
             body.asr_text[:60] if body.asr_text else None, body.asr_pass))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.post("/api/follow/verify")
async def follow_verify(request: Request, char: str = Form(...), word: str = Form(...),
                        file: UploadFile = File(...)):
    """词步跟读音校验（v38 拍板"必须跟读+低门槛正确性，对孩子无感知"）：
    录音→ASR 转写→拼音容错匹配。ASR 未配置/失败/超限一律降级放行（pass=true+degraded），
    前端另有 1.6s 超时兜底——判定层永不卡孩子节奏。"""
    auth.current_user(request)
    raw = await file.read()
    return asr.verify_audio(raw, file.content_type or "", word)


@app.post("/api/weekly/finish")
def weekly_finish(body: WeeklyFinishIn, request: Request):
    user = auth.current_user(request)
    scheduler.weekly_finish(user["id"], body.week_no, body.score, body.total, body.wrong_ids)
    return {"ok": True}


@app.post("/api/report/daily")
def report_daily(request: Request):
    """学习日报（wordslearn 同模式）：完成当日学习后前端触发，服务端聚合+SMTP，
    幂等（daily_mail 唯一日）；失败可重试，永不阻塞完成流程"""
    user = auth.current_user(request)
    return report.send_daily_report(user["id"])


@app.get("/api/report/weekly")
def report_weekly(request: Request):
    user = auth.current_user(request)
    db = database.get_db()
    try:
        rows = db.execute("SELECT * FROM weekly_reports WHERE user_id=? ORDER BY week_no",
                          (user["id"],)).fetchall()
        stars = db.execute("SELECT date, src, n FROM stars WHERE user_id=? ORDER BY date",
                           (user["id"],)).fetchall()
        wrong_recent = db.execute(
            """SELECT h.char, COUNT(*) n FROM quiz_log q JOIN hanzi h ON h.id=q.hanzi_id
               WHERE q.user_id=? AND q.correct=0 GROUP BY h.char ORDER BY n DESC LIMIT 10""",
            (user["id"],)).fetchall()
        # 近 7 天趋势：每日答题数/正确数（卡片完成 card 不算题）+ 新学字数
        from datetime import date, timedelta
        days = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        q_by_day = {r["ts"]: (r["n"], r["ok"]) for r in db.execute(
            """SELECT ts, COUNT(*) n, SUM(correct) ok FROM quiz_log
               WHERE user_id=? AND ts>=? AND quiz_type!='card' GROUP BY ts""",
            (user["id"], days[0]))}
        new_by_day = {r["d"]: r["n"] for r in db.execute(
            """SELECT first_date d, COUNT(*) n FROM user_hanzi
               WHERE user_id=? AND first_date>=? GROUP BY first_date""",
            (user["id"], days[0]))}
        # 跟读开口（近 7 天）：heard=读到 / timeout=超时 / na=无麦降级 —— 家长判断"孩子在真读"
        fstat = db.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(heard=1),0) heard,
                      COALESCE(SUM(heard=0),0) timeout, COALESCE(SUM(heard IS NULL),0) na
               FROM follow_log WHERE user_id=? AND ts>=?""",
            (user["id"], days[0])).fetchone()
        trend = []
        for d in days:
            n, ok = q_by_day.get(d, (0, 0))
            trend.append({"date": d, "label": f"{int(d[5:7])}/{int(d[8:10])}",
                          "n": n, "ok": ok, "new": new_by_day.get(d, 0)})
        return {"weekly": [dict(r) for r in rows], "stars": [dict(s) for s in stars],
                "top_wrong": [dict(w) for w in wrong_recent], "trend": trend,
                "follow": dict(fstat) if fstat["n"] else None}
    finally:
        db.close()


# Starlette StaticFiles 对非 ASCII 文件名 404，audio/strokes 用显式路由（须在 mount 之前注册）
from urllib.parse import unquote
from fastapi.responses import FileResponse

def _safe_file(sub: str, name: str) -> Path:
    p = (STATIC / sub / unquote(name)).resolve()
    if not str(p).startswith(str((STATIC / sub).resolve())) or not p.is_file():
        raise HTTPException(404)
    return p

@app.get("/static/audio/{name}")
def audio_file(name: str):
    # 音频走内容寻址（文件名=内容），可长缓存：省掉每次 304 revalidation 的 RTT——
    # 公网真机上题干音频的加载迟滞是"切题卡顿"主源之一（2026-11-02 用户实测反馈）
    r = FileResponse(_safe_file("audio", name), media_type="audio/mpeg")
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r

@app.get("/static/strokes/{name}")
def stroke_file(name: str):
    return FileResponse(_safe_file("strokes", name), media_type="application/json")

app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
