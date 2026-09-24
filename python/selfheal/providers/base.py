from __future__ import annotations

from abc import ABC, abstractmethod

from selfheal.models import RepairProposal, RepairRequest


class AIProvider(ABC):
    """Provider-neutral AI repair contract."""

    @abstractmethod
    def propose_repair(
        self,
        request: RepairRequest,
    ) -> RepairProposal | None:
        raise NotImplementedError
