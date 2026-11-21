# 不改老 ERP 一行代码，如何治好钢厂主数据与二次开发？ {#steel-mill-explainer}

用**外挂符号世界模型 + 料仓锚点 + 影子爬坡**，可在零改造老 ERP 前提下切掉 70% 至 80% 胶水二开并治好散码账。

- 历史散码零侵入寄生：保留老 ERP 编码不动，只在外挂自有层挂接归并边，财务旧账零破坏。[](source:steel-mdm)
- 料仓锚点解开多对多断链：MES 只报料仓号即可无损反推精确规格，不逼采购与成本统一编码。[](source:steel-mdm)
- 影子模式零风险切入：先伴跑两周只记账不写库，再按企微或飞书组织树与金额分级放行。[](source:org-auth)
- 双异构真实 ERP 全栈实证：华夏 ERP 与 Odoo 19 联动国产 DeepSeek 已跑通 4 个钢贸实战场景。[](source:b2b-lab)

@answer scope="河北钢厂 · 采购/地磅/MES/成本关账 · OpenOntology c27f6bd"
@meta locale=zh-CN style=editorial
@source runtime-core title="Pure Runtime 微内核（1328 行）" path=open_ontology/runtime.py lines=1:60
@source steel-mdm title="河北钢厂两死结 M1 插件" path=examples/steel_mdm_plugin.py lines=740:968
@source autonomy-shell title="三级自治度影子爬坡壳" path=open_ontology/autonomy.py lines=364:425
@source saga-recon title="写后失联先回读对账器" path=open_ontology/reconciliation.py lines=173:240
@source org-auth title="组织树与动态分级行权引擎" path=open_ontology/org_authority.py lines=231:320
@source cn-bridge title="企微与飞书交互审批桥" path=examples/cn_org_workcard_bridge.py lines=1:80
@source b2b-lab title="双异构真实 ERP 钢贸实验室" path=examples/live_b2b_steel_trade_lab.py lines=1:60

## 外挂壳层如何不改老 ERP 就接管治理边界？ {#arch-overview}

OpenOntology 把确定性微内核收束在 10 个模块内，老系统只通过薄驱动接入，风控、主数据与权限全在外侧插件运行。[](source:runtime-core)

@architecture id=steel-shell-arch title="钢厂零侵入外挂架构（10 模块内核 + 6 大生产壳）"
| erp_a | 华夏 ERP 采购 | external |
| erp_b | Odoo 与 CRM | external |
| yard | 地磅与料仓 | external |
| saga | 写后对账器 | security | source:saga-recon
| kernel | 10 模块微内核 | backend | source:runtime-core
| pg16 | PG16 物理门禁 | database |
| mdm | 外挂 MDM 插件 | backend | source:steel-mdm
| org | 组织树行权 | security | source:org-auth
| mcp | MCP 运行壳 | backend | source:autonomy-shell
| agent | 国产岗位代理 | frontend | source:b2b-lab
| chat | 企微与飞书卡 | frontend | source:cn-bridge
| erp_a > saga | REST 读写 |
| erp_b > saga | RPC 读写 |
| yard > mdm | 过磅入仓 |
| saga > kernel | 同态核销 | emphasis |
| kernel > pg16 | 6 表门禁 | security |
| mdm > kernel | 灰区开卡 | dashed |
| org > mcp | 令牌与行权 | security |
| kernel > mcp | 沙盘与提交 | emphasis |
| mcp > agent | 裁剪工具 |
| mcp > chat | 分级审批 |

@steps id=arch-steps
| s-saga | 写后对账器 | 网络超时先独立回读老 ERP，已落盘则绝不重复下单。 | steel-shell-arch | saga |
| s-pg | PG16 物理门禁 | 数据库触发器锁死六张核心表，绕过内核直写会被物理拒绝。 | steel-shell-arch | pg16 |
| s-mdm | 外挂 MDM 插件 | 监听新散码，硬键自动刷映射，灰区自动发卡交人工确认。 | steel-shell-arch | mdm |
| s-org | 组织树行权 | 同步企微与飞书厂区树，一分厂采购员看不见二分厂单据。 | steel-shell-arch | org |

## 如何解开钢厂主数据的两大真死结？ {#mdm-knots}

传统 SAP 失败在于单码制硬揉三个维度；外挂插件把散码、规格、厂家与料仓解耦为独立对象，各自按习惯干活。[](source:steel-mdm)

@workflow id=mdm-two-knots-flow title="旁路散码前松后紧与料仓精确溯源"
| lane weigh | 过磅不堵车 |
| lane consume | 料仓精确报工 |
| lane close | 关账前硬卡 |
| bypass | 采购老库建散码 | external | weigh |
| truck | 货车过磅入仓 | backend | weigh |
| prov | 挂临时态发卡 | security | weigh |
| bin | 料仓报工扣减 | backend | consume |
| exact | 精确归因规格 | database | consume |
| gate | 月底关账校验 | security | close |
| settle | 三账守恒放行 | frontend | close |
| bypass > truck | 进厂卸料 |
| truck > prov | 放行不排队 | emphasis |
| truck > bin | 绑定料仓 |
| bin > exact | 物理溯源 | emphasis |
| prov > gate | 审结转正 | security |
| exact > gate | 汇总台账 |
| gate > settle | 零临时单 | emphasis |

@steps id=mdm-steps
| m-prov | 挂临时态发卡 | 旁路散码入库标记 `PROVISIONAL` 放行，同步发出 24 小时审批卡。 | mdm-two-knots-flow | prov |
| m-exact | 精确归因规格 | MES 只报料仓号即按 `BIN_EXACT` 反推规格；无料仓工位显式标 `BRAND_FIFO`。 | mdm-two-knots-flow | exact |
| m-gate | 月底关账校验 | 只要仍有未转正散码，关账与付款必抛 `PROVISIONAL_SKU_BLOCKS_SETTLEMENT`。 | mdm-two-knots-flow | gate |

## 老板第一天不敢让 AI 写库，怎么安全爬坡？ {#shadow-org}

用三级自治度开关配合企微或飞书分级授权，先零写入伴跑两周验证准确率，再按厂区与金额逐档放权。[](source:autonomy-shell)

@workflow id=shadow-ramp-flow title="影子伴跑与三级金额动态行权"
| lane ramp | 自治度爬坡 |
| lane tier | 金额分级审批 |
| lane verify | 防幻读落盘 |
| req | 代理发起确认 | frontend | ramp |
| shadow | 两周影子伴跑 | backend | ramp |
| auto | 不超10万直过 | backend | tier |
| mgr | 10至50万厂长批 | security | tier |
| vp | 超50万总监批 | security | tier |
| t1 | 现场重算防幻读 | security | verify |
| commit | 核销写回ERP | database | verify |
| req > shadow | 零写试跑 |
| shadow > auto | 开放实盘 |
| shadow > mgr | 超10万 | security |
| shadow > vp | 超50万 | security |
| auto > commit | 自动提交 | emphasis |
| mgr > t1 | 签名回调 |
| vp > t1 | 签名回调 |
| t1 > commit | 校验通过 | emphasis |

@facts id=org-guard-facts
| 跨厂区越权 | 二分厂部长抢批一分厂单据直接报 `AUTHORITY_ORG_SCOPE_DENIED` |
| 越级审批 | 厂区部长越级批 80 万元单据直接报 `AUTHORITY_TIER_INSUFFICIENT` |
| 职责分离 | 采购发起人自批自己单据直接报 `SOD_SELF_APPROVAL_FORBIDDEN` |
| 幻读防线 | 开卡后审批前原单若被改态，点击同意时 `t1` 重算直接拒写 |

## 实证数据与传统二开方案对比 {#comparison}

以下数据全部来自本机真实容器与开源国产模型实测，并划清绝不硬揽的工控边界。[](source:b2b-lab)

| 对比维度 | 传统 SAP / ERP 二开 | OpenOntology 外挂方案 |
| :--- | :--- | :--- |
| 历史散码治理 | 停机洗码，财务旧账高危 {bad:推不动} | `__owned__` 寄生归并，老库零改动 {good:零破坏} |
| 规格 vs 厂家口径 | 强逼两部门统一编码 {bad:内耗大} | 料仓 `StorageBin` 与批次桶三账守恒 {good:各按习惯} |
| 跨软件风控与审批 | 分别找 ERP 与地磅厂商二开 {bad:按次收费} | 声明式覆层加 `WorkCard`，老软件零修改 {good:省70%至80%} |
| 接新 ERP 周期 | 人工对接联调数周 {caution:易漂移} | DeepSeek 反编译华夏 ERP 驱动 543 秒一次过 {good:实测0.12美元} |
| 代理核心业务安全 | 裸 MCP 靠提示词赌概率 {bad:不敢上线} | 沙盘预演加影子模式加 OOB 核销零脏写 {good:确定可控} |

@facts id=live-proof-facts
| 双企全栈实测 | 华夏 ERP 加 Odoo 19 加 BottleCRM 加 PG16，4 个场景 235.85 秒全通 |
| 国产代理调用 | 双端真实 `hermes` 调 `deepseek-v4.1-flash` 共 18 次 MCP 工具，0 次报错 |
| 华夏 ERP 盲考 | 零提示反编译 347 行薄驱动，公开与随机隐藏双租户均 100% 动作覆盖 |
| 自动化回归 | 双 Python 3.11 与 3.13 架构七门禁全绿，离线回归 657 项通过 |

@callout level=warning title="明确边界：这 20% 至 30% 不该用本体论硬揽"
| 地磅串口 Modbus 抓拍、高炉 PLC 毫秒级工控、行车 HMI 交互界面必须留在工控层。 |
| 复杂冶炼卷积成本核算与金税盘底层税控开票留在原 ERP，OpenOntology 只接管跨系统状态决策与风控边界。 |

## 河北钢厂两周零风险试点清单 {#pilot-checklist}

如果准备先找一条采购加过磅业务线验证效果，按下面顺序推进即可，不触碰生产库写入权限。

- [ ] 拉出近 30 天采购合同、地磅磅单与化验单只读快照，接入 `MDMSidecarPlugin` 跑出散码清单。
- [ ] 给主要原料料堆登记 `SteelStorageBin` 编号，让 MES 报工顺带回传料仓号。
- [ ] 同步企业微信或飞书厂区组织树，配置不超过 10 万、10 至 50 万、超过 50 万三级授权表。
- [ ] 开启 `SHADOW` 影子模式免费伴跑两周，对比系统抓出的短磅、客诉与散码异常和人工对账结果。
- [ ] 准确率达标后先开放 `COPILOT_ESCALATE` 人工点卡确认，再逐步放开小额常规单自动提交。
