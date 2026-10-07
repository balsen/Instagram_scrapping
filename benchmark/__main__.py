from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from scraper import (
    AccessBlocked,
    InstagramScraper,
    InvalidUsername,
    ProfileNotFound,
    RateLimited,
    ScraperConfig,
    ScraperError,
)

from .ground_truth import fetch_ground_truth
from .kpis import CaseRun, compute_kpis, post_checks, profile_checks
from .media import check_media_urls
from .report import render

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "output.schema.json").read_text())
DEFAULT_PROFILES = ["natgeo", "nasa", "instagram", "nike", "cristiano"]
ERROR_CASES = [("thisuserdoesnotexist_zq_98761", "not_found"), ("bad name", "invalid")]
OUTCOMES = (
    (InvalidUsername, "invalid"),
    (ProfileNotFound, "not_found"),
    (RateLimited, "rate_limited"),
    (AccessBlocked, "blocked"),
    (ScraperError, "error"),
)

logger = logging.getLogger("benchmark")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark", description="Benchmark the scraper against KPI targets."
    )
    parser.add_argument("--profiles", nargs="+", default=DEFAULT_PROFILES)
    parser.add_argument("--accuracy-sample", type=int, default=5)
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between profiles")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "benchmark" / "results")
    parser.add_argument("--report", type=Path, default=ROOT / "BENCHMARK.md")
    parser.add_argument("--strict", action="store_true", help="exit 1 if a KPI misses target")
    return parser.parse_args(argv)


async def _run_case(
    username: str, expected: str, config: ScraperConfig, sample: int, validator
) -> CaseRun:
    started = time.perf_counter()
    try:
        result = (await InstagramScraper(config).scrape(username)).to_dict()
    except ScraperError as exc:
        outcome = next(label for kind, label in OUTCOMES if isinstance(exc, kind))
        return CaseRun(username, expected, outcome, time.perf_counter() - started, str(exc))
    run = CaseRun(username, expected, "ok", time.perf_counter() - started, result=result)

    run.schema_errors = [
        f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
        for error in validator.iter_errors(result)
    ]

    shortcodes = [post["shortcode"] for post in result["posts"][:sample] if post["shortcode"]]
    profile_truth, post_truths = await fetch_ground_truth(config, username, shortcodes)
    comparisons = [("profile", profile_checks(result, profile_truth))] if profile_truth else []
    comparisons += [
        (post["shortcode"], post_checks(post, post_truths[post["shortcode"]]))
        for post in result["posts"]
        if post["shortcode"] in post_truths
    ]
    checks: dict[str, list[bool]] = {}
    for subject, comparison in comparisons:
        for name, (ok, scraped, reference) in comparison.items():
            if ok is None:
                continue
            checks.setdefault(name, []).append(ok)
            if not ok:
                run.mismatches.append(
                    {"subject": subject, "field": name, "scraped": scraped, "reference": reference}
                )
    run.checks = checks

    urls = [post["image_url"] or post["video_thumbnail"] for post in result["posts"]]
    run.media = await check_media_urls([url for url in urls if url], config.user_agent)
    return run


async def _run(args: argparse.Namespace) -> list[CaseRun]:
    config = replace(ScraperConfig.from_env(), max_posts=None)
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    cases = [(username, "ok") for username in args.profiles] + ERROR_CASES
    runs = []
    for index, (username, expected) in enumerate(cases):
        if index and expected != "invalid":
            await asyncio.sleep(args.pause)
        logger.info("[%d/%d] %s", index + 1, len(cases), username)
        run = await _run_case(username, expected, config, args.accuracy_sample, validator)
        logger.info("    → %s in %.1fs", run.outcome, run.wall_seconds)
        runs.append(run)
    return runs


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    logging.getLogger("scraper").setLevel(logging.WARNING)

    started_at = datetime.now(timezone.utc)
    runs = asyncio.run(_run(args))
    kpis = compute_kpis(runs)
    environment = {
        "date": started_at.strftime("%Y-%m-%d %H:%M UTC"),
        "session": "logged-in" if ScraperConfig.from_env().storage_state else "logged-out",
        "python": platform.python_version(),
        "playwright": version("playwright"),
        "platform": f"{platform.system()} {platform.machine()}",
        "accuracy_sample": args.accuracy_sample,
    }

    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    raw_dir = args.results_dir / stamp
    raw_dir.mkdir(parents=True, exist_ok=True)
    for run in runs:
        if run.result:
            (raw_dir / f"{run.username}.json").write_text(
                json.dumps(run.result, indent=2, ensure_ascii=False) + "\n"
            )
    summary = {
        "environment": environment,
        "kpis": [asdict(kpi) for kpi in kpis],
        "cases": [
            {
                "username": run.username,
                "expected": run.expected,
                "outcome": run.outcome,
                "wall_seconds": round(run.wall_seconds, 2),
                "error": run.error,
                "posts": len(run.result["posts"]) if run.result else None,
                "stats": run.result["meta"]["stats"] if run.result else None,
                "schema_errors": run.schema_errors,
                "accuracy_checks": run.checks,
                "mismatches": run.mismatches,
                "media_checks": run.media,
            }
            for run in runs
        ],
    }
    (args.results_dir / f"{stamp}.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )
    args.report.write_text(render(kpis, runs, environment))

    gated = [kpi for kpi in kpis if kpi.passed is not None]
    failed = [kpi for kpi in gated if not kpi.passed]
    logger.info(
        "\n%d/%d KPIs met target. Report: %s", len(gated) - len(failed), len(gated), args.report
    )
    for kpi in kpis:
        mark = {True: "PASS", False: "FAIL", None: "    "}[kpi.passed]
        logger.info("  %s  %-48s %s", mark, kpi.name, kpi.display)
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    sys.exit(main())
