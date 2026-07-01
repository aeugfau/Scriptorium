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

之所以保留 `civsim` 作包名，是为了避免改动已有导入路径；外部一切物料（README、仓库、文档站）都用 Scriptorium。

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
  models.py     结构化世界状态（Pydantic）—— 文明、人物、事件、纪元、既成事实(Fact)、命名规范、社会阶层
  engine.py     混合驱动引擎：规则推进 + 涌现 + 玩家事件 + 叙事；含寿命分级与命名/阶层演化
  generators.py 多体裁文本生成器 + 事后校验层（_violations/_validated）
  naming.py     LLM 命名生成器 + 角色身份提议器（NameGenerator/RoleProposer，mock 兜底）
  providers.py  可插拔 LLM 后端（mock / Anthropic / OpenAI兼容 / 本地）+ 配置文件工厂
  archive.py    档案库：Markdown 落盘 + SQLite 索引
  cli.py        rich 终端交互入口
config.yaml     默认初始世界（尤弥尔大陆：诺尔海姆 + 翠地亚）
llm.yaml        你的 LLM 接入配置（不入库；从 llm.example.yaml 复制）
llm.example.yaml LLM 接入模板（入库，带详细注释）
archives/       生成的文本档案（即"文明图书馆"）
saves/          世界存档（JSON）
```

## 关键设计

- **混合驱动**：规则维护数值状态，LLM 只叙事不做状态计算（见 `engine.tick` 四步）。
- **叙事时间真实性**：事件年份散落在 tick 区间内（非整 25/50/75），档案落款各自
  从区间内随机取年（见 `generators._year_in_span`），避免「同一年冒出 5 篇文档」。
- **既成事实台账 + 事后校验**：防止「死后写日记」「败者翻案」等叙事脱节。引擎在
  人物死亡/战争胜负/命名变革/身份涌现时写 `Fact`；生成器把 active facts 作为硬约束
  喂给 LLM，并在档案产出后用 `_violations` 机械校验、违规则重生成。详见 CONTRIBUTING.md 专节。
- **寿命分级与非自然死亡**：按社会发展水平给寿命区间（石器→工业），进必死窗口后每人随机
  寿限、几年内死而非一刀切；死因结构化记录到卡与 Fact（年迈/饥荒/瘟疫/战死/意外），约束叙事
  不得矛盾；战争/瘟疫等事件会点名带走涉事名人，身份相关意外按职业风险（渔→海难、兵→阵亡）。
  详见 CONTRIBUTING.md 专节。
- **命名与社会结构涌现**：每文明有结构化 `NamingStyle`（词库+模板+风格说明，可演化）；
  名字由 LLM 按规范即兴生成；社会身份（核心阶层枚举做骨架 + LLM 提议的具体头衔做血肉）
  随科技/政体演化涌现、加入 `role_pool` 增长。详见 CONTRIBUTING.md 专节。
- **官方文风连贯**：每文明存可演化的 `VoiceStyle`（按体裁记录笔法特征），生成时注入
  prompt 约束口吻，使同系列文本风格连贯；关键事件时 LLM 提议文风微调并写
  `Fact(kind="voice_reform")`，文风变迁本身成为可见文明史。详见 CONTRIBUTING.md 专节。
- **全员人物卡 + 关系图**：所有文本中出现的人物都建档（不止名人），平民卡也详细
  （性别/居所/性格/处境）；LLM 生成时附 `<CAST>` 块抽取人物（10 列含**标准名**与**本篇经历**），
  按「标准名+文明」归并——同一人即便文中用不同称谓（穆·禾氏/老穆/二叔）也识别为同一卡；
  亲属称谓由 LLM 解析为姓名+关系类型，建**双向结构化关系图**（父↔子、叔伯↔侄）；本篇经历
  追加进 `bio_entries` 让卡随被提及而成长；已故且长期未被提及者由 LLM 判断后清理（删前写
  Fact 存档）。详见 CONTRIBUTING.md 专节。
- **社会组织**：文明内部涌现可持久化的组织（村/教会/议会/行会/学堂...），由 LLM 在关键事件
  提议涌现、写 Fact；组织有纵向隶属（上下级）与双向成员关系（组织 members/officers + 人物
  orgs）；写作时只能引用既有组织（约束式生成，从源头堵新造）；新人物按 CAST 所属组织或
  阶层/role/home 推断归入组织。详见 CONTRIBUTING.md 专节。

## 给协作者的代码导读

建议按这个顺序读源码（每文件顶部都有模块级 docstring 说明职责与边界）：

1. `models.py` —— 先看数据形状。所有可序列化状态都在这里，理解了字段就理解了世界能表达什么。
   重点看 `Fact` 与 `World.active_facts`——这是叙事一致性的根基。
2. `engine.py` —— 看 `tick()` 的四步注释（规则→涌现→玩家事件→叙事），这是整个模拟的心跳。
   `LIFESPAN_BY_TECH` 是各时代寿命旋钮；`_emerge` 里写 Fact 的位置要看清。
3. `generators.py` —— 看 `ArtifactFactory`，每体裁一个方法，决定档案怎么生成；
   再看 `_violations`/`_validated`，理解事后校验如何兜底叙事一致性。
4. `naming.py` —— 看 `NameGenerator`/`RoleProposer`，理解 LLM 如何按命名规范生成名字、
   按文明现状涌现新身份；mock 兜底如何保证零配置可跑。
5. `providers.py` —— 看 `get_provider_from_config` 工厂与 `LLMProvider` 协议，换后端只动这里。
6. `archive.py` —— 看 `Archive.add/list/read`，档案如何落盘与检索。
7. `cli.py` —— 交互入口，串起以上各模块。

注释约定：模块级 docstring 讲「这文件做什么、与谁交互」；类/方法 docstring 讲「职责、参数、副作用」；
行内注释只标「为什么这么做」而非「做了什么」。改代码时请保持同样风格。详细约定见 CONTRIBUTING.md。

## 协作约定

- 在 `main` 之外开分支开发，PR 合并。
- 改动状态字段（`models.py`）时同步更新 `config.yaml` 与存档兼容性说明。
- 新增 LLM 后端只需在 `providers.py` 实现协议并在 `get_provider_from_config` 注册，不要在引擎/生成器里直接 import SDK。
- 新增文本体裁只需在 `generators.py` 加一个返回 `Artifact` 的方法并接入 `generate_for_tick`，并在 `archive.Genres` 登记；会自动经 `_validated` 校验。
- 新增不可逆事件（死亡/胜负/政变等）时，记得在引擎里同时写一条 `Fact`，措辞含「不得……」硬约束；校验规则用 id 匹配，不按名字。

## 后续可扩展

- 多文明外交/贸易/战争细化、地图可视化
- LLM 涌现事件提案（让演化更具戏剧性，而非纯阈值）
- 记忆管理升级：滚动摘要 + 向量检索回忆旧事
- Web UI 浏览档案库
