"""Small execution boundary; adapters own preparation data and durable receipts."""

from typing import Protocol, TypeVar

Prepared = TypeVar("Prepared")
Receipt = TypeVar("Receipt", covariant=True)


class ExecutionBackend(Protocol[Prepared, Receipt]):
    """Prepare without allocating, start once, inspect the same retained execution.

    Only explicit ParameterError from prepare permits live author correction.
    start must persist identity before external effects; ambiguous effects block
    rather than permit another start. Cancellation belongs to adapters that need it.
    """

    def prepare(self) -> Prepared: ...

    def start(self, prepared: Prepared) -> Receipt: ...

    def inspect(self) -> Receipt: ...
