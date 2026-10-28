#!/usr/bin/env python3
"""2026-10-28 跟读卡死修复回归（四场景，直测 followHeard 真实现，不 stub）

T1 静音流=线上故障复现（4 词×2 轮全超时）：超时→"没听到你读哦"重试文案→放行；
   单轮兜底 1.5s（wait→retry 间隔应 ~5.5s 而非旧 7.5s）——认步 char 跟读
T0 间歇声源(300ms 声/700ms 静≈说话节律)：读到→✓爆闪→流水 heard=1——词步 word 跟读
   （注：--use-file-for-fake-audio-cycles 的持续声会被动态阈值当本底，测不了这条路径）
T2 getUserMedia 永久挂起（权限弹窗未决=原硬卡死路径）：3s 超时→micDenied 降级，
   30s 失败记忆使后续项立即降级，全程推进不卡死，流水 heard=NULL——句步 sent 跟读
T3 /app.js?v=18 加载 200 + 全程无 JS pageerror

用法: python3 tests/follow_fix_test.py [base_url]   # 需本地服务已起
"""
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
DB = Path(__file__).parent.parent / "app" / "data" / "zilearn.db"
SHOTS = Path(__file__).parent / "shots"
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


def follow_rows():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT char,kind,heard,tries FROM follow_log WHERE user_id="
        "(SELECT id FROM users WHERE username='test') ORDER BY rowid").fetchall()
    conn.close()
    return rows


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
    db_exec("DELETE FROM follow_log WHERE user_id=(SELECT id FROM users WHERE username='test')")

    # ---- 登录（test 已 onboard，直达今日页）----
    page.goto(BASE, wait_until="networkidle")
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".day-card", timeout=10000)
    page.click("button.btn.pulse", force=True)
    page.wait_for_function(
        "window.ziapp && window.ziapp.curItem && window.ziapp.curItem.type==='newcard'"
        " && window.ziapp.ncStep===1", timeout=15000, polling=100)

    # ---- T1 静音流（认步 char）：线上故障复现 ----
    ch = page.evaluate(inject_stream(voice=False))
    print(f"  [T1] 新字: {ch}")
    page.wait_for_function("window.ziapp.follow.state==='wait'", timeout=15000, polling=100)
    check("T1 wait 态提示轮到你", "轮到你啦" in page.locator(".fb-hint").inner_text())
    t_wait = time.time()
    page.wait_for_function(
        "window.ziapp.follow.state==='model' && window.ziapp.follow.tries===1",
        timeout=15000, polling=100)
    gap1 = time.time() - t_wait
    check("T1 单轮超时 ~5.5s(兜底1.5s生效)", 4.5 <= gap1 <= 8, f"{gap1:.1f}s")
    check("T1 重试轮文案", "没听到你读哦" in page.locator(".fb-hint.retry").inner_text())
    page.screenshot(path=str(SHOTS / "fix-t1-retry-hint.png"), full_page=True)
    t_retry = time.time()
    page.wait_for_function("window.ziapp.follow.on===false", timeout=20000, polling=100)
    check("T1 第二轮后放行不卡死", True, f"{time.time()-t_retry:.1f}s")
    check("T1 按钮解禁", not page.locator(".learn-card > button.btn.big").is_disabled())
    rows = follow_rows()
    check("T1 流水 heard=0/tries=1", rows == [(ch, "char", 0, 1)], rows)

    # ---- T0 间歇声源（词步 word）：读到路径 ----
    page.click(".learn-card > button.btn.big")
    page.wait_for_function("window.ziapp.ncStep===2", timeout=8000, polling=100)
    page.evaluate(inject_stream(voice=True))       # 覆盖静音流
    t_word = time.time()
    page.wait_for_function("window.ziapp.followPop===true", timeout=25000, polling=50)
    check("T0 读到→✓爆闪", True, f"{time.time()-t_word:.1f}s")
    page.wait_for_function("window.ziapp.follow.on===false", timeout=40000, polling=100)
    total = time.time() - t_word
    check("T0 词跟读全部读到推进", total < 40, f"{total:.1f}s")
    w = [r for r in follow_rows() if r[1] == "word"]
    check("T0 流水 word×4 heard=1/tries=0", len(w) == 4 and all(r[2] == 1 and r[3] == 0 for r in w), w)
    page.screenshot(path=str(SHOTS / "fix-t0-word-done.png"), full_page=True)

    # ---- T2 挂起的 getUserMedia（句步 sent）：原硬卡死路径 ----
    page.evaluate("""() => {   // 废掉声源流→ensureMic 必须走 getUserMedia(已 wrap 成永久挂起)
      const app = window.ziapp;
      if (app._micStream) app._micStream.getTracks().forEach(t => t.stop());
      app._micStream = null; app._micCtx = null; app._micFailUntil = 0; app.micDenied = false;
    }""")
    page.click(".learn-card > button.btn.big")
    page.wait_for_function("window.ziapp.ncStep===3", timeout=8000, polling=100)
    t_sent = time.time()
    page.wait_for_function("window.ziapp.follow.on===true", timeout=20000, polling=100)
    page.wait_for_function("window.ziapp.micDenied===true", timeout=10000, polling=100)
    check("T2 挂起 getUserMedia 3s 超时→micDenied", True, f"{time.time()-t_sent:.1f}s")
    page.wait_for_function("window.ziapp.follow.on===false", timeout=25000, polling=100)
    total = time.time() - t_sent
    check("T2 句跟读全部降级推进不卡死", total < 25, f"{total:.1f}s")
    s = [r for r in follow_rows() if r[1] == "sent"]
    check("T2 流水 sent heard=NULL(降级三态)", len(s) >= 1 and all(r[2] is None for r in s), s)
    page.screenshot(path=str(SHOTS / "fix-t2-after-sent.png"), full_page=True)


if __name__ == "__main__":
    errors, v18 = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required",
                  "--use-fake-device-for-media-stream",
                  "--use-fake-ui-for-media-stream"])
        page = browser.new_page(viewport={"width": 820, "height": 1100})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: v18.append(r.status) if "app.js?v=18" in r.url else None)
        try:
            run(page)
        finally:
            browser.close()
    check("T3 app.js?v=18 加载 200", v18 == [200], v18)
    check("T3 无 JS pageerror", not errors, errors[:3])
    print(f"\n{PASS_N} pass / {FAIL_N} fail  （截图: tests/shots/fix-*.png）")
    sys.exit(1 if FAIL_N else 0)
