use crate::error::{CoreError, CoreResult};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct PolicyEngine {
    project_root: PathBuf,
}

impl PolicyEngine {
    pub fn new(project_root: PathBuf) -> CoreResult<Self> {
        let canonical = project_root.canonicalize().map_err(|err| {
            CoreError::InvalidPath(format!(
                "Cannot canonicalize project root {}: {err}",
                project_root.display()
            ))
        })?;

        Ok(Self {
            project_root: canonical,
        })
    }

    pub fn project_root(&self) -> &Path {
        &self.project_root
    }

    pub fn ensure_target_allowed(&self, target: &Path) -> CoreResult<PathBuf> {
        let canonical = target.canonicalize().map_err(|err| {
            CoreError::InvalidPath(format!(
                "Cannot canonicalize target {}: {err}",
                target.display()
            ))
        })?;

        if !canonical.starts_with(&self.project_root) {
            return Err(CoreError::Policy(format!(
                "Target {} is outside project root {}",
                canonical.display(),
                self.project_root.display()
            )));
        }

        if !canonical.is_file() {
            return Err(CoreError::Policy(format!(
                "Target {} is not a regular file",
                canonical.display()
            )));
        }

        Ok(canonical)
    }

    pub fn relative_target(&self, target: &Path) -> CoreResult<PathBuf> {
        let canonical = self.ensure_target_allowed(target)?;

        canonical
            .strip_prefix(&self.project_root)
            .map(Path::to_path_buf)
            .map_err(|_| {
                CoreError::Policy(format!(
                    "Could not derive project-relative path for {}",
                    canonical.display()
                ))
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn allows_file_inside_root() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("app.py");
        fs::write(&target, "x = 1\n").unwrap();

        let policy = PolicyEngine::new(dir.path().to_path_buf()).unwrap();
        let allowed = policy.ensure_target_allowed(&target).unwrap();

        assert_eq!(allowed, target.canonicalize().unwrap());
    }
}
