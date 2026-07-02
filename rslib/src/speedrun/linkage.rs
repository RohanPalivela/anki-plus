// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Resolving the flashcards a practice question can activate (D-2a).
//!
//! Linkage prefers precise per-question `gates::<note_id>` references when the
//! question carries them and they resolve to real cards, so a question that
//! names specific flashcards activates exactly those rather than its whole
//! coarse `topic::<name>` set. When a question has no usable `gates::` links we
//! fall back to shared-topic linkage (and a dangling `gates::` never silently
//! activates nothing — it falls back too).

use crate::prelude::*;
use crate::search::FieldSearchMode;
use crate::search::JoinSearches;
use crate::search::Negated;
use crate::search::SearchBuilder;
use crate::search::SearchNode;
use crate::search::SortMode;
use crate::speedrun::mastery::topics_from_tag_string;
use crate::speedrun::GATES_TAG_PREFIX;
use crate::speedrun::TOPIC_TAG_PREFIX;

impl Collection {
    /// Card ids the given question note can activate. The question's own cards
    /// are always excluded.
    ///
    /// Precedence (D-2a): if the question carries `gates::<note_id>` tags that
    /// resolve to at least one other note's card, only those precise links are
    /// used. Otherwise linkage falls back to cards of notes sharing any of the
    /// question's `topic::` tags. A `gates::` tag that points at nothing usable
    /// (dangling id, or only the question itself) falls through to topic
    /// linkage so a legitimate miss is never silently a no-op.
    pub(crate) fn linked_card_ids_for_question(
        &mut self,
        question_nid: NoteId,
    ) -> Result<Vec<CardId>> {
        let Some(note_tags) = self.storage.get_note_tags_by_id(question_nid)? else {
            return Ok(vec![]);
        };

        // Prefer precise per-question `gates::` linkage when it resolves.
        let gated_nids = gated_note_ids(&note_tags.tags);
        if !gated_nids.is_empty() {
            let search = SearchNode::from_note_ids(gated_nids.iter().copied())
                .and(SearchNode::from(question_nid).negated());
            let gated_cards = self.search_cards(search, SortMode::NoOrder)?;
            if !gated_cards.is_empty() {
                return Ok(gated_cards);
            }
            // Fall through to coarse topic linkage below.
        }

        // Fall back to shared-topic linkage.
        let topics = topics_from_tag_string(&note_tags.tags);
        let linker_nodes: Vec<SearchNode> = topics
            .iter()
            .map(|topic| SearchNode::Tag {
                tag: format!("{TOPIC_TAG_PREFIX}{topic}"),
                mode: FieldSearchMode::Normal,
            })
            .collect();
        if linker_nodes.is_empty() {
            return Ok(vec![]);
        }

        // (topic OR ...) AND not the question's own note.
        let search = SearchBuilder::any(linker_nodes).and(SearchNode::from(question_nid).negated());
        self.search_cards(search, SortMode::NoOrder)
    }
}

/// Parse `gates::<note_id>` tags into note ids.
fn gated_note_ids(tags: &str) -> Vec<NoteId> {
    tags.split_whitespace()
        .filter_map(|t| t.strip_prefix(GATES_TAG_PREFIX))
        .filter_map(|s| s.parse::<i64>().ok())
        .map(NoteId)
        .collect()
}

#[cfg(test)]
mod test {
    use crate::prelude::*;
    use crate::speedrun::test_helpers::*;

    /// When a question carries `gates::<nid>` tags that resolve to real cards,
    /// activation targets exactly those cards, not the coarse shared-topic set.
    #[test]
    fn gates_take_precedence_over_topic() {
        let mut col = Collection::new();
        // Two flashcards share the topic; only one is named by the question.
        let gated = add_note_with_tags(&mut col, &["topic::biochem"]);
        let topic_only = add_note_with_tags(&mut col, &["topic::biochem"]);
        let gate_tag = format!("{}{}", crate::speedrun::GATES_TAG_PREFIX, gated.id.0);
        let question = add_note_with_tags(&mut col, &["topic::biochem", gate_tag.as_str()]);

        let linked = col.linked_card_ids_for_question(question.id).unwrap();
        let gated_cids = col.storage.card_ids_of_notes(&[gated.id]).unwrap();
        let topic_cids = col.storage.card_ids_of_notes(&[topic_only.id]).unwrap();
        assert_eq!(linked, gated_cids, "only the gated card should be linked");
        assert!(
            topic_cids.iter().all(|c| !linked.contains(c)),
            "topic-only sibling must not be linked when gates:: resolves"
        );
    }

    /// A `gates::` tag that resolves to nothing usable falls back to topic
    /// linkage rather than silently linking zero cards (no silent no-op).
    #[test]
    fn dangling_gates_fall_back_to_topic() {
        let mut col = Collection::new();
        let sibling = add_note_with_tags(&mut col, &["topic::physics"]);
        // gates a non-existent note id.
        let question = add_note_with_tags(&mut col, &["topic::physics", "gates::999999999"]);

        let linked = col.linked_card_ids_for_question(question.id).unwrap();
        let sibling_cids = col.storage.card_ids_of_notes(&[sibling.id]).unwrap();
        assert_eq!(
            linked, sibling_cids,
            "dangling gates:: must fall back to topic linkage"
        );
    }
}
