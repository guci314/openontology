# OpenOntology 规格说明书（TK-ONTO-007 · O2C 左右分屏真实 Odoo 19 联动高清演示视频自动生成）

## 1. 目标

基于已跑通的 `TK-ONTO-006`（标准 O2C 纯业务本体 + 真实 Odoo 19 适配器），在 `/Users/guci/work/open-ontology/video/` 下编写一套自动化视频生成管线，生成一条 **1920×1080 高清、左右分屏（左屏：OpenOntology AI 决策指挥台 | 右屏：真实 Odoo 19 网页实时截图）、带自然中文 TTS 配音与大号底部字幕** 的演示视频：
- **最终产物路径**：`/Users/guci/work/open-ontology/video/o2c_ontology_demo.mp4`

---

## 2. 本机工具链路径

- **真实 Odoo 19 Web 地址**：`http://localhost:8069/web?db=erp_north`（账号 `admin`，密码 `admin`）
- **Playwright**：Python `playwright.sync_api`（已安装 Chromium）
- **TTS 配音**：优先使用 `/Users/guci/.local/bin/edge-tts --voice zh-CN-YunxiNeural`（若网络异常则自动回退 `/usr/bin/say -v Tingting` 转为 wav/mp3）
- **FFmpeg**：`/opt/homebrew/bin/ffmpeg` 与 `/opt/homebrew/bin/ffprobe`

---

## 3. 核心真实性铁律（严禁画假 UI 冒充 Odoo）

1. **右屏必须是真实的 Odoo 19 网页画面**：
   - 脚本必须先真实执行 `examples/odoo_o2c_adapter.py` 中的各阶段动作，并用 **Playwright 真实登录 `http://localhost:8069/web?db=erp_north`**，打开对应 `sale.order`、`stock.picking`、`account.move` 的真实 Odoo 19 表单页面（URL 形如 `http://localhost:8069/odoo/sales/{id}` 或 `/web#id={id}&model=sale.order&view_type=form`），等待页面渲染完毕后截取真实 Odoo 19 界面 PNG！
2. **左屏是高颜值深色主题的 OpenOntology AI 作战指挥台**：
   - 展示当前调用的纯业务 Action / Function 名称、入参 JSON、`EditBatch` 产出的 `ObjectDiff`、治理状态徽章（`COMMITTED` 绿标 / `REJECTED` 红牌警报 / `PENDING_APPROVAL` 黄标审批单）以及 `ontology_action_runs` 实时审计账本。
3. **合成 1920×1080 左右分屏帧 + 底部大号字幕条**：
   - 将左屏指挥台、右屏真实 Odoo 19 网页截图（带浏览器顶栏 `http://localhost:8069 · Odoo 19 (erp_north)` 标识与状态高亮框）、顶部场景标题栏、底部字幕条通过 HTML/CSS 模板在 Playwright `1920x1080` 视口下渲染为高清分镜图 `scene_01.png ~ scene_06.png`。

---

## 4. 六幕分镜剧本与配音台词（`video/build_o2c_video.py`）

| 幕次 | 顶部标题 | 左屏（OpenOntology 指挥台） | 右屏（真实 Odoo 19 网页实拍截图） | 中文配音与底部字幕 |
|---|---|---|---|---|
| **Scene 1** | `1/6 · 痛点与 O2C 经营体检` | 调用 `diagnose_o2c_bottlenecks`：扫出正常草稿单 `O2C-SO-001 (¥20,000)` 与超信用风险单 `O2C-SO-002 (¥120,000 > 限额 ¥100,000)` | 真实 Odoo 19 销售订单列表/草稿报价单页面截图 | 为什么企业不敢让大模型直接操作 ERP？因为大模型一旦产生幻觉改错单据、提前开出假发票，财务底账就会崩盘。来看看我们如何用 Palantir 同款 Ontology，给真实 Odoo 19 装上带保险栓的 AI 自动驾驶仪。 |
| **Scene 2** | `2/6 · 物理护栏：拦截未发货提前开票` | `confirm_sales_order` 通过后，AI 试图在发货前调 `issue_and_post_invoice`，弹出红色警报 `REJECTED: CANNOT_INVOICE_UNSHIPPED_ORDER` 并记入审计表 | 真实 Odoo 19 中已确认的销售订单 `S00...` 页面（显示待交货 Delivery 按钮，发票数为 0） | 先让 AI 确认两万元的正常订单，右侧 Odoo 实时生成销售单与待发货单。如果这时 AI 试图在仓库发货前违规提前开票，Ontology 的前置硬护栏会当场红牌拦截，并将未遂企图记入审计账本，确保 Odoo 里绝不产生一张假发票。 |
| **Scene 3** | `3/6 · 仓库出库：纯业务动词驱动拣货过账` | AI 调用 `ship_order_delivery(order_id="O2C-SO-001")` $\to$ `COMMITTED`，适配器自动驱动 `stock.picking.button_validate` | 真实 Odoo 19 交货单 `WH/OUT/...` 详情页截图（右上角状态条高亮为 **已完成 / Done**） | 在合法路径下，AI 根本不需要懂 Odoo 底层复杂的瞬态向导。AI 只需调用 ship_order_delivery 一个纯业务动词，底层适配器自动完成拣货数量填充与出库校验，右侧真实 Odoo 交货单瞬间变为已完成。 |
| **Scene 4** | `4/6 · 开票与收款核销：应收余额精准归零` | AI 连续调用 `issue_and_post_invoice` 与 `record_customer_payment` $\to$ `COMMITTED`，客户应收余额 `ar_balance = ¥0.00` | 真实 Odoo 19 客户发票 `INV/2026/...` 详情页截图（带绿色 **已付款 / PAID** 横幅且到期应付余额为 `0.00`） | 货物出库后，AI 连续调用开票与收款核销两个纯业务动作。底层适配器自动在 Odoo 中创建应收发票、正式过账，并驱动付款核销向导。右侧真实 Odoo 发票直接盖上 Paid 已付款绿章，待核销余额精准归零。 |
| **Scene 5** | `5/6 · 人机共治：12 万超信用大单挂起与总监审批` | AI 提交 `O2C-SO-002 (¥120,000)` 触发 `PENDING_APPROVAL` 黄色审批卡片；随后 `finance-director-01` 调用 `approve_action` 放行 | 真实 Odoo 19 中 `O2C-SO-002` 审批通过后变为正式销售订单 `Sales Order` 的页面截图 | 面对那张十二万元的超信用额度大单，AI 申请确认时被治理规则自动挂起为待审批，此时 Odoo 单据纹丝不动。只有当财务总监在指挥台核对沙箱预演并点击批准后，右侧真实 Odoo 才正式放行确认。 |
| **Scene 6** | `6/6 · 架构总结：薄纯业务本体 + 厚 ERP 适配器` | 展示 AlphaGo 神经符号分工（LLM 策略 + Ontology 世界模型 + ERP 传感器/执行器）与全仓测试全绿总结 | 展示 `overlay` 治理属性（`risk_tier=WATCHLIST`, `account_rep`）在 Odoo 全量重刷后 100% 存活的实证面板 | 上层是百分之百纯业务的 Ontology 世界模型，下层是可插拔的 ERP 适配器。不改旧系统一行代码，让 AI Agent 在铁律护栏下安全替你运行企业。 |

---

## 5. 视频合成与验收要求

1. 每幕单独生成配音音频 `scene_01.mp3 ~ scene_06.mp3`，用 `ffprobe` 读取真实音频时长 $d_i$，每个分镜片段时长设为 $d_i + 0.6\text{s}$，最后用 `ffmpeg` 合成为 `1920x1080`、`30fps`、`yuv420p`、含 AAC 音轨的 `/Users/guci/work/open-ontology/video/o2c_ontology_demo.mp4`。
2. 编写测试或校验脚本验证：
   - `/Users/guci/work/open-ontology/video/o2c_ontology_demo.mp4` 存在且文件大小 $> 1\text{MB}$；
   - `ffprobe` 验证分辨率为 `1920x1080`，同时包含 `video (h264)` 与 `audio (aac)` 流，总时长在 `60s ~ 140s` 之间；
   - 6 张真实 Odoo 原始截图 `video/assets/odoo_raw_01.png ~ odoo_raw_05.png` 與 6 张合成帧 `video/frames/scene_01.png ~ scene_06.png` 均落盘可查。
