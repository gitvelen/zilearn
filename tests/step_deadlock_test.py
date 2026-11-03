#!/usr/bin/env python3
"""断头路回归测试（2026-10-30 词步卡死 RCA 固化；2026-11-03 音频时钟化后重写）

核心不变量（音频时钟化）：任何按钮 disabled 的解除只依赖定时器或孩子操作，
永不依赖音频事件。每个"等待点"都必须有出路：
  1. 音频全挂（fetch 全 abort）→ narrLock 仍按 cap 定时器解锁、跟读槽照常开合、
     会话完整走完不卡死（v43"stepBusy 永锁"类的根除验证）
  2. 范读期乱点（大字/词卡=守卫 no-op）→ 不作废链、不产生新断头路
  3. quiz 拉取失败（弱网）→ 1.2s 自动跳过推进，不卡"出题中…"
  4. 答对后 submit 失败（弱网）→ 仍按 900ms 节奏推进
用法: python3 tests/step_deadlock_test.py [base_url]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8300"
PASS_N = FAIL_N = 0


def check(name, cond, detail=""):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"  ✓ {name}")
    else:
        FAIL_N += 1
        print(f"  ✗ {name}  {detail}")


def reset_test_user():
    import sqlite3
    conn = sqlite3.connect(str(Path(__file__).parent.parent / "app" / "data" / "zilearn.db"))
    uid = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()[0]
    conn.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL,
                    plan_start=NULL, plan_base_learned=0, confirm_upto=0, onboarded=0,
                    day_offset=0 WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports",
              "follow_log", "daily_mail"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


def onboard_to_session(page):
    page.goto(BASE, wait_until="networkidle")
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".wizard", timeout=10000)
    page.evaluate("() => { window.ziapp.followHeard = (ms, gen) => new Promise(r => setTimeout(() => r(true), 150)); }")
    page.click(".grade:first-child")
    page.click("button.btn.big")
    page.wait_for_timeout(400)
    page.fill("input[type=number]", "52")
    page.locator(".chip", has_text="30 天").click()
    page.click("button.btn.big")
    page.wait_for_selector(".char-grid", timeout=10000)
    page.click("button.btn.big")                     # 选完了，开始学（验证轮已砍）
    page.wait_for_function("window.ziapp.phase==='today-intro'", timeout=10000)
    page.evaluate("() => document.querySelector('button.btn.big').click()")
    page.wait_for_function("window.ziapp.sessQueue.length>0 && window.ziapp.curItem", timeout=10000)
    for _ in range(60):
        if page.evaluate("() => ziapp.curItem && ziapp.curItem.type === 'newcard'"):
            return
        page.evaluate("() => ziapp.advance()")
        page.wait_for_timeout(300)
    raise RuntimeError("未见 newcard")


def to_next_quiz(page):
    for _ in range(60):
        it = page.evaluate("() => ziapp.curItem && ziapp.curItem.type")
        if it and it.endswith("quiz"):
            return
        page.evaluate("() => ziapp.advance()")
        page.wait_for_timeout(300)
    raise RuntimeError("未见 quiz")


def run(page):
    reset_test_user()

    # ---- 场景1：音频全挂（fetch 全 abort）——时钟化核心不变量 ----
    onboard_to_session(page)
    page.route("**/static/audio/**", lambda r: r.abort())
    t0 = page.evaluate("() => performance.now()")
    page.wait_for_function("window.ziapp.narrLock===true", timeout=8000)
    check("音频全挂时范读锁仍置位（链不卡）", True)
    page.wait_for_function("window.ziapp.narrLock===false", timeout=14000, polling=100)   # cap 12s 兜底
    gap = (page.evaluate("() => performance.now()") - t0) / 1000
    check("音频全挂 narrLock 按 cap 解锁(≤13s)", gap <= 13, f"{gap:.1f}s")
    page.wait_for_function("window.ziapp.follow.on===true", timeout=8000)      # 槽照常开（范读失败不挡槽）
    page.wait_for_function("window.ziapp.follow.on===false", timeout=15000)    # stub 150ms 读到 → 收槽
    check("音频全挂跟读槽照常开合", True)
    check("槽后按钮解禁", not page.locator(".learn-card > button.btn.big").is_disabled())
    page.unroute("**/static/audio/**")

    # ---- 场景2：范读期乱点=守卫 no-op（不作废链、不新增断头路） ----
    page.evaluate("() => ziapp.ncGo(1)")            # 重进认步复现范读期
    page.wait_for_function("window.ziapp.narrLock===true", timeout=8000)
    page.click(".zi-big")                            # 范读中点大字（守卫应忽略）
    page.wait_for_timeout(2000)
    st = page.evaluate("() => ({nc: ziapp.ncStep, lock: ziapp.narrLock})")
    check("范读期点大字不跳步不炸链", st["nc"] == 1, str(st))
    page.wait_for_function("window.ziapp.follow.on===true", timeout=12000)     # 链照常进槽
    check("乱点后跟读槽照常启动", True)

    # ---- 场景3/4：弱网断头 ----
    to_next_quiz(page)
    page.route("**/api/quiz*", lambda r: r.abort())
    idx = page.evaluate("() => ziapp.sessIdx")
    page.evaluate("() => ziapp.advance()")
    page.wait_for_function(f"window.ziapp.sessIdx >= {idx+2}", timeout=6000, polling=200)  # 1.2s 跳过后自动 advance
    check("quiz拉取失败自动跳过推进", True)
    page.unroute("**/api/quiz*")

    to_next_quiz(page)
    page.wait_for_function("window.ziapp.curQuiz && ziapp.curQuiz.hanzi_id===ziapp.curItem.id", timeout=10000)
    page.route("**/api/submit*", lambda r: r.abort())
    idx = page.evaluate("() => ziapp.sessIdx")
    q = page.evaluate("() => ziapp.curQuiz")
    page.evaluate("""(q) => { for (const b of document.querySelectorAll('.opts .opt'))
        if (b.textContent.trim() === q.answer) b.click(); }""", q)
    page.wait_for_function(f"window.ziapp.sessIdx >= {idx+1}", timeout=6000, polling=200)
    check("答对后submit失败仍推进", True)
    page.unroute("**/api/submit*")

    reset_test_user()   # teardown


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
        args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    run(page)
    browser.close()

print(f"\n{PASS_N} pass / {FAIL_N} fail")
sys.exit(1 if FAIL_N else 0)
