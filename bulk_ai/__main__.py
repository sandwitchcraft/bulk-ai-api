"""Interactive menus. Run with `bulk-ai` or `python -m bulk_ai`."""
from __future__ import annotations

import asyncio
import copy
import csv
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import questionary
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import merge_styles
from questionary import Choice, Separator
from rich.markup import escape
from rich.table import Table

from . import __version__, config, ui
from .export import export
from .project import Project, Run, empty_run_config, is_project_dir, read_prompts, target_id
from .providers import ANTHROPIC_DEFAULT_MAX_TOKENS, APIError, list_models, make_client
from .runner import run_project
from .ui import console, plural, qty

BACK = "__back__"
CONFIGURE = "__configure__"
MODEL_FILTER_THRESHOLD = 40
RECOMMENDED_MAX_TOKENS = 4096
PREVIEW_ROWS = 3
LONG_LIST = 10  # past this, menus drop number shortcuts and pin Back to the top
ESC_HINT = "[key] Esc [/] [hint]back[/]"
CHECKBOX_HINT = "(↑↓ move, Space toggles, a toggles all, Enter confirms)"

# Messages that should survive the next screen clear (results of the last action).
_notices: list = []


class ToProjectMenu(Exception):
    pass


class ToMainMenu(Exception):
    """Unwind straight back to the main menu."""


class QuitApp(Exception):
    """Leave the app."""


# --- terminal helpers ------------------------------------------------------

def screen(*crumbs: str, hint: str = ESC_HINT) -> None:
    """Clear the terminal (including scrollback), then show a breadcrumb header and pending notices."""
    if console.is_terminal:
        console.file.write("\033[3J")  # scrollback; console.clear() only wipes the visible screen
        console.clear()
    if crumbs:
        parts = [f"[crumb]{escape(c)}[/]" for c in crumbs[:-1]] + [f"[title]{escape(crumbs[-1])}[/]"]
        bar = Table.grid(expand=True)
        bar.add_column()
        bar.add_column(justify="right", no_wrap=True)
        bar.add_row(" [crumb]›[/] ".join(parts), hint)
        console.print(bar)
        console.rule(style="dim")
        console.print()
    for msg in _notices:
        console.print(msg)
    if _notices:
        console.print()
    _notices.clear()


def say(msg: str) -> None:
    """Print now and again after the next screen clear."""
    console.print(msg)
    _notices.append(msg)


def ask(question, footer=None):
    """question.ask() in the house style, except Esc returns BACK. Ctrl-C still returns None.
    `footer` is a list of (style, text) tokens shown on a line under the prompt."""
    kb = KeyBindings()

    @kb.add(Keys.Escape, eager=True)
    def _back(event):
        event.app.exit(result=BACK)

    app = question.application
    app.key_bindings = merge_key_bindings([app.key_bindings, kb]) if app.key_bindings else kb
    app.style = merge_styles([app.style, ui.QSTYLE])
    app.ttimeoutlen = 0.1  # how long a lone Esc waits to see if it starts an escape sequence
    if footer:
        old = app.layout
        app.layout = Layout(
            HSplit([old.container, Window(FormattedTextControl(footer), height=1)]),
            focused_element=old.current_window,
        )
    return question.ask()


def menu(message: str, choices: list, default=None, footer=None):
    """A select list, numbered 1-9, 0 when it is short enough."""
    short = len(choices) <= LONG_LIST
    uniform_titles(choices)
    if short:
        # Number the choices ourselves: questionary leaves styled (list) titles unnumbered.
        keys = iter("1234567890")
        for c in choices:
            if c.disabled:  # also skips Separators
                continue
            c.shortcut_key = next(keys)
            c.title = [("class:text", f"{c.shortcut_key}) ")] + c.title
    return ask(questionary.select(
        message, choices=choices, default=default, use_shortcuts=short, pointer=ui.POINTER,
        instruction="(↑↓ or number keys, then Enter)" if short else "(↑↓, then Enter)",
    ), footer=footer)


def uniform_titles(choices: list) -> None:
    """Give every selectable choice a styled (list) title. questionary highlights plain-string
    titles (cursor row, default row) but not styled ones; this way every row looks the same."""
    for c in choices:
        if not c.disabled and isinstance(c.title, str):
            c.title = [("class:text", c.title)]


def checkbox(message: str, choices: list, **kwargs):
    uniform_titles(choices)
    return ask(questionary.checkbox(
        message, choices=choices, pointer=ui.POINTER, instruction=CHECKBOX_HINT, **kwargs
    ))


def back(label: str = "Back", value=BACK) -> Choice:
    return Choice(label, value)


def labeled(label: str, value: str, cls: str = "b", note: str = "") -> list:
    """Choice title 'Label: value' with the value styled."""
    title = [("class:text", f"{label}: "), (f"class:{cls}", value)]
    if note:
        title.append(("class:text", f"  {note}"))
    return title


def pause() -> None:
    ask(questionary.press_any_key_to_continue("Press any key to continue..."))


def open_external(path: Path) -> None:
    """Open a file in the OS's default app for it."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        say(ui.ok(f"Opened {ui.path(path, path.name)}."))
    except OSError as e:
        say(ui.fail(f"Couldn't open {ui.path(path)}: [err]{escape(str(e))}[/]"))


# --- small helpers ---------------------------------------------------------

def clean_path(raw: str) -> Path:
    """Accept paths pasted or dragged into the terminal (quotes, escaped spaces)."""
    raw = raw.strip().strip("'\"").replace("\\ ", " ")
    return Path(raw).expanduser()


def optional_number(raw: Optional[str], cast):
    raw = (raw or "").strip()
    return cast(raw) if raw else None


def is_number(cast, required=False):
    def check(raw: str):
        if not raw.strip():
            return "Enter a number" if required else True
        try:
            cast(raw.strip())
            return True
        except ValueError:
            return "Enter a number" if required else "Enter a number, or leave blank"
    return check


def positive_int(raw: str):
    return raw.strip().isdigit() and int(raw) > 0 or "Enter a positive whole number"


def fmt_time(iso: Optional[str]) -> str:
    """HH:MM DD/MM/YY in local time."""
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M %d/%m/%y")
    except ValueError:
        return iso


def time_markup(iso: Optional[str]) -> str:
    return f"[name]{fmt_time(iso)}[/]" if iso else "[muted]never[/]"


def one_line(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def key_state(provider: dict) -> tuple:
    """(style, text) describing where the provider's API key comes from."""
    key, source = config.get_key(provider)
    if not key:
        return ("muted", "– optional") if not provider.get("builtin") else ("err", f"{ui.FAIL} missing")
    where = {"env": f"env ${provider.get('key_env')}", "keyring": "keychain", "file": "keys file"}[source]
    return "ok", f"{ui.OK} {where}"


def key_status(provider: dict) -> str:
    style, text = key_state(provider)
    return f"[{style}]{escape(text)}[/]"


def has_key(provider: dict) -> bool:
    return bool(config.get_key(provider)[0]) or not provider.get("builtin")


def models_markup(models: list) -> str:
    return ui.bullets(models, "name") if models else "[muted]none chosen[/]"


async def _fetch_models(provider: dict, key: Optional[str]) -> list:
    async with make_client() as client:
        return await list_models(client, provider, key)


# --- connection status -----------------------------------------------------
# Checked live (by listing models) the first time a provider is shown, then cached for the
# session. Anything that changes a provider's key or URL clears the cache.

_status: dict = {}


def forget_status() -> None:
    _status.clear()


async def _check(cfg: config.Config, pids: list) -> list:
    async with make_client() as client:
        async def one(pid: str) -> str:
            p = cfg.provider(pid)
            if p is None:
                return "provider removed"
            key, _ = config.get_key(p)
            if not key and p.get("builtin"):
                return "no API key"
            try:
                await asyncio.wait_for(list_models(client, p, key), timeout=15)
                return "ok"
            except APIError as e:
                return f"failed: {e}"
            except asyncio.TimeoutError:
                return "failed: timed out"
            except Exception as e:
                return f"failed: {type(e).__name__}: {e}"

        return await asyncio.gather(*(one(pid) for pid in pids))


def connection_status(cfg: config.Config, pids) -> dict:
    """{provider_id: "ok" | "no API key" | "provider removed" | "failed: ..."}"""
    todo = [pid for pid in dict.fromkeys(pids) if pid not in _status]
    if todo:
        with console.status("Checking connections..."):
            _status.update(zip(todo, asyncio.run(_check(cfg, todo))))
    return {pid: _status[pid] for pid in pids}


def status_style(status: str) -> tuple:
    """(style, text) for a connection status."""
    if status == "ok":
        return "ok", f"{ui.OK} ok"
    if status == "no API key":
        return "warn", f"{ui.WARN} no API key"
    return "err", f"{ui.FAIL} {one_line(status, 60)}"


def status_markup(status: str) -> str:
    style, text = status_style(status)
    return f"[{style}]{escape(text)}[/]"


# --- providers -------------------------------------------------------------

def providers_menu(cfg: config.Config) -> None:
    while True:
        screen("Providers & API keys")
        table = ui.table("Provider", "Base URL", "API key", ("Parallel", "right"), "Models")
        for p in cfg.providers:
            table.add_row(
                f"[name]{escape(p['name'])}[/]", f"[path]{escape(p['base_url'])}[/]", key_status(p),
                f"[num]{p.get('concurrency', config.DEFAULT_CONCURRENCY)}[/]", models_markup(p["models"]),
            )
        console.print(table)
        console.print()

        choices = []
        for p in cfg.providers:
            style, text = key_state(p)
            choices.append(Choice(
                [("class:b", p["name"]), ("class:muted", "  "), (f"class:{style}", text)], p["id"]
            ))
        choices += [Choice("+ Add a custom OpenAI-compatible provider", "__add__"), back()]
        pick = menu("Pick a provider to configure:", choices)
        if pick in (None, BACK):
            return
        if pick == "__add__":
            p = add_custom_provider(cfg)
            if p:
                provider_menu(cfg, p)
        else:
            provider_menu(cfg, cfg.provider(pick))


def add_custom_provider(cfg: config.Config) -> Optional[dict]:
    name, base_url, parallel = "", "http://localhost:11434/v1", str(config.DEFAULT_CONCURRENCY)
    step = 0
    while step < 3:
        screen("Providers & API keys", f"Add custom provider · step {step + 1} of 3")
        if step == 0:
            answer = ask(questionary.text(
                "Display name (e.g. OpenRouter, Local Ollama):",
                default=name,
                validate=lambda s: bool(s.strip()) or "Name required",
            ))
        elif step == 1:
            ui.kv([("Name", f"[name]{escape(name)}[/]")])
            console.print()
            answer = ask(questionary.text(
                "Base URL (the part before /chat/completions):",
                default=base_url,
                validate=lambda s: bool(s.strip()) or "URL required",
            ))
        else:
            ui.kv([("Name", f"[name]{escape(name)}[/]"), ("Base URL", f"[path]{escape(base_url)}[/]")])
            console.print()
            answer = ask(questionary.text(
                "Parallel requests (how many prompts to send at once):",
                default=parallel,
                validate=positive_int,
            ))
        if answer is None or (answer == BACK and step == 0):
            return None
        if answer == BACK:
            step -= 1
            continue
        if step == 0:
            name = answer.strip()
        elif step == 1:
            base_url = answer.strip()
        else:
            parallel = answer.strip()
        step += 1
    p = cfg.add_custom_provider(name, base_url, int(parallel))
    say(ui.ok(f"Added [name]{escape(p['name'])}[/]."))
    return p


def set_key_prompt(p: dict) -> bool:
    """Ask for and store an API key. Returns True if one was saved."""
    key = ask(questionary.password(f"Paste {p['name']} API key:"))
    if key in (None, BACK) or not key.strip():
        return False
    where = config.set_key(p, key.strip())
    forget_status()
    if where == "file":
        say(ui.warn(
            "No OS keychain available; saved to "
            f"{ui.path(config.config_dir() / 'keys.json')} (readable only by you)."
        ))
    else:
        say(ui.ok(f"Saved the [name]{escape(p['name'])}[/] key to the OS keychain."))
    return True


def add_model_id(cfg: config.Config, p: dict) -> None:
    mid = ask(questionary.text("Model ID (exactly as the API expects it):"))
    if mid in (None, BACK) or not mid.strip():
        return
    if mid.strip() not in p["models"]:
        p["models"].append(mid.strip())
        cfg.save()
    say(ui.ok(f"Added [name]{escape(mid.strip())}[/] to [name]{escape(p['name'])}[/]."))


def provider_menu(cfg: config.Config, p: dict) -> None:
    while True:
        screen("Providers & API keys", p["name"])
        parallel = p.get("concurrency", config.DEFAULT_CONCURRENCY)
        ui.kv([
            ("Base URL", f"[path]{escape(p['base_url'])}[/]"),
            ("API key", key_status(p)),
            ("Connection", status_markup(_status[p["id"]])) if p["id"] in _status else None,
            ("Parallel", f"[num]{parallel}[/] requests at once"),
            ("Models", models_markup(p["models"])),
        ])
        console.print()
        choices = [
            Choice("Set / replace API key", "key"),
            Choice("Test connection", "test"),
            Choice("Choose models (fetch list from provider)", "models"),
            Choice("Add a model ID manually", "manual"),
            Choice(labeled("Parallel requests", str(parallel)), "parallel"),
        ]
        if not p.get("builtin"):
            choices.append(Choice("Edit base URL", "url"))
        choices.append(Choice("Remove saved API key", "delkey"))
        if not p.get("builtin"):
            choices.append(Choice("Delete this provider", "delete"))
        choices.append(back())
        action = menu("What do you want to do?", choices)
        if action in (None, BACK):
            return

        if action == "key":
            set_key_prompt(p)
        elif action == "test":
            key, _ = config.get_key(p)
            try:
                with console.status("Connecting..."):
                    models = asyncio.run(_fetch_models(p, key))
                _status[p["id"]] = "ok"
                say(ui.ok(f"Connected: {qty(len(models), 'chat model')} available."))
            except APIError as e:
                _status[p["id"]] = f"failed: {e}"
                say(ui.fail(f"Connection failed: [err]{escape(str(e))}[/]"))
        elif action == "models":
            choose_models(cfg, p)
        elif action == "manual":
            add_model_id(cfg, p)
        elif action == "parallel":
            raw = ask(questionary.text(
                f"Parallel requests for {p['name']}:", default=str(parallel), validate=positive_int
            ))
            if raw not in (None, BACK):
                p["concurrency"] = int(raw)
                cfg.save()
        elif action == "delkey":
            config.delete_key(p)
            forget_status()
            say(ui.ok("Removed the saved key [muted](environment variables are not affected)[/]."))
        elif action == "url":
            url = ask(questionary.text("Base URL:", default=p["base_url"]))
            if url not in (None, BACK) and url.strip():
                p["base_url"] = url.strip().rstrip("/")
                cfg.save()
                forget_status()
        elif action == "delete":
            if ask(questionary.confirm(f"Delete {p['name']}?", default=False)) is True:
                config.delete_key(p)
                cfg.remove_provider(p["id"])
                forget_status()
                say(ui.ok(f"Deleted [name]{escape(p['name'])}[/]."))
                return


def choose_models(cfg: config.Config, p: dict) -> None:
    key, _ = config.get_key(p)
    try:
        with console.status("Fetching models..."):
            available = asyncio.run(_fetch_models(p, key))
    except APIError as e:
        say(ui.fail(f"Couldn't fetch models: [err]{escape(str(e))}[/]"))
        say(ui.info("You can still add model IDs manually."))
        return
    if len(available) > MODEL_FILTER_THRESHOLD:
        text = ask(questionary.text(
            f"{len(available)} models found. Filter (substring, blank = show all):"
        ))
        if text in (None, BACK):
            return
        if text.strip():
            available = [m for m in available if text.strip().lower() in m.lower()]
    options = sorted(set(available) | set(p["models"]))
    if not options:
        say(ui.warn("No models matched."))
        return
    picked = checkbox(
        "Select the models you want available for projects:",
        [Choice(m, m, checked=m in p["models"]) for m in options],
    )
    if picked in (None, BACK):
        return
    # Keep previously chosen models that were filtered out of this view.
    kept = [m for m in p["models"] if m not in options]
    p["models"] = kept + picked
    cfg.save()
    say(ui.ok(f"[name]{escape(p['name'])}[/] now offers {qty(len(p['models']), 'model')}."))


# --- run settings ----------------------------------------------------------

MIXED = object()  # a per-model setting that differs between models

# (step number, step count) while the new-run wizard is walking its steps, else None.
_wizard_step: Optional[tuple] = None


def provider_name(cfg: config.Config, pid: str) -> str:
    p = cfg.provider(pid)
    return p["name"] if p else pid


def target_markup(cfg: config.Config, t: dict) -> str:
    return f"[name]{escape(t['model'])}[/] [muted]{escape(provider_name(cfg, t['provider']))}[/]"


def uniform(targets: list, field: str):
    """The value of `field` shared by every target, MIXED if they differ, None if no targets."""
    values = {t.get(field) for t in targets}
    if len(values) > 1:
        return MIXED
    return values.pop() if values else None


def describe(value) -> str:
    return "default" if value is None else str(value)


def describe_max(value) -> str:
    return "no max" if value is None else str(value)


def value_markup(value, fmt=describe) -> str:
    """A setting value: cyan when set, dim when left at the default."""
    return f"[muted]{fmt(value)}[/]" if value is None else f"[num]{escape(fmt(value))}[/]"


def describe_shared(targets: list, field: str, fmt=describe) -> str:
    value = uniform(targets, field)
    if not targets:
        return "-"
    return "per model" if value is MIXED else f"{fmt(value)} for all"


def uses_claude(cfg: config.Config, targets: list) -> bool:
    return any((cfg.provider(t["provider"]) or {}).get("kind") == "anthropic" for t in targets)


def show_settings_table(cfg: config.Config, rc: dict, counts: Optional[dict] = None) -> None:
    """MODEL | CONNECTION | TEMPERATURE | MAX OUTPUT TOKENS, plus results if `counts`."""
    targets = rc["targets"]
    if not targets:
        console.print(ui.warn("No models chosen."))
        return
    statuses = connection_status(cfg, [t["provider"] for t in targets])
    cols = ["Model", "Connection", ("Temperature", "right"), ("Max output tokens", "right")]
    if counts is not None:
        cols += [("Completed", "right"), ("Failed", "right"), ("Not run", "right")]
    table = ui.table(*cols)
    n = len(rc.get("prompts", []))
    for t in targets:
        row = [
            target_markup(cfg, t), status_markup(statuses[t["provider"]]),
            value_markup(t.get("temperature")), value_markup(t.get("max_tokens"), describe_max),
        ]
        if counts is not None:
            c = counts.get(t["id"], {"ok": 0, "error": 0})
            row += [
                ui.tally_pct(c["ok"], n, "ok"), ui.tally_pct(c["error"], n, "err"),
                ui.tally_pct(n - c["ok"] - c["error"], n, "warn"),
            ]
        table.add_row(*row)
    console.print(table)


def show_run_preview(cfg: config.Config, rc: dict, counts: Optional[dict] = None) -> None:
    prompts = rc.get("prompts", [])
    source = rc.get("source_file")
    ui.heading("Prompts")
    ui.kv([
        ("File", ui.path(source) if source else "[muted]none chosen[/]"),
        ("Count", qty(len(prompts), "prompt")),
    ])
    if prompts:
        table = ui.table(("#", "right"), "Prompt")
        table.columns[1].overflow, table.columns[1].no_wrap = "ellipsis", True
        for i, prompt in enumerate(prompts[:PREVIEW_ROWS]):
            table.add_row(f"[muted]{i + 1}[/]", escape(one_line(prompt, 200)))
        console.print(table)
        if len(prompts) > PREVIEW_ROWS:
            console.print(f"[muted]…and {plural(len(prompts) - PREVIEW_ROWS, 'more prompt')}[/]")
    console.print()

    system = (rc.get("system_prompt") or "").strip()
    source = rc.get("system_prompt_file")
    ui.heading("System prompt")
    ui.kv([
        ("From", ui.path(source)) if source else None,
        ("Text", f"[name]{escape(one_line(system, 200))}[/]" if system else "[muted]blank[/]"),
    ])
    console.print()

    ui.heading("Models")
    show_settings_table(cfg, rc, counts)
    console.print()


def ask_per_model(crumbs: tuple, targets: list, field: str, label: str, validate, cast) -> object:
    """Ask for `field` one model at a time. Esc steps back a model. Returns True or BACK."""
    values = [t.get(field) for t in targets]
    j = 0
    while j < len(targets):
        screen(*crumbs)
        if j:
            ui.kv([(t["id"], value_markup(v)) for t, v in zip(targets[:j], values[:j])])
            console.print()
        ans = ask(questionary.text(
            f"{label} for {targets[j]['id']} ({j + 1} of {len(targets)}):",
            default="" if values[j] is None else str(values[j]),
            validate=validate,
        ))
        if ans in (None, BACK):
            if j == 0:
                return BACK
            j -= 1
            continue
        values[j] = optional_number(ans, cast)
        j += 1
    for t, v in zip(targets, values):
        t[field] = v
    return True


def step_crumbs(project: Project, name: str) -> tuple:
    if _wizard_step:
        return project.name, "New run", f"Step {_wizard_step[0]} of {_wizard_step[1]} · {name}"
    return project.name, "Modify settings", name


# Each step edits the run config in place and returns True, or BACK if the user backed out.

def step_file(cfg: config.Config, project: Project, rc: dict) -> object:
    current = rc.get("source_file") or ""

    def file_ok(raw: str):
        if clean_path(raw).is_file():
            return True
        # The file has moved since the run it came from; keep that run's copy of the prompts.
        if raw.strip() == current and rc.get("prompts"):
            return True
        return "File not found"

    while True:
        screen(*step_crumbs(project, "Prompts file"))
        console.print("[hint]Prompts are read from the first column; the first row is a header.[/]\n")
        ans = ask(questionary.path("Select file (CSV):", default=current, validate=file_ok))
        if ans in (None, BACK):
            return BACK
        path = clean_path(ans)
        if not path.is_file():
            return True
        try:
            prompts = read_prompts(path)
        except (UnicodeDecodeError, OSError, csv.Error) as e:
            say(ui.fail(f"Couldn't read the file: [err]{escape(str(e))}[/]"))
            continue
        if not prompts:
            say(ui.fail("No prompts found in the first column."))
            continue
        rc["source_file"], rc["prompts"] = str(path.resolve()), prompts
        say(ui.ok(f"Loaded {qty(len(prompts), 'prompt')} from {ui.path(path, path.name)}."))
        return True


def step_models(cfg: config.Config, project: Project, rc: dict) -> object:
    targets = rc["targets"]
    checked = {t["id"] for t in targets}
    while True:
        screen(*step_crumbs(project, "Models"))
        options = {}
        for p in cfg.providers:
            for m in p["models"]:
                tid = target_id(p["id"], m)
                options[tid] = {"id": tid, "provider": p["id"], "model": m}
        # Models used before that are no longer in the provider's list stay selectable.
        for t in targets:
            options.setdefault(t["id"], {"id": t["id"], "provider": t["provider"], "model": t["model"]})
        statuses = connection_status(cfg, [t["provider"] for t in options.values()])

        groups: dict = {}
        for t in options.values():
            groups.setdefault(t["provider"], []).append(t)
        if groups:
            ui.kv([(provider_name(cfg, pid), status_markup(statuses[pid])) for pid in groups])
            console.print()
        choices = []
        for pid, group in groups.items():
            status = statuses[pid]
            disabled = status if status in ("provider removed", "no API key") else None
            choices.append(Separator(f"── {provider_name(cfg, pid)} ──"))
            for t in group:
                title = t["model"]
                if disabled is None and status != "ok":
                    title = [("class:text", t["model"]), ("class:err", f"  {ui.FAIL} {one_line(status, 50)}")]
                choices.append(Choice(
                    title, t, checked=t["id"] in checked and disabled is None, disabled=disabled
                ))
        choices.append(Separator("──"))
        choices.append(Choice("+ Add or configure models / API keys (tick, then Enter)", CONFIGURE))
        picked = checkbox(
            "Models to run:",
            choices,
            validate=lambda sel: True if sel else "Pick at least one model",
        )
        if picked in (None, BACK):
            return BACK
        if CONFIGURE in picked:
            checked = {t["id"] for t in picked if t != CONFIGURE}
            before = set(options)
            providers_menu(cfg)
            # Tick whatever was just added so the user doesn't have to find it again.
            for p in cfg.providers:
                for m in p["models"]:
                    tid = target_id(p["id"], m)
                    if tid not in before:
                        checked.add(tid)
            continue
        old = {t["id"]: t for t in targets}
        # New models inherit a setting when every existing model shares it.
        inherit = {}
        for field in ("temperature", "max_tokens"):
            shared = uniform(targets, field)
            inherit[field] = None if shared is MIXED else shared
        rc["targets"] = [old.get(t["id"]) or dict(t, **inherit) for t in picked]
        return True


def step_temperature(cfg: config.Config, project: Project, rc: dict) -> object:
    targets = rc["targets"]
    crumbs = step_crumbs(project, "Temperature")
    while True:
        shared = uniform(targets, "temperature")
        current = "default" if shared is None else "each" if shared is MIXED else "same"
        screen(*crumbs)
        ui.kv([("Current", f"[num]{escape(describe_shared(targets, 'temperature'))}[/]")])
        console.print()
        how = menu("Set temperature:", [
            Choice("Default for each model", "default"),
            Choice("Same for all models", "same"),
            Choice("Different for each model", "each"),
        ], default=current)
        if how in (None, BACK):
            return BACK
        if how == "default":
            for t in targets:
                t["temperature"] = None
            return True
        if how == "same":
            ans = ask(questionary.text(
                "Temperature for all models (e.g. 0.7):",
                default="" if shared is None or shared is MIXED else str(shared),
                validate=is_number(float, required=True),
            ))
            if ans in (None, BACK):
                continue
            for t in targets:
                t["temperature"] = float(ans)
            return True
        if ask_per_model(crumbs, targets, "temperature", "Temperature (blank = default)",
                         is_number(float), float) is True:
            return True


def step_system(cfg: config.Config, project: Project, rc: dict) -> object:
    crumbs = step_crumbs(project, "System prompt")
    while True:
        text, source = rc.get("system_prompt"), rc.get("system_prompt_file")
        current = "file" if source else "type" if text else "blank"
        screen(*crumbs)
        ui.kv([
            ("From", ui.path(source)) if source else None,
            ("Current", f"[name]{escape(one_line(text, 100))}[/]" if text else "[muted]blank[/]"),
        ])
        console.print()
        how = menu("System prompt:", [
            Choice("Run blank", "blank"),
            Choice("Text file (.txt)", "file"),
            Choice("Type here", "type"),
        ], default=current)
        if how in (None, BACK):
            return BACK
        if how == "blank":
            rc["system_prompt"] = rc["system_prompt_file"] = None
            return True
        if how == "file":
            ans = ask(questionary.path(
                "System prompt file:",
                default=source or "",
                validate=lambda raw: clean_path(raw).is_file() or "File not found",
            ))
            if ans in (None, BACK):
                continue
            path = clean_path(ans).resolve()
            try:
                loaded = path.read_text(encoding="utf-8-sig").strip()
            except (UnicodeDecodeError, OSError) as e:
                say(ui.fail(f"Couldn't read the file: [err]{escape(str(e))}[/]"))
                continue
            if not loaded:
                say(ui.fail("That file is empty."))
                continue
            rc["system_prompt"], rc["system_prompt_file"] = loaded, str(path)
            return True
        ans = ask(questionary.text("System prompt:", default=text or ""))
        if ans in (None, BACK):
            continue
        rc["system_prompt"], rc["system_prompt_file"] = ans.strip() or None, None
        return True


def step_max_tokens(cfg: config.Config, project: Project, rc: dict) -> object:
    targets = rc["targets"]
    crumbs = step_crumbs(project, "Max output tokens")
    while True:
        shared = uniform(targets, "max_tokens")
        current = (
            "recommended" if shared == RECOMMENDED_MAX_TOKENS else "none" if shared is None else "custom"
        )
        screen(*crumbs)
        ui.kv([("Current", f"[num]{escape(describe_shared(targets, 'max_tokens', describe_max))}[/]")])
        console.print()
        if uses_claude(cfg, targets):
            console.print(
                f"[hint]Claude always needs a limit, so it uses {ANTHROPIC_DEFAULT_MAX_TOKENS} "
                "under 'No max'.[/]\n"
            )
        how = menu("Max output tokens:", [
            Choice(f"{RECOMMENDED_MAX_TOKENS} (recommended default)", "recommended"),
            Choice("No max", "none"),
            Choice("Custom number", "custom"),
        ], default=current)
        if how in (None, BACK):
            return BACK
        if how in ("recommended", "none"):
            for t in targets:
                t["max_tokens"] = RECOMMENDED_MAX_TOKENS if how == "recommended" else None
            return True
        while True:
            screen(*crumbs)
            spread = menu("Custom max output tokens:", [
                Choice("Same for all models", "same"),
                Choice("Different for each model", "each"),
            ], default="each" if shared is MIXED else "same")
            if spread in (None, BACK):
                break
            if spread == "same":
                ans = ask(questionary.text(
                    "Max output tokens for all models:",
                    default="" if shared is None or shared is MIXED else str(shared),
                    validate=positive_int,
                ))
                if ans in (None, BACK):
                    continue
                for t in targets:
                    t["max_tokens"] = int(ans)
                return True
            if ask_per_model(crumbs, targets, "max_tokens", "Max output tokens",
                             positive_int, int) is True:
                return True


WIZARD = [step_file, step_models, step_temperature, step_system, step_max_tokens]


def run_wizard(cfg: config.Config, project: Project, rc: dict) -> bool:
    """Walk every step; Esc goes back one. False if backed out of the first step."""
    global _wizard_step
    i = 0
    try:
        while i < len(WIZARD):
            _wizard_step = (i + 1, len(WIZARD))
            if WIZARD[i](cfg, project, rc) == BACK:
                if i == 0:
                    return False
                i -= 1
            else:
                i += 1
    finally:
        _wizard_step = None
    return True


def modify_settings(cfg: config.Config, project: Project, rc: dict) -> None:
    while True:
        screen(project.name, "Modify settings")
        targets = rc["targets"]
        system = rc.get("system_prompt")
        action = menu("Change which setting?", [
            Choice(labeled("File", Path(rc["source_file"]).name if rc.get("source_file") else "-"),
                   step_file),
            Choice(labeled("Models", str(len(targets))), step_models),
            Choice(labeled("Temperature", describe_shared(targets, "temperature")), step_temperature),
            Choice(labeled("System prompt", one_line(system, 40) if system else "blank",
                           "b" if system else "text"), step_system),
            Choice(labeled("Max output tokens", describe_shared(targets, "max_tokens", describe_max)),
                   step_max_tokens),
            back("Back to run preview"),
        ])
        if action in (None, BACK):
            return
        action(cfg, project, rc)


# --- runs ------------------------------------------------------------------

def new_run(cfg: config.Config, project: Project, base: Optional[Run]) -> None:
    """Set up a run with the wizard (prefilled from `base`), then preview it."""
    rc = copy.deepcopy(base.config) if base else empty_run_config()
    if run_wizard(cfg, project, rc):
        preview_run(cfg, project, rc)


def preview_run(cfg: config.Config, project: Project, rc: dict) -> None:
    while True:
        screen(project.name, "Run preview")
        show_run_preview(cfg, rc)
        ready = bool(rc.get("prompts")) and bool(rc["targets"])
        if ready:
            requests = len(rc["prompts"]) * len(rc["targets"])
            console.print(
                f"Ready to send {qty(requests, 'request')} "
                f"[muted]({plural(len(rc['prompts']), 'prompt')} × {plural(len(rc['targets']), 'model')})[/]\n"
            )
        action = menu("Run with these settings?", [
            Choice("Begin run", "begin", disabled=None if ready else "choose a file and models first"),
            Choice("Modify settings", "modify"),
            back("Back to main menu", "main"),
        ])
        if action in (None, BACK):
            return
        if action == "main":
            raise ToMainMenu
        if action == "modify":
            modify_settings(cfg, project, rc)
        else:
            execute(cfg, project, project.create_run(rc), include_errors=False)
            return


def resume_preview(cfg: config.Config, project: Project, run: Run) -> None:
    """Preview of an unfinished run. Its settings are fixed; changing them means a new run."""
    while True:
        screen(project.name, f"Resume run #{run.id}")
        show_run_preview(cfg, run.config, counts=project.counts(run))
        ok, err, total = project.summary(run)
        left = total - ok - err
        line = f"Resuming sends [warn.bold]{left}[/] not-run {'request' if left == 1 else 'requests'}"
        if err:
            line += f" and retries [err.bold]{err}[/] failed {'one' if err == 1 else 'ones'}"
        console.print(line + ".")
        console.print("[hint]Settings can't change part-way through a run; set up a new run instead.[/]\n")
        action = menu("Resume with these settings?", [
            Choice("Resume run", "resume"),
            Choice("Set up a new run instead", "new"),
            back("Back to main menu", "main"),
        ])
        if action in (None, BACK):
            return
        if action == "main":
            raise ToMainMenu
        if action == "new":
            new_run(cfg, project, run)
        else:
            execute(cfg, project, run, include_errors=True)
        return


def execute(cfg: config.Config, project: Project, run: Run, include_errors: bool) -> None:
    screen(project.name, f"Run #{run.id}", hint="[key] Ctrl+C [/] [hint]stop[/]")
    console.print(ui.info("Press [key] Ctrl+C [/] at any point to stop the run; finished responses are kept.\n"))
    outcome = run_project(project, run, cfg, include_errors=include_errors)
    run_finished(cfg, project, run, outcome)


def show_results_table(cfg: config.Config, project: Project, run: Run) -> None:
    n = len(run.prompts)
    table = ui.table("Model", ("Completed", "right"), ("Failed", "right"), ("Not run", "right"))
    counts = project.counts(run)
    for t in run.targets:
        c = counts[t["id"]]
        table.add_row(
            target_markup(cfg, t), ui.tally_pct(c["ok"], n, "ok"), ui.tally_pct(c["error"], n, "err"),
            ui.tally_pct(n - c["ok"] - c["error"], n, "warn"),
        )
    if len(run.targets) > 1:
        ok, err, total = project.summary(run)
        table.add_section()
        table.add_row(
            "[heading]All models[/]", ui.tally_pct(ok, total, "ok"), ui.tally_pct(err, total, "err"),
            ui.tally_pct(total - ok - err, total, "warn"),
        )
    console.print(table)


def run_finished(cfg: config.Config, project: Project, run: Run, outcome: dict) -> None:
    targets = {t["id"]: t for t in run.targets}
    while True:
        csv_path = None
        if outcome["status"] != "blocked":
            try:
                csv_path, _ = export(project, run)
            except OSError as e:
                say(ui.fail(f"Couldn't save results: [err]{escape(str(e))}[/]"))
        headline = {
            "complete": f"[ok.bold]{ui.OK} Run complete[/]",
            "interrupted": "[warn.bold]■ Run interrupted[/]",
            "stopped": f"[err.bold]{ui.FAIL} Run stopped[/]",
            "blocked": f"[err.bold]{ui.FAIL} Run couldn't start[/]",
        }[outcome["status"]]
        screen(project.name, f"Run #{run.id}", "Results")
        console.print(headline + (f"  {escape(outcome['message'])}" if outcome["message"] else ""))
        if outcome.get("problems"):
            console.print(ui.bullets(outcome["problems"], "warn"))
        ok, err, total = project.summary(run)
        console.print(ui.done_failed_left(ok, err, total) + "\n")
        show_results_table(cfg, project, run)
        if outcome["last_error"]:
            console.print()
            ui.heading("Last error per model")
            for tid, message in outcome["last_error"].items():
                who = target_markup(cfg, targets[tid]) if tid in targets else f"[name]{escape(tid)}[/]"
                console.print(f"[muted]{ui.BULLET}[/] {who}: [err]{escape(one_line(message, 200))}[/]")
        console.print()
        if csv_path:
            ui.kv([("Results", ui.path(csv_path)), ("Log", ui.path(outcome['log']))])
        if outcome["status"] in ("interrupted", "stopped", "blocked"):
            console.print("[hint]Resume it from the project screen or Run history.[/]")
        console.print()
        choices = []
        if csv_path:
            choices.append(Choice("Open results in external app", "open"))
        if err and outcome["status"] == "complete":
            choices.append(Choice(f"Retry {err} failed {'request' if err == 1 else 'requests'}", "retry"))
        choices += [
            Choice("Set up another run", "another"),
            back("Back to project"),
            Choice("Quit", "quit"),
        ]
        action = menu("What's next?", choices)
        if action in (None, BACK):
            raise ToProjectMenu
        if action == "open":
            open_external(csv_path)
        elif action == "retry":
            screen(project.name, f"Run #{run.id}", hint="[key] Ctrl+C [/] [hint]stop[/]")
            console.print(ui.info("Retrying failed requests. Press [key] Ctrl+C [/] to stop.\n"))
            outcome = run_project(project, run, cfg, include_errors=True)
        elif action == "another":
            new_run(cfg, project, run)
            raise ToProjectMenu
        else:
            raise QuitApp


def run_title(run: Run, ok: int, err: int, total: int, widths: tuple) -> list:
    """'Run #3  14:22 19/10/26  ✓ 40 completed  ✗ 3 failed  ○ 0 not run  93%' as menu tokens,
    padded so a list of runs lines up."""
    w_id, w_count = widths
    left = total - ok - err

    def part(n, glyph, word, style):
        return (f"class:{style}" if n else "class:muted", f"{glyph} {n:>{w_count}} {word}")

    gap = ("class:muted", "  ")
    return [
        ("class:b", f"Run #{run.id:<{w_id}}"), gap,
        ("class:text", fmt_time(run.created_at)), gap,
        part(ok, ui.OK, "completed", "ok"), gap,
        part(err, ui.FAIL, "failed", "err"), gap,
        part(left, "○", "not run", "warn"), gap,
        ("class:b" if ok else "class:muted", f"{ui.pct(ok, total):>4} done"),
    ]


def run_history(cfg: config.Config, project: Project) -> None:
    while True:
        screen(project.name, "Run history")
        runs = project.runs()
        if not runs:
            say(ui.info("No runs yet."))
            return
        rows = [(r,) + tuple(project.summary(r)) for r in reversed(runs)]
        widths = (
            max(len(str(r.id)) for r in runs),
            max(len(str(n)) for row in rows for n in row[1:]),
        )
        choices = [Choice(run_title(r, ok, err, total, widths), r) for r, ok, err, total in rows]
        choices = [back()] + choices if len(choices) >= LONG_LIST else choices + [back()]
        pick = menu(f"Select a run ({plural(len(runs), 'run')}, newest first):", choices)
        if pick in (None, BACK):
            return
        historical_run(cfg, project, pick)


def historical_run(cfg: config.Config, project: Project, run: Run) -> None:
    while True:
        screen(project.name, "Run history", f"Run #{run.id}")
        ok, err, total = project.summary(run)
        ui.kv([
            ("Date run", time_markup(run.created_at)),
            ("Results", ui.done_failed_left(ok, err, total)),
        ])
        console.print()
        show_run_preview(cfg, run.config, counts=project.counts(run))
        choices = [Choice("Start a new run with these settings", "new")]
        if ok < total:
            choices.append(Choice([
                ("class:text", "Resume run  "),
                ("class:warn", f"{total - ok - err} not run"), ("class:text", ", "),
                ("class:err" if err else "class:muted", f"{err} failed"),
            ], "resume"))
        choices += [Choice("Export results as .csv", "export"), back()]
        action = menu("Options:", choices)
        if action in (None, BACK):
            return
        if action == "new":
            preview_run(cfg, project, copy.deepcopy(run.config))
        elif action == "resume":
            execute(cfg, project, run, include_errors=True)
        else:
            try:
                csv_path, _ = export(project, run)
            except OSError as e:
                say(ui.fail(f"Couldn't export: [err]{escape(str(e))}[/]"))
                continue
            say(ui.ok(f"Saved {ui.path(csv_path)}"))
            if ask(questionary.confirm("Open it in an external app?", default=False)) is True:
                open_external(csv_path)


# --- projects --------------------------------------------------------------

def project_screen(cfg: config.Config, project: Project) -> None:
    """The hub for one project. Returns when the user goes back to the main menu."""
    previously_opened = project.mark_opened()
    while True:
        screen("Projects", project.name)
        last = project.last_run()
        not_run = err = 0
        rows = [
            ("Last opened", time_markup(previously_opened)),
            ("Last run", (f"[name]#{last.id}[/]  " + time_markup(last.created_at)) if last else time_markup(None)),
        ]
        if last:
            ok, err, total = project.summary(last)
            not_run = total - ok - err
            rows += [
                ("Size", f"{qty(len(last.prompts), 'prompt')} × {qty(len(last.targets), 'model')}"),
                ("Status", ui.done_failed_left(ok, err, total)),
            ]
        ui.kv(rows)
        console.print()

        choices = []
        if not_run:
            choices.append(Choice([
                ("class:text", "Resume previous run  "), ("class:warn", f"{not_run} not run"),
            ], "resume"))
        choices += [
            Choice("View run history", "history"),
            Choice("Set up a new run", "new"),
            Choice("Rename project", "rename"),
            Choice("Delete project", "delete"),
            back("Back to main menu"),
        ]
        action = menu("Options:", choices)
        if action in (None, BACK):
            return
        try:
            if action == "resume":
                resume_preview(cfg, project, last)
            elif action == "history":
                run_history(cfg, project)
            elif action == "new":
                new_run(cfg, project, last)
        except ToProjectMenu:
            continue
        if action == "rename":
            name = ask(questionary.text(
                "New project name:", default=project.name,
                validate=lambda s: bool(s.strip()) or "Name required",
            ))
            if name in (None, BACK) or name.strip() == project.name:
                continue
            old_path = project.path.resolve()
            project.rename(name.strip())
            if str(old_path) in cfg.recent_projects:
                cfg.forget_project(old_path)
                cfg.remember_project(project.path)
            say(ui.ok(f"Renamed to [name]{escape(project.name)}[/]."))
        elif action == "delete":
            if ask(questionary.confirm(
                f"Permanently delete '{project.name}' and all its runs?", default=False
            )) is True:
                cfg.forget_project(project.path)
                project.delete()
                say(ui.ok(f"Deleted [name]{escape(project.name)}[/]."))
                return


def with_project(cfg: config.Config, path: Path) -> None:
    project = Project(path)
    try:
        project_screen(cfg, project)
    finally:
        project.close()


def new_project(cfg: config.Config) -> None:
    root = cfg.projects_path()
    existing = {p.name.lower() for p in root.iterdir()}

    def name_ok(raw: str):
        if not raw.strip():
            return "Name required"
        if config.slugify(raw).lower() in existing:
            return "A project with that name already exists"
        return True

    screen("New project")
    ui.kv([("Saved in", ui.path(root))])
    console.print()
    name = ask(questionary.text("Project name:", validate=name_ok))
    if name in (None, BACK):
        return
    project = Project.create(root, name.strip())
    project.close()
    say(ui.ok(f"Created {ui.path(project.path)}."))
    with_project(cfg, project.path)


def custom_location(cfg: config.Config) -> Optional[Path]:
    def folder(raw: str) -> Path:
        path = clean_path(raw)
        return path.parent if path.name == "project.json" else path

    screen("Open project", "Custom location")
    console.print(
        "[hint]Type the path to a project folder. Start with ~ for your home folder; "
        "Tab completes.[/]\n"
    )
    ans = ask(questionary.path(
        "Project location:",
        validate=lambda raw: is_project_dir(folder(raw)) or "No project there (looking for project.json)",
    ))
    if ans in (None, BACK):
        return None
    path = folder(ans).resolve()
    if path.parent != cfg.projects_path().resolve():
        cfg.remember_project(path)
    return path


def project_title(name: str, opened: Optional[str], last: Optional[Run], width: int) -> list:
    """'name  opened 14:22 19/10/26  last run 09:10 18/10/26' as menu tokens."""
    def when(label, iso):
        return [("class:text", f"  {label} {fmt_time(iso):<14}")]

    return (
        [("class:b", f"{name:<{width}}")]
        + when("opened", opened)
        + when("last run", last.created_at if last else None)
    )


def open_project(cfg: config.Config) -> None:
    while True:
        screen("Open project")
        paths = {p.resolve(): None for p in Project.list_all(cfg.projects_path())}
        for raw in cfg.recent_projects:
            if is_project_dir(Path(raw)):
                paths.setdefault(Path(raw).resolve(), None)
        found, broken = [], []
        for path in paths:
            try:
                proj = Project(path)
            except Exception as e:
                broken.append(Choice(f"{path.name} (unreadable: {e})", None, disabled="unreadable"))
                continue
            found.append((proj.last_opened or proj.created_at, proj.name, proj.last_opened, proj.last_run(), path))
            proj.close()
        found.sort(key=lambda e: e[0], reverse=True)
        width = max((len(e[1]) for e in found), default=0)
        entries = [Choice(project_title(name, opened, last, width), path) for _, name, opened, last, path in found]
        if not entries and not broken:
            console.print(ui.info("No projects yet.\n"))
        choices = entries + broken + [
            Choice("Other project (custom location)…", "custom"),
            back("Back to main menu"),
        ]
        pick = menu("Select a recent project:", choices)
        if pick in (None, BACK):
            return
        if pick == "custom":
            pick = custom_location(cfg)
            if pick is None:
                continue
        with_project(cfg, pick)
        return


# --- settings --------------------------------------------------------------

def settings_menu(cfg: config.Config) -> None:
    while True:
        screen("Settings")
        ui.kv([
            ("Config folder", ui.path(config.config_dir())),
            ("Projects folder", ui.path(cfg.projects_path())),
        ])
        console.print()
        action = menu("Change which setting?", [
            Choice(labeled("Projects folder", cfg.projects_dir), "folder"),
            Choice(labeled(
                "Verbose logging", "on" if cfg.verbose else "off", "ok" if cfg.verbose else "b",
                note="(on = show every prompt sent and response received during a run)",
            ), "verbose"),
            Choice("Delete all saved API keys", "delkeys"),
            back(),
        ])
        if action in (None, BACK):
            return
        if action == "verbose":
            cfg.verbose = not cfg.verbose
            cfg.save()
        elif action == "delkeys":
            if ask(questionary.confirm(
                "Delete the saved API key for every provider? (Environment variables are not affected.)",
                default=False,
            )) is True:
                config.delete_all_keys(cfg.providers)
                forget_status()
                say(ui.ok("Deleted all saved API keys."))
        else:
            raw = ask(questionary.path("Projects folder:", default=cfg.projects_dir, only_directories=True))
            if raw not in (None, BACK) and raw.strip():
                cfg.projects_dir = str(clean_path(raw))
                cfg.save()
                say(ui.ok(f"Projects will be stored in {ui.path(cfg.projects_path())}"))


# --- entry point -----------------------------------------------------------

def main() -> None:
    cfg = config.Config()
    try:
        while True:
            screen()
            ui.kv([("Projects", ui.path(cfg.projects_path()))])
            console.print()
            action = menu("Main menu", [
                Choice("Open project", "open"),
                Choice("New project", "new"),
                Choice("Providers & API keys", "providers"),
                Choice("Settings", "settings"),
                Choice("Quit", "quit"),
            ], footer=[
                ("class:b", "bulk-ai"), ("class:muted", f" v{__version__}  ·  made with love by Jackson Bigler"),
                ("class:muted", "  ·  ", ), ("class:b", "Ctrl+C quits"),
            ])
            if action in (None, "quit"):
                return
            try:
                if action == "open":
                    open_project(cfg)
                elif action == "new":
                    new_project(cfg)
                elif action == "providers":
                    providers_menu(cfg)
                elif action == "settings":
                    settings_menu(cfg)
            except ToMainMenu:
                continue
    except (KeyboardInterrupt, QuitApp):
        console.print("\n[muted]Bye.[/]")


if __name__ == "__main__":
    main()
