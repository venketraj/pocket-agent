"""Rehearse the demo against the real local model: runs every act in demo_script.yaml N times,
checks the result, and prints pass rate and timing.

    python rehearse.py            # 10 runs per act on LOCAL_MODEL
    python rehearse.py -n 3 --model qwen3.5:9b --acts agenda files
    python rehearse.py -n 1 --acts hybrid --cloud   # one real Claude call
"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("DEMO_NOW", "16:40")  # agenda answer must be "17:00, 20 minutes"
os.environ.setdefault("BROWSER_HEADLESS", "true")
if "--cloud" not in sys.argv:  # the API key has little credit: rehearse the offline fallback unless asked
    os.environ["ANTHROPIC_API_KEY"] = ""

import yaml
from rich.console import Console
from rich.table import Table

import pocket_agent as pa


def check_agenda(tools, answer, results):
    assert {"read_agenda", "get_time", "calculate"} <= set(tools), f"tools used: {tools}"
    assert results.get("calculate") == "20", f"calculate returned {results.get('calculate')!r} (audience sees this)"
    assert re.search(r"17[:.]00|5(:00)?\s*[pP]\.?[mM]", answer), "end time 17:00 missing"
    assert re.search(r"\b20\b", answer), "'20 minutes' missing"


def check_files(tools, answer, results):
    ws = pa.WORKSPACE
    invoices = {p.name for p in (ws / "Invoices").glob("*")}
    photos = {p.name for p in (ws / "Photos").glob("*")}
    assert invoices == {"invoice_amazon_oct.txt", "invoice_cloud_gpu.txt"}, f"Invoices/: {invoices}"
    assert photos == {"madurai_temple.png", "IMG_2041.png"}, f"Photos/: {photos}"
    summary = ws / "summary.md"
    assert summary.is_file() and len(summary.read_text(encoding="utf-8").strip()) > 40, "summary.md missing or empty"


def check_browser(tools, answer, results):
    assert "fill_and_submit" in tools, f"tools used: {tools}"
    sub = pa._page().evaluate("window.__submitted || null")
    assert sub, "form was not submitted"
    assert "venket" in sub["name"].lower(), f"name={sub['name']!r}"
    assert sub["session"] == "Pocket-Sized Brains", f"session={sub['session']!r}"
    assert sub["rating"] == "5", f"rating={sub['rating']!r}"
    assert sub["try_next"].strip() and sub["improve"].strip(), "free-text fields empty"


def check_hybrid(tools, answer, results):
    assert "ask_big_brain" in tools, f"tools used: {tools}"
    assert len(answer.strip()) > 40, "no usable answer"


CHECKS = {"agenda": check_agenda, "files": check_files, "browser": check_browser, "hybrid": check_hybrid}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--model", default=pa.LOCAL_MODEL)
    ap.add_argument("--acts", nargs="*", default=list(CHECKS))
    ap.add_argument("-v", action="store_true", help="print every answer")
    ap.add_argument("--cloud", action="store_true", help="really call Claude in the hybrid act (costs credit)")
    a = ap.parse_args()

    steps = [s for s in yaml.safe_load(open(pa.ROOT / "demo_script.yaml", encoding="utf-8"))
             if s.get("check") in a.acts]
    console = Console()
    console.print(f"Rehearsing {len(steps)} acts × {a.n} on [bold]{a.model}[/] "
                  f"(network {'ONLINE' if pa.online('1.1.1.1', 53) else 'OFFLINE'})")
    pa.prewarm(a.model)
    table = Table("act", "pass", "avg s", "max s", "avg tokens", "failures")
    failed = False
    for step in steps:
        passes, secs, toks, errors = 0, [], [], []
        for i in range(a.n):
            pa.reset_workspace()
            pa.close_browser()
            tools, rows, results = [], [], {}

            def emit(kind, **d):
                if kind == "tool":
                    tools.append(d["name"])
                elif kind == "tool_result":
                    results[d["name"]] = d["text"]  # last result per tool
                elif kind == "llm":
                    rows.append(d)

            agent = pa.Agent(model=a.model, emit=emit)
            t0 = time.time()
            try:
                answer = agent.ask(step["ask"])
                CHECKS[step["check"]](tools, answer, results)
                passes += 1
            except Exception as e:
                answer = locals().get("answer", "")
                errors.append(f"run {i + 1}: {e}")
            secs.append(time.time() - t0)
            toks.append(sum(r["tokens_in"] + r["tokens_out"] for r in rows))
            if a.v:
                console.print(f"[dim]{step['check']} #{i + 1} {secs[-1]:.1f}s tools={tools}[/]\n  {answer.strip()[:300]}")
        failed |= passes < a.n
        color = "green" if passes == a.n else "red"
        table.add_row(step["check"], f"[{color}]{passes}/{a.n}[/]", f"{sum(secs) / len(secs):.1f}",
                      f"{max(secs):.1f}", f"{sum(toks) // len(toks):,}", "\n".join(errors[:3]))
    pa.close_browser()
    pa.reset_workspace()
    console.print(table)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
