# Docker 部署 · Docker deployment

以下命令在项目根目录通过 POSIX shell 执行。准备可用的本地 Docker Engine 和支持 `up --wait` 的 Compose 插件、Python 3（用于生成本机密钥）及 curl。Windows/macOS 可安装 Docker Desktop，Linux 可安装 Docker Engine 与 Compose 插件。安装后检查 Context 的有效端点确为预期主机。 [Docker 安装](https://docs.docker.com/get-docker/) · [Compose 安装](https://docs.docker.com/compose/install/)

## 1. 本机 HTTPS 实例

从干净 shell 执行，避免将另一实例的 `FITSTUDIO_*` 或 `COMPOSE_*` 环境变量传入。Compose 优先使用 shell 里的同名值。以下示例取固定项目名与默认回环端口，出现冲突时请选择新项目名，并同步修改同一份环境文件中的端口设置。

```sh
docker context show
docker version
docker compose version
umask 077
python3 - <<'PY'
from pathlib import Path
from secrets import token_hex

template = Path(".env.example").read_text(encoding="utf-8")
assert template.count("FITSTUDIO_SECRET_KEY=\n") == 1
with Path(".env").open("x", encoding="utf-8") as env:
    env.write(template.replace("FITSTUDIO_SECRET_KEY=\n", f"FITSTUDIO_SECRET_KEY={token_hex(32)}\n"))
PY

dc() {
  docker compose -p fitstudio --env-file .env "$@"
}
dc config --quiet
dc up -d --build --wait
dc exec -T web python manage.py showmigrations studio
dc exec web python manage.py bootstrap_admin studio-owner
```

Web 启动自动迁移，成功后启动 Gunicorn。首次管理员命令需在交互式终端输入并确认强密码。每次实例使用独立密钥，环境文件应由实例维护者私下保管。

```sh
mkdir -p work
dc cp proxy:/data/caddy/pki/authorities/local/root.crt work/fitstudio-local-ca.crt
curl --fail --noproxy '*' --cacert work/fitstudio-local-ca.crt \
  --resolve fitstudio.localhost:8443:127.0.0.1 \
  https://fitstudio.localhost:8443/healthz/
dc exec -T web python -m config.check_health
```

健康端点返回 `{"status":"ok"}`。在浏览器访问 `https://fitstudio.localhost:8443/admin/`，使用专门信任本机根证书的测试浏览器或系统信任策略。上述 curl 验证只依赖单次指定的 CA，应先核对证书来源。会员入口是 `/portal/login/`。教练使用工作人员登录，在 `/admin/login/?next=/` 登录后可访问工作台。为教练创建普通工作人员账号并给所需权限，项目也提供 `configure_daily_coach --user-id <用户 ID>` 供预览日常业务权限，复核后加 --apply 才写入授权。

| 配置项 | 含义 |
| --- | --- |
| `FITSTUDIO_DOMAIN` | 裸主机名，本机默认为 `fitstudio.localhost`。 |
| `FITSTUDIO_SECRET_KEY` | 只在私有实例配置中生成与保存，生产设置要求至少 50 字符。 |
| `FITSTUDIO_HTTPS_PORT`、`FITSTUDIO_HTTPS_BIND` | 外部 HTTPS 端口和 Docker 绑定。修改端口时需同步。 |
| `FITSTUDIO_HTTP_BIND`、`FITSTUDIO_CADDYFILE` | HTTP 端口绑定和代理配置。本机配置默认关闭 HTTP 自动跳转。 |
| `FITSTUDIO_QUICK_COURSE_TYPE_ID` | 实例中启用的快捷购课课程 ID。 |
| `FITSTUDIO_BOOKING_MAX_DAYS`、`FITSTUDIO_BOOKING_COACH_ID`、`FITSTUDIO_BOOKING_COURSE_TYPE_ID` | 自主预约提前天数、在职教练 ID 及启用的 60 分钟、1 课时课程 ID。 |

在管理后台创建实际课程与教练后，将自己的 ID 写入私有环境文件，运行 `dc up -d --wait` 使设置生效。Web 使用 `studio_data` 中的 `/data/fitstudio.sqlite3`，Caddy 使用 `caddy_data` 和 `caddy_config`。更换项目名会更换默认卷。日常停止服务保留具名卷。

## 2. 公网 HTTPS 配置

已验收路径为本机独立实例。公网部署由站点维护者另行验证 DNS、防火墙、TLS、Host、日志和备份。保留同一实例的私有项目名与数据卷。将私有环境中 `FITSTUDIO_CADDYFILE` 设为 `./deploy/Caddyfile`、域名设为自己控制的裸域名、HTTPS 端口设为 `443`。确认宿主的 80/443 映射及允许范围后，再按自身网络策略配置绑定，如 `0.0.0.0:80` 和 `0.0.0.0:443`。Caddy 公网证书签发依赖该站点的域名与网络配置。对外只提供代理端口。

核对 `manage.py check --deploy`，根据真实站点评估 HSTS、外部日志采集与管理入口访问范围。当前应用使用 Django 会话和签名，不要求外部 API 密钥或独立数据库口令。保护 Docker 宿主、密钥、数据库和证书卷。将备份加密、限制访问、单独留存，并定期演练恢复。 [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https) · [Django 部署清单](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)

## 3. 一致性备份

在维护窗口执行。以下命令暂停写服务，使用 SQLite 备份 API 生成快照并检查完整性。成功后恢复服务。失败时保留原卷，确认诊断结果并按需运行 `dc up -d --wait` 恢复原服务。二进制重定向请使用 POSIX shell。截图和源码不包含备份。

```sh
umask 077
mkdir -p backups
dc stop proxy web
backup=$(mktemp backups/fitstudio-XXXXXXXX.sqlite3)
if ! dc run --rm --no-deps -T --entrypoint python web -c '
from contextlib import closing
from pathlib import Path
import shutil, sqlite3, sys, tempfile

with tempfile.TemporaryDirectory() as tmp:
    snap = Path(tmp) / "snapshot.sqlite3"
    with closing(sqlite3.connect("file:/data/fitstudio.sqlite3?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snap)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite integrity check failed")
    with snap.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
' > "$backup"; then
  rm -- "$backup"
  exit 1
fi
test -s "$backup" || exit 1
dc up -d --wait
```

单独安全备份私有环境配置及版本信息。SQLite 快照不包含 Caddy 私钥、证书卷或环境配置。若运维须保留本机 CA 或 TLS 状态，应在相关服务停止时按宿主平台对 Caddy 私有卷做受限备份。

## 4. 恢复到隔离实例

从同一代码版本开始。准备原实例私有环境的受保护副本，并为恢复实例单独设置回环绑定和配套 HTTPS 端口，例如 `127.0.0.1:18080`、`127.0.0.1:18443` 和 `FITSTUDIO_HTTPS_PORT=18443`，选用 `deploy/Caddyfile.local`。原配置内的课程和教练 ID 与恢复的数据库保持一致。为每次演练选择空白、唯一的项目名，必要时修改命令中的 `fitstudio-restore`。以下导入会在目标已有数据库或旁文件时停止，原实例继续使用自己的卷。

```sh
# Set RESTORE_ENV to an absolute path to the protected instance config copy
# with separate loopback ports before running this block.
test -r "$RESTORE_ENV" && test -s "$backup" || exit 1
restore_dc() {
  docker compose -p fitstudio-restore --env-file "$RESTORE_ENV" "$@"
}

restore_dc config --quiet
restore_dc build web
# Use a brand-new project and volume; do not run up before the import.
restore_dc run --rm --no-deps -T --entrypoint python web -c '
import os, shutil, sqlite3, sys, tempfile
from pathlib import Path

dest = Path("/data/fitstudio.sqlite3")
if any(Path(str(dest) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")):
    raise RuntimeError("Restore requires an empty data volume")
fd, staging = tempfile.mkstemp(prefix=".restore-", dir=dest.parent)
try:
    with os.fdopen(fd, "wb") as output:
        shutil.copyfileobj(sys.stdin.buffer, output)
    with sqlite3.connect(f"file:{staging}?mode=ro", uri=True) as snapshot:
        if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite integrity check failed")
        if not snapshot.execute("SELECT 1 FROM django_migrations WHERE app = ?", ("studio",)).fetchone():
            raise RuntimeError("FitStudio migration history is missing")
    os.link(staging, dest)
finally:
    os.unlink(staging)
' < "$backup" || exit 1
restore_dc up -d --wait
restore_dc exec -T web python manage.py showmigrations studio
restore_dc exec -T web python -m config.check_health
```

按第一节的方法，从恢复项目导出它自己的根证书，验证其 HTTPS、登录、会员、购课、预约和课时余额，再评估是否切换流量。切换生产流量与当前文档验收属于不同的操作。

[容器与数据卷架构](architecture/system-architecture.md) · [SQLite ADR](engineering/adr/0001-sqlite-single-instance.md) · [公共工程摘要](engineering/phase2-public-summary.md)
