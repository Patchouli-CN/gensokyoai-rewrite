# 配置约定

改配置相关代码时，这份文件是硬约束 —— 有测试兜底，但先看规则能少挨一次红。

## 谁是唯一来源

| 内容 | 位置 | 进仓库 |
| --- | --- | --- |
| 模型、供应商、任务、价格 | `config/llm/*.yaml` | ✅ |
| 计费覆盖、前缀缓存、静息整理 | `config/settings.yaml` | ❌（用 example 代替） |
| 密钥 | `config/llm/keys/*` | ❌ |
| 运行参数（GSK_*） | `nb2_plugin/.env` | ❌ |

`config/settings.yaml` 不跟踪，所以**每次加配置字段都要同步更新
`config/settings.example.yaml`**：`tests/test_settings_example.py` 会逐字段
核对 `Settings`、`PromptSettings`、`IdleSettings`、`BillingSettings`，
少了就红。

## 加一个旋钮的流程

1. 在 `gensokyoai/settings.py` 的对应 `msgspec.Struct` 里加字段（给默认值）
2. 在 `config/settings.example.yaml` 里加同名键 + 一行注释说清单位与取值
3. 在代码里读它（构造参数优先，模块常量只做默认值）
4. 跑 `pytest tests/test_settings_example.py`

默认值必须能独立工作：没有配置文件时 `Settings()` 就该跑得通。

## 反过来的约束

不要把"业务策略"塞进 `config/llm/`。那四份文件只描述"模型是什么、怎么计价、
哪个任务派给谁"；"多久压缩、留几条历史、静默多久算睡"属于 `settings.yaml`。
