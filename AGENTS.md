# RSI development

Runtime and tests use Python standard library only, Python >= 3.10.
The `agent/` and `evolution/` trees seed mutable generations. The `kernel/`
tree owns scoring, source manifests, activation and rollback and must never be
included in candidate editing scope. Changes to kernel code require lifecycle tests.

Preserve unrelated experiments. Do not run paid model experiments unless requested.
Use `python -m unittest discover -s tests -v` to verify the offline lifecycle.
Offline demo patches are scripted fixtures: never describe their scores as evidence
of autonomous discovery. Keep runtime limitations explicit in README.

机制开发进度以 README.md 的「上游机制落地清单」为准。每完成并验证一项机制，
同步更新勾选、实现说明与验证记录；部分实现保持未勾选并说明缺口。
不得把可选接入写成默认启用，也不得将基础规则检查描述为语义验证或安全沙箱。

DeepSeek/Codex 任务、编码、审查后端由 kernel/backends.py 固定实现；不要把密钥复制进仓库。
迁移上游能力时，接入与离线接口测试可以先完成；真实付费模型实验须有用户请求。
后端接入成功、模拟测试通过、真实模型实验通过是三种不同状态，必须分别记录。
Codex CLI 超时时，先对比系统代理与子进程代理环境，并检查原生事件是否已完成。
内置后端会桥接系统代理；--codex-isolated 是可选隔离，不得为解决超时擅自改全局配置。
