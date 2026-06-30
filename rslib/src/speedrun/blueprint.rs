// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! MCAT blueprint config: the topic list + per-topic weights (D-3 / D-17).
//!
//! Stored natively as collection config (JSON) so it syncs to Android for free,
//! and is the single source of truth for the `topic_weight` used by value
//! ordering, coverage, and (later) readiness.

use std::collections::HashMap;

use serde::Deserialize;
use serde::Serialize;

use crate::prelude::*;

/// Config key holding the MCAT blueprint JSON. camelCase to match the existing
/// config-key naming convention.
pub(crate) const BLUEPRINT_CONFIG_KEY: &str = "speedrunBlueprint";

/// Config key for the default coverage-sweep sample size (cards/topic, D-2c).
pub(crate) const SWEEP_SAMPLE_SIZE_CONFIG_KEY: &str = "speedrunSweepSampleSize";

/// Fallback sweep sample size when none is configured (D-2c: ~1–2 cards/topic).
pub(crate) const DEFAULT_SWEEP_SAMPLE_SIZE: u32 = 1;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Blueprint {
    #[serde(default)]
    pub topics: Vec<BlueprintTopic>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlueprintTopic {
    /// Topic name == the suffix after the `topic::` tag prefix (e.g.
    /// `biochemistry` for the tag `topic::biochemistry`).
    pub name: String,
    /// Relative exam weight. Any positive scale; only relative magnitudes
    /// matter for ordering.
    pub weight: f32,
}

impl Blueprint {
    /// topic name -> weight.
    pub(crate) fn topic_weight_map(&self) -> HashMap<String, f32> {
        self.topics
            .iter()
            .map(|t| (t.name.clone(), t.weight))
            .collect()
    }
}

impl Collection {
    /// The configured blueprint, or an empty blueprint if none is set.
    pub(crate) fn get_speedrun_blueprint(&self) -> Blueprint {
        self.get_config_optional(BLUEPRINT_CONFIG_KEY)
            .unwrap_or_default()
    }

    /// Persist the blueprint (undoable, syncs as config).
    pub fn set_speedrun_blueprint(&mut self, blueprint: &Blueprint) -> Result<OpOutput<()>> {
        self.set_config_json(BLUEPRINT_CONFIG_KEY, blueprint, true)
    }

    /// Configured default cards/topic for a sweep, falling back to the
    /// constant.
    pub(crate) fn speedrun_sweep_default_sample_size(&self) -> u32 {
        self.get_config_optional(SWEEP_SAMPLE_SIZE_CONFIG_KEY)
            .unwrap_or(DEFAULT_SWEEP_SAMPLE_SIZE)
    }
}
