"""
PIN verification tool — lets the model accept a PIN typed in conversation
and forward it to the code-level gate. The model never decides whether the
gate opens; it just relays the string. See core/pin_gate.py for the actual check.
"""
from core.pin_gate import verify_pin


def register_pin_tools(registry, channel_id: str = "default"):
    def submit_pin(pin: str) -> str:
        ok, msg = verify_pin(pin, channel_id=channel_id)
        return "PIN accepted." if ok else f"PIN rejected: {msg}"

    registry.register(
        name="submit_pin",
        fn=submit_pin,
        description=(
            "Submit a PIN/codeword provided by the user in conversation to unlock "
            "sensitive actions for this session. Call this whenever the user provides "
            "what appears to be a PIN, even mid-conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "The PIN/codeword as typed by the user."}
            },
            "required": ["pin"]
        }
    )