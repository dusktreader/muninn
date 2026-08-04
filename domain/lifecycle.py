"""Pure lifecycle transition helpers."""

from muninn.models import SyncState

LEGAL_TRANSITIONS: dict[SyncState, frozenset[SyncState]] = {
    SyncState.LOCAL_ONLY: frozenset({SyncState.SYNCING, SyncState.ATTENTION_REQUIRED, SyncState.DELETION_PENDING}),
    SyncState.SYNCING: frozenset({SyncState.LOCAL_ONLY, SyncState.PROCESSING, SyncState.ATTENTION_REQUIRED, SyncState.DELETION_PENDING}),
    SyncState.PROCESSING: frozenset({SyncState.READY, SyncState.FAILED, SyncState.ATTENTION_REQUIRED, SyncState.DELETION_PENDING}),
    SyncState.READY: frozenset({SyncState.PROCESSING, SyncState.ATTENTION_REQUIRED, SyncState.DELETION_PENDING}),
    SyncState.FAILED: frozenset({SyncState.PROCESSING, SyncState.DELETION_PENDING}),
    SyncState.ATTENTION_REQUIRED: frozenset({SyncState.SYNCING, SyncState.DELETION_PENDING}),
    SyncState.DELETION_PENDING: frozenset(),
}


def transition(current: SyncState, requested: SyncState) -> SyncState:
    """Apply one legal lifecycle transition."""
    if requested not in LEGAL_TRANSITIONS[current]:
        raise ValueError(f"illegal transition: {current} -> {requested}")
    return requested
