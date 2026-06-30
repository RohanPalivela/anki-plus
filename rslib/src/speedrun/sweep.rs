// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Coverage sweep: re-activate a spread of suspended cards across all blueprint
//! topics to fight info-tunneling / coverage holes (D-2c).

use std::collections::HashMap;
use std::collections::HashSet;

use crate::prelude::*;
use crate::search::FieldSearchMode;
use crate::search::JoinSearches;
use crate::search::SearchNode;
use crate::search::StateKind;
use crate::speedrun::TOPIC_TAG_PREFIX;

impl Collection {
    /// Re-activate a cross-topic spread of suspended cards. `sample_size == 0`
    /// (the proto3 default) means "use the configured default", never "sweep
    /// nothing"; the effective value is clamped to a minimum of 1.
    ///
    /// Atomic + undo-safe (single `Op::CoverageSweep` entry).
    pub(crate) fn run_coverage_sweep(&mut self, sample_size: u32) -> Result<OpOutput<Vec<CardId>>> {
        let per_topic = if sample_size == 0 {
            self.speedrun_sweep_default_sample_size()
        } else {
            sample_size
        }
        .max(1) as usize;

        self.transact(Op::CoverageSweep, |col| {
            let by_topic = col.suspended_cards_grouped_by_topic()?;
            let picks = stratified_sample(&by_topic, per_topic);
            let cards = col.all_cards_for_ids(&picks, false)?;
            col.unsuspend_or_unbury_searched_cards(cards)?;
            Ok(picks)
        })
    }

    /// topic name -> sorted, de-duplicated suspended card ids carrying that
    /// `topic::` tag.
    fn suspended_cards_grouped_by_topic(&mut self) -> Result<HashMap<String, Vec<CardId>>> {
        let search = SearchNode::Tag {
            tag: format!("{TOPIC_TAG_PREFIX}*"),
            mode: FieldSearchMode::Normal,
        }
        .and(StateKind::Suspended);
        let cards = self.all_cards_for_search(search)?;
        let note_ids: Vec<NoteId> = cards
            .iter()
            .map(|c| c.note_id)
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        let note_topics = self.note_topic_map(&note_ids)?;

        let mut by_topic: HashMap<String, Vec<CardId>> = HashMap::new();
        for card in &cards {
            if let Some(topics) = note_topics.get(&card.note_id) {
                for topic in topics {
                    by_topic.entry(topic.clone()).or_default().push(card.id);
                }
            }
        }
        for cids in by_topic.values_mut() {
            cids.sort_unstable();
            cids.dedup();
        }
        Ok(by_topic)
    }
}

/// Round-robin across topics, taking up to `per_topic` distinct, not-yet-picked
/// cards from each topic so no single topic dominates (AC-5). A card shared by
/// several topics is activated at most once. Deterministic ordering.
fn stratified_sample(by_topic: &HashMap<String, Vec<CardId>>, per_topic: usize) -> Vec<CardId> {
    let mut topics: Vec<&String> = by_topic.keys().collect();
    topics.sort();

    let mut picked: Vec<CardId> = Vec::new();
    let mut seen: HashSet<CardId> = HashSet::new();
    let mut taken_per_topic: HashMap<&String, usize> = HashMap::new();

    for _round in 0..per_topic {
        let mut progressed = false;
        for topic in &topics {
            if taken_per_topic.get(topic).copied().unwrap_or(0) >= per_topic {
                continue;
            }
            if let Some(&cid) = by_topic[*topic].iter().find(|c| !seen.contains(c)) {
                seen.insert(cid);
                picked.push(cid);
                *taken_per_topic.entry(topic).or_default() += 1;
                progressed = true;
            }
        }
        if !progressed {
            break;
        }
    }
    picked
}

#[cfg(test)]
mod test {
    use std::collections::HashSet;

    use crate::prelude::*;
    use crate::speedrun::mastery::topics_from_tag_string;
    use crate::speedrun::test_helpers::*;

    /// AC-5: a sweep re-activates a spread across topics, not a single-topic
    /// concentration, and is a no-op-free unsuspension.
    #[test]
    fn sweep_spreads_across_topics() {
        let mut col = Collection::new();
        let topics = ["topic::a", "topic::b", "topic::c", "topic::d"];
        for topic in topics {
            let note = add_note_with_tags(&mut col, &[topic]);
            let cids = col.storage.card_ids_of_notes(&[note.id]).unwrap();
            suspend(&mut col, &cids);
        }

        let out = col.run_coverage_sweep(2).unwrap();
        let activated = out.output;
        assert!(!activated.is_empty(), "sweep should activate cards");

        // Every topic is represented in the reactivated set.
        let mut distinct = HashSet::new();
        for &cid in &activated {
            let card = col.storage.get_card(cid).unwrap().unwrap();
            let note_tags = col
                .storage
                .get_note_tags_by_id(card.note_id)
                .unwrap()
                .unwrap();
            for topic in topics_from_tag_string(&note_tags.tags) {
                distinct.insert(topic);
            }
        }
        assert_eq!(
            distinct.len(),
            topics.len(),
            "sweep should touch all topics"
        );
        assert!(activated.iter().all(|&c| !is_suspended(&mut col, c)));
    }

    /// `sample_size == 0` must use the configured default (≥1), never "sweep
    /// nothing".
    #[test]
    fn sweep_zero_sample_uses_default() {
        let mut col = Collection::new();
        let note = add_note_with_tags(&mut col, &["topic::a"]);
        let cids = col.storage.card_ids_of_notes(&[note.id]).unwrap();
        suspend(&mut col, &cids);

        let out = col.run_coverage_sweep(0).unwrap();
        assert!(
            !out.output.is_empty(),
            "sample_size 0 must fall back to the default, not sweep nothing"
        );
    }
}
