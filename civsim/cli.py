"""
CLI 入口 —— 可玩的交互界面。

两种模式：
* ``python -m civsim.cli new-world config.yaml`` —— 从配置开始新局
* ``python -m civsim.cli resume saves/world.json`` —— 读档继续

主循环里玩家可：推进 N 个 tick、注入自由文本事件、浏览档案库、查看文明状态、存档退出。
显示用 ``rich`` 提升可读性。LLM 后端由环境变量自动选择（见 providers.get_provider）；
无 key 时走 mock，整个流程开箱即可演练。

本模块只做「组装与交互」，不含任何模拟/叙事逻辑——它是 engine/generators/archive 之上
的薄壳。改命令语义时优先去问 engine 提供的钩子（如 queue_player_event），不要在此
直接改 World 的字段。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from .archive import Archive
from .engine import Simulation
from .models import (
    Biome, Civilization, Era, Event, Government, NamingStyle, Organization,
    OrgType, Relation, SocialClass, TechLevel, VoiceStyle, World,
)
from .providers import get_provider, get_provider_from_config

console = Console()


# ---------------------------------------------------------------------------
# 从配置构造世界
# ---------------------------------------------------------------------------


def _build_organizations(civ_cfg: dict, orgs_cfg: list) -> list[Organization]:
    """从 config 的 organizations 块构造初始组织种子。

    每个组织：name/org_type/parent/ founded_year/status。parent 用名引用，转 id。
    """
    orgs: list[Organization] = []
    # 用配置中的 name 作为内部查找键；id 形如 {civ_id}-org{i}-{founded}。
    cid = civ_cfg["id"]
    name_to_id: dict[str, str] = {}
    for i, o in enumerate(orgs_cfg or [], 1):
        name = o.get("name", f"组织{i}")
        oid = f"{cid}-org{i}-{o.get('founded_year', 0)}"
        name_to_id[name] = oid
    for i, o in enumerate(orgs_cfg or [], 1):
        name = o.get("name", f"组织{i}")
        oid = name_to_id[name]
        parent = o.get("parent", "")
        parent_id = name_to_id.get(parent) if parent else None
        try:
            ot = OrgType(o.get("org_type", "other"))
        except Exception:
            ot = OrgType.OTHER
        orgs.append(Organization(
            id=oid, name=name, org_type=ot, civ_id=cid, parent_org_id=parent_id,
            founded_year=o.get("founded_year", 0), status=o.get("status", "stable"),
            last_mentioned_year=o.get("founded_year", 0),
            history_entries=[f"{o.get('founded_year', 0)}年：初始组织种子。"],
        ))
    return orgs


def build_world(cfg_path: str) -> World:
    """读取 YAML 配置，构造初始 :class:`World`。

    配置字段直接对应 models.py 里的模型；tech_level 用整数（见 TechLevel 枚举值）。
    relations 矩阵每条形如 [a, b, rel]，会双向设置（双方互持同一立场）。
    扩展初始设定：改 config.yaml 即可，无需改此函数。
    """
    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    wc = cfg["world"]

    civs: list[Civilization] = []
    for c in cfg.get("civs", []):
        naming_cfg = c.get("naming", {})
        civs.append(Civilization(
            id=c["id"], name=c["name"], biome=Biome(c["biome"]),
            population=c.get("population", 1000),
            food=c.get("food", 100.0), wealth=c.get("wealth", 50.0),
            tech_level=TechLevel(c.get("tech_level", 0)),
            government=Government(c.get("government", "tribal")),
            religion=c.get("religion", "animism"),
            culture_traits=c.get("culture_traits", []),
            color=c.get("color", "white"),
            # 命名规范与社会结构（均有默认值，缺省也能跑）。
            naming=NamingStyle(
                roots=naming_cfg.get("roots", []),
                prefixes=naming_cfg.get("prefixes", []),
                suffixes=naming_cfg.get("suffixes", []),
                clans=naming_cfg.get("clans", []),
                template=naming_cfg.get("template", "{root}"),
                style_note=naming_cfg.get("style_note", ""),
                gendered=naming_cfg.get("gendered", False),
            ),
            social_classes=[SocialClass(s) for s in c.get("social_classes", ["commoner"])],
            role_pool=c.get("role_pool", []),
            voice=VoiceStyle(
                general=voice_cfg.get("general", "庄重质朴"),
                by_genre=voice_cfg.get("by_genre", {}),
            ) if (voice_cfg := c.get("voice")) else VoiceStyle(),
            organizations=_build_organizations(c, c.get("organizations", [])),
        ))
    # 应用外交关系矩阵：双向写入。
    for a, b, rel in cfg.get("relations", []):
        r = Relation(rel)
        civs_map = {c.id: c for c in civs}
        if a in civs_map: civs_map[a].relations[b] = r
        if b in civs_map: civs_map[b].relations[a] = r

    era = cfg.get("era", {})
    return World(
        name=wc["name"], seed=wc.get("seed", 0),
        years_per_tick=wc.get("years_per_tick", 25),
        continents=wc.get("continents", []),
        civs=civs,
        eras=[Era(name=era.get("name", "黎明纪"), start_year=era.get("start_year", 0),
                  description=era.get("description", ""))] if era else [],
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def show_status(sim: Simulation) -> None:
    """用 rich 表格打印所有文明的当前状态概览。"""
    w = sim.world
    console.rule(f"[bold]{w.name} · 第 {w.year} 年 · 第 {w.tick_count} 纪元之轮")
    t = Table(show_header=True, header_style="bold magenta")
    for col in ("文明", "人口", "粮储", "财富", "科技", "政体", "稳定", "外交"):
        t.add_column(col)
    for c in w.civs:
        rels = " ".join(f"{civ_id[:3]}:{v.value}" for civ_id, v in c.relations.items()) or "-"
        t.add_row(c.name, str(c.population), f"{c.food:.0f}", f"{c.wealth:.0f}",
                  c.tech_level.name, c.government.value, f"{c.stability:.0f}", rels)
    console.print(t)


def show_tick(report, sim: Simulation) -> None:
    """打印单个 tick 的结果摘要：年份、事件数、档案数，逐条列出事件。"""
    if report.empty:
        console.print("[dim]本时段波澜不惊，未产出档案。[/dim]")
        return
    console.print(Panel.fit(
        f"[bold]{report.year} 年[/bold] · 发生事件 {len(report.events)} 件 · "
        f"产出档案 {report.artifacts_written} 篇",
        style="cyan",
    ))
    for ev in report.events:
        tag = {"player": "[yellow]玩家[/yellow]", "emergent": "[blue]涌现[/blue]",
               "system": "[grey]系统[/grey]"}.get(ev.source, ev.source)
        console.print(f"  {tag} {ev.title} — {ev.description}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run(sim: Simulation, archive: Archive) -> None:
    """主交互循环。逐条读取命令并分发；命令语义见顶部模块 docstring。

    所有异常在命令层捕获并打印，保证一条坏命令不会让整个会话崩溃。
    """
    console.print(Panel.fit(
        "[bold]尤弥尔·文明演化[/bold]\n"
        "命令: [cyan]n[/cyan]=推进若干tick(可带数字如 n5)  "
        "[cyan]e[/cyan]=插入事件  [cyan]a[/cyan]=浏览档案  "
        "[cyan]s[/cyan]=查看文明  [cyan]c[/cyan]=清除全部档案  "
        "[cyan]save[/cyan]=存档  [cyan]q[/cyan]=退出",
        style="green",
    ))
    show_status(sim)

    while True:
        cmd = Prompt.ask("\n[bold]>[/bold]").strip()
        if not cmd:
            continue
        head = cmd[0].lower()
        cmd_l = cmd.lower()
        try:
            # 多字母整词命令优先于单字母首字符匹配，避免「save」被「s」拦截。
            if cmd_l == "save" or cmd_l.startswith("save "):
                p = cmd.split(maxsplit=1)[1] if " " in cmd else "saves/world.json"
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                sim.save(p)
                console.print(f"[green]已存档至 {p}[/green]")
            elif head == "n":
                n = 1
                if cmd[1:].strip().isdigit():
                    n = max(1, int(cmd[1:].strip()))
                for _ in range(n):
                    report = sim.tick()
                    show_tick(report, sim)
                show_status(sim)
            elif head == "e":
                title = Prompt.ask("事件标题")
                desc = Prompt.ask("事件描述")
                sim.queue_player_event(title, desc)
                console.print("[green]已加入下个 tick 的事件队列。[/green]")
            elif head == "a":
                browse_archive(archive)
            elif head == "s":
                show_status(sim)
                for c in sim.world.civs:
                    console.print(f"  • {c.name} — {c.religion} · "
                                  f"特质:{'/'.join(c.culture_traits) or '无'}")
            elif head == "c":
                # 清除档案需二次确认，避免误删玩家一局的心血。
                if Prompt.ask("确认清除全部文本档案？(y/N)", default="N").lower() == "y":
                    n = archive.clear()
                    console.print(f"[green]已清除 {n} 篇档案。[/green]")
                else:
                    console.print("[dim]已取消。[/dim]")
            elif head == "q":
                console.print("[dim]再见，文明永续。[/dim]")
                break
            else:
                console.print(f"[red]未知命令: {cmd}[/red]")
        except Exception as exc:
            console.print(f"[red]出错: {exc}[/red]")


def browse_archive(archive: Archive) -> None:
    """浏览档案库：按体裁筛选 -> 列表 -> 选编号阅读全文。"""
    console.print("[bold]档案库[/bold]  (genre 可选: chronicle/diary/decree/scripture/minutes)")
    genre = Prompt.ask("体裁(回车=全部)", default="")
    rows = archive.list(genre=genre or None)
    if not rows:
        console.print("[dim]尚无档案。[/dim]")
        return
    t = Table(show_header=True, header_style="bold blue")
    for col in ("#", "体裁", "年份", "文明", "标题"):
        t.add_column(col)
    for i, r in enumerate(rows):
        t.add_row(str(i), r["genre"], str(r["year"]), r["civ_id"] or "-", r["title"])
    console.print(t)
    pick = Prompt.ask("输入编号阅读(回车返回)", default="")
    if pick.strip().isdigit() and 0 <= int(pick) < len(rows):
        console.print(Panel(archive.read(rows[int(pick)]), title=rows[int(pick)]["title"]))


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        console.print("用法:\n"
                      "  python -m civsim.cli new-world config.yaml\n"
                      "  python -m civsim.cli resume saves/world.json\n"
                      "  python -m civsim.cli clear          # 清除上次游戏生成的全部文本档案")
        return 1

    mode = argv[0]

    # clear 子命令：清空 archives/ 后直接退出，不进入游戏。
    if mode == "clear":
        archive = Archive("archives")
        n = archive.clear()
        archive.close()
        console.print(f"[green]已清除 {n} 篇文本档案。[/green]"
                      "[dim]（存档 saves/ 未动；如需一并删除请手动删该目录）[/dim]")
        return 0

    # 优先用 llm.yaml（用户接入接口）；没有则回退环境变量/mock。
    # 支持第二个位置参数指定配置：python -m civsim.cli new-world config.yaml my-llm.yaml
    llm_cfg = None
    if len(argv) >= 3 and argv[2].endswith((".yaml", ".yml")):
        llm_cfg = argv[2]
        # 用户显式指定了配置文件但文件不存在——明确警告，避免静默降级 mock 让人误以为 LLM 在工作。
        if not Path(llm_cfg).exists():
            console.print(f"[yellow]警告：指定的 LLM 配置文件 {llm_cfg} 不存在，"
                          f"将回退到 llm.yaml 或 mock。常见误因：把第二个参数（存档路径）"
                          f"误填成了 yaml 文件名。[/yellow]")
    provider, source = get_provider_from_config(llm_cfg or "llm.yaml")
    console.print(f"[dim]LLM 后端: {provider.name}（来源: {source}）[/dim]")
    if provider.name == "mock":
        console.print("[yellow]提示：当前为 mock 后端，产出的是占位文本（[mock-llm] 开头），"
                      "非真实 LLM 生成。检查 llm.yaml 是否正确配置了 provider/model/base_url/api_key。[/yellow]")
    archive = Archive("archives")

    if mode == "new-world":
        cfg = argv[1] if len(argv) > 1 else "config.yaml"
        world = build_world(cfg)
        sim = Simulation(world, provider=provider, archive=archive)
    elif mode == "resume":
        sim = Simulation.load(argv[1], provider=provider)
        sim.archive = archive
    else:
        console.print(f"[red]未知模式: {mode}[/red]")
        return 1

    run(sim, archive)
    archive.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
