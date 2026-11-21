# 贡献指南 · Contributing

仓库发布并开放贡献后，先通过 Issue 描述问题或功能需求。报告业务问题时说明角色、预期状态、实际状态与最小合成案例。安全问题走 [安全报告渠道](SECURITY.md)。

1. Fork 已发布的仓库，Clone 自己的 Fork，再把原仓库添加为 `upstream`。使用仓库界面提供的地址。
2. 从最新主分支创建独立分支，例如 `fix/booking-conflict`、`docs/deployment-restore` 或 `feature/member-search`。
3. 参照现有模块分工。业务校验、权限与原子写入留在服务层，模板保持语义清楚。行为变化需说明对已有迁移、数据、移动视图及角色权限的影响。使用合成数据。
4. 运行相关测试和全量回归。需要 Python、生产依赖以及 Node.js。部署或数据层修改应额外核对 Docker、迁移与恢复。
5. 提交信息用简短、明确的动词，例如 `fix: preserve credits on normal cancellation`。一个 PR 聚焦一个可说明的变化。
6. 将分支提交到自己的 Fork，再向主仓库开 PR。标明关联 Issue、影响、测试环境和结果。涉及界面时可附经过隐私检查的截图。

在项目根目录验证。

```sh
python -m venv .venv
# POSIX shell: . .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-production.txt
node --version
python -B manage.py test studio.tests --settings=config.test_settings --noinput
```

现有测试设置为每次测试生成独立 SQLite 文件。保留既有业务断言，回归修复应增加能覆盖问题的测试。提交内容应排除运行环境、证书、数据库、凭据和个人数据。项目发布及贡献授权状态见 [LICENSE](LICENSE)，社区规范见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
