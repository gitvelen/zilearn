#!/usr/bin/env python3
"""v60 毕业测前端走查（playwright）：intro 文案 / 三 slot 题序 / py-wf-defi 渲染 /
wf 全对与误点≥2 判错路径 / 结算三行统计。本地 8101 + test 账号，自包含造态。

用法: 先起本地服务（app 目录 uvicorn main:app --port 8101），再 python3 tests/grad_walkthrough_test.py
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
DB = ROOT / "app/data/zilearn.db"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8101"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def setup_grad_state(n_chars=3):
    """test 账号毕业测态：plan_start=今天-6，6 活动日，本期 n 字，grad_pending=1。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    uid = con.execute("SELECT id FROM users WHERE username='test'").fetchone()["id"]
    for t in ("user_hanzi", "quiz_log", "stars", "weekly_reports", "confirm_pick"):
        con.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    chars = [r["id"] for r in con.execute(
        "SELECT h.id FROM hanzi h WHERE h.book='1a' ORDER BY h.seq LIMIT ?", (n_chars,))]
    days = [(date.today() - timedelta(days=6 - i)).isoformat() for i in range(6)]
    con.execute("""UPDATE users SET grade='1A', target_chars=?, plan_days=7, plan_start=?,
                  plan_base_learned=0, onboarded=1, grad_pending=1, grad_anchor=?, day_offset=0
                  WHERE id=?""", (n_chars, days[0], days[0], uid))
    for i, hid in enumerate(chars[:n_chars]):
        con.execute("INSERT INTO user_hanzi(user_id,hanzi_id,status,stage,next_date,first_date) "
                    "VALUES(?,?,'learning',1,NULL,?)", (uid, hid, days[i % 6]))
        con.execute("INSERT INTO quiz_log(user_id,hanzi_id,phase,quiz_type,correct,ts) "
                    "VALUES(?,?,'learn','card',1,?)", (uid, hid, days[i % 6]))
    for i in range(n_chars, 6):                     # 补足 6 活动日
        con.execute("INSERT INTO quiz_log(user_id,hanzi_id,phase,quiz_type,correct,ts) "
                    "VALUES(?,?,'review','listen',1,?)", (uid, chars[0], days[i]))
    con.commit()
    con.close()


def login(page):
    page.goto(BASE)
    page.fill("input[placeholder='用户名']", "test")
    page.fill("input[placeholder='密码']", "test123")
    page.click("button:has-text('开 始')")
    page.wait_for_selector("text=毕业", timeout=8000)


def wait_quiz(page):
    """等当前题就绪（advance→enterItem→拉题间隙 curQuiz 为 null）"""
    page.wait_for_function("ziapp.curQuiz && !ziapp.quizRevealed", timeout=10000)


def answer_current(page):
    """答对当前题（真实点击路径）：wf 点全部伙伴字块，其余点答案选项。返回题型。"""
    if page.locator(".wf-tile").count() > 0:
        for ch in page.evaluate("ziapp.curQuiz.answer"):
            page.click(f".wf-tile:has-text('{ch}')")
            page.wait_for_timeout(150)
        return "wf"
    qt = "defi" if page.locator(".speak-xl").count() > 0 else "py"
    ans = page.evaluate("ziapp.curQuiz.answer")
    page.click(f".opt:has-text('{ans}')")
    return qt


def run_correct_path(browser):
    print("== 走查 A：全对路径 ==")
    setup_grad_state(3)
    page = browser.new_context().new_page()
    login(page)
    body = page.inner_text("body")
    check("intro 毕业大挑战 + 题量 9", "本期毕业大挑战" in body and "9 道题" in body, body[:200])
    check("intro 三维度提示", "认读音" in body and "找朋友" in body and "听意思" in body)
    page.click("button:has-text('开始')", force=True)
    page.wait_for_selector(".sess-label", timeout=8000)
    check("会话标签=🎓 毕业大挑战", "毕业" in page.inner_text(".sess-label"))
    qt_seen = set()
    for _ in range(9):
        qt = answer_current(page)
        qt_seen.add(qt)
        page.wait_for_timeout(1300)                 # 答对 900ms advance
    check("三维度都出现", qt_seen == {"py", "wf", "defi"}, qt_seen)
    page.wait_for_selector("text=真正掌握", timeout=10000)
    body = page.inner_text("body")
    check("结算字级掌握 + 三维度", "真正掌握" in body and "3 个字" in body
          and "认读音" in body and "找朋友" in body and "听意思" in body, body[:400])
    check("结算满分 9 题", "9 / 9" in body, body[:300])


def run_wrong_path(browser):
    """wf 误点≥2 判错 → 半卡 → 同题型重测到对；中途退出续做（localStorage id:slot）。"""
    print("== 走查 B：wf 误点判错路径 ==")
    setup_grad_state(2)                             # 2 字 6 题，缩短路径
    ctx = browser.new_context()
    page = ctx.new_page()
    login(page)
    page.click("button:has-text('开始')", force=True)
    page.wait_for_selector(".sess-label", timeout=8000)
    # 推进到第一道 wf（slot1 轮）
    n = 0
    while n < 5:
        wait_quiz(page)
        if page.locator(".wf-tile").count() > 0:
            break
        answer_current(page)
        page.wait_for_timeout(1300)
        n += 1
    check("出现 wf 题", page.locator(".wf-tile").count() > 0)
    # 误点 2 个不同干扰字 → 判错
    ans = set(page.evaluate("ziapp.curQuiz.answer"))
    tiles = page.evaluate("[...document.querySelectorAll('.wf-tile')].map(b=>b.textContent.trim())")
    dis = [t for t in tiles if t not in ans][:2]
    check("干扰字≥2 可误点", len(dis) == 2, (tiles, ans))
    page.click(f".wf-tile:has-text('{dis[0]}')")
    page.wait_for_timeout(500)
    check("首误点不判死（继续作答）", not page.evaluate("ziapp.quizRevealed"))
    page.click(f".wf-tile:has-text('{dis[1]}')")
    page.wait_for_timeout(700)
    check("误点≥2 → 判错 reveal", page.evaluate("ziapp.quizRevealed && !ziapp.quizCorrect"))
    check("错题行=好朋友都点亮", "好朋友都点亮" in page.inner_text("body"))
    check("错字已回炉标记", page.evaluate("ziapp.weeklyWrongIds.length") == 1)
    check("半卡内容挂载", page.evaluate("!!ziapp.curContent"))
    # 中途退出（🏠 长按 1.2s）→ 重进续做（断点 id:slot）
    prog_before = page.evaluate("JSON.parse(localStorage.getItem(ziapp.weeklyKey()))")
    check("进度键含 id:slot", any(":" in str(d) for d in prog_before["done"]), prog_before["done"])
    page.click(".home", force=True)
    page.wait_for_timeout(1500)
    page.goto(BASE)                                 # 重新进入（同 context，localStorage 保留）
    page.wait_for_selector("text=毕业", timeout=8000)
    check("重进提示剩余题数", "还剩" in page.inner_text("body"))
    page.click("button:has-text('开始')", force=True)
    page.wait_for_selector(".sess-label", timeout=8000)
    left = page.evaluate("ziapp.sessQueue.length")
    check("续做队列=总题-已答", left == 6 - len(prog_before["done"]),
          (left, prog_before["done"]))
    # 重测题先做完（同题型重测到对），再答完剩余
    for _ in range(left):
        if page.evaluate("ziapp.quizRevealed"):      # 半卡停留中的重测题：点继续
            page.click("button:has-text('记住了')", force=True)
            page.wait_for_timeout(800)
        wait_quiz(page)
        if page.locator(".wf-tile").count() > 0:
            for ch in page.evaluate("ziapp.curQuiz.answer"):
                page.click(f".wf-tile:has-text('{ch}')")
                page.wait_for_timeout(120)
        else:
            ans1 = page.evaluate("ziapp.curQuiz.answer")
            page.click(f".opt:has-text('{ans1}')")
        page.wait_for_timeout(1300)
    page.wait_for_selector("text=真正掌握", timeout=10000)
    body = page.inner_text("body")
    check("结算页到达（判错字计入未掌握）", "真正掌握" in body and "1 / 2 个字" in body, body[:400])


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/opt/google/chrome/chrome",
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        run_correct_path(browser)
        run_wrong_path(browser)
        browser.close()
    print(f"\n{PASS} pass / {FAIL} fail")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
