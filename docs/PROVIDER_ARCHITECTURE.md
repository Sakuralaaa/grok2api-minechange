# Provider Architecture (Fork Notes)

本 fork 吸收了原作者的 Provider Definition 能力边界，同时保留强制渠道后缀与运维增强。

## Public Model Naming

对外公开模型名继续强制渠道后缀，避免混用：

- Web: `*-web`
- Build: `*-build`
- Console: `*-console`

Definition 中的 `ModelNamespace`（`Build` / `Web` / `Console`）只用于内部能力描述与校验，**不会**改变 `/v1/models` 或请求体中的公开模型名。

当前阶段不提供：

- 无后缀公开模型名
- 同名跨渠道自动分流
- `model_route_aliases` 无后缀兼容解析

## Definition Responsibilities

每个生产 Adapter 必须实现 `DefinitionAdapter` 并声明：

- 模型目录类型（remote / static）
- 额度权威（billing / remote_window / local_window）
- 对话表面（responses / chat / messages / compact / stored）
- 媒体表面（image / image_edit / video）
- 凭据表面（oauth/sso、import/refresh/device oauth）
- 推理策略（usage 权威、403 是否按 egress 切换）

`provider.NewRegistry(...).Validate()` 在启动时校验：

1. 三渠道均已注册 Adapter 与 Definition
2. Definition 内部自洽
3. 声明的能力与实际小接口实现一致

## Gateway Consumption

网关仅把“能力边界类”判断收敛到 Definition：

- usage source
- compact / stored responses / previous_response_id
- 403 是否按 egress 故障切换
- remote/local window 额度处理

账号故障恢复、Build token 刷新、Console 401 标记等渠道特定恢复路径仍保留现有逻辑。

## Current Fork Preserved Advantages

- `docker/entrypoint.sh` 环境变量生成配置
- `compat.legacyAPIKeys`
- Web SSO 导入双写 Console
- Console SSE 心跳与合成 thinking
- Console 运营化 catalog 与历史别名
- `docs/MIGRATION-GO-CONSOLE.md`
- 细粒度 `provider.console.*` 配置项

## Change Checklist

上游变化时优先只改对应 Provider 包：

1. 更新 Adapter 协议实现与测试
2. 若对外能力边界变化，同步更新 `Definition()`
3. 跑 Definition 契约测试与相关 gateway 测试
4. 保持公开模型强制后缀规则不变

## Model Aliases (Phase 2)

历史/兼容公开模型名通过 `model_route_aliases` 解析到规范路由主键：

1. 请求公开名必须带合法渠道后缀；
2. 先精确匹配 `model_routes.public_id`；
3. 再查 `model_route_aliases.alias`；
4. 权限与审计使用规范路由 ID；
5. `/v1/models` 只返回 listed/规范模型，不返回别名。

Console 特殊约定：

- catalog 中 `AliasOf` 只用于别名绑定，不再 seed 独立可服务路由；
- 协议侧 `Effort`/工具选项仍按**请求的公开模型名**走 `ResolvePublic`；
- 别名自身声明的 `Effort` 继续生效，不会被规范路由吞掉。

管理端改名规范 `public_id` 时会保留路由主键，并把旧名写入别名表。
