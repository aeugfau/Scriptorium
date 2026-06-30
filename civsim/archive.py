"""
Artifact storage — the civilization's "library".

Every piece of generated text (chronicle chapter, diary entry, decree, ...)
becomes an :class:`Artifact`. Artifacts are persisted two ways:

* **SQLite index** (``archives/index.db``): queryable metadata for fast
  filtering by year / civilization / genre / keyword.
* **Markdown files** (``archives/<genre>/<slug>.md``): human-readable, so the
  archive doubles as a browsable document collection the player can read
  outside the game.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class Artifact:
    """A single generated text document."""

    genre: str          # "chronicle" | "diary" | "decree" | "scripture" | "minutes"
    title: str
    body: str
    year: int
    civ_id: Optional[str] = None
    tick: int = 0
    author: Optional[str] = None      # person name or institution（展示用）
    author_id: Optional[str] = None   # 若作者是个体人物，存其 Person.id（校验用，避免重名误判）
    mentioned_persons: list[str] = field(default_factory=list)  # 本篇提及的人物 id，便于按人物检索档案

    @property
    def slug(self) -> str:
        """文件名安全的 slug，由「年份-标题」生成，**保留中文**。

        为什么保留中文：Windows / macOS / Linux 文件系统都原生支持 Unicode 文件名，
        用中文标题命名更直观（玩家在文件管理器里一眼能认出是哪篇档案）。
        早期实现把非 ASCII 全剥光，导致中文标题只剩漏出的数字（如「诺尔海姆诏令·25年」
        → ``00050-50.md``），既丑又无法区分，属 bug。

        本方法只做「文件系统合法性」处理：把非法字符（``/ \\ : * ? " < > |``）
        及控制字符替换为 ``-``，折叠连续分隔符，去首尾分隔符，截断到 80 字符。
        不再强制小写——中文无大小写，且小写会让英文专名变形。
        """
        s = f"{self.year:04d}-{self.title}"
        # 替换 Windows/Unix 文件系统非法字符为连字符，并去掉控制字符。
        s = re.sub(r'[\/\\:*?"<>|\x00-\x1f]', "-", s)
        # 折叠连续分隔符、去首尾。
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return (s or "untitled")[:80]


class Archive:
    """Persists artifacts to disk + SQLite and lets the player browse them."""

    GENRES = ("chronicle", "diary", "decree", "scripture", "minutes")

    def __init__(self, root: str | Path = "archives"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.db")
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genre TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER NOT NULL,
                civ_id TEXT,
                tick INTEGER,
                author TEXT,
                slug TEXT NOT NULL,
                path TEXT NOT NULL,
                preview TEXT NOT NULL
            )
            """
        )
        self.db.commit()

    # --- writing -----------------------------------------------------------

    def add(self, art: Artifact) -> Path:
        """持久化一篇档案：写 Markdown 文件并登记进 SQLite。

        双写策略——文件给人读，数据库给检索。两处必须一致，故都在此方法内完成。
        Markdown 含类 YAML front matter（genre/title/year/...），便于外部工具解析。

        Args:
            art: 待写入的档案。

        Returns:
            落盘的 Markdown 文件路径。
        """
        genre_dir = self.root / art.genre
        genre_dir.mkdir(parents=True, exist_ok=True)
        path = genre_dir / f"{art.slug}.md"

        # 同名去重：同一 tick 不同文明可能产出标题相近的档案，或重跑模拟产生
        # 完全相同的标题。已存在文件则在文件名后追加 ``-2``、``-3``……，避免覆盖。
        if path.exists():
            stem, n = art.slug, 2
            while (genre_dir / f"{stem}-{n}.md").exists():
                n += 1
            path = genre_dir / f"{stem}-{n}.md"

        # Build a readable Markdown document with YAML-ish front matter.
        front = [
            f"genre: {art.genre}",
            f"title: {art.title}",
            f"year: {art.year}",
            f"civ: {art.civ_id or '-'}",
            f"author: {art.author or '-'}",
            f"tick: {art.tick}",
            "---",
        ]
        body = "\n".join([*front, "", f"# {art.title}", "", art.body.strip(), ""])
        path.write_text(body, encoding="utf-8")

        preview = art.body.strip().replace("\n", " ")[:140]
        self.db.execute(
            "INSERT INTO artifacts (genre,title,year,civ_id,tick,author,slug,path,preview)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (art.genre, art.title, art.year, art.civ_id, art.tick, art.author,
             art.slug, str(path), preview),
        )
        self.db.commit()
        return path

    # --- reading -----------------------------------------------------------

    def list(
        self,
        genre: Optional[str] = None,
        civ_id: Optional[str] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """按体裁/文明/年份区间筛选档案，按时间升序返回元数据行。

        返回的是 ``sqlite3.Row``，可用列名取值（``row["title"]`` 等）；
        全文用 ``read(row)`` 读。任何筛选参数为 None 即表示不过滤。
        """
        q = "SELECT * FROM artifacts WHERE 1=1"
        params: list = []
        if genre:
            q += " AND genre = ?"
            params.append(genre)
        if civ_id:
            q += " AND civ_id = ?"
            params.append(civ_id)
        if year_from is not None:
            q += " AND year >= ?"
            params.append(year_from)
        if year_to is not None:
            q += " AND year <= ?"
            params.append(year_to)
        q += " ORDER BY year ASC, id ASC LIMIT ?"
        params.append(limit)
        return self.db.execute(q, params).fetchall()

    def search(self, keyword: str, limit: int = 50) -> list[sqlite3.Row]:
        q = "SELECT * FROM artifacts WHERE title LIKE ? OR preview LIKE ? ORDER BY year ASC LIMIT ?"
        like = f"%{keyword}%"
        return self.db.execute(q, (like, like, limit)).fetchall()

    def read(self, row: sqlite3.Row) -> str:
        return Path(row["path"]).read_text(encoding="utf-8")

    def counts_by_genre(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT genre, COUNT(*) AS n FROM artifacts GROUP BY genre ORDER BY genre"
        ).fetchall()
        return {r["genre"]: r["n"] for r in rows}

    def close(self) -> None:
        self.db.close()

    # --- clearing ---------------------------------------------------------

    def clear(self) -> int:
        """清除所有已生成的文本档案，返回被清除的条目数。

        两处一起清：
        - SQLite 索引：``DELETE FROM artifacts``（并记下条数作返回值）。
        - Markdown 文件：删除 ``archives/<genre>/`` 下所有 ``.md`` 文件。
          保留空目录结构与 ``.gitkeep``，让游戏无需重建目录即可继续写入。

        这个方法是「一键清除上次游戏」的核心，供 CLI 的 ``clear`` 子命令与
        游戏内 ``c`` 键共同调用。注意：只清档案库，不动 ``saves/`` 存档——
        存档是另一回事，要删存档请手动删 ``saves/``。
        """
        n = self.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        self.db.execute("DELETE FROM artifacts")
        self.db.commit()
        # 删除每个体裁目录下的 .md 文件（保留 .gitkeep 与目录本身）。
        for genre_dir in self.root.iterdir():
            if not genre_dir.is_dir():
                continue
            for f in genre_dir.glob("*.md"):
                f.unlink()
        return n


def archive_many(archive: Archive, arts: Iterable[Artifact]) -> int:
    """Convenience: persist many artifacts, return count."""
    n = 0
    for a in arts:
        archive.add(a)
        n += 1
    return n
