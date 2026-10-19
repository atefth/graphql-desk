"""Publication guards for one generation job, without editor or file mutation."""

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

_Value = TypeVar("_Value")


@dataclass(frozen=True)
class Snapshot:
    """The source and all semantic inputs observed for a generation request."""

    project: str
    schema: str
    source_uri: str
    source_version: int
    dependency_versions: tuple[tuple[str, int], ...]
    schema_fingerprint: str
    config_fingerprint: str

    def __post_init__(self) -> None:
        dependencies = tuple(sorted(self.dependency_versions))
        if len({uri for uri, _ in dependencies}) != len(dependencies):
            raise ValueError("Dependency URIs must be unique")
        object.__setattr__(self, "dependency_versions", dependencies)


@dataclass(frozen=True, eq=False)
class Ticket:
    """Identity distinguishes requests even when their snapshots are equal."""

    snapshot: Snapshot


class Coordinator(Generic[_Value]):
    """Keep only current work and its value; callers serialize all transitions.

    Completion requires a freshly observed snapshot from the caller. This guard
    cannot observe external changes or make subsequent editor writes atomic.
    """

    def __init__(self) -> None:
        self._pending: Ticket | None = None
        self._state: Literal["pending", "ready", "unavailable"] = "unavailable"
        self._value: _Value | None = None

    @property
    def state(self) -> Literal["pending", "ready", "unavailable"]:
        return self._state

    @property
    def value(self) -> _Value | None:
        return self._value

    def begin(self, snapshot: Snapshot) -> Ticket:
        """Replace previous work and stop presenting any older value as current."""
        ticket = Ticket(snapshot)
        self._pending = ticket
        self._state = "pending"
        self._value = None
        return ticket

    def complete(self, ticket: Ticket, observed: Snapshot, value: _Value) -> bool:
        """Accept only the active request with unchanged, freshly observed inputs."""
        if ticket is not self._pending:
            return False
        if observed != ticket.snapshot:
            self.invalidate()
            return False
        self._pending = None
        self._state = "ready"
        self._value = value
        return True

    def fail(self, ticket: Ticket) -> bool:
        """Failure of superseded work cannot invalidate its replacement."""
        if ticket is not self._pending:
            return False
        self.invalidate()
        return True

    def cancel(self, ticket: Ticket) -> bool:
        return self.fail(ticket)

    def invalidate(self) -> None:
        """External changes invalidate pending work and any published value."""
        self._pending = None
        self._state = "unavailable"
        self._value = None
