# CLAUDE.md

## 项目协作规则

- 默认使用中文沟通、计划、总结和报告。
- 修改行为前先读现有代码和测试。
- 代码改动只围绕用户请求，不做无关重构。
- 不覆盖用户已有修改。
- 修改配置或用户级 hook 文件前，优先使用项目内置命令的备份机制；没有备份机制时先手动备份。

## 运行环境同步要求

本项目经常在本机以 daemon 方式运行，同时本地仓库代码也在编辑。凡是修改影响 hooks、daemon 行为、开机启动脚本、配置处理或运行态状态处理的代码，必须同时更新本地仓库和本机正在运行的环境：

1. 先修改并验证本地仓库代码。
2. 再从仓库同步到运行环境：

```powershell
python -m vibecodinglight sync-runtime
```

该命令会重写本机 hooks / 开机自启入口，并重启 daemon。当确认本机正在运行 VibeCodingLight 时，报告修复完成前必须执行这一步。

## 验证要求

- 先跑与改动相关的回归测试。
- 提交前跑全量测试：

```powershell
python -m pytest -q
```

- 对依赖正在运行 daemon 的行为，必要时运行 `python -m vibecodinglight status`，或查看 `%LOCALAPPDATA%\Temp\vibe_daemon.log`。
