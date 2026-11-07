#!/usr/bin/env python3
"""Serving-path latency breakdown for the shopping agent turn loop.

Times prefetch, each LLM stream, and each tool on live ACME turns. Does not
modify committed catalog, skill, or prompt files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

REPO = Path("/home/comp/cswjyu/orion-yuwenjun/commerce-agents")
for extra in (
    REPO / "commerce-common",
    REPO / "shopping-agent" / "core",
    REPO / "shopping-agent" / "runtime-messages-api",
    REPO / "examples",
):
    sys.path.insert(0, str(extra))

from demo_common import load_demo_env, provider_config_overrides
from demo_common.storefront_fixtures import rank_products
from retail.api.mock_retail import MockRetail
from shopping_agent import (
    SearchFilters,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent.executor import ShoppingToolExecutor

OUT = REPO / "eval-scope" / "results"
SKILLS = REPO / "shopping-agent" / "skills"
LIVE_TIMEOUT_S = 75.0
N_REPS = 2

CASES = [
    {
        "id": "search_tent",
        "label": "Search: tent",
        "text": (
            "I'm taking my partner and our 6-year-old camping next month. "
            "We need a tent, nothing too heavy, ideally under $250."
        ),
    },
    {
        "id": "search_adapter",
        "label": "Search: adapter",
        "text": "I need a universal travel adapter that can charge a laptop.",
    },
    {
        "id": "plan_camp",
        "label": "Plan: camp kit",
        "text": (
            "Two couples, first weekend of car camping, and no gear between us. "
            "Put together what we need and keep the whole list under $600."
        ),
    },
    {
        "id": "returns",
        "label": "Returns",
        "text": "Can I still return the yoga mat I ordered from you a while back? It's unused.",
    },
]

PRESENT_PREFIX = "present_"
SEARCH_TOOLS = {"search_products", "get_product_details"}
SKILL_TOOLS = {"load_skill"}


class TimedCM:
    def __init__(self, inner: Any, spans: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._spans = spans
        self._t0: float | None = None

    async def __aenter__(self) -> Any:
        self._t0 = time.perf_counter()
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            self._spans.append(
                {
                    "kind": "llm",
                    "name": f"round{sum(1 for s in self._spans if s['kind'] == 'llm')}",
                    "t0": self._t0,
                    "t1": time.perf_counter(),
                }
            )


def wrap_client(client: Any, spans: list[dict[str, Any]]) -> Any:
    orig = client.messages.stream

    def stream(*args: Any, **kwargs: Any) -> TimedCM:
        return TimedCM(orig(*args, **kwargs), spans)

    client.messages.stream = stream
    return client


def make_executor_class(spans: list[dict[str, Any]]) -> type[ShoppingToolExecutor]:
    class TimingExecutor(ShoppingToolExecutor):
        async def execute(self, name: str, tool_input: dict[str, Any] | None = None):
            t0 = time.perf_counter()
            try:
                return await super().execute(name, tool_input)
            finally:
                spans.append(
                    {
                        "kind": "tool",
                        "name": name,
                        "t0": t0,
                        "t1": time.perf_counter(),
                    }
                )

    return TimingExecutor


def make_client(timeout: float) -> Any:
    from anthropic import AsyncAnthropic

    kwargs: dict[str, Any] = {"timeout": timeout}
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base:
        kwargs["base_url"] = base
    return AsyncAnthropic(**kwargs)


def overlap_s(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def attribute(
    origin: float,
    end: float,
    prefetch: list[dict[str, Any]],
    llm: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, float]:
    points = {origin, end}
    for span in prefetch + llm + tools:
        points.add(min(end, max(origin, span["t0"])))
        points.add(min(end, max(origin, span["t1"])))
    pts = sorted(points)
    acc = {"prefetch": 0.0, "llm": 0.0, "overlap": 0.0, "tools": 0.0, "other": 0.0}
    for left, right in zip(pts, pts[1:]):
        if right <= left:
            continue
        mid = (left + right) / 2
        in_pre = any(s["t0"] <= mid < s["t1"] for s in prefetch)
        in_llm = any(s["t0"] <= mid < s["t1"] for s in llm)
        in_tool = any(s["t0"] <= mid < s["t1"] for s in tools)
        dt = right - left
        if in_pre:
            acc["prefetch"] += dt
        elif in_llm and in_tool:
            acc["overlap"] += dt
        elif in_llm:
            acc["llm"] += dt
        elif in_tool:
            acc["tools"] += dt
        else:
            acc["other"] += dt
    return acc


def tool_bucket(name: str) -> str:
    if name in SEARCH_TOOLS:
        return "search"
    if name in SKILL_TOOLS:
        return "skill"
    if name.startswith(PRESENT_PREFIX):
        return "present"
    return "other_tool"


def round_stack(
    origin: float, end: float, llm: list[dict[str, Any]]
) -> list[tuple[str, float]]:
    """Prefetch gap + each LLM stream + gaps between/after, summing toward wall."""
    rows: list[tuple[str, float]] = []
    cursor = origin
    ordered = sorted(llm, key=lambda s: s["t0"])
    if ordered:
        pre = max(0.0, ordered[0]["t0"] - cursor)
        rows.append(("prefetch", pre))
        cursor = ordered[0]["t0"]
    else:
        rows.append(("prefetch", max(0.0, end - origin)))
        return rows
    for i, span in enumerate(ordered):
        gap = max(0.0, span["t0"] - cursor)
        if gap:
            rows.append((f"join{i}", gap))
        rows.append((f"llm{i}", max(0.0, span["t1"] - span["t0"])))
        cursor = span["t1"]
    tail = max(0.0, end - cursor)
    if tail:
        rows.append(("tail", tail))
    return rows


async def run_turn(
    agent: Any,
    spans: list[dict[str, Any]],
    text: str,
    sid: str,
) -> dict[str, Any]:
    orig_prefetch = agent._prefetch

    async def timed_prefetch(session: ShoppingSessionContext):
        t0 = time.perf_counter()
        try:
            return await orig_prefetch(session)
        finally:
            spans.append({"kind": "prefetch", "name": "prefetch", "t0": t0, "t1": time.perf_counter()})

    agent._prefetch = timed_prefetch
    session = ShoppingSessionContext(session_id=sid, user_id="demo-user")
    state = ShoppingSessionState()
    messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
    events: list[tuple[float, Any]] = []
    origin = time.perf_counter()
    usage = None
    elapsed_ms = None
    try:
        async for event in agent.stream_turn(messages, session, state):
            events.append((time.perf_counter(), event))
            if event.type == "turn_complete":
                usage = event.data.get("usage")
                elapsed_ms = event.data.get("elapsed_ms")
    finally:
        agent._prefetch = orig_prefetch
    end = time.perf_counter()
    wall = (elapsed_ms / 1000.0) if elapsed_ms is not None else (end - origin)

    def first_event(pred) -> float | None:
        for ts, event in events:
            if pred(event):
                return ts - origin
        return None

    ttft = first_event(lambda e: e.type in {"text_delta", "tool_call"})
    t_search = first_event(
        lambda e: e.type == "tool_result" and e.data.get("tool") in SEARCH_TOOLS
    )
    t_ui = first_event(lambda e: e.type == "ui")
    tool_names = [
        event.data.get("tool")
        for _, event in events
        if event.type == "tool_call"
    ]
    llm = [s for s in spans if s["kind"] == "llm"]
    tools = [s for s in spans if s["kind"] == "tool"]
    prefetch = [s for s in spans if s["kind"] == "prefetch"]
    attr = attribute(origin, origin + wall, prefetch, llm, tools)
    cpu = {"search": 0.0, "skill": 0.0, "present": 0.0, "other_tool": 0.0}
    for span in tools:
        cpu[tool_bucket(span["name"])] += span["t1"] - span["t0"]
    overlapped = 0.0
    for tool in tools:
        overlapped += sum(
            overlap_s(tool["t0"], tool["t1"], call["t0"], call["t1"]) for call in llm
        )
    return {
        "ok": True,
        "wall_s": wall,
        "n_llm": len(llm),
        "tool_names": tool_names,
        "ttft_s": ttft,
        "t_first_search_s": t_search,
        "t_first_ui_s": t_ui,
        "attr": attr,
        "cpu": cpu,
        "tool_cpu_s": sum(cpu.values()),
        "overlapped_tool_s": overlapped,
        "llm_spans_s": [s["t1"] - s["t0"] for s in sorted(llm, key=lambda x: x["t0"])],
        "round_stack": round_stack(origin, origin + wall, llm),
        "usage": usage,
        "n_events": len(events),
    }


def bench_ranker() -> dict[str, float]:
    backend = MockRetail()
    session = ShoppingSessionContext(session_id="bench", user_id="demo-user")
    queries = [
        "family camping tent lightweight",
        "universal travel adapter laptop",
        "camping sleeping bag",
        "yoga mat",
    ]
    samples: list[float] = []
    for query in queries:
        for _ in range(50):
            t0 = time.perf_counter()
            ranked = rank_products(
                backend.products.values(),
                query,
                SearchFilters(),
                8,
                score=backend._score,
                hard_filter=lambda product, filters: True,
                soft_filter=backend._soft_filter,
            )
            del ranked
            samples.append(time.perf_counter() - t0)
    arr = np.array(samples)
    return {
        "n": int(arr.size),
        "mean_ms": float(arr.mean() * 1000),
        "p50_ms": float(np.percentile(arr, 50) * 1000),
        "p95_ms": float(np.percentile(arr, 95) * 1000),
    }


def mean_or_none(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return float(sum(present) / len(present))


def summarize_case(runs: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in runs if r.get("ok")]
    if not ok:
        return {"ok": False, "n": 0, "errors": [r.get("error") for r in runs]}
    n = len(ok)
    keys = ["prefetch", "llm", "overlap", "tools", "other"]
    attr = {k: sum(r["attr"][k] for r in ok) / n for k in keys}
    cpu_keys = ["search", "skill", "present", "other_tool"]
    cpu = {k: sum(r["cpu"][k] for r in ok) / n for k in cpu_keys}
    n_llm = max(r["n_llm"] for r in ok)
    llm_by_round: list[float] = []
    for i in range(n_llm):
        llm_by_round.append(
            sum(r["llm_spans_s"][i] for r in ok if i < len(r["llm_spans_s"]))
            / max(1, sum(1 for r in ok if i < len(r["llm_spans_s"])))
        )
    stack_keys: list[str] = []
    for r in ok:
        for name, _ in r["round_stack"]:
            if name not in stack_keys:
                stack_keys.append(name)
    stack = []
    for name in stack_keys:
        stack.append(
            {
                "name": name,
                "s": sum(dict(r["round_stack"]).get(name, 0.0) for r in ok) / n,
            }
        )
    return {
        "ok": True,
        "n": n,
        "wall_s": sum(r["wall_s"] for r in ok) / n,
        "n_llm": sum(r["n_llm"] for r in ok) / n,
        "ttft_s": mean_or_none([r["ttft_s"] for r in ok]),
        "t_first_search_s": mean_or_none([r["t_first_search_s"] for r in ok]),
        "t_first_ui_s": mean_or_none([r["t_first_ui_s"] for r in ok]),
        "attr": attr,
        "cpu": cpu,
        "tool_cpu_s": sum(r["tool_cpu_s"] for r in ok) / n,
        "overlapped_tool_s": sum(r["overlapped_tool_s"] for r in ok) / n,
        "llm_spans_s": llm_by_round,
        "round_stack": stack,
        "tool_names": ok[-1]["tool_names"],
        "usage": ok[-1].get("usage"),
        "runs": ok,
    }


def iter_runs(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in summary["cases"]:
        runs = [r for r in (case.get("runs") or []) if r.get("ok")]
        if not runs and case.get("ok"):
            runs = [case]
        for i, run in enumerate(runs, 1):
            stack = run.get("round_stack") or []
            if stack and isinstance(stack[0], (list, tuple)):
                stack = [{"name": name, "s": value} for name, value in stack]
            rows.append(
                {
                    "label": f"{case['label']}  {i}",
                    "case_id": case.get("id"),
                    "rep": i,
                    "wall_s": run["wall_s"],
                    "n_llm": run["n_llm"],
                    "ttft_s": run.get("ttft_s"),
                    "t_first_ui_s": run.get("t_first_ui_s"),
                    "round_stack": stack,
                    "cpu": run.get("cpu") or {},
                    "attr": run.get("attr") or {},
                    "tool_names": run.get("tool_names") or [],
                }
            )
    return rows


def _stack_parts(stack: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    llm_i = 0
    parts: list[tuple[str, str, float]] = []
    for item in stack:
        name, value = item["name"], float(item["s"])
        if value <= 0:
            continue
        if name.startswith("llm"):
            parts.append((f"llm{llm_i}", f"LLM round {llm_i}", value))
            llm_i += 1
        else:
            parts.append(("gap", "Setup / round gap", value))
    return parts


def draw(summary: dict[str, Any], path: Path) -> None:
    rows = iter_runs(summary)
    if not rows:
        raise SystemExit("no successful turns to plot")

    llm_palette = ["#1f4e79", "#2f6f9f", "#4f8fbf", "#7ab0d4", "#a3cbe3"]
    gap_color = "#c4b08a"
    tool_colors = {
        "search": "#2f5d8a",
        "other_tool": "#6b4c9a",
        "present": "#2a7a62",
        "skill": "#b4532a",
    }
    n = len(rows)
    fig, axes = plt.subplots(
        2,
        1,
        figsize=(11.2, 7.4),
        gridspec_kw={"height_ratios": [1.35, 0.85]},
        constrained_layout=True,
    )
    ax = axes[0]
    y = np.arange(n)
    bar_h = 0.58
    seen: set[str] = set()
    xmax = 0.0
    for i, row in enumerate(rows):
        left = 0.0
        for key, label, value in _stack_parts(row["round_stack"]):
            if key.startswith("llm"):
                idx = int(key.replace("llm", ""))
                color = llm_palette[idx % len(llm_palette)]
            else:
                color = gap_color
            show = label if label not in seen else None
            seen.add(label)
            ax.barh(i, value, left=left, height=bar_h, color=color, label=show, zorder=2)
            if key.startswith("llm") and value >= 0.55:
                ax.text(
                    left + value / 2,
                    i,
                    f"{value:.2f}s",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=7,
                    zorder=3,
                )
            left += value
        xmax = max(xmax, row["wall_s"], left)
        ax.text(
            row["wall_s"] + 0.12,
            i,
            f"{row['wall_s']:.2f}s  ·  {int(row['n_llm'])} LLM",
            va="center",
            fontsize=8,
        )
        if row["ttft_s"] is not None:
            ax.plot(row["ttft_s"], i, "o", color="#111", markersize=4.2, zorder=4)
        if row["t_first_ui_s"] is not None:
            ax.plot(row["t_first_ui_s"], i, "D", color="#b03a2e", markersize=4.2, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels([r["label"] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("Wall time (s)")
    ax.set_title("Serving latency by model round")
    ax.set_xlim(0, xmax * 1.18)
    from matplotlib.lines import Line2D

    handles, labels = ax.get_legend_handles_labels()
    handles.extend(
        [
            Line2D([0], [0], marker="o", color="none", markerfacecolor="#111", markersize=5, label="TTFT"),
            Line2D(
                [0],
                [0],
                marker="D",
                color="none",
                markerfacecolor="#b03a2e",
                markersize=5,
                label="First UI",
            ),
        ]
    )
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=8, ncol=3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax2 = axes[1]
    search = np.array([row["cpu"].get("search", 0.0) * 1000 for row in rows])
    other_tool = np.array([row["cpu"].get("other_tool", 0.0) * 1000 for row in rows])
    present = np.array([row["cpu"].get("present", 0.0) * 1000 for row in rows])
    skill = np.array([row["cpu"].get("skill", 0.0) * 1000 for row in rows])
    left = np.zeros(n)
    for name, values, color in (
        ("search_products", search, tool_colors["search"]),
        ("orders / policies", other_tool, tool_colors["other_tool"]),
        ("present_*", present, tool_colors["present"]),
        ("load_skill", skill, tool_colors["skill"]),
    ):
        ax2.barh(y, values, left=left, height=bar_h, color=color, label=name, zorder=2)
        left = left + values
    for i, row in enumerate(rows):
        total = left[i]
        n_search = sum(1 for t in row["tool_names"] if t == "search_products")
        ax2.text(
            total + max(left) * 0.02,
            i,
            f"{total:.0f} ms" + (f"  ·  {n_search} search" if n_search else ""),
            va="center",
            fontsize=8,
        )
    ax2.set_yticks(y)
    ax2.set_yticklabels([r["label"] for r in rows])
    ax2.invert_yaxis()
    ax2.set_xlabel("Tool CPU (ms), fully overlapped with LLM stream")
    ax2.set_title("Tool time (eager dispatch hides this from wall clock)")
    ax2.set_xlim(0, max(left) * 1.35 if max(left) > 0 else 1)
    ax2.legend(loc="lower right", frameon=False, fontsize=8, ncol=4)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    fig.savefig(path, dpi=160)
    plt.close(fig)


async def amain() -> dict[str, Any]:
    load_demo_env(REPO / "examples" / "retail")
    os.chdir(REPO)
    from shopping_agent_runtime import ShoppingAgent

    cfg = ShoppingAgentConfig(
        brand_name="ACME",
        assistant_name="ACME Assistant",
        brand_voice="professional, warm, and brief",
        request_timeout_s=LIVE_TIMEOUT_S,
        max_tool_iterations=5,
        enable_memory=False,
        **provider_config_overrides(),
    )
    ranker = bench_ranker()
    print(
        f"ranker ACME n={ranker['n']} mean={ranker['mean_ms']:.3f}ms "
        f"p50={ranker['p50_ms']:.3f}ms p95={ranker['p95_ms']:.3f}ms",
        flush=True,
    )

    logging.getLogger("shopping_agent_runtime.orchestrator").setLevel(logging.INFO)

    cases_out: list[dict[str, Any]] = []
    warmup_spans: list[dict[str, Any]] = []
    backend = MockRetail()
    client = wrap_client(make_client(LIVE_TIMEOUT_S), warmup_spans)
    agent = ShoppingAgent(
        backend=backend,
        skills_dir=SKILLS,
        config=cfg,
        client=client,
        executor_class=make_executor_class(warmup_spans),
    )
    try:
        warm = await asyncio.wait_for(
            run_turn(agent, warmup_spans, CASES[1]["text"], "warmup-adapter"),
            timeout=LIVE_TIMEOUT_S,
        )
        print(f"[warmup] adapter wall={warm['wall_s']:.2f}s n_llm={warm['n_llm']}", flush=True)
    except Exception as exc:
        print(f"[warmup] FAIL {type(exc).__name__}: {exc}", flush=True)

    for spec in CASES:
        runs: list[dict[str, Any]] = []
        for rep in range(N_REPS):
            spans: list[dict[str, Any]] = []
            backend = MockRetail()
            client = wrap_client(make_client(LIVE_TIMEOUT_S), spans)
            agent = ShoppingAgent(
                backend=backend,
                skills_dir=SKILLS,
                config=cfg,
                client=client,
                executor_class=make_executor_class(spans),
            )
            try:
                result = await asyncio.wait_for(
                    run_turn(agent, spans, spec["text"], f"{spec['id']}-r{rep}"),
                    timeout=LIVE_TIMEOUT_S,
                )
                runs.append(result)
                print(
                    f"[{spec['id']} r{rep}] wall={result['wall_s']:.2f}s "
                    f"n_llm={result['n_llm']} ttft={result['ttft_s']} "
                    f"ui={result['t_first_ui_s']} tools={result['tool_names']}",
                    flush=True,
                )
            except Exception as exc:
                runs.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{spec['id']} r{rep}] FAIL {type(exc).__name__}: {exc}", flush=True)
        block = summarize_case(runs)
        block["id"] = spec["id"]
        block["label"] = spec["label"]
        cases_out.append(block)

    ok_cases = [c for c in cases_out if c.get("ok")]
    mean_wall = sum(c["wall_s"] for c in ok_cases) / max(len(ok_cases), 1)
    mean_llm_share = (
        sum((c["attr"]["llm"] + c["attr"]["overlap"]) / c["wall_s"] for c in ok_cases)
        / max(len(ok_cases), 1)
        if ok_cases
        else 0.0
    )
    payload = {
        "model": cfg.model,
        "n_reps": N_REPS,
        "eager_tool_dispatch": cfg.eager_tool_dispatch,
        "rolling_conversation_cache": cfg.rolling_conversation_cache,
        "close_on_presentation": cfg.close_on_presentation,
        "ranker_acme": ranker,
        "mean_wall_s": mean_wall,
        "mean_llm_share": mean_llm_share,
        "cases": cases_out,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "latency.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if ok_cases:
        draw(payload, OUT / "fig_latency_breakdown.png")
        print("wrote", OUT / "fig_latency_breakdown.png")
    print("wrote", OUT / "latency.json")
    return payload


def main() -> int:
    plt.switch_backend("Agg")
    if os.environ.get("LATENCY_PLOT_ONLY") == "1":
        payload = json.loads((OUT / "latency.json").read_text(encoding="utf-8"))
        draw(payload, OUT / "fig_latency_breakdown.png")
        print("wrote", OUT / "fig_latency_breakdown.png")
        return 0
    payload = asyncio.run(amain())
    ok = sum(1 for c in payload["cases"] if c.get("ok"))
    print(f"ok_cases={ok}/{len(payload['cases'])} mean_wall={payload['mean_wall_s']:.2f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
