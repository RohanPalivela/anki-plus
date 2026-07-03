// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Performance model (§M3a): **P(correct on a NEW question)**.
//!
//! A 2-parameter logistic (2PL) IRT model with a guessing floor for 4-option
//! MCQ:
//!
//! ```text
//!   P = c + (1 − c) · σ(a · (θ − b)),   σ(z) = 1 / (1 + e^(−z)),   c = 0.25
//! ```
//!
//! - `θ` (theta) is the student's latent ability, estimated (with a standard
//!   error) from their real `revlog` responses on served questions.
//! - `a` (discrimination) and `b` (difficulty) are per-question IRT parameters
//!   stored on the `SpeedrunQuestion` note.
//! - `c` is the 4-option guessing floor (a named constant,
//!   [`GUESSING_FLOOR_C`]).
//!
//! **Why this does not collapse to the Memory model.** Memory reads FSRS
//! retrievability of *activated flashcards*; Performance is fit on *question
//! responses* and, at prediction time, blends the fitted ability with the
//! question's topic mastery through an additive **logit offset** dominated by
//! the **weakest-link (minimum) topic mastery**. Two questions with identical
//! `a`/`b` but different weakest-link mastery therefore get different
//! `P(correct)` — a real memory→performance gap that difficulty (`b`) widens
//! further. See [`predict_performance`] and the `memory_performance_gap` test.

use std::collections::BTreeMap;
use std::collections::HashMap;

use anki_proto::speedrun;

use crate::prelude::*;
use crate::search::FieldSearchMode;
use crate::search::JoinSearches;
use crate::search::Negated;
use crate::search::SearchNode;
use crate::speedrun::POOL_HELDOUT_TAG;
use crate::speedrun::POOL_SERVED_TAG;
use crate::speedrun::QUESTION_NOTETYPE_NAME;
use crate::speedrun::SYNTHETIC_SEEDED_CONFIG_KEY;
use crate::speedrun::TOPIC_TAG_PREFIX;

/// 4-option MCQ guessing floor: even a student who knows nothing has a ~1/4
/// chance of a correct answer, so `P(correct)` can never fall below this.
pub(crate) const GUESSING_FLOOR_C: f64 = 0.25;

/// Standard-normal prior SD on ability. Fitting θ by MAP (not raw MLE) with a
/// `Normal(0, 1)` prior regularises thin data (a student with 1–2 answers can't
/// be pushed to ±∞) and yields a proper posterior `Normal(θ, se)` for the
/// Readiness Monte Carlo. SD = 1.0 is the conventional IRT ability scale.
const ABILITY_PRIOR_SD: f64 = 1.0;

/// Deterministic ability search grid (documented, no convergence issues). We
/// scan θ over [−4, 4] at a 0.01 step for the MAP, then read the standard error
/// from the analytic Fisher information at that point.
const THETA_GRID_MIN: f64 = -4.0;
const THETA_GRID_MAX: f64 = 4.0;
const THETA_GRID_STEP: f64 = 0.01;

/// Fraction of a question's mastery signal taken from its **weakest** topic
/// (the rest from the mean). 0.5 gives the weakest link half the say, which is
/// what creates the memory→performance gap on multi-topic questions.
pub(crate) const WEAKEST_LINK_WEIGHT: f64 = 0.5;

/// Converts a mastery signal in [0, 1] to a logit offset in
/// [−scale, +scale]: mastery 1.0 → +2 logits (easier for this student),
/// 0.0 → −2 logits (harder). Scale 2.0 is a substantial but bounded shift
/// (~±0.24 in probability space at the inflection) so memory materially moves
/// performance without dominating ability/difficulty.
pub(crate) const MASTERY_LOGIT_SCALE: f64 = 2.0;

/// Mastery used for a topic that has questions answered but no activated-card
/// FSRS data yet: neutral (no logit offset), so performance rests on ability +
/// difficulty alone rather than an invented memory signal.
pub(crate) const NEUTRAL_MASTERY: f64 = 0.5;

/// Responses faster than this are treated as partly unreliable (likely
/// slips/guesses on a reading-heavy MCQ) and down-weighted in the ability fit.
/// This is how response time enters the model as a real per-response feature.
const MIN_ENGAGED_MILLIS: f64 = 1000.0;
/// Floor on the response-time reliability weight so a fast answer is
/// discounted, never discarded.
const MIN_ENGAGEMENT_WEIGHT: f64 = 0.5;

/// Representative IRT parameters for a topic with no question data (used only
/// in per-topic display / uncovered-topic simulation): average difficulty, unit
/// discrimination.
pub(crate) const DEFAULT_REPRESENTATIVE_A: f64 = 1.0;
pub(crate) const DEFAULT_REPRESENTATIVE_B: f64 = 0.0;

/// Abstention: minimum graded responses before a Performance score is emitted.
const PERF_MIN_GRADED: usize = 5;
/// Abstention: minimum blueprint-topic coverage before emitting a score.
const PERF_MIN_COVERAGE: f32 = 0.10;
/// Two-sided 80% normal quantile, for the ability→probability interval.
const Z_80: f64 = 1.2816;

/// σ(z) = 1 / (1 + e^(−z)).
pub(crate) fn sigmoid(z: f64) -> f64 {
    1.0 / (1.0 + (-z).exp())
}

/// 2PL probability of a correct answer with the guessing floor `c`.
pub(crate) fn p_correct_2pl(theta: f64, a: f64, b: f64) -> f64 {
    GUESSING_FLOOR_C + (1.0 - GUESSING_FLOOR_C) * sigmoid(a * (theta - b))
}

/// Blend a question's topic masteries into a single signal: half from the
/// weakest topic (weakest link), half from the mean.
pub(crate) fn mastery_signal(masteries: &[f64]) -> f64 {
    if masteries.is_empty() {
        return NEUTRAL_MASTERY;
    }
    let mean = masteries.iter().sum::<f64>() / masteries.len() as f64;
    let min = masteries.iter().cloned().fold(f64::INFINITY, f64::min);
    WEAKEST_LINK_WEIGHT * min + (1.0 - WEAKEST_LINK_WEIGHT) * mean
}

/// Mastery signal (0..1) → additive ability logit offset.
pub(crate) fn mastery_logit(signal: f64) -> f64 {
    MASTERY_LOGIT_SCALE * (2.0 * signal - 1.0)
}

/// `predict_performance(question, mastery_vector) -> P(correct)`.
///
/// Blends the fitted ability with the question's topic masteries (weakest link
/// dominant) before applying the 2PL. This is the memory→performance coupling.
pub(crate) fn predict_performance(theta: f64, a: f64, b: f64, masteries: &[f64]) -> f64 {
    let effective_theta = theta + mastery_logit(mastery_signal(masteries));
    p_correct_2pl(effective_theta, a, b)
}

/// One graded response to a served question, used to fit ability.
#[derive(Debug, Clone, Copy)]
pub(crate) struct QuestionResponse {
    pub a: f64,
    pub b: f64,
    pub correct: bool,
    /// Response-time reliability weight in [MIN_ENGAGEMENT_WEIGHT, 1.0].
    pub weight: f64,
}

/// Response-time → reliability weight. Unknown (0 ms) counts fully; sub-second
/// answers are discounted toward the floor.
fn engagement_weight(taken_millis: u32) -> f64 {
    if taken_millis == 0 {
        return 1.0;
    }
    (taken_millis as f64 / MIN_ENGAGED_MILLIS).clamp(MIN_ENGAGEMENT_WEIGHT, 1.0)
}

/// `estimate_theta(responses) -> (theta, se)`.
///
/// MAP estimate of ability under a `Normal(0, ABILITY_PRIOR_SD)` prior via a
/// deterministic grid search, with the standard error from the analytic Fisher
/// information (data + prior) at the estimate. With no responses this returns
/// the prior: `(0.0, ABILITY_PRIOR_SD)` — a maximally uncertain, chance
/// ability.
pub(crate) fn estimate_theta(responses: &[QuestionResponse]) -> (f64, f64) {
    let prior_precision = 1.0 / (ABILITY_PRIOR_SD * ABILITY_PRIOR_SD);

    let mut best_theta = 0.0;
    let mut best_lp = f64::NEG_INFINITY;
    let mut theta = THETA_GRID_MIN;
    while theta <= THETA_GRID_MAX + 1e-9 {
        let lp = log_posterior(theta, responses, prior_precision);
        if lp > best_lp {
            best_lp = lp;
            best_theta = theta;
        }
        theta += THETA_GRID_STEP;
    }

    let information = fisher_information(best_theta, responses) + prior_precision;
    let se = (1.0 / information).sqrt();
    (best_theta, se)
}

/// Weighted 2PL log-likelihood + the Gaussian prior term.
fn log_posterior(theta: f64, responses: &[QuestionResponse], prior_precision: f64) -> f64 {
    let mut ll = 0.0;
    for r in responses {
        let p = p_correct_2pl(theta, r.a, r.b).clamp(1e-9, 1.0 - 1e-9);
        ll += r.weight * if r.correct { p.ln() } else { (1.0 - p).ln() };
    }
    ll - 0.5 * prior_precision * theta * theta
}

/// Analytic Fisher information of the data at `theta` for the 3PL/2PL-with-
/// guessing model: `Σ wᵢ · aᵢ² · (Qᵢ/Pᵢ) · ((Pᵢ − c)/(1 − c))²`.
fn fisher_information(theta: f64, responses: &[QuestionResponse]) -> f64 {
    let c = GUESSING_FLOOR_C;
    let mut info = 0.0;
    for r in responses {
        let p = p_correct_2pl(theta, r.a, r.b).clamp(1e-9, 1.0 - 1e-9);
        let q = 1.0 - p;
        let factor = ((p - c) / (1.0 - c)).powi(2);
        info += r.weight * r.a * r.a * (q / p) * factor;
    }
    info
}

/// Per-topic aggregate of served-question IRT parameters + response tally.
#[derive(Debug, Clone)]
pub(crate) struct TopicQuestionAgg {
    pub mean_a: f64,
    pub mean_b: f64,
    pub response_count: usize,
}

/// A fitted Performance model: shared by the Performance and Readiness RPCs.
pub(crate) struct PerformanceFit {
    pub theta: f64,
    pub theta_se: f64,
    pub graded_count: usize,
    /// topic → representative (a, b) + response count.
    pub by_topic: BTreeMap<String, TopicQuestionAgg>,
    /// topic → FSRS mastery (0..1); only topics with activated-card data.
    pub mastery: HashMap<String, f64>,
    /// True when synthetic seed responses have been added to this collection.
    pub synthetic: bool,
}

impl PerformanceFit {
    /// Mastery to use for a topic's prediction: FSRS mastery if known, else
    /// neutral (no memory offset).
    pub(crate) fn topic_mastery(&self, topic: &str) -> f64 {
        self.mastery.get(topic).copied().unwrap_or(NEUTRAL_MASTERY)
    }
}

impl Collection {
    /// Gather served-question responses + IRT parameters and fit ability.
    /// Read-only. Shared by the Performance and Readiness models.
    pub(crate) fn speedrun_fit_performance(&mut self) -> Result<PerformanceFit> {
        // Served questions only — held-out is never fitted on (D-8).
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

        let synthetic = self
            .get_config_optional::<bool, _>(SYNTHETIC_SEEDED_CONFIG_KEY)
            .unwrap_or(false);

        // Field ordinals for the frozen SpeedrunQuestion contract.
        let (b_ord, a_ord) = match self.get_notetype_by_name(QUESTION_NOTETYPE_NAME)? {
            Some(nt) => (
                nt.get_field_ord("difficulty_b"),
                nt.get_field_ord("discrimination_a"),
            ),
            None => (None, None),
        };

        let mastery = self
            .compute_topic_mastery()?
            .by_topic
            .into_iter()
            .filter(|(_, m)| m.card_count > 0)
            .map(|(t, m)| (t, m.mastery as f64))
            .collect();

        let mut responses: Vec<QuestionResponse> = Vec::new();
        for card in &cards {
            let Some(note) = self.storage.get_note(card.note_id)? else {
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

            for entry in self.storage.get_revlog_entries_for_card(card.id)? {
                if !entry.has_rating_and_affects_scheduling() {
                    continue;
                }
                responses.push(QuestionResponse {
                    a,
                    b,
                    // Again (1) == incorrect; Hard/Good/Easy (>=2) == correct,
                    // matching the study loop's grade mapping.
                    correct: entry.button_chosen >= 2,
                    weight: engagement_weight(entry.taken_millis),
                });
            }
        }

        // Per-topic representative (a, b) + response counts (means per question).
        let by_topic = self.topic_question_aggregates(&cards, a_ord, b_ord)?;

        let (theta, theta_se) = estimate_theta(&responses);
        let graded_count = responses.len();
        Ok(PerformanceFit {
            theta,
            theta_se,
            graded_count,
            by_topic,
            mastery,
            synthetic,
        })
    }

    /// Per-topic mean (a, b) over its served *questions* and total responses.
    fn topic_question_aggregates(
        &mut self,
        cards: &[Card],
        a_ord: Option<usize>,
        b_ord: Option<usize>,
    ) -> Result<BTreeMap<String, TopicQuestionAgg>> {
        // topic -> (sum_a, sum_b, question_count, response_count)
        let mut acc: BTreeMap<String, (f64, f64, usize, usize)> = BTreeMap::new();
        for card in cards {
            let Some(note) = self.storage.get_note(card.note_id)? else {
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
            let responses = self
                .storage
                .get_revlog_entries_for_card(card.id)?
                .into_iter()
                .filter(|e| e.has_rating_and_affects_scheduling())
                .count();
            for topic in note
                .tags
                .iter()
                .filter_map(|t| t.strip_prefix(TOPIC_TAG_PREFIX))
                .filter(|s| !s.is_empty())
            {
                let e = acc.entry(topic.to_string()).or_insert((0.0, 0.0, 0, 0));
                e.0 += a;
                e.1 += b;
                e.2 += 1;
                e.3 += responses;
            }
        }
        Ok(acc
            .into_iter()
            .map(|(t, (sum_a, sum_b, qc, rc))| {
                let qc_f = qc.max(1) as f64;
                (
                    t,
                    TopicQuestionAgg {
                        mean_a: sum_a / qc_f,
                        mean_b: sum_b / qc_f,
                        response_count: rc,
                    },
                )
            })
            .collect())
    }

    /// Compute the Performance score (proto). Read-only.
    pub(crate) fn speedrun_performance_score(
        &mut self,
    ) -> Result<speedrun::PerformanceScoreResponse> {
        let fit = self.speedrun_fit_performance()?;
        let blueprint = self.get_speedrun_blueprint();

        // Blueprint-weighted overall over topics that have response data, at a
        // given ability (used for the point estimate and the SE-driven range).
        let eval_overall = |theta: f64| -> f64 {
            let mut num = 0.0;
            let mut den = 0.0;
            for bt in &blueprint.topics {
                if let Some(agg) = fit.by_topic.get(&bt.name) {
                    if agg.response_count == 0 {
                        continue;
                    }
                    let m = fit.topic_mastery(&bt.name);
                    let p = predict_performance(theta, agg.mean_a, agg.mean_b, &[m]);
                    num += bt.weight as f64 * p;
                    den += bt.weight as f64;
                }
            }
            if den > 0.0 {
                num / den
            } else {
                0.0
            }
        };

        let overall = eval_overall(fit.theta) as f32;
        let range_low = eval_overall(fit.theta - Z_80 * fit.theta_se).clamp(0.0, 1.0) as f32;
        let range_high = eval_overall(fit.theta + Z_80 * fit.theta_se).clamp(0.0, 1.0) as f32;

        // Per-topic breakdown across every blueprint topic (+ any extra with
        // response data).
        let mut topic_names: std::collections::BTreeSet<String> =
            blueprint.topics.iter().map(|t| t.name.clone()).collect();
        topic_names.extend(fit.by_topic.keys().cloned());
        let topics = topic_names
            .into_iter()
            .map(|name| {
                let agg = fit.by_topic.get(&name);
                let (a, b) = agg
                    .map(|g| (g.mean_a, g.mean_b))
                    .unwrap_or((DEFAULT_REPRESENTATIVE_A, DEFAULT_REPRESENTATIVE_B));
                let response_count = agg.map(|g| g.response_count).unwrap_or(0);
                let m = fit.topic_mastery(&name);
                speedrun::TopicPerformance {
                    topic: name,
                    p_correct: predict_performance(fit.theta, a, b, &[m]) as f32,
                    response_count: response_count as u32,
                    known: response_count > 0,
                }
            })
            .collect();

        let coverage = if blueprint.topics.is_empty() {
            if fit.graded_count > 0 {
                1.0
            } else {
                0.0
            }
        } else {
            let with_data = blueprint
                .topics
                .iter()
                .filter(|t| {
                    fit.by_topic
                        .get(&t.name)
                        .map(|g| g.response_count > 0)
                        .unwrap_or(false)
                })
                .count();
            with_data as f32 / blueprint.topics.len() as f32
        };

        let abstained = fit.graded_count < PERF_MIN_GRADED || coverage < PERF_MIN_COVERAGE;

        Ok(speedrun::PerformanceScoreResponse {
            topics,
            overall,
            range_low,
            range_high,
            coverage,
            graded_count: fit.graded_count as u32,
            abstained,
            theta: fit.theta as f32,
            theta_se: fit.theta_se as f32,
            synthetic: fit.synthetic,
        })
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn resp(a: f64, b: f64, correct: bool) -> QuestionResponse {
        QuestionResponse {
            a,
            b,
            correct,
            weight: 1.0,
        }
    }

    /// The 2PL formula honours the guessing floor and its monotonic shape.
    #[test]
    fn formula_respects_guessing_floor() {
        // Far below difficulty: collapses toward the floor c, never below it.
        let low = p_correct_2pl(-10.0, 1.0, 0.0);
        assert!(low >= GUESSING_FLOOR_C, "floor violated: {low}");
        assert!((low - GUESSING_FLOOR_C).abs() < 1e-3);
        // At θ == b the logistic term is 0.5 -> c + (1-c)/2 = 0.625.
        assert!((p_correct_2pl(0.0, 1.0, 0.0) - 0.625).abs() < 1e-6);
        // Far above difficulty approaches 1.
        assert!(p_correct_2pl(10.0, 1.0, 0.0) > 0.99);
        // Monotonic increasing in theta.
        assert!(p_correct_2pl(1.0, 1.5, 0.0) > p_correct_2pl(-1.0, 1.5, 0.0));
    }

    /// θ estimation recovers a known ability on a clean synthetic response set,
    /// and its SE shrinks as data grows.
    #[test]
    fn estimate_theta_recovers_ability() {
        // No data -> prior: theta 0, se == prior sd.
        let (t0, se0) = estimate_theta(&[]);
        assert!(t0.abs() < 1e-6);
        assert!((se0 - ABILITY_PRIOR_SD).abs() < 1e-6);

        // Generate responses from a true ability of +1.0 across a spread of
        // difficulties, drawing each outcome as a proper (deterministic)
        // Bernoulli sample from the true 2PL probability.
        use crate::speedrun::rng::SplitMix64;
        let true_theta = 1.0;
        let make = |n: usize| -> Vec<QuestionResponse> {
            let mut rng = SplitMix64::new(20240607);
            (0..n)
                .map(|i| {
                    let b = -2.0 + 4.0 * (i as f64) / (n as f64 - 1.0);
                    let p = p_correct_2pl(true_theta, 1.0, b);
                    resp(1.0, b, rng.next_f64() < p)
                })
                .collect()
        };
        let (_t_small, se_small) = estimate_theta(&make(40));
        let (t_large, se_large) = estimate_theta(&make(400));
        assert!(
            (t_large - true_theta).abs() < 0.4,
            "estimate {t_large} far from {true_theta}"
        );
        assert!(se_large < se_small, "SE should shrink with more data");
        assert!(se_large < se0, "SE should be below the prior with data");
    }

    /// The memory→performance gap: identical (a, b) questions get materially
    /// different P(correct) when their weakest-link mastery differs, so
    /// Performance does not collapse to a memory-only signal.
    #[test]
    fn memory_performance_gap() {
        let theta = 0.5;
        let (a, b) = (1.2, 0.0);

        // A student strong on both topics of the question.
        let strong = predict_performance(theta, a, b, &[0.9, 0.9]);
        // Same student, same question difficulty, but one weak topic (weakest
        // link). Mean mastery is still high, yet the weak link drags P down.
        let weak_link = predict_performance(theta, a, b, &[0.9, 0.1]);

        assert!(
            strong - weak_link > 0.1,
            "weakest-link mastery must open a non-trivial gap: {strong} vs {weak_link}"
        );

        // And difficulty independently matters: a harder question (higher b)
        // lowers P at the same ability + mastery.
        let easy = predict_performance(theta, a, -1.0, &[0.5, 0.5]);
        let hard = predict_performance(theta, a, 1.5, &[0.5, 0.5]);
        assert!(
            easy > hard,
            "difficulty must move performance: {easy} vs {hard}"
        );
    }

    /// Response-time weighting discounts sub-second answers but never discards
    /// them.
    #[test]
    fn engagement_weight_bounds() {
        assert_eq!(engagement_weight(0), 1.0); // unknown time -> full weight
        assert_eq!(engagement_weight(5000), 1.0); // slow -> full weight
        assert_eq!(engagement_weight(1000), 1.0);
        assert_eq!(engagement_weight(500), 0.5); // fast -> half
        assert_eq!(engagement_weight(1), MIN_ENGAGEMENT_WEIGHT); // never 0
    }
}
