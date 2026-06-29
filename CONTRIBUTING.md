# 贡献指南

感谢参与 Scriptorium！以下约定让协作顺畅。

## 开发环境

```bash
pip install -r requirements.txt
# 不配 API key 也能跑通（mock 后端）
python -m civsim.cli new-world config.yaml
```

## 注释约定（重要）

本项目要求**详细、充分的注释**，因为有多位协作者。请遵循：

- **模块级 docstring**：讲「这文件做什么、与谁交互、设计取舍」。改文件时同步更新。
- **类/方法 docstring**：讲「职责、参数、副作用、为什么这么做」。至少给出一句职责说明。
- **行内注释**：只标「为什么」，不标「做了什么」（代码本身已说明做了什么）。
- **可调系数**：在常量或魔法数字旁注明它是设计旋钮及如何调整。
- 风格与现有文件保持一致：中文叙述 + 代码标识符用反引号。

## 改动指引（按改动类型）

| 想做的事 | 改哪里 | 注意 |
|---|---|---|
| 加/改初始世界 | `config.yaml` | 字段须对应 `models.py`，枚举值用小写字符串/整数 |
| 加新地貌/政体/科技等级 | `models.py` 枚举 + `engine.py` 相关表 | 同步更新 `BIOME_YIELD`、稳定度均衡点等 |
| 调整演化难度/节奏 | `engine.py` 的系数 | 系数旁注释会提示影响 |
| 加新涌现事件 | `engine._emerge` 追加 if 块 | 涌现应是状态阈值 + 可复现随机 |
| 加新文本体裁 | `generators.py` 加方法 + 接入 `generate_for_tick` + 登记 `archive.Genres` | 返回 `Artifact` 或 `None`（不触发时） |
| 换/加 LLM 后端 | `providers.py` 实现协议 + 注册到 `get_provider`/`get_provider_from_config` | 不要在 engine/generators 直接 import SDK；接入优先走 `llm.yaml` 配置 |
| 加 CLI 命令 | `cli.run` 的命令分发 | 优先复用 engine 钩子，别直接改 World |

## Git 流程

- 在 `main` 之外开分支，PR 合并。
- 提交信息用祈使句，如「增加干旱涌现事件」「修复存档目录创建」。
- 涉及状态字段变更（`models.py`）时，在 PR 描述里说明对存档兼容性的影响。
- **切勿提交** `archives/` 实际内容、`saves/`、`.env` 或任何 API key。

## 验证

改动后至少跑一遍 mock 全流程：

```bash
python -c "
import os
from civsim.providers import get_provider_from_config
from civsim.cli import build_world
from civsim.archive import Archive
from civsim.engine import Simulation
p, src = get_provider_from_config('llm.yaml')   # 走配置文件，默认 mock
s = Simulation(build_world('config.yaml'), provider=p, archive=Archive('archives'))
s.queue_player_event('测试瘟疫','一场瘟疫席卷诺尔海姆','norheim')
for _ in range(2): s.tick()
print('OK, provider:', p.name, '| artifacts:', len(Archive('archives').list()))
"
```

期望输出 `OK, artifacts: <正数>`。
