use crate::audit::AuditLogger;
use crate::error::{CoreError, CoreResult};
use crate::patch::replace_text;
use crate::policy::PolicyEngine;
use crate::snapshot::SnapshotManager;
use crate::verifier::{VerificationConfig, VerificationReport, Verifier};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::fs;
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct EngineConfig {
    pub project_root: PathBuf,
    pub python_executable: String,
    pub run_tests: bool,
    pub snapshot_dir: String,
    pub audit_file: String,
}

#[derive(Debug, Clone)]
pub struct RepairReceipt {
    pub proposal_id: String,
    pub target_file: PathBuf,
    pub snapshot_file: PathBuf,
    pub verification: VerificationReport,
}

#[derive(Debug, Clone)]
pub struct CoreEngine {
    policy: PolicyEngine,
    verifier: Verifier,
    snapshots: SnapshotManager,
    audit: AuditLogger,
}

impl CoreEngine {
    pub fn new(config: EngineConfig) -> CoreResult<Self> {
        let policy = PolicyEngine::new(config.project_root)?;
        let root = policy.project_root().to_path_buf();

        let verifier = Verifier::new(
            policy.clone(),
            VerificationConfig {
                python_executable: config.python_executable,
                run_tests: config.run_tests,
            },
        );

        let snapshots = SnapshotManager::new(root.clone(), &config.snapshot_dir);
        let audit = AuditLogger::new(&root, &config.audit_file);

        Ok(Self {
            policy,
            verifier,
            snapshots,
            audit,
        })
    }

    pub fn verify(
        &self,
        target_file: &Path,
        replacement_source: &str,
        proposal_id: Option<&str>,
    ) -> CoreResult<(String, VerificationReport)> {
        let target = self.policy.ensure_target_allowed(target_file)?;
        let proposal_id = proposal_id
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        validate_proposal_id(&proposal_id)?;

        self.audit.write(
            "verification_started",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
            }),
        )?;

        let report = self.verifier.verify(&target, replacement_source)?;

        self.audit.write(
            "verification_finished",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
                "ok": report.ok,
                "syntax_ok": report.syntax_ok,
                "tests_ok": report.tests_ok,
                "details": report.details,
            }),
        )?;

        Ok((proposal_id, report))
    }

    pub fn verify_and_apply(
        &self,
        target_file: &Path,
        replacement_source: &str,
        proposal_id: Option<&str>,
        expected_source: Option<&str>,
    ) -> CoreResult<RepairReceipt> {
        let target = self.policy.ensure_target_allowed(target_file)?;
        let before = fs::read(&target)?;
        if expected_source.is_some_and(|source| source.as_bytes() != before) {
            return Err(CoreError::Policy("Source changed since review".to_string()));
        }
        let (proposal_id, verification) =
            self.verify(&target, replacement_source, proposal_id)?;

        if !verification.ok {
            return Err(CoreError::Verification(
                "Candidate repair did not pass verification.".to_string(),
            ));
        }

        if fs::read(&target)? != before {
            return Err(CoreError::Policy("Source changed during verification".to_string()));
        }
        let snapshot = self.snapshots.create(&target, &proposal_id)?;

        self.audit.write(
            "snapshot_created",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
                "snapshot_file": snapshot,
            }),
        )?;

        if let Err(err) = replace_text(&target, replacement_source) {
            let restore_result = self.snapshots.restore(&target, &snapshot);

            self.audit.write(
                "patch_apply_failed",
                json!({
                    "proposal_id": proposal_id,
                    "target_file": target,
                    "error": err.to_string(),
                    "rollback_after_apply_failure": restore_result.is_ok(),
                }),
            )?;

            return Err(err);
        }

        if let Err(err) = self.audit.write(
            "patch_applied",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
                "snapshot_file": snapshot,
            }),
        ) {
            self.snapshots.restore(&target, &snapshot)?;
            return Err(err);
        }

        Ok(RepairReceipt {
            proposal_id,
            target_file: target,
            snapshot_file: snapshot,
            verification,
        })
    }

    pub fn rollback(
        &self,
        target_file: &Path,
        snapshot_file: &Path,
        proposal_id: &str,
        reason: &str,
    ) -> CoreResult<()> {
        let target = self.policy.ensure_target_allowed(target_file)?;

        self.snapshots.restore(&target, snapshot_file)?;

        self.audit.write(
            "repair_rolled_back",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
                "snapshot_file": snapshot_file,
                "reason": reason,
            }),
        )?;

        Ok(())
    }

    pub fn commit(
        &self,
        target_file: &Path,
        proposal_id: &str,
    ) -> CoreResult<()> {
        let target = self.policy.ensure_target_allowed(target_file)?;

        self.audit.write(
            "repair_committed",
            json!({
                "proposal_id": proposal_id,
                "target_file": target,
            }),
        )?;

        Ok(())
    }

    pub fn record_failure(
        &self,
        target_file: &Path,
        exception_type: &str,
        message: &str,
        module_name: &str,
        symbol_name: &str,
    ) -> CoreResult<()> {
        let target = self.policy.ensure_target_allowed(target_file)?;

        self.audit.write(
            "failure_detected",
            json!({
                "target_file": target,
                "exception_type": exception_type,
                "message": message,
                "module_name": module_name,
                "symbol_name": symbol_name,
            }),
        )?;

        Ok(())
    }

    pub fn record_repair_event(&self, target_file: &Path, proposal_id: &str,
                               event: &str, details: &str) -> CoreResult<()> {
        let target = self.policy.ensure_target_allowed(target_file)?;
        validate_proposal_id(proposal_id)?;
        if !matches!(event, "repair_reviewed" | "repair_approved" | "repair_rejected") {
            return Err(CoreError::Policy("Unsupported review event".to_string()));
        }
        self.audit.write(event, json!({"proposal_id": proposal_id,
            "target_file": target, "details": details}))
    }
}

fn validate_proposal_id(id: &str) -> CoreResult<()> {
    if id.is_empty() || id.len() > 80 || !id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
        return Err(CoreError::Policy("Invalid proposal ID".to_string()));
    }
    Ok(())
}
