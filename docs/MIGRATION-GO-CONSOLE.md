# 迁移说明：Python → Go（Web / Build / Console）

## 渠道模型命名（强制后缀）

| 渠道 | Provider | 后缀 | 示例 PublicID |
| --- | --- | --- | --- |
| Web | `grok_web` | `-web` | `grok-chat-fast-web` |
| Build | `grok_build` | `-build` | `grok-4.5-build` |
| Console | `grok_console` | `-console` | `grok-4.3-high-console` |

- `GET /v1/models` **只返回**带后缀的 PublicID。
- 不提供无后缀公开别名（避免混渠道）。
- Console 历史别名（如 `0309-*`、`reasoning-console`）仍可调用，默认列表不展示。

## API Key 兼容

在 `config.yaml`：

```yaml
compat:
  legacyAPIKeys:
    - "现网明文 api_key"
```

同时支持管理端创建的 `g2a_*` 客户端密钥。

## 账号导入

- 导入 Web SSO（JSON / 明文）会 **双写** `grok_web` 与 `grok_console` 账号。
- Build 使用 Device OAuth / OAuth JSON。
- Console 上游：`console.x.ai/v1/responses`，SSO Cookie + `Bearer anonymous`。
- Console 本地额度窗口默认 `150 / 86400s`（可在设置页调整）。

## 流式稳定性

```yaml
provider:
  console:
    streamHeartbeatInterval: 15   # 秒；0=关闭
    timeoutSeconds: 300
```

- 长流式会在空闲时发送 SSE 注释心跳（`: ping`），避免 Zeabur / Nginx 空闲断连。
- 建议反代 `proxy_read_timeout` / idle timeout **≥ 心跳间隔 × 3**（至少 60s）。
- 应用侧：`server.readTimeout` 主要限制上传完整请求体时间；推理流式受 `requestTimeout` 与 Console `timeoutSeconds` 约束。

## 合成 reasoning

Console 默认注入合成 thinking 摘要（「已深度思考。」），兼容依赖思考 UI 的客户端；响应 `model` 字段回写带 `-console` 的 PublicID。

## Zeabur 仅环境变量部署（推荐）

不必挂载 `config.yaml`。只需：

1. **镜像**：`ghcr.io/sakuralaaa/grok2api-minechange:latest`
2. **端口**：`8000`
3. **数据卷**：挂载到 `/app/data`
4. **环境变量**（必填）：

| 变量 | 说明 |
| --- | --- |
| `GROK2API_JWT_SECRET` | ≥32 字符，`openssl rand -hex 32` |
| `GROK2API_CREDENTIAL_ENCRYPTION_KEY` | Base64 的 32 字节，`openssl rand -base64 32` |
| `GROK2API_ADMIN_PASSWORD` | 首次管理员密码 |
| `GROK2API_PUBLIC_API_BASE_URL` | 公网地址，如 `https://xxx.zeabur.app` |

可选：

| 变量 | 说明 |
| --- | --- |
| `GROK2API_ADMIN_USERNAME` | 默认 `admin` |
| `GROK2API_LEGACY_API_KEYS` | 旧明文 API Key，逗号分隔 |
| `GROK2API_SECURE_COOKIES` | 默认：HTTPS 自动 `true` |
| `GROK2API_CONFIG_YAML` | 整份 YAML 字符串（进阶） |
| `GROK2API_CONFIG_B64` | 整份 YAML 的 base64（进阶） |

仍支持挂载文件到 `/run/grok2api/config.yaml`（优先于环境变量生成）。

健康检查：`GET /healthz`

## Zeabur / VPS 切流

1. 备份现网 `data/` 与 `config.yaml`。
2. 使用 GHCR 镜像：`ghcr.io/sakuralaaa/grok2api-minechange:latest`（或 commit tag）。
3. 挂载配置到 `/run/grok2api/config.yaml`，数据卷 `/app/data`。
4. 健康检查：`GET /healthz`，端口 `8000`。
5. 写入 secrets + `compat.legacyAPIKeys` + `bootstrapAdmin`。
6. 导入 SSO，确认账号页 Web/Console 双写成功。
7. 冒烟（替换 Base URL 与 Key）：

```bash
# Console
curl -sS "$BASE/v1/models" -H "Authorization: Bearer $KEY" | jq '.data[].id' | grep console
curl -sS "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json"   -d '{"model":"grok-4.3-high-console","messages":[{"role":"user","content":"ping"}],"stream":false}'

# Web
curl -sS "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json"   -d '{"model":"grok-chat-fast-web","messages":[{"role":"user","content":"ping"}],"stream":false}'

# Build（需已有 OAuth 账号与同步模型）
curl -sS "$BASE/v1/responses" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json"   -d '{"model":"grok-4.5-build","input":"ping","store":false,"stream":false}'
```

8. 切换主流量后，旧容器保留 **≥ 24h** 便于回滚。

## 回滚

- Python 旧树归档于 `legacy/python-v2/`（仅参考，不再作为运行时）。
- 回滚镜像到上一 GHCR tag，恢复备份的 `data/` 与配置。
