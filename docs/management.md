# StrixOps 管理脚本

在源码目录中使用 `./strixops.sh` 管理 Console。支持 Linux 和 macOS，
不需要 sudo；无参数或 `--help` 显示用法。脚本不会安装系统软件或创建开机服务。
已有 systemd／launchd 部署继续使用原服务管理器，避免两种管理方式同时启动。

## 安装与启动

管理脚本需要可用的 Python 3.9+。StrixOps 运行环境需要 Python 3.12+；
安装时由 uv 根据项目要求选择解释器。另需 uv、Node.js 20.9+ 与 npm。
管理脚本优先使用本项目 `.venv/bin/python`，让维护检查与 Console 使用同一套
Python／SQLite。项目环境尚未安装或无法运行时，才依次查找 PATH 中的 Python
与 uv 已安装的解释器。
脚本不修改系统套件。真实扫描需要可用 Docker 与对应沙箱镜像。

```bash
./strixops.sh install
./strixops.sh start
./strixops.sh status
```

首次部署需要构建沙箱时，改用 `./strixops.sh install --build-images`。
这调用已有沙箱构建脚本；MCP 代理镜像仍按 [MCP 指南](mcp-traffic-workbench.md) 管理。

`install` 依次执行 `npm ci`、前端 build 和 `uv sync --frozen --no-dev`，
固定使用本项目 `.venv`。它不拉取 Git、不改变分支、不覆盖源码中的 prompt/skill。
若本脚本登记的 Console 正在运行且工作已结束，会先停止、安装，成功后恢复服务。
安装失败时保持停止并保留数据，修正错误后可再次执行 `install`。

`start` 在后台启动，默认 `127.0.0.1:8300`，任务目录为本项目 `strix_runs`。
它检查安装状态并等待健康检查与源码版本一致；不会暗中同步依赖或重建前端。
重复启动不会创建第二个服务。使用本脚本安装后，前端源文件变化会提示重新安装／构建。

```bash
./strixops.sh start --host 0.0.0.0 --port 8300 --runs-root /absolute/path/to/runs
./strixops.sh restart
```

监听地址、端口与绝对数据路径会保存，后续不需重复输入。首次运行会读取现有
`STRIX_RUNS`、`STRIXOPS_CONSOLE_CONFIG`、`STRIXOPS_PROJECTS_FILE`、
`STRIXOPS_PROJECT_REPORTS_DIR`、`STRIXOPS_QUEUE_DB`、`STRIXOPS_MCP_ROOT`、
`STRIXOPS_FOFA_ROOT`、`STRIXOPS_AUTH_DB`，后续保留这些路径。模型凭证仍从原 Console 设置读取。Console 登录账号与密码也会保留。
其他运行环境变量继承调用脚本的终端；脚本不会 source `.env` 或把凭证复制到管理记录。
如果要调整已保存的数据路径，请停止服务后修改 `.strixops/manager/config.json`；
任务目录也可通过 `restart --runs-root ...` 修改。

## 停止、重启与日志

```bash
./strixops.sh stop
./strixops.sh restart
./strixops.sh logs -n 200
./strixops.sh logs -f
./strixops.sh status --json
```

停止或更新前，会以只读方式检查任务、队列、MCP 测试／捕获和 FOFA 搜索。
执行中、等待中、受阻或无法确认状态的工作需要先在 Console 处理。
这些检查是维护前的状态快照；执行维护时请勿同时提交新任务。

主机重启或异常断电后，MCP 数据库可能仍保留 `capturing`／`running`，
即使对应容器已停止或不存在。若遇到「启动要求先安装，但安装要求先进入
Console 结束旧捕获」的循环，可在 Console 已停止且 Docker 可用时执行：

```bash
./strixops.sh install
./strixops.sh start
```

普通 `install` 会自动使用现有项目 `.venv` 中的 Docker SDK 核对保存的容器身份，并检查同一
任务的其他容器。确认容器不存在，或已退出且不会自动重启后，才允许此次安装。
旧版 `--recover-stale-captures` 选项继续兼容，但无需额外指定。
检查不会修改数据库、删除证据或操作容器；启动后仍可在 Console 停止／结束旧任务。
Docker 无法连接、容器身份缺失或不符、容器仍在运行或可能自动重启时仍会阻挡，
执行中的 MCP 请求测试及尚未完成的启动／停止／删除操作也仍需处理。
现有 Python 环境或 Docker SDK 缺失时也无法完成此核验。
执行时须使用原 Console 的 Docker 连接设置，包括原本使用的 `DOCKER_HOST`／TLS
等环境变量；旧记录没有保存 Docker daemon 身份，无法自动确认是否换了连接目标。
CLI 的 `docker ps` 可能使用不同的 context，不能单凭空列表跳过检查。
Console 仍在运行时不会套用此复原判断；`stop`、`uninstall` 保留原来的严格检查。

若看到 `MCP tasks: activity is unknown; stored state is unreadable or invalid.`，
表示检查无法读取或辨识 MCP 保存的状态，不能仅凭此讯息断定任务仍在运行或资料损坏。
旧版脚本优先使用全局 Python；其 SQLite 库可能无法只读打开 WAL 模式数据库，
即使项目 `.venv` 可以正常读取。更新 `strixops.sh` 后会优先使用项目环境；
尚未更新脚本时，可在源码目录执行 `.venv/bin/python scripts/strixops_manager.py stop`，
仍会完成相同的任务检查再停止服务。若项目解释器也报告相同错误，应继续检查保存路径、
文件权限、数据库与任务状态。

停止只向登记且出生时间与命令均匹配的 Console PID 发送 SIGTERM，
不会按名称／端口批量终止，也不会终止独立扫描进程或清理 Docker。
默认等待 30 秒；可用 `stop --timeout 60` 或 `restart --timeout 60` 延长至最多 300 秒。
超时不会自动 SIGKILL，记录保留，可检查日志后重试。

启动与 status 会分别显示监听地址和本机网址；监听 `0.0.0.0` 时本机网址仍可用 `127.0.0.1`。

日志为 `.strixops/manager/console.log`；配置、安装清单和 PID 记录也在该目录，
均属于忽略的本机文件，不会推送 Git。日志不自动删除，可按部署需求轮转。
`status` 就绪且版本一致时退出码为 0，其他运行状态为 3；管理操作失败为 1。

## 从手动启动迁移

1. 等待现有任务完成，在原终端 Ctrl+C，或通过原 systemd／launchd 服务停止。
2. 若原服务使用自订数据路径，在第一次调用脚本时提供相同环境变量与 `--runs-root`。
3. 执行 `./strixops.sh install`，再执行 `./strixops.sh start`。首次需建立完整安装与前端建置记录。
4. 使用 `./strixops.sh status` 确认版本与任务目录。

脚本会识别占用端口的现有 Console，但不会自动接管未登记程序，
也不会把未知程序的 PID 当作自己的服务停止。

## 卸载与数据保留

```bash
./strixops.sh uninstall
```

卸载要求存在本脚本的安装清单，工作已结束且 Console 可安全停止。
只移除清单中的 `.venv`、`console/web/node_modules`、`.next`、`.next-dev`、`out`。
符号链接、与数据路径重叠的目录、未知清单项目会拒绝处理。
现有 `.venv` 若不是指向本项目的 editable 安装，也不会覆盖或删除，
以免丢失 wheel 安装目录内编辑过的 prompt／skill。

以下内容始终保留：源码与 Git、prompt／skill、模型与项目设置、`.env`、
`strix_runs` 与自订任务目录、FOFA 历史、队列记录、MCP 捕获、CA／token／凭证、
报告、证据、管理配置与日志、Docker 镜像和容器。
如需再次使用，执行 `install` 后 `start`，继续使用保留的数据。
