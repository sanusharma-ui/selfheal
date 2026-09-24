use crate::error::{CoreError, CoreResult};
use crate::policy::PolicyEngine;
use crate::patch::replace_text;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use walkdir::WalkDir;

#[derive(Debug, Clone)]
pub struct VerificationReport {
    pub ok: bool,
    pub syntax_ok: bool,
    pub tests_ok: Option<bool>,
    pub details: String,
}

#[derive(Debug, Clone)]
pub struct VerificationConfig {
    pub python_executable: String,
    pub run_tests: bool,
}

#[derive(Debug, Clone)]
pub struct Verifier {
    policy: PolicyEngine,
    config: VerificationConfig,
}

impl Verifier {
    pub fn new(policy: PolicyEngine, config: VerificationConfig) -> Self {
        Self { policy, config }
    }

    pub fn verify(
        &self,
        target: &Path,
        replacement_source: &str,
    ) -> CoreResult<VerificationReport> {
        let relative_target = self.policy.relative_target(target)?;
        let temp_dir = tempfile::tempdir()?;
        let workspace = temp_dir.path().join("project");

        copy_project(self.policy.project_root(), &workspace)?;

        let candidate = workspace.join(relative_target);

        if let Some(parent) = candidate.parent() {
            fs::create_dir_all(parent)?;
        }

        replace_text(&candidate, replacement_source)?;

        let syntax_output = Command::new(&self.config.python_executable)
            .arg("-m")
            .arg("py_compile")
            .arg(&candidate)
            .current_dir(&workspace)
            .output()
            .map_err(|err| {
                CoreError::Process(format!(
                    "Could not start Python syntax check using '{}': {err}",
                    self.config.python_executable
                ))
            })?;

        let syntax_ok = syntax_output.status.success();

        if !syntax_ok {
            return Ok(VerificationReport {
                ok: false,
                syntax_ok: false,
                tests_ok: None,
                details: output_details("py_compile", &syntax_output),
            });
        }

        if !self.config.run_tests {
            return Ok(VerificationReport {
                ok: true,
                syntax_ok: true,
                tests_ok: None,
                details: "Python syntax verification passed. Project tests disabled."
                    .to_string(),
            });
        }

        let test_output = Command::new(&self.config.python_executable)
            .arg("-m")
            .arg("pytest")
            .arg("-q")
            .current_dir(&workspace)
            .output()
            .map_err(|err| {
                CoreError::Process(format!(
                    "Could not start pytest using '{}': {err}",
                    self.config.python_executable
                ))
            })?;

        let tests_ok = test_output.status.success();

        Ok(VerificationReport {
            ok: syntax_ok && tests_ok,
            syntax_ok,
            tests_ok: Some(tests_ok),
            details: output_details("pytest", &test_output),
        })
    }
}

fn output_details(label: &str, output: &Output) -> String {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    format!(
        "{label} exit={}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        stdout.trim(),
        stderr.trim()
    )
}

fn ignored_component(name: &str) -> bool {
    matches!(
        name,
        ".git"
            | ".venv"
            | "venv"
            | "__pycache__"
            | ".pytest_cache"
            | ".selfheal"
            | "target"
            | "dist"
            | "build"
    )
}

fn copy_project(source: &Path, destination: &Path) -> CoreResult<()> {
    fs::create_dir_all(destination)?;

    for entry in WalkDir::new(source).into_iter().filter_entry(|entry| {
        if entry.path() == source {
            return true;
        }

        let name = entry.file_name().to_string_lossy();
        !ignored_component(&name)
    }) {
        let entry = entry.map_err(|err| {
            CoreError::Io(std::io::Error::new(
                std::io::ErrorKind::Other,
                err.to_string(),
            ))
        })?;

        let relative = entry.path().strip_prefix(source).map_err(|_| {
            CoreError::InvalidPath(format!(
                "Could not create relative path for {}",
                entry.path().display()
            ))
        })?;

        let target = destination.join(relative);

        if entry.file_type().is_dir() {
            fs::create_dir_all(&target)?;
        } else if entry.file_type().is_file() {
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::copy(entry.path(), &target)?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ignored_dirs_are_recognized() {
        assert!(ignored_component(".git"));
        assert!(ignored_component(".selfheal"));
        assert!(!ignored_component("src"));
    }
}
