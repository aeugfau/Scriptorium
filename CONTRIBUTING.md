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
| 加/改初始世界 | `config.yaml` | 字段须对应 `models.py`，枚举值用小写字符串/整数；`seed: 0`=每次随机，正整数=可复现 |
| 加新地貌/政体/科技等级 | `models.py` 枚举 + `engine.py` 相关表 | 同步更新 `BIOME_YIELD`、稳定度均衡点、`LIFESPAN_BY_TECH` 等 |
| 调整演化难度/节奏 | `engine.py` 的系数 | 系数旁注释会提示影响 |
| 调整各时代寿命图景 | `engine.LIFESPAN_BY_TECH` | `(衰老起始, 硬上界)`；到上界必死，进窗口后概率递增 |
| 加新涌现事件 | `engine._emerge` 追加 if 块 | 涌现应是状态阈值 + 可复现随机；若事件确立不可逆事实，同时写 `Fact`（见下节） |
| 调命名规范 | `models.NamingStyle` + `config.yaml` 各文明 `naming:` 块 | 词库/模板/style_note 均可演化；引擎在科技/政体变更时调 `_maybe_evolve_naming` |
| 调社会阶层 | `models.SocialClass` 枚举 + `engine._unlock_classes_for_government` | 核心阶层用枚举（确定性逻辑用）；具体身份头衔走 `role_pool`（LLM 提议） |
| 加新角色身份 | `naming.RoleProposer._MOCK_ROLE_CANDIDATES`（mock 兜底）+ LLM 自动提议 | 关键事件触发；落 `role_pool` 并写 `Fact(kind="role_emergence")` |
| 加新文本体裁 | `generators.py` 加方法 + 接入 `generate_for_tick` + 登记 `archive.Genres` | 返回 `Artifact` 或 `None`（不触发时）；会自动经 `_validated` 校验 |
| 换/加 LLM 后端 | `providers.py` 实现协议 + 注册到 `get_provider`/`get_provider_from_config` | 不要在 engine/generators 直接 import SDK；接入优先走 `llm.yaml` 配置 |
| 加 CLI 命令 | `cli.run` 的命令分发 | 优先复用 engine 钩子，别直接改 World |

## 既成事实台账与叙事校验（重要设计）

为防止「人物死后还写日记」「败者翻案胜者」这类叙事与状态脱节，系统有两层防护：

**1. 既成事实台账（`models.Fact` / `World.facts`）**

事件（`Event`）是会被遗忘的流水；事实（`Fact`）是写入后永久约束叙事的权威记录。
引擎在关键节点写 fact：

- 人物辞世 → `engine._emerge` 写 `kind="death"` 的 fact
- 战争胜负 → `engine._diplomacy_tick` 写 `kind="victory"` 的 fact

生成器通过 `world.active_facts(year)` 取「该年仍生效」的事实，注入 `world_brief`，
标注「必须遵守，违反即叙事错误」喂给 LLM。新增不可逆事件时，记得同时写一条 fact，
并在 `statement` 里写清楚「不得……」的硬约束措辞。

**2. 事后校验（`generators._violations` / `_validated`）**

LLM 不 100% 可靠，每个档案产出后由 `_violations` 再扫一遍正文，机械判定：

- death：日记作者在落款年已死（按 `Artifact.author_id` 匹配 `Fact.subject`，**不按名字**——名字会撞名）即违规
- victory：正文出现「败者击败胜者」翻案措辞即违规

`_validated` 对违规格式重生成最多 2 次，仍违规则告警放行（不丢档案）。

关键约定：
- **按 id 匹配，不按名字**：`Fact.subject` 与 `Artifact.author_id` 都是 `Person.id`。
  人物名字库小会撞名，按名字匹配会让同名活人替死人背锅。新增校验规则时务必用 id。
- **日记作者筛选与校验同口径**：`diary` 候选用 `death_year > focal`（死前还活着可写），
  校验判定 `art.year >= f.year`（写日记时人已死才违规）。改一方必须同步另一方。
- `Artifact.author_id` 仅供校验用，`author` 才是展示名。

## 命名生成与社会结构涌现（重要设计）

为让视角人物层次更丰富、命名风格随文明演化，系统采用「结构化规范 + LLM 即兴」混合：

**1. 命名规范（`models.NamingStyle`）** —— 结构化词库+模板+风格说明，每文明一套，
   可入 `config.yaml`、可演化。名字本身由 `naming.NameGenerator` 调 LLM 按规范即兴组合
   （mock 兜底从词库随机组合）。演化由 `engine._maybe_evolve_naming` 在科技升级（加
   意象词根）/政体更替（改 style_note）时触发，写 `Fact(kind="naming_reform")`。

**2. 社会阶层（`models.SocialClass` 枚举）** —— 「骨架」，固定取值（nobility/commoner/
   artisan/soldier/clergy/outsider/marginal）。诏令「谁能发」、议事会「谁能出席」等
   确定性逻辑靠它判断。`engine._unlock_classes_for_government` 按政体解锁阶层。

**3. 具体身份头衔（`Civilization.role_pool`）** —— 「血肉」，自由文本（如「航海长」
   「角斗士」「异端审判官」）。由 `naming.RoleProposer` 在关键事件（科技/政体/宗教变革）
   时调 LLM 提议、指明所属核心阶层，引擎校验阶层合法后落 `role_pool` 并写
   `Fact(kind="role_emergence")`。spawn 人物时从 `role_pool` 抽身份。`role_pool` 的
   增长本身即文明社会演化的可见记录。

**4. 立体视角（`Person.social_class` + `age_note` + `role`）** —— `generators.diary`
   按阶层+年龄+具体身份挑作者并注入 prompt，让贵族、工匠、边缘流民语气迥异。

关键约定：
- **核心阶层用枚举、具体身份用自由文本**：确定性逻辑只认枚举，不被自由身份打破。
- **mock 兜底**：`NameGenerator`/`RoleProposer` 在 mock 模式分别走词库随机组合、
  `_MOCK_ROLE_CANDIDATES` 固定候选，保证零配置可跑通且行为可复现。
- **演化写 Fact**：命名变革与身份涌现都写 Fact，使社会结构变迁成为可见文明史，
  也供校验层与叙事层引用。

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

## 清除已生成的文本档案

每次测试/开发往往会在 `archives/` 留下大量上次游戏的档案，混淆新一轮的产出。
两种清除方式，都只动 `archives/`，不影响 `saves/` 存档：

```bash
# 命令行：开新局前一键清空，不进入游戏
python -m civsim.cli clear
```

游戏内按 `c` 键可随时清除，会二次确认 `y/N`（防误删一局心血）；命令行
`clear` 不确认（主动敲的就是要清）。

实现位置：`Archive.clear()`（`archive.py`）负责删 SQLite 索引 + 各体裁目录下
`.md` 文件并保留目录结构与 `.gitkeep`；`cli.py` 的 `clear` 子命令与游戏内 `c`
键均调用它。新增体裁时无需改 `clear()`——它按目录遍历，自动覆盖。
