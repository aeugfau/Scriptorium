"""
人物命名与社会身份生成（civsim.naming）。

本模块是「LLM 即兴生成 + 结构化规范约束」的接合点：

- :class:`NameGenerator` 按文明的 :class:`~civsim.models.NamingStyle`（词库+模板+
  风格说明）调 LLM 生成符合该文明气质的人名。规范是结构化的、可控、可演化；
  名字是 LLM 即兴组合的、有创意。mock 后端有兜底（从词库随机组合），零配置可跑。
- :class:`RoleProposer` 在关键事件时调 LLM 提议契合文明现状的新社会身份头衔
  （如「角斗士」「行会首脑」），并指明其所属核心阶层；引擎校验阶层合法后
  加入文明 ``role_pool``。这让文明的社会结构随时间涌现、可见地增长。

两套生成器都走 :class:`~civsim.providers.LLMProvider`，故换后端无需改本模块。
"""

from __future__ import annotations

import random
from typing import Optional

from .models import Civilization, NamingStyle, SocialClass
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
