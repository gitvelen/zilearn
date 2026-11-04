/* zilearn 前端 — Vue3 单文件（同 wordslearn 架构：无构建、状态机推进）
 *
 * 每日结构（learn 日）: 昨日重现(D+1批) → 新字×N(四步卡:认词句写 + 首学3题) → 今日朗读卡(例句串读) → 到期复习+抽检
 * weekly 日: 周测(scope 乱序每字1题) → 错题看半卡后同题型重测到对 → 结算星星
 * 题型: listen(听音选字) mean(组词补全) sent(句子填空) find(找一找)；测验不显示拼音（防泄漏）
 * 四步卡对标洪恩"玩认练写"+四五快读"字→词→句"递进；步指示器可点跳（家长快进）
 */
const { createApp } = Vue;
let app = null;
const ETYM = { picto: "象形字", component: "部件拆解", mnemonic: "巧记", scene: "课文场景" };
/* TTS 默认音≠课内音的字：字音文件是词境音频，按钮须明示会读到的词 */
const AUDIO_PHRASE = { "着": "看着", "只": "一只" };

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  if (r.status === 401) { if (app) app.phase = "login"; throw new Error("未登录"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || r.status);
  return j;
}

const root = createApp({
  errorHandler(err, inst, info) { console.error("[vue]", info, err && err.message); },
  data() {
    return {
      phase: "boot", loginForm: { u: "", p: "" }, loginErr: "", userName: "", isTest: false,
      progress: {}, obGrade: "", obTarget: 300, obDays: 50, planEst: null, gradesReady: [], obKick: false,
      confirmBatches: [], confirmPage: 0, confirmPicked: [],
      today: {}, sessQueue: [], sessIdx: 0,
      ncStep: 1, readLines: [], readPlaying: false, readCur: "",
      curQuiz: null, quizPick: null, quizRevealed: false, quizCorrect: false, quizPending: null,
      weeklyDoneIds: [],
      curContent: null, weeklyScore: 0, weeklyTotal: 0, weeklyWrong: [], weeklyWrongIds: [],
      traceMsg: "", tracing: false, traceDone: false, traceFallback: false, traceWatched: false,
      findFound: [], findMisses: 0, findMiss: "",
      doneStats: { new: 0, review: 0 }, report: null,
      follow: { on: false, kind: "", idx: 0, done: [], state: "off" }, micLvl: 0,
      micDenied: false, micState: "ok", followPop: false, quizBurst: false,
      narrLock: false, readAudioUrl: "",
      homeTip: false, homeHold: false, ncHold: -1, renewing: false, moreRound: false,
      BURST_DIRS: [["-70px","-60px"],["70px","-75px"],["0px","-95px"],["-55px","55px"],["55px","65px"],["0px","-30px"]],
      gateScores: {}, gateWrong: {}, readNews: [],
    };
  },
  computed: {
    curItem() { return this.sessQueue[this.sessIdx] || null; },
    curAlt() {
      if (!this.curContent || !this.curContent.hanzi || !this.curContent.hanzi.alt_readings) return null;
      try { return JSON.parse(this.curContent.hanzi.alt_readings); } catch { return null; }
    },
    curSentences() {
      if (!this.curContent) return [];
      if (this.curContent.sentList) return this.curContent.sentList;
      return this.curContent.sentence ? [this.curContent.sentence] : [];
    },
    sessProgressPct() {
      return this.sessQueue.length ? ((this.sessIdx) / this.sessQueue.length * 100) + "%" : "0%";
    },
    todayCN() {
      /* 虚拟日（TEST 跳日）优先显示：与调度同源 today.date；未加载回落真实日历 */
      const s = this.today && this.today.date;
      const d = s ? new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) : new Date();
      const wk = ["日", "一", "二", "三", "四", "五", "六"];
      return `${d.getMonth() + 1}月${d.getDate()}日 · 星期${wk[d.getDay()]}`;
    },
    estLocal() {
      const d = this.obDays || 1, t = this.obTarget || 1;
      const studyDays = d - Math.floor(d / 7);           /* 每周 1 天周测不投新字 */
      const daily = Math.max(1, Math.ceil(t / studyDays));
      const est = Math.round((daily * 2.5 + daily * 4 * 0.2 + 3) * 10) / 10;
      return { daily_new: daily, study_days: studyDays, est_minutes: est, over_30min: est > 30 };
    },
    estToday() {
      const n = (this.today.new || []).length, r = (this.today.review || []).length + (this.today.audit || []).length;
      return Math.round(n * 3.5 + r * 0.3 + 3.5);
    },
    planDone() {                                   /* 本期达成：learned-基准线>=目标（续订增量记账） */
      const p = this.progress;
      return !!(p && p.target_chars && (p.learned - (p.plan_base_learned || 0)) >= p.target_chars);
    },
    planLearned() {                                /* 本期已学（家长中心进度卡） */
      return Math.max(0, (this.progress.learned || 0) - (this.progress.plan_base_learned || 0));
    },
    planPct() {
      const t = this.progress.target_chars || 0;
      return t ? Math.min(100, Math.round(this.planLearned / t * 100)) : 0;
    },
    trendAcc() {                                   /* 近 7 天答题正确率（家长中心数字卡） */
      if (!this.report || !this.report.trend) return "—";
      const n = this.report.trend.reduce((s, d) => s + d.n, 0);
      const ok = this.report.trend.reduce((s, d) => s + d.ok, 0);
      return n ? Math.round(ok / n * 100) + "%" : "—";
    },
    todayNewN() {                                  /* 今日新学：完成页统计真源，重进路径取 learned_today */
      return this.doneStats.new || (this.today.learned_today || []).length;
    },
  },
  methods: {
    /* ---------- 通用 ---------- */
    async boot() {
      try {
        const me = await api("GET", "/api/me");
        this.progress = me.progress; this.userName = me.user.display_name || me.user.username;
        this.isTest = (me.user.username || "").toLowerCase() === "test";   /* TEST 专属跳日入口 */
        this.gradesReady = me.grades_ready || [];   /* 册内容就绪度（年级选择禁选"制作中"） */
        if (me.stage === "onboard") { this.obKick = !!me.user.grade; this.phase = "ob-grade"; }
        else if (me.stage === "plan") { this.obTarget = Math.min(300, me.progress.total_hanzi - (me.progress.skipped || 0)); this.phase = "ob-plan"; }
        else if (me.stage === "confirm") {
          /* 摸底中途刷新：回摸底页续选（2026-11-03 补洞——原直接进学习日，速选作废）。
             confirm_batches 按水位重出批次：补摸轮的已见字会重列（重选幂等），可接受 */
          const r = await api("GET", "/api/confirm/batches").catch(() => null);
          if (r && r.batches && r.batches.length) {
            this.confirmBatches = r.batches; this.confirmPage = 0; this.confirmPicked = [];
            this.moreRound = false; this.phase = "ob-confirm";
          } else { await this.loadToday(); }
        }
        else { await this.loadToday(); }
      } catch (e) { if (this.phase !== "login") this.phase = "login"; }
    },
    async doLogin() {
      this.loginErr = "";
      try {
        await api("POST", "/api/login", { username: this.loginForm.u, password: this.loginForm.p });
        await this.boot();
      } catch (e) { this.loginErr = "登录失败，再试试"; }
    },
    async logout() {
      /* 家长页切账号入口：清会话回登录页。密码框必清；其余状态由登录后 boot()/引导流重拉 */
      await api("POST", "/api/logout").catch(() => { });
      this.loginForm.p = "";
      this.phase = "login";
    },
    _resetPlayCtx() {
      /* 假死自愈（2026-11-03 MIDO 实报根治位）：弃旧建新。running 状态不可信——真机
         follow_log 实锤"state=running 但调度无声、onended 永不触发"（played=0×5、
         model_ms=duration+2s 超时形态），此时 resume 无效，唯一出路是重建。
         手势内重建（四事件兜底/startToday 链）= 活 ctx；自动链内重建尽力而为 */
      if (this._playCtx) { try { this._playCtx.close().catch(() => { }); } catch { } }
      this._playCtx = null; this._ctxSus = false;
      this._narrSrc = this._ttsSrc = this._charSrc = null;   /* 旧 ctx 的 source 引用随之作废 */
      try { this._playCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch { }
      return this._playCtx;
    },
    _ensurePlayCtx() {
      /* 播放专用 AudioContext：iPadOS Safari 的 element autoplay 在手势激活窗口外几乎
         全拒（v21 遥测实锤：played=0/model_ms≈3ms 遍地，唯一 played=1 恰距点击<1s）。
         AudioContext 手势内 resume 一次后持续可调度，绕开该限制。首次创建挂 startToday
         手势链（warmMic 同机），运行中被系统挂起由全局 pointerdown 兜底 resume。
         resume 后补一次 1 采样静音 start（v45）：部分 iPadOS 版本 resume() 置 running
         仍不开输出通道（v43 pointerdown 兜底、v44 四事件兜底后真机验证轮依旧全程
         无声，而 Chrome/WebKit 模拟同链路全绿——差异只剩"手势内从未实际调度过播放"），
         howler 等库的标准 unlock 均含此步；幂等，running 后不再重入。
         v49：_ctxSus（播放超时置位）→ 先重建再走解锁，孩子下一次点击即自愈 */
      if (this._ctxSus) { this._resetPlayCtx(); console.warn("[ctx] 假死，已重建"); }
      if (!this._playCtx) {
        try { this._playCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch { }
      }
      const c = this._playCtx;
      if (c && c.state === "suspended") {
        c.resume().then(() => {
          if (c.state !== "running") return;
          try {
            const b = c.createBuffer(1, 1, c.sampleRate);
            const s = c.createBufferSource();
            s.buffer = b; s.connect(c.destination); s.start(0);
          } catch { }
        }).catch(() => { });
      }
      return c;
    },
    async _loadBuf(ctx, url) {
      /* LRU 上限 48 条（2026-10-30 走查：整日会话不同音频数百个，decoded buffer
         每个百余 KB，无上限缓存在 iPad 上累积数十 MB）——命中提新，超限淘汰最旧。
         fetch/decode 各带超时（v43）：任何一层挂起都会顺着 playFile 卡死 stepAudio
         链（stepBusy 永锁），弱网断连时浏览器自身超时可长达 30s+，不可依赖 */
      if (!this._bufC) this._bufC = new Map();
      if (this._bufC.has(url)) {
        const b = this._bufC.get(url);
        this._bufC.delete(url); this._bufC.set(url, b);
        return b;
      }
      const ac = new AbortController();
      const ftmo = setTimeout(() => ac.abort(), 8000);
      let r;
      try { r = await fetch(url, { signal: ac.signal }); }
      finally { clearTimeout(ftmo); }
      if (!r.ok) throw new Error("audio " + r.status);
      const ab = await r.arrayBuffer();
      const buf = await Promise.race([                       /* Safari 老回调式 decodeAudioData 兼容；
                                                               回调不回来的形态 6s 兜底拒结 */
        new Promise((res, rej) => {
          const p = ctx.decodeAudioData(ab, res, rej);
          if (p && p.then) p.then(res, rej);
        }),
        new Promise((_, rej) => setTimeout(() => rej(new Error("decode-timeout")), 6000)),
      ]);
      this._bufC.set(url, buf);
      while (this._bufC.size > 48) this._bufC.delete(this._bufC.keys().next().value);
      return buf;
    },
    async _ensurePlayRunning(ms = 1500) {
      /* ctx 必须 running 才可调度播放：suspended 上 start() 合法但无声、onended 永不
         触发（2026-11-02 macOS Safari 真机实锤：后台/久置挂起 ctx 后新字卡认步
         playFile 永挂 → stepBusy 永锁 →"点小图标也不行，流程卡住"）。resume 带
         超时（Safari 的 resume() 可能永不 settle）。
         仍非 running → 重建 ctx 再试一次：Safari 特有 'interrupted'（电话/他 app
         音频抢占）对 resume 免疫，唯一出路是弃旧建新；新建于手势内（本调用多源自
         四事件兜底/点击链）即 running。最终仍失败返回 null，调用方回退 element——
         用户手势内的 element.play() 可放行，自动链静默失败但不挂死 */
      let ctx = this._ensurePlayCtx();
      if (!ctx) return null;
      if (ctx.state === "running") return ctx;
      await Promise.race([
        ctx.resume().catch(() => { }),
        new Promise(r => setTimeout(r, ms)),
      ]);
      if (ctx.state === "running") return ctx;
      this._resetPlayCtx();                          /* 异步弃旧防 Safari 配额；此处重建非手势尽力而为 */
      ctx = this._playCtx;
      if (!ctx) return null;
      if (!ctx) return null;
      if (ctx.state === "running") return ctx;
      await Promise.race([
        ctx.resume().catch(() => { }),
        new Promise(r => setTimeout(r, ms)),
      ]);
      return ctx.state === "running" ? ctx : null;
    },
    async _playBuf(url, vol = 1, ch = "narr") {
      /* WebAudio 播放底座：narr/tts/char 三通道各自单实例（新播停旧，onended 自动结清旧
         promise——无 element pause/换src 时序坑），sfx 多实例。返回 Promise：
         true=播完/被停，false=失败（404/decode 拒/超时兜底），null=ctx 不可用（调用方回退 element）
         播放超时兜底（v43）：duration+2s 仍未 onended（suspended 调度/onended 丢失等一切
         挂起形态）强制结清为 false——音频链任何一层都不得永久挂起 */
      const ctx = await this._ensurePlayRunning();
      if (!ctx) return null;
      const key = { narr: "_narrSrc", tts: "_ttsSrc", char: "_charSrc" }[ch];
      let buf;
      try { buf = await this._loadBuf(ctx, url); } catch { return false; }
      return new Promise(res => {
        if (key) {
          if (this[key]) { try { this[key].stop(); } catch { } }   /* stop 触发旧 onended：旧 promise 结清 */
        }
        const src = ctx.createBufferSource(), g = ctx.createGain();
        g.gain.value = vol;
        src.buffer = buf;
        src.connect(g); g.connect(ctx.destination);
        const tmo = setTimeout(() => { try { src.stop(); } catch { } this._ctxSus = true; res(false); },
          (buf.duration || 3) * 1000 + 2000);   /* 超时=onended 永不触发：ctx 假死实锤，置位待重建（v49） */
        src.onended = () => { clearTimeout(tmo); res(true); };
        if (key) this[key] = src;
        try { src.start(); } catch (e) { clearTimeout(tmo); res(false); }
      });
    },
    _elFallback(url) {                                 /* 无可用 AudioContext 的兜底（autoplay 听天由命） */
      return new Promise((res) => {
        const a = new Audio(url);
        let failed = false, tmo = null;
        a.onloadedmetadata = () => {                   /* 元数据超时兜底（v43）：事件全不来也不挂链 */
          tmo = setTimeout(() => { try { a.pause(); } catch { } res(false); },
            (a.duration || 10) * 1000 + 3000);
        };
        a.onended = () => { if (tmo) clearTimeout(tmo); res(!failed); };
        a.onerror = () => { failed = true; res(false); };
        a.onpause = () => { if (tmo) clearTimeout(tmo); res(!failed); };
        a.play().catch(() => { failed = true; res(false); });
      });
    },
    playChar(ch, altTone3) {
      this.stopNarration();                            /* 同一时刻最多一个字音（find 连点防叠音） */
      const f = `/static/audio/${encodeURIComponent(altTone3 ? `${ch}_${altTone3}` : ch)}.mp3`;
      return this._playBuf(f, 1, "char").then(async ok => {
        if (ok === null) await this._elFallback(f).catch(() => {});
      });                                               /* 返回 promise：讲解链可衔接字音后 */
    },
    async playFile(name) {
      /* 播放单个音频文件，resolve(true)=正常播完/被停，resolve(false)=缺文件或播放被拒
         （RCA：曾把失败静默当播完——"4 条 word 同秒"式哑播放无迹可查，现返回失败标记）
         v23 走 WebAudio 底座（iPadOS element autoplay 窗口外全拒），ctx 不可用回退 element */
      const url = `/static/audio/${encodeURIComponent(name)}.mp3`;
      const ok = await this._playBuf(url, 1, "narr");
      if (ok !== null) return ok;
      return this._elFallback(url);
    },
    stopNarration() {
      /* 三通道统一停（narration 链/指令/字音）：切题、跳步、答题后遗留音频继续响的根源
         是这里只停一个通道（2026-10-28 叠音检测器抓到 4 组）。
         音频时钟化（2026-11-03）：同时作废悬挂的环境范读链（代际）并立即解除 narrLock
         ——不再触碰任何流程状态；narrLock 的解除只由 _endNarr/cap 定时器/此处同步完成 */
      this._stepGen = (this._stepGen || 0) + 1;
      this._narrStop = true;
      this._endNarr();
      for (const k of ["_narrSrc", "_ttsSrc", "_charSrc"]) {
        if (this[k]) { try { this[k].stop(); } catch { } this[k] = null; }
      }
    },
    _beginNarr(ms, onCap) {
      /* 定时器锁（音频时钟化核心）：环境范读期防"没听就走"，cap ms 兜底——链在任何一层
         挂死（fetch/decode/ctx）也必然解锁（认步兜底回调直接进跟读槽）。不变量：
         任何按钮 disabled 的解除只依赖定时器或孩子操作，永不依赖音频事件 */
      this.narrLock = true;
      clearTimeout(this._narrT);
      this._narrT = setTimeout(() => { this.narrLock = false; if (onCap) onCap(); }, ms);
    },
    _endNarr() {
      clearTimeout(this._narrT);
      this.narrLock = false;
    },
    playOne(name) { this.stopNarration(); this.playFile(name); },
    playTts(name) {                                   /* 语音指令（t_ 前缀）：单实例，新的停旧的（防连题双指令叠音） */
      const u = `/static/audio/t_${name}.mp3`;
      this._playBuf(u, 1, "tts").then(ok => { if (ok === null) this._elFallback(u).catch(() => {}); });
    },
    playSfx(name, vol = 0.6) {                        /* 反馈音效（对/错/过关）：独立短音，不打断朗读；描红逐笔用低音量 */
      const u = `/static/audio/sfx_${name}.wav`;
      this._playBuf(u, vol, null).then(ok => { if (ok === null) this._elFallback(u).catch(() => {}); });
    },
    async stepAudio() {
      /* 四步卡每步音频（音频时钟化，2026-11-03 拍板）：环境范读链只负责"播"，不锁流程——
         下一步按钮的锁只来自定时器（narrLock，cap 兜底）与跟读槽窗（follow.on）。
         认步=字音→字源故事→读字槽（v32 讲解前置保留）；词步=整体连听后自由点选
         （逐词跟读槽已删，拍板"词步回自由"）；句步=逐句范读→读句槽。
         代际守卫（v21 沿用）：跳步时旧链的 playFile 还在 await，gen 比对作废 */
      const ch = this.curItem && this.curItem.char;
      if (!ch) return;
      const gen = (this._stepGen = (this._stepGen || 0) + 1);
      const stale = () => gen !== this._stepGen;
      if (this.ncStep === 1) {
        this._beginNarr(12000, () => {           /* 链挂死兜底：cap 到点强制进槽（v43 永锁类根除） */
          if (!stale()) { this._endNarr(); this.startFollow("char"); }
        });
        await this.playFile(ch);                 /* 字音 */
        if (stale()) return;
        await this.playFile(`e_${ch}`);          /* 字源故事（环境音） */
        if (stale()) return;
        this._endNarr();
        this.startFollow("char");                /* 认步读字槽（强制窗） */
      } else if (this.ncStep === 2) {
        this._beginNarr(10000);
        await this.playFile(`w_${ch}`);          /* 整体连听（环境音），完毕转自由点选 */
        if (stale()) return;
        this._endNarr();
      } else if (this.ncStep === 3) {
        this.startFollow("sent");                /* 逐句：范读→读句槽 */
      }
    },
    ncGo(step) {
      /* 跳步（步指示器可点=家长快进，绕过跟读/范读）；到写步才挂描红 */
      this.stopNarration();
      this.skipFollow();
      this.ncStep = step;
      if (step === 4) this.$nextTick(() => this.mountTracer(this.curItem.char));
      else this.stepAudio();
    },
    ncNext() {
      if (this.ncNextDisabled()) return;         /* 判定统一（键盘/时序残留兜底） */
      this.ncStep >= 4 ? this.advance() : this.ncGo(this.ncStep + 1);
    },
    ncNextDisabled() {
      if (this.follow.on) return true;           /* 跟读槽窗（强制·定时器）：读的时机不可跳过；
                                                    家长长按步指示器 ncGo 可绕过 */
      if (this.narrLock) return true;            /* 环境范读期（字音+故事/连听）：定时器 cap 兜底解锁 */
      return this.ncStep === 4 && !this.traceDone && !this.traceFallback;   /* 写步门槛（v32） */
    },
    ncNextLabel() {
      if (this.follow.on) return "跟着读一读吧 🎤";
      if (this.narrLock) return "先听一听哦 👂";
      if (this.ncStep < 4) return "下一步 →";
      if (!this.traceDone && !this.traceFallback) return "写完才能继续哦 ✍️";
      return this.hasNextNew() ? "这个字学会啦，下一个 →" : "我会写啦，去答题 →";
    },
    hasNextNew() {                                    /* 后面还有新字卡（写步按钮文案据此区分） */
      return this.sessQueue.slice(this.sessIdx + 1).some(q => q.type === "newcard");
    },
    async ensureMic() {
      /* 取可用麦+激活 AudioContext；引用存在≠可用：iOS 切后台/锁屏会结束 track（R2），
         每次检测前验 live，失效重取。getUserMedia（权限弹窗未决）与 resume()（无手势激活）
         都可能永久 pending，各套 3s 超时。v19 三修（RCA 五线审查抓到 v18 缺陷）：
         ① in-flight 去重——warmMic 与首个跟读并发 gUM 双双超时还互相作废；
         ② 失败分流记忆——v18 一刀切 30s 把"弹窗未决"也封死，家长点了允许还得白等；
            denied(明确拒绝)记 30s / pending(弹窗未决)记 5s / busy(设备占用)记 10s；
         ③ AbortSignal 塞 constraints 是无效成员（getUserMedia 单参数签名）——迟到流改
            引用比对收编/丢弃（宏任务时机：正常路径赋值已完成，超时路径迟到流被放掉） */
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return this._micFail("none", 60000);
      if (this._micEnsuring) return this._micEnsuring;
      if ((this._micFailUntil || 0) > Date.now()) { this.micDenied = true; return false; }
      const tmo = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("mic-timeout")), ms))]);
      this._micEnsuring = (async () => {
        try {
          const live = this._micStream && this._micStream.getTracks().some(t => t.readyState === "live");
          if (!live) {
            if (this._micStream) { try { this._micStream.getTracks().forEach(t => t.stop()); } catch { } }
            const prevStream = this._micStream;         /* 竞态哨兵：await 期间被 closeMic/他处替换则丢弃迟到流 */
            /* 约束显式化（v20：AEC/NS 把童声压地板下→关；v33 真机遥测：Safari 的 AGC 把
               童声钳在 0.008-0.018（JS 侧增益×3 被系统归一抵消、大吼才放行 0.039）→
               AGC 也关。v20 开 AGC 是为"绝对电平前提"，v33 阈值已改相对判据，不再依赖） */
            const g = navigator.mediaDevices.getUserMedia(
              { audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
            g.then(s => setTimeout(() => {                /* 迟到流（超时后才 resolve）：已被超越→停掉，别白亮指示灯 */
              if (this._micStream !== s) { try { s.getTracks().forEach(t => t.stop()); } catch { } }
            }, 0)).catch(() => { });
            const s = await tmo(g, 3000);
            if (this._micStream !== prevStream) { try { s.getTracks().forEach(t => t.stop()); } catch { } return this._micFail("busy", 1000); }
            this._micStream = s;
            /* 麦克风实际生效设置取证（Safari 可能忽略 EC/NS 约束——电平被压的唯一实证来源） */
            try {
              const st = s.getAudioTracks()[0];
              const cfg = st && st.getSettings && st.getSettings();
              if (cfg) this._micSettings = JSON.stringify(
                { ec: cfg.echoCancellation, ns: cfg.noiseSuppression, agc: cfg.autoGainControl });
            } catch { }
          }
          if (!this._micCtx) this._micCtx = new (window.AudioContext || window.webkitAudioContext)();
          if (this._micCtx.state === "suspended") await tmo(this._micCtx.resume(), 3000);
          this.micState = "ok"; this.micDenied = false;
          return true;
        } catch (e) {
          const n = e && e.name;
          if (n === "NotAllowedError" || n === "SecurityError") return this._micFail("denied", 30000);
          if (n === "NotReadableError" || n === "OverconstrainedError") return this._micFail("busy", 10000);
          return this._micFail("pending", 5000);          /* mic-timeout/其他：弹窗多半还挂着 */
        } finally { this._micEnsuring = null; }
      })();
      return this._micEnsuring;
    },
    _micFail(state, cooldownMs) {
      this.micState = state; this.micDenied = true;
      this._micFailUntil = Date.now() + cooldownMs;
      return false;
    },
    warmMic() {
      /* 跟读音量校验依赖 AudioContext.resume() 的用户激活；跟读发生时点击激活早过期，
         故在"开始学习"手势里预热。拒绝/无麦静默降级（followHeard 返回 null 不校验不夸） */
      this.ensureMic();
    },
    closeMic() {
      /* 会话结束放掉麦克风（整节课亮指示灯会让家长不安；下次 startToday 手势内重开） */
      this._recStop();
      if (this._micStream) { try { this._micStream.getTracks().forEach(t => t.stop()); } catch { } this._micStream = null; }
      if (this._micCtx) { try { this._micCtx.close(); } catch { } this._micCtx = null; }
      this._micFailUntil = 0; this.micState = "ok"; this.micDenied = false;   /* 新会话重给麦克风一次机会 */
    },
    /* ---------- 跟读（节奏槽，2026-11-03 两轮拍板：结构强制窗，不做正确性判定）
       槽=范读→"叮"→留白窗（定时器上限）：读的时机不可跳过，窗走完即完成；
       音量检测只做可视化/提前收窗/遥测（不 gate 不重试不录音）；
       ASR 判定已删（用户拍板：只要跟读完成即可，不判断正确性）；
       朗读卡会话录音（"听听自己读的"回放）保留——它是素材不是判定。 ---------- */
    _recStart() {
      /* 词步 ASR 录音：留白窗起点开录（范读已停——录音不含范读，堵"重听范读声=假通过"）。
         设备不支持 MediaRecorder/流失效则静默放弃（_recBlob=null → verify 降级放行） */
      if (this._recorder) { try { this._recorder.onstop = null; this._recorder.stop(); } catch { } this._recorder = null; }
      this._recBlob = null; this._recChunks = [];
      try {
        if (!this._micStream || !window.MediaRecorder) return;
        const mt = MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";   /* Safari=mp4，Chrome 默认 webm */
        const r = new MediaRecorder(this._micStream, mt ? { mimeType: mt } : undefined);
        r.ondataavailable = e => { if (e.data && e.data.size) this._recChunks.push(e.data); };
        r.start(1000);
        this._recorder = r;
      } catch { this._recorder = null; }
    },
    _recStop() {
      const r = this._recorder;
      this._recorder = null;
      if (!r) return Promise.resolve();
      return new Promise(res => {
        r.onstop = () => {
          this._recBlob = this._recChunks.length
            ? new Blob(this._recChunks, { type: r.mimeType || "audio/webm" }) : null;
          res();
        };
        try { r.state !== "inactive" ? r.stop() : r.onstop(); } catch { res(); }
      });
    },
    followItems(kind) {
      if (kind === "char") return [{ key: "c", audio: this.curItem.char }];
      if (kind === "sent") return this.curSentences
        .map((s, i) => ({ key: "s" + i, audio: `s_${this.curItem.char}_${i + 1}` }));
      return this.readLines.map(l => ({ key: "r" + l.char, audio: `s_${l.char}_1` }));
    },
    startFollow(kind) {
      /* 开槽（范读→叮→留白窗，窗必经）：窗的推进只认定时器/音量提前收窗/家长长按跳过 */
      const items = this.followItems(kind);
      if (!items.length) return this.followFinish();
      this.follow = { on: true, kind, idx: 0, done: [], state: "model", heardAny: false };
      this.followPlay(0);
    },
    async startReadSession() {
      /* 朗读卡（层3）：会话级录音（跨全部逐句槽）→ 结束生成"听听自己读的"回放 URL */
      const micReady = await this.ensureMic().catch(() => false);
      if (micReady && this.curItem && this.curItem.type === "readcard") this._recStart();
      this.startFollow("read");
    },
    async followPlay(i, replay = false) {
      const f = this.follow;
      if (!f.on || f.state === "all") return;
      const gen = ++this._followGen;                  /* 代际 token：重听/跳过后作废旧链，防双链竞争 */
      this._fStats = null;                            /* 每项清零：无麦/范读失败轮不再带上一个检测窗的
                                                          遥测（2026-11-01 抓到 heard=NULL 批量恒同值失真） */
      f.idx = i;
      const items = this.followItems(f.kind), it = items[i];
      if (!it) return this.followFinish();
      const micP = this.ensureMic();                  /* 范读期间并行取麦（RCA：v18 wait 后才取，首个
                                                         wait 有 1~3s 聋窗=对着空气读；且流先建立，
                                                         AEC/AGC 听过范读才预热，冷流会压童声） */
      f.state = "model";
      const tModel0 = performance.now();
      const played = await this.playFile(it.audio);   /* 范读（replay=用户点当前对象重听，主动重播） */
      this._fModel = { played: played ? 1 : 0, model_ms: Math.round(performance.now() - tModel0) };
      if (!played) console.warn("范读播放失败/被拒:", it.audio);   /* RCA：哑播放曾无迹可查；v21 落库遥测 */
      if (gen !== this._followGen || !f.on) return;
      f.state = "wait";
      this.playSfx("ding");                           /* "叮"=轮到你（不用啰嗦语音指令） */
      /* 留白按内容自适应：句=范读时长+2.5s（TTS 成人语速，幼儿复述慢得多），字 3.5s */
      let modelMs = 0, ms;
      if (f.kind === "sent" || f.kind === "read") {
        modelMs = (await this.audioDur(it.audio) || 3) * 1000;
        ms = modelMs + 2500;
      } else ms = 3500;
      if (gen !== this._followGen || !f.on) return;
      const micReady = await micP;                    /* 范读+时长查询期间取麦早该就绪；至多再等超时余量 */
      if (gen !== this._followGen || !f.on || f.state !== "wait") return;
      const heard = await this.followHeard(ms, gen, micReady, modelMs);   /* 音量快检：true读到(提前收窗)/false超时/null无麦 */
      this.micLvl = 0;
      if (gen !== this._followGen || !f.on || f.state !== "wait") return;
      this.followDoneOne(heard);                      /* 三态透传(true读到/false超时/null无麦降级)：
                                                          家长报表 na=heard IS NULL 靠它区分，折叠成 false 会全记超时 */
    },
    async followHeard(ms, gen, micReady, modelMs = 0) {
      /* 留白窗口内监听麦克风电量（不录音不保存）：说到(true)/超时(false)/无麦或拒绝(null=节奏式降级)
         R1 动态阈值=滚动本底×2.5，绝对下限 0.035（v20 据遥测分布定）。v19 结构修复（RCA 仿真抓到自掩蔽 bug）：
         孩子说话期间 base 被语音帧自己抬升，thr=2.5×base 在语音结束前反超童声电平——本底
         0.03+童声 0.12 只净积 183ms<200ms 判超时。修法：thr 先用上一帧 base 算，voiced 帧
         (rms>thr)冻结 base；EMA 改时间基准（1-exp(-dt/825)≈60fps×0.02）——120Hz 屏（iPad
         ProMotion）base 升速不再翻倍。数值调参（地板/cap/HEARD_MS）等遥测数据再动。
         R3 真实时间累积 ~0.12s 才算读到（v20：幼儿短音"天！"<300ms，200ms 偏紧）：滤瞬态
         达标后不立即切下一项：等静音 0.6s 让孩子把话说完（防范读抢话，读到≠读完）
         前 0.8s 排除"叮"/范读尾音的外放串扰（ding≈0.18s；v20 关 AEC 后尾音无压制，加长）
         过程量（peak_rms/thr_used/…）随结果落 _fStats 供 logFollow 上报：下次报障直判"没读"还是"漏检" */
      if (!(micReady === true || await this.ensureMic())) return null;
      try {
        /* 不录音（ASR 判定已删）：麦流只喂 AnalyserNode 做音量可视化与遥测 */
        const src = this._micCtx.createMediaStreamSource(this._micStream);
        /* 增益 ×3（2026-10-31 iPad 实测：Safari 链路童声仅 0.007-0.017，绝对地板 0.035 挡死）：
           拉回工作区；正常电平设备信号削顶仍远超阈值，base 同步放大、相对判据不变 */
        const gain = this._micCtx.createGain();
        gain.gain.value = 3;
        const an = this._micCtx.createAnalyser();
        an.fftSize = 512;
        src.connect(gain); gain.connect(an);
        const buf = new Uint8Array(an.fftSize);
        /* EXCL 1500（v45，2026-11-02 生产日志实锤"叮"直耦串扰秒过）：无 AEC 设备上扬声器
           播"叮"→麦 5cm 直耦拾音，尾音在 800ms 排除窗后仍高于 thr（深夜底噪低 thr 极低），
           80ms 即积满"读到"——词步 1.4-2.0s/项秒过、孩子不读也全过。1500ms 覆盖叮(180ms)
           +直耦拖尾+反射；代价=留白起点晚 0.7s */
        const EXCL = 1500;
        const sent = this.follow.kind === "sent" || this.follow.kind === "read";
        /* 句档放宽（v43，2026-11-02 真机 follow_log 实锤"句子没读完就跳"）：voiced_ms 恒
           ≈84ms 即判"读到"、dur 2.5-3.6s 即切——句中换气 600ms、起读 2.5s 封顶，长句必被
           腰斩；且门槛 80ms 连环境瞬态都挡不住（peak 0.025 刚过 thr 0.023 也记 heard=1）。
           v45 词/字档 80→120ms：串扰尾音（快速衰减）积不满 120ms，童声持续音节轻松积满
           （句档 v43 已验证 120ms 可行；v33"起音瞬态积不满"是 AGC+绝对地板时代，现 AGC
           关+增益×3+相对判据，条件已变） */
        const HEARD_MS = 120;
        const QUIET_MS = sent ? 1200 : 600;
        /* 反馈封顶（v41，2026-11-02 真机 follow_log 方差实锤：word 1960-6072ms）：
           读到后等静音 600ms 才确认，但家里背景声（空调/人声，≈thr 量级）反复清零
           静音计时，拖到 6.3s 硬兜底——孩子读完无反馈→以为没听到→更大声重复→更慢。
           v43 句档修正：固定 2.5s 对句是"起读即倒计时"，长句必被腰斩（用户实报"没读完
           就跳"）→ 句封顶=范读时长+2s；词 1.5s 维持 v41 */
        const DONE_CAP = sent ? Math.max(2500, modelMs + 2000) : 1500;
        const t0 = performance.now(), hardAt = ms + EXCL + 1500;
        let base = 0.05, minExcl = 1, voicedMs = 0, trailAt = 0, voicedDoneAt = 0, lastT = t0;
        let peak = 0, thrEnd = 0;
        const done = v => {
          this._fStats = { peak_rms: +peak.toFixed(3), noise_base: +base.toFixed(3),
                           thr_used: +thrEnd.toFixed(3), voiced_ms: Math.round(voicedMs),
                           dur_ms: Math.round(performance.now() - t0) };
          return v;
        };
        try {
          return await new Promise(res => {
            const tick = () => {
              const now = performance.now(), t = now - t0;
              if (gen !== this._followGen || !this.follow.on || this.follow.state !== "wait") return res(done(false));
              an.getByteTimeDomainData(buf);
              let sum = 0;
              for (let k = 0; k < buf.length; k++) { const d = buf[k] - 128; sum += d * d; }
              const rms = Math.sqrt(sum / buf.length) / 128;
              this.micLvl = Math.min(1, rms * 8);               /* 🎤/大字随声音跳动（×8：小声读也可见） */
              const dt = now - lastT; lastT = now;
              if (t < EXCL) { minExcl = Math.min(minExcl, rms); return requestAnimationFrame(tick); }
              /* 阈值=本底×2.5（相对判据）；本底地板 0.006 只防数字噪声——绝对地板已删
                 （2026-10-31 iPad 实测：Safari 链童声 0.007-0.017 被绝对地板 0.035 全挡，
                 "要非常大声才识别"；20 倍设备电平差异下绝对地板是有害假设，增益×3 配合
                 相对判据跨设备自适应） */
              const thr = Math.max(0.006, base) * 2.5;
              if (rms > peak) peak = rms;
              thrEnd = thr;
              if (t < EXCL + 120) base = Math.max(0.006, minExcl);
              else if (rms <= thr) {                            /* 非语音帧才更新本底（快降慢升） */
                const a = 1 - Math.exp(-dt / 825);              /* 时间基准：帧率无关 */
                base = rms < base ? rms : base + (rms - base) * a;
              }
              if (voicedMs >= HEARD_MS) {                       /* 已读到：等孩子说完 */
                if (!voicedDoneAt) voicedDoneAt = now;
                if (rms < thr) {
                  if (!trailAt) trailAt = now;
                  if (now - trailAt >= QUIET_MS) return res(done(true));
                } else trailAt = 0;
                /* 读到后封顶放行：背景声压不住阈值时静音计时反复清零，不能让反馈拖过
                   DONE_CAP（真机 6s 无反馈=家长报障"很大声读了还是听不到"的直接根源） */
                if (now - voicedDoneAt >= DONE_CAP) return res(done(true));
              } else {
                voicedMs = Math.max(0, voicedMs + (rms > thr ? dt : -dt * 0.5));
              }
              if (t > hardAt) return res(done(voicedMs >= HEARD_MS)); /* 持续噪音兜底：不无限等 */
              requestAnimationFrame(tick);
            };
            tick();
          });
        } finally { src.disconnect(); }
      } catch (e) { return null; }
    },
    async followDoneOne(heard) {
      /* 槽窗结束（heard=音量快检三态）：窗必经、结束必放行——检测永不拦截推进。
         ✓ 爆闪=结构完成的可视反馈（无论是否检测到）；音量命中加即时确认音。
         不做正确性判定（2026-11-03 拍板）：无录音无 ASR，槽走完即完成 */
      const f = this.follow;
      if (!f.on || f.state !== "wait") return;
      const snap = {
        char: f.kind === "read" ? (this.readLines[f.idx] || {}).char : this.curItem.char,
        stats: this._fStats || {}, model: this._fModel || {},
      };
      this.logFollow(heard, 0, snap);                       /* R5：开口流水（fire-and-forget） */
      f.heardAny = f.heardAny || !!heard;
      this.followPop = true;                                /* ✓ 爆闪：槽窗完成（拍板：无论检测与否） */
      clearTimeout(this._popT); this._popT = setTimeout(() => this.followPop = false, 650);
      if (heard) this.playSfx("ok");                        /* 音量快检命中：即时确认音 */
      this.followAdvance();
    },
    followAdvance() {
      const f = this.follow;
      f.done.push(f.idx);
      if (f.idx + 1 >= this.followItems(f.kind).length) this.followFinish();
      else this.followPlay(f.idx + 1);
    },
    fbHint() {                                         /* 跟读条文案（鼓励式两态：范读/轮到你；无麦降级明示原因） */
      const f = this.follow;
      if (this.micDenied) {
        if (this.micState === "pending") return "等爸爸妈妈点一下【允许】哦";
        return "麦克风没开，跟爸爸妈妈读一读吧";
      }
      if (f.state === "model") return "先听一听…";
      if (f.state === "wait") return "轮到你啦，大声读！";
      return "";
    },
    fbHintCls() {
      const f = this.follow;
      if (this.micDenied) return this.micState === "pending" ? "pending" : "denied";
      if (f.state === "wait") return "go";
      return "";
    },
    logFollow(heard, tries, snap) {                 /* snap=当项快照（v41 异步 ASR 下必传） */
      const f = this.follow;
      const char = (snap && snap.char) || (f.kind === "read" ? (this.readLines[f.idx] || {}).char : this.curItem.char);
      if (!char) return;
      const s = (snap && snap.stats) || this._fStats || {}, m = (snap && snap.model) || this._fModel || {};
      const asr = snap && snap.asr;               /* ASR 判定已删：恒 null，字段保留兼容旧流水 */
      api("POST", "/api/follow/log", {                        /* 过程量遥测：peak/thr/voiced/dur+范读结果+UA+客户端时刻 */
        char, kind: f.kind, heard, tries,
        peak_rms: s.peak_rms ?? null, noise_base: s.noise_base ?? null,
        thr_used: s.thr_used ?? null, voiced_ms: s.voiced_ms ?? null,
        dur_ms: s.dur_ms ?? null,
        played: m.played ?? null, model_ms: m.model_ms ?? null,
        mic_settings: this._micSettings || null,
        asr_text: asr ? (asr.text ?? (asr.reason ? "△" + asr.reason : null)) : null,
        asr_pass: asr ? (asr.degraded ? null : (asr.pass ? 1 : 0)) : null,
        ts_ms: Date.now(),
        ua: navigator.userAgent.slice(0, 160),
      }).catch(() => {});
    },
    async followFinish() {
      const f = this.follow;
      f.state = "all";
      if (f.kind === "char") await this.playFile(this.curItem.char);   /* 确认读 */
      if (f.kind === "read") {                                          /* 层3：朗读卡会话录音收尾 */
        await this._recStop();
        if (this._recBlob) this.readAudioUrl = URL.createObjectURL(this._recBlob);
      }
      f.on = false;                                                     /* 转自由点选模式 */
    },
    playReadAudio() {                                  /* "听听自己读的"（本地 blob，点击手势内播放） */
      if (!this.readAudioUrl) return;
      const a = new Audio(this.readAudioUrl);
      a.play().catch(() => {});
    },
    followReplay() {                                   /* 留白中点当前对象=重听范读（代际作废旧检测） */
      const f = this.follow;
      if (!f.on || f.state !== "wait") return;
      this.followPlay(f.idx, true);
    },
    skipFollow() {
      this._followGen = (this._followGen || 0) + 1;   /* 作废进行中的检测/播放链 */
      this._recStop();                                /* 槽录音随链作废（不产判定；read 会话录音亦止） */
      if (this._narrSrc) { try { this._narrSrc.stop(); } catch { } }   /* 只停范读/确认读，不置 _narrStop */
      this.micLvl = 0; this.followPop = false;
      this.follow = { on: false, kind: "", idx: 0, done: [], state: "off", heardAny: false };
    },
    /* 跟读槽中：点当前对象重听（其余忽略）；自由模式：点对象听音。
       环境范读期（narrLock）忽略点击：playOne/playChar 会 stopNarration 作废范读链，
       槽会开不起来——"这个字没有跟读"的根源（2026-11-01 复现实锤，v37 守卫沿用换载体） */
    followWordClick(i) {
      if (this.narrLock) return;
      this.playOne(`n_${this.curItem.char}_${i + 1}`);
    },
    followSentClick(i) {
      if (this.narrLock) return;
      if (this.follow.on) { if (i === this.follow.idx) this.followReplay(); return; }
      this.playOne(`s_${this.curItem.char}_${i + 1}`);
    },
    followReadClick(i) {
      if (this.narrLock) return;
      if (this.follow.on) { if (i === this.follow.idx) this.followReplay(); return; }
      if (this.readLines[i]) this.playOne(`s_${this.readLines[i].char}_1`);
    },
    ziClick() {                                    /* 认步大字（范读期防误触，同上） */
      if (this.narrLock) return;
      this.follow.on && this.follow.kind === "char" ? this.followReplay() : this.playChar(this.curItem.char);
    },
    charListen(altTone3) {                         /* 🔊 按钮/多音字（同上） */
      if (this.narrLock) return;
      this.playChar(this.curItem.char, altTone3);
    },
    etymListen() {                                 /* 字源故事 🔊（同上） */
      if (this.narrLock) return;
      this.playOne('e_' + this.curItem.char);
    },
    audioDur(name) {
      /* 时长查询走 _loadBuf 同一缓存（2026-10-30 走查：new Audio 预载与 _playBuf 的
         fetch 对同文件双下载双缓冲）；ctx 不可用回退 element metadata */
      const ctx = this._ensurePlayCtx();
      if (ctx) {
        return this._loadBuf(ctx, `/static/audio/${encodeURIComponent(name)}.mp3`)
          .then(b => b.duration || 0).catch(() => 0);
      }
      return new Promise(res => {
        const a = new Audio(`/static/audio/${encodeURIComponent(name)}.mp3`);
        const tmo = setTimeout(() => res(0), 5000);          /* 元数据不来也不得挂链（v43） */
        a.onloadedmetadata = () => { clearTimeout(tmo); res(a.duration || 0); };
        a.onerror = () => { clearTimeout(tmo); res(0); };
      });
    },
    skipHold(down) {                                   /* 家长长按 1.5s 跳过（防孩子误触）；返回是否达成 */
      if (down) { this._skipT = Date.now(); return false; }
      const ok = Date.now() - this._skipT >= 1500;
      if (ok) this.skipFollow();
      return ok;
    },
    wordHtml(w) { return w.split(this.curItem.char).join(`<b>${this.curItem.char}</b>`); },
    sentHtml(s) { return s.split(this.curItem.char).join(`<b>${this.curItem.char}</b>`); },
    etymName(t) { return ETYM[t] || "字的故事"; },
    listenLabel(ch) { const p = AUDIO_PHRASE[ch]; return p ? `听·${p}` : "听"; },
    inSession() {
      return ["newlearn", "review", "weekly"].includes(this.phase);
    },
    sessionLabel() {
      const t = this.curItem && this.curItem.type;
      if (t === "newcard") return "✨ 新字探索";
      if (t === "readcard") return "📖 今日朗读";
      if (t === "gatequiz" || t === "gatesummary")
        return `🚪 第${this.curItem.gate}关 · ${["听音", "拼音", "组词", "听句", "字义"][this.curItem.gate - 1]}`;
      if (this.phase === "weekly") return "🏆 周大挑战";
      if (t === "auditquiz") return "🕵️ 小侦探抽查";
      return "🎯 复习挑战";
    },
    /* ---------- 防误触（home/跳步长按化：幼儿乱点不丢进度、不跳节奏） ---------- */
    homeDown() { this._homeT = Date.now(); this.homeHold = true; },
    homeUp() {
      this.homeHold = false;
      if (Date.now() - (this._homeT || 0) >= 1200) { this.homeTip = false; this.exitSession(); }
      else this.holdTip("长按 🏠 一会儿才能回家哦");
    },
    ncDown(i) { this._ncT = Date.now(); this.ncHold = i; },
    ncUp(i) {
      this.ncHold = -1;
      if (this.ncStep === i + 1 || Date.now() - (this._ncT || 0) >= 800) this.ncGo(i + 1);
      else this.holdTip("想跳到这一步？按住一会儿");
    },
    ncLeave(i) { if (this.ncHold === i) this.ncHold = -1; },   /* 滑出走触屏常态：静默取消不提示 */
    holdTip(msg) {                                /* 长按引导统一提示（1.5s 自隐） */
      this.homeTip = msg;
      clearTimeout(this._tipT); this._tipT = setTimeout(() => this.homeTip = false, 1500);
    },
    exitSession() {                               /* skipFollow：不终止的话跟读链在退出后仍后台响（走查抓到） */
      this._stopHw();
      this._quizCache = {};                       /* 预取缓存随会话作废（防陈旧题跨会话命中） */
      this._contentCache = {};                    /* 内容预取缓存同作废 */
      this.stopNarration(); this.skipFollow(); this.closeMic();
      if (this.readAudioUrl) { URL.revokeObjectURL(this.readAudioUrl); this.readAudioUrl = ""; }
      this.homeTip = false; this.sessQueue = []; this.sessIdx = 0; this.loadToday();
    },

    /* ---------- 引导流 ---------- */
    async setGrade() {
      await api("POST", "/api/profile/grade", { grade: this.obGrade });
      const me = await api("GET", "/api/me"); this.progress = me.progress;   /* 换册：字数口径随新册刷新 */
      if (me.progress.target_chars) {          /* 被打回重选册但计划还在（册内容事故恢复）：不重定目标不重摸底 */
        await this.loadToday();
        return;
      }
      this.phase = "ob-plan";
    },
    async createPlan() {
      this.planEst = await api("POST", "/api/plan/create",
        { target_chars: this.obTarget, days: this.obDays });
      if (this.renewing) {                          /* 续订：不重走摸底，直接回今日 */
        this.renewing = false;
        await this.loadToday();
        return;
      }
      const r = await api("GET", "/api/confirm/batches");
      this.confirmBatches = r.batches; this.confirmPage = 0; this.confirmPicked = [];
      this.moreRound = false;                       /* 首批摸底（补摸轮由 showMoreConfirm 置位） */
      this.phase = "ob-confirm";
    },
    startRenew(from) {                              /* 本期达成 → 家长续订（完成页入口） */
      this.renewing = true; this._renewFrom = from;
      this.obTarget = Math.min(30, this.progress.pool || 30);
      this.phase = "ob-plan";
    },
    renewBack() { this.renewing = false; this.phase = this._renewFrom || "done"; },
    togglePick(id) {
      const i = this.confirmPicked.indexOf(id);
      i >= 0 ? this.confirmPicked.splice(i, 1) : this.confirmPicked.push(id);
    },
    async confirmSubmit() {
      /* 「跳过剩余/选完了」在任何页都直接提交：剩余批次视为生词。验证轮已砍
         （2026-11-03 拍板，wordslearn v4.1.2 同款决策）：已选直接 skipped；
         池内确认生词不足目标 → more_batch 回摸底表格页（轮次补摸） */
      const r = await api("POST", "/api/confirm/submit",
        { known_ids: this.confirmPicked }).catch(() => null);
      if (r && r.more_batch && r.more_batch.length) return this.showMoreConfirm(r.more_batch);
      await this.finishOnboard();
    },
    showMoreConfirm(batch) {
      /* 轮次补摸：池内确认生词不足目标 → 再摸一批（后端水位顺延，摸过的字不重复） */
      this.moreRound = true;
      this.confirmBatches = [batch]; this.confirmPage = 0; this.confirmPicked = [];
      this.phase = "ob-confirm";
    },
    async finishOnboard() {
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.obTarget = Math.max(10, Math.min(this.obTarget, me.progress.total_hanzi - me.progress.skipped));
      await this.loadToday();
    },

    /* ---------- 今日 ---------- */
    async loadToday() {
      this.today = await api("GET", "/api/today");
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.phase = "today-intro";
    },
    startToday() {
      this.warmMic();                                 /* 手势上下文预热麦克风+AudioContext（跟读校验依赖） */
      this._ensurePlayCtx();                          /* 手势上下文预热播放 ctx（iPad autoplay：解锁一次全程可用） */
      if (this.today.mode === "learn") {
        if (!this.today.new.length && !this.today.review.length && !this.today.audit.length) {
          this.goParent(); return;              /* 当日已清空（重进）：直接看成绩，不空转 */
        }
        this.phase = "newlearn"; this.buildLearnQueue();
      }
      else if (this.today.mode === "weekly") { this.phase = "weekly"; this.buildWeeklyQueue(); }
      else this.phase = "done";
    },
    async skipDay() {                                 /* TEST 专属：虚拟日历+1，今天直接学下一天课程 */
      await api("POST", "/api/dev/next-day").catch(() => {});
      await this.loadToday();
    },
    buildLearnQueue() {
      /* 分关结构（wordslearn v2.1 模式）：先学完 → 复习沉淀 → 分关测验（延迟回忆）→ 朗读收尾 */
      const q = [];
      const warm = this.today.review.filter(r => this.isWarm(r));
      const gen = this.today.review.filter(r => !warm.includes(r));
      warm.forEach(r => q.push({ type: "reviewquiz", phase: "review", ...r }));   // 昨日重现
      this.today.new.forEach(h => q.push({ type: "newcard", ...h }));             // 新字连播（纯呈现+跟读）
      gen.forEach(r => q.push({ type: "reviewquiz", phase: "review", ...r }));    // 复习沉淀
      this.today.audit.forEach(r => q.push({ type: "auditquiz", phase: "audit", ...r }));
      /* 分关：重进恢复——today.new 只含未学字（build_today 已剔除当日已学），关池 =
         未学字 ∪ 当日已学（关未完成时）；当日全部学完且关已过 → 不重建。
         写字关已删（2026-11-02 拍板）：新字卡第 4 步描红已覆盖写字，测验不重复 */
      const gateDone = sessionStorage.getItem("gateDone_" + this.today.date) === "1";
      const seenIds = new Set(this.today.new.map(h => h.id));
      const news = [...this.today.new,
        ...(!gateDone ? (this.today.learned_today || []).filter(l => !seenIds.has(l.id)) : [])];
      if (news.length) {
        const gates = [
          { gate: 1, label: "听音关", qt: "listen", icon: "👂" },
          { gate: 2, label: "拼音关", qt: "pin", icon: "🔤" },
          { gate: 3, label: "组词关", qt: "mean", icon: "🧩" },
          { gate: 4, label: "听句关", qt: "lsent", icon: "🎧" },
          { gate: 5, label: "字义关", qt: "defi", icon: "💡" },
        ];
        const lastGate = gates[gates.length - 1].gate;
        gates.forEach(g => {
          const chars = [...news].sort(() => Math.random() - 0.5);   // 各关独立乱序
          chars.forEach(h => q.push({ type: "gatequiz", phase: "learn",
            gate: g.gate, quiz_type: g.qt, ...h }));
          q.push({ type: "gatesummary", gate: g.gate, label: g.label, icon: g.icon,
                   total: chars.length, isLast: g.gate === lastGate });
        });
        this.gateScores = Object.fromEntries(gates.map(g => [g.gate, 0]));
        this.gateWrong = Object.fromEntries(gates.map(g => [g.gate, []]));
        q.push({ type: "readcard" });              // 朗读收尾
        this.readNews = news;                      // readcard 行=本次关池字
      } else { this.readNews = []; }
      this.sessQueue = q; this.sessIdx = 0; this.doneStats = { new: 0, review: 0 };
      this._promptDone = new Set(); this._quizCache = {};   /* 会话级：玩法指令首题化 + 题目预取缓存 */
      ["ok", "no", "ding", "win"].forEach(s => this._preloadFile(`sfx_${s}.wav`));  /* 反馈音效预载：首次答题不哑火 */
      this._prefetchItem(this.sessQueue[0]);                /* 首项预取（无答题停留窗可搭） */
      this.enterItem();
    },
    isWarm(r) { return r.stage === 0; },   /* stage=0 即只学过首学，就是昨日重现批 */
    buildWeeklyQueue() {
      /* 断点续做：localStorage 记当日周测进度（首答过的字不再重出，分数/错字保留） */
      const prog = this.weeklyProg();
      const done = new Set(prog ? prog.done : []);
      const q = this.today.scope.filter(h => !done.has(h.id))
        .map(h => ({ type: "weeklyquiz", phase: "weekly", ...h }));
      for (let i = q.length - 1; i > 0; i--) {          /* 乱序（防固定顺序产生位置预期） */
        const j = Math.floor(Math.random() * (i + 1)); [q[i], q[j]] = [q[j], q[i]];
      }
      this.sessQueue = q; this.sessIdx = 0;
      this._promptDone = new Set(); this._quizCache = {};   /* 会话级：玩法指令首题化 + 题目预取缓存 */
      ["ok", "no", "ding", "win"].forEach(s => this._preloadFile(`sfx_${s}.wav`));
      this._prefetchItem(this.sessQueue[0]);                /* 首项预取 */
      this.weeklyScore = prog ? prog.score : 0;
      this.weeklyTotal = this.today.scope.length;
      this.weeklyWrong = prog ? prog.wrong : [];
      this.weeklyWrongIds = prog ? prog.wrongIds : [];
      if (!q.length) return this.finishSession();   /* 已答完未结算（最后一题答完即退出） */
      this.enterItem();
    },
    weeklyKey() {
      /* 周测进度键=周号（2026-11-03 拍板跨日续做）：未结算不过期——一天内分几次做、
         或当天没做完次日续做，进度都保留；结算（finishSession）才清键 */
      return "weekly_w" + (this.today.week_no || 0);
    },
    weeklyProg() { try { return JSON.parse(localStorage.getItem(this.weeklyKey())) || null; } catch { return null; } },
    weeklyLeft() { const p = this.weeklyProg(); return p && this.today.scope ? this.today.scope.length - p.done.length : null; },
    weeklySave() {
      localStorage.setItem(this.weeklyKey(), JSON.stringify(
        { done: this.weeklyDoneIds, score: this.weeklyScore, wrong: this.weeklyWrong, wrongIds: this.weeklyWrongIds }));
    },

    /* ---------- 会话推进 ---------- */
    enterItem() {
      const it = this.curItem;
      if (this.readAudioUrl) { URL.revokeObjectURL(this.readAudioUrl); this.readAudioUrl = ""; }
      this.curQuiz = null; this.quizRevealed = false; this.quizCorrect = false; this.curContent = null;
      this.tracing = false; this.traceDone = false; this.traceFallback = false; this.traceWatched = false;
      this.traceMsg = "✏️ 点米字格，跟着橙色笔顺写一写";
      this.findFound = []; this.findMisses = 0; this.findMiss = "";
      this.ncStep = 1; this.readPlaying = false; this.readCur = "";
      this._stopHw();                               /* 换卡卸载：停上一卡描红实例的无限 loop */
      this.stopNarration();
      this.skipFollow();
      if (it && it.type === "newcard") this.learnDone = false;
      if (!it) return this.finishSession();
      if (it.type === "newcard") {
        const item = it;
        this.mountContent(it.id).then(() => {
          if (this.curItem !== item) return;          /* 已切卡则不播 */
          this.stepAudio();
        });
      } else if (it.type === "readcard") {
        const item = it;
        /* 中途哑声自愈（2026-11-04 遥测实锤：六关约 10min 后 readcard 范读 played=1/
           model_ms 正常但全程无声、孩子 13 句零跟读）——ctx "running 但输出未开"形态：
           onended 照常触发，v49 超时自愈（onended 永不来）不覆盖。此处仍在 gate summary
           点击→advance 的同步栈内（用户激活窗口），弃旧建新借手势 resume，等效每次进
           朗读卡重启音频。LRU buffer 跨 ctx 复用与 v49 _ctxSus 自愈同模式，不清缓存 */
        this._resetPlayCtx(); this._ensurePlayCtx();
        this.loadReadLines().then(async () => {
          if (this.curItem !== item) return;
          this._beginNarr(9000);                     /* 指令播放期点行不作废跟读启动（定时器锁） */
          await this.playFile("t_readcard");
          this._endNarr();
          if (this.curItem === item) this.startReadSession();   /* 会话录音 + 逐句跟读槽 */
        });
      } else if (it.type.endsWith("quiz")) {
        this.startQuiz(it);
      }
    },
    async mountContent(hid) {
      /* 预取命中直接用（一次性）：错题半卡/朗读卡不再现场等网络 */
      let r = (this._contentCache || {})[hid];
      if (r) delete this._contentCache[hid];
      else r = await api("GET", `/api/hanzi/${hid}/full`);
      let words = [], sentList = [];
      try { words = r.content && r.content.words ? JSON.parse(r.content.words) : []; }
      catch { words = []; }
      try { sentList = r.content && r.content.sentences ? JSON.parse(r.content.sentences) : []; }
      catch { sentList = []; }
      if (!sentList.length && r.content && r.content.sentence) sentList = [r.content.sentence];
      this.curContent = r.content ? { ...r.content, words, sentList, hanzi: r.hanzi }
                                  : { hanzi: r.hanzi, words, sentList };
    },
    _stopHw() {
      /* HanziWriter 实例治理（2026-10-30 走查）：loopCharacterAnimation 是 loop:true
         无限渲染链，实例不随卡片卸载自停——旧实例闭包滞留 detached DOM+字形数据且
         同 el 重复 create 会双链叠跑。换卡/跳步/退出时统一 pause */
      if (this._hw) { try { this._hw.pauseAnimation(); } catch { } }
      this._hw = null;
    },
    mountTracer(char) {
      this._stopHw();
      this.traceDone = false; this.traceFallback = false;   /* 写步门槛（v32）：写对才放行，重挂即重置 */
      /* 首见强制看笔顺一遍，复见直写（2026-11-03 拍板）：stroke_seen 服务端持久化，
         中途退出重进/跨会话都不会再强制重看 */
      const seen = !!(this.curItem && this.curItem.stroke_seen);
      this.traceWatched = seen;
      const el = document.getElementById("hw-" + this.curItem.id);
      if (!el || !window.HanziWriter) { this.traceFallback = true; return; }
      try {
        const w = HanziWriter.create(el, char, {
          width: 210, height: 210, padding: 5, showOutline: true,
          strokeAnimationSpeed: 1.2, strokeColor: "#ff9800", outlineColor: "#ffe0b2",
          charDataLoader: (c, done) =>
            fetch(`/static/strokes/${encodeURIComponent(c)}.json`)
              .then(r => { if (!r.ok) throw 0; return r.json(); })
              .then(done)
              .catch(() => { this.traceFallback = true; done(null); }),   /* 笔画数据缺失：门槛降级放行 */
        });
        this._hw = w;
        if (seen) {
          this.traceMsg = "✏️ 点米字格跟着写一写";
          w.loopCharacterAnimation();              /* 复见：循环演示供对照，不强制 */
        } else {
          /* 完整笔顺演示一遍（v36 拍板沿用）；完毕置服务端标记+转循环演示。
             换卡/跳步后旧实例的回调以 _hw 引用守卫拦下 */
          this.traceMsg = "👀 先看一遍笔顺演示…";
          w.animateCharacter({ onComplete: () => {
            if (this._hw !== w) return;
            this.traceWatched = true;
            this.traceMsg = "✏️ 看完啦，点米字格跟着写一写";
            api("POST", `/api/hanzi/${this.curItem.id}/stroke_seen`).catch(() => {});
            w.loopCharacterAnimation();
          } });
        }
        el.onclick = () => {
          if (!this.traceWatched) { this.holdTip("先看完一遍笔顺演示哦 👀"); return; }
          this.startTrace(el, w, char);
        };
      } catch (e) { this.traceFallback = true; }   /* 描红失败不阻塞学习流：门槛降级放行 */
    },
    replayStrokes() {                              /* 复见场景想再看：可点重播（不强制） */
      const w = this._hw;
      if (!w || this.tracing) return;
      this.traceWatched = true;
      w.animateCharacter({ onComplete: () => { if (this._hw === w) w.loopCharacterAnimation(); } });
    },
    startTrace(el, w, char) {
      if (this.tracing) return;
      this.tracing = true;
      el.classList.add("tracing");
      this.traceMsg = "跟着橙色提示，写下第 1 笔吧 ✍️";
      w.quiz({
        leniency: 1.4, showHintAfterMisses: 1,
        onCorrectStroke: (d) => { this.traceMsg = `真棒！第 ${d.strokeNum + 1} 笔写对了，继续～`; this.playSfx("ok", 0.3); },
        onMistake: () => { this.traceMsg = "没关系，再试一次～"; },
        onComplete: () => {
          el.classList.remove("tracing"); el.classList.add("done");
          this.tracing = false; this.traceDone = true;   /* 写对=解锁下一步（v32） */
          this.traceMsg = "写完啦 🎉 再点一下还能再写一遍";
          this.playChar(char);
        },
      });
    },
    /* ---------- 今日朗读卡（今日新字例句串读，目标字高亮） ---------- */
    async loadReadLines() {
      const news = (this.readNews && this.readNews.length) ? this.readNews : (this.today.new || []);
      const lines = await Promise.all(news.map(async h => {
        try {
          const r = await api("GET", `/api/hanzi/${h.id}/full`);
          let sents = [];
          try { sents = r.content && r.content.sentences ? JSON.parse(r.content.sentences) : []; }
          catch { sents = []; }
          return { char: h.char, sent: sents[0] || (r.content && r.content.sentence) || "" };
        } catch { return { char: h.char, sent: "" }; }
      }));
      this.readLines = lines.filter(l => l.sent);
    },
    readHtml(sent) {
      let out = sent;
      for (const h of (this.today.new || [])) out = out.split(h.char).join(`<b>${h.char}</b>`);
      return out;
    },
    readPlayAll() {
      if (this.narrLock) return;                       /* 指令播放期点连读会作废跟读启动（同点行守卫） */
      if (this.readPlaying) { this.stopNarration(); this.readPlaying = false; this.readCur = ""; return; }
      this.readPlaying = true;
      this._narrStop = false;
      (async () => {
        for (const l of this.readLines) {
          if (this._narrStop) break;
          this.readCur = l.char;
          await this.playFile(`s_${l.char}_1`);
        }
        this.readPlaying = false; this.readCur = "";
        if (!this._narrStop) this.playSfx("ding");       /* 连读完"叮"提示（不用啰嗦语音指令） */
      })();
    },
    _qtOf(it) {                                  /* 复习/周测题题型会话内固定（randType 结果挂 it，预取与出题一致） */
      return it.quiz_type || it._qt || (it._qt = this.randType(it));
    },
    _preloadFile(fname) {                        /* 音频预载（完整文件名含扩展名；LRU 入队，失败静默） */
      const ctx = this._ensurePlayCtx();
      if (ctx) this._loadBuf(ctx, `/static/audio/${encodeURIComponent(fname)}`).catch(() => { });
    },
    _preload(name) { this._preloadFile(name + ".mp3"); },
    _preloadQuizAudio(q) {                       /* 题干音频按题型确定（与 startQuiz 自动播音同源） */
      const t = q.quiz_type;
      if (t === "listen" || t === "find") this._preload(q.hanzi);
      else if (t === "rev" || t === "lsent" || t === "defi") this._preload(q.audio);
      else if (t === "sent") this._preload(`s_${q.hanzi}_1`);
    },
    _prefetchItem(it) {
      /* 会话推进统一预取（2026-11-02 卡顿整治）：题面+题干音频+内容+笔画数据。
         旧 _prefetchNext 只覆盖"答对停留窗的下一道 quiz"，四个盲区（错题重测、
         每关首题、新字卡内容、朗读卡）全部现场串行 await——公网 RTT 下"出题中…"
         闪烁+音频迟滞。预取源全部确定性可知（题型固定/音频 URL 在 payload 内） */
      if (!it || !this._quizCache) return;
      if (it.type === "newcard") {
        if (this._contentCache && this._contentCache[it.id]) return;
        api("GET", `/api/hanzi/${it.id}/full`).then(r => {
          (this._contentCache ||= {})[it.id] = r;
          /* 四步卡音频全套预载：字音/字源故事/组词连读/逐词/逐句（URL 全部可预知） */
          const ch = it.char;
          this._preload(ch); this._preload(`e_${ch}`); this._preload(`w_${ch}`);
          try {
            const words = r.content && r.content.words ? JSON.parse(r.content.words) : [];
            const sents = r.content && r.content.sentences ? JSON.parse(r.content.sentences)
              : (r.content && r.content.sentence ? [r.content.sentence] : []);
            words.forEach((w, i) => this._preload(`n_${ch}_${i + 1}`));
            sents.forEach((s, i) => this._preload(`s_${ch}_${i + 1}`));
          } catch { }
        }).catch(() => { });
        return;
      }
      if (it.type === "readcard") {
        this._preload("t_readcard");
        (this.readNews || []).forEach(h => this._preload(`s_${h.char}_1`));
        return;
      }
      if (!it.type.endsWith("quiz")) return;
      const key = this._qtOf(it) + ":" + it.id;
      if (this._quizCache[key]) { this._preloadQuizAudio(this._quizCache[key]); return; }
      api("GET", `/api/quiz?hanzi_id=${it.id}&quiz_type=${this._qtOf(it)}`)
        .then(q => {
          this._quizCache[key] = q;
          this._preloadQuizAudio(q);
          /* 答错半卡要拉内容：一并预取（mountContent 命中免现场等） */
          if (!(this._contentCache || {})[it.id])
            api("GET", `/api/hanzi/${it.id}/full`)
              .then(r => { (this._contentCache ||= {})[it.id] = r; }).catch(() => { });
        }).catch(() => { });
    },
    async startQuiz(it) {
      const qt = this._qtOf(it);
      const key = qt + ":" + it.id;
      /* 预取命中直接用（一次性）；未命中现拉，弱网失败并入素材缺失同路径 */
      this.curQuiz = (this._quizCache && this._quizCache[key]) ||
        await api("GET", `/api/quiz?hanzi_id=${it.id}&quiz_type=${qt}`).catch(() => null);
      if (this._quizCache) delete this._quizCache[key];
      if (!this.curQuiz) {                          /* 素材缺失兜底：防永久卡"出题中…" */
        this.traceMsg = "这题先跳过啦";
        setTimeout(() => this.advance(), 1200);
        return;
      }
      this.quizPending = it;
      const aqt = this.curQuiz.quiz_type;   /* 实际题型（可能与请求不同：题库退化） */
      const PROMPT = { listen: "q_listen", mean: "q_mean", sent: "q_sent",
                       find: "q_find", pin: "q_pin", rev: "q_rev",
                       lsent: "q_lsent", defi: "q_defi" };
      /* 玩法指令每会话每题型只播首次（2026-10-30 用户反馈：每题都播听着烦，
         且指令 2-3s 比答题慢，常"做完了还没读完"）；后续题自动播音不再等指令 */
      (this._promptDone ||= new Set());
      const firstTime = PROMPT[aqt] && !this._promptDone.has(aqt);
      if (firstTime) { this.playTts(PROMPT[aqt]); this._promptDone.add(aqt); }
      /* 自动播音用局部引用：孩子快速答完 advance 后 curQuiz 已置空，读它即崩；
         且切题后不再播（curQuiz 守卫），防旧题字音叠到新题；
         延迟=指令实际时长+300ms（固定延迟曾与指令尾叠音） */
      const qz = this.curQuiz;
      const promptMs = firstTime ? (await this.audioDur(`t_${PROMPT[aqt]}`) || 2) * 1000 + 300 : 400;
      if (aqt === "listen") setTimeout(() => { if (this.curQuiz === qz) this.playChar(qz.hanzi); }, promptMs);
      else if (aqt === "find") setTimeout(() => { if (this.curQuiz === qz) this.playChar(qz.hanzi); }, promptMs);
      else if (aqt === "rev" || aqt === "lsent" || aqt === "defi")
        setTimeout(() => { if (this.curQuiz === qz) this.playFile(qz.audio); }, promptMs);
    },
    /* ---------- 找一找（点出句/词中所有目标字） ---------- */
    findTap(li, ti, ch) {
      const q = this.curQuiz;
      if (!q || q.quiz_type !== "find" || this.quizRevealed) return;
      const key = `${li}-${ti}`;
      if (this.findFound.includes(key)) return;
      if (ch === q.hanzi) {
        this.findFound.push(key);
        this.playChar(q.hanzi);
        if (this.findFound.length >= q.count) this.findDone(false);
      } else {
        this.playSfx("no");
        this.findMiss = key;
        setTimeout(() => { this.findMiss = ""; }, 400);
        this.findMisses++;
        if (this.findMisses >= 2) this.findDone(true);
      }
    },
    findDone(failed) {
      if (!failed) { this.playSfx("ok"); this.burst(); }
      else setTimeout(() => {              /* 答错自动讲解（同 pickOpt：字音→语境音频） */
        if (this.curQuiz && this.quizRevealed && !this.quizCorrect) this._wrongAudio(this.curQuiz);
      }, 600);
      this.quizRevealed = true;
      this.quizCorrect = !failed;
      this.submitQuiz(this.quizCorrect);   /* advance 统一由 submitQuiz 负责（双定时器曾跳卡） */
    },
    randType(it) {
      /* 题型按记忆阶段分层（wordslearn d2/d5/d12 模式，2026-11-02 用户拍板"复习设计低级"重构）：
         stage0(D+1 昨日重现)=再认：听音选字/组词——刚学一天只求音形义再认；
         stage1(D+4)=语境：句子填空/组词——晋级证据须是"用得出"而非"认得出"；
         stage≥2(D+11/D+25)=深阶：听句辨字/字义——接近掌握，考句境与字义映射。
         find（词句中点目标字）=纯字形匹配游戏，不认识字也能玩，砍出复习/周测池。
         周测 scope 无 stage → 通用池。题库缺题型由后端 _QB_DEGRADE 退化链兜底。 */
      const st = it && it.stage;
      const pool = st === 0 ? ["listen", "mean"]
        : st === 1 ? ["sent", "mean"]
        : st >= 2 ? ["lsent", "defi", "sent"]
        : ["listen", "mean", "sent", "lsent", "defi"];
      return pool[Math.floor(Math.random() * pool.length)];
    },
    async _wrongAudio(qz) {
      /* 答错讲解链：字音（确认读音）→ 题型语境音频（半卡期间自动重听）——
         defi 答错=释义↔字形映射没建立，重听释义是闭环核心；mean/sent 播组词连读；
         rev/lsent 重播词/句。目标用户不识字，半卡只有文字词句等于没讲 */
      await this.playChar(qz.hanzi);
      if (this.curQuiz !== qz || !this.quizRevealed) return;   /* 半卡期已切题则止链 */
      const t = qz.quiz_type;
      if (t === "defi" || t === "rev" || t === "lsent") return this.playFile(qz.audio);
      if (t === "mean" || t === "sent") return this.playFile(`w_${qz.hanzi}`);
    },
    optClass(o) {
      if (!this.quizRevealed) return "";
      return o === this.curQuiz.answer ? "good" :
        (o === this.quizPick ? "bad" : "");
    },
    async pickOpt(o) {
      if (this.quizRevealed) return;
      this.stopNarration();                       /* 已作答即停指令/遗留音（防 s_ 回放与指令尾叠） */
      this.quizPick = o; this.quizRevealed = true;
      const qz = this.curQuiz;
      this.quizCorrect = o === qz.answer;
      this.playSfx(this.quizCorrect ? "ok" : "no");
      if (this.quizCorrect) {
        this.burst();                             /* 答对星星迸发：夸奖要看得见 */
        if (qz.quiz_type === "sent")
          setTimeout(() => this.playFile(`s_${qz.hanzi}_1`), 450);   /* 音效后再听整句 */
      } else {
        setTimeout(() => {                        /* 答错 600ms 后自动讲解：字音→题型语境音频 */
          if (this.curQuiz === qz && this.quizRevealed) this._wrongAudio(qz);
        }, 600);
      }
      await this.submitQuiz(this.quizCorrect);
    },
    burst() {                                      /* 星星迸发 0.8s（答题正确路径统一触发） */
      this.quizBurst = true;
      clearTimeout(this._burstT); this._burstT = setTimeout(() => this.quizBurst = false, 800);
    },
    async submitQuiz(correct) {
      const it = this.quizPending;
      /* 弱网兜底：submit 失败只丢流水不卡推进（答对 900ms 定时 advance 曾因它不调度） */
      const send = (body) => api("POST", "/api/submit", body).catch(e => console.warn("submit 弱网失败", e));
      if (it.type === "gatequiz") {
        /* 关题：调度已在卡片完成时注册（learn 幂等），此处只记流水+关分；答错看半卡、
           错题克隆推本关末重测（quizContinue，v40 拍板方案 A：立即重测短时记忆作弊、
           提取强度低，间隔=本关剩余题数） */
        const first = !it._answered; it._answered = true;
        await send({ hanzi_id: it.id, phase: "learn",
          quiz_type: this.curQuiz.quiz_type, correct });
        if (first) {
          if (correct) this.gateScores[it.gate]++;
          else this.gateWrong[it.gate].push(this.curQuiz.hanzi);
        }
        if (correct) { this._prefetchItem(this.sessQueue[this.sessIdx + 1]); setTimeout(() => this.advance(), 900); }
        else this.mountContent(it.id);
        return;
      }
      /* 重测（quizContinue 后再答）不提交不计数：错题回炉轨迹以首答为准，周测分不虚高 */
      const first = !it._answered; it._answered = true;
      /* 复习/周测/抽检答错：拉内容给"先看半卡再重测" */
      if (!correct) this.mountContent(it.id);
      if (first) {
        await send({ hanzi_id: it.id, phase: it.phase,
          quiz_type: this.curQuiz.quiz_type, correct });
        if (!correct && this.phase === "weekly") {
          this.weeklyWrong.push(this.curQuiz.hanzi);
          this.weeklyWrongIds.push(it.id);
        }
        if (this.phase === "weekly") { this.weeklyDoneIds.push(it.id); this.weeklySave(); }
      }
      if (correct) {
        if (first && this.phase === "weekly") this.weeklyScore++;
        this._prefetchItem(this.sessQueue[this.sessIdx + 1]);
        setTimeout(() => this.advance(), 900);
      }
    },
    async quizContinue() {
      const it = this.quizPending;
      if (it.type === "gatequiz") {
        /* 关题答错：错题克隆推到本关 gatesummary 前，间隔=本关剩余题数后重测（方案 A）。
           克隆继承 _answered=true → 重测答对不提交不计数（首答已记流水/关分，关分不虚高）；
           重测又错（it._retest）不再推、直接放行——防无限循环，靠 D+1 复习兜底。
           克隆重测走完整 startQuiz：defi/rev/lsent 自动播题干音频（旧路径
           quizContinue 快捷重拉不播释义，2026-11-02 用户实锤"断头路"） */
        if (!it._retest) {
          let pos = this.sessQueue.length;
          for (let k = this.sessIdx + 1; k < this.sessQueue.length; k++) {
            const nx = this.sessQueue[k];
            if (nx.type === "gatesummary" && nx.gate === it.gate) { pos = k; break; }
          }
          this.sessQueue.splice(pos, 0, { ...it, _retest: true });
        }
        return this.advance();
      }
      if (it.type === "reviewquiz" || it.type === "weeklyquiz" || it.type === "auditquiz") {
        /* 复习/周测错题：同题型立即重测到对（不重复提交，回炉轨迹完整；周测有
           结算语义 + D+1 回炉兜底，与关题的推关尾策略不同源） */
        this.quizRevealed = false; this.quizPick = null;
        this.findFound = []; this.findMisses = 0; this.findMiss = "";
        /* 弱网兜底：重拉失败直接过题（防卡在已重置的旧题上） */
        this.curQuiz = await api("GET", `/api/quiz?hanzi_id=${it.id}&quiz_type=${this.curQuiz.quiz_type}`)
          .catch(() => null);
        if (!this.curQuiz) return this.advance();
        if (this.curQuiz.quiz_type === "listen") setTimeout(() => this.playChar(this.curQuiz.hanzi), 400);
        return;
      }
      this.advance();
    },
    advance() {
      if (!this.sessQueue.length) return;          /* 已退出会话：迟到的 submit/跳题定时器不再推进（防甩到完成页） */
      /* 推进即预取下一项（覆盖非答题推进：四步卡下一步/关末结算/错题推队尾）——
         与 submitQuiz 答对停留窗的调用幂等重复（缓存命中即返回） */
      this._prefetchItem(this.sessQueue[this.sessIdx + 1]);
      const done = this.curItem;
      /* 新字卡四步完成 → 提交首学（注册 D+1 复习；分关题只记流水不再动调度） */
      if (done && done.type === "newcard" && !this.learnDone) {
        this.learnDone = true;
        api("POST", "/api/submit", { hanzi_id: done.id, phase: "learn",
          quiz_type: "card", correct: true });
      }
      /* 全部关过完 → 标记当日关完成（重进不重建关队列；键=虚拟日，跳日后同一天不误判已过） */
      if (done && done.type === "gatesummary" && done.isLast)
        sessionStorage.setItem("gateDone_" + this.today.date, "1");
      this.stopNarration();
      this.sessIdx++;
      this.enterItem();
    },
    async finishSession() {
      this.closeMic();
      if (this.phase === "weekly") {
        await api("POST", "/api/weekly/finish", { week_no: this.today.week_no,
          score: this.weeklyScore, total: this.weeklyTotal, wrong_ids: this.weeklyWrongIds })
          .catch(e => console.warn("weekly/finish 弱网失败", e));   /* 结算可丢，流程照走（防空白卡死） */
        localStorage.removeItem(this.weeklyKey());   /* 已结算，清断点 */
        this.phase = "weekly-done";
      } else {
        this.doneStats.new = this.today.new.length || (this.today.learned_today || []).length;   /* 重进恢复场景 new 为空 */
        this.doneStats.review = this.today.review_done != null ? this.today.review_done
          : this.today.review.length + this.today.audit.length;   /* 重进后 review 只剩未答项，取当日已答真源（scheduler review_done） */
        this.phase = "done";
      }
      this.playSfx("win");
      api("POST", "/api/report/daily").catch(() => {});   /* 学习日报（fire-and-forget）：完成即送家长邮箱 */
      const me = await api("GET", "/api/me"); this.progress = me.progress;
    },
    async goParent(from) {
      this._parentFrom = from || null;          /* 返回回来源页——不能用 boot()：任何 stage 降级（册内容
                                                  事故等）都会把"返回"变成重走引导流（2026-11-01 实锤） */
      this.report = await api("GET", "/api/report/weekly");
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.phase = "parent";
    },
    parentBack() {
      this.phase = this._parentFrom || "today-intro";   /* today 数据在进入前已载，直接回显 */
    },
  },
  async mounted() {
    this.boot();
    /* iPad/macOS Safari 系统随时可挂起 AudioContext（切后台/锁屏/久置）：任何用户手势内
       兜底创建+resume。事件必须四件套——Safari 的 WebAudio 解锁只可靠认传统手势事件
       （click/touchend/keydown），pointerdown(≈touchstart 时刻)不被认定：v43 只挂
       pointerdown 时摸底验证轮在 iPad 依旧全程无声（2026-11-02 二次实锤；主学习流程
       正常是因为 startToday 的 click 处理器内另有解锁）。Chrome 对事件类型不挑，多挂
       无害；passive 且只做状态检查+幂等 resume，开销可忽略 */
    for (const ev of ["pointerdown", "click", "touchend", "keydown"])
      window.addEventListener(ev, () => this._ensurePlayCtx(), { passive: true });
  },
});
app = root.mount("#app");
window.ziapp = app;
