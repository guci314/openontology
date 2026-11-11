# OpenOntology 规格说明书（TK-ONTO-011 · OpenOntology 架构与第一性原理高清讲解视频自动生成）

## 1. 目标与并发隔离纪律

将我们在 Obsidian `/Users/guci/obsidianRep/guci/100-Areas/110-AI/Palantir/` 下沉淀的完整理论体系与 `/Users/guci/work/open-ontology` 的真实内核架构，制作成一条 **1920×1080 高清、包含精美架构图/时序图/真实内核指标、带自然中文 TTS 配音与大号底部字幕** 的《OpenOntology 架构与第一性原理》讲解视频：
- **最终产物路径**：`/Users/guci/work/open-ontology/video_arch/open_ontology_architecture.mp4`

> **⚠️ 并发隔离铁律**：
> 1. **只准在独立目录 `/Users/guci/work/open-ontology/video_arch/` 下创建和写入文件**（严禁修改 `open_ontology/`、`examples/`、`tests/`、`video/` 或 `video_mdm/` 下的任何文件）！
> 2. 可以只读导入 `open_ontology` 内核跑内存自检获取真实元数据，严禁运行涉及外部 Odoo 的重置操作。

---

## 2. 本机工具链路径

- **Playwright**：Python `playwright.sync_api`（使用 Chromium `1920x1080` 视口将高颜值 SVG/CSS 架构图分镜渲染为高清 PNG）
- **TTS 配音**：优先使用 `/Users/guci/.local/bin/edge-tts --voice zh-CN-YunxiNeural`（若网络异常则自动回退 `/usr/bin/say -v Tingting`）
- **FFmpeg**：`/opt/homebrew/bin/ffmpeg` 与 `/opt/homebrew/bin/ffprobe`

---

## 3. 六幕分镜剧本与配音台词（`video_arch/build_arch_video.py`）

每一幕均为 `1920×1080` 深色科技风双栏/全景架构信息图（含内联 SVG 矢量架构框图、代码原语高亮卡片、核心对比矩阵与底部大号字幕条）：

| 幕次 | 顶部标题 | 核心视觉内容（SVG 架构图 + 对比卡片） | 中文配音与底部字幕 |
|---|---|---|---|
| **Scene 1** | `1/6 · 第一性原理：用 AlphaGo 架构解释 Palantir 方法论` | • 左栏：展示公式 $\text{AlphaGo} = \text{策略} + \text{世界模型} + \text{规划} + \text{价值} + \text{工具}$，剖析「普通 ReAct Agent」为什么必死（让 LLM 靠幻觉脑补企业世界模型并直接裸调 ERP API）<br>• 右栏：展示「Palantir / OpenOntology 神经符号分工」SVG 架构图（将确定性的**世界模型与规则裁判**从 LLM 中剥离为 Ontology；LLM 只做策略提案；Ontology 是 LLM 的唯一工具；外部 ERP/SaaS 退化为底下的传感器与机械臂） | 为什么普通的 ReAct 智能体在企业里一上生产就翻车？因为人们把策略、世界模型、规划和价值全塞进大模型脑子里靠幻觉脑补，还让大模型直接裸调 ERP 接口。用 AlphaGo 架构一照就能看穿：Palantir 的本质，是把确定性的世界模型和规则裁判从大模型中剥离出来，固化为 Ontology。从此大模型只管提策略，Ontology 成为大模型唯一的工具，而外部业务系统退化为底下的传感器与机械臂。 |
| **Scene 2** | `2/6 · 五层正交架构：100% 业务纯洁性 (PIM vs PSM)` | • 全景展示 `OpenOntology` 五层正交架构 SVG 图：<br>L5 智能与评测层（OSDK / MCP 工具桥 + AIP Evals）<br>L4 动力学沙箱层（`EditBatch` 干跑推演 + 乐观锁 + 审批/悔棋状态机）<br>L3 声明式原语层（`ObjectType` / `LinkType` / `@function` / `@action`）<br>L2 纯业务存储层（SQLite/PG 5 张纯业务表 + `ontology_write_gate` 7 个物理触发器）<br>L1 外部同步总线（Apache Camel EIP 路由，与内核物理隔离） | OpenOntology 采用五层正交设计，严守业务纯洁性铁律。内核只表达纯业务概念，绝不混入任何外部系统表名、HTTP 重试或连接池代码。底层由五张纯业务表和七个数据库物理写门禁触发器锁死，任何绕过 Action 直接写库的企图都会被数据库内核当场熔断；所有脏活累活全部隔离在外部的 Apache Camel 集成总线中。 |
| **Scene 3** | `3/6 · 语义层原语：高维对象图、集合代数与属性主权` | • 左栏：展示「一维扁平编码的有损降维投影」vs「Ontology 正交高维对象图 + `ObjectSet`（`pivot / union / intersect / aggregate`）多视角无损还原」<br>• 右栏：展示 Palantir OSv2 同款属性级主权划分（`source-backed` 源系统属性 vs `__owned__` 本体自有属性 + `ontology_overlay` 补丁表，演示 `replace-base-reapply-owned-overlay` 重刷存活与孤儿熔断） | 在语义层，OpenOntology 拒绝传统系统的一维扁平表结构，用对象、有向关系边与 ObjectSet 集合代数构建高维业务图谱，通过 pivot 群跳转实现跨部门多视角无损算账。同时引入属性级主权划分：同一个对象既能映射外部源系统字段，又能通过 overlay 补丁层挂载 Ontology 自有属性，外部底表全量重刷时自有状态百分之百存活。 |
| **Scene 4** | `4/6 · 动力学层原语：EditBatch 沙箱、有治理的自由与时光机` | • 展示 Action 执行生命期 SVG 流水线：<br>① `simulate_action`（在 `EditBatch` 内存沙箱中零副作用干跑，产出 `ObjectDiff`，支撑 AI 多步推演）<br>② `Preconditions` 与主权高压线（拦截违规并将未遂企图强制记入 `REJECTED` 审计）<br>③ `approval_rule`（Governed Freedom：AI 超阈值自动挂起 `PENDING_APPROVAL` + `read_versions` 乐观锁校验）<br>④ `revert_action`（基于 `diff.inverted()` 一键时光机逆向悔棋） | 在动力学层，所有动作绝不允许直接修改数据库，而是在 EditBatch 内存沙箱中推演并生成可逆的 ObjectDiff 补丁。AI 可以先做零副作用的沙箱推演；提交时若违反业务不变量，未遂企图也会被强制记入审计账本；若触碰大额风控红线则自动挂起等待人工审批，配合读取版本乐观锁与一键时光机逆向回滚，真正实现有治理的自由。 |
| **Scene 5** | `5/6 · 寄居蟹式演进：从 0% 寄生老系统到 100% 去掉 SaaS` | • 展示四阶段主权刻度尺 SVG 图：<br>阶段 1（0% Owned · 纯镜像寄生：不改老系统一分一毫，第一周出全局诊断）<br>阶段 2（20% Owned · 表皮长肉：用 `overlay` 3 天上线 AI 风控与协同新属性）<br>阶段 3（70% Owned · 核心脏器内迁：状态机收归 Ontology，老系统降级为下游记账机）<br>阶段 4（100% `__owned__=True` · 拔掉插头去掉 SaaS：旧系统断电退役，上层 AI 与业务代码零改动） | 为什么属性级主权能避免掀桌子重写的高风险？因为它把系统替换变成了一把平滑滑动的刻度尺：第一周零侵入寄生在旧 ERP 或云端 SaaS 上，用 overlay 快速长出 AI 协同新业务；随后按字段逐步接管核心状态主权；最终只需把主权声明切为百分之百自有，即可直接拔掉外部 SaaS 插头独立运行，而上层 AI Agent 与业务代码一行都不用改。 |
| **Scene 6** | `6/6 · 防旁路三把物理锁与全场景实证矩阵` | • 左栏：展示防旁路三把物理锁（① AI 工具断网 + 写门禁触发器锁；② 人类在老系统偷建野码被下游 `SKU_NOT_GOVERNED` 黑户卡脖子锁 + AI 追缴；③ 老系统收缴人类 Create 权限锁）<br>• 右栏：展示已跑通的四大实证样板卡片（① 双真实 Odoo 19 + 真 Apache Camel 路由；② 钢厂主数据死结破局与三视角无损对账；③ 标准 O2C 全流程；④ 吸干开源 BottleCRM 后拔插头独立运行） | 最后，通过 AI 触发器门禁、下游黑户入库卡脖子锁与老系统建码权限收缴三把物理锁，彻底焊死人类和 AI 的旁路偷写。从双 Odoo 与 Apache Camel 并购集成、钢厂主数据破局，到 O2C 全流程与拔掉 SaaS 独立运行，OpenOntology 用极简纯净的内核，让企业拥有真正可推演、可治理、可演进的决策操作系统。 |

---

## 4. 视频合成与验收要求

1. 在 `/Users/guci/work/open-ontology/video_arch/` 下生成：
   - 分镜图片：`video_arch/frames/scene_01.png ~ scene_06.png`（`1920x1080`）
   - 配音文件：`video_arch/audio/scene_01.mp3 ~ scene_06.mp3`
   - 最终视频：`/Users/guci/work/open-ontology/video_arch/open_ontology_architecture.mp4`（`1920x1080`, H.264 + AAC, `yuv420p`）
2. 使用 `ffprobe` 验证分辨率（`1920x1080`）、帧率（`30fps`）、音视频流完整、总时长（`90s ~ 160s`）后汇报。
