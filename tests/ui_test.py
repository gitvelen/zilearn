#!/usr/bin/env python3
"""zilearn UI 全流程走查（playwright headless，截图 + DOM 断言）

覆盖：登录/引导(年级·计划·摸底·验证)/学习日(四步卡·跳步·三类题·find·今日朗读卡)
     /完成页/家长页/周测(含错题半卡回看再重测)
用法: python3 tests/ui_test.py [base_url]   # 需本地服务已起
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
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


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def wait_sess(page, idx, timeout=15000):
    page.wait_for_function(f"window.ziapp && window.ziapp.sessIdx >= {idx}", timeout=timeout, polling=100)


def wait_quiz(page, timeout=10000):
    """等当前题就绪且与当前 item 同字（防 advance 窗口期拿到旧题）"""
    page.wait_for_function(
        "window.ziapp && window.ziapp.curQuiz && window.ziapp.curItem "
        "&& window.ziapp.curQuiz.hanzi_id === window.ziapp.curItem.id",
        timeout=timeout, polling=100)


def app(page):
    return page.evaluate("() => window.ziapp && {i: ziapp.sessIdx, type: ziapp.curItem && ziapp.curItem.type, phase: ziapp.phase, nc: ziapp.ncStep}")


AUDIO_HOOK = """
(() => {
  if (window.__audioHooked) return; window.__audioHooked = true;
  window.__audioEvents = [];
  const PLAY = Audio.prototype.play;
  Audio.prototype.play = function () {          /* 全程记录音频播放区间，检测长音频两两重叠 */
    const m = /([^/?]+)\\.(mp3|wav)$/.exec(this.src);
    const ev = { name: m ? decodeURIComponent(m[1]) : this.src, start: performance.now(), end: null };
    window.__audioEvents.push(ev);
    const close = () => { if (ev.end == null) ev.end = performance.now(); };
    this.addEventListener("ended", close, { once: true });
    this.addEventListener("pause", close, { once: true });
    this.addEventListener("error", close, { once: true });
    const p = PLAY.call(this); p.catch(close);
    return p;
  };
  window.__audioOverlaps = () => {              /* sfx_ 短音效豁免；>400ms 相交判叠 */
    const evs = window.__audioEvents.filter(e => e.end != null && !e.name.startsWith("sfx_"));
    const bad = [];
    for (let i = 0; i < evs.length; i++) for (let j = i + 1; j < evs.length; j++) {
      const a = evs[i], b = evs[j];
      const s = Math.max(a.start, b.start), e = Math.min(a.end, b.end);
      if (e - s > 400) bad.push(a.name + " × " + b.name + " " + Math.round(e - s) + "ms");
    }
    return bad;
  };
})();
"""


def check_no_overlap(page, label):
    bad = page.evaluate("() => window.__audioOverlaps ? window.__audioOverlaps() : []")
    check(label, not bad, "; ".join(bad[:5]))


def follow_wait(page, state, timeout=60000):
    """等待跟读状态机到达指定状态（on/off）"""
    page.wait_for_function(f"window.ziapp.follow.on==={'true' if state else 'false'}", timeout=timeout)


def follow_skip(page):
    """家长长按跳过跟读（真实事件路径，防 evaluate 绕过 UI 的教训）"""
    btn = page.locator(".follow-bar .skip-link").first
    btn.dispatch_event("pointerdown")
    page.wait_for_timeout(1700)
    btn.dispatch_event("pointerup")


def answer_quiz(page):
    """答对当前题：四选一点答案；find 依次点出全部目标字。
    若误答错（动画期间点击偏差等），走"记住了继续"再重答，保证推进。"""
    for attempt in range(3):
        q = page.evaluate("() => window.ziapp.curQuiz")
        if not q:
            return None
        if q["quiz_type"] == "find":
            page.wait_for_selector(".find-line", timeout=8000)
            q = page.evaluate("() => window.ziapp.curQuiz")   # 以渲染后的最新题为准
            lines = page.locator(".find-line")
            for li, line in enumerate(q["lines"]):
                tiles = lines.nth(li).locator(".find-tile")
                for ti, ch in enumerate(line):
                    if ch == q["hanzi"]:
                        tiles.nth(ti).click()
                        page.wait_for_timeout(250)
        else:
            btns = page.locator(".opts .opt")
            for i in range(btns.count()):
                if btns.nth(i).inner_text().strip() == q["answer"]:
                    btns.nth(i).click()
                    break
        try:
            page.wait_for_function("window.ziapp.quizRevealed === true", timeout=8000, polling=100)
        except Exception:
            continue
        if page.evaluate("() => window.ziapp.quizCorrect"):
            return q
        # 答错：点"记住了，继续"（learnquiz/wrong-note）后重试
        note = page.locator(".wrong-note .btn")
        if note.count():
            note.first.click()
            page.wait_for_timeout(800)
    return q


def reset_test_user():
    """幂等：每轮走查前清 test 用户状态（同 e2e reset_user 模式）"""
    import sqlite3
    conn = sqlite3.connect("/root/zilearn/app/data/zilearn.db")
    uid = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()[0]
    conn.execute("""UPDATE users SET grade=NULL, target_chars=NULL, plan_days=NULL,
                    plan_start=NULL, onboarded=0 WHERE id=?""", (uid,))
    for t in ("user_hanzi", "confirm_pick", "quiz_log", "stars", "weekly_reports", "follow_log"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


def run(page):
    reset_test_user()
    # ---- 登录 ----
    page.goto(BASE, wait_until="networkidle")
    check("登录页", page.locator(".login-card").count() == 1)
    shot(page, "01-login")
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".wizard", timeout=10000)
    # 模拟孩子跟读出声：stub 音量传感器输入（headless fake-audio 管线刷新率 ~2Hz，
    # 物理驱动不可行；stub 后端到端验证 读到→✓→夸奖→解锁 全链路，判定逻辑靠 review）。
    # 300ms 延迟=孩子开口时长：让"wait 态"可观察（聆听指示器断言+截图），又不打断推进
    page.evaluate("""() => { window.ziapp.followHeard = (ms, gen) =>
        new Promise(r => setTimeout(() => r(true), 300)); }""")

    # ---- 引导：年级 ----
    check("年级页(1A-5B共10)", page.locator(".grade").count() == 10)
    page.click(".grade:first-child")            # 1A
    page.click("button.btn.big")
    page.wait_for_timeout(600)

    # ---- 计划：52 字 / 30 天（daily_new=ceil(52/26)=2，流程可控） ----
    check("计划页", page.locator(".field").count() >= 1)
    page.fill("input[type=number]", "52")
    page.locator(".chip", has_text="30 天").click()
    page.click("button.btn.big")
    page.wait_for_selector(".char-grid", timeout=10000)
    check("摸底分批表格", page.locator(".char-chip").count() >= 50)
    shot(page, "02-confirm")

    # ---- 摸底：选前 2 个字（天地） ----
    page.locator(".char-chip").nth(0).click()
    page.locator(".char-chip").nth(1).click()
    page.click("button.btn.big")                 # 选完了去验证
    page.wait_for_selector(".quiz-box", timeout=10000)

    # ---- 验证 2 题（答案从 app 状态取；等指令音后自动播字音再作答，防 Q2 指令叠 Q1 字音尾） ----
    for k in range(2):
        ans = page.evaluate("() => window.ziapp.verifyQ && window.ziapp.verifyQ.answer")
        check(f"验证题 {k + 1} 生成", bool(ans))
        ch = page.evaluate("() => window.ziapp.verifyQ.hanzi")
        page.wait_for_function(f"window.__audioEvents.some(e => e.name === {ch!r})", timeout=8000)
        page.wait_for_timeout(500)
        btns = page.locator(".opts .char-opt")
        for i in range(btns.count()):
            if btns.nth(i).inner_text().strip() == ans:
                btns.nth(i).click()
                break
        page.wait_for_timeout(1600)              # 700ms 提交 + 换题
    page.wait_for_selector(".day-card", timeout=10000)

    # ---- 今日开始页 ----
    txt = page.locator(".day-card").inner_text()
    check("今日页新字 2 个", "新字 2 个" in txt, txt)
    shot(page, "03-today")
    page.click("button.btn.pulse", force=True)

    # ---- 新字 1：四步卡 ----
    page.wait_for_selector(".learn-card", timeout=10000)
    st = app(page)
    check("四步卡步1(认)", st["type"] == "newcard" and st["nc"] == 1)
    check("步指示器 4 点", page.locator(".nc-dot").count() == 4)
    check("字源故事显示", page.locator(".etym-story").count() == 1)
    check("多音字区隐藏/正常", page.locator(".learn-card .zi-big").inner_text().strip()[0] in ("天", "地", "人"))
    shot(page, "04-nc1-recite")

    # ---- 跟读（认步·节奏式）：字音→范读(叮)→留白听读→✓→解锁 ----
    follow_wait(page, True, timeout=15000)
    page.wait_for_function("window.ziapp.follow.state==='wait'", timeout=15000)
    check("跟读中(认·大字高亮+聆听指示器+按钮锁)", page.locator(".zi-big.follow-cur").count() == 1
          and page.locator(".listen-orb.wait").count() == 1
          and page.locator(".learn-card > button.btn.big").is_disabled())
    shot(page, "04b-follow-wait")
    follow_wait(page, False, timeout=20000)      # 留白+确认读+夸奖
    check("跟读完成解锁(认)", not page.locator(".learn-card > button.btn.big").is_disabled())
    shot(page, "04c-follow-done")
    import sqlite3 as _s
    _c = _s.connect("/root/zilearn/app/data/zilearn.db")
    _n = _c.execute("SELECT COUNT(*) FROM follow_log WHERE user_id="
                    "(SELECT id FROM users WHERE username='test')").fetchone()[0]
    _c.close()
    check("跟读开口流水已上报(R5)", _n >= 1, _n)
    ch1 = page.evaluate("() => window.ziapp.curItem.char")
    seq = page.evaluate("() => window.__audioEvents.map(e => e.name)")
    seqf = [n for n in seq if not n.startswith("sfx_")]
    check("验证轮音频链(指令→字音 ×2题)", seqf[:4] == ["t_verify", "天", "t_verify", "地"], seqf[:6])
    expect = [ch1, ch1, ch1, "t_great", "e_" + ch1]   # 字音→范读→(叮)确认读→夸→故事（sfx 已滤；无 t_gate_read）
    i0 = seqf.index(ch1)                              # 认步字音起点（跳过摸底验证轮音频）
    check("认步音频链(字音→范读→确认→夸奖→故事,读到才夸)", seqf[i0:i0 + 5] == expect, seqf[i0:i0 + 6])

    page.click(".learn-card > button.btn.big")   # 下一步 → 词（连听→逐词跟读）
    follow_wait(page, True, timeout=20000)
    check("步2(词)词卡≥2", page.locator(".word-chip.big").count() >= 2)
    check("词内目标字高亮", page.locator(".word-chip.big b").count() >= 2)
    check("词跟读当前词高亮其余变暗", page.locator(".word-chip.big.cur").count() == 1
          and page.locator(".word-chip.big.dim").count() >= 1)
    shot(page, "05-nc2-words")
    follow_wait(page, False, timeout=60000)      # 逐词全部完成
    page.click(".learn-card > button.btn.big")   # → 句（逐句跟读）

    follow_wait(page, True, timeout=10000)
    check("步3(句)例句≥2", page.locator(".sentence.pick").count() >= 2)
    check("句内目标字高亮", page.locator(".sentence.pick b").count() >= 2)
    check("句跟读当前句高亮", page.locator(".sentence.pick.cur").count() == 1)
    shot(page, "06-nc3-sentences")
    follow_wait(page, False, timeout=90000)      # 逐句全部完成（句留白=范读时长+2.5s）
    page.click(".learn-card > button.btn.big")   # → 写
    page.wait_for_timeout(1200)                  # HanziWriter 动画挂载
    check("步4(写)描红挂载", page.locator(".hanzi-wrap svg").count() >= 1)
    check("写步按钮文案(还有下一字→'下一个')", "下一个" in page.locator(".learn-card > button.btn.big").inner_text(),
          page.locator(".learn-card > button.btn.big").inner_text())
    shot(page, "07-nc4-trace")

    # ---- 新字2：家长长按跳过认步跟读（真实事件路径）→ while 循环内跳步直达写 ----
    page.click(".learn-card > button.btn.big")   # 新字1 步4 完成 → 新字2
    follow_wait(page, True, timeout=15000)
    follow_skip(page)
    follow_wait(page, False, timeout=5000)
    check("家长长按跳过跟读(新字2认步)", not page.locator(".learn-card > button.btn.big").is_disabled())
    quiz_seq, gates_seen = [], 0
    while gates_seen < 6:
        st = app(page)
        if st["type"] == "newcard":
            # 新字2：长按跳步直达"写"（家长快进；nc-dot 长按化防孩子误触）
            dot = page.locator(".nc-dot").nth(3)
            dot.dispatch_event("pointerdown")
            page.wait_for_timeout(900)
            dot.dispatch_event("pointerup")
            page.wait_for_timeout(1200)
            check("跳步直达写(描红挂载)", page.locator(".hanzi-wrap svg").count() >= 1
                  and app(page)["nc"] == 4)
            check("末字写步按钮文案('去答题')", "去答题" in page.locator(".learn-card > button.btn.big").inner_text(),
                  page.locator(".learn-card > button.btn.big").inner_text())
            shot(page, "09-nc-skip-trace")
            page.click(".learn-card > button.btn.big")
            page.wait_for_function(
                "window.ziapp.curItem && (window.ziapp.curItem.type === 'gatequiz' "
                "|| window.ziapp.curItem.type === 'gatesummary')", timeout=10000, polling=100)
            continue
        if st["type"] == "strokegate":
            page.wait_for_timeout(1200)              # HanziWriter 动画挂载
            check("写字关描红挂载", page.locator(".stroke-card .hanzi-wrap svg").count() >= 1)
            check("写字关未写完按钮锁", page.locator(".stroke-card > button.btn.big").is_disabled())
            shot(page, "10b-stroke-gate")
            # 家长长按跳过（真实路径：skipHold 返回值驱动 traceDone，勿用 evaluate 绕过）
            skip = page.locator(".stroke-card .skip-link")
            skip.dispatch_event("pointerdown")
            page.wait_for_timeout(1700)
            skip.dispatch_event("pointerup")
            page.wait_for_timeout(200)
            check("家长长按跳过解锁按钮", not page.locator(".stroke-card > button.btn.big").is_disabled())
            page.evaluate("() => { window.ziapp.gateScores[6] = 1; }")   # 保持 1/1 结算断言
            page.click(".stroke-card > button.btn.big")
            page.wait_for_timeout(600)
            continue
        if st["type"] == "gatesummary":
            gates_seen += 1
            want = "1 / 1" if gates_seen == 6 else "2 / 2"
            txt = page.locator(".gate-card .score-xl").inner_text()
            check(f"第{gates_seen}关结算 {want}", txt == want, txt)
            shot(page, f"10-gate{gates_seen}-summary")
            page.locator(".gate-card .btn.big").click()
            page.wait_for_timeout(400)
            continue
        wait_quiz(page)
        qt = page.evaluate("() => window.ziapp.curQuiz.quiz_type")
        quiz_seq.append(qt)
        if len(quiz_seq) == 1:
            shot(page, "08-gate1-listen")
        answer_quiz(page)
        page.wait_for_timeout(1400)
    check(f"五关题序 listen,pin,mean,rev×2,sent|find×2，写字关1题另计（{quiz_seq}）",
          quiz_seq[0:2] == ["listen", "listen"] and quiz_seq[2:4] == ["pin", "pin"]
          and quiz_seq[4:6] == ["mean", "mean"] and quiz_seq[6:8] == ["rev", "rev"]
          and sorted(quiz_seq[8:10]) == ["find", "sent"], quiz_seq)
    check("两字共 10 题", len(quiz_seq) == 10, quiz_seq)

    # ---- 今日朗读卡（收尾：指令→逐句跟读→自由朗读） ----
    st = app(page)
    check("朗读卡出现", st["type"] == "readcard", st)
    follow_wait(page, True, timeout=30000)      # t_readcard 指令(~7s) + 首句范读
    n_lines = page.locator(".read-line").count()
    check("朗读行数=今日新字数", n_lines == 2, n_lines)
    check("朗读行目标字高亮", page.locator(".read-line b").count() >= 2)
    check("朗读卡逐句跟读当前行高亮", page.locator(".read-line.cur").count() == 1)
    shot(page, "11-readcard")
    follow_wait(page, False, timeout=90000)     # 逐句全部完成
    check("朗读完成转自由模式(连读出现)", "连读" in page.locator(".btn.ghost").first.inner_text())
    page.locator(".btn.big").last.click()

    # ---- 完成页 → 家长页 ----
    page.wait_for_selector(".done-stats", timeout=10000)
    check("完成页统计", "新学 2 个字" in page.locator(".done-stats").inner_text())
    shot(page, "11-done")
    page.click("button.btn.big")                 # 给爸爸妈妈看成绩
    page.wait_for_selector(".parent-grid", timeout=10000)
    check("家长页卡片", page.locator(".p-card").count() >= 4)
    shot(page, "12-parent")
    check_no_overlap(page, "零叠音(学习日全程)")   # 不变量：四步卡/六关/朗读卡任何两路长音频不重叠

    print("== UI 阶段1（学习日）完成 ==")


def run_weekly(page):
    """插 6 个历史活动日流水（调度为学习日驱动）→ 今天为第 7 个学习日=周测日；走周测+错题半卡"""
    import sqlite3
    from datetime import date, timedelta
    db = sqlite3.connect("/root/zilearn/app/data/zilearn.db")
    uid = db.execute("SELECT id FROM users WHERE username='test'").fetchone()[0]
    # 用当日已学字插过去 6 个活动日的流水（correct=1 不进周测错题 scope，已学字不进新字池）
    hid = db.execute("""SELECT q.hanzi_id FROM quiz_log q
                        WHERE q.user_id=? AND q.phase='learn' LIMIT 1""", (uid,)).fetchone()[0]
    for i in range(6, 0, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        db.execute("INSERT INTO quiz_log(user_id, hanzi_id, phase, quiz_type, correct, ts) "
                   "VALUES(?,?,?,?,1,?)", (uid, hid, "review", "listen", d))
    db.commit()
    db.close()

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector(".day-card", timeout=10000)
    check("周测日生效", "周大挑战" in page.locator(".day-card").inner_text())
    shot(page, "13-weekly-intro")
    page.click("button.btn.pulse", force=True)
    page.wait_for_selector(".quiz-card", timeout=10000)

    # 第 1 题故意答错 → 验证错题半卡（M4）→ 重测到对
    wait_quiz(page)                                 # quiz-card 先渲染、题后到，防时序取 null
    q = page.evaluate("() => window.ziapp.curQuiz")
    if q["quiz_type"] == "find":
        tiles = page.locator(".find-tile")          # 连点 2 个非目标字 → 判错
        misses = 0
        for i in range(tiles.count()):
            if tiles.nth(i).inner_text().strip() != q["hanzi"]:
                tiles.nth(i).click()
                page.wait_for_timeout(300)
                misses += 1
                if misses == 2:
                    break
    else:
        wrong = next(o for o in q["options"] if o != q["answer"])
        btns = page.locator(".opts .opt")
        for i in range(btns.count()):
            if btns.nth(i).inner_text().strip() == wrong:
                btns.nth(i).click()
                break
    page.wait_for_timeout(1000)
    check("错题半卡出现(词)", page.locator(".wrong-card .word-chip").count() >= 1)
    check("错题半卡出现(句)", page.locator(".wrong-card .wc-line").count() >= 2)
    shot(page, "14-weekly-wrongcard")
    page.click(".wrong-note .btn")               # 记住了，继续 → 同题型重测
    page.wait_for_timeout(1000)
    answer_quiz(page)                            # 重测答对
    page.wait_for_timeout(1400)

    # 断点续做：第 1 题首答完成后退出重进 → 显示剩余、不从头再来（home 长按化防误触）
    home = page.locator(".home")
    home.dispatch_event("pointerdown")
    page.wait_for_timeout(1300)
    home.dispatch_event("pointerup")
    page.wait_for_selector(".day-card", timeout=10000)
    check("周测中断重进显示剩余", "还剩 1 道题" in page.locator(".day-card").inner_text())
    page.click("button.btn.pulse", force=True)
    page.wait_for_selector(".quiz-card", timeout=10000)

    # 剩余周测题全答对
    while page.evaluate("() => window.ziapp.phase") == "weekly" and \
            page.evaluate("() => window.ziapp.curItem && window.ziapp.curItem.type === 'weeklyquiz'"):
        answer_quiz(page)
        page.wait_for_timeout(1400)

    page.wait_for_selector(".weekly-done, .done-stats", timeout=10000)
    shot(page, "15-weekly-done")
    check("周测结算页", page.locator(".score-xl").count() == 1)
    check("周测断点分数累计保留", page.locator(".score-xl").inner_text().strip() == "1 / 2",
          page.locator(".score-xl").inner_text())
    check_no_overlap(page, "零叠音(周测全程)")     # goto 重进前取（重进后事件重新计）

    # 周测完成后重进：显示完成态、无开始按钮（防无限重刷星星）
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector(".day-card", timeout=10000)
    check("周测完成后重进不重开", "已经完成" in page.locator(".day-card").inner_text()
          and page.locator("button.btn.pulse").count() == 0)
    check("次日入口预告", "第 2 周" in page.locator(".day-card").inner_text())

    reset_test_user()   # teardown：清测试状态，防污染线上库（拨日历时代的教训）


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required",   # AudioContext 不被 suspend（analyser 全 0 根因）
                  "--use-fake-device-for-media-stream",   # 假麦克风：跟读音量校验测试
                  "--use-fake-ui-for-media-stream",       # 自动授权 getUserMedia
                  "--use-file-for-fake-audio-cycles="     # 注入模拟说话声（RMS≈0.3）：跟读走"读到"达标路径
                  "/root/zilearn/tests/fake_mic.wav"])
        page = browser.new_page(viewport={"width": 820, "height": 1100})
        page.add_init_script(AUDIO_HOOK)   # 全程叠音不变量检测（任何环节两路长音频重叠即 fail）
        try:
            run(page)
            run_weekly(page)
        finally:
            browser.close()
    print(f"\n{PASS_N} pass / {FAIL_N} fail  （截图: tests/shots/）")
    sys.exit(1 if FAIL_N else 0)
