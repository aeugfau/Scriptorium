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

from .models import (Civilization, NamingStyle, Organization, OrgType,
                    Person, SocialClass, VoiceStyle)
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
        """调 LLM 按命名规范即兴生成名字。词库作风格示例，LLM 可创造新词；新词回收进词库。

        设计意图：词库不是封闭选择集，而是「风格种子 + 示例」。LLM 真正按 style_note 的
        调性即兴创造名字（可参考示例词、也可发明新词根/氏族名），而非在 15 个组合里随机挑——
        否则等于把 LLM 当随机数生成器用。LLM 造的新词根/氏族，引擎解析回收进 civ.naming，
        词库随使用增长，后续生成选择面越来越广、撞名率递降。
        """
        ns = civ.naming
        gender_hint = f"性别倾向：{gender}。" if ns.gendered and gender else ""
        user = (
            f"请为「{civ.name}」文明即兴创造 {count} 个符合其命名风格的人名。\n"
            f"命名风格说明：{ns.style_note or '（无特殊说明）'}\n"
            f"组合结构（模板）：{ns.template}\n"
            f"风格示例（词根/前缀/后缀/氏族名，仅供参考调性，不限于这些）：\n"
            f"  词根示例：{ns.roots}\n  前缀示例：{ns.prefixes}\n  后缀示例：{ns.suffixes}\n"
            f"  氏族名示例：{ns.clans}\n{gender_hint}\n"
            f"你可以、且应当发明新的词根与氏族名（贴合风格），不要只在示例里挑。\n"
            f"只返回名字，每行一个，不要编号、不要解释。"
        )
        try:
            raw = self.provider.generate(GenRequest(
                system="你是命名生成器。按给定的命名风格说明与结构模板即兴创造人名，"
                       "名字须贴合该文明的气质与风格说明（如海民喜航海意象、氏族连名、音节短促）。"
                       "鼓励发明新词根与氏族名，而非仅在示例中挑选。只输出名字本身，每行一个。",
                user=user, context_docs=[], max_tokens=150,
            ))
        except Exception:
            raw = ""
        # 解析：每行一个名字，去掉可能的序号/标点。
        names = []
        for line in (raw or "").splitlines():
            s = line.strip().lstrip("0123456789.、-) ").strip()
            if s and len(s) <= 40:
                names.append(s)
        # 回收新词：按 template 结构从生成的名字里拆出新词根/氏族，加进词库。
        self._recycle_new_lexemes(civ, names)
        # 不足（含空返回/异常）则用 mock 补足，保证数量。
        if len(names) < count:
            names.extend(self._mock_generate(civ, count - len(names)))
        return names[:count]

    def _recycle_new_lexemes(self, civ: Civilization, names: list[str]) -> None:
        """从 LLM 生成的名字里拆出新词根/氏族，回收进 civ.naming 词库。

        按 template 的占位符结构拆分生成的名字。当前实现针对最常见的
        ``{root}·{clan}`` / ``{root}`` 结构：以 ``·`` 分段，前段作 root 候选、后段作 clan 候选；
        无 ``·`` 的整体作 root 候选。仅回收「长度合理、未在词库」的新词，每类上限 30 防
        词库无限膨胀。这让词库随 LLM 创造而增长，后续生成选择面越来越广。
        """
        ns = civ.naming
        for n in names:
            if not n or len(n) > 20:
                continue
            if "·" in n:
                parts = [p.strip() for p in n.split("·") if p.strip()]
                if len(parts) >= 2:
                    root, clan = parts[0], parts[-1]
                    if root and root not in ns.roots and len(ns.roots) < 30:
                        ns.roots.append(root)
                    if clan and clan not in ns.clans and len(ns.clans) < 30:
                        # 氏族名常带「氏」尾，规范化：确保以「氏」结尾。
                        if not clan.endswith("氏"):
                            clan = clan + "氏"
                        if clan not in ns.clans:
                            ns.clans.append(clan)
            else:
                # 无分隔符：整体作 root 候选。
                if n not in ns.roots and len(ns.roots) < 30:
                    ns.roots.append(n)

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
        try:
            raw = self.provider.generate(GenRequest(
                system="你是社会结构生成器。提议契合文明历史情境的新身份头衔，"
                       "头衔应具体、有时代感、不与常见泛称重复。严格按格式输出。",
                user=user, context_docs=[], max_tokens=150,
            ))
        except Exception:
            raw = ""
        out: list[tuple[str, SocialClass]] = []
        valid = {sc.value for sc in SocialClass}
        for line in (raw or "").splitlines():
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
        try:
            raw = self.provider.generate(GenRequest(
                system="你是文风审定者。提议契合文明新历史阶段的官方文风微调，"
                       "笔法说明应具体可执行（如『纪事去颂圣、载公民议』），保留该文明一贯气质。"
                       "严格按格式输出。",
                user=user, context_docs=[], max_tokens=180,
            ))
        except Exception:
            raw = ""
        out: dict[str, str] = {}
        valid = set(self.GENRES)
        for line in (raw or "").splitlines():
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
        raw = ""
        try:
            raw = self.provider.generate(GenRequest(
                system="你是人物传记精炼者。把给定事件对某人物的影响浓缩成一句经历（15-30字），"
                       "只输出这一句，不要年份前缀以外的解释。",
                user=f"人物：{person.name}（{person.role}，{person.gender or '性别未定'}）。\n"
                     f"事件：{year}年 {event_desc}\n请精炼成一句该人物的经历。",
                context_docs=[], max_tokens=120,
            ))
        except Exception:
            raw = ""
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
        raw = ""
        try:
            raw = self.provider.generate(GenRequest(
                system="你是人物档案管理者。判断已故且长期未被提及的人物是否可以从活动档案中清理"
                       "（其关键信息已另存既成事实）。判断依据：是否仍可能被未来文本牵连——"
                       "如有在世亲属、是重大历史人物则保留。只输出『可删』或『保留』。",
                user=f"人物：{person.name}（{person.role}，{person.gender or '性别未定'}），"
                     f"{person.birth_year}–{person.death_year}年，死因{person.cause_of_death}。"
                     f"经历：{person.bio_entries[:3]}。"
                     f"有在世亲属提示：{'是' if living_relatives_hint else '否'}。"
                     f"是否可删？",
                context_docs=[], max_tokens=120,
            ))
        except Exception:
            raw = ""
        raw = (raw or "").strip()
        if not raw:
            # LLM 空返回：保守保留（不删），等下个 tick 再判——避免空返回误删。
            return False
        return "可删" in raw


# ---------------------------------------------------------------------------
# 称谓判定（LLM 判断 + 规则兜底，避免把真名误判为称谓）
# ---------------------------------------------------------------------------


class AppellationJudge:
    """判断一个字符串是真名还是称谓/泛称，避免规则误判（如「阿兰」「冉阿让」被当称谓）。

    规则判定（``_looks_like_appellation``）够快但覆盖不全——能稳定识别「小岩儿之父」「阿爸」
    这类明确称谓，但漏判「秋子之父」「抄经人」等，也可能误杀含「阿」「老」的真名。
    本类用 LLM 做主判断（更灵活、能处理规则覆盖不到的称谓），规则做兜底：

    - LLM 判为称谓 → 弃用（改按命名规范生成真名）。
    - LLM 判为真名 → 保留。
    - LLM 返回空或异常 → 回退规则判定（不当成真名，避免空返回误判）。

    重要：``max_tokens`` 给到 120+——DeepSeek 在 token 预算极小时会返回空串，
    早期实现因 ``max_tokens=10`` 致空返回，代码把空串当真名，造成「全判真名」假象。
    """

    # 判定 prompt：明确给正反例，要求「是名字」/「不是名字」二选一。
    _SYSTEM = (
        "你是一个姓名判断助手。用户给你一个字符串，判断它是不是一个真实的人名。"
        "如「张三」「阿兰」「冉阿让」「穆·禾氏」是名字；"
        "「阿爸」「秋子之父」「二叔」「其母之弟」「抄经人」「老王」是称呼/泛称，不是名字。"
        "只回答「是名字」或「不是名字」，不要解释。"
    )

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def is_appellation(self, name: str) -> bool:
        """返回 name 是否为称谓/泛称（应弃用）而非真名。

        LLM 主判，规则兜底。空返回或异常时回退规则（``_looks_like_appellation``），
        不把空返回当真名——避免早期实现的「全判真名」bug。
        """
        from .generators import _looks_like_appellation
        if self.provider.name == "mock":
            # mock 走规则判定，零配置可跑且可复现。
            return _looks_like_appellation(name)
        raw = ""
        try:
            raw = self.provider.generate(GenRequest(
                system=self._SYSTEM,
                user=f"字符串：{name}",
                context_docs=[], max_tokens=120,
            ))
        except Exception:
            raw = ""
        raw = (raw or "").strip()
        if not raw:
            # LLM 空返回：回退规则，不当成真名。
            return _looks_like_appellation(name)
        # 解析：含「不是」→ 称谓；含「是名字/真名/人名」→ 真名。
        if "不是" in raw or "称谓" in raw or "称呼" in raw or "泛称" in raw:
            return True
        if "是名字" in raw or "真名" in raw or "人名" in raw or "是名" in raw:
            return False
        # 模糊：回退规则。
        return _looks_like_appellation(name)


# ---------------------------------------------------------------------------
# 社会组织涌现与清理
# ---------------------------------------------------------------------------


# mock 模式按触发关键词给的兜底组织候选：trigger -> [(name, org_type, 规模说明)]。
# 与 _MOCK_ROLE_CANDIDATES 同思路：关键事件触发，按情境给契合的组织涌现。
_MOCK_ORG_CANDIDATES: dict[str, list[tuple[str, OrgType, str]]] = {
    "tech_bronze": [("铜匠行会", OrgType.GUILD, "青铜冶铸"), ("铸器坊", OrgType.GUILD, "器物铸造")],
    "tech_iron": [("铁匠行会", OrgType.GUILD, "铁器锻造"), ("兵器坊", OrgType.GUILD, "甲兵打造")],
    "tech_medieval": [("城堡学堂", OrgType.SCHOOL, "贵族蒙学"), ("行会总会", OrgType.GUILD, "诸行统筹")],
    "tech_renaissance": [("文华学堂", OrgType.SCHOOL, "人文讲学"), ("测绘院", OrgType.SCHOOL, "星地测绘")],
    "tech_industrial": [("机巧工场", OrgType.GUILD, "机器制造"), ("实业总会", OrgType.GUILD, "工商统筹")],
    "gov_monarchy": [("王廷", OrgType.COUNCIL, "王廷议政"), ("近卫军", OrgType.ARMY, "王室亲军")],
    "gov_republic": [("议事会", OrgType.COUNCIL, "公民议事"), ("民选法庭", OrgType.COUNCIL, "裁断纠纷")],
    "gov_theocracy": [("教廷", OrgType.CHURCH, "神权中枢"), ("异端裁判所", OrgType.CHURCH, "教义稽查")],
    "gov_empire": [("行省总督府", OrgType.FIEF, "行省治理"), ("军团", OrgType.ARMY, "征战编制")],
    "default": [("市集", OrgType.SETTLEMENT, "集市聚落")],
}


class OrgProposer:
    """在关键事件时提议涌现新社会组织。

    LLM 提议契合文明现状的新组织（名/类型/规模/可选上级），引擎建 Organization 并写
    ``Fact(kind="org_emergence")``。mock 模式按触发关键词给固定候选。遵循"判断/提议类
    调用健壮性"约定：max_tokens≥120、try/except、空返回回退 mock。
    """

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def propose(self, civ: Civilization, trigger: str, count: int = 2) -> list[dict]:
        """提议 ``count`` 个新组织。返回 [{name, org_type, scale_note, parent_name?}]。

        ``trigger`` 形如 ``"tech_bronze"``/``"gov_monarchy"``。去重：不与已有组织同名。
        """
        if self.provider.name == "mock":
            cands = _MOCK_ORG_CANDIDATES.get(trigger, _MOCK_ORG_CANDIDATES["default"])
            picked = self.rng.sample(cands, min(count, len(cands)))
            out = [{"name": n, "org_type": ot.value, "scale_note": s, "parent_name": ""}
                   for n, ot, s in picked]
        else:
            out = self._llm_propose(civ, trigger, count)
        existing = {o.name for o in civ.organizations}
        return [d for d in out if d.get("name") and d["name"] not in existing][:count]

    def _llm_propose(self, civ: Civilization, trigger: str, count: int) -> list[dict]:
        """调 LLM 提议组织，结构化返回。要求每行『组织名|类型|规模说明|上级组织名(可空)』。"""
        valid_types = {ot.value for ot in OrgType}
        existing = "; ".join(f"{o.name}({o.org_type.value})" for o in civ.organizations) or "无"
        user = (
            f"文明：{civ.name}（政体={civ.government.value}，科技={civ.tech_level.name}，"
            f"信仰={civ.religion}，人口={civ.population}）。\n"
            f"触发情境：{trigger}（科技升级/政体更替/人口增长等）。\n"
            f"已有组织：{existing}\n"
            f"请提议 {count} 个契合该文明新阶段的新社会组织。类型须从这些里选："
            f"{sorted(valid_types)}（settlement聚落/church教会/guild行会/school学堂/"
            f"council议会/fief封地/army军队/other其他）。\n"
            f"格式：每行『组织名|类型|规模说明|上级组织名(可空)』，不要编号、不要解释。"
        )
        try:
            raw = self.provider.generate(GenRequest(
                system="你是社会结构生成器。提议契合文明历史阶段的新社会组织，"
                       "组织名应具体有时代感（如「霜鲸湾渔会」「海灵教会」），不与常见泛称重复。"
                       "严格按格式输出。",
                user=user, context_docs=[], max_tokens=200,
            ))
        except Exception:
            raw = ""
        out: list[dict] = []
        for line in (raw or "").splitlines():
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            while len(parts) < 4:
                parts.append("")
            name, otype, scale, parent = parts[0], parts[1].lower(), parts[2], parts[3]
            name = name.lstrip("0123456789.、-) ").strip()
            if name and otype in valid_types:
                out.append({"name": name, "org_type": otype, "scale_note": scale, "parent_name": parent})
        # 不足则用 mock 补足。
        if len(out) < count:
            cands = _MOCK_ORG_CANDIDATES.get(trigger, _MOCK_ORG_CANDIDATES["default"])
            for n, ot, s in cands:
                if len(out) >= count:
                    break
                out.append({"name": n, "org_type": ot.value, "scale_note": s, "parent_name": ""})
        return out[:count]


class OrgPurger:
    """判断已解散且长期未提及的组织是否可删卡，防组织卡膨胀。

    与 PersonPurger 对称：已解散且 last_mentioned_year 早于阈值者，LLM 判断是否仍可能被
    未来文本牵连，可删则引擎先写 Fact(kind="org_dissolve") 存关键信息再删。
    """

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    def should_purge(self, org: Organization) -> bool:
        """返回是否可删卡。mock 兜底：已解散即可删。空返回保守保留。"""
        if self.provider.name == "mock":
            return org.dissolved_year is not None
        try:
            raw = self.provider.generate(GenRequest(
                system="你是组织档案管理者。判断已解散且长期未被提及的组织是否可从活动档案清理"
                       "（其关键信息已另存既成事实）。只输出『可删』或『保留』。",
                user=f"组织：{org.name}（{org.org_type.value}），"
                     f"{org.founded_year}–{org.dissolved_year}年。"
                     f"经历：{org.history_entries[:3]}。是否可删？",
                context_docs=[], max_tokens=120,
            ))
        except Exception:
            raw = ""
        raw = (raw or "").strip()
        if not raw:
            return False  # 空返回保守保留
        return "可删" in raw


class OrgMemberInferrer:
    """按阶层/role/home 推断新人物应归入的组织（CAST 未给所属组织时兜底）。

    推断规则（确定性，mock 与 LLM 模式共用，保证可复现）：
    - clergy → 该文明一个 CHURCH（无则空）
    - soldier → 该文明一个 ARMY（无则空）
    - artisan → 该文明一个 GUILD（无则空）
    - commoner/nobility → 按 home 匹配 SETTLEMENT（组织名含 home 词根），否则随机 SETTLEMENT
    - outsider/marginal → 一般不归组织（返回空）
    命中后建双向成员关系（组织 members + Person.orgs）。
    """

    # 阶层 → 倾向的组织类型（按此顺序找该文明的同类组织）。
    _CLASS_TO_TYPE = {
        SocialClass.CLERGY: [OrgType.CHURCH],
        SocialClass.SOLDIER: [OrgType.ARMY],
        SocialClass.ARTISAN: [OrgType.GUILD, OrgType.SETTLEMENT],
        SocialClass.NOBILITY: [OrgType.FIEF, OrgType.COUNCIL, OrgType.SETTLEMENT],
        SocialClass.COMMONER: [OrgType.SETTLEMENT, OrgType.GUILD],
    }

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def infer(self, civ: Civilization, sclass: SocialClass, role: str, home: str) -> Optional[str]:
        """返回推断的组织 id，无合适则 None。优先按 home 匹配，否则随机同类。"""
        types = self._CLASS_TO_TYPE.get(sclass, [])
        living = [o for o in civ.organizations if o.dissolved_year is None]
        for ot in types:
            candidates = [o for o in living if o.org_type == ot]
            if not candidates:
                continue
            # 按 home 匹配：组织名含 home 词根则优先。
            if home:
                home_key = home.split("·")[0]
                for o in candidates:
                    if home_key and home_key in o.name:
                        return o.id
            return self.rng.choice(candidates).id
        return None
