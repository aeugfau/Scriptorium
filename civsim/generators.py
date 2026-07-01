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
from .models import Civilization, Event, Organization, Person, SocialClass, World
from .providers import GenRequest, LLMProvider


# 称谓识别：用于 CAST 抽取时区分「真名」与「亲属称谓/泛称」。
# 设计取舍：宁可漏判（把泛称当真名建卡，后续清理机制会处理），不要误杀真名
# （如「阿兰」「冉阿让」「老王」可能是真名）。故只在「明确是称谓结构」时弃用：
# 1) 精确命中亲属称谓词（阿爸/二叔/爹娘/小叔…）；
# 2) 含「之X」亲属链（小岩儿之父、其母之弟）——「之」连称谓是真名极少见的结构。
# 不再用宽泛单字关键词（老/小/阿/他），避免误判真名。
_APPELLATION_EXACT = {"阿爸", "阿妈", "爹", "娘", "爷", "奶", "二叔", "大叔", "小叔",
                      "大伯", "舅父", "舅母", "婶婶", "堂叔", "表叔"}
_APPELLATION_KINSHIP_PREFIX = ("之子", "之女", "之父", "之母", "之妻", "之夫",
                               "之兄", "之弟", "之姊", "之妹", "之叔", "之侄")


def _looks_like_appellation(name: str) -> bool:
    """判 name 是否为明确的亲属称谓（应弃用，改按命名规范生成真名）。

    只在称谓结构明确时判 True：精确命中称谓词，或含「之X」亲属链。
    「老王」「阿猫」「他二叔」等模糊情况判 False（保留为名，宁可漏判不误杀）。
    关系边照常建（CAST 的 rel 列独立给出），故弃用名字不影响亲属链接。
    """
    n = name.strip()
    if not n or n in _APPELLATION_EXACT:
        return True
    return any(m in n for m in _APPELLATION_KINSHIP_PREFIX)


def _normalize_relation(raw: str) -> str:
    """把 LLM 给的关系字段清洗为规范关系词。

    LLM 偶带括号说明（如「叔伯（小贝里克之父的弟弟）」）或自由短语。取括号前、
    再匹配 RELATION_INVERSE 的 key；命中则用该规范词，否则原样返回（inverse 会回退相识）。
    """
    from .models import RELATION_INVERSE
    s = raw.split("（")[0].split("(")[0].strip()
    # 直接命中规范词。
    if s in RELATION_INVERSE or s in {v for v in RELATION_INVERSE.values()}:
        return s
    # 在原串里找包含的规范词（如「之父之弟」含「父」——但优先长词）。
    keys = sorted(RELATION_INVERSE.keys(), key=len, reverse=True)
    for k in keys:
        if k in raw:
            return k
    return s


class ArtifactFactory:
    """Builds :class:`Artifact` objects for one tick via an LLM provider."""

    def __init__(self, provider: LLMProvider, rng: random.Random | None = None):
        self.provider = provider
        self.rng = rng or random.Random()

    # -- helpers -----------------------------------------------------------

    def civ_card(self, c: Civilization) -> str:
        """Compact text snapshot of a civ — the generator's "memory" of it.

        全员建档下人物多，不能全列（防爆上下文）：只列名人在世者 + 最近被提及的若干平民，
        每人含性别/年龄/阶层/处境一行。完整人物卡按需检索（见 _persons_block）。
        """
        rels = ", ".join(f"{k}:{v.value}" for k, v in c.relations.items()) or "无"
        living = [p for p in c.people if p.death_year is None]
        # 名人在世者全列；平民只列最近被提及的 8 位。
        notables = [p for p in living if p.kind == "notable"]
        commoners = sorted(
            [p for p in living if p.kind == "commoner"],
            key=lambda p: p.last_mentioned_year, reverse=True,
        )[:8]
        shown = notables + commoners

        def line(p: Person) -> str:
            bits = [p.name, p.gender or "?", p.age_note or "壮年", p.role, p.social_class.value]
            if p.home:
                bits.append(f"居{p.home}")
            if p.circumstance:
                bits.append(p.circumstance)
            # 关系：列出最多 3 条结构化关系（对方名+类型），让 LLM 知晓人物亲属网。
            # 名字查表局限在本文明（亲属多同文明）；跨文明 id 找不到则显示 id。
            if p.relations:
                by_id = {pp.id: pp.name for pp in c.people}
                rels = [f"{by_id.get(rid, rid)}({rt})" for rid, rt in list(p.relations.items())[:3]]
                bits.append("关系:" + ",".join(rels))
            return "/".join(bits)

        people = "; ".join(line(p) for p in shown) or "无在世人物"
        # 社会组织：列存续组织（名/类型/隶属/规模），让 LLM 写作时知晓并引用。
        # 限制最近提及 N 个防爆上下文；LLM 只能引用这些既有组织，不得新造具体组织名。
        living_orgs = sorted(
            [o for o in c.organizations if o.dissolved_year is None],
            key=lambda o: o.last_mentioned_year, reverse=True,
        )[:10]
        org_by_id = {o.id: o.name for o in c.organizations}
        org_lines = []
        for o in living_orgs:
            parent = f"隶属于{org_by_id.get(o.parent_org_id, '?')}" if o.parent_org_id else ""
            members_n = len(o.members)
            org_lines.append(f"{o.name}({o.org_type.value},成员{members_n}{parent})")
        orgs = "; ".join(org_lines) or "无存续组织"
        return (
            f"{c.name} [biome={c.biome.value}] 人口{c.population} "
            f"粮储{c.food:.0f} 财富{c.wealth:.0f} 科技={c.tech_level.name}({c.tech_progress:.0f}) "
            f"政体={c.government.value} 信仰={c.religion} 稳定{c.stability:.0f} "
            f"外交:{rels} 命名风格:{c.naming.style_note or '朴素'} "
            f"文风:{c.voice.general} "
            f"社会阶层:{[s.value for s in c.social_classes]} 在世人物({len(living)}):{people} "
            f"社会组织(存续):{orgs}"
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
            f"既成事实（必须遵守，违反即叙事错误）：\n{facts_block}\n"
            f"社会组织约束：文本中只能引用上文已列出的存续社会组织，不得新造具体组织名"
            f"（泛称场所如村/市集/路口不限）；人物所属组织须与上文一致。"
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
        # mock 后端不真正生成 <CAST> 块（会把指令模板原样返回，污染人物卡），
        # 故 mock 模式剥离 CAST 指令，走空抽取路径。
        if self.provider.name == "mock":
            user = user.replace(self.CAST_INSTRUCTION, "")
        return self.provider.generate(GenRequest(system=system, user=user, context_docs=context_docs, max_tokens=max_tokens))

    # -- 人物抽取与建档（让文本中出现的角色都有卡）--------------------------

    CAST_INSTRUCTION = (
        "\n\n文末附一段人物清单（便于建档与前后一致），格式严格如下，无则留空：\n"
        "<CAST>\n姓名|性别|身份头衔|阶层(nobility/commoner/artisan/soldier/clergy/outsider/marginal)|"
        "居所|性格特征|处境一句话|与作者的关系类型|标准名|本篇该人物的关键经历一句话|所属组织名\n"
        "...</CAST>\n每行一人，字段用|分隔，可空。仅列本篇实际出现且有名字的角色（含作者本人）。\n"
        "关系类型用规范词：父/母/子/女/祖父/祖母/孙/叔伯/姑/侄/兄/弟/姊/妹/堂兄/堂弟/"
        "堂姊/堂妹/配偶/夫妻/师/徒/主/仆/朋友/敌/相识。称谓如「阿爸」「二叔」须解析为"
        "对方姓名 + 关系类型（如「穆·禾氏|男|陶工|commoner|...|父」），不要把称谓当姓名。\n"
        "标准名：该人物的全名/正名（即便文中用称谓或简称，这里填全名），用于跨篇识别同一人。\n"
        "关键经历：本篇中该人物做了什么/遭遇了什么，一句话（如「航海遇险获救」），可空。\n"
        "所属组织名：该人物所属的既有社会组织名（须是上文已列出的存续组织）或其标准名，可空。"
        "不得新造组织名。"
    )

    def _split_cast(self, raw: str) -> tuple[str, list[dict]]:
        """从 LLM 输出中拆出正文与 <CAST> 人物清单。

        LLM 把人物清单附在文末 <CAST>...</CAST> 块里（每行一人，字段|分隔）。这种「结构化块」
        比要求 LLM 返回整段 JSON 更稳——正文仍可自由发挥，块可容错解析。无块则视为无抽取。
        """
        import re as _re
        body = raw
        specs: list[dict] = []
        m = _re.search(r"<CAST>(.*?)</CAST>", raw, _re.DOTALL)
        if m:
            body = (raw[:m.start()] + raw[m.end():]).strip()
            for line in m.group(1).strip().splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                # 补齐到 11 字段
                while len(parts) < 11:
                    parts.append("")
                specs.append({
                    "name": parts[0], "gender": parts[1], "role": parts[2],
                    "social_class": parts[3], "home": parts[4],
                    "traits": parts[5], "circumstance": parts[6], "relation": parts[7],
                    "canonical": parts[8], "bio_event": parts[9], "org_name": parts[10],
                })
        return body, specs

    def _org_id_by_name(self, civ: Civilization, name: str) -> Optional[str]:
        """按名查该文明的存续组织 id（CAST 的「所属组织」列校验用）。命中则返回 id，否则 None。"""
        n = (name or "").strip()
        if not n:
            return None
        for o in civ.organizations:
            if o.dissolved_year is None and (o.name == n):
                return o.id
        return None

    def _resolve_persons(self, w: World, civ: Civilization, specs: list[dict],
                         year: int, genre: str, author: Optional[Person] = None) -> list[str]:
        """归并/建档文本中出现的人物，返回其 Person.id 列表。

        按「标准名 + 文明」匹配已存在 Person：命中则补全空字段、更新 last_mentioned_year、
        记 mentioned_in、追加本篇经历；未命中则新建 commoner 卡。标准名优先取 CAST 的
        ``canonical`` 列（LLM 给的稳定全名），回退到 ``name``——这让同一人即便文中用不同
        称谓（穆·禾氏 / 老穆 / 二叔）也能识别为同一卡。

        若传入 ``author``：对每个 CAST 行带「与作者关系类型」的，建双向结构化关系边——
        author.relations[other_id]=rel，other.relations[author_id]=inverse(rel)。
        这让「阿爸」「二叔」等称谓绑定到具体人物卡，且可双向查询（父↔子、叔伯↔侄）。
        """
        from .models import SocialClass as _SC, inverse_relation
        ids: list[str] = []
        judge = getattr(self, "_appellation_judge", None)
        for s in specs:
            name = (s.get("name") or "").strip()
            canonical = (s.get("canonical") or "").strip()
            # 用 LLM 判别称谓（judge）+ 规则兜底：判定 name/canonical 是否称谓。
            # 称谓则弃用（改由 register 生成真名）；真名则用作归并 key（canonical 优先）。
            def _is_appe(s):
                if not s:
                    return True
                if judge is not None:
                    try:
                        return judge.is_appellation(s)
                    except Exception:
                        pass
                return _looks_like_appellation(s)
            canon_is_appe = _is_appe(canonical) if canonical else True
            name_is_appe = _is_appe(name)
            # 标准名优先：canonical 非称谓则用 canonical 归并（处理称谓变体）；否则用 name。
            key = canonical if (canonical and not canon_is_appe) else ("" if name_is_appe else name)
            if not key:
                # 无标准名但带关系：建一张新卡（名字由 register 生成），仍挂关系。
                if not (s.get("relation") or "").strip():
                    continue
            existing = next((p for p in civ.people if p.name == key), None) if key else None
            # 也按 canonical 在已有卡里找（防止 canonical 是别名、name 是建卡名的情况）。
            if existing is None and key and canonical and not canon_is_appe:
                existing = next((p for p in civ.people if canonical == p.name), None)
            bio_event = (s.get("bio_event") or "").strip()
            if existing:
                # 补全空字段（不覆盖已有权威信息）。
                if not existing.gender and s.get("gender"):
                    existing.gender = s["gender"]
                if not existing.home and s.get("home"):
                    existing.home = s["home"]
                if s.get("traits"):
                    for t in s["traits"].replace("、", ",").split(","):
                        t = t.strip()
                        if t and t not in existing.traits:
                            existing.traits.append(t)
                if not existing.circumstance and s.get("circumstance"):
                    existing.circumstance = s["circumstance"]
                if s.get("role") and existing.role in ("居民", ""):
                    existing.role = s["role"]
                existing.last_mentioned_year = year
                if genre not in existing.mentioned_in:
                    existing.mentioned_in.append(genre)
                # 追加本篇经历（去重），让人物卡随被提及而成长。
                if bio_event:
                    entry = f"{year}年（{genre}）：{bio_event}"
                    if entry not in existing.bio_entries:
                        existing.bio_entries.append(entry)
                pid = existing.id
                ids.append(pid)
                # 已有卡：若 CAST 给了存在的所属组织，把此人加进去（双向），否则不动。
                org_name = (s.get("org_name") or "").strip()
                org_id = self._org_id_by_name(civ, org_name) if org_name else None
                if org_id:
                    cb = getattr(self, "_assign_org_cb", None)
                    if cb is not None:
                        cb(existing, civ, home=existing.home, explicit_org_id=org_id)
            else:
                # 新建 commoner 卡：解析阶层，出生年回拨，给 max_age（与寿命机制对齐）。
                # 建卡名用 key（标准名/真名）；若 key 空则 register 生成。
                sc = _SC.COMMONER
                try:
                    if s.get("social_class"):
                        sc = _SC(s["social_class"])
                except Exception:
                    pass
                pid = self._register_commoner(civ, key, s.get("role") or "居民",
                                              sc, year, gender=s.get("gender", ""),
                                              home=s.get("home", ""),
                                              traits=[t.strip() for t in s.get("traits", "").replace("、", ",").split(",") if t.strip()],
                                              circumstance=s.get("circumstance", ""))
                ids.append(pid)
                # 新卡也记本篇经历。
                if bio_event:
                    newp = next((p for p in civ.people if p.id == pid), None)
                    entry = f"{year}年（{genre}）：{bio_event}"
                    if newp is not None and entry not in newp.bio_entries:
                        newp.bio_entries.append(entry)
                # 若 CAST 给了存在的所属组织，用显式组织覆盖 register 的推断归入。
                org_name = (s.get("org_name") or "").strip()
                org_id = self._org_id_by_name(civ, org_name) if org_name else None
                if org_id:
                    newp = next((p for p in civ.people if p.id == pid), None)
                    cb = getattr(self, "_assign_org_cb", None)
                    if newp is not None and cb is not None:
                        cb(newp, civ, home=s.get("home", ""), explicit_org_id=org_id)
            # 建双向关系边：CAST 的「与作者关系类型」绑定 author 与此人。
            # 关系类型须是规范词（父/叔伯/配偶…），LLM 偶带括号说明（如「叔伯（之父之弟）」）
            # 或自由短语——清洗为首个规范关系词，否则 inverse_relation 查不到会错建反向边。
            rel = _normalize_relation((s.get("relation") or "").strip())
            if author is not None and rel and pid != author.id:
                author.relations[pid] = rel
                other = next((p for p in civ.people if p.id == pid), None)
                if other is not None:
                    other.relations[author.id] = inverse_relation(rel)
        return ids

    def _register_commoner(self, civ: Civilization, name: str, role: str, sclass,
                           year: int, **extra) -> str:
        """新建一个平民人物卡。委托给 Simulation.register_commoner（若可用）。

        ArtifactFactory 不直接持有 Simulation，故通过一个回调注入。若未设置回调
        （如直接单元测试 factory），退化为就地建卡（不挂 max_age/死因机制，仅展示用）。
        """
        cb = getattr(self, "_register_cb", None)
        if cb is not None:
            person = cb(civ, name, role, sclass, year, **extra)
            return person.id if person is not None else ""
        # 退化路径：就地建卡。
        from .models import Person
        pid = f"{civ.id}-c{len(civ.people)+1}-{year}"
        birth = year - 30
        civ.people.append(Person(
            id=pid, name=name, role=role, social_class=sclass, civ_id=civ.id,
            birth_year=birth, kind="commoner", first_seen_year=year,
            last_mentioned_year=year, **{k: v for k, v in extra.items() if v},
        ))
        return pid

    def set_register_callback(self, cb) -> None:
        """由 Simulation 调用，注入建档回调（让新建卡能挂上寿命/死因机制）。"""
        self._register_cb = cb

    def set_appellation_judge(self, judge) -> None:
        """由 Simulation 调用，注入称谓判别器（LLM 判断真名 vs 称谓，规则兜底）。"""
        self._appellation_judge = judge

    def set_org_inferrer(self, inferrer) -> None:
        """由 Simulation 调用，注入组织归入推断器（CAST 未给所属组织时兜底归入）。"""
        self._org_inferrer = inferrer

    def set_assign_org_cb(self, cb) -> None:
        """由 Simulation 调用，注入组织归入回调（建双向成员关系）。"""
        self._assign_org_cb = cb

    def chronicle(self, w: World, events: list[Event]) -> Artifact:
        """A history-book chapter covering this tick."""
        y_from = w.year - w.years_per_tick
        y_to = w.year
        raw = self._gen(
            system=(
                "你是一位文明史官，负责撰写编年史。用凝练、庄重的半文言体中文，"
                "以纪传体/编年体风格记录本时段要事。不杜撰与上文矛盾之事，"
                "可在文案口吻中体现时代氛围。约150-300字。"
            ),
            user=(
                f"请撰写 {y_from} 至 {y_to} 年这一时段的编年史章节。"
                f"凡下文事件已标明年份的，照其年份书写，勿统一改写为整十年/整百年。"
                f"\n\n本时段要事：\n{self.events_brief(events)}"
                f"{self.CAST_INSTRUCTION}"
            ),
            context_docs=[self.world_brief(w)],
        )
        body, specs = self._split_cast(raw)
        # 编年史不属单一文明；按角色名找到所属文明归并/建档。
        mentioned: list[str] = []
        for s in specs:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            # 找该名所属文明：先在所有文明已有人物里找，找不到则按 CAST 字段里的居所/关系启发
            # 归入涉及事件的某文明。
            target_civ = None
            for c in w.civs:
                if any(p.name == name for p in c.people):
                    target_civ = c
                    break
            if target_civ is None:
                # 归入本 tick 涉及的第一个文明（或随机）。
                involved = next((c for c in w.civs if c.id in [ic for e in events for ic in e.involved_civs]), None)
                target_civ = involved or self.rng.choice(w.civs)
            ids = self._resolve_persons(w, target_civ, [s], self._focal_year(w, events), "chronicle")
            mentioned.extend(ids)
        return Artifact(
            genre="chronicle", title=f"{w.name}编年史·第{w.tick_count}章",
            body=body, year=self._focal_year(w, events), tick=w.tick_count, author="太史馆",
            mentioned_persons=mentioned,
        )

    def diary(self, w: World, events: list[Event], person=None) -> Optional[Artifact]:
        """A resident's diary entry — pick a living person (notable or commoner), else create one.

        作者必须是**在本日记落款年份仍存活**的人物。已故者不得写日记。落款年份从本 tick
        区间内随机取（:meth:`_year_in_span`），各篇散布。无在世人物时不再用「无名之民」——
        而是当场建档一个详细平民卡（有名字/性别/role/阶层/处境），让平民视角鲜活。
        生成后抽取文中出现的人物建档，保证配角前后一致。
        """
        focal = self._year_in_span(w)
        candidates = [
            p for c in w.civs for p in (c.people if person is None else [])
            # 在世判定：未死，或死于落款年份之后（落款那刻人还在）。
            if p.death_year is None or p.death_year > focal
        ]
        if candidates:
            who = self.rng.choice(candidates)
            author_id = who.id
            # 注入阶层/年龄/具体身份/性别/处境背景，让视角有层次。
            age_num = focal - who.birth_year
            perspective = (
                f"你是 {who.name}，{who.gender or '人'}，{age_num}岁（{who.age_note or '壮年'}），{who.role}"
                f"（{who.social_class.value}阶层）"
                f"{f'，{who.circumstance}' if who.circumstance else ''}。此时为 {focal} 年。"
                f"你的口吻须严格符合{age_num}岁之人的阅历与心境（少年轻率、壮年干练、老者沧桑），"
                f"不得写出与{age_num}岁年龄不符的言行或经历。"
            )
            civ_id = who.civ_id
            civ = w.civ(who.civ_id)
        else:
            # 无在世人物：当场建档一个详细平民作者，而非「无名之民」。
            civ = self.rng.choice(w.civs)
            sc = self.rng.choice(civ.social_classes) if civ.social_classes else SocialClass.COMMONER
            role = self.rng.choice(civ.role_pool) if civ.role_pool else "居民"
            gender = self.rng.choice(["男", "女"])
            # 用回调建档（名字由 register 按命名规范生成），挂上寿命机制。
            pid = self._register_commoner(civ, "", role, sc, focal, gender=gender)
            who = next(p for p in civ.people if p.id == pid)
            author_id = who.id
            age_num = focal - who.birth_year
            perspective = (
                f"你是 {who.name}，{who.gender}，{age_num}岁（{who.age_note or '壮年'}）{who.role}"
                f"（{who.social_class.value}阶层）。此时为 {focal} 年。"
                f"你的口吻须严格符合{age_num}岁之人的阅历与心境，不得写出与年龄不符的言行。"
            )
            civ_id = civ.id

        author = who.name
        # 更新作者最近被提及年。
        who.last_mentioned_year = focal
        raw = self._gen(
            system=(
                "你是一位游戏中的虚构人物，写一篇私人日记。第一人称、口语化、"
                "带情绪与生活细节，记录本时段影响你的事。120-250字。"
                "口吻须严格符合你的阶层与年龄身份：少年轻率、壮年干练、老者沧桑；"
                "不得写出与作者年龄/处境不符的言行或经历（如老人不得写少年心事、农夫不得写朝堂事）。"
                "严格遵守既成事实：已辞世之人不得作为日记作者出现，也不得在文中说话行动。"
            ),
            user=(
                f"{perspective}\n"
                f"本时段发生的事：\n{self.events_brief(events)}"
                f"{self.CAST_INSTRUCTION}"
            ),
            context_docs=[self.world_brief(w)],
            civ=civ, genre="diary",
        )
        body, specs = self._split_cast(raw)
        mentioned = self._resolve_persons(w, civ, specs, focal, "diary", author=who)
        if author_id and author_id not in mentioned:
            mentioned.append(author_id)
        return Artifact(
            genre="diary", title=f"{author}的日记·{focal}年",
            body=body, year=focal, civ_id=civ_id, tick=w.tick_count,
            author=author, author_id=author_id, mentioned_persons=mentioned,
        )

    def decree(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A ruler's decree — only if something governance-relevant happened."""
        gov_words = ("动荡", "饥荒", "战争", "交战", "贸易", "革新", "辞世", "崭露头角")
        if not any(any(k in e.title for k in gov_words) for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)  # 诏令落款年从区间内随机取，散布开。
        raw = self._gen(
            system=(
                "你是该文明的统治者，颁布一道诏令/法令。语气威严、正式，"
                "针对本时段要事给出政令内容与理由。80-180字。以'奉天承运……'或"
                "符合该政体的口吻开头。"
            ),
            user=(
                f"颁布地：{civ.name}（政体={civ.government.value}）。落款 {year} 年。\n"
                f"本时段要事：\n{self.events_brief(events)}"
                f"{self.CAST_INSTRUCTION}"
            ),
            context_docs=[self.civ_card(civ), self.world_brief(w)],
            civ=civ, genre="decree",
        )
        body, specs = self._split_cast(raw)
        mentioned = self._resolve_persons(w, civ, specs, year, "decree")
        return Artifact(
            genre="decree", title=f"{civ.name}诏令·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.name}王廷", mentioned_persons=mentioned,
        )

    def scripture(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A religious text excerpt — only on weighty, fate-like events."""
        if not any(e.magnitude >= 1.5 for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)
        raw = self._gen(
            system=(
                "你是本信仰的经文抄写者，写一段经文/偈语/神谕来诠释本时段之事。"
                "语带玄机、韵律感，可含神祇之名。60-150字。"
            ),
            user=(
                f"信仰背景：{civ.religion}（{civ.name}）。落款 {year} 年。\n"
                f"需诠释之事：\n{self.events_brief(events)}"
                f"{self.CAST_INSTRUCTION}"
            ),
            context_docs=[self.civ_card(civ)],
            civ=civ, genre="scripture",
        )
        body, specs = self._split_cast(raw)
        mentioned = self._resolve_persons(w, civ, specs, year, "scripture")
        return Artifact(
            genre="scripture", title=f"{civ.religion}经文·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author=f"{civ.religion}祭司团", mentioned_persons=mentioned,
        )

    def minutes(self, w: World, events: list[Event]) -> Optional[Artifact]:
        """A council/parliament minutes doc — only if diplomacy or governance events."""
        if not any(e.source == "player" or len(e.involved_civs) >= 2 or "动荡" in e.title for e in events):
            return None
        civ = self.rng.choice(w.civs)
        year = self._year_in_span(w)
        raw = self._gen(
            system=(
                "你是会议书记，撰写一份议事会/长老会/议会的会议纪要。"
                "条目化、严肃、记录议题与议决。150-300字。"
            ),
            user=(
                f"会议方：{civ.name}（{civ.government.value}）。落款 {year} 年。\n"
                f"议事背景：\n{self.events_brief(events)}"
                f"{self.CAST_INSTRUCTION}"
            ),
            context_docs=[self.civ_card(civ)],
            civ=civ, genre="minutes",
        )
        body, specs = self._split_cast(raw)
        mentioned = self._resolve_persons(w, civ, specs, year, "minutes")
        return Artifact(
            genre="minutes", title=f"{civ.name}议事纪要·{year}年",
            body=body, year=year, civ_id=civ.id, tick=w.tick_count,
            author="议事会书记", mentioned_persons=mentioned,
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
        校验两类问题：① 既成事实违规（``_violations``）；② **正文为空**（LLM 偶发空返回，
        如 BodyGuard 不查会落盘空档案）。任一问题触发重生成。
        仍违规则返回最后一次结果并打印告警——宁可带瑕疵产出也不要丢档案。
        """
        art = make_art()
        for attempt in range(retries):
            vs = self._violations(art, w) if art is not None else []
            # 空 body 也视为违规：DeepSeek 偶发空返回，重生成可救回正文。
            if art is not None and not (art.body or "").strip():
                vs = vs + ["正文为空"]
            if not vs:
                return art
            print(f"[civsim] 校验发现 {len(vs)} 处问题，重生成(第{attempt+1}次): {vs}")
            art = make_art()
        # 仍违规：记录后放行（避免死循环/丢档案）。
        if art is not None:
            print(f"[civsim] 经 {retries} 次重生成仍有问题，放行: {self._violations(art, w)}")
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