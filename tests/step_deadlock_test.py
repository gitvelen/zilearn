#!/usr/bin/env python3
"""断头路回归测试（2026-10-30 词步卡死 RCA 固化）

四步卡/测验每个"等待点"都必须有出路，任何一个永久锁死即断头路：
  1. 认步范读锁定期点大字（playChar→stopNarration 作废链）→ 按钮不得永久禁用
  2. 词步整体连听期点词卡（playOne→stopNarration）→ 同上（用户实报场景）
  3. quiz 拉取失败（弱网）→ 1.2s 自动跳过推进，不卡"出题中…"
  4. 答对后 submit 失败（弱网）→ 仍按 900ms 节奏推进
根因：stepAudio 的 finally 代际守卫在链被作废时不清 stepBusy，而无新链接手；
修复：stopNarration 同步交还锁 + 推进路径 api 全部兜底。
用法: python3 tests/step_deadlock_test.py [base_url]
"""
import sys
import time
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
    conn = sqlite3.connect("/root/zilearn/app/data/zilearn.db")
    uid = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()[0]
    conn.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL,
                    plan_start=NULL, onboarded=0 WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    conn.commit(); conn.close()


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
    page.click("button.btn.big")
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

    # ---- 场景1/2：范读锁定期打断 ----
    onboard_to_session(page)
    page.wait_for_function("window.ziapp.ncStep===1 && window.ziapp.stepBusy===true", timeout=8000)
    page.click(".zi-big")                                # 认步范读中点大字
    page.wait_for_timeout(2000)
    st = page.evaluate("() => ({busy: ziapp.stepBusy, dis: document.querySelector('.learn-card .btn.big').disabled})")
    check("认步范读期点大字后按钮解锁", not st["busy"] and not st["dis"], str(st))

    page.evaluate("() => ziapp.ncGo(2)")
    page.wait_for_function("window.ziapp.ncStep===2 && window.ziapp.stepBusy===true", timeout=8000)
    page.locator(".word-chip.big").first.click()          # 词步整体连听中点词卡（用户实报场景）
    page.wait_for_timeout(2000)
    st = page.evaluate("() => ({busy: ziapp.stepBusy, dis: document.querySelector('.learn-card .btn.big').disabled})")
    check("词步连听期点词卡后按钮解锁", not st["busy"] and not st["dis"], str(st))
    page.evaluate("() => document.querySelector('.learn-card .btn.big').click()")
    page.wait_for_timeout(800)
    check("解锁后下一步可推进到句步", page.evaluate("() => ziapp.ncStep") == 3)

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


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
        args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    run(page)
    browser.close()

print(f"\n{PASS_N} pass / {FAIL_N} fail")
sys.exit(1 if FAIL_N else 0)
