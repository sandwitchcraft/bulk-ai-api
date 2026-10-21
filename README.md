# bulk-ai

An interactive terminal tool for sending a CSV of prompts to many LLMs at once (ChatGPT, Claude, Gemini, Grok, and any OpenAI-compatible endpoint) with live progress bars and projects that resume after an interruption.

## Install

You need Python 3.9 or newer. Pick whichever installer you already have:

```sh
pipx install git+https://github.com/sandwitchcraft/bulk-ai-api
# or
pip3 install --user git+https://github.com/sandwitchcraft/bulk-ai-api
# or
uv tool install git+https://github.com/sandwitchcraft/bulk-ai-api
```

Then run `bulk-ai`.

To run it without installing:

```sh
git clone https://github.com/sandwitchcraft/bulk-ai-api && cd bulk-ai-api
pip3 install -r requirements.txt
python3 -m bulk_ai
```

## Quickstart

1. **Providers & API keys**: pick a provider, then use *Set API key* and *Choose models*. Model lists are fetched live from the provider.
   For OpenRouter, Ollama, vLLM, LM Studio and similar, use *Add custom OpenAI-compatible provider* and give it the base URL (e.g. `http://localhost:11434/v1`).
2. **New project**: give it a name and point it at a CSV. Prompts are read from the **first column**, and the **first row is treated as a header and skipped**. The project opens straight into setting up its first run.
3. **Set up a run**: every run has its own settings. The setup screen shows them as a table and lets you start straight away or change:
   - **Models**: tick or untick models; *+ Add more models...* sets up another provider or model without leaving the screen.
   - **Temperature** and **max output tokens**: one value for all models, or a different value per model.
   - **System prompt** and **parallel requests** per provider.

   A new run starts from the previous run's settings, so re-running with one change is: *New run* → change it → *Start run*. Press **Esc** at any prompt to go back.
4. Watch the progress bars. Errors are printed above them as they happen. **Ctrl-C** stops cleanly (press it twice to force quit). Every response is saved the moment it arrives, so nothing finished is lost.
   If a provider can't be reached (connection refused, DNS failure, connect timeout) after 3 quick attempts, or rejects the API key (HTTP 401), the run stops instead of failing every request; fix it and choose *Resume*.
5. **Open / resume project**: resume or retry the latest run, start a new one, open *All runs* to resume, export or delete a specific run, or export every run together.

## Logging

Every run writes `logs/run-<N>.log` in the project folder, with each request sent, completed, retried or failed. By default only errors are shown on screen; turn on **Settings → Verbose run logging** to also show every "sending prompt 3/120 -> openai:gpt-5.5" line above the progress bars.

## Where things are stored

| What | Where |
| --- | --- |
| API keys | OS keychain (macOS Keychain / Windows Credential Manager / Linux Secret Service). Falls back to `keys.json` (mode 600) in the config folder if no keychain is available |
| Key env vars (take priority) | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY` |
| Providers & settings | `~/.config/bulk-ai/config.json` (`%APPDATA%\bulk-ai` on Windows; override with `BULK_AI_CONFIG`) |
| Projects | `~/bulk-ai-projects/<name>/` (changeable in Settings) |

Each project folder contains:

- `project.json`: the project name and a frozen copy of the prompts. Editing the CSV later doesn't affect the project.
- `results.db`: SQLite with each run's settings and one row per (run, prompt, model): the response or error, finish reason, token counts, latency and attempts.
- `logs/run-<N>.log`: the run's log.
- `results_run<N>_wide.csv` / `results_run<N>.jsonl` (one run) or `results_all_runs_wide.csv` / `results_all_runs.jsonl` (all runs): written by *Export*. The CSV has one column per model (per run and model for all runs) and opens cleanly in Excel. The JSONL includes all metadata plus the temperature, max tokens and system prompt used.

Projects made with an earlier version are converted the first time they're opened: their models and settings become run #1, and the old database is kept as `results.v1-backup.db`.

## Notes

- Rate limits (429) and server errors are retried automatically with backoff, up to 5 attempts. Permanent errors such as a bad key or unknown model are recorded and shown in the end-of-run table.
- Claude requires a max-token limit, so it uses 4096 when you leave that blank.
- If finish reason is `length` / `max_tokens` in the JSONL, that response was cut off by the max-token limit.
