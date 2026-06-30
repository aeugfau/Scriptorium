"""
人物命名、社会身份与官方文风生成（civsim.naming）。

本模块是「LLM 即兴生成 + 结构化规范约束」的接合点：

- :class:`NameGenerator` 按文明的 :class:`~civsim.models.NamingStyle`（词库+模板+
  风格说明）调 LLM 生成符合该文明气质的人名。规范是结构化的、可控、可演化；
  名字是 LLM 即兴组合的、有创意。mock 后端有兜底（从词库随机组合），零配置可跑。
- :class:`RoleProposer` 在关键事件时调 LLM 提议契合文明现状的新社会身份头衔
  （如「角斗士」「行会首脑」），并指明其所属核心阶层；引擎校验阶层合法后
  加入文明 ``role_pool``。这让文明的社会结构随时间涌现、可见地增长。
- :class:`VoiceReformer` 在关键事件时调 LLM 提议官方文风微调（如「共和后编年史
  去颂圣、记公民议」），引擎采纳后更新 ``VoiceStyle`` 并写 ``Fact``，使同系列文本
  风格连贯、文风变迁成为可见文明史。

三套生成器都走 :class:`~civsim.providers.LLMProvider`，故换后端无需改本模块。
"""

from __future__ import annotations

import random
from typing import Optional

from .models import Civilization, NamingStyle, Person, SocialClass, VoiceStyle
from .providers import GenRequest, LLMProvider


# ---------------------------------------------------------------------------
# 名字生成
# ---------------------------------------------------------------------------


class NameGenerator:
    """按文明命名规范生成人名。LLM 即兴组合，mock 兜底随机组合。"""

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def generate(self, civ: Civilization, count: int = 1, gender: Optional[str] = None) -> list[str]:
        """生成 ``count`` 个符合 ``civ.naming`` 风格的人名。

        返回去重后的名字列表（与该文明已有人物不重名；重则加序号后缀）。
        mock 后端走 ``_mock_generate``，不调 LLM。
        """
        if self.provider.name == "mock":
            names = self._mock_generate(civ, count)
        else:
            names = self._llm_generate(civ, count, gender)
        return self._dedupe(civ, names)

    def _llm_generate(self, civ: Civilization, count: int, gender: Optional[str]) -> list[str]:
        """调 LLM 按规范生成名字。要求只返回名字、每行一个。"""
        ns = civ.naming
        gender_hint = f"性别倾向：{gender}。" if ns.gendered and gender else ""
        user = (
            f"请按以下命名规范生成 {count} 个{civ.name}风格的人名。\n"
            f"风格说明：{ns.style_note or '（无特殊说明）'}\n"
            f"词根库：{ns.roots}\n前缀库：{ns.prefixes}\n后缀库：{ns.suffixes}\n"
            f"氏族名库：{ns.clans}\n组合模板：{ns.template}\n{gender_hint}\n"
            f"只返回名字，每行一个，不要编号、不要解释。"
        )
        raw = self.provider.generate(GenRequest(
            system="你是命名生成器。严格按给定词库与模板风格生成人名，"
                   "名字需朗朗上口、符合文明气质。只输出名字本身，每行一个。",
            user=user, context_docs=[], max_tokens=120,
        ))
        # 解析：每行一个名字，去掉可能的序号/标点。
        names = []
        for line in raw.splitlines():
            s = line.strip().lstrip("0123456789.、-) ").strip()
            if s and len(s) <= 40:
                names.append(s)
        # 不足则用 mock 补足，保证数量。
        if len(names) < count:
            names.extend(self._mock_generate(civ, count - len(names)))
        return names[:count]

    def _mock_generate(self, civ: Civilization, count: int) -> list[str]:
        """兜底：从词库随机填充模板占位符组合名字。零配置可跑。"""
        ns = civ.naming
        names: list[str] = []
        for _ in range(count):
            names.append(self._fill_template(ns))
        return names

    def _fill_template(self, ns: NamingStyle) -> str:
        """从词库随机取词填入 ``ns.template`` 的占位符。"""
        def pick(pool: list[str]) -> str:
            return self.rng.choice(pool) if pool else ""
        mapping = {
            "prefix": pick(ns.prefixes),
            "root": pick(ns.roots) or "某",
            "suffix": pick(ns.suffixes),
            "clan": pick(ns.clans),
            "ordinal": "",
        }
        out = ns.template
        for key, val in mapping.items():
            out = out.replace("{" + key + "}", val)
        # 折叠空占位符留下的多余分隔符。
        import re
        out = re.sub(r"[·\-]{2,}", "·", out).strip("·-")
        return out or "某"

    def _dedupe(self, civ: Civilization, names: list[str]) -> list[str]:
        """与文明已有人物去重；重名加序号后缀。"""
        existing = {p.name for p in civ.people}
        result: list[str] = []
        ordinals = ["", "·二", "·三", "·四", "·五"]
        for n in names:
            base, suffix = n, ""
            if n in existing or n in result:
                for o in ordinals[1:]:
                    if f"{n}{o}" not in existing and f"{n}{o}" not in result:
                        suffix = o
                        break
            final = f"{base}{suffix}"
            existing.add(final)
            result.append(final)
        return result


# ---------------------------------------------------------------------------
# 角色身份提议
# ---------------------------------------------------------------------------


# mock 模式按触发关键词给的兜底候选（身份头衔, 核心阶层）。
_MOCK_ROLE_CANDIDATES: dict[str, list[tuple[str, SocialClass]]] = {
    "tech_bronze": [("铜匠", SocialClass.ARTISAN), ("铸器师", SocialClass.ARTISAN)],
    "tech_iron": [("铁匠", SocialClass.ARTISAN), ("兵器师", SocialClass.ARTISAN)],
    "tech_medieval": [("行会首脑", SocialClass.ARTISAN), ("城堡工匠", SocialClass.ARTISAN)],
    "tech_renaissance": [("人文导师", SocialClass.CLERGY), ("测绘师", SocialClass.ARTISAN)],
    "tech_industrial": [("工场主", SocialClass.NOBILITY), ("机工匠", SocialClass.ARTISAN)],
    "gov_monarchy": [("廷臣", SocialClass.NOBILITY), ("近卫长", SocialClass.SOLDIER)],
    "gov_republic": [("议事代表", SocialClass.COMMONER), ("民选执事", SocialClass.COMMONER)],
    "gov_theocracy": [("异端审判官", SocialClass.CLERGY), ("神殿守卫", SocialClass.CLERGY)],
    "gov_empire": [("殖民总督", SocialClass.NOBILITY), ("军团长", SocialClass.SOLDIER)],
    "religion": [("异端", SocialClass.MARGINAL), ("新宗传教士", SocialClass.CLERGY)],
    "default": [("游方商", SocialClass.OUTSIDER), ("流民首领", SocialClass.MARGINAL)],
}


class RoleProposer:
    """在关键事件时提议契合文明现状的新社会身份头衔。

    LLM 提议（身份头衔, 所属核心阶层），引擎校验阶层合法后加入 ``civ.role_pool``。
    mock 模式按触发关键词给固定候选，零配置可跑。
    """

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def propose(self, civ: Civilization, trigger: str, count: int = 2) -> list[tuple[str, SocialClass]]:
        """提议 ``count`` 个新身份。``trigger`` 形如 ``"tech_bronze"``/``"gov_monarchy"``。

        返回 [(头衔, 阶层)]；头衔会与 ``civ.role_pool`` 去重。
        """
        if self.provider.name == "mock":
            cands = _MOCK_ROLE_CANDIDATES.get(trigger, _MOCK_ROLE_CANDIDATES["default"])
            picked = self.rng.sample(cands, min(count, len(cands)))
        else:
            picked = self._llm_propose(civ, trigger, count)
        # 去重：不与已有 role_pool 重复。
        existing = set(civ.role_pool)
        fresh = [(r, sc) for r, sc in picked if r not in existing]
        return fresh[:count]

    def _llm_propose(self, civ: Civilization, trigger: str, count: int) -> list[tuple[str, SocialClass]]:
        """调 LLM 提议身份，结构化返回。要求每行『头衔|阶层』。"""
        user = (
            f"文明：{civ.name}（政体={civ.government.value}，科技={civ.tech_level.name}，"
            f"信仰={civ.religion}，特质={civ.culture_traits}）。\n"
            f"触发情境：{trigger}（如科技升级/政体更替/宗教变革）。\n"
            f"请提议 {count} 个契合该文明现状的、尚未存在的新社会身份头衔，"
            f"并指出每个属于哪个核心阶层（从 nobility/commoner/artisan/soldier/"
            f"clergy/outsider/marginal 中选）。\n"
            f"格式：每行『头衔|阶层』，不要编号、不要解释。"
        )
        raw = self.provider.generate(GenRequest(
            system="你是社会结构生成器。提议契合文明历史情境的新身份头衔，"
                   "头衔应具体、有时代感、不与常见泛称重复。严格按格式输出。",
            user=user, context_docs=[], max_tokens=150,
        ))
        out: list[tuple[str, SocialClass]] = []
        valid = {sc.value for sc in SocialClass}
        for line in raw.splitlines():
            if "|" not in line:
                continue
            title, _, cls = line.strip().partition("|")
            title = title.strip().lstrip("0123456789.、-) ").strip()
            cls = cls.strip().lower()
            if title and cls in valid:
                out.append((title, SocialClass(cls)))
        # 不足则用 mock 补足。
        if len(out) < count:
            cands = _MOCK_ROLE_CANDIDATES.get(trigger, _MOCK_ROLE_CANDIDATES["default"])
            for r, sc in cands:
                if len(out) >= count:
                    break
                out.append((r, sc))
        return out[:count]


# ---------------------------------------------------------------------------
# 官方文风演化
# ---------------------------------------------------------------------------


# mock 模式按触发关键词给的兜底文风候选：trigger -> {genre: 笔法说明}。
# 与 RoleProposer 的兜底同思路：关键事件触发，按情境给契合的文风微调。
_MOCK_VOICE_CANDIDATES: dict[str, dict[str, str]] = {
    "gov_monarchy": {"chronicle": "纪事尚王统，多载即位、封赏、征伐", "decree": "诏令称奉天承运，语气威严"},
    "gov_republic": {"chronicle": "纪事去颂圣，载公民议、公决、法令", "decree": "政令以议事会名义颁，语气平实"},
    "gov_theocracy": {"chronicle": "纪事多引神谕、圣迹，语带敬畏", "scripture": "经文加重，多言审判与救赎"},
    "gov_empire": {"chronicle": "纪事尚武功，载行省、军团、征服", "decree": "诏令以皇帝名义，语气雄浑"},
    "tech_bronze": {"decree": "诏令多涉铸器、矿冶之政"},
    "tech_iron": {"decree": "诏令多涉兵器、甲胄之政"},
    "tech_renaissance": {"chronicle": "纪事兼载学术、星象、游历", "minutes": "议事纪要渐涉度量、测绘"},
    "tech_industrial": {"chronicle": "纪事载工场、机巧、商路", "minutes": "议事纪要涉工场、税制"},
    "religion": {"scripture": "经文转向新宗意象", "chronicle": "纪事载宗教变革与信徒动向"},
}


class VoiceReformer:
    """在关键事件时提议官方文风微调。

    LLM 提议契合新阶段的文风调整（体裁→笔法说明），引擎采纳后更新 ``civ.voice``
    并写 ``Fact(kind="voice_reform")``。mock 模式按触发关键词给固定候选，零配置可跑。

    设计意图：同系列文本（如某文明历年编年史）风格连贯，而非每篇 LLM 自由发挥；
    文风随时代微调，变迁本身成为可见文明史。
    """

    # 文风微调触及的体裁集合（限定 LLM 输出范围，避免乱改无关体裁）。
    GENRES = ("chronicle", "diary", "decree", "scripture", "minutes")

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def propose(self, civ: Civilization, trigger: str) -> dict[str, str]:
        """提议文风微调，返回 {genre: 新笔法说明}（可能为空 dict 表示无需调整）。

        ``trigger`` 形如 ``"gov_republic"``/``"tech_bronze"``。返回的 key 必在
        :attr:`GENRES` 内，引擎据此更新 ``civ.voice.by_genre``。
        """
        if self.provider.name == "mock":
            cands = _MOCK_VOICE_CANDIDATES.get(trigger, {})
            # mock 下随机选 1-2 个体裁的微调，模拟「不是每次都全改」。
            items = list(cands.items())
            self.rng.shuffle(items)
            return dict(items[: self.rng.randint(1, max(1, len(items)))]) if items else {}
        return self._llm_propose(civ, trigger)

    def _llm_propose(self, civ: Civilization, trigger: str) -> dict[str, str]:
        """调 LLM 提议文风微调，结构化返回。要求每行『体裁|新笔法说明』。"""
        current = "; ".join(f"{g}:{v}" for g, v in civ.voice.by_genre.items()) or civ.voice.general
        user = (
            f"文明：{civ.name}（政体={civ.government.value}，科技={civ.tech_level.name}，"
            f"信仰={civ.religion}）。\n"
            f"触发情境：{trigger}（政体更替/科技升级/宗教变革）。\n"
            f"当前文风：{current}\n"
            f"请提议契合新阶段的官方文风微调，从这些体裁中选：{list(self.GENRES)}。\n"
            f"只对需调整的体裁给出新笔法说明，格式：每行『体裁|新笔法说明』，不要编号、不要解释。\n"
            f"若无调整必要，输出空。"
        )
        raw = self.provider.generate(GenRequest(
            system="你是文风审定者。提议契合文明新历史阶段的官方文风微调，"
                   "笔法说明应具体可执行（如『纪事去颂圣、载公民议』），保留该文明一贯气质。"
                   "严格按格式输出。",
            user=user, context_docs=[], max_tokens=180,
        ))
        out: dict[str, str] = {}
        valid = set(self.GENRES)
        for line in raw.splitlines():
            if "|" not in line:
                continue
            genre, _, note = line.strip().partition("|")
            genre = genre.strip().lower()
            note = note.strip().lstrip("0123456789.、-) ").strip()
            if genre in valid and note:
                out[genre] = note
        return out


# ---------------------------------------------------------------------------
# 人物经历精炼
# ---------------------------------------------------------------------------


class BioSummarizer:
    """把人物卷入的事件精炼成一句经历，追加进 ``Person.bio_entries``。

    人物卡随时间「长」：每次人物被重大事件牵连（被点名死、参与战争、被诏令提及等），
    调本类生成一句精炼总结追加。mock 兜底直接用事件标题。
    """

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def summarize(self, person: Person, event_desc: str, year: int) -> str:
        """返回一句精炼经历，形如「年：...」。mock 兜底用事件描述截断。"""
        if self.provider.name == "mock":
            return f"{year}年：{event_desc[:24]}"
        raw = self.provider.generate(GenRequest(
            system="你是人物传记精炼者。把给定事件对某人物的影响浓缩成一句经历（15-30字），"
                   "只输出这一句，不要年份前缀以外的解释。",
            user=f"人物：{person.name}（{person.role}，{person.gender or '性别未定'}）。\n"
                 f"事件：{year}年 {event_desc}\n请精炼成一句该人物的经历。",
            context_docs=[], max_tokens=60,
        ))
        line = raw.strip().splitlines()[0].strip() if raw.strip() else event_desc[:24]
        return f"{year}年：{line}"


# ---------------------------------------------------------------------------
# 离世人物清理
# ---------------------------------------------------------------------------


class PersonPurger:
    """判断已故且长期未被提及的人物是否可删卡，防人物卡无限膨胀。

    全员建档下，人物卡会随时间增长；已故且很久没被任何文本牵连者可清理。
    删卡前由引擎写 ``Fact(kind="person_archive")`` 存其关键信息，保证历史一致性。
    LLM 判断「是否仍可能被未来文本牵连」（有在世亲属/重大历史意义等）；mock 兜底按规则。
    """

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def should_purge(self, person: Person, living_relatives_hint: bool) -> bool:
        """返回是否可删卡。``living_relatives_hint``：是否有在世同名/同氏族亲属的粗判。

        mock 兜底：已故且有在世亲属提示则保留，否则可删（确定性、可复现）。
        """
        if self.provider.name == "mock":
            return not living_relatives_hint
        raw = self.provider.generate(GenRequest(
            system="你是人物档案管理者。判断已故且长期未被提及的人物是否可以从活动档案中清理"
                   "（其关键信息已另存既成事实）。判断依据：是否仍可能被未来文本牵连——"
                   "如有在世亲属、是重大历史人物则保留。只输出『可删』或『保留』。",
            user=f"人物：{person.name}（{person.role}，{person.gender or '性别未定'}），"
                 f"{person.birth_year}–{person.death_year}年，死因{person.cause_of_death}。"
                 f"经历：{person.bio_entries[:3]}。"
                 f"有在世亲属提示：{'是' if living_relatives_hint else '否'}。"
                 f"是否可删？",
            context_docs=[], max_tokens=10,
        ))
        return "可删" in raw
