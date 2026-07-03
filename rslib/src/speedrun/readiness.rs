// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Readiness model (§M3b): **projected MCAT score + range**.
//!
//! A Monte-Carlo simulation over the MCAT blueprint. Each iteration:
//!
//! 1. samples an ability `θ` from its posterior `Normal(θ, se)` (from the
//!    Performance fit);
//! 2. allocates the exam's items to blueprint topics by their weights;
//! 3. for each item, computes `P(correct)` with the Performance 2PL (blending
//!    `θ` with the topic's mastery) and samples a Bernoulli outcome;
//! 4. converts the raw proportion correct to the MCAT scaled score (472–528)
//!    through a documented, monotonic piecewise-linear concordance curve.
//!
//! The spread of the resulting scaled-score distribution gives the **80%
//! interval**. Uncovered topics use a low prior mastery *and* extra per-sim
//! variance, so thin coverage both lowers the score and widens the interval —
//! confidence is tied to coverage. Everything is driven by a **fixed seed** for
//! reproducible, testable output.
//!
//! ## Concordance curve (approximate — adjustable)
//!
//! The raw→scaled anchors below are a *stated approximation* of the MCAT total
//! concordance, not official AAMC data (which is copyrighted). They should be
//! replaced with real percentile↔scaled data if/when licensed. They are
//! monotonic and span the full 472–528 range; ~50% correct ≈ 500 (near the
//! historical median), 100% ≈ 528, 0% ≈ 472.

use anki_proto::speedrun;

use crate::prelude::*;
use crate::speedrun::performance::predict_performance;
use crate::speedrun::performance::PerformanceFit;
use crate::speedrun::performance::DEFAULT_REPRESENTATIVE_A;
use crate::speedrun::performance::DEFAULT_REPRESENTATIVE_B;
use crate::speedrun::rng::SplitMix64;

/// MCAT scaled-score bounds.
const MCAT_MIN: f64 = 472.0;
const MCAT_MAX: f64 = 528.0;

/// Number of Monte-Carlo iterations. 2000 gives stable median / 10th–90th
/// percentiles while staying well within the dashboard latency budget.
const READINESS_SIMS: usize = 2000;

/// Fixed PRNG seed: identical output on every run and every device (desktop and
/// Android share this engine), which the determinism test pins.
const READINESS_SEED: u64 = 0x5EED_1234_ABCD_0001;

/// Scored items in a simulated exam. The MCAT has 230 scored questions.
const EXAM_ITEMS: usize = 230;

/// Prior mastery for a blueprint topic never exercised by questions/cards.
const UNCOVERED_PRIOR_MASTERY: f64 = 0.2;
/// Per-sim SD applied to an uncovered topic's mastery, so missing coverage
/// widens the interval (ties confidence to coverage).
const UNCOVERED_PRIOR_SD: f64 = 0.15;

/// Scaled-score interval width (points) at/above which confidence hits 0.
const CONFIDENCE_WIDTH_REF: f64 = 40.0;

/// Abstention defaults (overridable via collection config, mirroring the
/// sweep/cap config pattern).
const DEFAULT_READINESS_MIN_GRADED: usize = 10;
const DEFAULT_READINESS_MIN_COVERAGE: f64 = 0.5;
const DEFAULT_READINESS_MAX_WIDTH: f64 = 30.0;

/// Config keys for the abstention thresholds (camelCase to match siblings).
const READINESS_MIN_GRADED_CONFIG_KEY: &str = "speedrunReadinessMinGraded";
const READINESS_MIN_COVERAGE_CONFIG_KEY: &str = "speedrunReadinessMinCoverage";
const READINESS_MAX_WIDTH_CONFIG_KEY: &str = "speedrunReadinessMaxWidth";

/// Documented, monotonic raw→scaled concordance anchors (approximate; see the
/// module docs). `(proportion_correct, scaled_score)`, strictly increasing.
const CONCORDANCE: &[(f64, f64)] = &[
    (0.00, 472.0),
    (0.30, 486.0),
    (0.50, 500.0),
    (0.65, 508.0),
    (0.80, 515.0),
    (0.90, 521.0),
    (1.00, 528.0),
];

/// Map a raw proportion correct (0..1) to a scaled MCAT score via piecewise-
/// linear interpolation of [`CONCORDANCE`] (clamped to the exam's range).
pub(crate) fn raw_to_scaled(p: f64) -> f64 {
    let p = p.clamp(0.0, 1.0);
    for pair in CONCORDANCE.windows(2) {
        let (x0, y0) = pair[0];
        let (x1, y1) = pair[1];
        if p <= x1 {
            let t = if x1 > x0 { (p - x0) / (x1 - x0) } else { 0.0 };
            return (y0 + t * (y1 - y0)).clamp(MCAT_MIN, MCAT_MAX);
        }
    }
    MCAT_MAX
}

/// One blueprint topic's simulation inputs.
struct TopicPlan {
    name: String,
    items: usize,
    a: f64,
    b: f64,
    /// FSRS mastery if known.
    mastery: Option<f64>,
    weight: f64,
}

/// Nearest-rank percentile of a pre-sorted slice.
fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 - 1.0) * q).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

impl Collection {
    fn readiness_min_graded(&self) -> usize {
        self.get_config_optional::<i64, _>(READINESS_MIN_GRADED_CONFIG_KEY)
            .filter(|v| *v >= 0)
            .map(|v| v as usize)
            .unwrap_or(DEFAULT_READINESS_MIN_GRADED)
    }

    fn readiness_min_coverage(&self) -> f64 {
        self.get_config_optional::<f64, _>(READINESS_MIN_COVERAGE_CONFIG_KEY)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(DEFAULT_READINESS_MIN_COVERAGE)
    }

    fn readiness_max_width(&self) -> f64 {
        self.get_config_optional::<f64, _>(READINESS_MAX_WIDTH_CONFIG_KEY)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(DEFAULT_READINESS_MAX_WIDTH)
    }

    /// Compute the Readiness score (proto). Read-only.
    pub(crate) fn speedrun_readiness_score(&mut self) -> Result<speedrun::ReadinessScoreResponse> {
        let fit = self.speedrun_fit_performance()?;
        let blueprint = self.get_speedrun_blueprint();

        // Coverage (SPOV3 sense): a topic is exercised if it has question
        // responses OR activated-and-studied memory cards (mastery known).
        let covered = |topic: &str| -> bool {
            fit.by_topic
                .get(topic)
                .map(|g| g.response_count > 0)
                .unwrap_or(false)
                || fit.mastery.contains_key(topic)
        };
        let coverage = if blueprint.topics.is_empty() {
            if fit.graded_count > 0 {
                1.0
            } else {
                0.0
            }
        } else {
            blueprint.topics.iter().filter(|t| covered(&t.name)).count() as f32
                / blueprint.topics.len() as f32
        };

        // Allocate exam items to topics by weight (largest-remainder so the
        // total is exactly EXAM_ITEMS). Fall back to a single neutral topic when
        // there is no blueprint.
        let plans = self.build_topic_plans(&fit, &blueprint);

        // Run the simulation.
        let mut rng = SplitMix64::new(READINESS_SEED);
        let total_items: usize = plans.iter().map(|p| p.items).sum::<usize>().max(1);
        let mut raws: Vec<f64> = Vec::with_capacity(READINESS_SIMS);
        let mut scaleds: Vec<f64> = Vec::with_capacity(READINESS_SIMS);
        for _ in 0..READINESS_SIMS {
            let theta = fit.theta + fit.theta_se * rng.next_normal();
            let mut correct = 0usize;
            for plan in &plans {
                // Uncovered topics draw a fresh, wide mastery each sim.
                let mastery = match plan.mastery {
                    Some(m) => m,
                    None => (UNCOVERED_PRIOR_MASTERY + UNCOVERED_PRIOR_SD * rng.next_normal())
                        .clamp(0.0, 1.0),
                };
                let p = predict_performance(theta, plan.a, plan.b, &[mastery]);
                for _ in 0..plan.items {
                    if rng.next_f64() < p {
                        correct += 1;
                    }
                }
            }
            let raw = correct as f64 / total_items as f64;
            raws.push(raw);
            scaleds.push(raw_to_scaled(raw));
        }
        raws.sort_by(|a, b| a.partial_cmp(b).unwrap());
        scaleds.sort_by(|a, b| a.partial_cmp(b).unwrap());

        let scaled_median = percentile(&scaleds, 0.50);
        let scaled_low = percentile(&scaleds, 0.10);
        let scaled_high = percentile(&scaleds, 0.90);
        let raw_median = percentile(&raws, 0.50);
        let width = scaled_high - scaled_low;

        // Confidence: coverage scaled down as the interval widens.
        let width_factor = (1.0 - (width / CONFIDENCE_WIDTH_REF)).clamp(0.0, 1.0);
        let confidence = (coverage as f64 * width_factor).clamp(0.0, 1.0) as f32;

        // Per-topic point projection (at the fitted ability).
        let topics = plans
            .iter()
            .map(|plan| {
                let mastery = plan.mastery.unwrap_or(UNCOVERED_PRIOR_MASTERY);
                speedrun::ReadinessTopic {
                    topic: plan.name.clone(),
                    p_correct: predict_performance(fit.theta, plan.a, plan.b, &[mastery]) as f32,
                    weight: plan.weight as f32,
                    known: plan.mastery.is_some(),
                }
            })
            .collect();

        let min_graded = self.readiness_min_graded();
        let min_coverage = self.readiness_min_coverage();
        let max_width = self.readiness_max_width();
        let abstained =
            fit.graded_count < min_graded || (coverage as f64) < min_coverage || width > max_width;

        let top_reasons = readiness_reasons(&fit, &blueprint, coverage, width, max_width);

        Ok(speedrun::ReadinessScoreResponse {
            scaled_median: scaled_median as f32,
            scaled_low: scaled_low as f32,
            scaled_high: scaled_high as f32,
            raw_median: raw_median as f32,
            coverage,
            confidence,
            graded_count: fit.graded_count as u32,
            abstained,
            synthetic: fit.synthetic,
            topics,
            top_reasons,
        })
    }

    /// Build the per-topic simulation plans (item allocation + representative
    /// IRT params + mastery) from the blueprint and the Performance fit.
    fn build_topic_plans(
        &self,
        fit: &PerformanceFit,
        blueprint: &crate::speedrun::blueprint::Blueprint,
    ) -> Vec<TopicPlan> {
        if blueprint.topics.is_empty() {
            // No blueprint: a single neutral topic carrying the whole exam.
            return vec![TopicPlan {
                name: String::new(),
                items: EXAM_ITEMS,
                a: DEFAULT_REPRESENTATIVE_A,
                b: DEFAULT_REPRESENTATIVE_B,
                mastery: None,
                weight: 1.0,
            }];
        }

        let total_weight: f64 = blueprint.topics.iter().map(|t| t.weight as f64).sum();
        let total_weight = if total_weight > 0.0 {
            total_weight
        } else {
            1.0
        };

        // Largest-remainder apportionment so items sum to exactly EXAM_ITEMS.
        let mut plans: Vec<TopicPlan> = Vec::with_capacity(blueprint.topics.len());
        let mut remainders: Vec<(f64, usize)> = Vec::with_capacity(blueprint.topics.len());
        let mut allocated = 0usize;
        for (i, bt) in blueprint.topics.iter().enumerate() {
            let exact = bt.weight as f64 / total_weight * EXAM_ITEMS as f64;
            let floor = exact.floor();
            allocated += floor as usize;
            remainders.push((exact - floor, i));
            let (a, b) = fit
                .by_topic
                .get(&bt.name)
                .map(|g| (g.mean_a, g.mean_b))
                .unwrap_or((DEFAULT_REPRESENTATIVE_A, DEFAULT_REPRESENTATIVE_B));
            plans.push(TopicPlan {
                name: bt.name.clone(),
                items: floor as usize,
                a,
                b,
                mastery: fit.mastery.get(&bt.name).copied(),
                weight: bt.weight as f64,
            });
        }
        // Hand out the leftover items to the largest fractional remainders.
        let mut leftover = EXAM_ITEMS.saturating_sub(allocated);
        remainders.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        let mut ri = 0;
        while leftover > 0 && !remainders.is_empty() {
            plans[remainders[ri % remainders.len()].1].items += 1;
            leftover -= 1;
            ri += 1;
        }
        plans
    }
}

/// Up to three short diagnostic phrases behind the score/abstention.
fn readiness_reasons(
    fit: &PerformanceFit,
    blueprint: &crate::speedrun::blueprint::Blueprint,
    coverage: f32,
    width: f64,
    max_width: f64,
) -> Vec<String> {
    let mut reasons = Vec::new();

    if !blueprint.topics.is_empty() && coverage < 1.0 {
        let exercised = (coverage * blueprint.topics.len() as f32).round() as usize;
        reasons.push(format!(
            "Only {exercised} of {} blueprint topics exercised",
            blueprint.topics.len()
        ));
    }

    // Weakest known topic (by FSRS mastery).
    if let Some((topic, mastery)) = fit
        .mastery
        .iter()
        .min_by(|a, b| a.1.partial_cmp(b.1).unwrap())
    {
        reasons.push(format!(
            "Weakest area: {topic} ({}%)",
            (mastery * 100.0).round() as i32
        ));
    }

    if width > max_width {
        reasons.push("Wide range — more practice needed to narrow it".to_string());
    }

    if fit.synthetic {
        reasons.push("Includes synthetic seed data (not real progress)".to_string());
    }

    reasons.truncate(3);
    reasons
}

#[cfg(test)]
mod test {
    use super::*;

    /// Concordance is monotonic and spans the MCAT range.
    #[test]
    fn concordance_is_monotonic() {
        assert_eq!(raw_to_scaled(0.0), MCAT_MIN);
        assert_eq!(raw_to_scaled(1.0), MCAT_MAX);
        assert!((raw_to_scaled(0.5) - 500.0).abs() < 1e-6);
        let mut prev = raw_to_scaled(0.0);
        let mut p = 0.0;
        while p <= 1.0 {
            let s = raw_to_scaled(p);
            assert!(s >= prev - 1e-9, "not monotonic at {p}: {s} < {prev}");
            prev = s;
            p += 0.01;
        }
        // Out-of-range inputs clamp.
        assert_eq!(raw_to_scaled(-1.0), MCAT_MIN);
        assert_eq!(raw_to_scaled(2.0), MCAT_MAX);
    }

    /// The PRNG (hence the whole simulation) is deterministic under the fixed
    /// seed and produces a plausible spread.
    #[test]
    fn prng_is_deterministic() {
        let mut a = SplitMix64::new(READINESS_SEED);
        let mut b = SplitMix64::new(READINESS_SEED);
        for _ in 0..1000 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
        // Uniform draws stay in range; normal draws have ~0 mean.
        let mut r = SplitMix64::new(1);
        let mut sum = 0.0;
        for _ in 0..10_000 {
            let u = r.next_f64();
            assert!((0.0..1.0).contains(&u));
            sum += r.next_normal();
        }
        assert!(
            (sum / 10_000.0).abs() < 0.1,
            "normal mean off: {}",
            sum / 10_000.0
        );
    }

    #[test]
    fn percentile_nearest_rank() {
        let v: Vec<f64> = (0..=100).map(|i| i as f64).collect();
        assert_eq!(percentile(&v, 0.0), 0.0);
        assert_eq!(percentile(&v, 0.5), 50.0);
        assert_eq!(percentile(&v, 1.0), 100.0);
        assert_eq!(percentile(&[], 0.5), 0.0);
    }
}
