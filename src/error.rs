use std::fmt::{Display, Formatter};
use std::io;

#[derive(Debug)]
pub enum CoreError {
    Io(io::Error),
    Policy(String),
    Verification(String),
    Process(String),
    InvalidPath(String),
    Serialization(serde_json::Error),
}

impl Display for CoreError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(err) => write!(f, "I/O error: {err}"),
            Self::Policy(msg) => write!(f, "Policy error: {msg}"),
            Self::Verification(msg) => write!(f, "Verification error: {msg}"),
            Self::Process(msg) => write!(f, "Process error: {msg}"),
            Self::InvalidPath(msg) => write!(f, "Invalid path: {msg}"),
            Self::Serialization(err) => write!(f, "Serialization error: {err}"),
        }
    }
}

impl std::error::Error for CoreError {}

impl From<io::Error> for CoreError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<serde_json::Error> for CoreError {
    fn from(value: serde_json::Error) -> Self {
        Self::Serialization(value)
    }
}

pub type CoreResult<T> = Result<T, CoreError>;
