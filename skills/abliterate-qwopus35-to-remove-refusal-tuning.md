# Skill: Abliterate Qwopus3.5 (remove refusal tuning)

## Procedure

1. **Download and verify BLF16 source**
   ```bash
   # Download from HuggingFace
   huggingface-cli download Jackrong/Qwopus3.5-4B-v3 --local-dir ~/qwopus-bf16/ 
   ```
   - Verify you have two shards (~9.3GB total): `model-index.json` + safetensors files
   - Check `config.json`: should have `