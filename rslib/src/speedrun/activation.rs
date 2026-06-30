// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Question-gated card activation (the central grading gate, M1 1a).
//!
//! Activation flips a *set* of cards from `Suspended` back to their scheduled
//! queue, atomically and undo-safely, by reusing the verified
//! `unsuspend_or_unbury_searched_cards` primitive inside `transact(Op::…)`. It
//! never touches FSRS intervals/due dates.

use crate::card::CardQueue;
use crate::prelude::*;
use crate::speedrun::MissReason;

impl Collection {
    /// Activate (unsuspend) the cards linked to a missed question — but only
    /// for qualifying memory-problem reasons (D-2b). For non-qualifying
    /// reasons (including the proto3 default `Unspecified`) this is a no-op
    /// that records no undo entry and returns an empty list.
    ///
    /// Atomic + undo-safe (single `Op::ActivateForMiss` entry) and idempotent
    /// on already-active cards.
    pub(crate) fn activate_cards_for_miss(
        &mut self,
        question_nid: NoteId,
        reason: MissReason,
    ) -> Result<OpOutput<Vec<CardId>>> {
        if !reason.activates() {
            // No-op: no undo entry, empty result.
            return self.transact(Op::SkipUndo, |_col| Ok(Vec::new()));
        }
        self.transact(Op::ActivateForMiss, |col| {
            let cids = col.linked_card_ids_for_question(question_nid)?;
            col.activate_card_ids(&cids)
        })
    }

    /// Unsuspend the given cards (idempotent), returning the ids actually
    /// activated (those that were `Suspended` before the call). Buried cards
    /// are left untouched — only suspension represents the Speedrun "off"
    /// state.
    ///
    /// Must be called inside a `transact` so the queue changes are undoable.
    pub(crate) fn activate_card_ids(&mut self, cids: &[CardId]) -> Result<Vec<CardId>> {
        let suspended: Vec<Card> = self
            .all_cards_for_ids(cids, false)?
            .into_iter()
            .filter(|c| c.queue == CardQueue::Suspended)
            .collect();
        let activated: Vec<CardId> = suspended.iter().map(|c| c.id).collect();
        self.unsuspend_or_unbury_searched_cards(suspended)?;
        Ok(activated)
    }
}

#[cfg(test)]
mod test {
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::prelude::*;
    use crate::speedrun::test_helpers::*;
    use crate::speedrun::MissReason;

    /// AC-2: only KNOWLEDGE_GAP / MISSING_CONTEXT activate; the rest are
    /// no-ops.
    #[test]
    fn activates_only_qualifying_reasons() {
        let mut col = Collection::new();
        let question = add_note_with_tags(&mut col, &["topic::biochem"]);
        let flashcard = add_note_with_tags(&mut col, &["topic::biochem"]);
        let cids = col.storage.card_ids_of_notes(&[flashcard.id]).unwrap();
        suspend(&mut col, &cids);
        assert!(cids.iter().all(|&c| is_suspended(&mut col, c)));

        for reason in [MissReason::KnowledgeGap, MissReason::MissingContext] {
            suspend(&mut col, &cids);
            let out = col.activate_cards_for_miss(question.id, reason).unwrap();
            assert_eq!(out.output.len(), cids.len(), "{reason:?} should activate");
            assert!(
                cids.iter().all(|&c| !is_suspended(&mut col, c)),
                "{reason:?} should unsuspend linked cards"
            );
        }

        for reason in [
            MissReason::Misunderstanding,
            MissReason::Careless,
            MissReason::Unspecified,
        ] {
            suspend(&mut col, &cids);
            let out = col.activate_cards_for_miss(question.id, reason).unwrap();
            assert!(out.output.is_empty(), "{reason:?} should be a no-op");
            assert!(
                cids.iter().all(|&c| is_suspended(&mut col, c)),
                "{reason:?} must leave cards suspended"
            );
        }
    }

    /// Only the question's *linked* cards (shared topic) activate; unrelated
    /// suspended cards stay off.
    #[test]
    fn activates_only_linked_cards() {
        let mut col = Collection::new();
        let question = add_note_with_tags(&mut col, &["topic::biochem"]);
        let linked = add_note_with_tags(&mut col, &["topic::biochem"]);
        let unrelated = add_note_with_tags(&mut col, &["topic::physics"]);
        let linked_cids = col.storage.card_ids_of_notes(&[linked.id]).unwrap();
        let unrelated_cids = col.storage.card_ids_of_notes(&[unrelated.id]).unwrap();
        suspend(&mut col, &linked_cids);
        suspend(&mut col, &unrelated_cids);

        let out = col
            .activate_cards_for_miss(question.id, MissReason::KnowledgeGap)
            .unwrap();
        assert_eq!(out.output, linked_cids);
        assert!(unrelated_cids.iter().all(|&c| is_suspended(&mut col, c)));
    }

    /// AC-3 + AC-6: activation is a single undoable step, the integrity check
    /// passes, and FSRS scheduling fields are untouched.
    #[test]
    fn activation_is_undoable_and_preserves_scheduling() {
        let mut col = Collection::new();
        let question = add_note_with_tags(&mut col, &["topic::physics"]);
        let flashcard = add_note_with_tags(&mut col, &["topic::physics"]);
        let cid = col.storage.card_ids_of_notes(&[flashcard.id]).unwrap()[0];

        // Give the card real FSRS scheduling state, then suspend it (no undo
        // entry, so the only undoable op is the activation).
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Suspended;
        card.due = 123;
        card.interval = 30;
        card.reps = 4;
        card.memory_state = Some(FsrsMemoryState {
            stability: 40.0,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs(1_000));
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
        let before = col.storage.get_card(cid).unwrap().unwrap();

        let out = col
            .activate_cards_for_miss(question.id, MissReason::KnowledgeGap)
            .unwrap();
        assert_eq!(out.output, vec![cid]);

        let after = col.storage.get_card(cid).unwrap().unwrap();
        assert_ne!(
            after.queue,
            CardQueue::Suspended,
            "card should be activated"
        );
        // FSRS / scheduling fields unchanged (AC-6).
        assert_eq!(after.due, before.due);
        assert_eq!(after.interval, before.interval);
        assert_eq!(after.reps, before.reps);
        assert_eq!(after.ctype, before.ctype);
        assert_eq!(after.memory_state, before.memory_state);
        assert_eq!(after.last_review_time, before.last_review_time);

        // Undo restores suspension in one step, leaving scheduling intact (AC-3).
        col.undo().unwrap();
        let undone = col.storage.get_card(cid).unwrap().unwrap();
        assert_eq!(undone.queue, CardQueue::Suspended);
        assert_eq!(undone.due, before.due);
        assert_eq!(undone.memory_state, before.memory_state);

        // Collection integrity check passes after the round-trip.
        col.check_database().unwrap();
    }
}
