use crate::error::{CoreError, CoreResult};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use uuid::Uuid;

fn sibling_temp_path(target: &Path) -> CoreResult<PathBuf> {
    let parent = target.parent().ok_or_else(|| {
        CoreError::InvalidPath(format!(
            "Target has no parent directory: {}",
            target.display()
        ))
    })?;

    let file_name = target
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            CoreError::InvalidPath(format!(
                "Target has invalid file name: {}",
                target.display()
            ))
        })?;

    Ok(parent.join(format!(
        ".{file_name}.{}.selfheal.tmp",
        Uuid::new_v4()
    )))
}

pub fn replace_text(target: &Path, replacement: &str) -> CoreResult<()> {
    let temp_path = sibling_temp_path(target)?;
    let result = (|| -> CoreResult<()> {
        let mut handle = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temp_path)?;

        handle.write_all(replacement.as_bytes())?;
        handle.flush()?;
        handle.sync_all()?;
        if let Ok(metadata) = fs::metadata(target) {
            fs::set_permissions(&temp_path, metadata.permissions())?;
        }
        drop(handle);
        // Rename with replacement; never remove the original first.
        fs::rename(&temp_path, target)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp_path);
    }
    result
}
