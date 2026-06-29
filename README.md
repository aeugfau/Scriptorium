# Scriptorium

> *“一个文明的形状，藏在它留给后人的文本里。”*

Scriptorium（拉丁语「抄写室」）是一个**以文本档案作为唯一输出**的文明演化模拟器。
你不会看到地图、数字面板或战斗动画——文明的发展只通过它自己产出的材料被「读」出来：
编年史章节、居民日记、王廷诏令、宗教经文、议事会会议纪要……这些档案就是游戏本身。

玩家在开局设定初始世界（地理、文明、信仰、政体），并在演化进程中随时插入自由文本事件
（一场瘟疫、一次革新、一段被强行写入的历史）来影响走向。

---

## 名字与代码的关系

- **项目名 / GitHub 仓库名 / 对外展示名**：`Scriptorium`
- **Python 包名**：`civsim`（即 `civsim/` 目录）。代码内一律用 `from civsim...` 导入。

之所以保留 `civsim` 作包名，是为了避免改动已有导入路径；外部一切物料（README、仓库、
文档站）都用 Scriptorium。

## 设计思路（混合驱动）

- **规则引擎**（`engine.py`）确定性维护文明数值状态：人口、粮储、财富、科技、稳定、政体、外交。
- **涌现检测**在阈值跨越时（饥荒、科技突破、动荡、战争、名人出世）自动产生事件。
- **玩家事件**经自由文本注入，按关键词映射到机械效果（瘟疫→减人口、革新→加速科技……）。
- **文本生成器**（`generators.py`）把状态卡 + 本时段事件交给 LLM，产出多体裁档案。
- LLM **只负责叙事，不做状态计算**——这就是"混合"的核心：可控与生动兼得。

LLM 后端可插拔（`providers.py`）：mock（无需 key 即可跑通）/ Anthropic / 本地 OpenAI 兼容服务。

## 安装

```bash
pip install -r requirements.txt
```

## 接入你自己的 AI（重要）

这个游戏**不内置任何模型**——你接哪个它就用哪个。编辑仓库根目录的
`llm.yaml`（首次使用从 `llm.example.yaml` 复制），填四项即可，无需改代码：

```yaml
provider: openai          # mock | anthropic | openai | local
model: deepseek-chat      # 各平台文档里的 model id
base_url: https://api.deepseek.com/v1   # OpenAI 兼容端点
api_key: sk-你的key
```

- `mock`：不接模型，内置模板占位。零配置零费用，先跑通看效果用。
- `openai`：接任何 OpenAI 兼容的云端 API —— DeepSeek / 通义千问 / 智谱 GLM /
  OpenRouter 等（便宜路线都走这条）。
- `local`：接本机模型（Ollama / LM Studio），零费用可离线。
- `anthropic`：接 Claude，质量最高。

接入优先级：`llm.yaml` > 环境变量 > mock。模板里每条选项都有注释和示例。

> ⚠️ 含密钥的 `llm.yaml` 已被 git 忽略；仓库只提供 `llm.example.yaml` 模板。
> 切勿提交自己的 key。

## 运行

```bash
# 用默认世界开新局
python -m civsim.cli new-world config.yaml

# 指定另一份 LLM 配置：第三个位置参数
python -m civsim.cli new-world config.yaml my-llm.yaml

# 读档继续
python -m civsim.cli resume saves/world.json
```

> Windows 终端若中文乱码，先执行 `chcp 65001` 切到 UTF-8，或设 `PYTHONIOENCODING=utf-8`。

## 交互命令

| 命令 | 作用 |
|------|------|
| `n` / `n5` | 推进若干 tick（每 tick 默认 25 年） |
| `e` | 插入玩家事件（标题 + 描述，下个 tick 生效） |
| `a` | 浏览档案库（按体裁/年份筛选并阅读） |
| `s` | 查看诸文明状态 |
| `c` | 清除全部已生成的文本档案（会二次确认） |
| `save [路径]` | 存档（默认 `saves/world.json`） |
| `q` | 退出 |

> 想在开新局前一键清空旧档案，可直接 `python -m civsim.cli clear`。这只删 `archives/`，
> 不动 `saves/` 存档。

## 项目结构

```
civsim/
  models.py     结构化世界状态（Pydantic）—— 文明、人物、事件、纪元
  engine.py     混合驱动引擎：规则推进 + 涌现 + 玩家事件 + 叙事
  generators.py 多体裁文本生成器（编年史/日记/诏令/经文/会议纪要）
  providers.py  可插拔 LLM 后端（mock / Anthropic / OpenAI兼容 / 本地）+ 配置文件工厂
  archive.py    档案库：Markdown 落盘 + SQLite 索引
  cli.py        rich 终端交互入口
config.yaml     默认初始世界（尤弥尔大陆：诺尔海姆 + 翠地亚）
llm.yaml        你的 LLM 接入配置（不入库；从 llm.example.yaml 复制）
llm.example.yaml LLM 接入模板（入库，带详细注释）
archives/       生成的文本档案（即"文明图书馆"）
saves/          世界存档（JSON）
```

## 给协作者的代码导读

建议按这个顺序读源码（每文件顶部都有模块级 docstring 说明职责与边界）：

1. `models.py` —— 先看数据形状。所有可序列化状态都在这里，理解了字段就理解了世界能表达什么。
2. `engine.py` —— 看 `tick()` 的四步注释（规则→涌现→玩家事件→叙事），这是整个模拟的心跳。
3. `generators.py` —— 看 `ArtifactFactory`，每体裁一个方法，决定档案怎么生成。
4. `providers.py` —— 看 `get_provider()` 工厂与 `LLMProvider` 协议，换后端只动这里。
5. `archive.py` —— 看 `Archive.add/list/read`，档案如何落盘与检索。
6. `cli.py` —— 交互入口，串起以上各模块。

注释约定：模块级 docstring 讲「这文件做什么、与谁交互」；类/方法 docstring 讲「职责、参数、副作用」；
行内注释只标「为什么这么做」而非「做了什么」。改代码时请保持同样风格。

## 协作约定

- 在 `main` 之外开分支开发，PR 合并。
- 改动状态字段（`models.py`）时同步更新 `config.yaml` 与存档兼容性说明。
- 新增 LLM 后端只需在 `providers.py` 实现协议并在 `get_provider()` 注册，不要在引擎/生成器里直接 import SDK。
- 新增文本体裁只需在 `generators.py` 加一个返回 `Artifact` 的方法并接入 `generate_for_tick`，并在 `archive.Genres` 登记。

## 后续可扩展

- 多文明外交/贸易/战争细化、地图可视化
- LLM 涌现事件提案（让演化更具戏剧性，而非纯阈值）
- 记忆管理升级：滚动摘要 + 向量检索回忆旧事
- Web UI 浏览档案库
