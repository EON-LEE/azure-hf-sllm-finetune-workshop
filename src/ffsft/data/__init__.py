"""Dataset loading with license gating.

Loads the Korean SFT/DPO mixes declared in `configs/datasets.yaml`. The gating
rule that lives here: non-commercial datasets require an explicit opt-in, and
benchmark datasets can never enter a training mix.
"""
