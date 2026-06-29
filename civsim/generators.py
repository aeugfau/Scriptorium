"""
Text-generators — turn structured state + events into narrative artifacts.

This module is the soul of the game: each generator takes a slice of the
world (a state "card" plus the events that happened this tick) and asks the
LLM provider to write a document in a particular genre. The output is an
:class:`Artifact`, persisted by the archive.

Genres are deliberately varied so the same event can appear as a chronicle
chapter, a diary entry, a royal decree, a scripture verse, or council
minutes — the "anything that forms text" framing.
"""

from __future__ import annotations

import random
from typing import Optional

from .archive import Artifact
from .models import Civilization, Event, World
from .providers import GenRequest, LLMProvider


class ArtifactFactory:
    """Builds :class:`Artifact` objects for one tick via an LLM provider."""

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    # -- helpers -----------------------------------------------------------

    def civ_card(self, c: Civilization) -> str:
        """Compact text snapshot of a civ — the generator's "memory" of it."""
        rels = ", ".join(f"{k}:{v.value}" for k, v in c.relations.items()) or "无"
        people = "; ".join(
            f"{p.name}({p.role}/{p.social_class.value})"
            for p in c.people if p.death_year is None
        ) or "无名人"
        return (
            f"{c.name} [biome={c.biome.value}] 人口{c.population} "
            f"粮储{c.food:.0f} 财富{c.wealth:.0f} 科技={c.tech_level.name}({c.tech_progress:.0f}) "
            f"政体={c.government.value} 信仰={c.religion} 稳定{c.stability:.0f} "
            f"外交:{rels} 命名风格:{c.naming.style_note or '朴素'} "
            f"文风:{c.voice.general} "
            f"社会阶层:{[s.value for s in c.social_classes]} 名人:{people}"
        )

    def world_brief(self, w: World) -> str:
        """One-paragraph world context fed to every generator."""
        civ_cards = "\n".join("  - " + self.civ_card(c) for c in w.civs)
        chronicle = "\n".join("  - " + line for line in w.chronicle[-8:]) or "  - (新初创世)"
        # 既成事实：把当前仍生效的硬约束列出，生成器必须遵守。
        facts = w.active_facts()
        facts_block = "\n".join(f"  - {f.statement}" for f in facts) or "  - (无)"
        return (
            f"世界 {w.name}，当前 {w.year} 年（第{w.tick_count}纪元之轮）。\n"
            f"诸文明状态：\n{civ_cards}\n"
            f"近年要事：\n{chronicle}\n"
            f"既成事实（必须遵守，违反即叙事错误）：\n{facts_block}"
        )

    def events_brief(self, events: list[Event]) -> str:
        if not events:
            return "  - (本时段平静如水)"
        return "\n".join(
            f"  - [{e.source}] {e.year}年: {e.title} —— {e.description}"
            for e in events
        )

    def _focal_year(self, w: World, events: list[Event]) -> int:
        """取该篇档案的"代表年份"——用于标题与日期落款。

        优先取本篇涉及事件中 magnitude 最大的年份；无事件则取区间中点
        （prev_year + w.year）/ 2。这样日记/诏令标题的年份落在真实历史内，
        而非永远卡在 tick 末年份（25/50/75）。

        注意：仅编年史（综述整段）用本方法锚定末年；日记/诏令/经文/会议纪要改用
        :meth:`_year_in_span` 各自从区间内随机取年，避免「同 tick 多篇都撞同一年」。
        """
        if events:
            return max(events, key=lambda e: e.magnitude).year
        # 区间中点：本 tick 覆盖 [w.year-years_per_tick, w.year]。
        return (w.year - w.years_per_tick + w.year) // 2

    def _year_in_span(self, w: World) -> int:
        """从本 tick 区间 ``[w.year-years_per_tick, w.year]`` 内随机取一年。

        日记是某天的、诏令是某天的——它们本就该落在区间内不同年份，而非共用一个
        focal year 造成「同一年冒出 5 篇文档」的不自然。用 ``self.rng``，可复现。
        """
        lo = w.year - w.years_per_tick
        hi = w.year
        return self.rng.randint(lo, hi)

    # -- per-genre generators ---------------------------------------------

    def _gen(self, system: str, user: str, context_docs: list[str], max_tokens: int = 900,
             civ: Optional[Civilization] = None, genre: Optional[str] = None) -> str:
        """调 provider 生成文本。若传入 ``civ`` 与 ``genre``，注入该文明该体裁的官方文风。

        文风来自 ``civ.voice.for_genre(genre)``，拼进 system prompt 末尾作为口吻约束，
        保证同系列文本风格连贯。无文风则不加约束。
        """
        if civ is not None and genre is not None:
            voice = civ.voice.for_genre(genre)
            if voice:
                system = f"{system}\n本文明此体裁的一贯文风须遵循：{voice}"
        return self.provider.generate(GenRequest(system=system, user=user, context_docs=context_docs, max_tokens=max_tokens))

    def chronicle(self, w: World, events: list[Event]) -> Artifact:
        """A history-book chapter covering this tick."""
        y_from = w.year - w.years_per_tick
        y_to = w.year
        body = self._gen(
            system=(
                "你是一位文明史官，负责撰写编年史。用凝练、庄重的半文言体中文，"
                "以纪传体/编年体风格记录本时段要事。不杜撰与上文矛盾之事，"
                "可在文案口吻中体现时代氛围。约150-300字。"
            ),
            user=(
                f"请撰写 {y_from} 至 {y_to} 年这一时段的编年史章节。"
                f"凡下文事件已标明年份的，照其年份书写，勿统一改写为整十年/整百年。"
                f"\n\n本时段要事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.world_brief(w)],
        )
        return Artifact(
            genre="chronicle", title=f"{w.name}编年史·第{w.tick_count}章",
            body=body, year=self._focal_year(w, events), tick=w.tick_count, author="太史馆",
        )

    def diary(self, w: World, events: list[Event], person=None) -> Optional[Artifact]:
        """A resident's diary entry — pick a living notable, else an anonymous citizen.

        作者必须是**在本日记落款年份仍存活**的人物。已故者不得写日记——
        这正是「人物死后还能写日记」bug 的修复点。落款年份从本 tick 区间内随机取
        （:meth:`_year_in_span`），使各篇日记散布在不同年份；若该年作者已死，则排除。
        无合适名人时退化为匿名居民。注入作者所属文明的日记文风，保证口吻连贯。
        """
        focal = self._year_in_span(w)
        candidates = [
            p for c in w.civs for p in (c.people if person is None else [])
            # 在世判定：未死，或死于落款年份之后（落款那刻人还在）。
            if p.death_year is None or p.death_year > focal
        ]
        if candidates:
            who = self.rng.choice(candidates)
            author = who.name
            author_id = who.id  # 记 id 而非仅名字：校验按 id 匹配，避免重名误判。
            # 注入阶层/年龄/具体身份背景，让视角有层次（贵族与边缘流民语气迥异）。
            perspective = (
                f"你是 {who.name}，{who.age_note or '壮年'}{who.role}"
                f"（{who.social_class.value}阶层）。此时为 {focal} 年。"
                f"用符合你身份与年龄的口吻写日记。"
            )
            civ_id = who.civ_id
            civ = w.civ(who.civ_id)
        else:
            author = "无名之民"
            author_id = None
            civ = self.rng.choice(w.civs)
            perspective = f"你是 {civ.name} 的一名普通居民。此时为 {focal} 年。"
            civ_id = civ.id

        body = self._gen(
            system=(
                "你是一位游戏中的虚构人物，写一篇私人日记。第一人称、口语化、"
                "带情绪与生活细节，记录本时段影响你的事。120-250字。"
                "口吻须符合你的阶层与年龄身份（贵族矜持、工匠务实、边缘人困顿等）。"
                "严格遵守既成事实：已辞世之人不得作为日记作者出现，也不得在文中说话行动。"
            ),
            user=(
                f"{perspective}\n"
                f"本时段发生的事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.world_brief(w)],
            civ=civ, genre="diary",
        )
        return Artifact(
            genre="diary", title=f"{author}的日记·{focal}年",
            body=body, year=focal, civ_id=civ_id, tick=w.tick_count,
            author=author, author_id=author_id,
        )

    def decree(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A ruler's decree — only if something governance-relevant happened."""
        gov_words = ("动荡", "饥荒", "战争", "交战", "贸易", "革新", "辞世", "崭露头角")
        if not any(any(k in e.title for k in gov_words) for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)  # 诏令落款年从区间内随机取，散布开。
        body = self._gen(
            system=(
                "你是该文明的统治者，颁布一道诏令/法令。语气威严、正式，"
                "针对本时段要事给出政令内容与理由。80-180字。以'奉天承运……'或"
                "符合该政体的口吻开头。"
            ),
            user=(
                f"颁布地：{civ.name}（政体={civ.government.value}）。落款 {year} 年。\n"
                f"本时段要事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ), self.world_brief(w)],
            civ=civ, genre="decree",
        )
        return Artifact(
            genre="decree", title=f"{civ.name}诏令·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.name}王廷",
        )

    def scripture(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A religious text excerpt — only on weighty, fate-like events."""
        if not any(e.magnitude >= 1.5 for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)
        body = self._gen(
            system=(
                "你是本信仰的经文抄写者，写一段经文/偈语/神谕来诠释本时段之事。"
                "语带玄机、韵律感，可含神祇之名。60-150字。"
            ),
            user=(
                f"信仰背景：{civ.religion}（{civ.name}）。落款 {year} 年。\n"
                f"需诠释之事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ)],
            civ=civ, genre="scripture",
        )
        return Artifact(
            genre="scripture", title=f"{civ.religion}经文·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.religion}祭司团",
        )

    def minutes(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A council/parliament minutes doc — only if diplomacy or governance events."""
        if not any(e.source == "player" or len(e.involved_civs) >= 2 or "动荡" in e.title for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)
        body = self._gen(
            system=(
                "你是会议书记，撰写一份议事会/长老会/议会的会议纪要。"
                "条目化、严肃、记录议题与议决。150-300字。"
            ),
            user=(
                f"会议方：{civ.name}（{civ.government.value}）。落款 {year} 年。\n"
                f"议事背景：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ)],
            civ=civ, genre="minutes",
        )
        return Artifact(
            genre="minutes", title=f"{civ.name}议事纪要·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author="议事会书记",
        )

    # -- 校验 -----------------------------------------------------------

    def _violations(self, art: Artifact, w: World) -> list[str]:
        """扫描已生成的正文，返回与既成事实冲突的描述列表（空=通过）。

        这是「事后校验」层：台账 facts 已在 prompt 里作为硬约束，但 LLM 不 100%
        可靠，仍可能违规。此处用规则再扫一遍正文，抓住可机械判定的违规：

        - death：日记体裁（第一人称）中不得出现已死作者作为在世说话者。
          判定：若 art.author 是已死之人（且死于落款年之前），而正文又以第一人称
          写作且未明确记述其生前往事，即视为违规。简化判定：日记作者是死者即违规
          （作者筛选本应排除，这是双保险）。
        - victory：不得写「败者取胜/击败胜者」这类翻案措辞。

        无法机械判定的（如某人语气像在世）留给人工；此处只抓确定的。
        """
        violations: list[str] = []
        facts = w.active_facts(year=art.year)

        # death 校验：日记的 author 不应是「落款年已死」之人。
        # 与 diary 作者筛选同口径：death_year > focal 时人在世、可写；
        # 仅当落款年 >= 死亡年（写日记时人已死）才判违规。
        # 按 author_id（Person.id）匹配 fact.subject，而非按名字——名字会撞名，
        # 用 id 才能精确锁定具体那个人，避免「同名活人替死人背锅」的误判。
        if art.genre == "diary" and art.author_id:
            for f in facts:
                if f.kind != "death" or f.subject != art.author_id:
                    continue
                if art.year >= f.year:
                    violations.append(
                        f"日记作者 {art.author}（id={art.author_id}）已于 {f.year} 年辞世，"
                        f"不得在 {art.year} 年（落款）再写日记（fact: {f.id}）。"
                    )

        # victory 校验：不得出现败者击败胜者的翻案措辞。
        for f in facts:
            if f.kind != "victory":
                continue
            # statement 形如「204 年 A 击败 B，...」。
            parts = f.statement.split("击败")
            if len(parts) != 2:
                continue
            winner = parts[0].split("年")[-1].strip()
            loser = parts[1].split("，")[0].strip()
            if winner and loser:
                if f"{loser}击败{winner}" in art.body or f"{loser}战胜{winner}" in art.body:
                    violations.append(f"正文出现「{loser}击败{winner}」与胜败事实冲突（fact: {f.id}）。")
        return violations

    def _validated(self, make_art, w: World, events: list[Event], retries: int = 2) -> Optional[Artifact]:
        """调用生成器并校验，违规则重生成，最多 ``retries`` 次。

        ``make_art`` 是无参闭包，返回新 Artifact（每次重生成调一次，LLM 因温度有差异）。
        仍违规则返回最后一次结果并打印告警——宁可带瑕疵产出也不要丢档案。
        """
        art = make_art()
        for attempt in range(retries):
            vs = self._violations(art, w) if art is not None else []
            if not vs:
                return art
            print(f"[civsim] 校验发现 {len(vs)} 处违规，重生成(第{attempt+1}次): {vs}")
            art = make_art()
        # 仍违规：记录后放行（避免死循环/丢档案）。
        if art is not None:
            print(f"[civsim] 经 {retries} 次重生成仍有违规，放行: {self._violations(art, w)}")
        return art

    # -- dispatch ----------------------------------------------------------

    def generate_for_tick(self, w: World, events: list[Event]) -> list[Artifact]:
        """产出本 tick 的全部档案：编年史必出，其余体裁按触发条件可选产出。

        设计意图：同一批事件在不同体裁里反复出现，让玩家从多角度「读」到历史——
        史官的庄重、居民的私语、王廷的政令、祭司的神谕、议会的条目，互为补充。

        容错：单个生成器抛异常不影响整 tick，降级为跳过该体裁（避免一次 LLM
        抖动毁掉整轮推进）。每个档案产出后经 ``_validated`` 事后校验，违规则重生成。
        新增体裁：在此加一个方法 + 接入本函数的遍历列表，并在 archive.Genres 登记。
        """
        arts: list[Artifact] = []
        for fn in (self.chronicle, self.diary, self.decree, self.scripture, self.minutes):
            try:
                a = self._validated(lambda f=fn: f(w, events), w, events)
            except Exception as exc:  # 单体裁失败不拖垮整 tick
                a = None
                print(f"[civsim] generator {fn.__name__} failed: {exc}")
            if a is not None:
                arts.append(a)
        return arts