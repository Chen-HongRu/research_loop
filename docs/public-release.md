# Source distribution boundary

The current source tree contains the native Codex runtime, reusable read-only
dashboard, role instructions, synthetic tests and a synthetic goal example.
Private Studies, raw transcripts, credentials, deployment configuration, research
inputs and results stay outside commits. The MIT license covers project code;
third-party assets retain the licenses listed in [notices](../THIRD_PARTY_NOTICES.md).

Ignore rules do not protect already tracked files. Review the staged file list
and contents before publishing. Pattern checks are supporting evidence, not proof
that no sensitive material exists.

## 历史边界

历史发行曾对当时本地可达的 55 个提交做有界元数据、文件路径和模式审计：旧提交仍包含已移除的研究输入和部署文档。此次修改只清理发行树，未重写历史，也未改变仓库可见性。Git 作者、提交说明、旧分支、Issue、PR、附件和托管平台缓存不在当前树清理的覆盖范围内。

**不要仅合并本 PR 后就把原仓库改为 public。** 对外共享时可先使用下面的不带历史源码包；要公开一个 Git 仓库，可将该快照作为新仓库的初始提交，再确认要保留的作者署名、贡献记录及托管元数据。处理原仓库历史属于另一个有明确范围的操作。

## 导出不带历史的源码包

先确认待分发版本已提交且通过检查，再在该任务分支运行：

```sh
mkdir -p dist
git archive --format=tar.gz --prefix=research-loop/ \
  --output=dist/research-loop-public.tar.gz HEAD
tar -tzf dist/research-loop-public.tar.gz
```

`git archive` 仅打包指定提交的树，不包含 `.git`、未跟踪文件或忽略的本地运行产物。导出后核对文件清单；新加或重新加入的敏感文件仍可能进入源码包。发送给合作者的是归档文件，不是原仓库的克隆或 bundle。

在独立目录解压后可验证：

```sh
tar -xzf research-loop-public.tar.gz
cd research-loop
uv sync --locked
uv run research-loop doctor
```

模型账户配置与凭据、原始数据、外部任务仓库和训练资源都不在包内。MIT 许可不改变外部数据或模型的使用条件。
