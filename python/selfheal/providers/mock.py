from __future__ import annotations

from selfheal.models import RepairProposal, RepairRequest
from selfheal.providers.base import AIProvider


class MockAIProvider(AIProvider):
    """
    Deterministic provider for the included division-by-zero demo.

    It is deliberately narrow. It proves the runtime transaction without
    requiring an external AI API.
    """

    def propose_repair(
        self,
        request: RepairRequest,
    ) -> RepairProposal | None:
        failure = request.failure

        if failure.exception_type != "ZeroDivisionError":
            return None

        if failure.symbol_name != "divide":
            return None

        replacement = """def divide(a, b):
    if b == 0:
        return None
    return a / b
"""

        return RepairProposal(
            target_file=failure.target_file,
            replacement_source=replacement,
            explanation=(
                "Guard division by zero so the operation can return a "
                "controlled value instead of raising."
            ),
        )
