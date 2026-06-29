"""
The hybrid simulation engine.

Every tick does four things, in order:

1. **Rules step** — deterministically advance each civilization's numeric
   state (population, food, wealth, tech progress, stability) using simple,
   readable rules. This is what keeps the world internally consistent.
2. **Emergence** — inspect the post-step state for threshold crossings
   (famine, tech breakthrough, unrest, war declarations) and turn them into
   :class:`Event` records. This is where "drama" enters the system
   deterministically.
3. **Player events** — consume any events the player queued via the CLI and
   fold them into the timeline, applying their mechanical effects.
4. **Narrative** — hand the new events + current state to the generators,
   which produce text artifacts via the LLM provider.

The LLM is never trusted with state math; it only narrates what the rules
already decided. That's the "hybrid" in hybrid-driven.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .models import (
    Civilization,
    Event,
    Fact,
    Government,
    Person,
    Relation,
    TechLevel,
    World,
)
from .providers import LLMProvider, get_provider


# 地貌粮食产出系数（相对基准 1.0）。平原最肥、冻土最贫瘠。
# 这些数字是有意「可调」的设计旋钮：改这里就能改变不同地貌文明的兴衰节奏，
# 无需改动逻辑。想加新地貌时，同时在此表与 models.Biome 登记。
BIOME_YIELD = {
    "coastal": 1.15,
    "plains": 1.25,
    "forest": 1.05,
    "desert": 0.70,
    "mountain": 0.85,
    "tundra": 0.60,
}

# 按社会发展水平（TechLevel）的寿命区间：(衰老起始年龄, 硬上界年龄)。
# 设计意图：石器/青铜时代均寿短，越往后医疗与社会组织进步、寿命上界抬升。
# 到「衰老起始」后逐年递增死亡概率，到「硬上界」则必死——避免出现奴隶制社会
# 活到 179 这类不合理情况。这是可调设计旋钮：改数字即调整各时代寿命图景。
# 注：当前不预留"个别个体打破年龄上界"的特殊设定（如长生者），后续如需再加。
LIFESPAN_BY_TECH = {
    TechLevel.STONE: (45, 60),        # 石器：衰老45起，最迟60
    TechLevel.BRONZE: (50, 65),       # 青铜
    TechLevel.IRON: (55, 70),         # 铁器
    TechLevel.MEDIEVAL: (60, 78),     # 中古
    TechLevel.RENAISSANCE: (65, 82),  # 文艺复兴
    TechLevel.INDUSTRIAL: (70, 88),   # 工业化
}


@dataclass
class TickReport:
    """Summary of what happened in a single tick, for CLI display."""

    year: int
    events: list[Event]
    artifacts_written: int

    @property
    def empty(self) -> bool:
        return not self.events and self.artifacts_written == 0


class Simulation:
    """Drives world state forward one tick at a time."""

    def __init__(self, world: World, provider: LLMProvider | None = None,
                 archive: "Archive | None" = None, rng: random.Random | None = None):
        self.world = world
        self.provider = provider or get_provider()
        self.archive = archive
        # 随机性播种规则（兼顾"每次开局不同"与"可复现一局"）：
        #   - 显式传入 rng：直接用，调用方完全控制（测试用）。
        #   - world.seed > 0：用该种子播种，可复现（config.yaml 指定 seed 时生效）。
        #   - world.seed == 0（默认/未指定）：用真随机播种，每次开局都不同。
        # 之所以把 0 当作"未指定"：Pydantic 模型 int 字段默认 0，无法用 None 区分，
        # 故约定 0 = 不固定。想要确定性，在 config 里写一个非零 seed。
        if rng is not None:
            self.rng = rng
        elif world.seed:
            self.rng = random.Random(world.seed)
        else:
            self.rng = random.Random()
        # Import lazily to avoid a hard dependency cycle (generators -> engine).
        from .generators import ArtifactFactory

        self.factory = ArtifactFactory(self.provider)

    # ------------------------------------------------------------------ step

    def tick(self) -> TickReport:
        """推进世界一个 tick（= ``years_per_tick`` 年）。整个模拟的心跳。

        严格四步、顺序敏感：

        1. 规则推进各文明数值 —— 状态先变，叙事才能跟上。
        2. 涌现检测 —— 读推进后的状态，把阈值跨越转为事件。
        3. 消费玩家事件 —— 玩家在上个 tick 排队的事件在此生效（含机械效果）。
        4. 叙事生成 —— 把本 tick 事件交给生成器产出档案并落盘。

        顺序为何重要：玩家事件若在规则之前消费，其效果会被本 tick 规则覆盖；
        若在涌现之前消费，则它也能触发涌现（如玩家制造饥荒→触发饥荒事件）。
        把玩家事件放在规则之后、叙事之前，是最自然的位置。

        Returns:
            ``TickReport``，含本 tick 年份、事件列表、档案数，供 CLI 展示。
        """
        w = self.world
        prev_year = w.year
        w.year += w.years_per_tick
        w.tick_count += 1

        new_events: list[Event] = []

        # 1) 规则推进：确定性更新人口/粮/财富/科技/稳定/政体。
        for civ in w.civs:
            self._rules_step(civ)

        # 2) 涌现：从推进后的状态里挑出值得记录的阈值跨越。
        new_events.extend(self._emerge(w, prev_year))

        # 3) 玩家事件：把上一轮排队的注入事件拉出来并施加机械效果。
        new_events.extend(self._consume_pending(w))

        # 记入全局时间线，并截断到最近 500 条防止无限增长（旧史在档案库里）。
        w.events.extend(new_events)
        w.events = w.events[-500:]

        # 4) 叙事：生成档案并落盘（若配置了 archive）。
        artifacts = self.factory.generate_for_tick(w, new_events)
        if self.archive is not None:
            for art in artifacts:
                self.archive.add(art)

        # 滚动记忆：每条事件压成一行摘要，喂给后续 tick 的生成器作上下文。
        # 只保留最近 40 条——更久远的历史靠档案库本身承载。
        for ev in new_events:
            line = f"Y{ev.year}: {ev.title}."
            w.chronicle.append(line)
        w.chronicle = w.chronicle[-40:]

        return TickReport(year=w.year, events=new_events, artifacts_written=len(artifacts))

    # ------------------------------------------------------------------ rules

    def _rules_step(self, c: Civilization) -> None:
        """确定性更新单个文明一个 tick 的全部数值状态。

        每段都对应一个子系统的「经济直觉」，注释解释系数含义与权衡。
        所有系数都是可调设计旋钮——改数字即可调难度/节奏，无需改结构。
        """
        yrs = self.world.years_per_tick

        # --- 粮食：产出 vs 消耗 ---
        # 产出 ∝ 人口 × 地貌系数；消耗 ∝ 人口。系数差(0.0012 vs 0.0010)制造
        # 温和盈余，使正常年景人口缓慢增长。
        yield_mult = BIOME_YIELD.get(c.biome.value, 1.0)
        production = c.population * 0.0012 * yield_mult * yrs  # 盈余单位
        consumption = c.population * 0.0010 * yrs
        c.food += production - consumption
        c.food = max(0.0, c.food)

        # --- 人口：随粮储/稳定增长，饥荒时骤减 ---
        # 增长率与「人均粮储」的平方根挂钩（边际递减），避免指数爆炸。
        if c.food > 5 and c.stability > 25:
            growth = 1 + 0.04 * (c.food / max(c.population, 1)) ** 0.5
            c.population = int(c.population * growth)
        elif c.food < 1:
            # 饥荒：人口打 85 折、稳定度扣 12，并会在 _emerge 里产生饥荒事件。
            c.population = int(c.population * 0.85)
            c.stability -= 12

        # --- 财富：随科技与贸易型政体累积 ---
        trade_boost = 1.0 + 0.15 * c.tech_level.value
        if c.government in (Government.REPUBLIC, Government.EMPIRE):
            trade_boost += 0.2  # 商业政体额外加成
        c.wealth += 5 * trade_boost

        # --- 科技：财富与人口共同资助研究，混乱（低稳定）拖慢进度 ---
        research = max(0.0, c.wealth * 0.05 + c.population * 0.0005)
        research *= 0.5 + 0.5 * (c.stability / 100.0)  # 稳定度 0→研究×0.5，100→×1.0
        c.tech_progress += research
        c.wealth = max(0.0, c.wealth - research * 0.5)  # 研究消耗一半投入的财富

        # 科技升级：满 100 进一级，清零进度；INDUSTRIAL 是上限。
        if c.tech_progress >= 100 and c.tech_level != TechLevel.INDUSTRIAL:
            c.tech_level = TechLevel(c.tech_level.value + 1)
            c.tech_progress = 0.0

        # --- 稳定度：向「政体均衡点」缓慢漂移（一阶趋近）---
        # 共和制均衡最高(70)、帝国最低(58)——体现「大帝国难维系」的直觉。
        eq = {"tribal": 55, "chiefdom": 60, "monarchy": 65, "republic": 70,
              "theocracy": 68, "empire": 58}.get(c.government.value, 60)
        c.stability += (eq - c.stability) * 0.1  # 每tick朝均衡点走 10%
        c.stability = max(0.0, min(100.0, c.stability))

        # 政体演化：在粗阈值上自动跃迁（见 _maybe_evolve_government）。
        self._maybe_evolve_government(c)

    def _maybe_evolve_government(self, c: Civilization) -> None:
        """政体在粗阈值上自动跃迁。阈值是有意的「非常粗糙」——本游戏重叙事轻数值。

        跃迁链：部落 →(人口>4000) 酋邦 →(青铜) 君主制 →(文艺复兴+稳定) 共和/神权
                →(共和+人口>50000) 帝国。改阈值即可调整政体更替节奏。
        """
        if c.government is Government.TRIBAL and c.population > 4000:
            c.government = Government.CHIEFDOM
        elif c.government is Government.CHIEFDOM and c.tech_level.value >= TechLevel.BRONZE.value:
            c.government = Government.MONARCHY
        elif c.government is Government.MONARCHY and c.tech_level.value >= TechLevel.RENAISSANCE.value:
            # 文艺复兴后依稳定度分流：高稳定→共和，低稳定→神权（乱世求神）。
            c.government = Government.REPUBLIC if c.stability > 55 else Government.THEOCRACY
        elif c.government is Government.REPUBLIC and c.population > 50000:
            c.government = Government.EMPIRE

    # ---------------------------------------------------------------- emerge

    def _emerge(self, w: World, prev_year: int) -> list[Event]:
        """检测阈值跨越并转为事件。这里给「规则」注入「戏剧性」。

        涌现基于状态阈值 + 固定概率；随机部分用 ``self.rng``（由 seed 决定），
        故同一存档可复现。新增涌现类型时，在此追加一个 if 块即可。

        Args:
            prev_year: 本 tick 推进前的年份（预留给「年度跨越」类事件，当前未用）。

        Returns:
            本 tick 涌现出的事件列表。
        """
        # 真实年份分配：本 tick 覆盖 [prev_year, w.year] 这段历史区间。
        # 涌现事件不是发生在区间末尾的"整 tick 年"，而是散落在区间内某年——
        # 这让生成器写出的日记/诏令日期是具体年份（如 17 年、63 年），而非
        # 永远卡在 25/50/75。区间内随机、可复现（用 self.rng）。
        def real_year() -> int:
            return self.rng.randint(prev_year, w.year)

        events: list[Event] = []
        for c in w.civs:
            # 饥荒：粮储 <1（与 _rules_step 的饥荒判定一致）。
            if c.food < 1:
                events.append(Event(
                    year=real_year(), title=f"{c.name} 遭遇饥荒",
                    description=f"粮储耗尽，{c.population} 居民面临断粮，多地出现流民。",
                    involved_civs=[c.id], magnitude=2.0, source="emergent",
                ))
            # 科技突破：本 tick 刚升级（progress 被清零且等级 >0）。
            if c.tech_progress == 0.0 and c.tech_level.value > 0:
                level_name = c.tech_level.name.title()
                events.append(Event(
                    year=real_year(), title=f"{c.name} 步入{level_name}时代",
                    description=f"{c.name} 的工匠与学者取得突破，技术进入 {level_name} 阶段。",
                    involved_civs=[c.id], magnitude=1.5, source="emergent",
                ))
            # 动荡：稳定度跌破 25。
            if c.stability < 25:
                events.append(Event(
                    year=real_year(), title=f"{c.name} 爆发动荡",
                    description=f"民心不稳，{c.name} 境内出现抗议与地方叛乱。",
                    involved_civs=[c.id], magnitude=2.0, source="emergent",
                ))
            # 名人出世：每 tick 15% 概率，每文明上限 6 位（控制状态膨胀）。
            if self.rng.random() < 0.15 and len(c.people) < 6:
                p = self._spawn_person(c, w.year)
                c.people.append(p)
                events.append(Event(
                    year=real_year(), title=f"{p.name} 崭露头角",
                    description=f"{p.name} 以 {p.role} 之姿出现在 {c.name} 的历史中。",
                    involved_civs=[c.id], magnitude=1.0, source="emergent",
                ))
            # 名人辞世：按所属文明的社会发展水平（TechLevel）决定寿命。
            age_start, age_cap = LIFESPAN_BY_TECH.get(c.tech_level, (55, 70))
            for p in list(c.people):
                if p.death_year is not None:
                    continue
                age = w.year - p.birth_year
                # 到硬上界必死；进衰老窗口后按年龄递增概率死亡。
                must_die = age >= age_cap
                in_decline = age >= age_start
                # 衰老窗口内：年龄越接近上界，死亡概率越高（线性插值到 ~0.5/tick）。
                chance = 0.0
                if in_decline:
                    span = max(1, age_cap - age_start)
                    chance = 0.08 + 0.42 * (age - age_start) / span
                if must_die or (in_decline and self.rng.random() < chance):
                    # 死亡年份：
                    # - 必死（到上界）：取「恰好活到上界」那年 = birth + age_cap，
                    #   而非区间随机年——否则 dyear 可能超过上界，造成「活过头」记录。
                    # - 概率死亡：取本 tick 区间内随机年（人在区间内某刻老死）。
                    dyear = (p.birth_year + age_cap) if must_die else real_year()
                    # 区间边界保护：dyear 不应早于本 tick 起点，也不晚于当前年。
                    dyear = max(prev_year, min(dyear, w.year))
                    p.death_year = dyear
                    events.append(Event(
                        year=dyear, title=f"{p.name} 辞世",
                        description=f"{c.name} 的 {p.role} {p.name} 于 {dyear} 年离世，享年 {dyear - p.birth_year}。",
                        involved_civs=[c.id], magnitude=0.8, source="emergent",
                    ))
                    # 写入既成事实：此人死亡是永久约束，其后任何档案不得让此人说话/行动。
                    w.facts.append(Fact(
                        id=f"death-{p.id}-{dyear}", kind="death", year=dyear,
                        subject=p.id, scope=p.id,
                        statement=f"{p.name}（{p.role}）已于 {dyear} 年辞世，"
                                  f"此后不得再以该人物视角写日记、发言或行动。",
                    ))

        # 文明间外交：≥2 文明时，25% 概率触发一次互动。
        if len(w.civs) >= 2 and self.rng.random() < 0.25:
            events.extend(self._diplomacy_tick(w, prev_year))
        return events

    def _diplomacy_tick(self, w: World, prev_year: int) -> list[Event]:
        """随机抽两个文明，按当前关系推进一次外交互动（交战/贸易）。

        ``prev_year`` 给出本 tick 区间左端，事件年份散落在 [prev_year, w.year] 内。
        """
        a, b = self.rng.sample(w.civs, 2)
        y = self.rng.randint(prev_year, w.year)  # 区间内真实年份
        stance = a.relations.get(b.id, Relation.NEUTRAL)
        events: list[Event] = []
        if stance is Relation.WAR and self.rng.random() < 0.5:
            # 战争：以「财富+人口」估强弱，胜者通吃部分败者资源。
            winner, loser = (a, b) if a.wealth + a.population >= b.wealth + b.population else (b, a)
            loser.wealth *= 0.7
            loser.stability -= 10
            loser.population = int(loser.population * 0.92)
            events.append(Event(
                year=y, title=f"{winner.name} 与 {loser.name} 交战",
                description=f"两军交锋，{winner.name} 取胜，{loser.name} 损失惨重。",
                involved_civs=[a.id, b.id], magnitude=2.0, source="emergent",
            ))
            # 写入既成事实：此役胜负不可逆，叙事不得翻案。
            w.facts.append(Fact(
                id=f"victory-{winner.id}-{loser.id}-{y}", kind="victory", year=y,
                subject=winner.id, scope="world",
                statement=f"{y} 年 {winner.name} 击败 {loser.name}，"
                          f"{loser.name} 损失惨重。此后叙事不得改写此役胜负。",
            ))
            # 40% 概率战后转为敌对（停火但记仇）。
            if self.rng.random() < 0.4:
                a.relations[b.id] = Relation.RIVALRY
                b.relations[a.id] = Relation.RIVALRY
        elif stance in (Relation.NEUTRAL, Relation.RIVALRY) and self.rng.random() < 0.5:
            # 中立/敌对 → 缔结贸易，双方得财富。
            a.relations[b.id] = Relation.TRADE
            b.relations[a.id] = Relation.TRADE
            a.wealth += 8
            b.wealth += 8
            events.append(Event(
                year=y, title=f"{a.name} 与 {b.name} 缔结贸易",
                description=f"商队往返于两地，{a.name} 与 {b.name} 建立贸易关系。",
                involved_civs=[a.id, b.id], magnitude=1.0, source="emergent",
            ))
        return events

    def _spawn_person(self, c: Civilization, year: int) -> Person:
        """随机生成一位名人。名字库/角色库都很小，刻意保持朴素。

        出生年回拨 20–40 年，使其出场时已是成年。id 含文明+序号+年份，保证唯一。
        名字带序号后缀（如「苏萨·诺尔海姆·二」）以避免撞名——名字库小而文明寿命长，
        不加区分会出现同名人物，导致叙事与校验混淆。
        扩充名字/角色库直接改下面两个列表即可。
        """
        roles = ["国王", "将军", "哲人", "商人", "祭司", "史官", "工程师"]
        names = ["阿兰", "苏萨", "卡恩", "伊岚", "穆克", "薇拉", "诺亚", "萨拉",
                 "伊萨", "图兰", "薇恩", "赫尔"]
        ordinals = ["", "·二", "·三", "·四", "·五", "·六", "·七"]
        role = self.rng.choice(roles)
        given = self.rng.choice(names)
        # 该文明已有多少位同名（名前缀相同）者，据此加序号后缀避免完全同名。
        same = sum(1 for p in c.people if p.name.split("·")[0] == given) if c.people else 0
        suffix = ordinals[min(same, len(ordinals) - 1)]
        name = f"{given}·{c.name}{suffix}"
        pid = f"{c.id}-p{len(c.people)+1}-{year}"
        return Person(
            id=pid, name=name, role=role, civ_id=c.id,
            birth_year=year - self.rng.randint(20, 40),
            bio=f"{c.name} 的一位 {role}。",
        )

    # ------------------------------------------------------------- player ev

    def _consume_pending(self, w: World) -> list[Event]:
        """取出上一轮排队的玩家事件，施加机械效果并返回。"""
        out: list[Event] = []
        while w.pending_events:
            ev = w.pending_events.pop(0)
            self._apply_player_event(w, ev)
            out.append(ev)
        return out

    def _apply_player_event(self, w: World, ev: Event) -> None:
        """为常见的玩家事件施加轻量机械效果。

        玩家用自由文本描述事件；这里靠关键词匹配映射到效果（瘟疫/丰收/战争/革新等）。
        命中哪个就施加哪个，可叠加；决定「影响到哪些文明」靠「文本人含文明名 或
        显式传入 involved_civs」。叙无论如何都会由生成器产出，这里只是保持状态相干。

        这是一个有意「粗糙」的映射：玩家想加更复杂的效果时，扩展关键词表即可，
        无需让玩家写结构化指令。
        """
        text = (ev.title + " " + ev.description)
        for c in w.civs:
            if c.name in text or c.id in ev.involved_civs:
                # 灾害类：减人口/稳定/粮储。
                if any(k in text for k in ("瘟疫", "饥荒", "灾害", "地震", "旱")):
                    c.population = int(c.population * 0.85)
                    c.stability -= 15
                    c.food *= 0.5
                # 繁荣类：增财富/粮储/稳定。
                if any(k in text for k in ("丰收", "繁荣", "黄金", "盛世")):
                    c.wealth += 30
                    c.food += 40
                    c.stability += 10
                # 战争类：减人口/稳定。
                if any(k in text for k in ("战争", "入侵", "征伐")):
                    c.population = int(c.population * 0.90)
                    c.stability -= 8
                # 进步类：直接推进科技进度。
                if any(k in text for k in ("革新", "发明", "启蒙")):
                    c.tech_progress += 40
        # 统一钳制稳定度到合法区间。
        for c in w.civs:
            c.stability = max(0.0, min(100.0, c.stability))

    # ------------------------------------------------------------- utilities

    def queue_player_event(self, title: str, description: str, civ_ids: list[str] | None = None) -> None:
        """CLI 注入玩家事件的公开钩子。仅入队，下个 tick 由 _consume_pending 消费。

        ``civ_ids`` 容错：传入单个字符串会被自动包成单元素列表，方便调用方
        少写一层方括号。
        """
        if isinstance(civ_ids, str):
            civ_ids = [civ_ids]
        self.world.pending_events.append(Event(
            year=self.world.year, title=title, description=description,
            involved_civs=civ_ids or [], magnitude=1.5, source="player",
        ))

    def save(self, path: str) -> None:
        """把整个 World 序列化为 JSON 存盘。父目录会自动创建。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.world.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str, provider: LLMProvider | None = None) -> "Simulation":
        """从 JSON 存档重建 Simulation。archive 需调用方在 resume 时另行注入。"""
        from pathlib import Path as _P

        w = World.model_validate_json(_P(path).read_text(encoding="utf-8"))
        return cls(w, provider=provider)


# save/load 用到的 Path 在此导入；放在文件末尾以保持上方规则逻辑的整洁。
from pathlib import Path  # noqa: E402
