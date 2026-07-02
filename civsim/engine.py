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
    Organization,
    OrgType,
    Person,
    Relation,
    SocialClass,
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

# 按社会发展水平（TechLevel）的寿命区间：(衰老起始, 死亡窗口下界, 死亡窗口上界)。
# 设计意图：石器/青铜时代均寿短，越往后医疗与社会组织进步、寿命上界抬升。
# - age < 衰老起始：无自然死亡风险。
# - 衰老起始 <= age < 窗口下界：逐年递增的衰老死亡概率。
# - 窗口下界 <= age <= 窗口上界：进入「必死窗口」，每人在此区间内随机取一个
#   个人上限年，到该年必死——避免「到固定某一年集体死亡」的不自然，年龄散布在窗口内。
# - age > 窗口上界：强制死亡（兜底，理论上不会触发，因个人上限 <= 窗口上界）。
# 这是可调设计旋钮：改数字即调整各时代寿命图景。
# 注：当前不预留"个别个体打破年龄上界"的特殊设定（如长生者），后续如需再加。
LIFESPAN_BY_TECH = {
    TechLevel.STONE: (40, 50, 60),        # 石器：40起衰，50-60必死窗口
    TechLevel.BRONZE: (45, 55, 68),       # 青铜
    TechLevel.IRON: (50, 60, 75),         # 铁器
    TechLevel.MEDIEVAL: (55, 65, 82),     # 中古
    TechLevel.RENAISSANCE: (60, 70, 88),  # 文艺复兴
    TechLevel.INDUSTRIAL: (65, 75, 95),   # 工业化
}

# 身份关键词 → 意外死亡风险倍率。按 role 头衔含的关键词匹配，反映职业危险性：
# 渔民/海员风险最高（海难），工匠/矿冶次之（矿塌、灼伤），贵族/祭司最低（远离险地）。
# 这是可调设计旋钮：改数字即调整各职业意外风险。配合 SOCIAL_CLASS_BASE 叠加。
ROLE_RISK = {
    "渔": 3.0, "海": 3.0, "舟": 2.5, "航": 2.5,          # 涉海职业：海难高发
    "矿": 2.5, "冶": 2.0, "铸": 1.8, "铁": 1.6,          # 矿冶：塌方、灼伤
    "兵": 2.2, "卫": 1.8, "军": 2.0, "战": 2.2,          # 军人：阵亡
    "商": 1.5, "行": 1.3,                                 # 行商：遇盗、路途
    "猎": 1.8, "樵": 1.5,                                 # 山野：野兽、坠崖
    "农": 1.0, "陶": 1.1, "织": 1.0, "牧": 1.2,          # 农耕：相对安稳
    "官": 0.6, "廷": 0.6, "议": 0.7,                      # 官僚：远离险地
    "祭": 0.5, "神": 0.6, "史": 0.5, "学": 0.5,           # 神职/学者：最安
    "贵": 0.4, "王": 0.5, "督": 0.6,                     # 贵族：最低
}

# 核心阶层 → 意外风险基准倍率（与 ROLE_RISK 相乘）。军人阶层固有风险，边缘阶层生存条件恶劣。
SOCIAL_CLASS_BASE = {
    SocialClass.SOLDIER: 1.5,    # 职业暴力，和平时期也常意外
    SocialClass.MARGINAL: 1.4,   # 流民/奴隶：无保障、危险营生
    SocialClass.OUTSIDER: 1.3,   # 外乡人：路途风险
    SocialClass.ARTISAN: 1.0,
    SocialClass.COMMONER: 1.0,
    SocialClass.NOBILITY: 0.7,
    SocialClass.CLERGY: 0.8,
}

# 意外死因按身份关键词映射（意外死亡路径用，区别于事件级死因）。
ACCIDENT_CAUSE = {
    "渔": "海难", "海": "海难", "舟": "覆舟", "航": "海难",
    "矿": "矿塌", "冶": "炉祸", "铸": "炉祸", "铁": "锻造之祸",
    "兵": "阵亡", "卫": "械斗", "军": "阵亡", "战": "战死",
    "商": "遇盗", "行": "路途之祸", "猎": "兽袭", "樵": "坠崖",
    "农": "田野之祸", "陶": "窑塌", "织": "走水", "牧": "畜伤",
    "官": "急病", "廷": "急病", "议": "急病",
    "祭": "暴病", "神": "暴病", "史": "暴病", "学": "暴病",
    "贵": "急病", "王": "急病", "督": "急病",
}


def _role_risk(role: str, sclass: SocialClass) -> float:
    """取该身份的意外风险倍率 = 阶层基准 × 命中的最高 role 关键词倍率。

    role 是自由文本头衔（如「航海长」），可能命中多个关键词（「航」「长」），取最高；
    无命中则用 1.0。反映「渔民出海意外远大于农民」的直觉。
    """
    role_mult = 1.0
    for kw, mult in ROLE_RISK.items():
        if kw in role:
            role_mult = max(role_mult, mult)
    return role_mult * SOCIAL_CLASS_BASE.get(sclass, 1.0)


def _accident_cause(role: str) -> str:
    """按 role 关键词取意外死因；无命中回退「意外亡故」。"""
    for kw, cause in ACCIDENT_CAUSE.items():
        if kw in role:
            return cause
    return "意外亡故"


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
        from .naming import (AppellationJudge, BioSummarizer, NameGenerator,
                             OrgMemberInferrer, OrgProposer, OrgPurger,
                             PersonPurger, RoleProposer, VoiceReformer)

        self.factory = ArtifactFactory(self.provider)
        # 命名生成器、角色提议器、文风审定器：LLM 即兴生成，mock 兜底。与叙事生成共用同一 provider。
        self.name_gen = NameGenerator(self.provider, rng=self.rng)
        self.role_proposer = RoleProposer(self.provider, rng=self.rng)
        self.voice_reformer = VoiceReformer(self.provider, rng=self.rng)
        # 经历精炼器 + 离世清理器：同样 LLM 即兴、mock 兜底。
        self.bio_summarizer = BioSummarizer(self.provider, rng=self.rng)
        self.purger = PersonPurger(self.provider, rng=self.rng)
        # 称谓判别器：LLM 判断真名 vs 称谓，规则兜底（避免规则误杀真名或漏判称谓）。
        self.appellation_judge = AppellationJudge(self.provider, rng=self.rng)
        # 社会组织：涌现提议器 + 清理器 + 成员归入推断器。
        self.org_proposer = OrgProposer(self.provider, rng=self.rng)
        self.org_purger = OrgPurger(self.provider, rng=self.rng)
        self.org_inferrer = OrgMemberInferrer(rng=self.rng)
        # 把建档回调注入工厂：文本抽取到新人物时，走 register_commoner 挂上寿命/死因机制。
        self.factory.set_register_callback(self.register_commoner)
        # 把称谓判别器注入工厂：CAST 抽取时判定名字是否称谓。
        self.factory.set_appellation_judge(self.appellation_judge)
        # 把组织归入回调注入工厂：新人物建卡时按阶层/CAST 所属组织归入组织。
        self.factory.set_org_inferrer(self.org_inferrer)
        self.factory.set_assign_org_cb(self.assign_to_org)

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

        # 离世清理：已故且长期未被提及的人物卡归档删除，防全员建档下的状态膨胀。
        # 删卡前写 person_archive Fact 存其关键信息，保证历史一致性。
        self._purge_dead_unreferenced(w)
        # 组织清理：阶段1暂无解散触发，留接口待阶段后续。
        # self._purge_dissolved_orgs(w)

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
        leveled_up = False
        if c.tech_progress >= 100 and c.tech_level != TechLevel.INDUSTRIAL:
            c.tech_level = TechLevel(c.tech_level.value + 1)
            c.tech_progress = 0.0
            leveled_up = True

        # --- 稳定度：向「政体均衡点」缓慢漂移（一阶趋近）---
        # 共和制均衡最高(70)、帝国最低(58)——体现「大帝国难维系」的直觉。
        eq = {"tribal": 55, "chiefdom": 60, "monarchy": 65, "republic": 70,
              "theocracy": 68, "empire": 58}.get(c.government.value, 60)
        c.stability += (eq - c.stability) * 0.1  # 每tick朝均衡点走 10%
        c.stability = max(0.0, min(100.0, c.stability))

        # 政体演化：在粗阈值上自动跃迁（见 _maybe_evolve_government）。
        prev_gov = c.government
        self._maybe_evolve_government(c)
        gov_changed = c.government != prev_gov

        # 命名规范与角色身份的演化：科技升级/政体更替时触发（见对应方法）。
        if leveled_up:
            self._on_tech_up(c)
        if gov_changed:
            self._on_gov_change(c)

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

    # -------------------------------------------------------- naming/role 演化

    def _on_tech_up(self, c: Civilization) -> None:
        """科技升级时：调整命名规范（加新意象词根）+ 提议新身份 + 解锁阶层 + 文风微调 + 涌现组织。

        规则给基础演化（确定性），LLM 提议新身份/文风/组织（涌现）。各步都写 Fact，
        使「命名/社会结构/文风的变迁」成为可见的文明史。
        """
        trigger = f"tech_{c.tech_level.name.lower()}"
        self._maybe_evolve_naming(c, kind="tech", trigger=trigger)
        self._maybe_propose_roles(c, trigger)
        self._maybe_evolve_voice(c, trigger)
        self._maybe_propose_orgs(c, trigger)

    def _on_gov_change(self, c: Civilization) -> None:
        """政体更替时：调整命名风格说明 + 提议新身份 + 解锁/调整阶层 + 文风微调 + 涌现组织。"""
        trigger = f"gov_{c.government.value}"
        self._maybe_evolve_naming(c, kind="gov", trigger=trigger)
        self._maybe_propose_roles(c, trigger)
        self._unlock_classes_for_government(c)
        self._maybe_evolve_voice(c, trigger)
        self._maybe_propose_orgs(c, trigger)

    def _maybe_propose_orgs(self, c: Civilization, trigger: str) -> None:
        """关键事件时提议涌现新社会组织，建 Organization 并写 Fact(kind="org_emergence")。

        LLM 提议契合文明新阶段的组织（如青铜时代→铜匠行会、君主制→王廷），引擎建卡。
        人口过阈值的 SETTLEMENT 升级（村→镇）不在此处，留后续。mock 兜底按触发关键词给候选。
        """
        try:
            proposals = self.org_proposer.propose(c, trigger, count=2)
        except Exception as exc:
            print(f"[civsim] org_proposer 失败 ({exc})，跳过。")
            return
        w = self.world
        for p in proposals:
            name = p.get("name", "")
            otype = p.get("org_type", "other")
            try:
                ot = OrgType(otype)
            except ValueError:
                ot = OrgType.OTHER
            parent_name = (p.get("parent_name") or "").strip()
            parent_id = None
            if parent_name:
                for o in c.organizations:
                    if o.name == parent_name:
                        parent_id = o.id
                        break
            oid = f"{c.id}-org{len(c.organizations)+1}-{w.year}"
            org = Organization(
                id=oid, name=name, org_type=ot, civ_id=c.id, parent_org_id=parent_id,
                founded_year=w.year, last_mentioned_year=w.year,
                history_entries=[f"{w.year}年：以{ot.value}之姿涌现于{c.name}（{p.get('scale_note','')}）。"],
            )
            c.organizations.append(org)
            w.facts.append(Fact(
                id=f"org-emergence-{oid}", kind="org_emergence", year=w.year,
                subject=c.id, scope=c.id,
                statement=f"{w.year} 年 {c.name} 涌现社会组织「{name}」（{ot.value}）。",
            ))

    def _unlock_classes_for_government(self, c: Civilization) -> None:
        """按政体解锁核心阶层——让「谁能说话」反映社会形态。

        如君主制补 NOBILITY/CLERGY；共和制补 COMMONER；神权制补 CLERGY；
        帝国补 NOBILITY/SOLDIER；部落/酋邦保留简朴。奴隶/边缘阶层 MARGINAL
        在低级政体或动荡社会才解锁。
        """
        gov_classes = {
            Government.TRIBAL: [SocialClass.COMMONER, SocialClass.SOLDIER],
            Government.CHIEFDOM: [SocialClass.COMMONER, SocialClass.SOLDIER, SocialClass.NOBILITY],
            Government.MONARCHY: [SocialClass.NOBILITY, SocialClass.COMMONER, SocialClass.SOLDIER, SocialClass.CLERGY],
            Government.REPUBLIC: [SocialClass.NOBILITY, SocialClass.COMMONER, SocialClass.ARTISAN, SocialClass.SOLDIER],
            Government.THEOCRACY: [SocialClass.CLERGY, SocialClass.NOBILITY, SocialClass.COMMONER, SocialClass.MARGINAL],
            Government.EMPIRE: [SocialClass.NOBILITY, SocialClass.SOLDIER, SocialClass.COMMONER, SocialClass.OUTSIDER],
        }.get(c.government, [SocialClass.COMMONER])
        for sc in gov_classes:
            if sc not in c.social_classes:
                c.social_classes.append(sc)

    def _maybe_evolve_naming(self, c: Civilization, kind: str, trigger: str) -> None:
        """按规则调整命名规范，并写 ``Fact(kind="naming_reform")`` 记录。

        演化是有意的「轻触」：只在词库追加少量契合新阶段的意象词根、微调
        style_note，不重写整套规范——保留文明命名连续性。
        """
        ns = c.naming
        year = self.world.year
        additions: list[str] = []
        if kind == "tech":
            # 每个科技阶段引入对应意象词根（金属/工艺/学术）。
            tech_roots = {
                TechLevel.BRONZE: ["铜", "锡", "铸"],
                TechLevel.IRON: ["铁", "刃", "炉"],
                TechLevel.MEDIEVAL: ["堡", "纹", "约"],
                TechLevel.RENAISSANCE: ["翰", "星", "卷"],
                TechLevel.INDUSTRIAL: ["机", "烟", "轮"],
            }.get(c.tech_level, [])
            for r in tech_roots:
                if r not in ns.roots:
                    ns.roots.append(r)
                    additions.append(f"词根「{r}」")
        elif kind == "gov":
            gov_notes = {
                Government.MONARCHY: "王权时代，名字常带氏族与封地",
                Government.REPUBLIC: "共和公民名，去贵族前缀、尚简朴",
                Government.THEOCRACY: "神权时代，名字多取圣徒与神迹意象",
                Government.EMPIRE: "帝国时代，名字尚武功与行省",
            }.get(c.government, "")
            if gov_notes:
                ns.style_note = (ns.style_note + "；" if ns.style_note else "") + gov_notes
                additions.append(f"风格说明更新为「{gov_notes}」")
        if additions:
            w = self.world
            w.facts.append(Fact(
                id=f"naming-{c.id}-{year}-{kind}", kind="naming_reform", year=year,
                subject=c.id, scope=c.id,
                statement=f"{year} 年 {c.name} 命名规范变更：{'、'.join(additions)}。"
                          f"此后该文明人物命名依新规范。",
            ))

    def _maybe_propose_roles(self, c: Civilization, trigger: str) -> None:
        """调 ``RoleProposer`` 提议新身份，加入 ``role_pool`` 并写 ``Fact``。

        LLM 提议契合文明现状的新头衔（如科技进青铜→「铜匠」），引擎去重后落库。
        role_pool 的增长本身即文明社会演化的可见记录。mock 模式给固定候选。
        """
        try:
            proposals = self.role_proposer.propose(c, trigger, count=2)
        except Exception as exc:  # LLM 抖动不拖垮 tick
            print(f"[civsim] role_proposer 失败 ({exc})，跳过。")
            return
        w = self.world
        for title, sc in proposals:
            if title in c.role_pool:
                continue
            c.role_pool.append(title)
            w.facts.append(Fact(
                id=f"role-{c.id}-{w.year}-{title}", kind="role_emergence", year=w.year,
                subject=c.id, scope=c.id,
                statement=f"{w.year} 年 {c.name} 出现「{title}」这一身份"
                          f"（属{sc.value}阶层）。",
            ))

    def _maybe_evolve_voice(self, c: Civilization, trigger: str) -> None:
        """调 ``VoiceReformer`` 提议文风微调，更新 ``civ.voice`` 并写 ``Fact``。

        LLM 提议契合新阶段的笔法调整（如共和后编年史去颂圣），引擎采纳后更新
        ``by_genre``。同系列文本因此风格连贯，文风变迁也成为可见文明史。
        mock 模式按触发关键词给固定候选。无调整则不写 Fact。
        """
        try:
            changes = self.voice_reformer.propose(c, trigger)
        except Exception as exc:  # LLM 抖动不拖垮 tick
            print(f"[civsim] voice_reformer 失败 ({exc})，跳过。")
            return
        if not changes:
            return
        w = self.world
        for genre, note in changes.items():
            c.voice.by_genre[genre] = note
        summary = "、".join(f"{g}笔法改为「{n}」" for g, n in changes.items())
        w.facts.append(Fact(
            id=f"voice-{c.id}-{w.year}-{trigger}", kind="voice_reform", year=w.year,
            subject=c.id, scope=c.id,
            statement=f"{w.year} 年 {c.name} 文风变更：{summary}。",
        ))

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
            # 名人出世：每 tick 15% 概率。名人数上限放宽到 8（平民由抽取建档+清理机制管理，
            # 不在此限）。控制 notable 状态膨胀，但不卡死文本涌现的配角。
            notable_count = sum(1 for p in c.people if p.kind == "notable" and p.death_year is None)
            if self.rng.random() < 0.15 and notable_count < 8:
                p = self._spawn_person(c, w.year)
                c.people.append(p)
                events.append(Event(
                    year=real_year(), title=f"{p.name} 崭露头角",
                    description=f"{p.name} 以 {p.role} 之姿出现在 {c.name} 的历史中。",
                    involved_civs=[c.id], magnitude=1.0, source="emergent",
                ))
            # 名人辞世：按所属文明的社会发展水平（TechLevel）决定寿命。
            # 区间为 (衰老起始, 必死窗口下界, 必死窗口上界)。进窗口后每人在窗口内
            # 随机取一个个人寿限 max_age，到该年龄必死——年龄散布在窗口内，非一刀切。
            age_start, cap_lo, cap_hi = LIFESPAN_BY_TECH.get(c.tech_level, (50, 60, 75))
            for p in list(c.people):
                if p.death_year is not None:
                    continue
                age = w.year - p.birth_year
                # 进入必死窗口时，给此人定一个个人寿限（窗口内随机）。
                # 取 [cap_lo+1, cap_hi] 保证进窗口后至少活 1 年，不致「刚进窗口即死」。
                if p.max_age is None and age >= cap_lo:
                    p.max_age = self.rng.randint(cap_lo + 1, cap_hi) if cap_hi > cap_lo else cap_lo
                in_decline = age >= age_start
                in_window = p.max_age is not None  # 已进必死窗口
                must_die = in_window and age >= p.max_age
                # 概率：衰老窗口内递增；必死窗口内更高（几年内必死，但非立即）。
                chance = 0.0
                if in_window:
                    # 窗口内：随年龄接近 max_age 升至 ~0.6/tick，保证几年内死。
                    span = max(1, p.max_age - cap_lo)
                    chance = 0.25 + 0.35 * (age - cap_lo) / span
                elif in_decline:
                    span = max(1, cap_lo - age_start)
                    chance = 0.06 + 0.20 * (age - age_start) / span
                if must_die or ((in_decline or in_window) and self.rng.random() < chance):
                    # 死亡年龄须与判定一致：从 [age 起点下界, 当前age] 内取一个死亡年龄，
                    # 再 dyear = birth + death_age。避免「判定时 52 岁、记录成 38 岁」
                    # 的脱节（旧实现用 real_year() 区间随机年导致记录年龄偏低且可低于 age_start）。
                    if must_die:
                        death_age = p.max_age
                    elif in_window:
                        death_age = self.rng.randint(cap_lo, max(cap_lo, age))
                    else:  # in_decline
                        death_age = self.rng.randint(age_start, max(age_start, age))
                    dyear = p.birth_year + death_age
                    # 区间边界保护：dyear 不应早于本 tick 起点，也不晚于当前年。
                    dyear = max(prev_year, min(dyear, w.year))
                    p.death_year = dyear
                    cause = self._death_cause(c, p, events)
                    p.cause_of_death = cause
                    events.append(Event(
                        year=dyear, title=f"{p.name} 辞世",
                        description=f"{c.name} 的 {p.role} {p.name} 于 {dyear} 年{cause}离世，"
                                    f"享年 {dyear - p.birth_year}。",
                        involved_civs=[c.id], magnitude=0.8, source="emergent",
                    ))
                    # 写入既成事实：此人死亡是永久约束，含死因，其后任何档案不得让此人
                    # 说话/行动，也不得改写其死因。
                    w.facts.append(Fact(
                        id=f"death-{p.id}-{dyear}", kind="death", year=dyear,
                        subject=p.id, scope=p.id,
                        statement=f"{c.name}的{p.name}（{p.role}）已于 {dyear} 年因{cause}辞世，"
                                  f"此后不得再以该人物视角写日记、发言或行动，"
                                  f"亦不得改写其死因为其他。",
                    ))

        # 文明间外交：≥2 文明时，25% 概率触发一次互动。
        if len(w.civs) >= 2 and self.rng.random() < 0.25:
            events.extend(self._diplomacy_tick(w, prev_year))

        # 非自然死亡：本 tick 区间内，名人可能因「身份相关意外」或「事件级致死」提前死。
        # 这填补了「战争/瘟疫只削文明数值不杀人物」与「无职业风险」两个缺口。
        # 注意：在自然衰老死亡循环之后调用，已死人物不会重复处理。
        events.extend(self._maybe_kill_notables(w, events, prev_year))
        return events

    def _kill_person(self, w: World, c: Civilization, p: Person, dyear: int, cause: str,
                    events: list[Event]) -> None:
        """统一的人物死亡落账：设 death_year/cause、记事件、写 Fact。

        自然衰老死亡与事件/意外死亡都走这里，保证死因记录口径一致。
        """
        p.death_year = dyear
        p.cause_of_death = cause
        events.append(Event(
            year=dyear, title=f"{p.name} 辞世",
            description=f"{c.name} 的 {p.role} {p.name} 于 {dyear} 年{cause}离世，"
                        f"享年 {dyear - p.birth_year}。",
            involved_civs=[c.id], magnitude=1.2, source="emergent",
        ))
        w.facts.append(Fact(
            id=f"death-{p.id}-{dyear}", kind="death", year=dyear,
            subject=p.id, scope=p.id,
            statement=f"{c.name}的{p.name}（{p.role}）已于 {dyear} 年因{cause}辞世，"
                      f"此后不得再以该人物视角写日记、发言或行动，亦不得改写其死因。",
        ))

    def _maybe_kill_notables(self, w: World, recent_events: list[Event], prev_year: int) -> list[Event]:
        """非自然死亡：身份相关意外 + 事件级致死（战争/瘟疫/饥荒）。

        两类来源，每个在世名人都查：
        1. **事件级致死**：若该文明本 tick 有战争/瘟疫/饥荒事件，涉事名人按事件类型概率被点名
           （战争 25%、瘟疫 30%、饥荒 15%），死因为「战死/瘟疫/饥荒」。这让重大事件真正带走人物。
        2. **身份相关意外**：每 tick 按「基础概率 × 阶层基准 × role 风险倍率」判定，反映
           渔民出海、矿工塌方、士兵阵前等职业风险。死因按 role 关键词映射（如渔→海难）。

        被点名死亡时，dyear 取区间内随机年；同 tick 一人至多死一次（死后跳过）。
        """
        events: list[Event] = []
        # 收集本 tick 各文明的事件致死触发。
        event_risks: dict[str, list[tuple[float, str]]] = {}  # civ_id -> [(概率, 死因)]
        for e in recent_events:
            for cid in e.involved_civs:
                title = e.title + e.description
                if "瘟疫" in title:
                    event_risks.setdefault(cid, []).append((0.30, "瘟疫"))
                if "饥荒" in title or "断粮" in title:
                    event_risks.setdefault(cid, []).append((0.15, "饥荒"))
                if "交战" in title or "战争" in title:
                    event_risks.setdefault(cid, []).append((0.25, "战死"))
                if "动荡" in title:
                    event_risks.setdefault(cid, []).append((0.12, "动乱"))

        ACCIDENT_BASE = 0.02   # 每 tick 基础意外概率（乘风险倍率后约 0.7%~9%/tick）。
        for c in w.civs:
            triggers = event_risks.get(c.id, [])
            for p in list(c.people):
                if p.death_year is not None:
                    continue
                # 1) 事件级致死：遍历该文明的所有触发，任一命中即死。
                # dyear 取区间内随机年；"享年"用 dyear-birth 真实计算（_kill_person 内）。
                died = False
                for prob, cause in triggers:
                    if self.rng.random() < prob:
                        dyear = self.rng.randint(prev_year, w.year)
                        self._kill_person(w, c, p, dyear, cause, events)
                        died = True
                        break
                if died:
                    continue
                # 2) 身份相关意外：基础概率 × 阶层基准 × role 风险倍率。
                risk = ACCIDENT_BASE * _role_risk(p.role, p.social_class)
                if self.rng.random() < risk:
                    cause = _accident_cause(p.role)
                    dyear = self.rng.randint(prev_year, w.year)
                    self._kill_person(w, c, p, dyear, cause, events)
        return events

    def _death_cause(self, c: Civilization, p: Person, recent_events: list[Event]) -> str:
        """按状态与近期事件给人物确定死因（规则生成，非 LLM）。

        死因必须结构化记录到 ``Person.cause_of_death`` 与 death Fact 中，后续叙事不得改写。
        当前只在自然死亡路径调用，但会参考近期状态：若文明正饥荒/动荡，则死因可能是
        "饥荒"/"动乱"，否则多为"年迈"。未来若加入战死/处决/瘟疫等人物级死亡，
        也应通过本函数或同口径写入 cause_of_death。
        """
        titles = " ".join(e.title + e.description for e in recent_events if c.id in e.involved_civs)
        if "瘟疫" in titles:
            return "瘟疫"
        if "饥荒" in titles or c.food < 1:
            return "饥荒"
        if "动荡" in titles or c.stability < 25:
            return "动乱"
        if "交战" in titles or "战争" in titles:
            return "战乱余波"
        return "年迈"

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
        """生成一位名人：名字由 ``name_gen`` 按命名规范生成，身份从 ``role_pool`` 抽取。

        立体化：随机选一个核心阶层（从该文明已解锁 ``social_classes``）+ 该阶层下
        的具体身份头衔（从 ``role_pool`` 抽，空则用「居民」）+ 年龄段（少年/壮年/老），
        写入 ``social_class``/``role``/``age_note``。出生年回拨 20–40 年。
        id 含文明+序号+年份，保证唯一；名字由 NameGenerator 去重。
        """
        # 选阶层与身份：阶层从已解锁池抽；身份从 role_pool 抽（偏好该阶层下的）。
        sclass = self.rng.choice(c.social_classes) if c.social_classes else SocialClass.COMMONER
        # role_pool 里的身份不直接带阶层信息，这里简化：随机抽一个；引擎在提议身份时
        # 已把阶层记在 Fact，此处 role 仅作展示。身份空则用泛称。
        role = self.rng.choice(c.role_pool) if c.role_pool else "居民"
        # 年龄段：影响视角质感（少年/壮年/老），写入 age_note 供生成器挑视角。
        age_note = self.rng.choice(["少年", "壮年", "壮年", "老"])
        # 性别：随机；后续若 NamingStyle.gendered 可影响命名。
        gender = self.rng.choice(["男", "女"])
        # 名字：LLM 按命名规范生成（mock 兜底随机组合），自动去重。
        names = self.name_gen.generate(c, count=1, gender=gender if c.naming.gendered else None)
        name = names[0] if names else "某人"
        pid = f"{c.id}-p{len(c.people)+1}-{year}"
        birth = year - self.rng.randint(20, 40)
        p = Person(
            id=pid, name=name, role=role, social_class=sclass, civ_id=c.id,
            gender=gender, birth_year=birth, age_note=age_note, kind="notable",
            first_seen_year=year, last_mentioned_year=year,
            bio_entries=[f"{year}年：以{role}之姿出现在{c.name}的历史中。"],
        )
        # 名人按概率归入一个组织（常为 officer）；按阶层/role/home 推断兜底。
        self.assign_to_org(p, c, home="")
        return p

    def assign_to_org(self, person: Person, civ: Civilization, home: str = "",
                      explicit_org_id: str = "") -> None:
        """把人物归入组织，建双向成员关系（组织 members + Person.orgs）。

        优先用 CAST 显式给的所属组织（``explicit_org_id``，需校验存在）；否则按
        阶层/role/home 推断（OrgMemberInferrer）。无合适组织则暂留空，等后续涌现。
        """
        org = None
        if explicit_org_id:
            for o in civ.organizations:
                if o.id == explicit_org_id and o.dissolved_year is None:
                    org = o
                    break
        if org is None:
            oid = self.org_inferrer.infer(civ, person.social_class, person.role, home)
            if oid:
                for o in civ.organizations:
                    if o.id == oid and o.dissolved_year is None:
                        org = o
                        break
        if org is None:
            return
        if person.id not in org.members:
            org.members.append(person.id)
            org.last_mentioned_year = max(org.last_mentioned_year, person.last_mentioned_year)
        if org.id not in person.orgs:
            person.orgs.append(org.id)

    def register_commoner(self, c: Civilization, name: str, role_hint: str,
                          social_class: SocialClass, year: int,
                          gender: str = "", home: str = "", traits: list[str] | None = None,
                          circumstance: str = "", relations_note: str = "") -> Person:
        """为文本中出现的平民建档（生成器抽取到新人物时调用）。

        逻辑与 _spawn_person 对齐：出生年回拨、按文明寿命区间给 max_age（参与自然死亡
        与意外死亡机制）、标 kind="commoner"。平民卡也填详细字段让视角鲜活。
        若 name 为空，按命名规范生成（mock 兜底）。返回新建的 Person（已加入 c.people）。
        """
        if not name:
            names = self.name_gen.generate(c, count=1, gender=gender if c.naming.gendered else None)
            name = names[0] if names else "某人"
        # 跨时间 + 跨文明同名防护：先查本文明（含已故未清理），再查全文明。
        # 若另一文明已有同名人物，加序号后缀避免新角色冒充旧人致年龄/经历矛盾。
        # 归并命中已有在世同名的场景不在 register（走 _resolve_persons 的 existing 分支），
        # 故此处只可能是「真新角色撞了旧名」——加序号区分。
        existing_same = [p for p in c.people if p.name == name]
        if not existing_same:
            for oc in self.world.civs:
                if oc.id == c.id:
                    continue
                existing_same = [p for p in oc.people if p.name == name]
                if existing_same:
                    break
        if existing_same:
            ordinals = ["", "·二", "·三", "·四", "·五", "·六", "·七"]
            for o in ordinals[1:]:
                # 序号去重也要跨文明查，确保「·二」在另一文明也不存在。
                candidate = f"{name}{o}"
                collision = any(p.name == candidate for civ2 in self.world.civs for p in civ2.people)
                if not collision:
                    name = candidate
                    break
        if not gender:
            gender = self.rng.choice(["男", "女"])
        _, cap_lo, cap_hi = LIFESPAN_BY_TECH.get(c.tech_level, (50, 60, 75))
        age_now = self.rng.randint(18, 55)
        birth = year - age_now
        pid = f"{c.id}-c{len(c.people)+1}-{year}"
        p = Person(
            id=pid, name=name or "某人", role=role_hint or "居民",
            social_class=social_class, civ_id=c.id, gender=gender or self.rng.choice(["男", "女"]),
            birth_year=birth, age_note=("老" if age_now > 50 else "壮年"),
            kind="commoner", home=home, traits=traits or [],
            circumstance=circumstance, relations_note=relations_note,
            first_seen_year=year, last_mentioned_year=year,
            bio_entries=[f"{year}年：以{role_hint or '居民'}身份见于记载。"],
        )
        c.people.append(p)
        # 按 home/阶层归入组织；CAST 给的显式 org_id 由生成器侧传入（见 factory 调用），
        # 这里 register 不直接接 explicit_org 参数——生成器在建卡后另行调 assign_to_org。
        self.assign_to_org(p, c, home=home)
        return p

    def add_bio_entry(self, p: Person, event_desc: str, year: int) -> None:
        """人物卷入重大事件时，精炼一句经历追加进 ``bio_entries``。LLM 即兴，mock 兜底。"""
        try:
            entry = self.bio_summarizer.summarize(p, event_desc, year)
        except Exception as exc:  # LLM 抖动不拖垮
            print(f"[civsim] bio_summarizer 失败 ({exc})，用兜底。")
            entry = f"{year}年：{event_desc[:24]}"
        if entry not in p.bio_entries:
            p.bio_entries.append(entry)

    def _purge_dead_unreferenced(self, w: World, stale_years: int = 50) -> int:
        """清理已故且长期未被提及的人物卡，防膨胀。返回清理数。

        候选：已故 + ``last_mentioned_year`` 早于 ``stale_years`` 年前。
        对每个候选调 ``PersonPurger`` 判断是否仍可能被未来文本牵连；可删则：
        先写 ``Fact(kind="person_archive")`` 存其关键信息（删卡不丢历史一致性），再删卡。
        """
        purged = 0
        for c in w.civs:
            for p in list(c.people):
                if p.death_year is None:
                    continue
                if p.last_mentioned_year == 0 or w.year - p.last_mentioned_year < stale_years:
                    continue
                # 粗判在世亲属：同文明是否有同氏族（name 含相同词根）的活人。
                living_relatives = any(
                    other.death_year is None and other.id != p.id
                    and other.name.split("·")[0] == p.name.split("·")[0]
                    for other in c.people
                )
                try:
                    if not self.purger.should_purge(p, living_relatives):
                        continue
                except Exception as exc:
                    print(f"[civsim] purger 失败 ({exc})，保留。")
                    continue
                # 删卡前存档：把关键信息写 person_archive Fact，永久约束叙事一致性。
                w.facts.append(Fact(
                    id=f"person-archive-{p.id}", kind="person_archive", year=p.death_year,
                    subject=p.id, scope=c.id,
                    statement=(f"{p.name}（{p.role}，{p.gender or '性别未定'}，"
                               f"{p.birth_year}–{p.death_year}年，死因{p.cause_of_death}）"
                               f"的人物卡已归档。经历：{'；'.join(p.bio_entries[-3:])}。"
                               f"此后若文本提及此人，须与上述一致。"),
                ))
                c.people.remove(p)
                purged += 1
        return purged

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
