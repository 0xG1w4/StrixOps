# StrixOps 管理脚本

在源码目录中使用 `./strixops.sh` 管理 Console。支持 Linux 和 macOS，
不需要 sudo；无参数或 `--help` 显示用法。脚本不会安装系统软件或创建开机服务。
已有 systemd／launchd 部署继续使用原服务管理器，避免两种管理方式同时启动。

## 安装与启动

管理脚本需要可用的 Python 3.9+。StrixOps 运行环境需要 Python 3.12+；
安装时由 uv 根据项目要求选择解释器。另需 uv、Node.js 20.9+ 与 npm。
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
`STRIXOPS_FOFA_ROOT`，后续保留这些路径。模型凭证仍从原 Console 设置读取。
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

停止只向登记且出生时间与命令均匹配的 Console PID 发送 SIGTERM，
不会按名称／端口批量终止，也不会终止独立扫描进程或清理 Docker。
默认等待 30 秒；可用 `stop --timeout 60` 或 `restart --timeout 60` 延长至最多 300 秒。
超时不会自动 SIGKILL，记录保留，可检查日志后重试。

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
