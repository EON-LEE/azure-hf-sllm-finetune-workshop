"""Training backends.

Four interchangeable paths, selected by the `ModelSpec.provider` of whichever
model the user picked:

- ``local``               -- QLoRA/LoRA via trl+peft on a local GPU
- ``aml``                 -- the same script submitted as an Azure ML command job
- ``foundry_serverless``  -- submit JSONL to Foundry serverless fine-tuning
- ``aoai``                -- Azure OpenAI SFT/DPO/RFT
"""
