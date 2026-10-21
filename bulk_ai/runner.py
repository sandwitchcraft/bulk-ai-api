"""Concurrent execution with retries, live progress bars, logging, and graceful Ctrl-C."""
from __future__ import annotations

import asyncio
import os
import random
import signal
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from . import config
from .ui import FAIL, console, plural
from .project import Project, Run
from .providers import APIError, complete, make_client

MAX_ATTEMPTS = 5
# An unreachable endpoint gets a couple of quick retries (to ride out a blip), then stops the run.
CONNECT_ATTEMPTS = 3


class FatalError(Exception):
    """A failure that would hit every request to a provider, so the run stops."""


class RunLog:
    """Writes every event to the run's log file; prints errors (and, if verbose, everything)
    above the progress bars."""

    def __init__(self, path: Path, verbose: bool) -> None:
        self.path = path
        self.verbose = verbose
        self.file = open(path, "a", encoding="utf-8")

    def _write(self, level: str, text: str) -> str:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.file.write(f"{datetime.now().isoformat(timespec='seconds')} {level:<5} {text}\n")
        self.file.flush()
        return stamp

    def record(self, text: str) -> None:
        """Log file only."""
        self._write("INFO", text)

    def info(self, text: str) -> None:
        stamp = self._write("INFO", text)
        if self.verbose:
            console.print(f"[muted]{escape(f'[{stamp}]')}[/] [muted]{escape(text)}[/]")

    def error(self, text: str) -> None:
        stamp = self._write("ERROR", text)
        console.print(f"[muted]{escape(f'[{stamp}]')}[/] [err.bold]{FAIL}[/] [err]{escape(text)}[/]")

    def close(self) -> None:
        self.file.close()


async def _call_with_retry(client, provider, key, model, prompt, settings, log, label):
    """Returns (Result, attempts). Raises APIError after the final attempt, or FatalError."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await complete(client, provider, key, model, prompt, settings), attempt
        except APIError as e:
            if e.kind == "auth":
                raise FatalError(f"{provider['name']} rejected the API key ({e})") from e
            if e.kind == "connect":
                if attempt >= CONNECT_ATTEMPTS:
                    raise FatalError(
                        f"Can't connect to {provider['name']} at {provider['base_url']} ({e})"
                    ) from e
                delay = float(attempt)
            elif not e.retryable or attempt == MAX_ATTEMPTS:
                e.attempts = attempt
                raise
            else:
                delay = e.retry_after or min(60.0, 2.0 ** attempt) * (0.5 + random.random())
            log.info(f"retrying {label} in {delay:.0f}s (attempt {attempt + 1}): {e}")
            await asyncio.sleep(delay)


def run_project(project: Project, run: Run, cfg: config.Config, include_errors: bool = False) -> dict:
    """Run every pending request. Returns {"status", "message", "last_error", "log"} where status
    is "complete", "interrupted" (Ctrl-C), "stopped" (provider unusable) or "blocked" (never started)."""
    outcome = {"status": "complete", "message": "", "last_error": {}, "log": project.log_path(run.id)}
    jobs = project.pending(run, include_errors)
    if not jobs:
        return outcome

    providers, keys = {}, {}
    missing = []
    for t in run.targets:
        pid = t["provider"]
        if pid in providers:
            continue
        p = cfg.provider(pid)
        if p is None:
            missing.append(f"{pid} (provider no longer configured)")
            continue
        key, _ = config.get_key(p)
        if not key and p.get("builtin"):
            missing.append(f"{p['name']} (no API key)")
            continue
        providers[pid], keys[pid] = p, key
    if missing:
        outcome["status"] = "blocked"
        outcome["message"] = "Fix these in 'Providers & API keys' first:"
        outcome["problems"] = missing
        return outcome

    log = RunLog(outcome["log"], cfg.verbose)
    try:
        asyncio.run(_run(project, run, providers, keys, jobs, log, outcome))
    finally:
        log.close()
    return outcome


async def _run(
    project: Project, run: Run, providers: dict, keys: dict, jobs: list, log: RunLog, outcome: dict
) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    presses = [0]
    fatal = []

    def on_sigint(signum, frame):
        presses[0] += 1
        if presses[0] >= 2:
            console.print("\n[err.bold]Force quit.[/] Completed responses are already saved.")
            os._exit(130)
        loop.call_soon_threadsafe(stop.set)

    old_handler = signal.signal(signal.SIGINT, on_sigint)

    n_prompts = len(run.prompts)
    pending_per_target = {}
    for _, t in jobs:
        pending_per_target[t["id"]] = pending_per_target.get(t["id"], 0) + 1

    queues = {pid: deque() for pid in providers}
    for job in jobs:
        queues[job[1]["provider"]].append(job)

    errors = {t["id"]: 0 for t in run.targets}
    oks = [0]
    last_error = outcome["last_error"]

    progress = Progress(
        TextColumn("{task.description}", justify="left"),
        BarColumn(bar_width=None, style="bar.back", complete_style="cyan", finished_style="green"),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[err]{task.fields[errs]}"),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    )
    # Bars count only this pass's requests, so a resumed run starts at 0 rather than at what was
    # already saved; models with nothing left to send get no bar.
    overall = progress.add_task(f"[title]Run #{run.id}[/] [muted]all models[/]", total=len(jobs), errs="")
    bars = {
        t["id"]: progress.add_task(f"[name]{escape(t['id'])}[/]", total=pending_per_target[t["id"]], errs="")
        for t in run.targets
        if t["id"] in pending_per_target
    }

    async def worker(client, pid):
        q = queues[pid]
        while q and not stop.is_set():
            idx, t = q.popleft()
            label = f"prompt {idx + 1}/{n_prompts} -> {t['id']}"
            log.info(f"Sent prompt {idx + 1} to {t['id']}")
            started = time.monotonic()
            try:
                res, attempts = await _call_with_retry(
                    client, providers[pid], keys[pid], t["model"], run.prompts[idx],
                    run.settings_for(t), log, label,
                )
                elapsed = time.monotonic() - started
                project.save_result(
                    run.id, idx, t["id"], "ok",
                    response=res.text,
                    finish_reason=res.finish_reason,
                    input_tokens=res.input_tokens,
                    output_tokens=res.output_tokens,
                    latency_ms=int(elapsed * 1000),
                    attempts=attempts,
                )
                tokens = f", {res.output_tokens} tokens" if res.output_tokens is not None else ""
                log.info(f"Received response {idx + 1} from {t['id']} ({elapsed:.1f}s{tokens})")
                oks[0] += 1
            except asyncio.CancelledError:
                raise
            except FatalError as e:
                # Not saved, so this pair is still pending when the run is resumed.
                if not fatal:
                    fatal.append(str(e))
                    log.error(f"{e}. Stopping the run.")
                stop.set()
                return
            except Exception as e:  # one bad request must not kill the whole run
                message = str(e) if isinstance(e, APIError) else f"{type(e).__name__}: {e}"
                attempts = getattr(e, "attempts", 1)
                project.save_result(
                    run.id, idx, t["id"], "error",
                    error=message,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                )
                log.error(f"failed {label} after {plural(attempts, 'attempt')}: {message}")
                errors[t["id"]] += 1
                last_error[t["id"]] = message
                progress.update(bars[t["id"]], errs=f"{FAIL} {errors[t['id']]} failed")
            progress.advance(bars[t["id"]])
            progress.advance(overall)

    log.record(
        f"run #{run.id}: {len(jobs)} requests to {', '.join(t['id'] for t in run.targets)}"
    )
    interrupted = False
    started = time.monotonic()
    try:
        async with make_client() as client:
            with progress:
                tasks = [
                    asyncio.ensure_future(worker(client, pid))
                    for pid in providers
                    for _ in range(max(1, int(providers[pid].get("concurrency", config.DEFAULT_CONCURRENCY))))
                ]
                everything = asyncio.gather(*tasks)
                stopper = asyncio.ensure_future(stop.wait())
                await asyncio.wait({everything, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    interrupted = True
                    for task in tasks:
                        task.cancel()
                stopper.cancel()
                await asyncio.gather(everything, stopper, return_exceptions=True)
    finally:
        signal.signal(signal.SIGINT, old_handler)

    elapsed = time.monotonic() - started
    # Counts for this pass only, matching the progress bars.
    total = len(jobs)
    ok, err = oks[0], sum(errors.values())
    if fatal:
        outcome["status"], outcome["message"] = "stopped", fatal[0]
        summary = f"Stopped: {fatal[0]}. {ok + err}/{total} saved."
    elif interrupted:
        outcome["status"] = "interrupted"
        summary = f"Interrupted. {ok + err}/{total} saved."
    else:
        summary = f"Finished in {elapsed:.0f}s. {ok}/{total} ok, {err} errors."
    log.record(summary)
