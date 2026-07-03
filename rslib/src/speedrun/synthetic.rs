// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Synthetic seed responses (dev/test helper) — GATED, never silent.
//!
//! Real collections often have little/no `revlog` on served questions, so the
//! Performance and Readiness models can't be exercised (and a grader can't show
//! they "beat chance"). This helper deterministically fabricates responses for a
//! chosen *true* ability, sampled from the same 2PL the Performance model fits,
//! and writes them as native `revlog` entries on served-question cards.
//!
//! **Gating (so synthetic data never masquerades as real progress):**
//! - It is only ever run by an *explicit* call (`col.speedrun.seed_synthetic_
//!   responses(...)` / this method). Nothing in the normal study loop calls it.
//! - It flips the [`SYNTHETIC_SEEDED_CONFIG_KEY`] flag, which the Performance and
//!   Readiness responses echo as `synthetic = true`, so any surfaced score is
//!   visibly labelled synthetic on every client.
//! - It is atomic + undoable (a single `Op::SeedSyntheticResponses`), and only
//!   *adds* `revlog` rows — it never touches FSRS state, due dates, or the
//!   question/flashcard content.

use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogId;
use crate::revlog::RevlogReviewKind;
use crate::search::FieldSearchMode;
use crate::search::JoinSearches;
use crate::search::Negated;
use crate::search::SearchNode;
use crate::speedrun::performance::p_correct_2pl;
use crate::speedrun::performance::DEFAULT_REPRESENTATIVE_A;
use crate::speedrun::performance::DEFAULT_REPRESENTATIVE_B;
use crate::speedrun::rng::SplitMix64;
use crate::speedrun::POOL_HELDOUT_TAG;
use crate::speedrun::POOL_SERVED_TAG;
use crate::speedrun::QUESTION_NOTETYPE_NAME;
use crate::speedrun::SYNTHETIC_SEEDED_CONFIG_KEY;

/// Default fabricated ability (logit scale): clearly above chance so the models
/// "beat chance", without being unrealistically perfect.
pub(crate) const DEFAULT_SYNTHETIC_TRUE_THETA: f64 = 1.0;
/// Default fabricated responses per served question.
pub(crate) const DEFAULT_SYNTHETIC_RESPONSES_PER_QUESTION: u32 = 3;
/// Default PRNG seed for reproducible synthetic runs.
pub(crate) const DEFAULT_SYNTHETIC_SEED: u64 = 0x5EED_D474_0000_0001;

/// Fabricated response time (ms): comfortably above the engagement threshold so
/// synthetic answers count at full reliability weight.
const SYNTHETIC_TAKEN_MILLIS: u32 = 8_000;
/// Ease factor stamped on synthetic entries (non-zero so they are not read as a
/// reset/cram; value is otherwise unused by the models).
const SYNTHETIC_EASE_FACTOR: u32 = 2500;

impl Collection {
    /// Seed deterministic synthetic responses on served-question cards for a
    /// given true ability, and mark the collection as synthetically seeded.
    ///
    /// Returns the number of `revlog` entries added. Atomic + undoable.
    pub(crate) fn speedrun_seed_synthetic_responses(
        &mut self,
        responses_per_question: u32,
        true_theta: f64,
        seed: u64,
    ) -> Result<OpOutput<usize>> {
        let per_q = responses_per_question.max(1);

        // Served questions only — never fabricate on the held-out pool.
        let search = SearchNode::Notetype(QUESTION_NOTETYPE_NAME.into())
            .and(SearchNode::Tag {
                tag: POOL_SERVED_TAG.into(),
                mode: FieldSearchMode::Normal,
            })
            .and(
                SearchNode::Tag {
                    tag: POOL_HELDOUT_TAG.into(),
                    mode: FieldSearchMode::Normal,
                }
                .negated(),
            );
        let cards = self.all_cards_for_search(search)?;

        let (b_ord, a_ord) = match self.get_notetype_by_name(QUESTION_NOTETYPE_NAME)? {
            Some(nt) => (
                nt.get_field_ord("difficulty_b"),
                nt.get_field_ord("discrimination_a"),
            ),
            None => (None, None),
        };

        self.transact(Op::SeedSyntheticResponses, |col| {
            let mut rng = SplitMix64::new(seed);
            let mut added = 0usize;
            for card in &cards {
                let Some(note) = col.storage.get_note(card.note_id)? else {
                    continue;
                };
                let fields = note.fields();
                let b = b_ord
                    .and_then(|o| fields.get(o))
                    .and_then(|s| s.trim().parse::<f64>().ok())
                    .filter(|v| v.is_finite())
                    .unwrap_or(DEFAULT_REPRESENTATIVE_B);
                let a = a_ord
                    .and_then(|o| fields.get(o))
                    .and_then(|s| s.trim().parse::<f64>().ok())
                    .filter(|v| v.is_finite() && *v > 0.0)
                    .unwrap_or(DEFAULT_REPRESENTATIVE_A);

                let p = p_correct_2pl(true_theta, a, b);
                for _ in 0..per_q {
                    let correct = rng.next_f64() < p;
                    let entry = RevlogEntry {
                        id: RevlogId::new(),
                        cid: card.id,
                        usn: col.usn()?,
                        // Good (3) == correct, Again (1) == incorrect.
                        button_chosen: if correct { 3 } else { 1 },
                        interval: 0,
                        last_interval: 0,
                        ease_factor: SYNTHETIC_EASE_FACTOR,
                        taken_millis: SYNTHETIC_TAKEN_MILLIS,
                        review_kind: RevlogReviewKind::Review,
                    };
                    col.add_revlog_entry_undoable(entry)?;
                    added += 1;
                }
            }
            // Flag the collection so every surfaced score is labelled synthetic.
            col.set_config(SYNTHETIC_SEEDED_CONFIG_KEY, &true)?;
            Ok(added)
        })
    }
}

#[cfg(test)]
mod test {
    use crate::prelude::*;
    use crate::speedrun::SYNTHETIC_SEEDED_CONFIG_KEY;

    /// Seeding fabricates responses, flags the collection synthetic, and lets a
    /// previously-abstaining Performance model beat chance — while any score is
    /// honestly labelled synthetic.
    #[test]
    fn seeding_beats_chance_and_is_labelled() {
        let mut col = Collection::new();
        col.set_speedrun_blueprint(&crate::speedrun::blueprint::Blueprint {
            topics: vec![crate::speedrun::blueprint::BlueprintTopic {
                name: "biology".into(),
                weight: 1.0,
            }],
        })
        .unwrap();

        // Provision served questions of the SpeedrunQuestion notetype.
        crate::speedrun::synthetic::test::add_served_questions(&mut col, 12);

        // Before seeding: no responses -> Performance abstains, not synthetic.
        let before = col.speedrun_performance_score().unwrap();
        assert!(before.abstained);
        assert!(!before.synthetic);

        let out = col
            .speedrun_seed_synthetic_responses(4, 1.5, 123)
            .unwrap();
        assert!(out.output > 0, "should add synthetic responses");

        let after = col.speedrun_performance_score().unwrap();
        assert!(after.synthetic, "score must be labelled synthetic");
        assert!(!after.abstained, "enough synthetic data to score");
        // A +1.5 ability student should clear the guessing floor comfortably.
        assert!(after.overall > 0.4, "should beat chance: {}", after.overall);
        assert_eq!(
            col.get_config_optional::<bool, _>(SYNTHETIC_SEEDED_CONFIG_KEY),
            Some(true)
        );

        // Deterministic: same seed -> identical fitted ability.
        let mut col2 = Collection::new();
        col2.set_speedrun_blueprint(&crate::speedrun::blueprint::Blueprint {
            topics: vec![crate::speedrun::blueprint::BlueprintTopic {
                name: "biology".into(),
                weight: 1.0,
            }],
        })
        .unwrap();
        crate::speedrun::synthetic::test::add_served_questions(&mut col2, 12);
        col2.speedrun_seed_synthetic_responses(4, 1.5, 123).unwrap();
        let after2 = col2.speedrun_performance_score().unwrap();
        assert!((after.theta - after2.theta).abs() < 1e-6, "seed must be deterministic");
    }

    /// Add `n` served SpeedrunQuestion notes with varied difficulty.
    pub(super) fn add_served_questions(col: &mut Collection, n: usize) {
        use crate::notetype::Notetype;
        use crate::speedrun::QUESTION_NOTETYPE_NAME;

        let ntid = match col.get_notetype_by_name(QUESTION_NOTETYPE_NAME).unwrap() {
            Some(nt) => nt.id,
            None => {
                let basic = col.get_notetype_by_name("Basic").unwrap().unwrap();
                let mut nt: Notetype = (*basic).clone();
                nt.id = NotetypeId(0);
                nt.name = QUESTION_NOTETYPE_NAME.to_string();
                // Ensure the frozen field contract exists so a/b parse.
                for field in ["correct", "explanation", "source", "difficulty_b", "discrimination_a"] {
                    nt.add_field(field);
                }
                col.add_notetype(&mut nt, true).unwrap();
                nt.id
            }
        };
        let nt = col.get_notetype(ntid).unwrap().unwrap();
        let b_ord = nt.get_field_ord("difficulty_b");
        let a_ord = nt.get_field_ord("discrimination_a");
        for i in 0..n {
            let mut note = nt.new_note();
            let b = -1.5 + 3.0 * (i as f64) / (n as f64 - 1.0).max(1.0);
            if let Some(o) = b_ord {
                note.fields_mut()[o] = format!("{b:.2}");
            }
            if let Some(o) = a_ord {
                note.fields_mut()[o] = "1.00".to_string();
            }
            note.tags = vec!["topic::biology".into(), "pool::served".into()];
            col.add_note(&mut note, DeckId(1)).unwrap();
        }
    }
}
