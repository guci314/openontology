---
name: platform-genesis
description: 平台管理员创世与设备接入标准流程：从注册外部设备、创建轻量级设备适配器（含异构字段映射）到同步写入世界状态。
---

# 平台管理员创世与设备接入指南

当用户希望把外部 REST/OpenAPI 设备接入到本体世界状态时，遵循以下标准步骤：

1. **检查现有资源**：先查询 `PlatformDevice`、`PlatformWorld`、`PlatformModel`，确认设备与目标世界是否已就绪。
2. **缺失即创世**：
   - 若无领域模型，调用 `create_model_spec` 创建模型；
   - 若无算力宿体，调用 `provision_runtime` 装配宿体；
   - 若无世界状态，调用 `create_world` 创建世界；
   - 若设备未注册，调用 `register_device` 注册设备（`kind="symbolic"`）。
3. **绑定轻量级适配器（推荐）**：
   - 调用 `create_adapter` 将源设备（`device_id`）绑定到目标世界（`world_id`）的指定对象类型（`object_type`）。
   - 若设备字段名与本体模型字段名不一致，通过 `field_mapping_json` 配置映射（例如 `{"ext_id":"id","num":"value"}`）；一致则传 `{}`。
   - 设置 `interval_sec`（例如 `3` 秒）即可在 **Runtime** 标签页看到适配器并自动周期同步。
4. **受控交互**：执行前优先渲染 `action_form` 或 `diff_preview` 卡片供用户确认。
