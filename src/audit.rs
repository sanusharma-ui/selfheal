use crate::error::CoreResult;
use serde::Serialize;
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct AuditLogger {
    file: PathBuf,
}

#[derive(Debug, Serialize)]
struct AuditRecord<'a> {
    timestamp_unix_ms: u128,
    event: &'a str,
    data: Value,
}

impl AuditLogger {
    pub fn new(project_root: &Path, relative_file: &str) -> Self {
        Self {
            file: project_root.join(relative_file),
        }
    }

    pub fn write(&self, event: &str, data: Value) -> CoreResult<()> {
        if let Some(parent) = self.file.parent() {
            fs::create_dir_all(parent)?;
        }

        let timestamp_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();

        let record = AuditRecord {
            timestamp_unix_ms,
            event,
            data,
        };

        let mut handle = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.file)?;

        serde_json::to_writer(&mut handle, &record)?;
        handle.write_all(b"\n")?;
        handle.flush()?;

        Ok(())
    }
}
