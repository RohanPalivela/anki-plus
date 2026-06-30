// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Pure value-ordering math: `value = topic_weight × weakness`.
//!
//! The queue-builder integration that applies these to gathered cards lives in
//! `crate::scheduler::queue::builder::speedrun_value`; this module holds only
//! the (unit-testable) arithmetic + comparator.

use std::cmp::Ordering;
use std::collections::HashMap;

use crate::prelude::CardId;

/// Value of surfacing a card for a single topic: how much exam weight is at
/// stake (`topic_weight`) times how weak the student is on it (`weakness`).
pub(crate) fn topic_value(topic_weight: f32, weakness: f32) -> f32 {
    topic_weight * weakness
}

/// A card's overall value = the max `topic_value` across its topics (the
/// single most-valuable/weakest topic). This never over-credits a card whose
/// heavy-weight topic is already mastered. Cards with no blueprint topic get 0.
///
/// Missing weights default to 0 (off-blueprint topic) and missing weakness
/// defaults to 1.0 (no mastery data yet ⇒ maximally weak).
pub(crate) fn card_value(
    topics: &[String],
    topic_weights: &HashMap<String, f32>,
    topic_weakness: &HashMap<String, f32>,
) -> f32 {
    topics
        .iter()
        .map(|topic| {
            let weight = topic_weights.get(topic).copied().unwrap_or(0.0);
            let weakness = topic_weakness.get(topic).copied().unwrap_or(1.0);
            topic_value(weight, weakness)
        })
        .fold(0.0_f32, f32::max)
}

/// Order two `(value, card_id)` pairs by descending value, with a deterministic
/// stable tiebreak on ascending card id.
pub(crate) fn compare_desc(a: (f32, CardId), b: (f32, CardId)) -> Ordering {
    b.0.partial_cmp(&a.0)
        .unwrap_or(Ordering::Equal)
        .then_with(|| a.1.cmp(&b.1))
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn card_value_uses_max_topic_product() {
        let weights = HashMap::from([("a".to_string(), 0.8), ("b".to_string(), 0.2)]);
        // Strong on the heavy topic (low weakness), weak on the light topic.
        let weakness = HashMap::from([("a".to_string(), 0.1), ("b".to_string(), 0.9)]);
        let topics = vec!["a".to_string(), "b".to_string()];
        // max(0.8*0.1, 0.2*0.9) = max(0.08, 0.18) = 0.18
        assert!((card_value(&topics, &weights, &weakness) - 0.18).abs() < 1e-6);
        // A card with no blueprint topic scores 0.
        assert_eq!(card_value(&["zzz".to_string()], &weights, &weakness), 0.0);
        // Unknown weakness defaults to maximal (1.0).
        let topics_unknown = vec!["a".to_string()];
        assert!((card_value(&topics_unknown, &weights, &HashMap::new()) - 0.8).abs() < 1e-6);
    }

    #[test]
    fn compare_desc_orders_high_value_first_then_id() {
        let mut v = vec![
            (0.1_f32, CardId(5)),
            (0.9, CardId(2)),
            (0.9, CardId(1)),
            (0.5, CardId(9)),
        ];
        v.sort_by(|a, b| compare_desc(*a, *b));
        assert_eq!(
            v,
            vec![
                (0.9, CardId(1)),
                (0.9, CardId(2)),
                (0.5, CardId(9)),
                (0.1, CardId(5)),
            ]
        );
    }
}
