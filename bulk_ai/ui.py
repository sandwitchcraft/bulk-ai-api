"""Shared look: one Rich theme and one questionary style, plus small formatting helpers.

Labels are dim, real data is bold or coloured: green = done/ok, red = failed, yellow = waiting
or needs attention, cyan = numbers and settings.
"""
from __future__ import annotations

from typing import Optional

import questionary
from questionary.prompts import common as _qcommon
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.theme import Theme

OK, FAIL, WARN, BULLET, SEP = "✓", "✗", "!", "•", "·"

THEME = Theme({
    "title": "bold cyan",
    "crumb": "dim",
    "heading": "bold",
    "label": "dim",
    "hint": "dim italic",
    "muted": "dim",
    "name": "bold",
    "num": "bold cyan",
    "path": "cyan",
    "ok": "green",
    "ok.bold": "bold green",
    "err": "red",
    "err.bold": "bold red",
    "warn": "yellow",
    "warn.bold": "bold yellow",
    "key": "bold reverse",
})

console = Console(theme=THEME)

# Menus use the same palette, minus cyan: menu rows are never highlighted, only data in them is
# coloured. The extra classes (ok, err, ...) are for those data tokens.
QSTYLE = questionary.Style([
    ("qmark", "fg:ansicyan bold"),
    ("question", "bold"),
    ("answer", "fg:ansicyan bold"),
    ("pointer", "bold"),
    ("highlighted", ""),
    ("selected", "fg:ansigreen bold"),
    ("separator", "bold"),
    ("instruction", "italic dim"),
    ("disabled", "italic dim"),
    ("text", ""),
    ("validation-toolbar", "bg:ansired fg:ansiwhite bold"),
    ("b", "bold"),
    ("muted", "dim"),
    ("ok", "fg:ansigreen"),
    ("err", "fg:ansired"),
    ("warn", "fg:ansiyellow"),
])
POINTER = "❯"

# Square checkboxes read as checkboxes; the default dots read as radio buttons.
if hasattr(_qcommon, "INDICATOR_SELECTED"):
    _qcommon.INDICATOR_SELECTED = "[✓]"
    _qcommon.INDICATOR_UNSELECTED = "[ ]"


# --- text ------------------------------------------------------------------

def path(p, text: Optional[str] = None) -> str:
    """A file-system path as markup, clickable in terminals that support hyperlinks: opens the
    folder (or, for a file, the folder holding it) in Finder / the file manager."""
    from pathlib import Path
    p = Path(p).expanduser()
    folder = p if p.is_dir() or not p.suffix else p.parent
    try:
        uri = folder.resolve().as_uri()
    except ValueError:
        return f"[path]{escape(text if text is not None else str(p))}[/]"
    return f"[path][link={uri}]{escape(text if text is not None else str(p))}[/link][/]"


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def qty(n: int, word: str, many: Optional[str] = None) -> str:
    """'[num]3[/] prompts'"""
    return f"[num]{n}[/] {word if n == 1 else many or word + 's'}"


def pct(n: int, total: int) -> str:
    """Whole percent that never rounds a partial count to 0% or 100%."""
    if not total:
        return "0%"
    text = f"{n * 100 / total:.0f}"
    if text == "0" and n:
        return "<1%"
    if text == "100" and n < total:
        return ">99%"
    return f"{text}%"


def tally(n: int, style: str) -> str:
    """A count in `style` when non-zero, dim when zero."""
    return f"[{style}]{n}[/]" if n else f"[muted]{n}[/]"


def tally_pct(n: int, total: int, style: str) -> str:
    """'[green]12[/] (40%)' for a results table cell."""
    return f"{tally(n, style)} [muted]({pct(n, total)})[/]"


def done_failed_left(ok: int, err: int, total: int) -> str:
    """'✓ 40 completed  ✗ 3 failed  ○ 0 not run' with each part coloured when non-zero."""
    left = total - ok - err
    return (
        f"[{'ok' if ok else 'muted'}]{OK} {ok} completed[/]  "
        f"[{'err' if err else 'muted'}]{FAIL} {err} failed[/]  "
        f"[{'warn' if left else 'muted'}]○ {left} not run[/]"
    )


def bullets(items, style: str = "") -> str:
    """One '• item' per line; items are escaped."""
    open_, close = (f"[{style}]", "[/]") if style else ("", "")
    return "\n".join(f"[muted]{BULLET}[/] {open_}{escape(str(i))}{close}" for i in items)


# --- blocks ----------------------------------------------------------------

def table(*columns, **kwargs) -> Table:
    """A table in the house style. Columns are names, or (name, justify) pairs."""
    t = Table(box=box.ROUNDED, border_style="dim", header_style="bold", **kwargs)
    for col in columns:
        name, justify = col if isinstance(col, tuple) else (col, "left")
        t.add_column(name, justify=justify)
    return t


def kv(rows) -> None:
    """Aligned 'label  value' lines. Values are Rich markup; None rows are skipped."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="label", no_wrap=True)
    grid.add_column()
    for row in rows:
        if row is not None:
            grid.add_row(*row)
    console.print(grid)


def heading(text: str) -> None:
    console.print(f"[heading]{text}[/]")


def ok(msg: str) -> str:
    return f"[ok.bold]{OK}[/] {msg}"


def fail(msg: str) -> str:
    return f"[err.bold]{FAIL}[/] {msg}"


def warn(msg: str) -> str:
    return f"[warn.bold]{WARN}[/] {msg}"


def info(msg: str) -> str:
    return f"[muted]{BULLET}[/] {msg}"
