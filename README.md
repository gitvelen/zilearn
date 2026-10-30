# zilearn 字乐

面向自家孩子的网页版儿童识字应用（对标洪恩识字），严格跟随人教版（部编版）语文教材识字表。

## 架构（复用 wordslearn 模式）

- **后端**: FastAPI + SQLite（`app/main.py`），uvicorn 8000
- **前端**: Vue3 自托管单文件（无构建链）+ Hanzi Writer（描红/笔顺，自托管）
- **数据管线**: `fetch_zi.py`（课本 PDF→字表）、`fetch_strokes.py`（笔顺 JSON）、`fetch_audio.py`（edge-tts 字音）
- **静态资源**: `app/static/{audio,strokes}/` 全量自托管（304 音频 + 300 笔顺）

## 核心设计（2026-10-26 定稿）

1. **周循环 + 日日复习**: 每周 6 学习日 + 1 周测日（第 7 天，不投新字）。
   调度 `INTERVALS=[1,3,7,14]`：首学 D0 → 复习 D+1 → D+4 → D+11 → D+25 → mastered（间隔从每次复习日起算）。
   答错回炉 stage=1/next=D+1；mastered 每日抽检 3 个，抽检错打回 learning。
2. **周测=批量复习事件**: 周测题按 phase='weekly' 提交，对→按当前 stage 推进、错→回炉；
   范围=本周新字+本周错字+当日到期（一锅端防同字重复）。周测对 1 题得 1⭐。
3. **摸底三步**（表格确认）: 速选认识 → 1 题四选一验证（对→skipped 剔除、错→回池）→ 计划按教材序续补。
4. **出题**: listen(听音选字)/mean(组词补全)/sent(句子填空)/find(找一找)；**测验选项一律不显示拼音**（防泄漏）；
   干扰项形近优先（一年级形近混淆种子表 178 字）；
   mean 干扰字代入挖空不得成真词、sent 例句目标字恰好 1 次且干扰字不入句、find 复现≥2（准确性硬校验）；
   退化链 find→sent→mean→listen。
5. **多音字**: 课内定音为准（fetch_zi 从课本 PDF 拼音破译+人工确认 8 个）；音频按 (字,读音) 双文件，
   TTS 词境消歧（"长短"定 cháng，alt 无词境则跳过防默认音错读）。
6. **每日结构**: 昨日重现(D+1批) → 新字×N（四步卡：认/词/句/写，含跟读）→ 复习沉淀+抽检
   → 分关测验 6 关（听音/拼音/组词/听词/读句/写字关，各关乱序、答错半卡重测到对）→ 今日朗读卡。
   计划页实时预估用时，>30 分钟黄色警示（不硬限）。
   **跟读（2026-10-30 v24，鼓励式可选）**: 范读→留白监听，读到即夸（✓+音效）；不读不重试不停留、
   不锁"下一步"（仅音频范读期锁防没听就走）。检测参数与 follow_log 遥测不变。
   （2026-10-27 版"跟读强制+两轮重试"由用户 10-30 拍板调整，对齐 wordslearn 哲学：跟读是机会不是门槛。）
7. **音频**: 字音领读单遍（"天。"，rate -20%）；e_/w_/s_{char}.mp3 字源故事/组词/例句朗读，
   新字卡自动连播（字音→故事→组词→例句），各节可点 🔃 重听。
   反馈音效 sfx_ok/no/win.wav（Python 合成，答对/答错/过关）。

## 数据源

- **字表: data/zi.txt（权威源，用户按 2024 最新课本录入，10 册 3152 字）**。
  `python3 fetch_zi.py books` 生成 hanzi_books.csv；同字跨册按首现册归属（INSERT OR IGNORE）。
  一上 281 字（课本标注 280）。旧 2016 版 PDF 管线（fetch_zi.py parse）保留作历史。
- 拼音: pypinyin 默认音 + 2016 课内定音沿用（子zǐ/地de/只zhī/长cháng/数shù/着zhe/行xíng）；
  常用多音字确认清单见 fetch_zi.py books 输出（爪/答/更等待课本核对）。
- 出题干扰池: 严格同学册（gen_quiz book 参数），形近种子优先。
- 笔顺/音频: 仅生成 1a（281+4）；其他册用到时跑 fetch_strokes.py / fetch_audio.py（脚本已加 book 过滤）。
- 内容: zi_content.json 91 字（前 84 字全覆盖+旧 36 中靠后者）；缺内容字卡片降级显示。

## 部署（已上线）

- **https://zi.velen.ltd**（Caddy 自动 HTTPS，反代 zilearn:8000，main_default 网络）
- 修改 Caddyfile 后必须 `docker restart main-caddy-1`（单文件挂载绑定 inode，reload 读不到重写后的文件）
- 容器内 pip 需清华源（直连 pypi 不可达）
- `docker compose up -d --build` 重建代码后：`docker exec zilearn python seed.py hanzi`（幂等）

## 测试（本地与线上同套）

```bash
python3 tests/e2e.py                        # API + 状态机 + 题库出题（47 断言）
python3 tests/ui_test.py                    # 浏览器全流程走查: 引导+学习日+跟读gate+周测（35 断言）
python3 tests/ui_test.py https://zi.velen.ltd   # 打线上
```
smoke.py/smoke2.py 已删除（旧版 UI 断言过时，由 ui_test.py 取代）。
ui_test 需 fake 麦克风 flags（launch args 已内置）跑跟读 gate 路径。

测试用系统 Chrome（--no-sandbox），截图落 tests/shots/。

## 登录

用户 mido，密码见 seed 输出（`python3 app/seed.py users` 首次打印，不入库）。

## P1 待办

- **绘本子系统**（用户定级"精品"）: 50 本离线预生成，三层字级（目标字≥3 复现高亮/已学字/脚手架字注音≤15%），
  程序校验门禁（复现数/生字比/句长/注音），文案人工精修；读完 +1⭐
- 字表换 2024 修订版（等 mido 课本核对）
- 内容全量生产（当前 36/300 字，流水线：组词例句字源 → zi_content.json → seed）
- 部署到 Caddy 子域（需定域名）
- invariant 级长会话 fuzz 测试（3 周连续随机交互 + 10 组不变式）
- 象形字演变图（~150 字，开源字源调研）替代/增强字源故事文本
