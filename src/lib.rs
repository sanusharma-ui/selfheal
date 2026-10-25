mod audit;
mod engine;
mod error;
mod patch;
mod policy;
mod snapshot;
mod verifier;

use engine::{CoreEngine as NativeCoreEngine, EngineConfig};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::path::PathBuf;

fn py_err<E: std::fmt::Display>(err: E) -> PyErr {
    PyRuntimeError::new_err(err.to_string())
}

#[pyclass]
#[derive(Clone)]
pub struct VerificationReport {
    #[pyo3(get)]
    pub ok: bool,
    #[pyo3(get)]
    pub syntax_ok: bool,
    #[pyo3(get)]
    pub tests_ok: Option<bool>,
    #[pyo3(get)]
    pub details: String,
    #[pyo3(get)]
    pub proposal_id: String,
}

#[pyclass]
#[derive(Clone)]
pub struct RepairReceipt {
    #[pyo3(get)]
    pub proposal_id: String,
    #[pyo3(get)]
    pub target_file: String,
    #[pyo3(get)]
    pub snapshot_file: String,
    #[pyo3(get)]
    pub verification_ok: bool,
    #[pyo3(get)]
    pub verification_details: String,
}

#[pyclass]
pub struct CoreEngine {
    inner: NativeCoreEngine,
}

#[pymethods]
impl CoreEngine {
    #[new]
    #[pyo3(signature = (
        project_root,
        python_executable,
        run_tests=true,
        snapshot_dir=None,
        audit_file=None
    ))]
    fn new(
        project_root: String,
        python_executable: String,
        run_tests: bool,
        snapshot_dir: Option<String>,
        audit_file: Option<String>,
    ) -> PyResult<Self> {
        if project_root.trim().is_empty() {
            return Err(PyValueError::new_err("project_root cannot be empty"));
        }

        if python_executable.trim().is_empty() {
            return Err(PyValueError::new_err(
                "python_executable cannot be empty",
            ));
        }

        let config = EngineConfig {
            project_root: PathBuf::from(project_root),
            python_executable,
            run_tests,
            snapshot_dir: snapshot_dir
                .unwrap_or_else(|| ".selfheal/snapshots".to_string()),
            audit_file: audit_file
                .unwrap_or_else(|| ".selfheal/audit.jsonl".to_string()),
        };

        let inner = NativeCoreEngine::new(config).map_err(py_err)?;

        Ok(Self { inner })
    }

    #[pyo3(signature = (target_file, replacement_source, proposal_id=None, timeout_seconds=None))]
    fn verify(
        &self,
        py: Python<'_>,
        target_file: String,
        replacement_source: String,
        proposal_id: Option<String>,
        timeout_seconds: Option<f64>,
    ) -> PyResult<VerificationReport> {
        let timeout = verification_timeout(timeout_seconds)?;
        let (id, report) = py.detach(|| {
            self.inner.verify_with_timeout(
                &PathBuf::from(target_file),
                &replacement_source,
                proposal_id.as_deref(),
                timeout,
            )
        }).map_err(py_err)?;

        Ok(VerificationReport {
            ok: report.ok,
            syntax_ok: report.syntax_ok,
            tests_ok: report.tests_ok,
            details: report.details,
            proposal_id: id,
        })
    }

    #[pyo3(signature = (target_file, replacement_source, proposal_id=None, expected_source=None, timeout_seconds=None))]
    fn verify_and_apply(
        &self,
        py: Python<'_>,
        target_file: String,
        replacement_source: String,
        proposal_id: Option<String>,
        expected_source: Option<String>,
        timeout_seconds: Option<f64>,
    ) -> PyResult<RepairReceipt> {
        let timeout = verification_timeout(timeout_seconds)?;
        let receipt = py.detach(|| {
            self.inner.verify_and_apply_with_timeout(
                &PathBuf::from(target_file),
                &replacement_source,
                proposal_id.as_deref(),
                expected_source.as_deref(),
                timeout,
            )
        }).map_err(py_err)?;

        Ok(RepairReceipt {
            proposal_id: receipt.proposal_id,
            target_file: receipt.target_file.display().to_string(),
            snapshot_file: receipt.snapshot_file.display().to_string(),
            verification_ok: receipt.verification.ok,
            verification_details: receipt.verification.details,
        })
    }

    fn rollback(
        &self,
        target_file: String,
        snapshot_file: String,
        proposal_id: String,
        reason: String,
    ) -> PyResult<()> {
        self.inner
            .rollback(
                &PathBuf::from(target_file),
                &PathBuf::from(snapshot_file),
                &proposal_id,
                &reason,
            )
            .map_err(py_err)
    }

    fn commit(
        &self,
        target_file: String,
        proposal_id: String,
    ) -> PyResult<()> {
        self.inner
            .commit(&PathBuf::from(target_file), &proposal_id)
            .map_err(py_err)
    }

    fn record_failure(
        &self,
        target_file: String,
        exception_type: String,
        message: String,
        module_name: String,
        symbol_name: String,
    ) -> PyResult<()> {
        self.inner
            .record_failure(
                &PathBuf::from(target_file),
                &exception_type,
                &message,
                &module_name,
                &symbol_name,
            )
            .map_err(py_err)
    }

    fn record_repair_event(&self, target_file: String, proposal_id: String,
                           event: String, details: String) -> PyResult<()> {
        self.inner.record_repair_event(&PathBuf::from(target_file), &proposal_id,
                                       &event, &details).map_err(py_err)
    }
}

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_API_VERSION", 1)?;
    module.add("PACKAGE_VERSION", env!("CARGO_PKG_VERSION"))?;
    module.add_class::<CoreEngine>()?;
    module.add_class::<VerificationReport>()?;
    module.add_class::<RepairReceipt>()?;
    Ok(())
}

fn verification_timeout(value: Option<f64>) -> PyResult<std::time::Duration> {
    let seconds = value.unwrap_or(120.0);
    if !seconds.is_finite() || seconds <= 0.0 || seconds > 3600.0 {
        return Err(PyValueError::new_err("timeout_seconds must be finite, positive and at most 3600"));
    }
    Ok(std::time::Duration::from_secs_f64(seconds))
}
