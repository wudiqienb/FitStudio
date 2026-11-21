# 第三方声明 · Third-party notices

## 随源码提供的内容

| 内容 | 分发范围和许可 |
| --- | --- |
| Django 项目脚手架 | [manage.py](../manage.py)、[config/asgi.py](../config/asgi.py) 和 [config/wsgi.py](../config/wsgi.py) 来自 Django 项目脚手架，经项目名称实例化。[config/settings.py](../config/settings.py) 保留脚手架结构，并配置项目应用、模板、数据库和业务设置。[config/urls.py](../config/urls.py) 在脚手架基础上加入项目路由和健康探针。上游部分保留 BSD-3-Clause，完整版权、条款和免责声明取自 Django 5.2.19 发行包。 [Django BSD-3-Clause 全文](licenses/Django-BSD-3-Clause.txt) |
| Bootstrap 5.3.8 | [Bootstrap CSS](../studio/static/studio/vendor/bootstrap.min.css) 和 [Bootstrap Grid CSS](../studio/static/studio/vendor/bootstrap-grid.min.css) 保留上游原始字节及版权头。 [Bootstrap MIT 全文](../studio/static/studio/vendor/LICENSE.bootstrap.txt) |
| Django Admin 图标 | [后台截图](screenshots/admin.png)展示 Django 5.2.19 Admin 提供的图标。图标版权归 Code Charm Ltd，上游图标许可证为 MIT。 [Code Charm Ltd 声明及 MIT 全文](licenses/Django-Admin-Icons-MIT.txt) |

## 安装依赖

| 依赖 | 版本 | 许可证 |
| --- | --- | --- |
| Django | 5.2.19 | BSD-3-Clause |
| asgiref | 3.13.0 | BSD-3-Clause |
| sqlparse | 0.6.0 | BSD-3-Clause |
| tzdata | 2026.5 | Apache-2.0（Python 发行包声明） |
| Gunicorn | 27.0.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| WhiteNoise | 6.9.0 | MIT |
| Bootstrap | 5.3.8 | MIT，见随附声明 |

Python、SQLite、Caddy、Docker、Compose 和 Node.js 由各自环境提供。Python 镜像与 SQLite 的版权及许可取决于实际发行物，Caddy 2.11.9 核心采用 Apache-2.0。预构建镜像、生成后的静态目录和依赖二进制不随此源码候选分发。

[项目内容许可](CONTENT-LICENSE.md) · [原创软件代码 MIT](../LICENSE)
