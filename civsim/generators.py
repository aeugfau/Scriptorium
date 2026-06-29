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
        people = "; ".join(f"{p.name}({p.role})" for p in c.people if p.death_year is None) or "无名人"
        return (
            f"{c.name} [biome={c.biome.value}] 人口{c.population} "
            f"粮储{c.food:.0f} 财富{c.wealth:.0f} 科技={c.tech_level.name}({c.tech_progress:.0f}) "
            f"政体={c.government.value} 信仰={c.religion} 稳定{c.stability:.0f} "
            f"外交:{rels} 名人:{people}"
        )

    def world_brief(self, w: World) -> str:
        """One-paragraph world context fed to every generator."""
        civ_cards = "\n".join("  - " + self.civ_card(c) for c in w.civs)
        chronicle = "\n".join("  - " + line for line in w.chronicle[-8:]) or "  - (新初创世)"
        return (
            f"世界 {w.name}，当前 {w.year} 年（第{w.tick_count}纪元之轮）。\n"
            f"诸文明状态：\n{civ_cards}\n"
            f"近年要事：\n{chronicle}"
        )

    def events_brief(self, events: list[Event]) -> str:
        if not events:
            return "  - (本时段平静如水)"
        return "\n".join(
            f"  - [{e.source}] {e.year}年: {e.title} —— {e.description}"
            for e in events
        )

    # -- per-genre generators ---------------------------------------------

    def _gen(self, system: str, user: str, context_docs: list[str], max_tokens: int = 900) -> str:
        return self.provider.generate(GenRequest(system=system, user=user, context_docs=context_docs, max_tokens=max_tokens))

    def chronicle(self, w: World, events: list[Event]) -> Artifact:
        """A history-book chapter covering this tick."""
        body = self._gen(
            system=(
                "你是一位文明史官，负责撰写编年史。用凝练、庄重的半文言体中文，"
                "以纪传体/编年体风格记录本时段要事。不杜撰与上文矛盾之事，"
                "可在文案口吻中体现时代氛围。约150-300字。"
            ),
            user=(
                f"请撰写 {w.year} 年前后这一时段的编年史章节。"
                f"\n\n本时段要事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.world_brief(w)],
        )
        return Artifact(
            genre="chronicle", title=f"{w.name}编年史·第{w.tick_count}章",
            body=body, year=w.year, tick=w.tick_count, author="太史馆",
        )

    def diary(self, w: World, events: list[Event], person=None) -> Optional[Artifact]:
        """A resident's diary entry — pick a living notable, else an anonymous citizen."""
        candidates = [p for c in w.civs for p in (c.people if person is None else [])
                     if p.death_year is None]
        if candidates:
            who = self.rng.choice(candidates)
            author = who.name
            perspective = f"你是 {who.name}，{who.role}。"
            civ_id = who.civ_id
        else:
            author = "无名之民"
            civ = self.rng.choice(w.civs)
            perspective = f"你是 {civ.name} 的一名普通居民。"
            civ_id = civ.id

        body = self._gen(
            system=(
                "你是一位游戏中的虚构人物，写一篇私人日记。第一人称、口语化、"
                "带情绪与生活细节，记录本时段影响你的事。120-250字。"
            ),
            user=(
                f"{perspective}\n"
                f"本时段发生的事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.world_brief(w)],
        )
        return Artifact(
            genre="diary", title=f"{author}的日记·{w.year}年",
            body=body, year=w.year, civ_id=civ_id, tick=w.tick_count, author=author,
        )

    def decree(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A ruler's decree — only if something governance-relevant happened."""
        gov_words = ("动荡", "饥荒", "战争", "交战", "贸易", "革新", "辞世", "崭露头角")
        if not any(any(k in e.title for k in gov_words) for e in events):
            return None
        civ = self.rng.choice(w.civs)
        body = self._gen(
            system=(
                "你是该文明的统治者，颁布一道诏令/法令。语气威严、正式，"
                "针对本时段要事给出政令内容与理由。80-180字。以'奉天承运……'或"
                "符合该政体的口吻开头。"
            ),
            user=(
                f"颁布地：{civ.name}（政体={civ.government.value}）。\n"
                f"本时段要事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ), self.world_brief(w)],
        )
        return Artifact(
            genre="decree", title=f"{civ.name}诏令·{w.year}年",
            body=body, year=w.year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.name}王廷",
        )

    def scripture(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A religious text excerpt — only on weighty, fate-like events."""
        if not any(e.magnitude >= 1.5 for e in events):
            return None
        civ = self.rng.choice(w.civs)
        body = self._gen(
            system=(
                "你是本信仰的经文抄写者，写一段经文/偈语/神谕来诠释本时段之事。"
                "语带玄机、韵律感，可含神祇之名。60-150字。"
            ),
            user=(
                f"信仰背景：{civ.religion}（{civ.name}）。\n"
                f"需诠释之事：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ)],
        )
        return Artifact(
            genre="scripture", title=f"{civ.religion}经文·{w.year}年",
            body=body, year=w.year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.religion}祭司团",
        )

    def minutes(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A council/parliament minutes doc — only if diplomacy or governance events."""
        if not any(e.source == "player" or len(e.involved_civs) >= 2 or "动荡" in e.title for e in events):
            return None
        civ = self.rng.choice(w.civs)
        body = self._gen(
            system=(
                "你是会议书记，撰写一份议事会/长老会/议会的会议纪要。"
                "条目化、严肃、记录议题与议决。150-300字。"
            ),
            user=(
                f"会议方：{civ.name}（{civ.government.value}）。\n"
                f"议事背景：\n{self.events_brief(events)}"
            ),
            context_docs=[self.civ_card(civ)],
        )
        return Artifact(
            genre="minutes", title=f"{civ.name}议事纪要·{w.year}年",
            body=body, year=w.year, civ_id=civ.id, tick=w.tick_count,
            author="议事会书记",
        )

    # -- dispatch ----------------------------------------------------------

    def generate_for_tick(self, w: World, events: list[Event]) -> list[Artifact]:
        """产出本 tick 的全部档案：编年史必出，其余体裁按触发条件可选产出。

        设计意图：同一批事件在不同体裁里反复出现，让玩家从多角度「读」到历史——
        史官的庄重、居民的私语、王廷的政令、祭司的神谕、议会的条目，互为补充。

        容错：单个生成器抛异常不影响整 tick，降级为跳过该体裁（避免一次 LLM
        抖动毁掉整轮推进）。新增体裁：在此加一个方法 + 接入本函数的遍历列表，
        并在 archive.Genres 登记。
        """
        arts: list[Artifact] = [self.chronicle(w, events)]
        for fn in (self.diary, self.decree, self.scripture, self.minutes):
            try:
                a = fn(w, events)
            except Exception as exc:  # 单体裁失败不拖垮整 tick
                a = None
                print(f"[civsim] generator {fn.__name__} failed: {exc}")
            if a is not None:
                arts.append(a)
        return arts