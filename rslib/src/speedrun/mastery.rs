// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Shared per-topic mastery / weakness map (DAG task T3a).
//!
//! Reuses the exact read-only FSRS retrievability call used by the stats graphs
//! (`current_retrievability_seconds`, gated on `card.memory_state`) and
//! aggregates it to an **unweighted** (equal per-card) mean per-topic mastery
//! over **activated** (non-suspended) cards carrying each `topic::` tag.
//!
//! Mastery is **projected to a future horizon** (an optional configured exam
//! date, else a small default), not measured at "now". Measured at now, a card
//! reviewed seconds ago reads ~100% retrievability regardless of whether it was
//! graded Again/Hard/Good/Easy — the forgetting curve hasn't decayed yet — so
//! the score would read a deceptive ~100% right after every session. Projecting
//! to the horizon lets a lapsed (low-stability) card decay below a well-learned
//! (high-stability) one, so Again/Hard pull the score down immediately.
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
use crate::speedrun::BANK_AI_GENERATED_TAG;
use crate::speedrun::QUESTION_NOTETYPE_NAME;
use crate::speedrun::TOPIC_TAG_PREFIX;

/// Config key: project mastery retrievability this many days into the future,
/// so recently-missed (low-stability) cards read low immediately instead of a
/// deceptive ~100% right after review. camelCase to match the sibling keys.
pub(crate) const MASTERY_HORIZON_DAYS_CONFIG_KEY: &str = "speedrunMasteryHorizonDays";

/// Config key: absolute exam/target date as unix seconds. When set and in the
/// future it overrides the day horizon — mastery becomes "probability you'll
/// still recall this on exam day", which is the metric a learner actually cares
/// about.
pub(crate) const EXAM_DATE_CONFIG_KEY: &str = "speedrunExamDate";

/// Fallback projection horizon when no exam date is configured. A one-week
/// retention horizon is the honest readiness question for exam prep ("will I
/// still recall this in a week?"): a just-lapsed sub-day-stability card decays
/// to near zero while a well-learned card stays high, so Again/Hard answers
/// pull the score down the way a learner expects. A 1-day horizon is too
/// lenient (you can recall almost anything tomorrow); a much longer one
/// collapses even solid cards. Overridden by a configured future exam date.
pub(crate) const DEFAULT_MASTERY_HORIZON_DAYS: f64 = 7.0;

/// Clamp on the projection horizon so a distant or misconfigured exam date
/// can't push every card's retrievability to ~0 (which would make the score
/// useless). ~180 days spans a full prep cycle.
const MAX_MASTERY_HORIZON_DAYS: f64 = 180.0;

/// Seconds per day, for horizon arithmetic.
const SECONDS_PER_DAY: f64 = 86_400.0;

#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct TopicMastery {
    /// Unweighted (equal per-card) mean FSRS retrievability over activated
    /// cards (0..1), projected to the mastery horizon (see module docs).
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
    /// Global unweighted mean retrievability (0..1) over those cards.
    pub overall: f32,
}

/// Running **unweighted** retrievability accumulator: (Σ R, count).
///
/// Deliberately NOT stability-weighted. A stability-weighted mean
/// (`Σ(R·S)/Σ(S)`) lets high-stability (well-learned) cards dominate while
/// just-lapsed Again/Hard cards — which have tiny stability — contribute almost
/// nothing, so misses become nearly invisible and the score reads far too high.
/// An equal per-card mean makes every activated card count the same, so a batch
/// of Again/Hard answers pulls mastery down the way a learner expects.
#[derive(Default, Clone, Copy)]
struct Accumulator {
    sum_r: f32,
    count: usize,
}

impl Accumulator {
    fn add(&mut self, r: f32) {
        self.sum_r += r;
        self.count += 1;
    }

    fn mean(&self) -> f32 {
        if self.count > 0 {
            self.sum_r / self.count as f32
        } else {
            0.0
        }
    }
}

impl Collection {
    /// How many seconds into the future to project retrievability for the
    /// mastery pass. Prefers a configured exam date (so mastery reads as
    /// "chance you'll still recall this on exam day"); otherwise the
    /// day-horizon config, else [`DEFAULT_MASTERY_HORIZON_DAYS`]. Clamped
    /// to a sane maximum so a distant/misconfigured date can't collapse
    /// every card to ~0.
    fn mastery_horizon_seconds(&self) -> u32 {
        let max_seconds = MAX_MASTERY_HORIZON_DAYS * SECONDS_PER_DAY;
        // A configured future exam date wins: project exactly to it.
        if let Some(exam_secs) = self.get_config_optional::<i64, _>(EXAM_DATE_CONFIG_KEY) {
            let remaining = (exam_secs - TimestampSecs::now().0) as f64;
            if remaining > 0.0 {
                return remaining.min(max_seconds) as u32;
            }
        }
        let days = self
            .get_config_optional::<f64, _>(MASTERY_HORIZON_DAYS_CONFIG_KEY)
            .filter(|d| d.is_finite() && *d >= 0.0)
            .unwrap_or(DEFAULT_MASTERY_HORIZON_DAYS)
            .min(MAX_MASTERY_HORIZON_DAYS);
        (days * SECONDS_PER_DAY) as u32
    }

    /// Single FSRS pass over ACTIVATED (non-suspended) cards carrying any
    /// `topic::` tag, producing both the per-topic map (T3a) and the
    /// collection-wide aggregates (1b). Read-only; never mutates.
    pub(crate) fn compute_topic_mastery(&mut self) -> Result<MasteryData> {
        // Suspended cards are excluded here (and never gathered into queues), so
        // "activated" falls out of the suspension state. Practice-question cards
        // are excluded too: they carry topic:: tags and only grade, so counting
        // them would let raw question review skew the memory/weakness signal —
        // the model measures retention of the linked memory cards only.
        // AI-generated flashcard variants are also excluded (review-only): a
        // variant is a reworded copy of an existing fact, so counting it would
        // double-count that fact's retention in the per-topic mastery.
        let search = SearchNode::Tag {
            tag: format!("{TOPIC_TAG_PREFIX}*"),
            mode: FieldSearchMode::Normal,
        }
        .and(StateKind::Suspended.negated())
        .and(SearchNode::Notetype(QUESTION_NOTETYPE_NAME.into()).negated())
        .and(
            SearchNode::Tag {
                tag: BANK_AI_GENERATED_TAG.to_string(),
                mode: FieldSearchMode::Normal,
            }
            .negated(),
        );
        let cards = self.all_cards_for_search(search)?;
        if cards.is_empty() {
            return Ok(MasteryData::default());
        }

        let note_ids: Vec<NoteId> = cards.iter().map(|c| c.note_id).unique().collect();
        let note_topics = self.note_topic_map(&note_ids)?;

        let timing = self.timing_today()?;
        let horizon_seconds = self.mastery_horizon_seconds();
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
            // Project past the horizon: at now, elapsed≈0 for a just-reviewed
            // card so R≈1.0 regardless of grade; at elapsed+horizon a lapsed
            // (low-stability) card has decayed while a well-learned one has not.
            let projected_seconds = elapsed_seconds.saturating_add(horizon_seconds);
            let r = fsrs.current_retrievability_seconds(
                state.into(),
                projected_seconds,
                card.decay.unwrap_or(FSRS5_DEFAULT_DECAY),
            );
            // Each card counts once globally, but towards every topic it carries.
            // Equal weight per card (see Accumulator): a low-stability Again/Hard
            // card counts as much as a well-learned one, so misses are visible.
            global.add(r);
            for topic in topics {
                by_topic.entry(topic.clone()).or_default().add(r);
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
                            card_count: acc.count,
                        },
                    )
                })
                .collect(),
            graded_count: global.count,
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
    use crate::speedrun::mastery::MASTERY_HORIZON_DAYS_CONFIG_KEY;
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

    /// Like [`grade_card`] but with a caller-chosen stability and a review time
    /// of *now*, so elapsed≈0 (mirroring a card just answered this session).
    fn grade_card_now(col: &mut Collection, note: &Note, stability: f32) {
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now());
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
    }

    /// Add a graded note of the `SpeedrunQuestion` notetype (cloned from
    /// Basic), carrying the given tags.
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
    /// even though it carries a `topic::` tag and is not suspended — only
    /// linked memory cards count.
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

    /// An AI-generated flashcard variant must not contribute to the mastery
    /// pass, even though it is a non-suspended Basic card carrying a `topic::`
    /// tag — a reworded copy of a fact would otherwise double-count it.
    #[test]
    fn ai_variant_cards_are_excluded_from_mastery() {
        let mut col = Collection::new();

        let memory_card = add_note_with_tags(&mut col, &["topic::mem"]);
        grade_card(&mut col, &memory_card);
        let variant = add_note_with_tags(&mut col, &["topic::aivar", "bank::ai-generated"]);
        grade_card(&mut col, &variant);

        let data = col.compute_topic_mastery().unwrap();
        assert_eq!(
            data.graded_count, 1,
            "only the source memory card should count"
        );
        assert!(data.by_topic.contains_key("mem"));
        assert!(
            !data.by_topic.contains_key("aivar"),
            "AI-variant topic must not appear in mastery"
        );
    }

    /// A card just reviewed with sub-day stability (i.e. lapsed / graded
    /// Again-Hard) must read materially lower mastery than a just-reviewed,
    /// high-stability one — even though both were reviewed *now* (elapsed≈0).
    /// Without horizon projection both would read ~1.0; with it, the low-
    /// stability card decays below the strong one, so misses show up right
    /// away.
    #[test]
    fn projection_penalizes_low_stability_cards() {
        let mut col = Collection::new();
        col.set_config(MASTERY_HORIZON_DAYS_CONFIG_KEY, &1.0_f64)
            .unwrap();

        let weak = add_note_with_tags(&mut col, &["topic::weak"]);
        grade_card_now(&mut col, &weak, 0.2); // ~5h stability -> decays fast
        let strong = add_note_with_tags(&mut col, &["topic::strong"]);
        grade_card_now(&mut col, &strong, 100.0); // months of stability

        let data = col.compute_topic_mastery().unwrap();
        let weak_m = data.by_topic.get("weak").unwrap().mastery;
        let strong_m = data.by_topic.get("strong").unwrap().mastery;

        assert!(
            weak_m < strong_m,
            "low-stability mastery {weak_m} should be below high-stability {strong_m}"
        );
        assert!(
            weak_m < 0.9,
            "a sub-day-stability card should read well under 1.0 at a 1-day horizon, got {weak_m}"
        );
        assert!(
            strong_m > 0.95,
            "a high-stability card should stay near 1.0 at a 1-day horizon, got {strong_m}"
        );
    }

    /// Within one topic, a just-lapsed (low-stability) card must drag the topic
    /// mastery down toward the midpoint rather than being masked by a strong
    /// card. Under the old stability-weighted mean the strong card's huge
    /// stability dominated (topic ≈ strong card ≈ ~0.95); the unweighted mean
    /// gives each card equal say, so a strong+weak pair lands well below 0.95.
    #[test]
    fn mastery_is_unweighted_across_stability() {
        let mut col = Collection::new();
        col.set_config(MASTERY_HORIZON_DAYS_CONFIG_KEY, &1.0_f64)
            .unwrap();

        let strong = add_note_with_tags(&mut col, &["topic::mix"]);
        grade_card_now(&mut col, &strong, 100.0);
        let weak = add_note_with_tags(&mut col, &["topic::mix"]);
        grade_card_now(&mut col, &weak, 0.2);

        let data = col.compute_topic_mastery().unwrap();
        let mix = data.by_topic.get("mix").unwrap();
        assert_eq!(mix.card_count, 2);
        assert!(
            mix.mastery < 0.9,
            "unweighted mean of a strong+weak pair should sit clearly below the \
             strong card's ~0.95 (stability weighting would keep it ~0.95); got {}",
            mix.mastery
        );
    }
}
