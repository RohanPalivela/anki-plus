// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Speedrun (MCAT fork) engine.
//!
//! Inverts the normal flashcard loop: every flashcard starts suspended, and a
//! card only becomes reviewable when a *missed* practice question — classified
//! as a memory problem — activates it. This module owns:
//!
//! - **activation** ([`activation`]): atomic, undo-safe unsuspension of a
//!   missed question's linked cards (qualifying reasons only).
//! - **linkage** ([`linkage`]): resolving which cards a question can activate
//!   via shared `topic::` tags and optional `gates::<note_id>` references.
//! - **mastery** ([`mastery`]): the shared per-topic FSRS mastery/weakness map
//!   (consumed by both value ordering and the Memory model).
//! - **value ordering** ([`value_order`]): `value = topic_weight × weakness`.
//! - **coverage sweep** ([`sweep`]): re-activating a spread across all topics.
//! - **blueprint** ([`blueprint`]): the MCAT topic-weight config.
//!
//! It never alters FSRS intervals/due dates — it governs *activation +
//! ordering* only.

pub(crate) mod activation;
pub(crate) mod blueprint;
pub(crate) mod linkage;
pub(crate) mod mastery;
pub(crate) mod service;
pub(crate) mod sweep;
pub(crate) mod value_order;

/// Tag prefix marking a blueprint topic that a note (question or flashcard)
/// belongs to. The shared `topic::<name>` tag is the default question↔card
/// link.
pub(crate) const TOPIC_TAG_PREFIX: &str = "topic::";

/// Optional tag prefix on a question note pointing at a specific flashcard note
/// id it can activate, for when shared-topic linkage is too coarse (D-2a).
pub(crate) const GATES_TAG_PREFIX: &str = "gates::";

/// Why a practice question was missed. Mirrors the protobuf `MissReason`, but
/// kept as a plain Rust enum so the core gating logic and its tests don't
/// depend on generated protobuf types.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MissReason {
    /// Defensive default (proto3 unset) — treated as a no-op.
    Unspecified,
    /// Didn't know the fact — activates linked cards.
    KnowledgeGap,
    /// Knew the fact but didn't connect/apply it — activates linked cards.
    MissingContext,
    /// Reasoning error, not a memory problem — no-op.
    Misunderstanding,
    /// Slip — no-op.
    Careless,
}

impl MissReason {
    /// Only memory problems activate linked cards (D-2b).
    pub(crate) fn activates(self) -> bool {
        matches!(self, MissReason::KnowledgeGap | MissReason::MissingContext)
    }
}

impl From<anki_proto::speedrun::MissReason> for MissReason {
    fn from(value: anki_proto::speedrun::MissReason) -> Self {
        use anki_proto::speedrun::MissReason as Proto;
        match value {
            Proto::Unspecified => MissReason::Unspecified,
            Proto::KnowledgeGap => MissReason::KnowledgeGap,
            Proto::MissingContext => MissReason::MissingContext,
            Proto::Misunderstanding => MissReason::Misunderstanding,
            Proto::Careless => MissReason::Careless,
        }
    }
}

#[cfg(test)]
pub(crate) mod test_helpers {
    use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryOrSuspendMode;

    use crate::card::CardQueue;
    use crate::prelude::*;

    /// Add a `Basic` note in the default deck carrying the given tags.
    pub(crate) fn add_note_with_tags(col: &mut Collection, tags: &[&str]) -> Note {
        let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = notetype.new_note();
        note.set_field(0, "content").unwrap();
        note.tags = tags.iter().map(|t| t.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();
        note
    }

    pub(crate) fn suspend(col: &mut Collection, cids: &[CardId]) {
        col.bury_or_suspend_cards(cids, BuryOrSuspendMode::Suspend)
            .unwrap();
    }

    pub(crate) fn is_suspended(col: &mut Collection, cid: CardId) -> bool {
        col.storage.get_card(cid).unwrap().unwrap().queue == CardQueue::Suspended
    }
}
