# OpenOntology AI-FDE 的完整执行逻辑 {#ai-fde-execution-logic}

OpenOntology 的 **AI-FDE** 采用“外部可插拔编程 Agent + 独立编译证伪裁判”的物理隔离闭环。它把陌生外部系统编译为声明式映射和受限薄驱动，经盲测与篡改检验后，才热挂载到 `DeviceDriverRegistry`。

- 选手与裁判物理隔离：Agent 只拥有 `out/` 目录写权限，只读 ABI、需求与裁判脚本全程受 SHA-256 保护。[](source:jsherp-bench)
- 静态 AST 牢笼防作弊：薄驱动代码不超过 350 行，严格封禁高危导入、动态执行、写盘和样本硬编码。[](source:ast-cage)
- 波普尔双重证伪：先在公开世界 A 通过静态编译与差分证伪，再由外层裁判注入未知隐藏世界 B 盲测。[](source:hidden-verify)
- 真实 ERP 盲测已跑通：在 Odoo 19 与华夏 ERP 上，DeepSeek V4.1 Flash 均 0 轮返工一次通过，单次成本低于 0.12 美元。[](source:jsherp-bench)

@answer scope="OpenOntology e25ad5a · AI-FDE 设计态闭环 · 已测 Odoo 19 与华夏 ERP"
@meta locale=zh-CN style=editorial
@source fde-skill title="AI-FDE 技能契约与薄驱动规范" path=skills/openontology-fde/SKILL.md lines=10:27
@source fde-driver-spec title="薄驱动入口与状态漂移契约" path=skills/openontology-fde/SKILL.md lines=105:133
@source ast-cage title="audit_thin_driver_ast 静态牢笼" path=open_ontology/design.py lines=773:808
@source verify-loop title="run_design_verify 坩埚与差分证伪" path=open_ontology/design.py lines=1796:1875
@source hidden-verify title="verify_thin_driver_on_hidden_world 隐藏世界盲测" path=open_ontology/design.py lines=1890:1945
@source jsherp-bench title="华夏 ERP 盲测隔离与反剧透守卫" path=examples/agy_fde_live_jsherp_protocol_benchmark.py lines=1:28
@source driver-bus title="MountedDevice 热挂载总线契约" path=open_ontology/drivers.py lines=82:120

## 为什么必须把 AI-FDE 与裁判物理隔离？ {#isolation-boundary}

内核不内置大模型循环，而是把模型降级为受限外部施工员，把确定性裁判留在外层不可触碰区。[](source:fde-skill)

@architecture id=fde-isolation-arch title="AI-FDE 三区物理隔离架构"
| ro_zone | 只读契约区 0444 | security | source:fde-skill |
| player | 外部编程 Agent | frontend | source:jsherp-bench |
| out_zone | 唯一可写区 out/ | backend | source:fde-driver-spec |
| local_cli | 本地裁判脚本 0555 | security | source:verify-loop |
| world_a | 公开世界 A 与重置钩子 | external | source:jsherp-bench |
| outer_ref | 外层独立裁判 | security | source:hidden-verify |
| world_b | 随机隐藏世界 B | external | source:hidden-verify |
| bus | DeviceDriverRegistry | database | source:driver-bus |
| ro_zone > player | 读 ABI 与需求 | |
| player > world_a | 探测实时接口 | |
| player > out_zone | 写入映射与驱动 | emphasis |
| player > local_cli | 调 check 与 verify | |
| local_cli > out_zone | 读取待测产物 | |
| local_cli > world_a | 沙箱演练与重置 | |
| out_zone > outer_ref | 移交最终产物 | emphasis |
| outer_ref > world_b | 盲测与带外篡改 | security |
| outer_ref > bus | 验真后热挂载 | emphasis |

@steps id=isolation-steps
| step-ro | 只读契约区 0444 | 锁定 `target_abi.json`、`requirements.md` 和 `SKILL.md`，全程拦截改写。 | fde-isolation-arch | ro_zone |
| step-player | 外部编程 Agent | 可自由替换 `opencode + deepseek-v4.1-flash` 或 `agy`，环境屏蔽 `docker` 与数据库直连命令。 | fde-isolation-arch | player |
| step-out | 唯一可写区 out/ | Agent 只能输出声明式 JSON 与薄驱动 `driver.py`，无法触碰标准本体或测试代码。 | fde-isolation-arch | out_zone |
| step-ref | 外层独立裁判 | 核对只读区 SHA-256、跑静态 AST 牢笼，并注入 Agent 从未见过的隐藏世界 B。 | fde-isolation-arch | outer_ref |

## 从盲探到热挂载的完整执行时序是什么？ {#inner-outer-loop}

AI-FDE 执行分为“内环自驱修错”与“外环盲测终审”两段，外环未签发通过前绝不触碰生产总线。[](source:verify-loop)

@sequence id=fde-exec-seq title="AI-FDE 内环迭代与外环盲测时序"
| actor agent | 外部编程 Agent | frontend | source:jsherp-bench |
| actor world_a | 公开世界 A | external | source:jsherp-bench |
| actor cli | design check / verify | security | source:verify-loop |
| actor referee | 外层独立裁判 | security | source:hidden-verify |
| actor world_b | 随机隐藏世界 B | external | source:hidden-verify |
| actor bus | DeviceDriverRegistry | database | source:driver-bus |
| phase | 阶段 1：只读探索与产物生成 |
| agent | world_a | 探测 OpenAPI / RPC / 只读表结构与样例行 |
| world_a | agent | 返回真实字段、状态枚举与业务流转 | return |
| agent | cli | 写入 out/ 映射与薄驱动，执行 verify_driver.sh check |
| cli | agent | 静态校验角色、字段、枚举逆映射与 AST 预算 | return |
| phase | 阶段 2：公开世界 A 波普尔证伪（内环自修） |
| agent | cli | 执行 verify_driver.sh verify 启动差分证伪 |
| cli | world_a | 触发重置钩子恢复快照，回放多波次动作并比对回读 |
| world_a | cli | 返回物理真实状态，未过则输出反例供 Agent 返工 | return |
| cli | agent | 公开世界 A 达标，签发 CORROBORATED | return |
| phase | 阶段 3：外层裁判独立盲测（Agent 已退场） |
| referee | world_b | 校验只读区 SHA-256 与 AST 牢笼，新建随机偏移租户 B |
| referee | world_b | 注入未知凭证跑差分证伪，验证泛化能力 | emphasis |
| referee | world_b | 绕过本体直接删改单据，验证捕获 DEVICE_STATE_MISMATCH | security |
| world_b | referee | 确认 REJECTED 且 abox_unchanged=True，零脏写 | return |
| phase | 阶段 4：热挂载上线 |
| referee | bus | 注册 MountedDevice，原子切换至生产运行态总线 | emphasis |

## AI-FDE 产出哪四类受控产物？ {#artifact-contract}

语义规则留在声明式 JSON，网络协议细节压进受限薄驱动，避免模型把业务规则藏进不可审计的代码分支。[](source:fde-driver-spec)

| 产物文件 | 定位与职责 | 核心约束 | 适用场景 |
| --- | --- | --- | --- |
| `out/*_mapping.json` | 声明表映射、字段转换、正反向枚举与动作绑定 | `extra="forbid"`，覆盖全部源状态与动作目标态 {good:零执行风险} | 所有 SQLite / RPC / REST 设备 |
| `out/entity_resolution.json` | 声明多系统主数据消歧规则、硬键与灰区阈值 | `auto_merge` 约 0.90、`review` 约 0.55，严禁压低阈值强行合并 | 跨 CRM / ERP 共有实体对齐 |
| `out/customer_overlay.json` | 在标准 ABI 之上单调叠加企业专属前置规则 | 只增不减，禁用 `remove_*` 与放宽角色权限 {good:契约单调} | 客户定制风控与审批红线 |
| `out/driver.py` | 仅负责协议传输与回读的薄适配层 | 暴露 `build_device_bridge`，不超过 350 行且过 AST 牢笼 {caution:严格受限} | 多步向导、REST、JSON-RPC 系统 |

@code id=ast-audit-code source=ast-cage language=python

## 外层裁判的 4 道硬门禁如何防住作弊与脏写？ {#four-gates}

只要模型试图靠硬编码、偷看答案或吞掉错误来蒙混过关，会在对应门禁直接判 `FALSIFIED`。[](source:hidden-verify)

| 门禁 | 检验机制 | 防范的典型作弊或故障 | 失败结果 |
| --- | --- | --- | --- |
| 1. 隔离与反剧透 | 只读区 SHA-256 比对 + 40 项源码/字段禁词扫描 + 命令替身屏蔽 | 篡改测试脚本、查宿主机数据库、靠提示词泄题 | 直接终止评审 {bad:阻断} |
| 2. AST 静态牢笼 | `audit_thin_driver_ast` 扫描行预算、导入白名单与世界 A 字面量 | 写死样本单号/凭证、`eval`/`exec`、落盘缓存或跨包调用 | `FORBIDDEN_*` {bad:阻断} |
| 3. 隐藏世界 B 盲测 | 裁判独立建库并偏移主键与金额，注入新 `endpoint_config` 跑证伪 | 仅针对样例数据过关、映射逻辑缺少泛化能力 | `FALSIFIED` {bad:阻断} |
| 4. 带外篡改演练 | 绕过本体直接删除或取消外部单据，再发起本体动作 | `actuate` 盲目吞错或回显假状态导致内核账实不符 | 必须捕获 `DEVICE_STATE_MISMATCH` {good:零脏写} |

## 真实 ERP 盲测结果与成本对比如何？ {#live-benchmarks}

以下数据均来自本地真实 Docker 容器与公开运行日志，不包含模拟桩。[](source:jsherp-bench)

| 实测目标与模型 | 返工轮次 | 薄驱动行数 | 隐藏世界 B 动作覆盖 | 带外篡改结果 | 单次成本 |
| --- | --- | --- | --- | --- | --- |
| Live Odoo 19 · `agy` + Gemini 4 Neon | 0 轮 | 合规（不超过 350 行） | 100% `CORROBORATED` | `DEVICE_STATE_MISMATCH` {good:零脏写} | 订阅额度 |
| Live Odoo 19 · `opencode` + DeepSeek V4.1 Flash | 0 轮 | 合规（不超过 350 行） | 100% `CORROBORATED` | `DEVICE_STATE_MISMATCH` {good:零脏写} | 约 0.07 美元 {good:低成本} |
| Live 华夏 ERP · `opencode` + DeepSeek V4.1 Flash | 0 轮 | 347 行（0 条 AST 问题） | 100%（4/4，跃迁比 1.67） | `DEVICE_STATE_MISMATCH` {good:零脏写} | 0.1193 美元 {good:低成本} |

## AI-FDE 当前的工程边界与上线验收清单 {#boundaries-and-checklist}

@callout level=warning title="三个不能靠 AI-FDE 盲解解决的真实边界"
| 1. 无可探测接口的系统：既无 OpenAPI/RPC 也无只读库视图的纯桌面 GUI 或专有二进制老系统，AI-FDE 无法自动反编译。 |
| 2. 隐性服务端副作用：例如华夏 ERP 传入 `linkNumber` 会隐式推进源单状态，或短时重复请求触发会话清理，必须靠真实写测暴露。 |
| 3. 业务口径归人类定稿：多对多归属、结算红线与审批分级须由业务专家先锁定 Target ABI 与 Overlay。AI-FDE 只负责对齐旧系统。 |

接入新外部系统并准备热挂载前，逐项核对以下条件：

- [ ] 公开世界 A 的重置钩子可无残留恢复快照，且不向 Agent 暴露管理员凭据。
- [ ] `out/driver.py` 通过 `audit_thin_driver_ast`，行数不超过 350 行且 0 条硬编码字面量。
- [ ] 隐藏世界 B 采用新租户、偏移主键与新金额，且差分证伪返回 `CORROBORATED`。
- [ ] 带外篡改测试确认 `DEVICE_STATE_MISMATCH` 触发 `REJECTED` 且 `abox_unchanged=True`。
- [ ] 跨系统主数据若存在 L4 灰区候选，已保留待人工裁决而非调低阈值强行合并。
