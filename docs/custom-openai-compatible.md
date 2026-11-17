# Using a custom OpenAI-compatible endpoint

Select **Custom (OpenAI-compatible)** in Settings and configure its own endpoint,
API key, and a model returned by that endpoint's model discovery. Keep the keys
for fixed providers in their respective backends.

For example, to test A2Agent through the existing Custom backend:

- Base URL: `https://api.a2agent.me/v1`
- API key: `<YOUR_PRIVATE_TEST_KEY>` (enter it only in the API key field)
- Model: select an available model from **Refresh Models**; availability depends
  on the key and can change.

The Chat Completions route is `/v1/chat/completions`; do not append another `/v1`
to the base URL. No built-in provider entry or fixed-provider URL override is
required. This configuration example is not a certification of all models or
features, and no live provider requests are made by the regression tests.

The offline regression fixtures in `tests/test_chat_completions_sse_fields.py`
exercise both `data: {...}` and `data:{...}` SSE fields through the shared
regular streaming and telemetry-assembly paths. They include fragmented reads,
UTF-8 text, malformed frames, terminal markers, trailing usage, tool-call
assembly, transport interruption, and consumer cancellation.

Run these tests without credentials:

```sh
python -m pytest tests/test_chat_completions_sse_fields.py tests/test_lmstudio_backend.py
```
