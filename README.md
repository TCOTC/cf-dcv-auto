# Cloudflare DCV 自动续订

每天检查 Cloudflare 上是否有证书需要重新做域名控制验证（DCV）。有的话：

1. **先在仓库里创建一条 issue 提醒你**（创建失败则工作流直接失败，不会偷偷继续）；
2. 然后把 DCV 需要的 TXT 记录自动写进 Cloudflare DNS；
3. 再调用 Cloudflare API 触发一次即时重新验证；
4. 验证完成后，自动关闭之前那条提醒 issue。

全程不需要任何本地机器常驻运行。

## 为什么需要它

证书涵盖通配符（`*.mytemos.com`）时，CA/B 论坛新规不允许使用 HTTP DCV，只能走 TXT 验证。
于是每个续订周期（约 90 天）Cloudflare 都会发邮件要求手动添加一条
`_acme-challenge.<域名>` 的 TXT 记录。这个仓库把这一步自动化，并用 issue 留痕。

## 设置步骤

### 1. Cloudflare API token

后台 → 头像 → My Profile → API Tokens → Create Custom Token，权限：

| 权限 | 用途 | 必须 |
| --- | --- | --- |
| Zone → Zone → Read | 用域名查 zone_id | 是 |
| Zone → SSL and Certificates → Read | 读取待验证的 TXT token | 是 |
| Zone → DNS → Edit | 写入 TXT 记录 | 是 |
| Zone → SSL and Certificates → Edit | 触发即时重新验证 | 可选，不授权就把 NO_TRIGGER 设为 true |

Zone Resources 选 `mytemos.com`。然后到仓库 Settings → Secrets and variables → Actions
新增 secret：`CLOUDFLARE_API_TOKEN`。

### 2. 创建 issue 用的 token

通常**不需要**额外配置。工作流里的 `permissions: issues: write` 已经让内置的
`github.token` 能够在本仓库创建 issue，issue 作者会显示为 `github-actions[bot]`。

只有下面两种情况才需要额外加一个 secret `NOTIFY_GH_TOKEN`（个人 access token，
经典 token 勾 `repo`，细粒度 token 勾 Issues: Read and write）：

- 你想让 issue 的作者显示成你自己；
- 仓库的 Actions 权限被组织策略限制，内置 token 创建不了 issue。

配置了就会优先使用它，没配置就用内置 token。

### 3. 验证

Actions → `Cloudflare DCV auto` → Run workflow，先勾上 `dry_run` 看日志是否符合预期，
再不带 `dry_run` 跑一次。

## 触发与行为

| 触发 | 说明 |
| --- | --- |
| `schedule: 23 3 * * *` | 每天一次（UTC 03:23 = 北京时间 11:23） |
| `schedule: 0 3 1 * *` | 每月 1 号提交心跳，防止 60 天无提交导致定时任务被停用 |
| `workflow_dispatch` | 手动触发，可勾选 `dry_run`；另有 `keepalive` 可用来单独验证每月心跳任务 |

工作流里使用的 Action（目前只有 `actions/checkout`）由 Dependabot 每周检查并提 PR
（配置见 `.github/dependabot.yml`），合并前可以先看 PR 里的 release notes。

可用的环境变量（在 workflow 里追加）：

| 变量 | 作用 |
| --- | --- |
| `DRY_RUN=true` | 只打印，不做任何修改 |
| `NO_TRIGGER=true` | 只写 TXT，不调用 PATCH 触发验证 |
| `NOTIFY_ISSUE=true` | 创建 issue 提醒（工作流里已默认开启） |
| `CLOSE_ISSUES=true` | 验证完成后自动关闭提醒 issue，默认 true |
| `NOTIFY_MENTION=用户名` | 在 issue 正文里 @ 某人，默认 @ 仓库所有者 |
| `CLEANUP=true` | 清理存在超过 `CLEANUP_AGE_DAYS` 天且不再需要的 `_acme-challenge` TXT |
| `CLEANUP_AGE_DAYS=60` | 清理阈值天数 |
| `TXT_TTL=120` | TXT 记录 TTL |

## 注意事项

- **公开仓库的 Actions 日志是公开的**：日志里会出现域名、`_acme-challenge` 记录名和
  token 值 —— 这些在 DNS 里本来就是公开可查的。但 **API token 只能放 secret**，
  绝不能写进代码或日志。GitHub 会自动遮蔽 secret。
- **不要改成由 PR 触发**：那样外部 PR 的代码可能拿到你的 secret。
  本仓库只由 `schedule` 和手动触发运行。
- **issue 创建失败会导致工作流失败**，这是刻意设计：宁可报错让你知道，
  也不要出现"没有提醒却悄悄续订"的情况。
- 脚本幂等：同一条 token 已存在就跳过，重复运行没有副作用；
  同一个 token 也不会重复创建 issue。
- 默认不删除任何 DNS 记录，只有 `CLEANUP=true` 时才会清理过期的 `_acme-challenge.` 记录。
