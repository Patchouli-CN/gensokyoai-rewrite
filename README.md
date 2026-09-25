<div align="center">

# GensokyoAI

**幻想乡角色扮演引擎 · 重写版**

在 8K~32K 上下文的小模型上，养一个「活着」的角色。

![Python](https://img.shields.io/badge/Python-%3E%3D3.14-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Alpha-orange)

> 「啊啦～，欢迎来到白玉楼～。这里很少有客人来呢。
> ……啊，点心的话，我的那份也绝对不能少哦。」

</div>

## 这是什么

一个用本地 LLM 跑角色扮演的引擎。目标是让小模型（8K~32K 上下文）也能稳定、快速地待在人设里——不串味、不忘事、不乱说话。

做法只有一句话：**把推理和记忆挤出上下文，交给外部工程去管。** 角色一致性、记忆、发言时机、思考深度，全部由工程代码承载，而不是塞进提示词里祈祷模型自觉。上下文里只留最精华的部分。

## 特性

**思考**

- 五档动态推理深度（`NONE / LOW / MID / HIGH / MAX`）：寒暄只跑收尾步（1 次调用），重大剧情跑全链还翻倍预算。档位由裁判打分决定，规则路由兜底。
- 可定制思考链：角色卡里的 `think_chain` 按名拼装思考步骤，**换链 = 换角色的思维方式，零代码**。卡片作者也可以直接写内联步骤。
- 接力思考：没配思考链时走多轮接力，模型每轮自述要不要继续想，轮数按档位封顶。
- 工具调用：接力与思考链都能挂工具，内置时间 / 日期 / 月相。本地小模型不走原生协议，走文本喊话（`调用 工具名 {"参数": 值}`），参数 JSON 尽力提取。
- 过渡语掩延迟：深思考前先垫一句角色口吻的「唔……让妾身想想」，掩盖接力思考的停顿。

**发言门控（System-1 层）**

- 新消息先过零成本规则（收尾语 / 私聊 / 被 @ 直判），群聊模糊带才调裁判——一次调用同时回答「该不该接话」和「该想多深」。
- 裁判后端可插拔：本地模型（`LocalJudge`，无状态小调用）或真 [TypeSafe jev](https://typesafe.ai)（可选依赖）。
- 裁判挂了会自动退化：不接的回合回落「点名才回」，档位回落规则路由。
- OOC 三重守门：初稿规则预筛（零成本）→ 命中才纠偏重生成 → 发出后异步深审。出戏率飙升时自动抬高推理档位下限，恢复后自行撤销。

**表达与感知**

- Eyes / Mouth 分层：感知器协议屏蔽平台差异，口层支持流式逐字蹦或整段投递，控制台和 WebSocket 对称接入。
- 主动发言：四维对话欲规则评估（表达欲 / 情绪唤起 / 关系牵引 / 情境时机），冷场时角色会自己冒泡——零 token。
- 频道中枢：多路输入合流成一条快照流，一频道一世界，空闲自动回收。

**记忆与运维**

- 分级记忆：工作记忆 + 长期记忆，重要性直存、访问强化、时间衰减淘汰，定期蒸馏压缩成摘要，重启可恢复。
- 会话持久化：记忆、快照、跨回合运行时状态事件驱动落盘（走自家 [ayafileio](https://github.com/Patchouli-CN/ayafileio) 真异步 IO），关闭前 flush。
- 资源闸门：全局并发 + 每租户 RPM / 日预算限流，保护本地单卡；入口令牌桶在变成模型调用之前就拒掉超速用户。
- 健康监控：token / 延迟 / 上下文占用 / OOC 率 / 费用，超限告警 + 主动干预（上下文告急自动触发蒸馏）。
- 计费兼容层：输入 / 缓存 / 输出四条流统一计价，内置主流云端模型价格表，本地模型明确不计价（绝不瞎猜）。
- 思考轨迹留档：每回合一行 JSONL，含逐步推理记录，复盘调参用。

## 快速开始

```bash
pip install -e .          # 或 pip install -e .[dev]
python main.py            # 控制台模式，直接和角色对话
```

模型默认指向 `http://127.0.0.1:8080/v1`（llama-server 或任何 OpenAI 兼容端点）。

完整步骤见 [docs/QUICKSTART.md](docs/QUICKSTART.md)，常见问题见 [docs/QA.md](docs/QA.md)。

## 五档推理深度

| 档位 | 思考链（配了卡片链） | 接力思考（未配卡） | 适用场景 |
|---|---|---|---|
| NONE | 不跑链 | 不跑 | 空输入 / 纯寒暄收尾 |
| LOW | 只留收尾步（1 次调用） | 2 轮 | 日常寒暄、简单问答 |
| MID | 首 + 尾（2 次调用） | 3 轮 | 一般对话，需看关系和情绪 |
| HIGH | 全链（N 次调用） | 5 轮 | 复杂剧情推进、多角色交互 |
| MAX | 全链 + 每步深思考（预算 ×2、低温） | 8 轮 | 重大剧情节点、情感转折 |

## 发言门控

```
新消息 → 收尾语/私聊/被@（规则直判，零成本）→ 群聊模糊带 → 裁判一次调用：
        {该不该说 should_reply, 该想多深 needs_deep, addressed, needs_search}
```

`should_reply` 过阈值才接话；裁判的输入里带着角色自己的活跃度（近 5 分钟说了多少、距上次发言多久）——防刷屏靠模型自觉，代码不设硬冷却。

## 项目结构

```
gensokyoai/
├── utils/          # 链式基类 / 日志 / token 计数
├── schemas/        # 数据契约（msgspec 结构体）
├── prompts/        # 提示词集中注册
├── core/           # 引擎内核
│   ├── brain/      #   决策：接力引擎 / 门控 / 裁判 / 思考链 / OOC
│   ├── responder/  #   表达：生成 / 风格 / 过渡语 / 纠偏
│   ├── memorizer/  #   记忆：分级存储 / 蒸馏
│   └── health/     #   监控：指标 / 告警 / 干预
├── models/         # 模型接入：llama_cpp / qwen_local / 价格表
├── satori/         # 感知层（觉）：perceiver / 多路合流 / OneBot11 解析
├── mouth/          # 口层：console / 广播+流式
├── roleplay/       # 领域：角色卡 / 主循环 / 频道中枢
├── command/        # 命令子系统：解析 / 执行 / 权限
├── tools/          # 内置小工具（时间 / 日期 / 月相）
└── backends/       # 入口：ws_server（WebSocket 多路频道）
```

分层铁律：依赖只能向下，`core` 内部四个模块之间零直接 import（通信走事件总线或装配层注入），CI 有 AST 检查兜底。

## 开发

```bash
python -m pytest          # 测试
python -m ruff check .    # lint
python -m mypy gensokyoai # 类型
python scripts/real_verify.py --fresh   # 真机验证（需要本地 llama-server）
```

提交遵循 [Conventional Commits](CONTRIBUTING.md)，AI 提交请标注模型名。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 安装、配置、跑起来、调参、排错 |
| [docs/QA.md](docs/QA.md) | 常见问题：模型、门控、思考链、记忆、性能、故障 |
| [CHANGES.md](CHANGES.md) | 更新日志（含真机实录踩坑史） |

## 相关项目

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — llama-server 推理后端
- [TypeSafe AI](https://typesafe.ai) — jev 决策模型（可选裁判后端）
- [ayafileio](https://github.com/Patchouli-CN/ayafileio) — 自家跨平台真异步文件 IO
- [MaiBot](https://github.com/Mai-with-u/MaiBot) — 回复时机 / 表情 / 记忆思路的来源

## 二次创作声明

本作品是以上海爱丽丝幻乐团（ZUN）的《东方 Project》系列为题材的二次创作，与原作方无关。内置角色卡的版权归原作方所有，本项目仅作技术演示用途。

## License

[MIT](LICENSE)
