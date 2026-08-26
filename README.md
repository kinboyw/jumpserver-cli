# jumpserver-cli

`jumpserver-cli` 是一个轻量的命令行工具，用于通过 JumpServer 已授权的资产生成临时 SSH 连接，并提供交互式 SSH 和 PTY 方式的远程命令执行。

它不是 JumpServer 服务端，也不会绕过 JumpServer 的权限控制。使用者必须已经拥有对应 JumpServer 实例、资产和系统用户的访问权限。

## 功能

- 复用浏览器 JumpServer 会话 Cookie，或使用 JumpServer Access Key / Secret。
- 按 IP 或主机名搜索资产，并选择可用系统用户。
- 生成短时有效的 SSH 连接令牌。
- `jssh` 进入交互式 SSH 会话。
- `jexec` 通过 PTY 执行远程命令，并对高风险操作提供确认门禁。
- `jscp` 保留为实验性入口。部分 JumpServer 网关要求 PTY，标准 SCP 可能不可用。

## 前置条件

- Python 3.10 或更高版本。
- 能访问 JumpServer 和目标资产网络。
- 推荐安装 [uv](https://docs.astral.sh/uv/)，也可以直接使用系统 Python。
- 执行 SSH 连接需要 `sshpass` 和 `ssh`。
- 浏览器 Cookie 认证需要 Tampermonkey 或兼容用户脚本的扩展。

检查环境：

```bash
python3 --version
uv --version                 # 使用 uv 时
ssh -V
sshpass -V
```

## 安装

### 方式一：直接运行

```bash
git clone <repository-url> jumpcli
cd jumpcli
export JMS_BASE_URL='https://jumpserver.example.com'
uv run python jump_cli.py --help
```

本项目当前使用标准库，不需要下载 Python 第三方依赖。`uv run` 只负责使用项目声明的 Python 版本约束。

没有 uv 时：

```bash
cd jumpcli
JMS_BASE_URL='https://jumpserver.example.com' python3 jump_cli.py --help
```

### 方式二：安装命令包装器

项目没有把源码发布成 PyPI 包。建议使用仓库内脚本，或者在个人目录建立链接：

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$(pwd)/jssh" "$HOME/.local/bin/jssh"
ln -sfn "$(pwd)/jexec" "$HOME/.local/bin/jexec"
ln -sfn "$(pwd)/jscp" "$HOME/.local/bin/jscp"
```

确认 `"$HOME/.local/bin"` 在 `PATH` 中，然后可以直接使用 `jssh`、`jexec` 和 `jscp`。

## 配置

推荐将不敏感的连接配置保存到用户配置文件。配置文件默认位置为 `~/.config/jumpserver-cli/config.json`，由 CLI 创建并设置为仅当前用户可读写：

```bash
python3 jump_cli.py config set \\
  --base-url 'https://jumpserver.example.com' \\
  --org-id 'your-org-id'
python3 jump_cli.py config show
```

配置文件只保存 JumpServer 地址和组织 ID，不保存 Cookie、Access Key、Secret 或临时 Token。也可以只配置地址：

```bash
python3 jump_cli.py config set --base-url 'https://jumpserver.example.com'
```

配置优先级从高到低为：命令行 `--base-url`、环境变量 `JMS_BASE_URL`、配置文件、示例默认地址。一次性覆盖配置：

```bash
JMS_BASE_URL='https://jumpserver.example.com' python3 jump_cli.py status
python3 jump_cli.py --base-url 'https://jumpserver.example.com' status
```

如果需要把配置文件放到其他位置，可设置 `JMS_CONFIG_FILE`：

```bash
export JMS_CONFIG_FILE="$HOME/.config/jumpserver-cli/company.json"
```

常用环境变量：

| 变量 | 用途 |
| --- | --- |
| `JMS_BASE_URL` | JumpServer 根地址，默认是示例地址，必须替换 |
| `JMS_ORG_ID` | JumpServer 组织 ID |
| `JMS_CONFIG_FILE` | 自定义非敏感配置文件路径 |
| `JMS_ACCESS_KEY_ID` | 临时使用的 Access Key ID |
| `JMS_ACCESS_KEY_SECRET` | 临时使用的 Access Key Secret |
| `JMS_TOKEN_CACHE_TTL` | 临时 SSH Token 缓存秒数，默认 600 |
| `JMS_TOKEN_REFRESH_COOLDOWN` | `client-url` 刷新冷却秒数，默认 30 |
| `JMS_TOKEN_CACHE_DISABLE` | 设置为 `1` 禁用 Token 缓存 |

不要把 Access Key Secret、Cookie 或 Token 写入仓库、Shell 脚本、工单或聊天记录。

### 组织 ID

组织 ID 会作为 `X-JMS-ORG` 发送给 JumpServer。当前 CLI 不会从 AK/SK 自动发现组织，会使用环境变量、配置文件或标准默认值。单组织的标准安装通常可以直接使用默认值：

```text
00000000-0000-0000-0000-000000000002
```

如果账号属于多个组织，登录 JumpServer 网页后打开浏览器开发者工具的 Network 面板，查看任意 `/api/v1/` 请求的 Request Headers，找到当前组织对应的 `X-JMS-ORG` 值，再保存：

```bash
./jump_cli.py config set --org-id '从请求头复制的组织ID'
```

## 认证

优先使用 JumpServer Access Key / Secret。它不依赖浏览器状态，也更适合日常 CLI 使用。

### Access Key / Secret

临时环境变量方式：

```bash
export JMS_ACCESS_KEY_ID='your-access-key-id'
export JMS_ACCESS_KEY_SECRET='your-access-key-secret'
export JMS_ORG_ID='your-org-id'
python3 jump_cli.py status --probe --probe-search 127.0.0.1
```

或者将凭据保存到本机权限为 `600` 的缓存文件。Secret 会被交互式隐藏输入：

```bash
python3 jump_cli.py login-aksk --key-id 'your-access-key-id' --org-id 'your-org-id'
```

默认缓存目录为 `~/.cache/jumpserver-cli`。其中的 `credentials.json` 是敏感文件，只应保留在个人机器上。

### 浏览器 Cookie

1. 打开 `tampermonkey-jumpserver-session.user.js`，将顶部的 `@match` 改成公司 JumpServer 域名。
2. 在浏览器安装并启用脚本，登录 JumpServer。
3. 点击页面右下角的 `Copy JMS Session`。
4. 将复制的 JSON 通过标准输入交给 CLI：

```bash
python3 jump_cli.py login
```

也可以从文件读取，但文件必须是临时文件，使用后立即删除并确保不会进入 Git：

```bash
python3 jump_cli.py login --cookie-file /path/to/session.json
```

该脚本申请 `GM_cookie` 权限以读取 HttpOnly 会话 Cookie。只应在可信浏览器配置中安装，使用完毕后可以禁用或删除。

## 验证认证

不打印凭据地查看本地状态：

```bash
python3 jump_cli.py status
```

调用 JumpServer API 做实际验证。`--probe-search` 应使用一个你有权限访问的、尽可能具体的资产 IP 或主机名：

```bash
python3 jump_cli.py status --probe --probe-search 127.0.0.1
```

看到 `auth_mode` 和探测成功信息后，再进行资产解析。

## 常用命令

### TUI 交互模式

无参数启动会进入全屏资产控制台；也可以显式使用 `tui`：

```bash
./jump_cli.py
./jump_cli.py tui
```

启动时会先验证认证并加载资产树。界面左侧是资产列表，右侧显示当前资产详情和近期会话。近期会话只保存在本机，按连接次数降序、最后使用时间降序排列。

资产、系统用户和近期会话面板支持鼠标点击选中，鼠标滚轮可以移动当前面板；选中后按 `Enter` 进入下一步或启动 SSH。

首次使用时，TUI 会先引导确认真实的 JumpServer 地址，再进入 AK/SK 或浏览器 Cookie 认证流程。项目中的 `jumpserver.example.com` 只是占位地址：

```bash
./jump_cli.py
```

如果是在脚本或非交互终端中运行，请提前配置地址：

```bash
./jump_cli.py config set --base-url 'https://your-jumpserver.example.com'
```

| 按键 | 操作 |
| --- | --- |
| `Up` / `Down` | 移动当前列表；搜索时也立即导航 |
| `j` / `k` | 在非资产面板中移动列表；资产面板中直接作为搜索字符 |
| `Tab` | 在资产列表和近期会话之间切换焦点 |
| 任意可打印字符 | 资产焦点下直接开始 IP/Hostname 检索 |
| `Enter` | 直接连接当前 focus 的资产/系统用户/历史会话 |
| `Esc` | 退出检索、返回资产列表或退出 TUI |
| `r` | 重新加载资产树 |
| `Ctrl-U` | 清空检索词 |
| `Ctrl-C` | 退出 TUI |

历史文件为 `~/.local/state/jumpserver-cli/history.json`，只包含资产和系统用户标识、显示名称、连接次数和时间戳，不包含 Cookie、密码、Token 或 SSH 命令。

解析资产和系统用户：

```bash
python3 jump_cli.py resolve 10.0.0.10
python3 jump_cli.py resolve app.example.internal --system-user ops
```

生成并显示临时连接信息。默认不显示临时密码：

```bash
python3 jump_cli.py token 10.0.0.10
python3 jump_cli.py token 10.0.0.10 --show-password  # 仅限本地调试
```

进入交互式会话：

```bash
./jssh 10.0.0.10
```

传递 SSH 选项：

```bash
./jssh -o ServerAliveInterval=30 10.0.0.10
```

执行远程命令。命令在 JumpServer 提供的 PTY shell 中运行：

```bash
./jexec 10.0.0.10 -- 'hostname && uptime'
```

需要 sudo 时必须明确指定：

```bash
./jexec --sudo 10.0.0.10 -- 'id && systemctl status nginx'
```

高风险、破坏性或服务影响操作还需要 `--yes`。确认前请核对目标资产、完整命令和影响范围：

```bash
./jexec --sudo --yes 10.0.0.10 -- 'systemctl restart nginx'
```

## 安全注意事项

- 这是有权限的运维工具。命令会以当前 JumpServer 系统用户权限访问目标资产。
- 不要使用 `--show-password` 或 `--print-command` 的输出作为日志、截图或文档内容。
- 不要把 `~/.cache/jumpserver-cli/` 下的任何文件分享给他人。
- `tokens.json` 中包含临时 SSH 凭据，即使有短 TTL 也应按 Secret 处理。
- 不要把真实 Cookie、Access Key、内网域名、内网 IP 或资产 ID 写入 README 和提交记录。
- `--yes` 不是权限提升，它只是确认用户已明确接受高风险操作。
- 认证失效或怀疑泄露时，立即注销浏览器会话、删除本地缓存，并在 JumpServer 侧轮换凭据。

清理本机缓存：

```bash
rm -rf "$HOME/.cache/jumpserver-cli"
```

只对上述明确目录执行清理，不要将命令中的目标替换为更宽泛的路径。

## 故障排查

### `cached session is expired` 或 HTTP 401/403

重新执行 `login-aksk`，或重新从浏览器复制 Cookie。确认 Access Key 具备资产查询和连接 Token 所需权限。

### 找不到资产

使用完整 IP 或主机名，确认当前账号对该资产有授权，并检查 `JMS_BASE_URL` 是否指向正确的 JumpServer 实例。

### `sshpass is required`

安装操作系统对应的 `sshpass` 包。只执行 `resolve` 或 `token` 时不需要它，但建立 SSH 会话需要。

### SSH 返回 255

CLI 会尝试使缓存 Token 失效并刷新一次。仍然失败时，先执行 `status --probe`，再检查 JumpServer 网关、目标资产网络和系统用户授权。

### `No PTY requested`

JumpServer 网关可能拒绝标准 SCP。优先使用 `jssh` 或 `jexec`；`jscp` 当前属于实验性能力，不应作为稳定文件传输方案。

### Cookie 中没有 `jms_sessionid`

浏览器脚本没有读取 HttpOnly Cookie。确认 Tampermonkey 已授予 `GM_cookie` 权限，或改用 Access Key / Secret。

## 开发和自检

源码入口为 `src/jumpserver_cli/cli.py`，根目录的 `jump_cli.py` 和三个包装脚本负责直接运行源码。

提交前执行：

```bash
python3 -m py_compile jump_cli.py jssh jexec jscp src/jumpserver_cli/*.py
python3 jump_cli.py --help
JMS_BASE_URL='https://jumpserver.example.com' python3 jump_cli.py status
```

不要在没有明确授权的情况下对生产 JumpServer 或资产执行探测、连接和远程命令。
