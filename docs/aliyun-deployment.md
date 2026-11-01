# 阿里云 ProTok 部署

当前状态：阿里云唯一研究控制器已启动，服务为 `research-loop-protok.service`；2026-11-01 16:13:53 至 2026-11-02 16:13:53（北京时间），历史之外最多新增24 GPU小时，保留原复核预留。用户已允许暂时使用原VPS校园通道，阿里云独立SSH认证及debug资源验证通过。Mac实例继续保持STOP，无旧heartbeat。

2026-11-01 19:40（北京时间）只读检查：服务使用 `runtime-exploration-v1`，第 7 轮处于执行阶段，作业已提交。Scout 最近一次因将本地记录路径当作外部来源而未通过链接校验，先前的种子建议仍保留。这是当时的快照，不代表当前作业状态。

## 服务器文件

服务器通过 `ssh aliyun-codex` 访问，项目位于 `/root/research-loop`，使用现有 Python 3.12 `.venv`。Codex、手机远控、Clash 和认证由接入会话负责，本部署不修改它们。

部署目录为 `/root/research-loop/deployments/protok-se3-20261101`：

|路径|用途|
|---|---|
|`archive/study`|旧实验的文档、节点和原始结构化回执归档；不是可执行实例|
|`archive/control/backend-snapshot.json`|最后核账的只读快照，明确标为非实时 HPC|
|`external-records`|22个历史记录、19次计分预约，累计19084 GPU秒；含取消作业费用|
|`source-history`|独立Git对象库，可读取9个不同的历史workspace提交，不依赖Mac alternates|
|`best-source`|最佳实验0009的冻结源码，提交`9db091f93f37e990b6d2269f703855d9c4a36853`|
|`runtime-resume-v2-r2`|恢复控制器及CPU测试源码|
|`runtime-exploration-v1`|探索策略和后台 Scout 的冻结运行版本|
|`manifest.json`、`preflight-verification.json`、`deployment-status.json`|来源、身份与部署核对结果|

Mac 上的原始账本和实验目录保持不变。传输不包含SSH私钥、Codex认证、旧owner token、虚拟环境、数据、缓存或checkpoint内容；历史元数据中的owner_token字段已移除。原始未脱敏记录继续保留在Mac。

## 只读看板

服务：`research-loop-archive-dashboard.service`，开机启动，监听服务器 `127.0.0.1:8765`，内存上限128MiB。已观察实际占用约13MiB，HTTP快照及实验0009详情验证通过。

```sh
systemctl status research-loop-archive-dashboard.service
```

看板无公网监听或鉴权入口，远程访问通过受保护的连接。它读取归档，不查询HPC、不调用Agent、不启动实验。

## 恢复合同

用户新增授权：在历史19084 GPU秒之外，最多增加86400 GPU秒；从实际恢复开始最多运行86400秒。原3600秒复核预留继续适用，不重置历史费用、计数或旧session。

`/root/research-loop/runs/protok-resume-20261101/control/pending-budget.json` 保留激活前预算、逐记录费用锚点和显式记录目录迁移；激活后的期限和控制状态以运行目录中的记录为准。

schema2控制器保持科学配置身份及受保护评测器不变；旧session/amendments原样保留，新的期限仅用于受控内存视图。新增预算按当前累计费用减历史锚点计算，下一次完整1800秒分配和复核预留必须同时可容纳。STOP、到期取消和只恢复已有owned job的核账路径保留。

最佳导入目标：fingerprint `d9a98a6059885ae005a7aa6557838d7b8fe47dcf395b19db86326c15ae27d7ba`，RMSD 2.2394492307654823 Å。已在服务器验证路径迁移没有改变该fingerprint或科学config identity。新Study应以此明确历史导入开始，旧取消节点0010不重放、不伪造Review；旧DAG作为外部历史归档保留。

恢复入口支持受信任环境参数：`PROTOK_PROJECT`、`PROTOK_CODEX`、`PROTOK_BASE_COMMIT`、`PROTOK_BASE_FINGERPRINT`、`PROTOK_BASE_VALUE`。新实例应使用Linux路径和已验证的gpt-6.1-sol客户端，不复用Mac可执行路径。

## 激活前条件

1. 当前获准的VPS校园通道和阿里云 `ssh hkust-hpc` 非交互认证已验证；不依赖Mac转发agent。
2. 原Mac仍停止且没有owned job；阿里云只建立一个新的控制权lease。
3. 以真实恢复时间生成新start/deadline和owner nonce，封存预算与handoff证据；此时才开始新24小时计时。
4. 核对运行环境、冻结控制器版本、原记录镜像和启动开关；之后初始化新的延续Study并执行一次无GPU历史最佳导入，再进入新候选评价。

## 验证与回退

本地52项ProTok相关CPU测试通过，Ruff通过；独立审阅发现的过期终态核账与迁移缓存配置问题已修复并通过针对性检查。服务器验证结果见`server-tests-r2.log`；测试使用临时夹具和模拟传输，不代表新的GPU实验。

停止看板可执行 `systemctl disable --now research-loop-archive-dashboard.service`。保留源码库、归档和STOP即可保持可回退状态；不删除或覆盖Mac原项目、HPC checkpoint或历史账本。
