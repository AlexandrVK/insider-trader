"""Этап 6: полноценная оценка, статистический вердикт, Monte Carlo, bootstrap CI, атрибуция PnL, MD/HTML/CSV."""
from __future__ import annotations

import csv
import html
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np

from .config import Settings, load_config
from .db import Database


def max_drawdown(values: list[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100.0


def bootstrap_expectancy_ci(pnls: list[float], n_boot: int = 500, seed: int = 42) -> tuple[float, float]:
    if len(pnls) < 5:
        return (float("nan"), float("nan"))
    arr = np.asarray(pnls, dtype=float)
    rng = np.random.default_rng(seed)
    means = [float(np.mean(rng.choice(arr, size=len(arr), replace=True))) for _ in range(n_boot)]
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def monte_carlo_percentile(pnls: list[float], n_sim: int = 500, seed: int = 42) -> float:
    if len(pnls) < 5:
        return 50.0
    arr = np.asarray(pnls, dtype=float)
    actual = float(np.sum(arr))
    rng = np.random.default_rng(seed)
    sims = [float(np.sum(np.abs(arr) * rng.choice([-1.0, 1.0], size=len(arr)))) for _ in range(n_sim)]
    return float(np.mean([1.0 if actual > s else 0.0 for s in sims]) * 100.0)


def trade_stats(rows: list[dict]) -> dict:
    pnl = [float(r["pnl"] or 0.0) for r in rows]
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    arr = np.asarray(pnl, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if n > 1 else 0.0
    down_std = float(np.std(losses, ddof=1)) if len(losses) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(n)) if std > 1e-12 else 0.0
    sortino = (mean / down_std * math.sqrt(n)) if down_std > 1e-12 else 0.0
    gl = float(-np.sum(losses))
    pf = (float(np.sum(wins)) / gl) if gl > 0 else (float("inf") if len(wins) else 0.0)
    ci_lo, ci_hi = bootstrap_expectancy_ci(pnl)
    max_single_share = (float(np.max(wins)) / float(np.sum(arr))) if len(wins) and np.sum(arr) > 0 else 1.0
    return {
        "n": n,
        "win_rate": len(wins) / n * 100.0,
        "total": float(np.sum(arr)),
        "expectancy": mean,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "profit_factor": pf,
        "sharpe": sharpe,
        "sortino": sortino,
        "mc_percentile": monte_carlo_percentile(pnl),
        "max_single_trade_share": max_single_share,
        "fees": sum(float(r["fees"] or 0.0) for r in rows),
        "mae_pct": sum(float(r["mae_pct"] or 0.0) for r in rows) / n,
        "mfe_pct": sum(float(r["mfe_pct"] or 0.0) for r in rows) / n,
    }


def evaluate_verdict(all_real_stats: dict, days: float, dd_pct: float, cfg: Settings) -> tuple[str, str]:
    v = cfg.verdict
    n = all_real_stats.get("n", 0)
    weeks = days / 7.0
    if weeks < v.min_weeks or n < v.min_trades:
        return (
            "не доказано",
            f"Недостаточно данных: {n} сделок за {weeks:.1f} нед. (пороги: >= {v.min_trades} сделок и >= {v.min_weeks} нед.).",
        )
    ok_sharpe = all_real_stats["sharpe"] > v.min_sharpe
    ok_dd = dd_pct < v.max_drawdown_pct
    ok_pf = all_real_stats["profit_factor"] > v.min_profit_factor
    ok_ci = not math.isnan(all_real_stats["ci_lo"]) and all_real_stats["ci_lo"] > 0
    ok_conc = all_real_stats["max_single_trade_share"] < 0.45

    if ok_sharpe and ok_dd and ok_pf and ok_ci and ok_conc:
        return (
            "статистически подтверждено",
            f"Sharpe={all_real_stats['sharpe']:.2f}, PF={all_real_stats['profit_factor']:.2f}, "
            f"DD={dd_pct:.1f}%, CI_lo={all_real_stats['ci_lo']:.2f} > 0, MC={all_real_stats['mc_percentile']:.0f}%.",
        )
    if all_real_stats["total"] > 0 and ok_dd and all_real_stats["profit_factor"] > 1.05:
        return (
            "обнадёживающе",
            f"Положительный результат (PnL={all_real_stats['total']:.2f}, PF={all_real_stats['profit_factor']:.2f}), "
            "но не все статистические пороги пройдены.",
        )
    return (
        "не доказано",
        f"Стратегии не превосходят пороги (Sharpe={all_real_stats['sharpe']:.2f}, PF={all_real_stats['profit_factor']:.2f}, DD={dd_pct:.1f}%).",
    )


def build_report(db: Database, cfg: Settings, out_dir: Optional[str] = None, now: Optional[float] = None) -> dict:
    now = now or time.time()
    out = Path(out_dir or cfg.storage.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M", time.gmtime(now))

    snaps = db.query("SELECT ts, equity, cash, exposure, drawdown FROM snapshots ORDER BY ts")
    bets = db.query("SELECT * FROM bets WHERE status='closed'")
    real_bets = [b for b in bets if not b.get("benchmark")]
    ai_bets = [b for b in real_bets if b.get("ai_assisted")]
    no_ai_bets = [b for b in real_bets if not b.get("ai_assisted")]

    by_s: dict[str, list] = {}
    for b in bets:
        by_s.setdefault(b["strategy_id"], []).append(b)
    stats = {sid: trade_stats(rows) for sid, rows in by_s.items()}
    real_agg = trade_stats(real_bets)

    t0 = snaps[0]["ts"] if snaps else now
    t1 = snaps[-1]["ts"] if snaps else now
    days = (t1 - t0) / 86400.0
    eq0 = cfg.capital.initial_usdt
    eq1 = snaps[-1]["equity"] if snaps else eq0
    dd = max_drawdown([eq0] + [s["equity"] for s in snaps])
    ret = (eq1 / eq0 - 1.0) * 100.0
    calmar = (ret / dd) if dd > 0 else 0.0
    category, detail = evaluate_verdict(real_agg, days, dd, cfg)

    rej = Counter()
    for r in db.query("SELECT reasons_json FROM rejected_signals"):
        for reason in json.loads(r["reasons_json"]):
            rej[reason.split(":")[0]] += 1

    L = [
        f"# Отчёт paper-торговли ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))})", "",
        "> Бумажная торговля (paper). Обещаний прибыли нет. Бэктест LLM ненадёжен — учитывается только forward-тест.", "",
        f"## Вердикт: **{category.upper()}**",
        detail, "",
        "## Сводка портфеля",
        f"- Период: {days:.1f} дн. ({len(snaps)} снимков)",
        f"- Капитал: {eq0:,.2f} -> {eq1:,.2f} USDT ({ret:+.2f}%)",
        f"- Max Drawdown: {dd:.2f}% | Calmar: {calmar:.2f}",
        f"- Закрытых сделок (всего / без бенчмарка): {len(bets)} / {len(real_bets)}",
        f"- Атрибуция ИИ: с ИИ ({len(ai_bets)} сд., PnL={sum(b['pnl'] or 0 for b in ai_bets):.2f}) "
        f"vs без ИИ ({len(no_ai_bets)} сд., PnL={sum(b['pnl'] or 0 for b in no_ai_bets):.2f})", "",
        "## По стратегиям",
        "| Стратегия | Сделок | Win% | PnL USDT | Ожидание | Sharpe | Sortino | PF | MC% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for sid, st in sorted(stats.items()):
        L.append(
            f"| {sid} | {st['n']} | {st['win_rate']:.1f} | {st['total']:.2f} | {st['expectancy']:.2f} | "
            f"{st['sharpe']:.2f} | {st['sortino']:.2f} | {st['profit_factor']:.2f} | {st['mc_percentile']:.0f} |"
        )
    if not stats:
        L.append("| (закрытых сделок пока нет) | 0 | - | - | - | - | - | - | - |")

    L += ["", "## Отклонённые сигналы", ""] + ([f"- {k}: {v}" for k, v in rej.most_common()] or ["- нет"])
    md_text = "\n".join(L)

    md_path = out / f"report_{stamp}.md"
    md_path.write_text(md_text, encoding="utf-8")

    html_path = out / f"report_{stamp}.html"
    html_path.write_text(
        f"<!doctype html><html lang='ru'><meta charset='utf-8'><title>Report {stamp}</title>"
        f"<body style='font-family:system-ui;max-width:960px;margin:24px auto;background:#111;color:#eee;padding:16px'>"
        f"<pre style='white-space:pre-wrap'>{html.escape(md_text)}</pre></body></html>",
        encoding="utf-8",
    )

    csv_path = out / f"trades_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "symbol", "instrument", "side", "strategy", "ai_assisted", "entry_px", "exit_px", "pnl", "fees", "exit_reason"])
        for b in bets:
            w.writerow([b["id"], b["symbol"], b["instrument"], b["side"], b["strategy_id"], b.get("ai_assisted", 0),
                        b["entry_px"], b["exit_px"], b["pnl"], b["fees"], b["exit_reason"]])

    return {
        "paths": {"md": str(md_path), "html": str(html_path), "trades_csv": str(csv_path)},
        "stats": stats,
        "verdict_category": category,
        "verdict": f"{category}: {detail}",
        "return_pct": ret,
        "days": days,
    }


if __name__ == "__main__":
    cfg = load_config("config.yaml")
    db = Database(cfg.storage.db_path)
    res = build_report(db, cfg)
    print(Path(res["paths"]["md"]).read_text(encoding="utf-8"))
