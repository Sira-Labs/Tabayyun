use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("invalid frame: {0}")]
    InvalidFrame(String),
    #[error("unknown check id: {0}")]
    UnknownCheck(String),
    #[error("invalid parameters for {check}: {reason}")]
    InvalidParams { check: String, reason: String },
    #[error("check {check} requires metadata `{field}` which is missing")]
    MissingMetadata { check: String, field: &'static str },
    #[error("arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),
    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("cache error: {0}")]
    Cache(String),
    #[error("invalid group {group}: {reason}")]
    InvalidGroup { group: String, reason: String },
}

pub type Result<T> = std::result::Result<T, Error>;
