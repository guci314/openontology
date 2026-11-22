# Palantir 底层原理与 OpenOntology 系统架构全景 {#palantir-oo-arch}

Palantir 用**确定性本体操作系统关住概率性大模型**。OpenOntology 以 10 模块微内核加 6 大外挂壳落地这套范式，并补齐 AI 反编译驱动与同态核销。

- 语义与动力学合一：把 ERP 碎片表投影为业务对象图，废除裸 CRUD，一切写操作经受控 Action 落账。[](source:palantir-doc)
- 微内核极简零污染：核心 Runtime 锁死在 10 个模块内，MCP、PG16、MDM 与组织行权全部外挂在壳层。[](source:arch-doc)
- 同态核销防脏写：反事实沙盘预演后无锁调用老 ERP，必须经独立只读回读核验一致才提交 ABox。[](source:settlement)
- AI 驱动反编译与盲测：外部国产代理零提示反编译异构 ERP 薄驱动，由波普尔裁判在随机隐藏世界证伪。[](source:design-cli)

@answer scope="Palantir Foundry/AIP · OpenOntology e25ad5a · M2/M1 全栈架构"
@meta locale=zh-CN style=editorial
@source palantir-doc title="Palantir 与 OpenOntology 原理对照" path=docs/PALANTIR_AND_OPEN_ONTOLOGY.md lines=1:77
@source arch-doc title="OpenOntology M2 平台宪法" path=docs/ARCHITECTURE.md lines=1:90
@source init-core title="Pure Runtime 10 模块导出面" path=open_ontology/__init__.py lines=1:80
@source runtime-core title="OntologyRuntime 微内核（1328 行）" path=open_ontology/runtime.py lines=1:75
@source settlement title="HomomorphicSettlementAspect 同态核销" path=open_ontology/settlement.py lines=1:66
@source drivers title="DeviceDriverRegistry 外设驱动总线" path=open_ontology/drivers.py lines=1:45
@source design-cli title="Design-Time 编译器与差分裁判 CLI" path=open_ontology/design.py lines=1:65
@source std-core title="5 原语 REA 常识细腰底座" path=examples/std_core_ontology.py lines=1:55
@source gates-test title="七道架构防污染自动门禁" path=tests/test_architecture_boundaries.py lines=1:68

## Palantir 三位一体原理：为什么不是 BI 或裸 RAG？ {#palantir-principles}

传统数仓只读不写，裸 MCP 代理直接调 CRUD 又极易幻觉脏写。Palantir 在底层 IT 与上层调用者间插入三层本体与双平面存储，实现安全闭环。[](source:palantir-doc)

@architecture id=palantir-triad-arch title="Palantir Foundry（OSv2）与 AIP 核心原理"
| erp | 异构 ERP 与数仓 | external |
| backing | 外部同步底表 | database |
| overlay | 本体决策叠加层 | database |
| semantic | 语义层对象图 | backend | source:palantir-doc
| kinetic | 动力学动作层 | security | source:palantir-doc
| dynamic | 动态推演与 OSDK | backend |
| human | 业务人员应用 | frontend |
| aip | AIP 概率大模型 | frontend |
| erp > backing | 管线同步 |
| backing > semantic | 投影合并 |
| overlay > semantic | 叠加不冲掉 | emphasis |
| semantic > kinetic | 读状态校验 |
| kinetic > overlay | 受控落账 | security |
| kinetic > erp | 副作用写回 | dashed |
| kinetic > dynamic | 暴露契约 |
| dynamic > human | 应用交互 |
| dynamic > aip | 强类型工具 | emphasis |

@steps id=palantir-steps
| p-sem | 语义层对象图 | 将几十张物理表统一为对象、属性与关系，消除调用方语义鸿沟。 | palantir-triad-arch | semantic |
| p-kin | 动力学动作层 | 废除裸接口，每次变更必过角色权限、字段标签与业务不变量。 | palantir-triad-arch | kinetic |
| p-ovl | 本体决策叠加层 | 外部管线重刷底表时保留本体自有裁决字段，冲突可精细裁决。 | palantir-triad-arch | overlay |
| p-aip | AIP 概率大模型 | 模型只做语义理解与方案生成，计算、试演与落盘交由确定代码。 | palantir-triad-arch | aip |

## OpenOntology 系统总架构：微内核、外挂壳与设计时隔离 {#openontology-arch}

OpenOntology 遵循类 Java 分层：10 模块微内核充当确定性 JVM，6 大生产壳按需外挂，重型 AI 反编译完全剥离至外部 Design-Time 沙盒。[](source:arch-doc)

@architecture id=openontology-full-arch title="OpenOntology 四层解耦架构（基线 e25ad5a）"
| fde | 外部 AI-FDE 代理 | external |
| judge | 差分裁判 CLI | security | source:design-cli
| m1 | M1 细腰与标准域 | backend | source:std-core
| bus | 外设驱动总线 | backend | source:drivers
| kernel | 10 模块微内核 | backend | source:runtime-core
| pg | SQLite 或 PG16 | database |
| shells | 6 大外挂生产壳 | security | source:init-core
| callers | 国产代理与企微 | frontend |
| fde > judge | 产驱动与映射 | dashed |
| judge > bus | 盲测放行 | security |
| m1 > kernel | 声明式编译 | emphasis |
| bus > kernel | 单写隔离 |
| kernel > pg | 6 表物理门禁 | security |
| kernel > shells | 纯同步扩展 | emphasis |
| shells > callers | MCP 与审批卡 |

@steps id=oo-arch-steps
| o-judge | 差分裁判 CLI | 在独立隔离沙盒执行 AST 反作弊与随机隐藏世界盲测，运行时对其一无所知。 | openontology-full-arch | judge |
| o-m1 | M1 细腰与标准域 | `std_core` 仅保留 5 个 REA 常识原语，同级专业域严禁互相导入。 | openontology-full-arch | m1 |
| o-kernel | 10 模块微内核 | 负责内存 `fork()` 沙盘、状态机、不变量、乐观锁、同态核销与 OSDK。 | openontology-full-arch | kernel |
| o-shells | 6 大外挂生产壳 | `mcp_server`、`autonomy`、`reconciliation`、`postgres_backend`、`mdm_plugin`、`org_authority` 零侵入外挂。 | openontology-full-arch | shells |

@facts id=oo-scale-facts
| Pure Runtime 体积 | `import open_ontology` 仅加载 10 模块共 8781 行，较拆包前缩减 71.2% |
| 微内核行数约束 | `open_ontology/runtime.py` 实测 1328 行，严格守在 1335 行预算内 |
| 物理表与审计态 | SQLite 与 PostgreSQL 16 均严格维持 6 表，账本仅存 `COMMITTED/REVERTED/REJECTED` |
| 广义 Tool 统一 | 软件接口、个人审批与跨部门协作统一对象化为 M1 `WorkCard`，内核零异步挂起态 |

## 一次受治理动作如何完成零脏写同态闭环？ {#action-lifecycle}

写操作绝不直接裸发老 ERP。请求依次经过组织鉴权、影子爬坡、内存沙盘预演、写后失联对账与独立只读核销，任意环节漂移立即零脏写拒单。[](source:settlement)

@sequence id=homomorphic-seq title="MCP 代理写操作三阶段同态闭环"
| actor agent | 国产岗位代理 | frontend |
| actor shell | MCP与行权爬坡壳 | security |
| actor rt | Pure Runtime核 | backend |
| actor saga | 写后对账器 | security |
| actor erp | 真实老 ERP | external |
| actor db | PG16 六表门禁 | database |
| phase | 准入鉴权与反事实预演 |
| agent | shell | tools/call submit_action |
| shell | rt | 校验行权爬坡并 fork() 预演 |
| rt | shell | 返回精确 diff 与版本号 | return |
| phase | 无锁外部执行与超时先回读 |
| rt | saga | Phase B 脱离写锁调用 actuate(move) | emphasis |
| saga | erp | 携带 SHA-256 幂等键写入 |
| erp | saga | 成功或抛出网络超时异常 | return |
| saga | erp | 超时必先独立只读 observe 回读 | security |
| erp | saga | 返回真实外设物理状态 | return |
| phase | 同态核销与物理门禁原子提交 |
| saga | rt | 返回独立观测快照 | return |
| rt | db | Phase C 开闸核验版本并写入 COMMITTED | emphasis |
| db | agent | 返回提交账本或 DEVICE_STATE_MISMATCH | return |

@steps id=seq-steps
| q-auth | MCP与行权爬坡壳 | 服务端钉死身份与厂区权限；处于 `SHADOW` 影子态则只记演练账本不触库。 | homomorphic-seq | shell |
| q-sim | Pure Runtime核 | 预演在 `:memory:` 分叉库运行，零锁生产 PG16，非法跃迁直接前置拦截。 | homomorphic-seq | rt |
| q-saga | 写后对账器 | 遇网络超时绝不盲重试，先读外设：已落盘直接核销，未落盘才同键重试。 | homomorphic-seq | saga |
| q-db | PG16 六表门禁 | 仅合法事务内开闸写表；绕过内核直接执行 SQL 会被触发器物理阻断。 | homomorphic-seq | db |

## 七道防污染铁笼如何锁死体系边界？ {#seven-gates}

架构约束不靠口头约定，全部固化在自动门禁测试中，任一提交越界即刻红灯拦截。[](source:gates-test)

| 门禁编号 | 守护边界 | 强制验证机制 | 工程效果 |
| :--- | :--- | :--- | :--- |
| Gate 1 | 技术不污染业务 | AST 扫描全部 M1 本体，禁 `runtime/io/os/http` | 纯业务模型可脱离框架独立复用 {good:零技术债} |
| Gate 2 | 业务不污染内核 | 递归扫描 `open_ontology/**`，禁反引 `examples` 与裸 IO | 微内核 100% 领域无关 {good:纯 M2} |
| Gate 3 | M1 词表与厂商禁令 | 动态收集全部 M1 标识与厂商词反扫内核源码 | 杜绝隐式硬编码与供应商耦合 {good:零泄漏} |
| Gate 4 | 跨领域盲测验证 | 用虚构星舰领域跑通 Runtime、规划器与坩埚 | 证明内核不依赖任何商贸假设 |
| Gate 5 | 纯同步六表铁笼 | 同时校验 `schema.sql` 与 `schema_pg.sql` 6 表及状态集 | 永久封杀异步挂起状态与双写脏账 {good:双库齐守} |
| Gate 6 | Runtime 零例外隔离 | 干净子进程校验 `sys.modules` 严格等于 10 模块 | 杜绝加载 Design-Time、外挂壳与 `psycopg` {good:零膨胀} |
| Gate 7 | 常识细腰零互耦 | 自动发现 `std_*_ontology`，禁同级专业域交叉导入 | `std_p2p` 与 `std_o2c` 仅经 5 原语互操作 {good:防网状爆炸} |

@callout level=note title="体系核心哲学：常识细腰与先验投影"
| `std_core` 仅沉淀 `BusinessParty`、`TradeResource`、`TradeCommitment`、`FulfillmentEvent`、`WorkCard` 五个常识原语，不建 SAP 式巨型上帝对象库。 |
| 本体是带先验 Schema 的投影，不复刻目标库。老系统日志在边界外自动忽略，同域新事实经独立回读自然摄入。 |

## Palantir Foundry/AIP 与 OpenOntology 客观对照 {#palantir-vs-oo}

两者共享同一套确定性世界模型思想，但目标生态与工程纵深各有侧重，必须实事求是划清边界。[](source:palantir-doc)

| 对比维度 | Palantir Foundry / AIP | OpenOntology（开源基线 `e25ad5a`） |
| :--- | :--- | :--- |
| 代码透明与可控性 | 闭源商业 SaaS 与私有部署，绑定强 {caution:黑盒} | 全开源极简微内核，7 道自动门禁防架构腐化 {good:全栈可控} |
| 异构老系统接入 | 依赖高薪驻场 FDE 手写 Connector 管线 | 外部 AI-FDE 反编译薄驱动加隐藏世界盲测证伪 {good:分钟级验真} |
| 写回核销与防绕过 | 依赖管线异步重索引与冲突裁决 | 内建无锁独立回读同态核销加写后失联对账器 {good:抓现行零脏写} |
| 人工审批与广义工具 | Workshop 表单加工作流引擎 | 统一 M1 `WorkCard` 加企微或飞书 `t1` 防幻读重算 {good:内核零挂起} |
| 本土生态与成本 | 面向欧美大客户，强绑定海外云与模型 | 实证华夏 ERP、PG16、企微飞书与国产 DeepSeek {good:本土可用} |
| 超大规模数仓与 BI | PB 级分布式计算管线、血缘与低代码应用成熟 {good:工业霸主} | 专注业务决策与跨系统治理，不自带 PB 级数仓 {caution:轻量定位} |

@callout level=warning title="客观边界：OpenOntology 当前尚未覆盖的深水区"
| OpenOntology 不取代 Spark/Flink 级海量离线数仓、实时流计算引擎或复杂报表 BI 平台。 |
| 跨地域多活高可用集群、细粒度联邦密钥托管与军工级合规认证仍需依托企业现有云底座与运维体系补齐。 |
