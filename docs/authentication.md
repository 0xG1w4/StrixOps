# Console 登录与账号设置

Console 只有一个账号 `strix`，没有注册、用户列表或角色系统。
首次安装使用 `strix / strix123` 登录，随后必须修改密码，才能查看任务和使用工作台。
密码修改后保持当前浏览器登录，同时撤销其他旧会话。升级、重启、重新安装及卸载执行环境不会重置密码。

登录页不预填或提示有效账号，需自行输入账号与密码。浏览器仍可使用密码管理器填写自己保存的凭据。
错误账号与错误密码使用同一类错误提示；账号不预填不替代密码强度、登录限速和会话保护。

右上角的 Strix 头像支持悬停、点击、键盘与触控打开菜单。选择**设置**修改密码并查看
最近五次成功登录的时间和 IP；选择**登出**结束当前会话。登出不停止扫描、队列或 MCP 捕获。
登录时间以 UTC 保存，按浏览器时区显示；首次修改密码不会额外增加一笔登录记录。

## 密码与会话

- 新密码经 Unicode NFKC 规范化后需为 15–128 个字符、UTF-8 不超过 512 字节。
  允许空格、密码管理器与粘贴；拒绝内置常见密码、账号衍生、重复和顺序模式。
  内置列表不是完整的已泄漏密码数据库。
- 密码使用独立随机盐与 scrypt（N=131072、r=8、p=1）保存；不会储存明文密码。
- 随机会话令牌只放在 HttpOnly、SameSite=Strict、无 Domain 的 Cookie 中；数据库只存令牌摘要。
  HTTPS 使用 Secure 及 `__Host-` Cookie。浏览器不把 Console 密码、会话令牌或 CSRF 令牌写入 localStorage。
- 每次登录生成新会话。会话最长 12 小时，连续 30 分钟无认证请求后失效，最多保留 10 个会话。
  自动轮询属于请求；这不是检测键盘／鼠标活动的计时器。
- 登录与改密码尝试在执行密码散列前限速：每个 IP 每 5 分钟 10 次、全局每 5 分钟 50 次。
  限速记录会持久保存，重启不会清除。密码散列工作最多并行 2 个。

## 服务访问边界

所有任务、设置、报告、证据下载、FOFA 与 MCP REST API 都需要有效会话。
首次改密码前只开放账号操作。修改操作要求 CSRF 令牌及同源请求检查；不开放通配 CORS。
已建立的 SSE 连接也会在继续发送时检查会话，防止登出后仍接收新数据。
账号请求限制体积和读取时间，参数化 SQL 不拼接用户输入，验证错误不回显密码。

`/api/health` 公开部分仅包含健康状态、产品和版本，不再透露目录或任务数量。
源码管理脚本仍可用：本地程序身份与命令核对负责识别实例，健康检查负责确认服务和版本。
内建 Swagger／ReDoc 页面及 OpenAPI 导出关闭。静态 HTML 使用本次构建的内联脚本 SHA-256
白名单 CSP，并禁止嵌入框架、插件对象与非同源脚本。现有样式组件仍需要内联样式。

前端开发统一通过 Next.js 的 `/api/*` 同源转发访问后端。直接跨端口调用 API 不再通过开放 CORS 放行。
开发代理会重写 Host，启动开发后端时明确允许自己的开发页来源：

```bash
STRIXOPS_AUTH_TRUSTED_ORIGINS=http://localhost:3100,http://127.0.0.1:3100 \
  uv run strixops-console --host 127.0.0.1 --port 8300
```

该列表默认空白，而且只有本机上游可用；生产同源部署不需配置。
生产使用 `next build` 的静态输出，由 Console 提供服务，不运行 Next.js Server Actions 或 RSC 服务端。

MCP 页面直接沿用 Console 登录，不再依靠旧自动浏览器 Token 作为登录凭证。
外部 MCP 程序若需要无浏览器连接，可使用明确配置的 `STRIXOPS_MCP_TOKEN`，且它**只适用于**
`/api/mcp/transport`，不能访问 Console REST API、下载证据或修改账号。
原有代理监听凭证和 CA 与 Console 登录是独立的，不需重新导入 CA。

## HTTP、HTTPS 与来源 IP

仍支持现有 `http://主机IP:8300` 部署，但 HTTP 不能保护密码和 Cookie 在网络上的传输。
远程或公开部署应先通过可信任的反向代理配置 HTTPS，再使用默认账号完成首次改密码；
默认账号只用于初始化，不适合作为长期凭证。

IP 来自 Uvicorn 已验证的连接信息，应用不会自行信任任意 `X-Forwarded-For` 或 `X-Real-IP`。
默认仅信任本机反向代理；其他代理应将 `FORWARDED_ALLOW_IPS` 设为具体可信代理 IP，
不要使用 `*`。代理需覆盖客户端传来的转发头，保留正确的 Host 并传递 HTTPS 协议。
若未配置可信代理，记录显示代理 IP，避免把客户端伪造的 IP 当成登录来源。

## 数据与维护

默认账号数据库：`~/.strixops/auth.sqlite3`，或 `STRIXOPS_CONSOLE_CONFIG` 所在目录的
`auth.sqlite3`。可用 `STRIXOPS_AUTH_DB` 覆写；管理脚本会保留这一路径。
数据库及锁文件权限为 0600，不进入 Git 或发布包。备份时与其他 Console 设置一起保留，
不要在升级时删除；存储损坏会拒绝认证，不会自动恢复默认密码。

此功能不提供网络上的找回密码、注册或重置默认账号入口。

实现参考：[OWASP 密码储存指南](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、
[NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)。
依赖检查同时核对 [Next.js 2026 年 8 月安全公告](https://nextjs.org/blog/august-2026-security-release)，
并将 Next 内嵌 PostCSS 限定为 8.5.26，覆盖
[PostCSS 安全修补](https://github.com/postcss/postcss/security/advisories/GHSA-fxqj-rqcc-2cmp)。
安全措施与依赖审计降低已知风险，不代表能够保证没有未知漏洞。
