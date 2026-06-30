// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Resolving the flashcards a practice question can activate (D-2a).
//!
//! Default linkage is the shared `topic::<name>` tag; an optional
//! `gates::<note_id>` tag on the question adds precise note-level links.

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
    /// Card ids the given question note can activate: cards of notes that share
    /// any of the question's `topic::` tags, plus cards of notes named by the
    /// question's `gates::<note_id>` tags. The question's own cards are
    /// excluded.
    pub(crate) fn linked_card_ids_for_question(
        &mut self,
        question_nid: NoteId,
    ) -> Result<Vec<CardId>> {
        let Some(note_tags) = self.storage.get_note_tags_by_id(question_nid)? else {
            return Ok(vec![]);
        };
        let topics = topics_from_tag_string(&note_tags.tags);
        let gated_nids = gated_note_ids(&note_tags.tags);

        // Build the OR of all link predicates.
        let mut linker_nodes: Vec<SearchNode> = topics
            .iter()
            .map(|topic| SearchNode::Tag {
                tag: format!("{TOPIC_TAG_PREFIX}{topic}"),
                mode: FieldSearchMode::Normal,
            })
            .collect();
        if !gated_nids.is_empty() {
            linker_nodes.push(SearchNode::from_note_ids(gated_nids.iter().copied()));
        }
        if linker_nodes.is_empty() {
            return Ok(vec![]);
        }

        // (topic OR ... OR gated nids) AND not the question's own note.
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
