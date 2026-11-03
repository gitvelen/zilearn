#!/usr/bin/env python3
"""跟读节奏槽回归（2026-11-03 两轮拍板重写；直测 followHeard 真实现，不 stub）

槽语义（音频时钟化）：范读→"叮"→留白窗（定时器上限）——窗必经、结束必放行；
音量检测只做可视化/提前收窗/遥测，永不 gate；**不做正确性判定**（ASR 已删：
无录音上传，槽走完即完成；朗读卡会话录音回放保留）。
  T1 静音流（认步 char 槽）：超时 ~6.5s → ✓ 爆闪（无论检测）→ 推进；heard=0；零 ASR 请求
  T0 间歇声源（句步 sent 槽）：读到 → 提前收窗 + 确认音；heard=1
  T2 挂起的 getUserMedia（原硬卡死路径）：heard=NULL 降级，槽窗照常走完推进
  T3 /app.js?v=48 加载 200 + 全程无 JS pageerror

用法: python3 tests/follow_fix_test.py [base_url]   # 需本地服务已起
"""
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
DB = Path(__file__).parent.parent / "app" / "data" / "zilearn.db"
SHOTS = Path(__file__).parent.parent / "tests" / "shots"
SHOTS.mkdir(exist_ok=True)
PASS_N = FAIL_N = 0


def check(name, cond, detail=""):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"  ✓ {name}")
    else:
        FAIL_N += 1
        print(f"  ✗ {name}  {detail}")


def db_exec(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def follow_rows(kind=None):
    conn = sqlite3.connect(DB)
    q = ("SELECT char,kind,heard,asr_pass FROM follow_log WHERE user_id="
         "(SELECT id FROM users WHERE username='test')")
    if kind:
        q += f" AND kind='{kind}'"
    rows = conn.execute(q + " ORDER BY rowid").fetchall()
    conn.close()
    return rows


def reset_test_user():
    conn = sqlite3.connect(DB)
    uid = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()[0]
    conn.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL,
                    plan_start=NULL, plan_base_learned=0, confirm_upto=0, onboarded=0,
                    day_offset=0 WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports",
              "follow_log", "daily_mail"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


# WebAudio 构造 MediaStream 预设到 app（followHeard 真跑）：
# 静音流→拾音恒 0=超时路径；间歇流(声 300ms/静 700ms, RMS≈0.28)→读到路径
def inject_stream(voice):
    gain = "0.4" if voice else "0"
    flicker = """
      let on = false;
      setInterval(() => { on = !on; g.gain.setTargetAtTime(on ? 0.4 : 0.0, ctx.currentTime, 0.02); }, 350);
    """ if voice else ""
    return f"""
() => {{
  const app = window.ziapp;
  const ctx = new AudioContext();
  const osc = ctx.createOscillator(), g = ctx.createGain();
  g.gain.value = {gain}; osc.frequency.value = 440;
  const dest = ctx.createMediaStreamDestination();
  osc.connect(g); g.connect(dest); osc.start();
  {flicker}
  if (app._micStream) app._micStream.getTracks().forEach(t => t.stop());
  app._micStream = dest.stream; app._micCtx = ctx; app._micFailUntil = 0;
  navigator.mediaDevices.getUserMedia = () => new Promise(() => {{}});  // 防重取（T2 复用为挂起态）
  return app.curItem && app.curItem.char;
}}"""


def run(page):
    reset_test_user()
    verify_reqs = []
    page.on("request", lambda r: verify_reqs.append(r.url) if "/api/follow/verify" in r.url else None)
    # ---- 登录 → 引导（验证轮已砍）→ 首张新字卡认步 ----
    page.goto(BASE, wait_until="networkidle")
    # secure context 自检：容器 IP 直连（http://172.x.x.x）非安全上下文 → mediaDevices
    # undefined → gUM/录音链路全灭，跟读相关断言会以诡异形态挂（2026-11-03 误判"产品
    # bug"一小时）。BASE 必须是 127.0.0.1/localhost 或 https。
    check("secure context（mediaDevices 可用）", page.evaluate("() => !!navigator.mediaDevices"),
          BASE)
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".wizard", timeout=10000)
    page.click(".grade:first-child")
    page.click("button.btn.big")
    page.wait_for_timeout(400)
    page.fill("input[type=number]", "52")
    page.locator(".chip", has_text="30 天").click()
    page.click("button.btn.big")
    page.wait_for_selector(".char-grid", timeout=10000)
    page.click("button.btn.big")
    page.wait_for_function("window.ziapp.phase==='today-intro'", timeout=10000)
    page.evaluate("() => document.querySelector('button.btn.pulse').click()")
    page.wait_for_function(
        "window.ziapp && window.ziapp.curItem && window.ziapp.curItem.type==='newcard'"
        " && window.ziapp.ncStep===1", timeout=15000, polling=100)

    # ---- T1 静音流（认步 char 槽）：线上故障复现（超时=结构完成，✓ 后放行） ----
    ch = page.evaluate(inject_stream(voice=False))
    print(f"  [T1] 新字: {ch}")
    page.wait_for_function("window.ziapp.follow.state==='wait'", timeout=15000, polling=100)
    check("T1 wait 态提示轮到你", "轮到你啦" in page.locator(".fb-hint").inner_text())
    t_wait = time.time()
    page.wait_for_function("window.ziapp.follow.on===false", timeout=20000, polling=100)
    gap1 = time.time() - t_wait
    check("T1 超时窗后放行(5.5-10s)", 5.5 <= gap1 <= 10, f"{gap1:.1f}s")
    check("T1 槽后按钮解禁", not page.locator(".learn-card > button.btn.big").is_disabled())
    page.wait_for_timeout(800)                       # 流水 fire-and-forget settle
    rows = [r for r in follow_rows() if r[1] == "char"]
    check("T1 char 槽流水 heard=0", rows and rows[-1][2] == 0, rows)
    check("T1 零 ASR 请求（正确性判定已删）", not verify_reqs, verify_reqs[:2])

    # ---- 词步回自由：连听(narrLock)后无槽，直接可推进 ----
    page.click(".learn-card > button.btn.big")
    page.wait_for_function("window.ziapp.ncStep===2 && window.ziapp.narrLock===false",
                           timeout=12000, polling=100)
    check("T1b 词步无跟读槽(连听后自由)", not page.evaluate("() => window.ziapp.follow.on"))

    # ---- T0 间歇声源（句步 sent 槽）：读到路径 ----
    page.evaluate(inject_stream(voice=True))         # 覆盖静音流
    page.click(".learn-card > button.btn.big")       # → 句
    t_sent = time.time()
    page.wait_for_function("window.ziapp.followPop===true", timeout=25000, polling=50)
    check("T0 读到→✓爆闪", True, f"{time.time()-t_sent:.1f}s")
    page.wait_for_function("window.ziapp.follow.on===false", timeout=60000, polling=100)
    check("T0 句槽读到后推进", True, f"{time.time()-t_sent:.1f}s")
    page.wait_for_timeout(800)
    s_rows = [r for r in follow_rows("sent")]
    check("T0 句槽流水 heard=1", s_rows and all(r[2] == 1 for r in s_rows[:1]), s_rows)

    # ---- T2 挂起的 getUserMedia（原硬卡死路径的槽语义复验） ----
    # 已在句步自由态（follow.on=false）：ncGo(1) 重进认步复现取麦挂起
    page.evaluate("""() => {
      const app = window.ziapp;
      if (app._micStream) app._micStream.getTracks().forEach(t => t.stop());
      app._micStream = null; app._micCtx = null; app._micFailUntil = 0; app.micDenied = false;
      app._micEnsuring = null;
      navigator.mediaDevices.getUserMedia = () => new Promise(() => {});
    }""")
    page.evaluate("() => ziapp.ncGo(1)")
    t2 = time.time()
    page.wait_for_function("window.ziapp.follow.on===true", timeout=15000, polling=100)
    page.wait_for_function("window.ziapp.micDenied===true", timeout=10000, polling=100)
    check("T2 挂起 gUM 3s 超时→micDenied", True, f"{time.time()-t2:.1f}s")
    page.wait_for_function("window.ziapp.follow.on===false", timeout=20000, polling=100)
    total = time.time() - t2
    check("T2 无麦槽窗照常走完不卡死", total < 20, f"{total:.1f}s")
    page.wait_for_timeout(800)
    c2 = [r for r in follow_rows("char")]
    check("T2 降级流水 heard=NULL(三态保留)", c2 and c2[-1][2] is None, c2)
    page.screenshot(path=str(SHOTS / "fix-t2-degraded-slot.png"), full_page=True)


if __name__ == "__main__":
    errors, v47 = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required",
                  "--use-fake-device-for-media-stream",
                  "--use-fake-ui-for-media-stream"])
        page = browser.new_page(viewport={"width": 820, "height": 1100})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: v47.append(r.status) if "app.js?v=" in r.url else None)
        try:
            run(page)
        finally:
            browser.close()
    check("T3 app.js?v=N 加载 200（版本 bust 生效）", v47 == [200], v47)
    check("T3 无 JS pageerror", not errors, errors[:3])
    reset_test_user()   # teardown
    print(f"\n{PASS_N} pass / {FAIL_N} fail  （截图: tests/shots/fix-*.png）")
    sys.exit(1 if FAIL_N else 0)
