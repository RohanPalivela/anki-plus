// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Shared per-topic mastery / weakness map (DAG task T3a).
//!
//! Reuses the exact read-only FSRS retrievability call used by the stats graphs
//! (`current_retrievability_seconds`, gated on `card.memory_state`) and
//! aggregates it to a stability-weighted per-topic mastery over **activated**
//! (non-suspended) cards carrying each `topic::` tag.
//!
//! This is consumed by BOTH value ordering (via
//! [`Collection::topic_weakness_map`]) and the Memory model — it is created
//! once here, never duplicated.

use std::collections::HashMap;

use fsrs::FSRS;
use fsrs::FSRS5_DEFAULT_DECAY;
use itertools::Itertools;

use crate::prelude::*;
use crate::search::FieldSearchMode;
use crate::search::JoinSearches;
use crate::search::Negated;
use crate::search::SearchNode;
use crate::search::StateKind;
use crate::speedrun::QUESTION_NOTETYPE_NAME;
use crate::speedrun::TOPIC_TAG_PREFIX;

#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct TopicMastery {
    /// Stability-weighted mean FSRS retrievability over activated cards (0..1).
    pub mastery: f32,
    /// Number of activated cards with FSRS memory state that contributed.
    pub card_count: usize,
}

/// Output of a single mastery pass: the per-topic map plus collection-wide
/// aggregates needed by the Memory model (counting each card once, even if it
/// carries several topics).
#[derive(Debug, Clone, Default)]
pub(crate) struct MasteryData {
    pub by_topic: HashMap<String, TopicMastery>,
    /// Distinct activated cards with FSRS memory state.
    pub graded_count: usize,
    /// Global stability-weighted mean retrievability (0..1) over those cards.
    pub overall: f32,
}

/// Running stability-weighted accumulator: (Σ R·S, Σ S, count).
#[derive(Default, Clone, Copy)]
struct Accumulator(f32, f32, usize);

impl Accumulator {
    fn add(&mut self, r: f32, stability: f32) {
        self.0 += r * stability;
        self.1 += stability;
        self.2 += 1;
    }

    fn mean(&self) -> f32 {
        if self.1 > 0.0 {
            self.0 / self.1
        } else {
            0.0
        }
    }
}

impl Collection {
    /// Single FSRS pass over ACTIVATED (non-suspended) cards carrying any
    /// `topic::` tag, producing both the per-topic map (T3a) and the
    /// collection-wide aggregates (1b). Read-only; never mutates.
    pub(crate) fn compute_topic_mastery(&mut self) -> Result<MasteryData> {
        // Suspended cards are excluded here (and never gathered into queues), so
        // "activated" falls out of the suspension state. Practice-question cards
        // are excluded too: they carry topic:: tags and only grade, so counting
        // them would let raw question review skew the memory/weakness signal —
        // the model measures retention of the linked memory cards only.
        let search = SearchNode::Tag {
            tag: format!("{TOPIC_TAG_PREFIX}*"),
            mode: FieldSearchMode::Normal,
        }
        .and(StateKind::Suspended.negated())
        .and(SearchNode::Notetype(QUESTION_NOTETYPE_NAME.into()).negated());
        let cards = self.all_cards_for_search(search)?;
        if cards.is_empty() {
            return Ok(MasteryData::default());
        }

        let note_ids: Vec<NoteId> = cards.iter().map(|c| c.note_id).unique().collect();
        let note_topics = self.note_topic_map(&note_ids)?;

        let timing = self.timing_today()?;
        let fsrs = FSRS::new(None).unwrap();
        let mut by_topic: HashMap<String, Accumulator> = HashMap::new();
        let mut global = Accumulator::default();
        for card in &cards {
            let Some(state) = card.memory_state else {
                continue;
            };
            let Some(topics) = note_topics.get(&card.note_id) else {
                continue;
            };
            let elapsed_seconds = card.seconds_since_last_review(&timing).unwrap_or_default();
            let r = fsrs.current_retrievability_seconds(
                state.into(),
                elapsed_seconds,
                card.decay.unwrap_or(FSRS5_DEFAULT_DECAY),
            );
            // Guard against a zero/degenerate stability so the weighted mean is
            // well defined.
            let stability = state.stability.max(f32::MIN_POSITIVE);
            // Each card counts once globally, but towards every topic it carries.
            global.add(r, stability);
            for topic in topics {
                by_topic.entry(topic.clone()).or_default().add(r, stability);
            }
        }

        Ok(MasteryData {
            by_topic: by_topic
                .into_iter()
                .map(|(topic, acc)| {
                    (
                        topic,
                        TopicMastery {
                            mastery: acc.mean(),
                            card_count: acc.2,
                        },
                    )
                })
                .collect(),
            graded_count: global.2,
            overall: global.mean(),
        })
    }

    /// Per-topic mastery map (T3a). Topics with no contributing (reviewed) card
    /// are absent.
    pub(crate) fn topic_mastery_map(&mut self) -> Result<HashMap<String, TopicMastery>> {
        Ok(self.compute_topic_mastery()?.by_topic)
    }

    /// `weakness = 1 − mastery` per topic, consumed by value ordering. Topics
    /// with no data are absent; callers default missing topics to maximal
    /// weakness (1.0) so brand-new activated cards still surface.
    pub(crate) fn topic_weakness_map(&mut self) -> Result<HashMap<String, f32>> {
        Ok(self
            .topic_mastery_map()?
            .into_iter()
            .map(|(topic, m)| (topic, (1.0 - m.mastery).clamp(0.0, 1.0)))
            .collect())
    }

    /// note id -> the topic names (suffixes after `topic::`) on that note.
    /// Notes with no topic tag are omitted.
    pub(crate) fn note_topic_map(
        &self,
        note_ids: &[NoteId],
    ) -> Result<HashMap<NoteId, Vec<String>>> {
        let mut map = HashMap::new();
        for note_tags in self.storage.get_note_tags_by_id_list(note_ids)? {
            let topics = topics_from_tag_string(&note_tags.tags);
            if !topics.is_empty() {
                map.insert(note_tags.id, topics);
            }
        }
        Ok(map)
    }
}

/// Extract topic names from a DB-form (space-separated) tag string, stripping
/// the `topic::` prefix. Empty results for notes without topic tags.
pub(crate) fn topics_from_tag_string(tags: &str) -> Vec<String> {
    tags.split_whitespace()
        .filter_map(|t| t.strip_prefix(TOPIC_TAG_PREFIX))
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

#[cfg(test)]
mod test {
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::notetype::Notetype;
    use crate::prelude::*;
    use crate::speedrun::test_helpers::*;
    use crate::speedrun::QUESTION_NOTETYPE_NAME;

    /// Give a note's card real, non-suspended FSRS memory state so it would be
    /// counted by the mastery pass unless deliberately excluded.
    fn grade_card(col: &mut Collection, note: &Note) {
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.memory_state = Some(FsrsMemoryState {
            stability: 40.0,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs(1_000));
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
    }

    /// Add a graded note of the `SpeedrunQuestion` notetype (cloned from Basic),
    /// carrying the given tags.
    fn add_question_note(col: &mut Collection, tags: &[&str]) -> Note {
        let ntid = match col.get_notetype_by_name(QUESTION_NOTETYPE_NAME).unwrap() {
            Some(nt) => nt.id,
            None => {
                let basic = col.get_notetype_by_name("Basic").unwrap().unwrap();
                let mut nt: Notetype = (*basic).clone();
                nt.id = NotetypeId(0);
                nt.name = QUESTION_NOTETYPE_NAME.to_string();
                col.add_notetype(&mut nt, true).unwrap();
                nt.id
            }
        };
        let nt = col.get_notetype(ntid).unwrap().unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "content").unwrap();
        note.tags = tags.iter().map(|t| t.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();
        note
    }

    /// A graded practice-question card must not contribute to the mastery pass,
    /// even though it carries a `topic::` tag and is not suspended — only linked
    /// memory cards count.
    #[test]
    fn question_cards_are_excluded_from_mastery() {
        let mut col = Collection::new();

        let memory_card = add_note_with_tags(&mut col, &["topic::mem"]);
        grade_card(&mut col, &memory_card);
        let question = add_question_note(&mut col, &["topic::qonly"]);
        grade_card(&mut col, &question);

        let data = col.compute_topic_mastery().unwrap();
        assert_eq!(data.graded_count, 1, "only the memory card should count");
        assert!(data.by_topic.contains_key("mem"));
        assert!(
            !data.by_topic.contains_key("qonly"),
            "question-only topic must not appear in mastery"
        );
    }
}
