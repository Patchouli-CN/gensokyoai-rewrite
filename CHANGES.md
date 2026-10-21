# CHANGES

## 2.0.0（开发中）

整体重写，与 1.x 不兼容。1.x 历史见 git 记录。

### 架构

- 房间（Room）为唯一事实源：append-only 事件流，器官只依赖房间、互相不见面
- 旧的大脑层 / 决策管线 / 喊话式工具提取 / 注意力档位全部移除
- Mind 改为原生 tool_call：模型用 `send_message / wait / read_events` 动作说话，正文直接丢弃
- 场景插件系统：`@scene` / `@tool` 声明，entry points 或目录扫描挂载，内置 chat 场景

### 持久化

- 房间事件落盘（ayafileio，append-only JSONL），重启回放，单文件超限轮转
- 记忆 JSONL：bigram 召回，条数过压力线时调模型压缩成概要

### 运行时

- BgTasks：后台任务登记处（一次性 / 周期），统一监督、退出前 drain
- DMN 静息整理：房间静默超阈值才跑整理
- ws 网关：token 校验、房间数上限、ReplyStyle 按连接声明
- 斜杠命令：/doctor、/help，权限分级
- Loop 单轮失败退避重试，模型连接全进程复用
