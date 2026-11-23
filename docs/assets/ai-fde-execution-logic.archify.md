# OpenOntology AI-FDE 的完整执行逻辑 {#ai-fde-execution-logic}

OpenOntology 的 **AI-FDE** 采用“双引擎外部编程 Agent + 独立编译证伪裁判”物理隔离闭环。它按单域把陌生 ERP 编译为声明式映射与受限薄驱动，经隐藏世界 B 与带外篡改终审后，落仓为持久适配包并组合挂载到 `DeviceDriverRegistry`。

- 选手与裁判物理隔离：Agent 仅能写入 `out/` 目录，只读 ABI、需求与裁判脚本全程受 SHA-256 保护。[](source:wo24c-orch)
- 静态 AST 牢笼防作弊：薄驱动代码不超过 350 行，封禁高危导入、动态执行、写盘和样本硬编码。[](source:ast-cage)
- 双引擎自动兜底与双重证伪：每域先跑 DeepSeek V4.1 Flash，未过任一门禁即自动切 `agy` Gemini，全部须过公开 A 与隐藏 B 盲测。[](source:wo24c-orch)
- 双 ERP 6 域实测全通：Odoo 19（`o2c/p2p/r2r`）与华夏 ERP（`p2p/o2c/h2r`）6/6 盲过，DeepSeek 首轮过 2 域，Gemini 兜底过 4 域，缺失域自动 `standalone` 补齐。[](source:hybrid-mount)

@answer scope="OpenOntology 979d47f · AI-FDE 设计态闭环 · 双 ERP 6 域实测"
@meta locale=zh-CN style=editorial
@source fde-skill title="AI-FDE 技能契约与薄驱动规范" path=skills/openontology-fde/SKILL.md lines=10:27
@source fde-driver-spec title="薄驱动入口与状态漂移契约" path=skills/openontology-fde/SKILL.md lines=105:133
@source ast-cage title="audit_thin_driver_ast 静态牢笼" path=open_ontology/design.py lines=773:808
@source verify-loop title="run_design_verify 坩埚与差分证伪" path=open_ontology/design.py lines=1796:1875
@source hidden-verify title="verify_thin_driver_on_hidden_world 隐藏世界盲测" path=open_ontology/design.py lines=1890:1945
@source wo24c-orch title="WO-24C 双 ERP 双引擎盲考编排器" path=examples/wo24c_dual_erp_fde_orchestrator.py lines=1:34
@source hybrid-mount title="双 ERP 混合世界模型挂载器" path=examples/adapters/erp_world_mount.py lines=1:10
@source driver-bus title="MountedDevice 热挂载总线契约" path=open_ontology/drivers.py lines=82:120
@source crucible-role title="SymbolicCrucible Gate 5 闭集角色豁免" path=open_ontology/induction/crucible.py lines=162:198

## 为什么必须把 AI-FDE 与裁判物理隔离？ {#isolation-boundary}

10 模块 Pure Runtime 内核零内置大模型循环，仅设计态 `induction/` 保留早期程序化组件。主线 AI-FDE 把模型降级为受限外部施工员，确定性裁判留在外层不可触碰区。[](source:fde-skill)

@architecture id=fde-isolation-arch title="AI-FDE 三区物理隔离与持久挂载架构"
| ro_zone | 只读契约区 0444 | security | source:fde-skill |
| player | 双引擎外部 Agent | frontend | source:wo24c-orch |
| out_zone | 唯一可写区 out/ | backend | source:fde-driver-spec |
| local_cli | 本地裁判脚本 0555 | security | source:verify-loop |
| world_a | 公开世界 A 与重置钩子 | external | source:wo24c-orch |
| outer_ref | 外层独立裁判 | security | source:hidden-verify |
| world_b | 随机隐藏世界 B | external | source:hidden-verify |
| bus | adapters/ 与总线 | database | source:hybrid-mount |
| ro_zone > player | 读 ABI 与需求 | |
| player > world_a | 探测实时接口 | |
| player > out_zone | 写入映射与驱动 | emphasis |
| player > local_cli | 调 check 与 verify | |
| local_cli > out_zone | 读取待测产物 | |
| local_cli > world_a | 沙箱演练与重置 | |
| out_zone > outer_ref | 移交最终产物 | emphasis |
| outer_ref > world_b | 盲测与带外篡改 | security |
| outer_ref > bus | 落盘并组合挂载 | emphasis |

@steps id=isolation-steps
| step-ro | 只读契约区 0444 | 锁定 `target_abi.json`、`requirements.md` 和 `SKILL.md`，全程拦截改写。 | fde-isolation-arch | ro_zone |
| step-player | 双引擎外部 Agent | 优先运行 `opencode + deepseek-v4.1-flash`，失败自动切 `agy` Gemini；环境屏蔽 `docker` 与直连数据库命令。 | fde-isolation-arch | player |
| step-out | 唯一可写区 out/ | Agent 只能输出声明式 JSON 与薄驱动 `driver.py`，无法触碰标准本体或裁判代码。 | fde-isolation-arch | out_zone |
| step-ref | 外层独立裁判 | 核对只读区 SHA-256、跑静态 AST 牢笼，并注入 Agent 从未见过的隐藏世界 B。 | fde-isolation-arch | outer_ref |
| step-bus | adapters/ 与总线 | 验签通过后逐字节落盘 `examples/adapters/`，叠加 `overlay.json` 挂载至 `DeviceDriverRegistry`（缺失域自动 `standalone`）。 | fde-isolation-arch | bus |

## 从盲探到组合挂载的完整执行时序是什么？ {#inner-outer-loop}

AI-FDE 执行分为内环自驱修错、双引擎兜底重考、外环盲测终审与混合挂载四段，外环未签发通过前绝不落盘上线。[](source:wo24c-orch)

@sequence id=fde-exec-seq title="AI-FDE 双引擎内环迭代与外环盲测时序"
| actor agent | 双引擎外部 Agent | frontend | source:wo24c-orch |
| actor world_a | 公开世界 A | external | source:wo24c-orch |
| actor cli | design check / verify | security | source:verify-loop |
| actor referee | 外层独立裁判 | security | source:hidden-verify |
| actor world_b | 随机隐藏世界 B | external | source:hidden-verify |
| actor bus | DeviceDriverRegistry | database | source:hybrid-mount |
| phase | 阶段 1：只读探索与产物生成 |
| agent | world_a | 探测 OpenAPI / JSON-RPC 与实时样例记录 |
| world_a | agent | 返回真实字段、状态枚举与业务流转 | return |
| agent | cli | 写入 out/ 映射与薄驱动，执行 verify_driver.sh check |
| cli | agent | 静态校验角色、字段、枚举逆映射与 AST 预算 | return |
| phase | 阶段 2：公开世界 A 证伪（DeepSeek 优先，未过自动切 Gemini） |
| agent | cli | 执行 verify_driver.sh verify 启动差分证伪 |
| cli | world_a | 触发重置钩子恢复快照，回放多波次动作并比对回读 |
| world_a | cli | 返回真实状态与反例；未收敛则换 Gemini 重开隔离舱 | return |
| cli | agent | 公开世界 A 达标，签发 CORROBORATED | return |
| phase | 阶段 3：外层裁判独立盲测（Agent 已退场） |
| referee | world_b | 校验只读区 SHA-256 与 AST 牢笼，新建随机 PK 偏移世界 B |
| referee | world_b | 注入未知凭证跑差分证伪，验证 100% 动作覆盖与泛化 | emphasis |
| referee | world_b | 绕过本体直接删改单据，验证捕获 DEVICE_STATE_MISMATCH | security |
| world_b | referee | 确认 REJECTED 且 abox_unchanged=True，零脏写 | return |
| phase | 阶段 4：落盘持久化与混合世界组合挂载 |
| referee | bus | 落盘 adapters/，叠加 Overlay 组合挂载 MountedDevice | emphasis |

## AI-FDE 产出哪四类受控产物？ {#artifact-contract}

语义规则留在声明式 JSON，网络协议细节压进单域薄驱动，避免模型把业务规则藏进不可审计的代码分支。[](source:fde-driver-spec)

| 产物文件 | 定位与职责 | 核心约束 | 落地形态 |
| --- | --- | --- | --- |
| `mapping.json` | 声明表映射、字段转换、正反向枚举与动作绑定 | `extra="forbid"`，覆盖全部源状态与动作目标态 {good:零执行风险} | `examples/adapters/<erp>/<domain>/mapping.json` |
| `entity_resolution.json` | 声明多系统主数据消歧规则、硬键与灰区阈值 | `auto_merge` 约 0.90、`review` 约 0.55，严禁压低阈值强行合并 | 跨 CRM / ERP 共有实体对齐 |
| `overlay.json` | 在 6 域标准世界模型上单调叠加专属字段与前置红线 | 只增不减，禁用 `remove_*` 与放宽角色权限 {good:契约单调} | `examples/adapters/<erp>/overlay.json` |
| `driver.py` | 仅负责协议传输与回读的单域薄适配层 | 暴露 `build_device_bridge`，不超过 350 行且过 AST 牢笼 {caution:严格受限} | `examples/adapters/<erp>/<domain>/driver.py` |

@code id=ast-audit-code source=ast-cage language=python

## 外层裁判的 4 道硬门禁如何防住作弊与脏写？ {#four-gates}

只要模型试图靠硬编码、偷看答案或吞掉错误蒙混过关，对应门禁立即判 `FALSIFIED`。[](source:hidden-verify)

| 门禁 | 检验机制 | 防范的典型作弊或故障 | 失败结果 |
| --- | --- | --- | --- |
| 1. 隔离与反剧透 | 只读区 SHA-256 比对 + 华夏 ERP 39 项 / Odoo 15 项禁词扫描 + 命令替身屏蔽 | 篡改测试脚本、查宿主机数据库、靠提示词泄题 | 直接终止评审 {bad:阻断} |
| 2. AST 静态牢笼 | `audit_thin_driver_ast` 扫描 350 行上限、导入白名单与世界 A 字面量 | 写死样本单号/凭证、`eval`/`exec`、落盘缓存或跨包调用 | `FORBIDDEN_*` {bad:阻断} |
| 3. 隐藏世界 B 盲测 | 裁判独立建库并随机偏移主键与金额，注入新 `endpoint_config` 跑证伪 | 仅针对样例数据过关、映射逻辑缺少泛化能力 | `FALSIFIED` {bad:阻断} |
| 4. 带外篡改演练 | 绕过本体直接删除或取消外部单据，再发起本体动作 | `actuate` 盲目吞错或回显假状态导致内核账实不符 | 必须捕获 `DEVICE_STATE_MISMATCH` {good:零脏写} |

## 真实双 ERP 6 域盲测实况如何（DeepSeek vs Gemini）？ {#live-benchmarks}

以下为 `WO-24C` 在本机真实 Odoo 19 与华夏 ERP Docker 上 6 域全量盲考实测数据，全部持久化文件与原始日志 SHA-256 逐字节一致。[](source:wo24c-orch)

| ERP / 标准域 | DeepSeek V4.1 Flash 首轮实况 | 最终胜出引擎（耗时） | 薄驱动行数 | 隐藏世界 B 覆盖 | 带外篡改 |
| --- | --- | --- | --- | --- | --- |
| Odoo 19 · `std_o2c` | 首轮通过（0.0602 美元） {good:一次通过} | `deepseek-v4.1-flash`（540s） | 350 行 | 100%（`shift_pks=5`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |
| Odoo 19 · `std_p2p` | 600s 超时且未产出 `out/` {caution:触发兜底} | `gemini-4-neon-medium`（297s） | 293 行 | 100%（`shift_pks=5`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |
| Odoo 19 · `std_r2r` | 600s 超时且未产出 `out/` {caution:触发兜底} | `gemini-4-neon-medium`（278s） | 318 行 | 100%（`shift_pks=7`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |
| 华夏 ERP · `std_p2p` | 首轮通过（0.0518 美元） {good:一次通过} | `deepseek-v4.1-flash`（307s） | 312 行 | 100%（`shift_pks=8`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |
| 华夏 ERP · `std_o2c` | 600s 超时且 367 行超 350 预算 {caution:触发兜底} | `gemini-4-argon-medium`（445s） | 285 行 | 100%（`shift_pks=5`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |
| 华夏 ERP · `std_h2r` | 600s 超时且调岗差分失败（66.7%） {caution:触发兜底} | `gemini-4-neon-medium`（243s） | 300 行 | 100%（`shift_pks=6`） | `DEVICE_STATE_MISMATCH` {good:零脏写} |

DeepSeek V4.1 Flash 在单据流熟悉的 2/6 域以约 0.05–0.06 美元首轮过关。面对新域复杂向导或 1 秒防重放限制，自动切 Gemini 在 4–7 分钟内收敛至不超过 318 行并盲过隐藏世界 B。挂载到 6 域企业标准世界模型时，两台 ERP 各自缺失的闭环域自动由本体 `standalone` 补齐。[](source:hybrid-mount)

## AI-FDE 当前的工程边界与上线验收清单 {#boundaries-and-checklist}

@callout level=warning title="四个必须守住的真实工程边界"
| 1. 无可探测接口的系统：既无 OpenAPI/RPC 也无只读库视图的纯桌面 GUI 或专有二进制老系统，AI-FDE 无法自动反编译。 |
| 2. 隐性服务端副作用：例如华夏 ERP 传入 `linkNumber` 会隐式推进源单状态，或 1 秒内重复写请求触发防重放校验，必须靠真实写测暴露。 |
| 3. 单域蓝图切分与闭集角色门禁：`SymbolicCrucible` Gate 5 单蓝图上限 250 AST 节点，多域须按单域解耦再组合挂载。`979d47f` 已在 `crucible.py` 以纯 M2 `declared_roles` 消除 `role_name` 误判。 |
| 4. 业务口径归人类定稿：多对多归属、结算红线与审批分级须由业务专家先锁定 Target ABI 与单调 Overlay，AI-FDE 只负责对齐旧系统。 |

接入新外部系统并落盘挂载前，逐项核对以下条件：[](source:crucible-role)

- [ ] 公开世界 A 重置钩子可无残留恢复快照，且不向 Agent 暴露管理员凭据。
- [ ] `driver.py` 通过 `audit_thin_driver_ast`，行数不超过 350 行且 0 条硬编码字面量。
- [ ] 隐藏世界 B 采用新租户、偏移主键与新金额，差分证伪返回 `CORROBORATED` 且目标动作 100% 覆盖。
- [ ] 带外篡改测试确认 `DEVICE_STATE_MISMATCH` 触发 `REJECTED` 且 `abox_unchanged=True`。
- [ ] 落仓 `examples/adapters/` 产物与盲考日志 SHA-256 逐字节一致，单调 `overlay.json` 通过 `open_ontology.design check`。
- [ ] 跨系统主数据若存在 L4 灰区候选，已保留待人工裁决而非调低阈值强行合并。
