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
| 调整各时代寿命图景 | `engine.LIFESPAN_BY_TECH` | `(衰老起始, 必死窗口下界, 必死窗口上界)`；进窗口后每人随机取个人寿限 max_age，几年内必死而非立即死 |
| 加新致死路径（战死/瘟疫/处决等） | `engine._maybe_kill_notables` 或新方法 + 调 `_kill_person` 同口径写 `cause_of_death` | 死因须落 `Person.cause_of_death` 与 death `Fact`；叙事不得改写 |
| 调身份意外风险 | `engine.ROLE_RISK`（关键词倍率）+ `SOCIAL_CLASS_BASE`（阶层基准）+ `ACCIDENT_CAUSE`（死因映射） | 渔/海最高、贵/祭最低；意外死因按 role 关键词映射（渔→海难） |
| 加新涌现事件 | `engine._emerge` 追加 if 块 | 涌现应是状态阈值 + 可复现随机；若事件确立不可逆事实，同时写 `Fact`（见下节） |
| 调命名规范 | `models.NamingStyle` + `config.yaml` 各文明 `naming:` 块 | 词库/模板/style_note 均可演化；引擎在科技/政体变更时调 `_maybe_evolve_naming` |
| 调社会阶层 | `models.SocialClass` 枚举 + `engine._unlock_classes_for_government` | 核心阶层用枚举（确定性逻辑用）；具体身份头衔走 `role_pool`（LLM 提议） |
| 加新角色身份 | `naming.RoleProposer._MOCK_ROLE_CANDIDATES`（mock 兜底）+ LLM 自动提议 | 关键事件触发；落 `role_pool` 并写 `Fact(kind="role_emergence")` |
| 调官方文风 | `models.VoiceStyle` + `config.yaml` 各文明 `voice:` 块 | 按体裁记笔法；关键事件 LLM 提议微调（`naming.VoiceReformer`）并写 `Fact(kind="voice_reform")` |
| 加人物卡字段 | `models.Person` | 平民卡也详细（home/traits/circumstance/gender/relations）；`bio_entries` 累积经历；`relations` 双向关系图 |
| 抽取文本人物建档 | `generators._split_cast`/`_resolve_persons` + `CAST_INSTRUCTION` | LLM 在正文后附 `<CAST>` 块（10列：姓名/性别/身份/阶层/居所/性格/处境/关系类型/标准名/本篇经历）；按「标准名+文明」归并（处理同一人不同称谓），新人物走 `register_commoner`；亲属称谓建双向 `relations` 边；称谓当名由 `_looks_like_appellation` 识别；关系类型由 `_normalize_relation` 清洗；本篇经历追加进 `bio_entries` |
| 调离世清理 | `engine._purge_dead_unreferenced` + `naming.PersonPurger` | 已故且长期未提及者 LLM 判断后删；删前写 `Fact(kind="person_archive")` 存关键信息 |
| 加新文本体裁 | `generators.py` 加方法 + 接入 `generate_for_tick` + 登记 `archive.Genres` | 返回 `Artifact` 或 `None`（不触发时）；会自动经 `_validated` 校验；落款用 `_year_in_span` 散布；附 `CAST_INSTRUCTION` 抽取人物 |
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

## 官方文风与落款散布（重要设计）

**1. 文风连贯（`models.VoiceStyle`）**：每文明存按体裁的笔法说明（`by_genre`），
   `generators._gen` 生成时把 `civ.voice.for_genre(genre)` 拼进 system prompt 末尾，
   约束该篇口吻与该文明一贯风格一致。否则同一文明历年编年史会忽文言忽白话。

**2. 文风演化（`naming.VoiceReformer`）**：关键事件（科技/政体/宗教变革）时调 LLM
   提议文风微调，引擎采纳后更新 `civ.voice.by_genre` 并写 `Fact(kind="voice_reform")`。
   mock 模式按触发关键词给固定候选（`_MOCK_VOICE_CANDIDATES`）。文风变迁本身成为可见文明史。

**3. 落款散布（`generators._year_in_span`）**：日记/诏令/经文/会议纪要各自从本 tick
   区间 ``[w.year-years_per_tick, w.year]`` 随机取落款年，而非共用一个 focal year——
   避免「同一年冒出 5 篇文档」的不自然。编年史是综述，仍覆盖整段区间。
   日记作者存活检查用该篇自己的落款年（与校验层 `_violations` 同口径）。

## 寿命分级与非自然死亡（重要设计）

让人物生死既符合社会发展水平、又被重大事件真实带走，且死因前后一致。

**1. 寿命分级（`engine.LIFESPAN_BY_TECH`）**：按 TechLevel 给 `(衰老起始, 必死窗口下界,
   必死窗口上界)`，石器(40,50,60)→工业(65,75,95)。进必死窗口后每人随机取个人寿限
   `Person.max_age`（窗口内 rand），几年内必死而非到固定某年一刀切——年龄散布在窗口内。
   判定 age 用 w.year，死亡年龄从 `[age起点下界, 当前age]` 取再 `dyear=birth+death_age`，
   保证记录年龄与判定一致（旧实现用 `real_year()` 区间随机年导致记录年龄偏低、可低于 age_start）。

**2. 死因记录（`Person.cause_of_death` + `engine._death_cause`）**：死因结构化记录到卡与
   death `Fact`「因X辞世……亦不得改写其死因为其他」，约束叙事不得矛盾。`_death_cause` 按
   状态/近期事件规则生成（年迈/饥荒/瘟疫/动乱/战乱余波）。

**3. 非自然死亡（`engine._maybe_kill_notables` + `_kill_person`）**：填补「战争/瘟疫只削数值
   不杀人物」与「无职业风险」两个缺口。两类来源：
   - **事件级致死**：本 tick 有战争/瘟疫/饥荒/动荡事件时，涉事文明名人按概率被点名
     （战 25%/瘟 30%/荒 15%/乱 12%），死因为战死/瘟疫/饥荒/动乱。
   - **身份相关意外**：每 tick 按 `ACCIDENT_BASE(0.006) × SOCIAL_CLASS_BASE × ROLE_RISK` 判定。
     `ROLE_RISK` 按 role 关键词倍率（渔/海 3.0、矿/冶 2.5、兵/战 2.2、农 1.0、贵/祭 0.4-0.5）；
     死因按 `ACCIDENT_CAUSE` 关键词映射（渔→海难、矿→矿塌、兵→阵亡）。渔民意外远大于农民。
   两类共用 `_kill_person` 统一落账（设 death_year/cause、记事件、写 Fact），保证死因口径一致；
   同 tick 一人至多死一次。

## 全员人物卡与文本抽取（重要设计）

让世界「厚」起来：所有文本材料中出现的人物都有详细人物卡，而非只有名人。

**1. 人物卡分层（`models.Person`）**：`kind` 区分 notable（名人，引擎 spawn）/
   commoner（平民，文本抽取建档）。平民卡也详细：`gender`/`home`/`traits`/
   `circumstance`/`relations_note`，让渔民寡妇与宫廷贵妇视角都鲜活。`bio_entries`
   是经历条目列表，卷入重大事件时 `naming.BioSummarizer` 精炼一句追加，卡随时间成长。

**2. 文本抽取建档（`generators._split_cast`/`_resolve_persons`）**：每个体裁生成时，
   `CAST_INSTRUCTION` 要求 LLM 在正文后附 10 列 `<CAST>` 块：姓名/性别/身份/阶层/居所/性格/
   处境/关系类型/**标准名/本篇经历**。引擎按「**标准名 + 文明**」归并（标准名优先取 canonical 列，
   处理同一人不同称谓——穆·禾氏/老穆/二叔都标 canonical=穆·禾氏 则归并同卡）；命中则补全空字段、
   记 `mentioned_in`、更新 `last_mentioned_year`、**追加本篇经历到 `bio_entries`**；未命中则走
   `engine.register_commoner` 建新卡（挂寿命/死因机制）。这保证同一角色跨篇前后一致、事件同步进卡。
   `Artifact.mentioned_persons` 记录本篇提及的人物 id，便于按人物检索档案。mock 模式剥离 CAST
   指令走空抽取（mock 会原样返回指令模板污染卡片）。

**3. 人物关系图（`Person.relations` + `models.inverse_relation`）**：`relations` 是
   `dict[对方id → 关系类型]`（父/母/子/叔伯/兄/配偶/师徒/朋友/敌等）。CAST 的「关系类型」列
   让 LLM 把称谓（阿爸/二叔）解析为「对方姓名+关系类型」；`_resolve_persons(author=...)` 据此
   建双向边——author.relations[other]=rel，other.relations[author]=`inverse_relation(rel)`
   （父↔子、叔伯↔侄、配偶↔配偶）。对称关系逆为自身，未列出的回退「相识」。diary 传 author=who；
   decree/scripture/minutes 作者是机构传 None。`civ_card` 列每人最多 3 条关系注入 LLM。
   兜底：`_looks_like_appellation` 精确识别称谓当名（小岩儿之父/阿爸）→弃用→生成真名，关系边照常建
   （故意保守：宁可漏判泛称也不误杀真名阿兰/冉阿让）；`_normalize_relation` 清洗关系字段
   （去括号说明如「叔伯（之父之弟）」→「叔伯」），否则 inverse 查不到会错建反向边。

**4. 离世清理（`engine._purge_dead_unreferenced` + `naming.PersonPurger`）**：全员建档下
   人物卡会膨胀；已故且 `last_mentioned_year` 早于 50 年前者，`PersonPurger` 调 LLM 判断
   是否仍可能被未来文本牵连（有在世亲属/重大历史意义则留），可删则：先写
   `Fact(kind="person_archive")` 存其关键信息（名字/role/生卒/死因/经历），再删卡——
   删卡不丢历史一致性。文明高等级同时在世人物过多的问题留待后续处理。

关键约定：
- **归并按「标准名+文明」**：CAST 的 canonical 列优先，处理同一人不同称谓；命中只补全空字段，不覆盖已有权威信息。
- **称谓当名用精确规则弃用**：`_looks_like_appellation` 只弃用明确称谓结构（之父/阿爸/二叔），保守不误杀真名。
- **新建平民走 register_commoner**：挂上 max_age/死因机制，参与自然死亡与意外死亡。
- **`civ_card` 不列全部人**：全员建档下人物多，只列在世名人 + 最近提及的 8 位平民，
  防上下文爆炸；完整卡按需检索。

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
