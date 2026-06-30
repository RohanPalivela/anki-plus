// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! `SpeedrunService` RPC implementations (Rust, so Android shares them — D-12).

use std::collections::BTreeSet;

use anki_proto::speedrun;

use crate::prelude::*;
use crate::speedrun::MissReason;

/// D-6 abstention: minimum graded responses before a Memory score is emitted.
const MEMORY_MIN_GRADED: usize = 30;
/// D-6 abstention: minimum blueprint-topic coverage before emitting a score.
const MEMORY_MIN_COVERAGE: f32 = 0.60;
/// Two-sided 80% normal quantile, for the Memory uncertainty interval.
const Z_80: f32 = 1.2816;

impl crate::services::SpeedrunService for Collection {
    fn activate_cards_for_miss(
        &mut self,
        input: speedrun::ActivateCardsForMissRequest,
    ) -> Result<speedrun::ActivateCardsResponse> {
        let reason: MissReason = input.miss_reason().into();
        let out = self.activate_cards_for_miss(NoteId(input.question_note_id), reason)?;
        Ok(activate_response(out))
    }

    fn run_coverage_sweep(
        &mut self,
        input: speedrun::RunCoverageSweepRequest,
    ) -> Result<speedrun::ActivateCardsResponse> {
        let out = self.run_coverage_sweep(input.sample_size)?;
        Ok(activate_response(out))
    }

    fn get_memory_score(
        &mut self,
        _input: speedrun::GetMemoryScoreRequest,
    ) -> Result<speedrun::MemoryScoreResponse> {
        self.speedrun_memory_score()
    }
}

impl Collection {
    /// Compute the Memory score: per-topic mastery + an overall point estimate,
    /// an 80% uncertainty interval, coverage, and an explicit abstention flag
    /// (D-6). Read-only.
    pub(crate) fn speedrun_memory_score(&mut self) -> Result<speedrun::MemoryScoreResponse> {
        let data = self.compute_topic_mastery()?;
        let blueprint = self.get_speedrun_blueprint();

        // Report every blueprint topic (so not-yet-activated topics read as
        // unknown/low) plus any extra topics that have data.
        let mut topic_names: BTreeSet<String> =
            blueprint.topics.iter().map(|t| t.name.clone()).collect();
        topic_names.extend(data.by_topic.keys().cloned());
        let topics = topic_names
            .into_iter()
            .map(|name| {
                let entry = data.by_topic.get(&name);
                speedrun::TopicMastery {
                    topic: name,
                    mastery: entry.map(|m| m.mastery).unwrap_or(0.0),
                    card_count: entry.map(|m| m.card_count as u32).unwrap_or(0),
                    known: entry.map(|m| m.card_count > 0).unwrap_or(false),
                }
            })
            .collect();

        let coverage = if blueprint.topics.is_empty() {
            // No blueprint: coverage is meaningless, treat as full if any data.
            if data.graded_count > 0 {
                1.0
            } else {
                0.0
            }
        } else {
            let with_data = blueprint
                .topics
                .iter()
                .filter(|t| {
                    data.by_topic
                        .get(&t.name)
                        .map(|m| m.card_count > 0)
                        .unwrap_or(false)
                })
                .count();
            with_data as f32 / blueprint.topics.len() as f32
        };

        let (range_low, range_high) = memory_interval(data.overall, data.graded_count);
        let abstained = data.graded_count < MEMORY_MIN_GRADED || coverage < MEMORY_MIN_COVERAGE;

        Ok(speedrun::MemoryScoreResponse {
            topics,
            overall: data.overall,
            range_low,
            range_high,
            coverage,
            graded_count: data.graded_count as u32,
            abstained,
        })
    }
}

fn activate_response(out: OpOutput<Vec<CardId>>) -> speedrun::ActivateCardsResponse {
    speedrun::ActivateCardsResponse {
        changes: Some(out.changes.into()),
        activated_card_ids: out.output.into_iter().map(|c| c.0).collect(),
    }
}

/// 80% interval for the overall mastery proportion via a normal approximation;
/// widens as the sample shrinks, and is maximally wide (`0..1`) with no data.
fn memory_interval(p: f32, n: usize) -> (f32, f32) {
    if n == 0 {
        return (0.0, 1.0);
    }
    let se = (p * (1.0 - p) / n as f32).sqrt();
    let half = Z_80 * se;
    ((p - half).clamp(0.0, 1.0), (p + half).clamp(0.0, 1.0))
}
