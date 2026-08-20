"""Korean corpus preparation logic, shared by Fabric and local runs.

This is the Fabric half of "Fabric + Foundry", and the split is economic rather
than architectural. Filtering, deduplicating and reformatting a Korean
instruction corpus is pure CPU work. Doing it inside the training job means an
A100 at $4.96/hr runs what is effectively pandas, and it repeats on every
restart. Fabric's Spark pool does it once on cheap CPU nodes and hands Azure ML
a clean JSONL.

The functions here are deliberately plain Python with no Spark import. A Fabric
notebook maps them over a DataFrame; the tests call them directly. Spark is
expensive to unit-test and none of the actual bugs live in the Spark plumbing --
they live in Unicode handling and filter thresholds, which is what is tested.

The one non-obvious rule is NFC normalisation. Korean text from macOS and from
several crawlers arrives decomposed (NFD): "가" as U+1100 U+1161 rather than
U+AC00. It renders identically, compares unequal, and hashes differently, so
deduplication silently does nothing on a corpus that mixes sources.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: Precomposed syllables, conjoining jamo, and compatibility jamo.
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")

#: Any letter, in any script. The denominator for the Korean ratio: counting
#: spaces or punctuation would make the ratio depend on formatting.
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

_WS_RUN = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

DEFAULT_MIN_HANGUL_RATIO = 0.3
DEFAULT_MIN_OUTPUT_CHARS = 10
DEFAULT_MAX_REPEAT_RATIO = 0.5


def hangul_ratio(text: str) -> float:
    """Fraction of *letters* that are Hangul. 0.0 for text with no letters."""
    if not text:
        return 0.0
    letters = _LETTER.findall(text)
    if not letters:
        return 0.0
    hangul = sum(1 for char in letters if _HANGUL.match(char))
    return hangul / len(letters)


def normalize_text(text: str) -> str:
    """NFC-normalise, collapse whitespace, and cap consecutive blank lines.

    Paragraph breaks survive because long-form Korean training data loses
    meaning without them; runs of five blank lines do not.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def dedup_key(text: str) -> str:
    """Stable hash for near-duplicate detection.

    Whitespace, Unicode form and punctuation are stripped before hashing.
    Crawled corpora repeat the same sentence with and without a trailing period
    constantly, and treating those as distinct inflates the dataset with rows
    that teach the model nothing.
    """
    canonical = normalize_text(text).lower()
    canonical = _PUNCT.sub("", canonical)
    canonical = re.sub(r"\s+", "", canonical)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _repetition_ratio(text: str) -> float:
    """How dominated the text is by its single most frequent token."""
    tokens = text.split()
    if len(tokens) < 4:
        return 0.0
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return max(counts.values()) / len(tokens)


def quality_reasons(
    instruction: str,
    output: str,
    *,
    min_hangul_ratio: float = DEFAULT_MIN_HANGUL_RATIO,
    min_output_chars: int = DEFAULT_MIN_OUTPUT_CHARS,
    max_repeat_ratio: float = DEFAULT_MAX_REPEAT_RATIO,
) -> list[str]:
    """Every reason to drop this row. Empty list means keep it.

    Returning reasons rather than a boolean is what makes the Fabric notebook
    useful: the rejected rows get written out grouped by reason, so a corpus
    that loses 80% of its rows can be diagnosed instead of guessed at.
    """
    reasons: list[str] = []

    instruction_n = normalize_text(instruction)
    output_n = normalize_text(output)

    if not instruction_n:
        reasons.append("empty_instruction")
    if not output_n:
        reasons.append("empty_output")

    if output_n and len(output_n) < min_output_chars:
        reasons.append("output_too_short")

    if instruction_n and output_n:
        combined = f"{instruction_n} {output_n}"
        if hangul_ratio(combined) < min_hangul_ratio:
            reasons.append("not_korean")
        if dedup_key(instruction_n) == dedup_key(output_n):
            # Scraped instruction sets are full of this, and training on it
            # teaches the model to echo the prompt back.
            reasons.append("output_echoes_instruction")
        if _repetition_ratio(output_n) > max_repeat_ratio:
            reasons.append("repetitive")

    return reasons


def build_chat_record(
    *,
    instruction: str,
    output: str,
    input_text: str = "",
    system: str = "",
    source: str = "",
) -> dict:
    """Convert one row into the chat format TRL's SFTTrainer consumes.

    Emitting `messages` rather than a pre-rendered prompt string keeps the
    chat template a property of the model: swapping the model in
    configs/models.yaml then changes the template automatically, which is the
    whole point of a model-swappable asset.
    """
    instruction_n = normalize_text(instruction)
    input_n = normalize_text(input_text)
    output_n = normalize_text(output)

    user_content = f"{instruction_n}\n\n{input_n}" if input_n else instruction_n

    messages = []
    if system:
        messages.append({"role": "system", "content": normalize_text(system)})
    messages.append({"role": "user", "content": user_content})
    messages.append({"role": "assistant", "content": output_n})

    record: dict = {"messages": messages}
    if source:
        record["source"] = source
    return record
