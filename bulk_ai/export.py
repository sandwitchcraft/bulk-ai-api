"""Write a run's results to a wide CSV (one column per model) and a long JSONL."""
from __future__ import annotations

import csv
import json

from .project import Project, Run


def export(project: Project, run: Run) -> tuple:
    """Write results_run<N>_wide.csv and results_run<N>.jsonl. Returns (csv_path, jsonl_path)."""
    prompts = run.prompts
    targets = {t["id"]: t for t in run.targets}
    columns = list(targets)
    grid = {}
    jsonl_path = project.path / f"results_run{run.id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in project.rows([run.id]):
            if row["prompt_idx"] >= len(prompts):
                continue
            cell = row["response"] if row["status"] == "ok" else f"[ERROR] {row['error']}"
            grid[(row["prompt_idx"], row["target_id"])] = cell
            target = targets.get(row["target_id"], {})
            record = dict(
                row,
                prompt=prompts[row["prompt_idx"]],
                temperature=target.get("temperature"),
                max_tokens=target.get("max_tokens"),
                system_prompt=run.config.get("system_prompt"),
            )
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # utf-8-sig so Excel detects the encoding.
    csv_path = project.path / f"results_run{run.id}_wide.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["prompt"] + columns)
        for i, prompt in enumerate(prompts):
            writer.writerow([prompt] + [grid.get((i, tid), "") for tid in columns])
    return csv_path, jsonl_path
