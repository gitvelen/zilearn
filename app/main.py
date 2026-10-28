"""zilearn FastAPI 入口"""
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import database
import auth
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

class ConfirmVerifyIn(BaseModel):
    hanzi_id: int
    correct: bool

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


VALID_GRADES = {f"{g}{t}" for g in range(1, 7) for t in ("A", "B")}


def _client_ip(request):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
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
    stage = "onboard"
    if user["grade"]:
        stage = "plan"
    if user["target_chars"]:
        stage = "confirm" if user["onboarded"] == 0 else "study"
    return {"user": {"username": user["username"], "display_name": user["display_name"],
                     "grade": user["grade"]},
            "progress": progress, "stage": stage}


@app.post("/api/profile/grade")
def set_grade(body: GradeIn, request: Request):
    user = auth.current_user(request)
    if body.grade not in VALID_GRADES:
        raise HTTPException(400, "无效年级")
    db = database.get_db()
    try:
        db.execute("UPDATE users SET grade=? WHERE id=?", (body.grade, user["id"]))
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
    user = auth.current_user(request)
    scheduler.confirm_submit(user["id"], body.known_ids)
    return scheduler.confirm_progress(user["id"])


@app.post("/api/confirm/verify")
def confirm_verify(body: ConfirmVerifyIn, request: Request):
    user = auth.current_user(request)
    scheduler.confirm_verify(user["id"], body.hanzi_id, body.correct)
    prog = scheduler.confirm_progress(user["id"])
    # 下一道验证题（形近干扰：错选概率真实化）
    db = database.get_db()
    try:
        nxt = db.execute("""SELECT p.hanzi_id FROM confirm_pick p
                            WHERE p.user_id=? AND p.result IS NULL LIMIT 1""",
                         (user["id"],)).fetchone()
    finally:
        db.close()
    q = scheduler.gen_quiz(nxt["hanzi_id"], "listen", book=scheduler.book_of(user)) if nxt else None
    return {"progress": prog, "next_verify": q}


@app.get("/api/confirm/verify/first")
def confirm_first(request: Request):
    user = auth.current_user(request)
    db = database.get_db()
    try:
        nxt = db.execute("""SELECT p.hanzi_id FROM confirm_pick p
                            WHERE p.user_id=? AND p.result IS NULL ORDER BY p.hanzi_id LIMIT 1""",
                         (user["id"],)).fetchone()
    finally:
        db.close()
    if not nxt:
        return {"next_verify": None}
    return {"next_verify": scheduler.gen_quiz(nxt["hanzi_id"], "listen", book=scheduler.book_of(user))}


@app.get("/api/today")
def today(request: Request):
    user = auth.current_user(request)
    return scheduler.build_today(user["id"])


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
            "INSERT INTO follow_log(user_id, hanzi_id, char, kind, heard, tries) VALUES(?,?,?,?,?,?)",
            (user["id"], h["id"] if h else None, body.char, body.kind,
             None if body.heard is None else int(body.heard), body.tries))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.post("/api/weekly/finish")
def weekly_finish(body: WeeklyFinishIn, request: Request):
    user = auth.current_user(request)
    scheduler.weekly_finish(user["id"], body.week_no, body.score, body.total, body.wrong_ids)
    return {"ok": True}


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
    return FileResponse(_safe_file("audio", name), media_type="audio/mpeg")

@app.get("/static/strokes/{name}")
def stroke_file(name: str):
    return FileResponse(_safe_file("strokes", name), media_type="application/json")

app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
