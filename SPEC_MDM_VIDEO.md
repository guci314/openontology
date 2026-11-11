# OpenOntology 规格说明书（TK-ONTO-008 · 钢厂主数据两大死结破局高清演示视频自动生成）

## 1. 目标与严格并发隔离纪律

基于已验证通过的 `examples/steel_mdm_demo.py`（`TK-ONTO-005`），在独立目录 `/Users/guci/work/open-ontology/video_mdm/` 下编写自动化视频生成管线，生成一条 **1920×1080 高清、左右分屏、带自然中文 TTS 配音与大号底部字幕** 的钢厂主数据破局演示视频：
- **最终产物路径**：`/Users/guci/work/open-ontology/video_mdm/steel_mdm_demo.mp4`

> **⚠️ 并发隔离铁律（当前 `程序员` 正在并行制作 `video/` 下的 O2C 视频）**：
> 1. **只准在 `/Users/guci/work/open-ontology/video_mdm/` 目录下创建和写入文件**（严禁触碰 `/Users/guci/work/open-ontology/video/` 目录、严禁修改 `open_ontology/` 内核或 `examples/` 下已有文件）！
> 2. **测试隔离**：只允许运行 `pytest -v tests/test_steel_mdm.py`（严禁运行全量 `pytest` 或任何包含 `odoo` 的测试，以免重置 Odoo 数据库干扰隔壁会话）！

---

## 2. 本机工具链路径

- **Playwright**：Python `playwright.sync_api`（使用 Chromium `1920x1080` 视口渲染高颜值分镜 HTML 为高清 PNG）
- **TTS 配音**：优先使用 `/Users/guci/.local/bin/edge-tts --voice zh-CN-YunxiNeural`（若网络异常则自动回退 `/usr/bin/say -v Tingting`）
- **FFmpeg**：`/opt/homebrew/bin/ffmpeg` 与 `/opt/homebrew/bin/ffprobe`

---

## 3. 六幕分镜剧本与配音台词（`video_mdm/build_steel_mdm_video.py`）

脚本真实调用 `examples.steel_mdm_demo` 中的 `OntologyRuntime` 获取每一步的真实运行状态、审计记录、`ObjectDiff` 与三视角对账数据，并将其渲染为 **1920×1080 双栏高清可视化画面（左屏：OpenOntology 动力学沙盘、硬护栏告警与审计流 | 右屏：高维对象关系拓扑图 + 成本/采购/财务三视角实时数据表）**：

| 幕次 | 顶部标题 | 左屏（OpenOntology 动作与治理沙盘） | 右屏（高维图谱拓扑与三视角实测数据表） | 中文配音与底部字幕 |
|---|---|---|---|---|
| **Scene 1** | `1/6 · 钢厂主数据两大绝望死结` | 展示传统一维编码在 ERP（按规格）进老 MES（按厂家）时的「有损降维投影」痛点 | 展示老 ERP 存量 4 个混乱焦炭散码（`ERP-SKU-9001~9004`）及山西焦化（`BRAND-1`）同时供一级焦 `SPEC-A` 和二级焦 `SPEC-B` 的多对多困局 | 钢铁企业主数据治理有两个公认死结：一是采购为签合同开票随意建散码，挂着历史财务账不敢合并；二是采购按规格管、生产按厂家管，山西焦化既供一级焦又供二级焦，MES 消耗后回传 ERP 时多对多断链，成本全部算歪。 |
| **Scene 2** | `2/6 · 治存量：overlay 零侵入寄生归并` | 执行 `map_legacy_sku_to_spec(affects_source=False)`，写入 `ontology_overlay`；触发老 ERP 全量 `load_snapshot` 重刷验证 | 展示 4 个老 ERP 散码原封不动（财务发票零风险），虚线归并边指向 `SPEC-A / SPEC-B`，重刷底表后 `overlay preserved = True` | 面对存量散码，OpenOntology 不改老 ERP 底表一颗螺丝钉，用 overlay 补丁层将四个散码寄生归并到标准物理规格。哪怕老 ERP 每天全量重刷底表，归并关系依然百分之百完好存活。 |
| **Scene 3** | `3/6 · 治增量：Action 硬护栏与新物种审批` | 展示 3 条真实运行记录：① 无归属野码被 `UNMAPPED_PHYSICAL_SPEC` 红牌拦截；② 灰分超标挂一级焦被 `SPEC_QUALITY_MISMATCH` 拦截；③ 申请新规格 `SPEC-C` 挂起 `PENDING_APPROVAL` 经管理员批准生效 | 展示 `ontology_action_runs` 审计账本（2 条 `REJECTED` 红牌记录 + 1 条 `mdm-steward-01` 审批通过创建的 `SPEC-C 三级冶金焦`） | 面对增量建码，采购新建开票名目必须通过 register_contract_sku 门禁。不绑规格的野码和灰分超标的错配被当场红牌拦截并记入审计；申请全厂首次采购的新物理品种则自动挂起，由主数据管理员签批后生效。 |
| **Scene 4** | `4/6 · T1 入库截留：构建高维批次桶 MaterialLot` | 执行 `receive_material_lot`，在货物从 ERP 进 MES 的入库口截留完整多维信息（未归并黑户散码会被 `SKU_NOT_GOVERNED` 卡死） | 可视化展示 `LOT-01 (100吨, SPEC-A, BRAND-1 @¥2000)`、`LOT-02 (80吨, SPEC-B, BRAND-1 @¥1600)`、`LOT-03 (120吨, SPEC-A, BRAND-2 @¥1950)` 同时拉住规格、厂家、发票三条正交边 | 为什么老 MES 把规格弄丢了还能找回来？秘密在入库口。当原料入库时，Ontology 在中间截留完整信息，构建高维批次桶 MaterialLot，同时锁死物理规格、生产厂家和原始发票散码三条正交关系。 |
| **Scene 5** | `5/6 · T2 报工还原：三视角无损对账恒等` | MES 工人按老习惯只报 `consume_by_brand_fifo(brand_id="BRAND-1", tons=150)`，沙箱按 FIFO 自动扣减 `LOT-01 (100吨)` + `LOT-02 (50吨)` | 高亮并排展示三张对账表：<br>① 成本科（山西焦化 150吨 = ¥280,000）<br>② 采购/ERP报产（无损拆出 `SPEC-A 100吨 ¥200,000 + SPEC-B 50吨 ¥80,000`）<br>③ 财务科（`SKU-9001 100吨 + SKU-9003 50吨`）<br>`balanced = True` | 高炉车间照常只按厂家报工：今日消耗山西焦化一百五十吨。Ontology 按先进先出自动扣减高维批次桶，顺着三条关系边一键生成成本科、采购报产、财务发票三张对账单，一百吨一级焦加五十吨二级焦无损还原，三方总吨数与二十八万元总成本分毫不差！ |
| **Scene 6** | `6/6 · 防旁路三把物理锁：彻底淘汰传统 MDM` | 演示 `revert_action` 时光机一键撤回投料恢复库存，并展示 `ontology_write_gate` 物理触发器 | 展示「三把物理防绕过锁」总结图：① AI 断网与触发器锁；② 老系统偷建野码在下游被 `SKU_NOT_GOVERNED` 黑户卡脖子锁；③ 老系统收缴人类 Create 权限锁 | 配合 AI 触发器门禁、下游黑户入库卡位锁与老系统建码权限收缴，任何人和 AI 都无法绕过 Ontology 制造脏数据。不买笨重的传统 MDM 软件，不逼业务部门打架，三天算清钢厂每一分真实成本。 |

---

## 4. 视频合成与验收要求

1. 在 `/Users/guci/work/open-ontology/video_mdm/` 下生成：
   - 分镜图片：`video_mdm/frames/scene_01.png ~ scene_06.png`（`1920x1080`）
   - 配音文件：`video_mdm/audio/scene_01.mp3 ~ scene_06.mp3`
   - 最终视频：`/Users/guci/work/open-ontology/video_mdm/steel_mdm_demo.mp4`（`1920x1080`, H.264 + AAC, `yuv420p`）
2. 运行 `pytest -v tests/test_steel_mdm.py` 与 `ffprobe` 校验视频分辨率、时长（60s~140s）、音视频流完整后汇报。
