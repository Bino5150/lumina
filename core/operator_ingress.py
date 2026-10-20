"""OPERATOR-INGRESS-PROVENANCE-01 -- where a piece of owner-facing text came from.

Operator commands (/status, /btw, /compact, /stop, /stop all, /context ...)
are desktop authority. Whether text may run one is decided by a
runtime-minted ingress envelope created at the point the text entered
Lumina -- never by the text itself, a `source` string, an event-id prefix,
a model-supplied flag, or ambient UI state.

  LOCAL_TEXT_SUBMISSION      the owner typed it into the desktop chat input
                             (ui/main_window.py's message_submitted slot);
                             the only kind that may dispatch a command.
  TELEGRAM_RELAYED_TEXT      authenticated owner Telegram text relayed into
                             the desktop (origin routing). An ordinary owner
                             turn, never a command, whatever its leading
                             characters.
  LOCAL_RUNTIME_ACTION       text Lumina's own UI code synthesizes for a
                             local action (the image-approval button).
  LOCAL_SUSPENSION_CONTROL   an owner action on the desktop's runaway-
                             suspension controls (initialize, resume,
                             recover; AGENT-RUNAWAY-SUSPEND-01). Never a
                             command, and never minted for relayed text.

Before this module, Telegram's `filters.TEXT & ~filters.COMMAND` was the
only barrier: it keys on a bot_command entity at offset 0, so a
whitespace-prefixed or entity-less " /btw test" passed it, was relayed, and
the desktop parser (which strips whitespace) dispatched it.

Envelopes can only be built by the factories below (a module-private mint
token), are frozen, and are checked by exact type, so neither a forged
look-alike object nor a subclass counts as local authority. Pure module: no
Qt, no Telegram imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.operator_commands import parse_operator_command


class IngressKind(Enum):
    LOCAL_TEXT_SUBMISSION = "local_text_submission"
    TELEGRAM_RELAYED_TEXT = "telegram_relayed_text"
    LOCAL_RUNTIME_ACTION = "local_runtime_action"
    LOCAL_SUSPENSION_CONTROL = "local_suspension_control"


_MINT = object()

# Fixed machine text for owner text, arriving over Telegram, that the desktop
# parser would read as an operator command. Echoes nothing back.
RELAYED_COMMAND_REFUSAL = (
    "[Lumina: operator commands only run when typed into the desktop chat. "
    "That message looked like one, so it was not run and was not sent to the model.]"
)


@dataclass(frozen=True)
class OperatorIngress:
    kind: IngressKind
    event_id: Optional[str]
    _mint: object

    def __post_init__(self):
        if self._mint is not _MINT:
            raise TypeError("OperatorIngress is created only by core.operator_ingress factories")


def local_text_submission() -> OperatorIngress:
    """The owner submitted text from the desktop chat input."""
    return OperatorIngress(IngressKind.LOCAL_TEXT_SUBMISSION, None, _MINT)


def telegram_relayed_text(event_id: Optional[str]) -> OperatorIngress:
    """Authenticated owner Telegram text relayed into the desktop."""
    return OperatorIngress(IngressKind.TELEGRAM_RELAYED_TEXT, event_id, _MINT)


def local_runtime_action(name: str) -> OperatorIngress:
    """Text synthesized by Lumina's own UI code for a local action."""
    return OperatorIngress(IngressKind.LOCAL_RUNTIME_ACTION, name, _MINT)


def local_suspension_control(action: str) -> OperatorIngress:
    """The owner acted on the desktop's runaway-suspension controls."""
    return OperatorIngress(IngressKind.LOCAL_SUSPENSION_CONTROL, action, _MINT)


def may_dispatch_operator_command(ingress) -> bool:
    """True only for a genuine local text-submission envelope."""
    return (type(ingress) is OperatorIngress
            and ingress._mint is _MINT
            and ingress.kind is IngressKind.LOCAL_TEXT_SUBMISSION)


def may_control_suspension(ingress) -> bool:
    """True only for a genuine local desktop envelope that may initialize,
    resume or recover the runaway-suspension store: the suspension controls
    themselves, or text the owner typed into the desktop chat. Relayed
    Telegram text and other runtime actions (the image-approval button)
    never qualify."""
    return (type(ingress) is OperatorIngress
            and ingress._mint is _MINT
            and ingress.kind in (IngressKind.LOCAL_SUSPENSION_CONTROL,
                                 IngressKind.LOCAL_TEXT_SUBMISSION))


def looks_like_operator_command(text) -> bool:
    """Exactly the desktop parser's own test, so the Telegram refusal and the
    desktop dispatcher can never disagree about what counts as a command."""
    return isinstance(text, str) and parse_operator_command(text) is not None
