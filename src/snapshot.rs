use crate::error::{CoreError, CoreResult};
use crate::patch::replace_text;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct SnapshotManager {
    project_root: PathBuf,
    snapshot_root: PathBuf,
}

impl SnapshotManager {
    pub fn new(project_root: PathBuf, snapshot_dir: &str) -> Self {
        let snapshot_root = project_root.join(snapshot_dir);

        Self {
            project_root,
            snapshot_root,
        }
    }

    pub fn create(&self, target: &Path, proposal_id: &str) -> CoreResult<PathBuf> {
        let relative = target.strip_prefix(&self.project_root).map_err(|_| {
            CoreError::InvalidPath(format!(
                "Target {} is outside project root {}",
                target.display(),
                self.project_root.display()
            ))
        })?;

        let destination = self.snapshot_root.join(proposal_id).join(relative);

        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }

        // Reused IDs must not overwrite the original recovery point.
        let mut output = fs::OpenOptions::new().write(true).create_new(true).open(&destination)?;
        output.write_all(&fs::read(target)?)?;
        output.sync_all()?;
        Ok(destination)
    }

    pub fn restore(&self, target: &Path, snapshot: &Path) -> CoreResult<()> {
        if !snapshot.exists() {
            return Err(CoreError::InvalidPath(format!(
                "Snapshot does not exist: {}",
                snapshot.display()
            )));
        }

        let canonical_snapshot = snapshot.canonicalize()?;
        let canonical_snapshot_root = self.snapshot_root.canonicalize().map_err(|err| {
            CoreError::InvalidPath(format!(
                "Snapshot root is unavailable: {} ({err})",
                self.snapshot_root.display()
            ))
        })?;

        if !canonical_snapshot.starts_with(&canonical_snapshot_root) {
            return Err(CoreError::Policy(format!(
                "Rollback source {} is outside SelfHeal snapshot storage {}",
                canonical_snapshot.display(),
                canonical_snapshot_root.display()
            )));
        }

        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }

        let original = fs::read_to_string(canonical_snapshot)?;
        replace_text(target, &original)?;
        Ok(())
    }
}
