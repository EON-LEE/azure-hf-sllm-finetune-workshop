# Fabric notebook source

# METADATA ********************
# META {
# META   "kernel_info": { "name": "synapse_pyspark" },
# META   "language_info": { "name": "python" }
# META }

# MARKDOWN ********************

# # 01 · Korean corpus preparation (Fabric)
#
# The CPU half of the pipeline. This notebook filters, deduplicates and
# reformats a Korean instruction corpus on Fabric's Spark pool, then writes a
# clean JSONL that the Azure ML GPU job trains on.
#
# **Why this is not part of the training job.** Filtering a corpus is pandas
# work. Running it inside the training script means an `NC24ads_A100_v4` at
# **$4.96/hr** does it, and redoes it on every restart and every hyperparameter
# sweep. Fabric does it once on CPU nodes and the GPU only ever sees clean rows.
#
# **What this notebook does not contain.** All of the filtering logic lives in
# `ffsft.data.fabric_prep`, which is plain Python with unit tests. Spark is
# expensive to test and no bugs live in the plumbing -- they live in Unicode
# handling and thresholds. Keeping the logic outside the notebook is what makes
# it testable.
#
# **Output contract.** One JSON object per line, `{"messages": [...]}`, the
# format `trl`'s `SFTTrainer` consumes directly. Emitting `messages` rather than
# a rendered prompt keeps the chat template a property of the model, so swapping
# the model in `configs/models.yaml` swaps the template too.

# PARAMETERS CELL ********************

# Overridable from a Fabric pipeline activity.
SOURCE_TABLE = ""          # Lakehouse table to read; empty means use HF_DATASETS
HF_DATASETS = "nlpai-lab/kullm-v2"   # comma-separated Hugging Face dataset ids
OUTPUT_PATH = "Files/ffsft/ko_sft"   # Lakehouse-relative output directory
REJECTED_PATH = "Files/ffsft/ko_rejected"
MIN_HANGUL_RATIO = 0.3
MIN_OUTPUT_CHARS = 10
MAX_ROWS = 0               # 0 = no cap; set small for a smoke run
SYSTEM_PROMPT = ""

# CELL ********************

import json
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, StringType, StructField, StructType

spark = SparkSession.builder.getOrCreate()

# ffsft is installed onto the Spark pool as a wheel, or added via
# %pip install. Failing loudly here beats a cryptic executor-side error later.
try:
    from ffsft.data.fabric_prep import (
        build_chat_record,
        dedup_key,
        quality_reasons,
    )
except ImportError:
    print(
        "ffsft is not installed on this Spark pool.\n"
        "Run  %pip install ffsft  in a cell above, or attach the wheel to the "
        "environment, then re-run.",
        file=sys.stderr,
    )
    raise

# CELL ********************

# MARKDOWN ********************
# ## Load
#
# Either a Lakehouse Delta table already landed by an upstream pipeline, or
# Hugging Face datasets pulled on the driver. The Hugging Face path is
# convenient but single-node: for anything large, land it to Delta first and
# point `SOURCE_TABLE` here.

# CELL ********************

def load_source():
    if SOURCE_TABLE:
        print(f"reading Delta table {SOURCE_TABLE}")
        return spark.read.table(SOURCE_TABLE)

    from datasets import load_dataset

    frames = []
    for dataset_id in [d.strip() for d in HF_DATASETS.split(",") if d.strip()]:
        print(f"downloading {dataset_id}")
        hf = load_dataset(dataset_id, split="train")
        rows = [
            {
                "instruction": str(row.get("instruction") or ""),
                "input": str(row.get("input") or ""),
                "output": str(row.get("output") or ""),
                "source": dataset_id,
            }
            for row in hf
        ]
        frames.append(spark.createDataFrame(rows))

    if not frames:
        raise ValueError("no source configured: set SOURCE_TABLE or HF_DATASETS")

    df = frames[0]
    for extra in frames[1:]:
        df = df.unionByName(extra)
    return df


raw = load_source()
if MAX_ROWS:
    raw = raw.limit(MAX_ROWS)

raw = raw.cache()
raw_count = raw.count()
print(f"loaded {raw_count:,} rows")

# CELL ********************

# MARKDOWN ********************
# ## Filter
#
# `quality_reasons` returns *every* reason a row fails rather than a boolean,
# so the rejects can be written out grouped by reason. A corpus that loses 80%
# of its rows is then diagnosable instead of a mystery.

# CELL ********************

reasons_udf = F.udf(
    lambda instruction, output: quality_reasons(
        instruction or "",
        output or "",
        min_hangul_ratio=MIN_HANGUL_RATIO,
        min_output_chars=MIN_OUTPUT_CHARS,
    ),
    ArrayType(StringType()),
)

scored = raw.withColumn("reasons", reasons_udf(F.col("instruction"), F.col("output")))

rejected = scored.filter(F.size("reasons") > 0)
kept = scored.filter(F.size("reasons") == 0).drop("reasons")

rejected_count = rejected.count()
print(f"rejected {rejected_count:,} of {raw_count:,}")

print("\nrejection reasons:")
(
    rejected.select(F.explode("reasons").alias("reason"))
    .groupBy("reason")
    .count()
    .orderBy(F.desc("count"))
    .show(truncate=False)
)

# CELL ********************

# MARKDOWN ********************
# ## Deduplicate
#
# `dedup_key` strips whitespace, punctuation and Unicode form before hashing.
# The Unicode part matters more than it looks: Korean text from macOS and from
# several crawlers arrives decomposed (NFD), so "가" is U+1100 U+1161 rather
# than U+AC00. It renders identically and hashes differently, which makes naive
# deduplication a no-op on any corpus that mixes sources.

# CELL ********************

dedup_udf = F.udf(lambda text: dedup_key(text or ""), StringType())

deduped = (
    kept.withColumn("_key", dedup_udf(F.concat_ws(" ", "instruction", "output")))
    .dropDuplicates(["_key"])
    .drop("_key")
)

deduped = deduped.cache()
final_count = deduped.count()
print(f"kept {final_count:,} after dedup ({kept.count() - final_count:,} duplicates)")

# CELL ********************

# MARKDOWN ********************
# ## Format and write

# CELL ********************

chat_schema = StructType([StructField("json", StringType())])


def to_chat_json(instruction, input_text, output, source):
    record = build_chat_record(
        instruction=instruction or "",
        output=output or "",
        input_text=input_text or "",
        system=SYSTEM_PROMPT,
        source=source or "",
    )
    return json.dumps(record, ensure_ascii=False)


chat_udf = F.udf(to_chat_json, StringType())

formatted = deduped.select(
    chat_udf(
        F.col("instruction"),
        F.coalesce(F.col("input"), F.lit("")),
        F.col("output"),
        F.coalesce(F.col("source"), F.lit("")),
    ).alias("value")
)

# coalesce(1) because the training job reads a single JSONL and a few hundred
# thousand chat rows is tens of MB, not a distributed-read problem.
formatted.coalesce(1).write.mode("overwrite").text(OUTPUT_PATH)
rejected.write.mode("overwrite").json(REJECTED_PATH)

print(f"\nwrote {final_count:,} rows to {OUTPUT_PATH}")
print(f"wrote {rejected_count:,} rejects to {REJECTED_PATH}")

# CELL ********************

# MARKDOWN ********************
# ## Handoff to Azure ML
#
# The training job reads this path as an `azureml://` data asset. Register it
# once, then every run refers to a version rather than a path:
#
# ```bash
# az ml data create --name ko-sft --version 1 \
#   --path abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lakehouse>/Files/ffsft/ko_sft \
#   --type uri_folder
# ```
#
# Then:
#
# ```bash
# ffsft train submit --model qwen3.8-27b --data azureml:ko-sft:1
# ```

# CELL ********************

display(deduped.limit(5))
