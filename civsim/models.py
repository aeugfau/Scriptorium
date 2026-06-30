"""
结构化世界状态模型（civsim.models）。

本模块定义模拟世界所有可序列化的状态。这是整个项目的「数据骨架」：

- 引擎（engine.py）读写这些模型来推进文明——所有数值变化都发生在这里。
- 生成器（generators.py）只读这些模型，把状态翻译成叙事文本。
- 存档/读档直接序列化整个 ``World``（``World.model_dump_json()``）。

为什么全部用 Pydantic 而非普通 dataclass？
    1. 自动校验：config.yaml 里的笔误（如把 ``coastal`` 写成 ``coast``）会在加载时
       立即报错，而不是等运行到一半才崩。
    2. 序列化免费：``model_dump_json`` / ``model_validate_json`` 一行完成存档读档。
    3. 枚举约束：``Biome``/``Government``/``TechLevel``/``Relation`` 用枚举限定取值，
       避免引擎里到处写魔法字符串。

设计原则：状态字段尽量「扁平 + 可量化」，叙事性的、无法用规则演化的内容（如人物传记）
留给 LLM 在生成档案时即兴填充，不存进结构化状态。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举：限定取值范围，避免魔法字符串
# ---------------------------------------------------------------------------


class Biome(str, Enum):
    """地貌类型。决定 ``engine.BIOME_YIELD`` 里的粮食产出系数与稳定度基线。

    用 ``str`` 混入枚举，是为了让 YAML 配置里直接写 ``biome: coastal`` 即可反序列化，
    ``Biome("coastal")`` 与 ``Biome.COASTAL`` 等价。
    """

    COASTAL = "coastal"    # 沿海：渔获补益，产出中上
    PLAINS = "plains"      # 平原：农耕最佳，产出最高
    FOREST = "forest"      # 森林：产出中等，资源多样
    DESERT = "desert"      # 沙漠：产出低下，但常出贸易型文明
    MOUNTAIN = "mountain"  # 山地：产出偏低，易守难攻
    TUNDRA = "tundra"      # 冻土：产出最低，生存压力最大


class Government(str, Enum):
    """政体。影响稳定度均衡点（见 ``engine._rules_step``）与贸易加成。"""

    TRIBAL = "tribal"          # 部落：起步政体
    CHIEFDOM = "chiefdom"      # 酋邦：人口过阈值后由部落演化而来
    MONARCHY = "monarchy"      # 君主制：进入青铜时代后由酋邦演化
    REPUBLIC = "republic"      # 共和制：高稳定 + 文艺复兴后可能演化，贸易加成高
    THEOCRACY = "theocracy"    # 神权制：低稳定分支，宗教叙事重
    EMPIRE = "empire"          # 帝国：共和制人口膨胀后演化，贸易加成高


class TechLevel(int, Enum):
    """科技阶段。用 ``int`` 混入，可直接比较大小与递增。

    引擎里 ``tech_progress`` 累积到 100 时升一级；升满 INDUSTRIAL 后不再升级。
    """

    STONE = 0          # 石器
    BRONZE = 1         # 青铜
    IRON = 2           # 铁器
    MEDIEVAL = 3       # 中古
    RENAISSANCE = 4    # 文艺复兴
    INDUSTRIAL = 5     # 工业化（上限）


class Relation(str, Enum):
    """外交立场。注意是「单向」的：A 对 B 的态度，不自动等于 B 对 A 的态度。

    不过引擎在 ``_diplomacy_tick`` 里通常成对设置，保持双向一致。
    """

    ALLIANCE = "alliance"  # 结盟
    TRADE = "trade"        # 贸易
    NEUTRAL = "neutral"    # 中立（默认）
    RIVALRY = "rivalry"    # 敌对
    WAR = "war"            # 交战


class SocialClass(str, Enum):
    """核心社会阶层（枚举，确定性逻辑用）——视角人物的「骨架」。

    为什么用枚举而非自由文本：诏令「谁能发」、议事会「谁能出席」这类判断
    需要确定性逻辑（如「君主制→NOBILITY 发诏令」），枚举保证可判定。
    具体的身份头衔（如「航海长」「角斗士」「异端审判官」）是「血肉」，
    由 LLM 提议、加入文明 ``role_pool``，spawn 时从中抽取——每个身份归入
    某个核心阶层，故既丰富又不破坏确定性逻辑。
    """

    NOBILITY = "nobility"    # 贵族/统治者
    COMMONER = "commoner"    # 平民（农/渔/牧）
    ARTISAN = "artisan"      # 工匠/匠人
    SOLDIER = "soldier"      # 军人
    CLERGY = "clergy"        # 祭司/神职
    OUTSIDER = "outsider"    # 外乡人/客商
    MARGINAL = "marginal"    # 边缘：奴隶/流民/异教徒/寡妇等


# ---------------------------------------------------------------------------
# 核心模型
# ---------------------------------------------------------------------------


class NamingStyle(BaseModel):
    """一个文明的命名规范 —— 结构化词库+模板，可控、可演化、可入 config。

    名字本身由 LLM 按本规范即兴组合（见 ``naming.NameGenerator``），但「组合的
    规矩」是结构化的：换文明有不同词库与模板，且随文明演化（科技升级/政体更替）
    而调整，调整时写 ``Fact(kind="naming_reform")`` 记录，使命名风格变迁本身成为文明史。

    ``template`` 支持占位符：{prefix}/{root}/{suffix}/{clan}/{ordinal}，
    未用到的占位符在组合时省略。``style_note`` 是给 LLM 的风格说明（如「海民喜
    航海意象、父子连名」），让生成的名字贴合文明气质。
    """

    roots: list[str] = Field(default_factory=list)        # 词根库（名字核心音节/意象）
    prefixes: list[str] = Field(default_factory=list)     # 前缀库
    suffixes: list[str] = Field(default_factory=list)     # 后缀库
    clans: list[str] = Field(default_factory=list)        # 氏族名库
    template: str = "{root}"                              # 组合模板
    style_note: str = ""                                  # 文字风格说明，给 LLM 参考
    gendered: bool = False                                # 是否区分性别用名


class VoiceStyle(BaseModel):
    """一个文明的官方文风 —— 按体裁记录笔法特征，保证同系列文本风格连贯。

    没有 VoiceStyle 时，每次生成都独立 prompt、LLM 自由发挥，同一文明隔几个 tick 的
    编年史可能一会儿文言一会儿白话。VoiceStyle 把「这个文明的编年史一贯是什么腔调」
    固化下来，生成时注入 prompt 约束；关键事件时由 LLM 提议微调并写
    ``Fact(kind="voice_reform")``，文风变迁本身也成为可见文明史。

    ``by_genre`` 的 key 取自 archive.Genres（chronicle/diary/decree/scripture/minutes）。
    ``for_genre`` 取某体裁文风，缺省回退到 ``general``。
    """

    general: str = "庄重质朴"                              # 总体文风一句话
    by_genre: dict[str, str] = Field(default_factory=dict)  # 体裁→笔法说明

    def for_genre(self, genre: str) -> str:
        """取某体裁的笔法说明；缺省回退到 general。"""
        return self.by_genre.get(genre) or self.general


class Person(BaseModel):
    """重要人物。每个文明只维护少数几位，用于给叙事提供「角色」。

    ``role`` 承载具体的身份头衔（如「航海长」「角斗士」），由 LLM 提议、
    从文明 ``role_pool`` 抽取；``social_class`` 是该身份所属的核心阶层（枚举），
    供确定性逻辑判断。``bio`` 留空/简短是有意的：丰满生平交给生成器即兴展开，
    不持久化进状态——避免长篇大论污染结构化数据、也避免跨 tick 不一致。
    """

    id: str                       # 唯一标识，形如 ``norheim-p1-25``
    name: str                     # 显示名
    role: str                     # 具体身份头衔，如 "航海长"/"角斗士"
    social_class: SocialClass = SocialClass.COMMONER  # 所属核心阶层（确定性逻辑用）
    civ_id: str                   # 所属文明 id
    birth_year: int               # 出生年
    death_year: Optional[int] = None  # 卒年；``None`` 表示仍在世
    cause_of_death: Optional[str] = None  # 死因，如 "年迈"/"饥荒"/"瘟疫"/"战死"；死后写入，约束叙事不得矛盾
    max_age: Optional[int] = None  # 个人自然寿限；进入必死窗口时按文明寿命区间随机定，到该年龄必死
    age_note: str = ""            # 年龄段提示，如 "老"/"少年"——给生成器挑视角用，非权威状态
    bio: str = ""                 # 简短备注，生成器可扩展（不作为权威状态）


class Civilization(BaseModel):
    """一个文明。引擎每个 tick 更新它的数值字段，生成器读取它来产出档案。

    字段分四组：人口经济、科技进度、文化政治、外交与人物。注释里标注了
    引擎在何处修改它们，方便协作者追踪数据流。
    """

    id: str
    name: str
    biome: Biome
    # --- 人口与经济（engine._rules_step 维护）---
    population: int = 1000        # 人口；随粮储/稳定增减
    food: float = 100.0           # 粮食盈余储备（单位：年-口粮）；<1 触发饥荒事件
    wealth: float = 50.0          # 财富；用于资助科研、受贸易/战争影响
    # --- 科技进度（engine._rules_step 维护）---
    tech_level: TechLevel = TechLevel.STONE
    tech_progress: float = 0.0    # 0..100，满 100 升一级并清零
    # --- 文化与政治 ---
    government: Government = Government.TRIBAL    # engine._maybe_evolve_government 演化
    religion: str = "animism"                     # 自由文本，由 config 设定，叙事用
    culture_traits: list[str] = Field(default_factory=list)  # 文化特质标签，叙事用
    # --- 命名与社会结构 ---
    naming: NamingStyle = Field(default_factory=NamingStyle)  # 命名规范，可演化（见 naming.NameGenerator）
    social_classes: list[SocialClass] = Field(default_factory=lambda: [SocialClass.COMMONER])  # 已解锁核心阶层
    role_pool: list[str] = Field(default_factory=list)  # 已涌现的具体身份头衔（LLM 提议扩充），spawn 时抽取
    voice: VoiceStyle = Field(default_factory=VoiceStyle)  # 官方文风（按体裁），可演化（见 naming.VoiceReformer）
    # --- 外交：对方 civ_id -> 本文明对其的立场 ---
    relations: dict[str, Relation] = Field(default_factory=dict)  # engine._diplomacy_tick 维护
    # --- 人物 ---
    people: list[Person] = Field(default_factory=list)  # engine._emerge/_spawn_person 维护
    # --- 簿记 ---
    stability: float = 60.0       # 0..100 稳定度；<25 触发动荡事件
    founded_year: int = 0
    color: str = "white"          # 预留给未来地图/渲染着色


class Event(BaseModel):
    """一条事件。事件是「状态变化」与「叙事」之间的桥梁：

    引擎产生事件（涌现）、玩家产生事件（注入），统一进 ``World.events`` 时间线；
    生成器再依据本 tick 的事件列表产出档案。``magnitude`` 控制叙事权重，
    例如经文只在 ``magnitude >= 1.5`` 的事件上生成。
    """

    year: int
    title: str
    description: str
    involved_civs: list[str] = Field(default_factory=list)  # 涉及的文明 id
    magnitude: float = 1.0        # 0..N，越大越值得浓墨重彩
    source: str = "emergent"      # "emergent"(规则涌现) | "player"(玩家注入) | "system"


class Era(BaseModel):
    """纪元。历法本身也是一种档案：玩家读到「黎明纪」「铁血纪」时即感知时代。

    目前仅作为展示用标签；未来可让规则在不同纪元有不同行为。
    """

    name: str
    start_year: int
    end_year: Optional[int] = None  # ``None`` 表示进行中
    description: str = ""


class Fact(BaseModel):
    """既成事实台账 —— 不可逆的权威记录，约束叙事不得违背。

    事件（Event）是「发生了什么」的流水；事实（Fact）是「由此确立的、
    以后永远成立的状态」。区别在于：事件会被时间线截断、会被遗忘；
    而事实一旦写入，生成器在产出档案时必须遵守，且违反即视为叙事错误。

    典型用途：
    - 人物死亡：某人于某年因某因辞世 → 之后任何档案不得让此人说话/行动/写日记，
      也不得改写其死因（如已记年迈，不得再写战死）。
    - 战争胜负：A 于某年战胜 B → 不得再写 B 此役取胜。
    - 政体更替：某文明某年改共和 → 不得再写其为君主。
    - 命名变革（naming_reform）：某文明某年改用……命名。
    - 身份涌现（role_emergence）：某文明某年出现『角斗士』这一身份。
    - 文风变革（voice_reform）：某文明某年编年史笔法改为……。

    ``scope`` 给出生效范围；``holds_until`` 留给"暂时性事实"（如停战），
    永久事实为 None。
    """

    id: str
    kind: str            # "death" | "victory" | "regime_change" | "treaty" | ...
    year: int            # 事实成立的年份
    subject: str         # 主语，如人物 id 或文明 id
    statement: str       # 人类可读的陈述句，直接喂给 LLM 作为硬约束
    scope: str = "world"  # 生效范围："world" | civ_id | person_id
    holds_until: Optional[int] = None  # None=永久；否则到该年失效


class World(BaseModel):
    """完整世界状态。整个模拟的可序列化根。

    一次 ``tick()`` 的流程：推进 ``year``/``tick_count`` → 规则更新各 civ →
    产生新事件并入 ``events``（同时在不可逆节点写 ``facts``）→ 生成档案并经校验 →
    把事件一行摘要压入 ``chronicle``。
    ``chronicle`` 是滚动记忆，只保留最近若干条，喂给生成器作为上下文；
    ``facts`` 是既成事实台账，永久约束叙事（见 :class:`Fact`）。

    随机性：``seed == 0`` 时每次开局真随机；填正整数则该局可复现（见 engine 播种逻辑）。
    """

    name: str
    year: int = 0                 # 当前世界年
    tick_count: int = 0           # 已推进的 tick 数
    seed: int = 0                 # 0=每次随机；正整数=可复现
    years_per_tick: int = 25      # 一个 tick 推进多少年；粗粒度让档案可读
    continents: list[str] = Field(default_factory=list)  # 仅展示用的大陆名
    civs: list[Civilization] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)       # 全时间线（截断到最近 500 条）
    eras: list[Era] = Field(default_factory=list)
    chronicle: list[str] = Field(default_factory=list)      # 滚动摘要，最近 40 条
    pending_events: list[Event] = Field(default_factory=list)  # 玩家已排队、下个 tick 消费
    # 既成事实台账：不可逆权威记录，约束叙事。见 Fact 文档。
    facts: list[Fact] = Field(default_factory=list)

    def civ(self, civ_id: str) -> Civilization:
        """按 id 取文明；找不到则抛 ``KeyError``（属编程错误，应早暴露）。"""
        for c in self.civs:
            if c.id == civ_id:
                return c
        raise KeyError(f"unknown civilization {civ_id!r}")

    def living_people(self) -> list[Person]:
        """返回所有在世人物（跨文明）。供生成器挑选日记作者。"""
        out = []
        for c in self.civs:
            for p in c.people:
                if p.death_year is None or p.death_year > self.year:
                    out.append(p)
        return out

    def active_facts(self, year: int | None = None, scope: str | None = None) -> list[Fact]:
        """返回在 ``year`` 年仍生效的事实（可按 scope 过滤）。

        生成器调用此方法把相关事实注入 prompt，作为叙事硬约束。
        "生效"判定：year >= fact.year 且（holds_until 为 None 或 year < holds_until）。
        """
        y = self.year if year is None else year
        out = []
        for f in self.facts:
            if y < f.year:
                continue
            if f.holds_until is not None and y >= f.holds_until:
                continue
            if scope is not None and f.scope not in ("world", scope):
                continue
            out.append(f)
        return out
