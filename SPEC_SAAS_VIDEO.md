# OpenOntology 规格说明书（TK-ONTO-016 · OpenOntology 驱动与无感拔除 SaaS 1080p 演示视频）

## 1. 目标

制作第 5 支 **1920×1080 1080p 高清带中文专业配音的架构与实证演示视频**（`/Users/guci/work/open-ontology/video_saas/saas_unplug_demo.mp4`），完整讲透并展示 **「OpenOntology 如何像寄居蟹一样先寄生驱动多套异构 SaaS（Shopify/Stripe/Zendesk 与真实 BottleCRM + 真实 Odoo 19），在 `ontology_overlay` 中沉淀企业自有治理记忆，最终通过 `unplug_all_saas()` 一键物理拔掉外部 SaaS 插头、100% 契约冻结独立运行」** 的全过程。

---

## 2. 隔离与目录铁律（严禁破坏其他目录）

1. **只准在 `/Users/guci/work/open-ontology/video_saas/` 目录下创建和写入文件**：
   - `/Users/guci/work/open-ontology/video_saas/build_saas_video.py`
   - `/Users/guci/work/open-ontology/video_saas/frames/scene_01.png ~ scene_06.png`
   - `/Users/guci/work/open-ontology/video_saas/audio/scene_01.mp3 ~ scene_06.mp3`
   - `/Users/guci/work/open-ontology/video_saas/segments/seg_01.mp4 ~ seg_06.mp4`
   - `/Users/guci/work/open-ontology/video_saas/saas_unplug_demo.mp4`
2. **严禁修改 `open_ontology/`、`examples/`、`tests/` 或其他 `video*/` 目录下的任何文件**（因为 `cb-coder-01` 正在修改 `open_ontology/`）。
3. 数据采集必须通过只读调用 `examples.saas_unplug_demo` 与 `examples.real_crm_unplug_demo`（若本机 `vendor_crm/bottlecrm.sqlite3` 与 Odoo 19 就绪则直接实跑 `real_crm_unplug_demo.main()`，同时采集 `saas_unplug_demo.main()` 的 `contract_identical = True` 对拍数据）获取真实运行数据，严禁硬编码假数字。

---

## 3. 六幕分镜剧本与视觉设计（1920×1080 深色科技风 HTML + 内联 SVG + Playwright 截图）

风格要求参考 `/Users/guci/work/open-ontology/video_arch/build_arch_video.py` 与 `/Users/guci/work/open-ontology/video_mcts/build_mcts_video.py`：
- 画布尺寸严格 `1920×1080`，深色科技背景（`#070B14` ~ `#0F172A`），顶部统一导航条（显示幕号、章节标题、主权演进进度条 `Phase A 寄生态 20% → Phase B 脱壳态 100%`），底部统一字幕条（显示与当前幕 TTS 配音完全一致的精炼字幕）。

### 第 1 幕：企业被 SaaS 绑架的三大痛点 vs「寄居蟹式演进」破局
- **视觉内容**：
  - 左侧对比卡「传统企业的 SaaS 困局」：① 数据碎片化割裂在 CRM（BottleCRM / Zendesk）、ERP（Odoo 19 / Shopify）、支付（Stripe）多个烟囱；② 每次全量同步重刷都会抹掉本地治理标注；③ 想要替换掉昂贵 SaaS 就得推倒重写全部业务与 AI Agent 代码。
  - 右侧内联 SVG 架构图「OpenOntology 寄居蟹双相演进」：
    - **Phase A（寄生态 `standalone=False`）**：外部 SaaS 退化为底层快照源，企业独有的风控分 `fraud_score`、VIP 等级 `vip_tier`、AI 分诊方案 `ai_resolution_plan` 声明为 `__owned__` 写入 `ontology_overlay`；
    - **Phase B（脱壳态 `standalone=True` / `unplug_all_saas()`）**：一键将全部对象与关系晋升为 `__owned__ = True`，物理切断外部连接，上层 Action 与 AI Agent 零感知平滑接管！

### 第 2 幕：Phase A · 吸干真实 BottleCRM (`django-crm 1.44.0`) 与真实 Odoo 19 数据
- **视觉内容**：
  - 展示从真实开源 `BottleCRM (bottlecrm.sqlite3)` 的 `accounts_account` / `opportunity_opportunity` / `cases_case` 表与真实 `Odoo 19 (erp_north)` 的 `sale.order` 抽取快照装入 `OpenOntology` 的实时流向图；
  - 展示实跑指标卡：吸收的 `CustomerAccount`、`SalesOpportunity`、`SupportCase`、`SalesOrder` 真实数量，以及 `account_opportunities`、`account_cases`、`account_orders` 语义关系图。

### 第 3 幕：Phase A · AI 分诊写入 `ontology_overlay` 补丁层 + 源系统全量重刷 100% 存活
- **视觉内容**：
  - 展示 `AI_AGENT` 执行 `ai_triage_crm_case` 动作，为工单计算并写入 `fraud_score`（85 / 35 / 15）、`vip_tier`（`STANDARD` / `SILVER` / `GOLD`）与 `ai_resolution_plan`；
  - 左右对比矩阵展示 **「BottleCRM + Odoo 19 第二次全量重刷 (`absorb_from_real_saas`) 前后对比」**：源系统字段刷新，但 `ontology_overlay` 中的 3 条 AI 治理补丁 **`overlay_before == overlay_after_resync (True)` 100% 完好存活**！

### 第 4 幕：Phase B · 执行 `unplug_all_saas()` 一键物理拔除 SaaS 插头与主权晋升
- **视觉内容**：
  - 中央高光展示 `unplug_all_saas(runtime, bottlecrm, erp)` 执行的四大原子动作：
    1. 物理切断连接：`bottlecrm.disconnect()`、`erp.disconnect()`；
    2. 5 大对象类型（`CustomerAccount`, `SalesOpportunity`, `SupportCase`, `SalesOrder`, `RefundSettlement`）全部晋升 `__owned__ = True`；
    3. 4 条关系边全部晋升 `owned = True`，动作 `affects_source` 归零；
    4. 铁证拦截面板：再次调用 `bottlecrm.fetch_accounts()` 立即抛出 `SaasDisconnectedError`（4 项外部调用全拦截），外部妄图再调 `load_snapshot` 覆盖被 `GovernanceViolationError` 拦截（5 个类型全部拒之门外），且原有对象与 `overlay` 零丢失！

### 第 5 幕：Phase B · 零外部 SaaS 下独立跑完核心业务闭环（赢单转订单 → 小额自动退 → 大额 `PENDING_APPROVAL` → `revert_action` 时光机撤回）
- **视觉内容**：
  - 四步横向流水线卡片（展示拔掉 SaaS 后完全靠 `OpenOntology` 自身 SQLite 内核独立跑通的全生命周期真实流水）：
    1. **商机赢单转订单**（`OFFLINE-WIN`：`win_opportunity_and_create_order` 生成 `SO-OFFLINE-001`，状态 `COMMITTED`）；
    2. **原生建工单 + $80 小额 AI 自动退款**（`OFFLINE-REFUND-SMALL`：低于 $100 阈值，AI 直接 `COMMITTED`）；
    3. **大额退款触发人类审批门**（`OFFLINE-REFUND-BIG`：超阈值自动挂起为 `PENDING_APPROVAL`，经 `OPS_MANAGER` 调用 `approve_action` 后放行）；
    4. **欺诈高分红线拦截 (`FRAUD_SCORE_TOO_HIGH -> REJECTED`) + 时光机一键撤回 (`revert_action -> REVERTED`)**。

### 第 6 幕：契约冻结定理（`contract_identical = True`）与总结
- **视觉内容**：
  - 展示 `examples/saas_unplug_demo.py` 中 `run_eval_suite` 在 **寄生态 (`standalone=False`)** 与 **脱壳态 (`standalone=True`)** 下跑同一套 `AIP_EVALS` 的对拍表：
    - Action 名称、参数 Schema、错误码（`RETURN_WINDOW_EXPIRED`、`FRAUD_SCORE_TOO_HIGH`）、审批状态（`PENDING_APPROVAL`）、撤回能力 100% 逐字节一致，**`contract_identical = True`**；
  - 总结金句：**「先用 Ontology 统一语义并驱动现有 SaaS，再按属性把业务逻辑沉淀进 Action 与 Overlay，最后拔掉插头——上层 AI Agent 一行代码不用改！」**

---

## 4. 配音与 macOS QuickTime Player 零黑屏编码铁律（必遵！）

1. **配音合成**：
   - 优先使用 `/Users/guci/.local/bin/edge-tts`（音色 `zh-CN-YunxiNeural`，语速 `+10%` 或 `+12%`，带重试机制），若网络异常可回退 `/usr/bin/say -v Tingting` 转 MP3；
   - 支持 `--reuse` 参数复用已有 `frames/` 与 `audio/`。
2. **macOS QuickTime 零黑屏 FFmpeg 合成参数（绝对禁止使用 `fade=t=in:st=0` 与 `concat -c copy`！）**：
   - 在 `build_segments` 生成单幕分片时：**严禁添加 `fade=t=in:st=0` 或 `fade=t=out`**，必须加上 `-bf 0 -video_track_timescale 30000`；
   - 在 `concat_segments` 拼接总片时：**严禁使用 `-c copy`**，必须使用全量重编码并重置时间戳：
     ```bash
     -vf setpts=PTS-STARTPTS -af asetpts=PTS-STARTPTS \
     -c:v libx264 -preset veryfast -crf 18 -bf 0 -pix_fmt yuv420p -tag:v avc1 \
     -c:a aac -b:a 192k -movflags +faststart
     ```
3. **脚本内置 `ffprobe` 终验断言**：
   - 分辨率 `1920x1080`、编码 `h264` (`avc1`) + `aac`、总时长 `80s ~ 165s`、文件大小 `> 1.5 MB`；
   - **`has_b_frames == 0`**；
   - **`start_pts == 0`** 且 **`start_time == 0.000000`**；
   - **第 0 帧像素亮度均值 `frame0_mean > 20.0`**（绝不能是黑帧！）。
