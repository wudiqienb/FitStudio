# Phase 2 公共工程摘要

FitStudio 以单个 Django Web 实例运行。Gunicorn 为应用提供 WSGI，Caddy 负责 HTTPS 与反向代理。SQLite 用具名卷持久化，Web 容器以 UID/GID 10001 启动，在提供流量前应用迁移，静态资源构建后由 WhiteNoise 提供。

预约和课时写入依赖 SQLite `IMMEDIATE` 事务。预约只占用可用课时，完成、迟取消或爽约才按规则计入课时流水。验证覆盖全部 0001–0008 迁移、空库和合成历史数据。 [SQLite 决策](adr/0001-sqlite-single-instance.md) · [业务流程](../architecture/business-flows/README.md)

| 日期 | 历史验证 | 环境与范围 |
| --- | --- | --- |
| 2026-11-18 | 333 项自动化通过 | Windows，Python 3.14.9，Django 5.2.19，Node 24.22.1。覆盖会员账号、权限、购课、预约、课时、并发和部署检查。 |
| 2026-11-19 | 132 项容器定向回归通过 | Linux，Python 3.12，Django 5.2.19。使用断网容器和独立 SQLite，覆盖上述全量套件中的核心用例。 |
| 2026-11-19 | 12 项 Docker 实机验收通过 | 本机隔离环境，Engine 29.9.3、Compose v5.7.0。核对构建与启动、迁移、HTTPS/CSRF、角色权限、容器重建、数据持久化、日志脱敏和清理。 |

上述记录存在覆盖重叠。Docker 验收使用回环网络和实例本机 CA，业务数据均为合成数据。 [Docker 部署](../deployment.md)
