// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Speedrun value ordering, integrated into the queue builder.
//!
//! The value (`topic_weight × weakness`) cannot be computed at the `build` site
//! (`QueueBuilder::build` has no `Collection` and `DueCard`/`NewCard` carry no
//! tags). So:
//!   1. [`SpeedrunQueueContext::load`] runs in `QueueBuilder::new` (has `col`),
//!      loading the blueprint weights + the shared topic-weakness map.
//!   2. [`QueueBuilder::compute_speedrun_values`] runs in `gather_cards` (has
//!      `col`), batch-loading topic tags and stashing each card's value.
//!   3. [`QueueBuilder::sort_by_speedrun_value`] runs in `build`, behind
//!      `BoolKey::SpeedrunOrdering`.
//!
//! All steps are no-ops unless the flag is set, so stock scheduling is
//! unchanged. None of this alters FSRS intervals/due dates.

use std::collections::HashMap;
use std::collections::HashSet;

use super::QueueBuilder;
use crate::prelude::*;
use crate::speedrun::value_order;

/// Per-build Speedrun ordering inputs, loaded once when the flag is set.
#[derive(Debug, Clone)]
pub(super) struct SpeedrunQueueContext {
    topic_weights: HashMap<String, f32>,
    topic_weakness: HashMap<String, f32>,
}

impl SpeedrunQueueContext {
    /// Returns `None` (no reordering) unless `BoolKey::SpeedrunOrdering` is
    /// set.
    pub(super) fn load(col: &mut Collection) -> Result<Option<Self>> {
        if !col.get_config_bool(BoolKey::SpeedrunOrdering) {
            return Ok(None);
        }
        let topic_weights = col.get_speedrun_blueprint().topic_weight_map();
        let topic_weakness = col.topic_weakness_map()?;
        Ok(Some(Self {
            topic_weights,
            topic_weakness,
        }))
    }
}

impl QueueBuilder {
    /// After gathering, compute and stash each gathered review/new card's
    /// Speedrun value. No-op unless ordering is enabled.
    pub(super) fn compute_speedrun_values(&mut self, col: &mut Collection) -> Result<()> {
        if self.context.speedrun.is_none() {
            return Ok(());
        }
        // (card id, note id) for every orderable (review + new) card. DueCard
        // and NewCard are distinct types, so collect the shared fields first.
        let card_notes: Vec<(CardId, NoteId)> = self
            .review
            .iter()
            .map(|c| (c.id, c.note_id))
            .chain(self.new.iter().map(|c| (c.id, c.note_id)))
            .collect();
        let note_ids: Vec<NoteId> = card_notes
            .iter()
            .map(|(_, nid)| *nid)
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        let note_topics = col.note_topic_map(&note_ids)?;

        let ctx = self
            .context
            .speedrun
            .as_ref()
            .expect("speedrun context present");
        let empty: Vec<String> = Vec::new();
        let computed: Vec<(CardId, f32)> = card_notes
            .iter()
            .map(|(cid, nid)| {
                let topics = note_topics.get(nid).unwrap_or(&empty);
                let value =
                    value_order::card_value(topics, &ctx.topic_weights, &ctx.topic_weakness);
                (*cid, value)
            })
            .collect();
        for (id, value) in computed {
            self.speedrun_values.insert(id, value);
        }
        Ok(())
    }

    /// Sort gathered review + new cards by descending Speedrun value, stable on
    /// card id. No-op unless ordering is enabled. `build` consumes `self` right
    /// after, so we take the values map out rather than juggle field borrows.
    pub(super) fn sort_by_speedrun_value(&mut self) {
        if self.context.speedrun.is_none() {
            return;
        }
        let values = std::mem::take(&mut self.speedrun_values);
        let value_of = |id: CardId| values.get(&id).copied().unwrap_or(0.0);
        self.review.sort_by(|a, b| {
            value_order::compare_desc((value_of(a.id), a.id), (value_of(b.id), b.id))
        });
        self.new.sort_by(|a, b| {
            value_order::compare_desc((value_of(a.id), a.id), (value_of(b.id), b.id))
        });
    }
}

#[cfg(test)]
mod test {
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::prelude::*;
    use crate::speedrun::blueprint::Blueprint;
    use crate::speedrun::blueprint::BlueprintTopic;
    use crate::speedrun::test_helpers::*;

    /// Turn the note's first card into a due review card with FSRS state, so
    /// its retrievability (hence topic weakness) is controllable.
    fn setup_review_card(
        col: &mut Collection,
        nid: NoteId,
        stability: f32,
        elapsed_secs: i64,
    ) -> CardId {
        let cid = col.storage.card_ids_of_notes(&[nid]).unwrap()[0];
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.due = 0;
        card.interval = 50;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-elapsed_secs));
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
        cid
    }

    /// AC-1 + AC-4: suspended cards are never queued, and activated cards are
    /// ordered by `topic_weight × weakness` (so a weak light-topic card beats a
    /// mastered heavy-topic card).
    #[test]
    fn queue_excludes_suspended_and_orders_by_value() {
        let mut col = Collection::new();
        col.set_speedrun_blueprint(&Blueprint {
            topics: vec![
                BlueprintTopic {
                    name: "heavy".into(),
                    weight: 0.9,
                },
                BlueprintTopic {
                    name: "light".into(),
                    weight: 0.1,
                },
            ],
        })
        .unwrap();
        col.set_config_bool(BoolKey::SpeedrunOrdering, true, false)
            .unwrap();

        // Heavy topic, freshly reviewed -> high retrievability -> low weakness
        // -> low value despite the heavy weight.
        let heavy = add_note_with_tags(&mut col, &["topic::heavy"]);
        let heavy_cid = setup_review_card(&mut col, heavy.id, 100.0, 0);

        // Light topic, long overdue -> low retrievability -> high weakness ->
        // higher value despite the light weight.
        let light = add_note_with_tags(&mut col, &["topic::light"]);
        let light_cid = setup_review_card(&mut col, light.id, 1.0, 10_000_000);

        // A suspended card must never be gathered.
        let suspended_note = add_note_with_tags(&mut col, &["topic::heavy"]);
        let suspended_cids = col.storage.card_ids_of_notes(&[suspended_note.id]).unwrap();
        suspend(&mut col, &suspended_cids);

        let queue = col.build_queues(DeckId(1)).unwrap();
        let order: Vec<CardId> = queue.iter().map(|e| e.card_id()).collect();

        for cid in &suspended_cids {
            assert!(!order.contains(cid), "suspended card must not be queued");
        }
        let pos_light = order
            .iter()
            .position(|c| c == &light_cid)
            .expect("light card queued");
        let pos_heavy = order
            .iter()
            .position(|c| c == &heavy_cid)
            .expect("heavy card queued");
        assert!(
            pos_light < pos_heavy,
            "higher-value (weak, light-topic) card should come before the mastered heavy-topic card: {order:?}"
        );
    }
}
