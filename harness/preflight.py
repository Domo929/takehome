"""Preflight check.

Run this before any load test against a real endpoint. It answers, for a fraction of
a cent, the questions that are expensive to discover mid-sweep:

* do the credentials resolve, and to which project and region
* is the model actually reachable and does it return what we expect
* what does one real request cost, and how long does it take
* is quota on-demand or provisioned (``trafficType``)
* does the thinking budget behave the way the provider assumes

A failed permission or a wrong region should cost one request, not a thousand.

Secrets are never printed. Keys are reported as present or absent with a masked
suffix so a screenshot or a pasted log cannot leak them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib

from dotenv import dotenv_values, load_dotenv
import sys
import time

from llm.errors import LLMAuthenticationError, LLMError
from llm.gemini import Gemini
from llm.pricing import pricing_for

ENV_FILE = ".env.local"


def load_env_file(path: str = ENV_FILE) -> list[str]:
    """Load an untracked env file into the environment. Returns the keys loaded.

    `override=False` so a real exported variable always beats the file. That ordering
    matters: CI and the shell are the authoritative sources, and a stale `.env.local`
    silently winning over an explicit export is a confusing way to run against the
    wrong project.

    This was hand-rolled first. Replaced after comparing against python-dotenv on the
    same input, where the hand-rolled version got three things wrong: `export KEY=val`
    produced a key literally named "export KEY", `KEY=` was dropped instead of set
    empty, and `.strip('"')` mangled any value containing quotes rather than failing.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return []
    parsed = dotenv_values(p)
    load_dotenv(p, override=False)
    return [k for k in parsed if k]


def mask(value: str | None) -> str:
    if not value:
        return "unset"
    return f"set (len={len(value)}, ...{value[-4:]})"


async def main_async(args: argparse.Namespace) -> int:
    loaded = load_env_file(args.env_file)
    if loaded:
        print(f"Loaded {len(loaded)} key(s) from {args.env_file}: {', '.join(sorted(loaded))}")

    backend = (args.backend or os.getenv("GEMINI_BACKEND") or "vertex").lower()

    print("\nCredentials")
    if backend == "developer":
        key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            print("  backend            developer (Gemini API)")
            print("\nNo API key found. Put GOOGLE_API_KEY=... in .env.local or export it.")
            return 2
        print("  backend            developer (Gemini API)")
        print(f"  GOOGLE_API_KEY     {mask(key)}")
    else:
        print(f"  backend            vertex")
        print(f"  GOOGLE_CLOUD_PROJECT   {os.getenv('GOOGLE_CLOUD_PROJECT') or 'unset'}")
        print(f"  GOOGLE_CLOUD_LOCATION  {os.getenv('GOOGLE_CLOUD_LOCATION') or 'global (default)'}")
        try:
            import google.auth

            creds, project = google.auth.default()
            print(f"  ADC                    resolved (project={project or 'n/a'})")
        except Exception as exc:
            print(f"  ADC                    FAILED: {exc}")
            print("\nRun: gcloud auth application-default login")
            return 2

    try:
        provider = Gemini(
            backend=backend,
            model=args.model,
            thinking_budget=args.thinking_budget,
            max_output_tokens=args.max_output_tokens,
        )
    except ValueError as exc:
        print(f"\nProvider could not be constructed: {exc}")
        return 2

    print("\nProvider")
    for key, value in provider.describe().items():
        print(f"  {key:<22} {value}")

    pricing = pricing_for(provider.model)
    print(f"  price in / out per 1M  ${pricing.input_per_1m:.2f} / ${pricing.output_per_1m:.2f}")

    print(f"\nSending 1 request to {provider.backend}:{provider.model} ...")
    started = time.perf_counter()
    try:
        result = await provider.ask_generic_question(
            "You are a market research assistant. Answer concisely.",
            args.question,
            1.0,
        )
    except LLMAuthenticationError as exc:
        print(f"\nAUTH FAILED: {exc}")
        print("  developer backend: check the API key is valid and the API is enabled")
        print("  vertex backend: check the project has aiplatform.googleapis.com enabled")
        return 1
    except LLMError as exc:
        print(f"\nFAILED [{exc.error_class}] {exc}")
        if exc.status_code:
            print(f"  HTTP {exc.status_code}")
        return 1

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    print("\nResult")
    print(f"  finish_reason          {result.finish_reason.value}")
    print(f"  usable                 {result.is_usable}")
    print(f"  latency (wall)         {elapsed_ms:.0f} ms")
    print(f"  attempts               {result.attempts}")
    print(f"  input tokens           {result.input_tokens}")
    print(f"  output tokens (billed) {result.output_tokens}")
    print(f"    of which thinking    {result.thinking_tokens}")
    print(f"    visible              {result.visible_output_tokens}")
    print(f"  cost                   ${result.cost_usd:.8f}")
    for key, value in result.metadata.items():
        print(f"  {key:<22} {value}")

    answer = (result.answer or "").strip().replace("\n", " ")
    print(f"\n  answer[:180]  {answer[:180]}")

    projected = (result.cost_usd or 0.0) * 1000
    print(f"\nAt this shape, 1,000 requests would cost about ${projected:.2f}.")
    print("Preflight OK.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate credentials and cost with one request.")
    p.add_argument("--backend", choices=["vertex", "developer"], default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--thinking-budget", type=int, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--env-file", default=ENV_FILE)
    p.add_argument(
        "--question",
        default="Which robot vacuum brands are worth considering?",
        help="Generic product question; avoids sending anything sensitive.",
    )
    return p.parse_args()


def main() -> None:
    sys.exit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
