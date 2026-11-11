# OpenOntology 规格说明书（TK-ONTO-013 · AlphaGo 蒙特卡洛树搜索 (MCTS) 在符号世界模型中的多步沙盘推演高清视频）

## 1. 目标与并发隔离纪律

基于已验证通过的 `examples/mcts_world_model_demo.py`（`TK-ONTO-012`），在独立目录 `/Users/guci/work/open-ontology/video_mcts/` 下编写自动化视频生成管线，生成一条 **1920×1080 高清、包含 MCTS 搜索树可视化 SVG + 真实沙盘推演数据对比、带自然中文 TTS 配音与大号底部字幕** 的演示视频：
- **最终产物路径**：`/Users/guci/work/open-ontology/video_mcts/mcts_world_model_demo.mp4`

> **⚠️ 并发隔离铁律**：
> 只准在 `/Users/guci/work/open-ontology/video_mcts/` 目录下创建和写入文件，严禁修改 `open_ontology/`、`examples/`、`tests/` 或其他 `video*` 目录。

---

## 2. 本机工具链路径

- **Playwright**：Python `playwright.sync_api`（使用 Chromium `1920x1080` 视口渲染 6 幕高清 PNG）
- **TTS 配音**：`/Users/guci/.local/bin/edge-tts --voice zh-CN-YunxiNeural`（若网络异常回退 `/usr/bin/say -v Tingting`）
- **FFmpeg**：`/opt/homebrew/bin/ffmpeg` 与 `/opt/homebrew/bin/ffprobe`

---

## 3. 六幕分镜剧本与配音台词（`video_mcts/build_mcts_video.py`）

脚本必须真实调用 `examples.mcts_world_model_demo` 获取实跑数据（禁止硬编码假数字），渲染为 `1920x1080` 深色科技风双栏/树图画面：

| 幕次 | 顶部标题 | 核心视觉内容（左屏沙盘/树图 + 右屏实测数据） | 中文配音与底部字幕 |
|---|---|---|---|
| **Scene 1** | `1/6 · 为什么单步推演不够？企业决策中的贪心陷阱` | • 左屏：展示三波次急救冷链运单（`ORD-1` 胰岛素 $\le 4\text{h}$、`ORD-2` 特级器官血浆 $\le 1.5\text{h}$ 且 2 级冷链、违约重罚 5 万、`ORD-3` 疫苗 $\le 3\text{h}$）与三辆冷链车（`TRK-FAST`、`TRK-MID`、`TRK-HEAVY`）<br>• 右屏：剖析为什么普通单步 Agent 只看眼前一步必然掉进局部贪心陷阱 | 在复杂企业调度中，只看眼前一步的贪心决策往往会酿成大祸。比如医院同时下达三张连环急救运单，其中第二单是特级器官血浆，硬性要求一点五小时内送达且具备二级超低温冷链，一旦断供将面临五万元重罚。 |
| **Scene 2** | `2/6 · 方法 A 翻车实录：单步贪心策略撞上死胡同` | • 左屏：展示单步贪心路径图：第 1 步因 `TRK-FAST` 运费最便宜（400 元，先验 $P=0.553$）将其派给普通单 `ORD-1`；第 2 步处理 `ORD-2` 时无车可用，触发红色警报 `InvariantError: ETA_OR_COLD_LEVEL_VIOLATION`<br>• 右屏：展示贪心终局账单：履约 1/3，违约重罚 `-65,000` 元，终局净价值 `V = -55,400` 元 | 看普通单步贪心智能体的翻车现场：第一步处理普通胰岛素订单时，它贪图王牌快车运费最便宜，直接把唯一一辆超低温快车派了出去；结果到了第二步处理特级血浆急单时，剩下的车全部违反时效与冷链铁律，当场触发死局，造成负五万五千四百元的惨重亏损。 |
| **Scene 3** | `3/6 · 破局之道：AlphaGo MCTS 在符号世界模型中分叉推演` | • 左屏：展示 `fork_runtime` 原理（利用 SQLite 原生内存 `backup` 毫秒级克隆平行宇宙沙盘）与 MCTS 四阶段循环（PUCT 选择 $\to$ 符号裁判扩展与 `InvariantError` 剪枝 $\to$ `@spec.function` 终局价值评估 $V(s)$ $\to$ 反向传播）<br>• 右屏：展示沙盘隔离实证：主库写入 `0` 次，23 个平行沙盘宇宙完全隔离 | AlphaGo 的蒙特卡洛树搜索，本质上就是在确定性的符号世界模型中做多步沙盘推演。OpenOntology 通过毫秒级内存克隆开辟平行宇宙沙盘，由 Action 前置护栏充当符号规则裁判自动剪枝非法死局，由纯业务 Function 精算终局价值，并沿搜索树反向传播更新动作价值 Q。 |
| **Scene 4** | `4/6 · 80 次 MCTS 树搜索可视化：PUCT 如何逃离贪心陷阱` | • 全景/双栏展示真实的 MCTS 搜索树 SVG 拓扑图与根节点统计表：<br>`ORD-1 -> TRK-FAST`: 先验 `P=0.553` 最高，但探明死局后 `Q=-55,400`，仅访问 `N=6` 次即被抛弃！<br>`ORD-1 -> TRK-MID`: `P=0.246`, `N=10`, `Q=+18,700`<br>`ORD-1 -> TRK-HEAVY`: 先验 `P=0.201` 最低，但解锁三单全通，`Q=+44,600`，吸引 `N=64` 次访问胜出！<br>符号裁判累计剪枝 `12` 个非法节点 | 在八十次蒙特卡洛沙盘推演中，尽管第一步派王牌快车的先验概率高达零点五五三，但树搜索往下推演一步就撞上符号裁判剪枝，期望价值暴跌至负五万五千四百元，仅试探六次就被果断放弃；而先验最低的长途车路线因为把王牌救命车留给了第二步的特级血浆单，吸引了六十四次访问脱颖而出！ |
| **Scene 5** | `5/6 · 全局最优三步序列落子与时光机一键悔棋` | • 左屏：展示 MCTS 锁定的全局最优三步序列：<br>Step 1: `ORD-1 -> TRK-HEAVY` (¥1,100)<br>Step 2: `ORD-2 -> TRK-FAST` (¥400，救命车精准留给血浆单！)<br>Step 3: `ORD-3 -> TRK-MID` (¥900)<br>• 右屏：展示主库零污染指纹（搜索期间主库写入 `0`），正式 `submit_action` 后净赚 `+44,600` 元（比贪心策略多赚 `100,000` 元！），并演示 `revert_action` 时光机撤回与重做 | 最终，MCTS 锁定全局最优三步序列：第一步派长途车送普通单，第二步把王牌超低温快车精准留给特级血浆急单，第三步派中型车送疫苗单。三单全部合法履约，净收益正四万四千六百元，比单步贪心策略逆转整整十万元！整个搜索期间主库零写入零污染，落子后仍支持时光机一键悔棋。 |
| **Scene 6** | `6/6 · 终极范式：LLM 提直觉先验 + Ontology 做符号树搜索` | • 展示「企业级 AlphaGo 决策架构」总结面板：<br>LLM = 策略先验网络 $P(s,a)$<br>`OpenOntology` (`Objects/Links/Actions`) = 确定性符号世界模型与规则裁判<br>`@spec.function` = 精确价值网络 $V(s)$<br>`OntologyMCTSPlanner` = 多步沙盘推演规划器 | 大模型提供直觉先验，OpenOntology 提供零幻觉的符号世界模型与规则裁判，配合蒙特卡洛多步沙盘推演，让企业 AI 从看一步走一步的盲目试错，跃升为算清全局终局价值的 AlphaGo 级决策系统。 |

---

## 4. 视频验收要求

1. 生成 `/Users/guci/work/open-ontology/video_mcts/mcts_world_model_demo.mp4`（`1920x1080`, H.264 + AAC, `yuv420p`, `30fps`）。
2. 使用 `ffprobe` 验证分辨率 `1920x1080`、音轨完整、时长在 `70s ~ 160s` 之间、文件大小 $> 1\text{MB}$ 后汇报。
