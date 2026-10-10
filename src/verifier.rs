use crate::error::{CoreError, CoreResult};
use crate::policy::PolicyEngine;
use crate::patch::replace_text;
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};
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
        self.verify_with_timeout(target, replacement_source, Duration::from_secs(120))
    }

    pub fn verify_with_timeout(&self, target: &Path, replacement_source: &str,
                               timeout: Duration) -> CoreResult<VerificationReport> {
        let deadline = Instant::now().checked_add(timeout)
            .ok_or_else(|| CoreError::Process("Invalid verification deadline".into()))?;
        let relative_target = self.policy.relative_target(target)?;
        let temp_dir = tempfile::tempdir()?;
        let workspace = temp_dir.path().join("project");

        copy_project(self.policy.project_root(), &workspace, deadline)?;

        let candidate = workspace.join(relative_target);

        if let Some(parent) = candidate.parent() {
            fs::create_dir_all(parent)?;
        }

        remaining(deadline)?;
        replace_text(&candidate, replacement_source)?;

        let mut syntax_command = Command::new(&self.config.python_executable);
        syntax_command.arg("-c").arg(include_str!("../python/selfheal/_process_tree.py"))
            .arg("-m").arg("py_compile").arg(&candidate);
        let syntax_output = run_check_until(syntax_command, &workspace, "Python syntax check", deadline)?;

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

        let mut test_command = Command::new(&self.config.python_executable);
        test_command.arg("-c").arg(include_str!("../python/selfheal/_process_tree.py"))
            .arg("-m").arg("pytest").arg("-q");
        let test_output = run_check_until(test_command, &workspace, "pytest", deadline)?;

        let tests_ok = test_output.status.success();

        Ok(VerificationReport {
            ok: syntax_ok && tests_ok,
            syntax_ok,
            tests_ok: Some(tests_ok),
            details: output_details("pytest", &test_output),
        })
    }
}

// Redirect to files so full pipes cannot deadlock the polling loop. Captured
// diagnostics are bounded; execution uses host permissions, not a sandbox.
#[cfg(test)]
fn run_check(command: Command, workspace: &Path, label: &str, timeout: Duration) -> CoreResult<Output> {
    run_check_until(command, workspace, label, Instant::now() + timeout)
}

fn remaining(deadline: Instant) -> CoreResult<Duration> {
    let left = deadline.saturating_duration_since(Instant::now());
    if left.is_zero() {
        return Err(CoreError::Process("Repair verification deadline exceeded; candidate rejected".into()));
    }
    Ok(left)
}

fn stop_tree(child: &mut Child) -> CoreResult<()> {
    #[cfg(unix)]
    {
        // Each verification leader owns a fresh process group; kill descendants
        // even if the leader just exited. No shell commands or arbitrary PIDs.
        unsafe extern "C" { fn kill(pid: i32, signal: i32) -> i32; }
        let result = unsafe { kill(-(child.id() as i32), 9) };
        if result != 0 && std::io::Error::last_os_error().raw_os_error() != Some(3) {
            return Err(CoreError::Process("Could not stop verification process group".into()));
        }
    }
    #[cfg(not(unix))]
    if child.try_wait()?.is_none() {
        // The Python bootstrap establishes a kill-on-close Windows Job before
        // launching py_compile/pytest. Terminating it stops the entire job.
        child.kill()?;
    }
    child.wait()?;
    Ok(())
}

fn run_check_until(mut command: Command, workspace: &Path, label: &str, deadline: Instant) -> CoreResult<Output> {
    remaining(deadline)?;
    let mut stdout = tempfile::tempfile()?;
    let mut stderr = tempfile::tempfile()?;
    let import_paths = std::env::join_paths([
        workspace.to_path_buf(), workspace.join("src"), workspace.join("python"),
    ]).map_err(|err| CoreError::Process(err.to_string()))?;
    command.current_dir(workspace)
        .env("PYTHONPATH", import_paths)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout.try_clone()?))
        .stderr(Stdio::from(stderr.try_clone()?));
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|err| {
        CoreError::Process(format!("Could not start {label}: {err}"))
    })?;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {},
            Err(err) => {
                stop_tree(&mut child)?;
                return Err(CoreError::Process(format!("Could not wait for {label}: {err}")));
            }
        }
        if Instant::now() >= deadline {
            stop_tree(&mut child)?;
            return Err(CoreError::Process(format!("{label} exceeded the verification deadline; candidate rejected")));
        }
        thread::sleep(Duration::from_millis(25));
    };
    stop_tree(&mut child)?;
    remaining(deadline)?;
    stdout.seek(SeekFrom::Start(0))?;
    stderr.seek(SeekFrom::Start(0))?;
    let mut out = Vec::new();
    let mut err = Vec::new();
    stdout.take(64_000).read_to_end(&mut out)?;
    stderr.take(64_000).read_to_end(&mut err)?;
    Ok(Output { status, stdout: out, stderr: err })
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

fn copy_project(source: &Path, destination: &Path, deadline: Instant) -> CoreResult<()> {
    fs::create_dir_all(destination)?;

    for entry in WalkDir::new(source).into_iter().filter_entry(|entry| {
        if entry.path() == source {
            return true;
        }

        let name = entry.file_name().to_string_lossy();
        !ignored_component(&name) && name != ".env" && !name.starts_with(".env.")
    }) {
        remaining(deadline)?;
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

    #[test]
    fn verification_child_is_stopped_at_deadline() {
        let dir = tempfile::tempdir().unwrap();
        let python = std::env::var("PYO3_PYTHON").unwrap_or_else(|_| "python".into());
        let mut command = Command::new(python);
        command.arg("-c").arg("import time; time.sleep(5)");
        let result = run_check(command, dir.path(), "deadline test", Duration::from_millis(100));
        assert!(matches!(result, Err(CoreError::Process(message)) if message.contains("verification deadline")));
    }

    #[test]
    fn verification_prefers_copied_project_imports() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("src")).unwrap();
        fs::write(dir.path().join("src/copied_app.py"), "VALUE = 'copied candidate'\n").unwrap();
        let python = std::env::var("PYO3_PYTHON").unwrap_or_else(|_| "python".into());
        let mut command = Command::new(python);
        command.arg("-c").arg("from copied_app import VALUE; print(VALUE)");
        let output = run_check(command, dir.path(), "import test", Duration::from_secs(10)).unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8_lossy(&output.stdout).contains("copied candidate"));
    }
}
