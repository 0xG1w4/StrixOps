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

`stop` 会直接停止本项目登记的 Console、可确认归属的扫描引擎及相关容器，
不再要求任务状态为空闲。先发送 SIGTERM，等待超时后发送 SIGKILL；
保存状态中的 `running`、`capturing` 或无法读取的记录不会阻止 Console 停止。
已停止工作的有效记录会进行收尾，保留历史、报告与证据。管理脚本只停止残留容器，
不主动删除容器或镜像；引擎正常退出时仍可能完成其原有的容器清理。
无法确认归属或无法连接 Docker 的项目会在已执行的停止操作之后单独报告，
不会假称所有资源已经关闭，也不会按端口或容器名称批量终止其他服务。

`stop --force` 与普通 `stop` 相同。若只想在无工作时平顺停止 Console，使用
`stop --graceful`。`restart`、`install` 与 `uninstall` 仍先检查任务是否空闲；
需要中止工作后更新时，先执行 `stop`。这些维护检查是状态快照，期间请勿提交新任务。

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
Console 仍在运行时不会套用此复原判断；`uninstall` 与 `stop --graceful` 保留严格检查。

若看到 `MCP tasks: activity is unknown; stored state is unreadable or invalid.`，
表示检查无法读取或辨识 MCP 保存的状态，不能仅凭此讯息断定任务仍在运行或资料损坏。
旧版脚本优先使用全局 Python；其 SQLite 库可能无法只读打开 WAL 模式数据库，
即使项目 `.venv` 可以正常读取。更新 `strixops.sh` 后会优先使用项目环境；
新版普通 `stop` 不会以此状态检查阻止关闭。若在状态收尾时仍报告相同问题，
应检查保存路径、文件权限与数据库；无法读取的记录会保留，不会清空数据库来假装恢复。

停止前会核对程序的出生身份、用户与本项目命令；容器则核对保存的身份与归属标签。
`stop --timeout 60` 可调整各停止阶段的等待时间，默认 30 秒，最多 300 秒，
各阶段之后还包含强制退出确认时间。`restart --timeout 60` 则仍只调整平顺停止等待，
超时后保留程序与记录。

启动与 status 会分别显示监听地址和本机网址；监听 `0.0.0.0` 时本机网址仍可用 `127.0.0.1`。

日志为 `.strixops/manager/console.log`；配置、安装清单和 PID 记录也在该目录，
均属于忽略的本机文件，不会推送 Git。日志不自动删除，可按部署需求轮转。
`status` 就绪且版本一致时退出码为 0，其他运行状态为 3；管理操作失败为 1。

扫描任务的引擎日志另存于任务目录中的 `engine.log`。Root 完成测试后，先保存
可阅读的草稿，再整理证据、调用模型生成正式报告及清理执行环境。中文正式报告
统一使用简体中文。报告页在收尾期间仍可查看草稿，并显示当前阶段；引擎每 30 秒
回报收尾存活状态。只有模型输出成功保存后，才以正式报告取代草稿。
模型报告的两次尝试共用 `STRIXOPS_REPORT_SYNTHESIS_TIMEOUT` 秒总等待时间
（默认 900 秒），超时或模型失败时保留草稿，并明确提示正式报告未生成。
草稿下载内容也带有草稿标记。`run.json` 的 `report_synthesized=true` 表示模型正式报告
已保存；`false` 表示尚未生成，停用合成或 dry-run 也只保留草稿。
完成所有收尾后才标记整个任务完成；任务完成状态不代表正式报告已生成。
这些行为适用于更新后新启动的引擎；更新源码或环境变量不会改变已运行程序的等待设置。

任务停止或完成后，报告页的「生成报告」按钮可使用已保存的漏洞、内部发现、
root 结论与证据重新调用报告模型，不需要重跑扫描或启动沙箱。生成期间可离开页面，
回来后继续显示状态；同一任务的重复点击会沿用正在执行的生成工作。
新报告成功保存后才替换旧报告；失败保留原内容，并显示逾时、模型连接、
输入容量、输出不完整等可辨识原因。Console 重启中断的生成工作可以再次启动。
正在扫描或收尾的任务需先结束，避免扫描引擎与手动生成同时写入报告。

手动生成使用当前保存的模型设置：优先使用该任务原先选择的 profile，
若该 profile 已删除则使用当前启用的 profile；原始扫描设置与完成状态保持不变。
`STRIXOPS_REPORT_SYNTHESIS=0` 只停用自动生成，仍可通过按钮明确要求生成。
新任务的自动生成诊断保存在 `.state/report-synthesis.json`，
手动生成状态保存在 `.state/report-generation.json`；错误说明不包含模型密钥或请求正文。

报告内容采用 phase 报告的技术叙事：交代观察、验证、证据、取得的访问能力与影响，
按主题组织相关发现。正文使用连贯段落，表格整理主机、凭证和摘要，流程图按需要选用；
不再要求每个段落限制在五行以内。

报告来源按模型输入容量分配，并预留指令和输出空间。漏洞、内部发现与 root 草稿
不再固定截为每栏 6,000 字符：能放入的字段保留完整值；放不下的整个字段会在
`Source Coverage` 中明确记录，原始记录保持完整。所有 finding 的身份都会保留；
若连目标范围和 finding 身份都无法放入输入容量，则保留草稿并记录原因。
重试会移除补充资料，但不会再直接丢弃全部内部发现正文。

来源还会读取 finding 或 root 草稿引用、且已经保存成功的文字证据。重要引用优先
进入附件清单；大型文字日志可提供有关联的完整行及上下文，并标明来源行号与省略量。
PoC 脚本、JSON、CSV、凭证行与多行密钥不会被切成半段。读取最多 80 个引用附件，
单文件最多 4 MiB、总读取最多 32 MiB；超出限制、二进制或无法安全读取的附件
只记录省略原因，不会被当成扫描失败。模型不会自动读取整个工作区或完整会话历史。

任务目录 `.state/report-system-prompt.md` 保存本次报告指令，
`.state/report-source-1.md` 与可能存在的 `.state/report-source-2.md` 保存来源快照。
排查报告缺漏时，可以直接核对 `Referenced Evidence Content` 与 `Source Coverage`，
确认哪些完整字段、证据区段进入了输入，哪些资料被省略。

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
