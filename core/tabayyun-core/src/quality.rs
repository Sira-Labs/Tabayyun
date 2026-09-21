//! Normalised sample quality. Sources (OPC UA StatusCode, PI, Canary, meter A/E flags, AEMO
//! substitution types, PMU STAT words) are mapped to this four-state model at ingest.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Quality {
    #[default]
    Good,
    Uncertain,
    Bad,
    /// Value was estimated/substituted by the source (metering VEE, historian fill).
    Estimated,
}

impl Quality {
    /// Map an OPC UA / OPC DA style status code. OPC UA: top two bits 00 = Good,
    /// 01 = Uncertain, 10 = Bad. OPC DA classic uses bits 7-6 of an 8-bit byte with the same
    /// meaning; callers pass the DA byte shifted into the UA position or use `from_opc_da`.
    pub fn from_opc_ua(status: u32) -> Self {
        match status >> 30 {
            0 => Quality::Good,
            1 => Quality::Uncertain,
            _ => Quality::Bad,
        }
    }

    pub fn from_opc_da(quality_byte: u8) -> Self {
        match (quality_byte >> 6) & 0b11 {
            0b11 => Quality::Good,
            0b01 => Quality::Uncertain,
            _ => Quality::Bad,
        }
    }

    /// Parse common textual flags: `good`, `bad`, `uncertain`, `estimated`, metering `A`
    /// (actual) / `E` (estimated), `substituted`.
    pub fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "good" | "g" | "a" | "actual" | "ok" | "0" | "" => Some(Quality::Good),
            "uncertain" | "u" | "questionable" | "1" => Some(Quality::Uncertain),
            "bad" | "b" | "invalid" | "2" => Some(Quality::Bad),
            "estimated" | "e" | "substituted" | "s" | "3" => Some(Quality::Estimated),
            _ => None,
        }
    }

    pub fn as_u8(self) -> u8 {
        match self {
            Quality::Good => 0,
            Quality::Uncertain => 1,
            Quality::Bad => 2,
            Quality::Estimated => 3,
        }
    }

    pub fn from_u8(v: u8) -> Self {
        match v {
            1 => Quality::Uncertain,
            2 => Quality::Bad,
            3 => Quality::Estimated,
            _ => Quality::Good,
        }
    }

    pub fn is_usable(self) -> bool {
        matches!(self, Quality::Good | Quality::Estimated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opc_mapping() {
        assert_eq!(Quality::from_opc_ua(0), Quality::Good);
        assert_eq!(Quality::from_opc_ua(0x4000_0000), Quality::Uncertain);
        assert_eq!(Quality::from_opc_ua(0x8000_0000), Quality::Bad);
        assert_eq!(Quality::from_opc_da(0xC0), Quality::Good);
        assert_eq!(Quality::from_opc_da(0x40), Quality::Uncertain);
        assert_eq!(Quality::from_opc_da(0x00), Quality::Bad);
        assert_eq!(Quality::parse("E"), Some(Quality::Estimated));
    }
}
