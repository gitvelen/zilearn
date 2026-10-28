"""登录与会话：bcrypt 哈希 + 签名 cookie + 登录失败限速（15分钟窗口8次）"""
import time
import bcrypt
from fastapi import HTTPException
from database import get_db

_fail = {}  # ip -> [fail_count, window_start_ts]


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def login(username, password, ip):
    now = time.time()
    rec = _fail.get(ip)
    if rec and rec[0] >= 8 and now - rec[1] < 900:
        raise HTTPException(429, "尝试次数过多，请15分钟后再试")
    db = get_db()
    try:
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        db.close()
    if not row or not verify_password(password, row["password_hash"]):
        if rec and now - rec[1] < 900:
            rec[0] += 1
        else:
            _fail[ip] = [1, now]
        raise HTTPException(401, "用户名或密码错误")
    _fail.pop(ip, None)
    return row


def current_user(request):
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "未登录")
    db = get_db()
    try:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        db.close()
    if not row:
        raise HTTPException(401, "用户不存在")
    return row
