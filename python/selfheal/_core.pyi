class VerificationReport:
    ok: bool
    syntax_ok: bool
    tests_ok: bool | None
    details: str
    proposal_id: str


class RepairReceipt:
    proposal_id: str
    target_file: str
    snapshot_file: str
    verification_ok: bool
    verification_details: str


class CoreEngine:
    def __init__(
        self,
        project_root: str,
        python_executable: str,
        run_tests: bool = True,
        snapshot_dir: str | None = None,
        audit_file: str | None = None,
    ) -> None: ...

    def verify(
        self,
        target_file: str,
        replacement_source: str,
        proposal_id: str | None = None,
    ) -> VerificationReport: ...

    def verify_and_apply(
        self,
        target_file: str,
        replacement_source: str,
        proposal_id: str | None = None,
        expected_source: str | None = None,
    ) -> RepairReceipt: ...

    def rollback(
        self,
        target_file: str,
        snapshot_file: str,
        proposal_id: str,
        reason: str,
    ) -> None: ...

    def commit(
        self,
        target_file: str,
        proposal_id: str,
    ) -> None: ...

    def record_failure(
        self,
        target_file: str,
        exception_type: str,
        message: str,
        module_name: str,
        symbol_name: str,
    ) -> None: ...

    def record_repair_event(self, target_file: str, proposal_id: str,
                            event: str, details: str) -> None: ...
