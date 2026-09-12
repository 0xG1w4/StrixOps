"""Manage a source-checkout Console without importing its runtime dependencies."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from strixops_activity import activity_blockers

ROOT = Path(__file__).resolve().parents[1]
GENERATED = (
    ".venv",
    "console/web/node_modules",
    "console/web/.next",
    "console/web/.next-dev",
    "console/web/out",
)
PATH_ENV = {
    "console_config": "STRIXOPS_CONSOLE_CONFIG",
    "projects_file": "STRIXOPS_PROJECTS_FILE",
    "project_reports": "STRIXOPS_PROJECT_REPORTS_DIR",
    "queue_db": "STRIXOPS_QUEUE_DB",
    "mcp_root": "STRIXOPS_MCP_ROOT",
    "fofa_root": "STRIXOPS_FOFA_ROOT",
}


class ManagerError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ManagerError(f"无法安全读取管理记录：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, OSError) as exc:
        raise ManagerError(f"管理记录损坏或无法读取：{path}") from exc


def write_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def source_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if not match:
        raise ManagerError("无法读取项目版本。")
    return match.group(1)


def frontend_fingerprint(root: Path) -> str:
    web = root / "console/web"
    paths = [
        web / name
        for name in (
            "package.json",
            "package-lock.json",
            "next.config.ts",
            "postcss.config.mjs",
            "tsconfig.json",
        )
    ]
    paths += sorted(path for path in (web / "src").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def process_helpers(root: Path):
    spec = importlib.util.spec_from_file_location(
        "strixops_manager_processes", root / "src/strixops/queue/processes.py"
    )
    if spec is None or spec.loader is None:
        raise ManagerError("缺少程序识别模块，请使用完整的源码目录。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def absolute(value: str, root: Path) -> str:
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else root / path).resolve())


class Manager:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.directory = self.root / ".strixops/manager"
        if (self.root / ".strixops").is_symlink() or self.directory.is_symlink():
            raise ManagerError("管理目录不能是符号链接。")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.record_path = self.directory / "console.json"
        self.config_path = self.directory / "config.json"
        self.install_path = self.directory / "install.json"
        self.log_path = self.directory / "console.log"
        self.processes = process_helpers(self.root)
        self.version = source_version(self.root)

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.directory / "manager.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ManagerError("另一个管理操作正在进行，请稍后重试。") from exc
            yield
        finally:
            os.close(fd)

    def config(self, args=None) -> dict:
        saved = read_json(self.config_path)
        config = {
            "host": saved.get("host", "127.0.0.1"),
            "port": saved.get("port", 8300),
            "runs_root": saved.get("runs_root")
            or absolute(os.environ.get("STRIX_RUNS") or "strix_runs", self.root),
        }
        for name in ("host", "port", "runs_root"):
            value = getattr(args, name, None)
            if value is not None:
                config[name] = absolute(value, self.root) if name == "runs_root" else value
        if config["host"] != "localhost":
            try:
                ipaddress.ip_address(config["host"])
            except ValueError as exc:
                raise ManagerError("--host 请使用 IP 地址或 localhost。") from exc
        if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
            raise ManagerError("--port 必须是 1–65535 的整数。")
        previous = saved.get("paths", {})
        console = previous.get("console_config") or absolute(
            os.environ.get("STRIXOPS_CONSOLE_CONFIG") or "~/.strixops/console.json", self.root
        )
        parent = Path(console).parent
        defaults = {
            "console_config": console,
            "projects_file": str(Path.home() / ".strixops/projects.json"),
            "project_reports": str(Path.home() / ".strixops/project_reports"),
            "queue_db": str(parent / "scan_queue.sqlite3"),
            "fofa_root": str(parent / "fofa"),
            "mcp_root": str(Path.home() / ".strixops/mcp_tasks"),
        }
        config["paths"] = {
            key: previous.get(key) or absolute(os.environ.get(env) or defaults[key], self.root)
            for key, env in PATH_ENV.items()
        }
        config["paths"]["runs_root"] = config["runs_root"]
        return config

    @staticmethod
    def url(config: dict) -> str:
        host = config["host"]
        host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else "::1" if host == "::" else host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{config['port']}"

    def health(self, config: dict) -> dict | None:
        try:
            with build_opener(ProxyHandler({})).open(self.url(config) + "/api/health", timeout=2) as response:
                data = json.loads(response.read(65537))
            if not isinstance(data, dict) or data.get("product") != "StrixOps" or data.get("ok") is not True:
                return None
            return data
        except (OSError, ValueError, URLError):
            return None

    def port_available(self, config: dict) -> bool:
        host = config["host"]
        if host == "localhost":
            host = "127.0.0.1"
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, config["port"]))
                return True
            except OSError:
                return False

    def record(self) -> dict:
        record = read_json(self.record_path)
        if record and (record.get("root") != str(self.root) or not isinstance(record.get("config"), dict)):
            raise ManagerError("程序记录不属于当前项目，拒绝操作。")
        return record

    def owned_alive(self, record: dict) -> bool:
        if not record:
            return False
        pid, identity = record.get("pid"), record.get("identity")
        if type(pid) is not int or pid <= 1 or not isinstance(identity, str) or not identity:
            raise ManagerError("程序记录缺少可靠身份信息，拒绝发送信号。")
        alive = self.processes.process_alive(pid, identity)
        if alive is None:
            raise ManagerError("无法确认 Console 程序身份，保留程序与记录。")
        if not alive:
            return False
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=", "-o", "command="], capture_output=True, text=True, timeout=3
        )
        try:
            uid, command = result.stdout.strip().split(None, 1)
            entry = str(self.root / ".venv/bin/strixops-console")
            argv = record["argv"]
            if not isinstance(argv, list) or not argv or argv[0] != entry:
                raise ValueError
            # ps displays argv without shell quoting. Compare the complete
            # recorded command suffix so paths containing spaces stay intact.
            expected = " ".join(argv)
            prefix = command[: -len(expected)].rstrip() if command.endswith(expected) else None
            interpreter = prefix == "" or bool(
                prefix and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(prefix).name)
            )
            valid = result.returncode == 0 and int(uid) == os.getuid() and prefix is not None and interpreter
        except (ValueError, KeyError):
            valid = False
        if not valid:
            raise ManagerError("PID 对应的命令或用户已变化，拒绝操作该程序。")
        return True

    def ensure_idle(self, config: dict) -> None:
        blockers = activity_blockers(config["paths"], health=self.health(config))
        if blockers:
            raise ManagerError(
                "仍有工作执行中或状态无法确认，请先在 Console 处理：\n- " + "\n- ".join(blockers)
            )

    def ensure_port_free(self, config: dict) -> None:
        if not self.port_available(config):
            health = self.health(config)
            detail = (
                f"检测到现有 Console v{health.get('version', 'unknown')}"
                if health
                else "端口已被其他程序占用"
            )
            raise ManagerError(
                f"{detail}（{config['port']}）。请先用原启动方式停止该服务；脚本不会接管或终止未登记的程序。"
            )

    def installation_info(self, *, allow_missing: bool = False) -> dict:
        python = self.root / ".venv/bin/python"
        if not python.is_file():
            if allow_missing:
                return {}
            raise ManagerError("尚未安装 Python 环境，请先执行 ./strixops.sh install。")
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import json,sys,importlib.metadata as md\n"
                "try: dist=md.distribution('strixops')\n"
                "except md.PackageNotFoundError: print(json.dumps({'missing':True})); sys.exit(0)\n"
                "import strixops\n"
                "print(json.dumps({'version':strixops.__version__,'source':strixops.__file__,"
                "'python':list(sys.version_info[:2]),"
                "'direct_url':json.loads(dist.read_text('direct_url.json') or '{}')}))",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            info = json.loads(result.stdout)
            if result.returncode == 0 and info.get("missing") and allow_missing:
                return {}
            from urllib.parse import unquote, urlsplit

            direct = info["direct_url"]
            url = urlsplit(direct.get("url", ""))
            editable = (
                Path(info["source"]).resolve() == self.root / "src/strixops/__init__.py"
                and direct.get("dir_info", {}).get("editable") is True
                and url.scheme == "file"
                and url.netloc in {"", "localhost"}
                and Path(unquote(url.path)).resolve() == self.root
            )
            if result.returncode or not editable:
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise ManagerError(
                "现有 .venv 未以 editable 方式指向本项目；为保留其中的 prompt/skill，脚本不会覆盖或删除它。"
            ) from exc
        return info

    def verify_installation(self) -> None:
        info = self.installation_info()
        if info["version"] != self.version or tuple(info["python"]) < (3, 12):
            raise ManagerError("Python 环境与当前源码版本不一致，请执行 ./strixops.sh install。")
        web = self.root / "console/web"
        if (
            not (web / "out/index.html").is_file()
            or read_json(web / "package.json").get("version") != self.version
        ):
            raise ManagerError("前端尚未建置或版本不一致，请执行 ./strixops.sh install。")
        marker = read_json(self.install_path)
        if not marker or (
            marker.get("root") != str(self.root)
            or marker.get("version") != self.version
            or not marker.get("complete")
            or marker.get("frontend_fingerprint") != frontend_fingerprint(self.root)
        ):
            raise ManagerError("前端来源或安装记录已变化，请执行 ./strixops.sh install 完成建置后再启动。")

    def start(self, config: dict) -> None:
        record = self.record()
        if self.owned_alive(record):
            if record["config"] != config:
                raise ManagerError("Console 已执行中；修改监听设置请使用 restart。")
            health = self.health(config)
            if not health or health.get("version") != self.version:
                raise ManagerError("Console 已执行但尚未就绪或版本不同，请检查 status/logs 后使用 restart。")
            print(f"Console 已执行：{self.url(config)} · v{health['version']} · PID {record['pid']}")
            return
        self.verify_installation()
        self.ensure_port_free(config)
        argv = [
            str(self.root / ".venv/bin/strixops-console"),
            "--host",
            config["host"],
            "--port",
            str(config["port"]),
            "--runs-root",
            config["runs_root"],
        ]
        env = os.environ.copy()
        env.update({name: config["paths"][key] for key, name in PATH_ENV.items()})
        env["STRIX_RUNS"] = config["runs_root"]
        fd = os.open(self.log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab", buffering=0) as log:
            process = subprocess.Popen(
                argv,
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        identity = self.processes.process_identity(process.pid)
        if not identity:
            # The child is still represented by this Popen, so no PID-file trust is involved.
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
            raise ManagerError("无法记录新程序身份，启动已中止。请检查 logs。")
        record = {
            "schema_version": 1,
            "root": str(self.root),
            "pid": process.pid,
            "identity": identity,
            "argv": argv,
            "config": config,
            "version": self.version,
            "started_at": time.time(),
        }
        write_json(self.record_path, record)
        write_json(self.config_path, config)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.record_path.unlink(missing_ok=True)
                raise ManagerError(
                    f"Console 启动失败（exit {process.returncode}），请执行 ./strixops.sh logs。"
                )
            health = self.health(config)
            if (
                health
                and health.get("version") == self.version
                and absolute(health.get("runs_root", ""), self.root) == config["runs_root"]
            ):
                print(
                    f"Console 已启动：{self.url(config)} · v{self.version} · PID {process.pid}\n"
                    f"日志：{self.log_path}"
                )
                return
            time.sleep(0.2)
        raise ManagerError("Console 尚未通过健康检查，程序记录已保留；请检查 status/logs，不会重复启动。")

    def stop(self, timeout: float = 30) -> None:
        record = self.record()
        if not self.owned_alive(record):
            self.ensure_port_free(self.config())
            self.record_path.unlink(missing_ok=True)
            print("Console 已停止。")
            return
        self.ensure_idle(record["config"])
        # Recheck birth identity and command immediately before signalling this PID only.
        if self.owned_alive(record):
            os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.owned_alive(record):
                self.record_path.unlink(missing_ok=True)
                print("Console 已平顺停止；设置与任务数据保留。")
                return
            time.sleep(0.2)
        raise ManagerError("Console 仍在结束请求，未强制终止。请检查 logs，稍后再执行 stop。")

    def status(self, json_output: bool = False) -> int:
        record = self.record()
        managed = self.owned_alive(record)
        config = record["config"] if managed else self.config()
        health = self.health(config)
        result = {
            "state": "running"
            if managed and health
            else "starting_or_unhealthy"
            if managed
            else "unmanaged"
            if health
            else "stopped"
            if self.port_available(config)
            else "port_in_use",
            "managed": managed,
            "pid": record.get("pid") if managed else None,
            "version": health.get("version") if health else None,
            "source_version": self.version,
            "url": self.url(config),
            "runs_root": config["runs_root"],
            "log": str(self.log_path),
        }
        if json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Console: {result['state']}\n地址：{result['url']}\n"
                f"运行版本：{result['version'] or '—'} · 源码版本：{self.version}\n"
                f"PID：{result['pid'] or '—'}\n任务目录：{config['runs_root']}\n日志：{self.log_path}"
            )
            if health and health.get("version") != self.version:
                print("版本不一致：请完成 install 后 restart，并重新载入浏览器。")
            if health and not managed:
                print("这是未由脚本登记的服务，请先使用原启动方式停止，再执行 start。")
        return 0 if managed and health and health.get("version") == self.version else 3

    def generated_path(self, relative: str, config: dict) -> Path:
        if relative not in GENERATED:
            raise ManagerError("安装记录包含未知路径，拒绝删除。")
        path = self.root / relative
        if path.is_symlink() or path.resolve() != path:
            raise ManagerError(f"执行环境包含符号链接，保留：{path}")
        for value in config["paths"].values():
            protected = Path(value).resolve()
            if protected == path or path in protected.parents or protected in path.parents:
                raise ManagerError(f"数据目录与执行环境重叠，保留：{path}")
        return path

    def install(self, build_images: bool = False) -> None:
        for command in ("uv", "node", "npm"):
            if shutil.which(command) is None:
                raise ManagerError(f"缺少 {command}，请安装后重试。脚本不会修改系统套件。")
        result = subprocess.run(
            ["node", "-p", "process.versions.node"], capture_output=True, text=True, timeout=5, check=True
        )
        if tuple(int(part) for part in result.stdout.strip().split(".")[:2]) < (20, 9):
            raise ManagerError("需要 Node.js 20.9 或更新版本。")
        if build_images:
            if shutil.which("docker") is None:
                raise ManagerError("--build-images 需要 Docker。")
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=True,
            )
        config = self.config()
        for relative in GENERATED:
            self.generated_path(relative, config)
        repair_venv = False
        if (self.root / ".venv").exists():
            previous_install = read_json(self.install_path)
            repair = (
                previous_install.get("root") == str(self.root)
                and previous_install.get("complete") is False
                and ".venv" in previous_install.get("owned", [])
            )
            info = self.installation_info(allow_missing=repair)
            repair_venv = repair and not info
        record = self.record()
        running = self.owned_alive(record)
        if running:
            config = record["config"]
        else:
            self.ensure_port_free(config)
        self.ensure_idle(config)
        if running:
            self.stop()
        if repair_venv:
            # This incomplete, manager-owned environment has no installed
            # StrixOps resources. Recreate it so a partial uv setup is retryable.
            shutil.rmtree(self.generated_path(".venv", config))
        # All listed locations are rebuildable, and any pre-existing venv was verified editable.
        write_json(
            self.install_path,
            {"schema_version": 1, "root": str(self.root), "complete": False, "owned": list(GENERATED)},
        )
        env = os.environ.copy()
        env["UV_PROJECT_ENVIRONMENT"] = str(self.root / ".venv")
        env["NEXT_TELEMETRY_DISABLED"] = "1"
        try:
            for command in (
                ["npm", "--prefix", "console/web", "ci"],
                ["npm", "--prefix", "console/web", "run", "build"],
                [
                    "uv",
                    "sync",
                    "--frozen",
                    "--no-dev",
                    "--reinstall-package",
                    "strixops",
                    "--project",
                    str(self.root),
                ],
            ):
                print("执行：" + shlex.join(command), flush=True)
                subprocess.run(command, cwd=self.root, env=env, check=True)
            if build_images:
                subprocess.run(["bash", "containers/build-images.sh"], cwd=self.root, env=env, check=True)
            write_json(
                self.install_path,
                {
                    "schema_version": 1,
                    "root": str(self.root),
                    "version": self.version,
                    "complete": True,
                    "owned": list(GENERATED),
                    "frontend_fingerprint": frontend_fingerprint(self.root),
                },
            )
            self.verify_installation()
        except (OSError, subprocess.SubprocessError, ManagerError) as exc:
            write_json(
                self.install_path,
                {"schema_version": 1, "root": str(self.root), "complete": False, "owned": list(GENERATED)},
            )
            raise ManagerError(
                "安装未完成。Console 保持停止，数据保留；处理上方错误后重新执行 install。"
            ) from exc
        print(f"安装完成：v{self.version}。设置、prompt/skill 与历史任务均保留。")
        if running:
            self.start(config)
        else:
            print("执行 ./strixops.sh start 启动。")
        if not build_images:
            print("真实测试另需可用 Docker 与沙箱映像；首次部署可执行 ./strixops.sh install --build-images。")

    def uninstall(self) -> None:
        marker = read_json(self.install_path)
        if marker.get("root") != str(self.root) or not isinstance(marker.get("owned"), list):
            raise ManagerError("没有本脚本的安装记录。为保留手动安装与编辑的资源，不删除现有环境。")
        record = self.record()
        config = record["config"] if self.owned_alive(record) else self.config()
        paths = [self.generated_path(relative, config) for relative in marker["owned"]]
        if (self.root / ".venv").exists() and marker.get("complete"):
            self.installation_info()
        self.ensure_idle(config)
        self.stop()
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                raise ManagerError(f"预期执行环境目录，但遇到文件，已保留：{path}")
        self.install_path.unlink(missing_ok=True)
        print(
            "已卸载脚本管理的依赖与建置产物。"
            "原始码、prompt/skill、设置、憑证、扫描结果、日志及 Docker 映像均保留。"
        )

    def logs(self, lines: int, follow: bool) -> None:
        if not self.log_path.exists():
            print("尚无 Console 日志。")
            return
        if self.log_path.is_symlink():
            raise ManagerError("日志路径不能是符号链接。")
        # POSIX tail bounds memory even when the Console log is large.
        subprocess.run(
            ["tail", "-n", str(lines), *(["-f"] if follow else []), str(self.log_path)], check=True
        )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="strixops.sh", description="StrixOps 管理器：安装与管理本源码目录的 Console，保留任务数据。"
    )
    commands = cli.add_subparsers(dest="command")
    install = commands.add_parser("install", help="安装/更新前端与 Python 环境；原服务若在执行，完成后恢复")
    install.add_argument("--build-images", action="store_true", help="同时建置 Docker 沙箱映像")
    commands.add_parser("uninstall", help="删除脚本管理的环境，保留源码、设置、憑证与任务数据")
    for name in ("start", "restart"):
        command = commands.add_parser(
            name, help="后台启动 Console" if name == "start" else "沿用原配置重新启动 Console"
        )
        command.add_argument("--host", help="监听 IP；默认 127.0.0.1")
        command.add_argument("--port", type=int, help="监听端口；默认 8300")
        command.add_argument("--runs-root", help="任务目录；首次默认项目内 strix_runs")
        if name == "restart":
            command.add_argument("--timeout", type=float, default=30, help="平顺停止等待秒数，默认 30")
    stop = commands.add_parser("stop", help="平顺停止登记的 Console")
    stop.add_argument("--timeout", type=float, default=30, help="停止等待秒数，默认 30；不强制终止")
    commands.add_parser("status", help="查看运行/源码版本、PID、地址与目录").add_argument(
        "--json", action="store_true"
    )
    logs = commands.add_parser("logs", help="读取 Console 日志")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.add_argument("-f", "--follow", action="store_true")
    return cli


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if not args.command:
        cli.print_help()
        return 0
    if not 0 < getattr(args, "timeout", 30) <= 300 or not 1 <= getattr(args, "lines", 100) <= 10000:
        cli.error("timeout 需介于 0–300 秒，日志行数需介于 1–10000。")
    try:
        manager = Manager()
        if args.command == "logs":
            manager.logs(args.lines, args.follow)
            return 0
        with manager.lock():
            if args.command == "status":
                return manager.status(args.json)
            if args.command == "install":
                manager.install(args.build_images)
            elif args.command == "uninstall":
                manager.uninstall()
            elif args.command == "stop":
                manager.stop(args.timeout)
            elif args.command == "start":
                manager.start(manager.config(args))
            elif args.command == "restart":
                config = manager.config(args)
                manager.verify_installation()
                manager.stop(args.timeout)
                manager.start(config)
        return 0
    except KeyboardInterrupt:
        print("\n管理操作已中断；未强制终止其他程序。", file=sys.stderr)
        return 130
    except (ManagerError, OSError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
