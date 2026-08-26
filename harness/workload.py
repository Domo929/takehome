"""Workload corpus.

Shaped after what this system appears to exist for: asking a model which brands it
recommends in a category, then extracting what it said about them. Extraction is not
mention-counting - "we would not recommend BrandA" contains the mention and means the
opposite - so the real downstream step is closer to sentiment-attributed entity
extraction. That does not change the load profile, but it does change what a truncated
answer costs: a fragment cut mid-sentence can invert the sense of the clause it was
in, which is worse than a missing sample.

Prompt length is a guess, and it is the assumption in this file most likely to be
wrong. These sit under 250 characters because that is the scale the shipped providers
appear built around, not because anyone confirmed it. A corpus of long analytical
prompts would shift the workload from request-bound to token-bound and move every
throughput number in FINDINGS. `--complex-fraction` exists so that regime can be
measured rather than assumed; COMPLEX_TEMPLATES below is the same idea in k6.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

SYSTEM_PROMPT = (
    "You are a market research assistant. Answer concisely and name specific brands "
    "and products. Do not add disclaimers."
)

CATEGORIES = [
    "robot vacuums", "electric toothbrushes", "noise-cancelling headphones",
    "running shoes", "espresso machines", "standing desks", "air purifiers",
    "mechanical keyboards", "electric kettles", "cordless drills",
    "wireless earbuds", "smart thermostats", "meal kit services", "cast iron skillets",
    "carry-on luggage", "mattresses", "dash cams", "portable power stations",
    "office chairs", "sous vide cookers",
]

TEMPLATES = [
    "Which {category} would you recommend?",
    "What are the best {category} available right now?",
    "I'm shopping for {category}. What should I consider?",
    "Name the top five {category} and say why.",
    "Which brands make the most reliable {category}?",
    "What {category} offer the best value for money?",
    "Compare the leading {category} on the market.",
    "If you had to pick one of the {category}, which would it be?",
]

# A deliberately harder set: longer expected answers, more reasoning. Used to show
# that the throughput ceiling moves when the workload shifts from request-bound to
# token-bound.
COMPLEX_TEMPLATES = [
    "Compare the top {category} across price, durability, and warranty, then rank them.",
    "Build a decision framework for choosing between {category} for a first-time buyer.",
    "What tradeoffs separate premium from mid-range {category}? Give concrete examples.",
]


@dataclass(frozen=True)
class Prompt:
    id: str
    system: str
    question: str
    category: str
    kind: str

    @property
    def char_len(self) -> int:
        return len(self.question)


def build_corpus(
    *,
    size: int = 200,
    complex_fraction: float = 0.0,
    seed: int = 20260824,
    repeat_prompt: bool = False,
) -> list[Prompt]:
    """Deterministic corpus.

    Seeded so a rerun issues the same questions in the same order: comparing two runs
    is only meaningful when the workload is identical.

    Two shapes, because they measure different things:

    * ``repeat_prompt=False`` (default) builds ``size`` *distinct* prompts. This is the
      right shape for throughput work, because varied inputs make it impossible to
      accidentally measure a cache instead of the vendor.
    * ``repeat_prompt=True`` repeats a single prompt ``size`` times, which is the real
      unit of work: Evertune samples one prompt 100 times and reads the distribution.

    Throughput does not care which is used. The workload is request-bound rather than
    content-bound, and at ~35 input tokens nothing is cacheable either way (FINDINGS
    6c). Interpretation cares a great deal, so the shape is explicit rather than
    implied.
    """
    rng = random.Random(seed)
    prompts: list[Prompt] = []
    if repeat_prompt:
        template = TEMPLATES[0]
        category = CATEGORIES[0]
        question = template.format(category=category)
        return [
            Prompt(
                id=f"repeat-{i:05d}",
                system=SYSTEM_PROMPT,
                question=question,
                category=category,
                kind="repeat",
            )
            for i in range(size)
        ]
    for i in range(size):
        category = CATEGORIES[i % len(CATEGORIES)]
        use_complex = rng.random() < complex_fraction
        template = rng.choice(COMPLEX_TEMPLATES if use_complex else TEMPLATES)
        question = template.format(category=category)
        digest = hashlib.sha256(question.encode()).hexdigest()[:12]
        prompts.append(
            Prompt(
                id=f"{i:05d}-{digest}",
                system=SYSTEM_PROMPT,
                question=question,
                category=category,
                kind="complex" if use_complex else "simple",
            )
        )
    return prompts


def corpus_fingerprint(prompts: list[Prompt]) -> str:
    """Stable hash of a corpus, recorded in run manifests so results are traceable."""
    h = hashlib.sha256()
    for p in prompts:
        h.update(p.id.encode())
        h.update(p.question.encode())
    return h.hexdigest()[:16]


def mean_input_chars(prompts: list[Prompt]) -> float:
    return sum(p.char_len for p in prompts) / max(1, len(prompts))
