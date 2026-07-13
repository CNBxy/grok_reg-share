# OpenAI-CPA 本地收件回退设计

## 目标

让本地 Windows 项目在没有公网 IP、VPS 或 Cloudflare Tunnel 时，也能选择 `OpenAI-CPA 内存池` 完成验证码接收。

## 数据流

注册时仍按 `defaultDomains` 生成随机 Catch-all 邮箱。取码循环每轮先检查 Webhook 内存池；若没有验证码并且启用了 `openai_cpa_cloudmail_fallback`，则使用已有 CloudMail 管理地址、管理员邮箱和密码获取公共 Token，并查询目标邮箱。

内存池先命中时不会发起 CloudMail 邮件查询。Webhook 不可用但 CloudMail 配置完整时，可以完全依靠 CloudMail 本地回退。CloudMail 暂时失败但 Webhook 已配置时，流程继续等待内存池，不会立即终止。

## 配置与安全

- 不重复保存 CloudMail 凭证，直接复用现有字段。
- `openai_cpa_cloudmail_fallback` 可通过页面或环境变量控制。
- 日志只记录通道状态、邮件主题和验证码，不输出管理员密码或公共 Token。
- VPS 模板默认关闭回退，优先使用 Webhook；本地默认开启回退。

## 验证

- 单独验证纯内存池、纯 CloudMail 回退和双通道优先级。
- 验证两条通道都不可用时给出明确配置错误。
- 浏览器确认开关和 CloudMail 字段只在 OpenAI-CPA 模式显示。
