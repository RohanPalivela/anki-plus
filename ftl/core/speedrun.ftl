### Strings for the Speedrun (MCAT) fork: question-gated card activation,
### coverage sweep, and the Memory model. These are currently in development.

## Undo entries (referenced by Op::describe in the Rust engine).

# Undo entry shown after a missed question activates its linked cards.
speedrun-activate-for-miss = Activate Cards for Miss
# Undo entry shown after a coverage sweep re-activates cards across topics.
speedrun-coverage-sweep = Coverage Sweep

## Menu (Tools).

speedrun-menu = Speedrun (MCAT)
speedrun-setup-action = Set up Speedrun (MCAT)…
speedrun-study-action = Study (question-first)…
speedrun-dashboard-action = Memory dashboard…

## Setup.

speedrun-setup-load-demo-prompt =
    Load synthetic demo data (placeholder practice questions + linked cards)?
speedrun-setup-complete = Speedrun (MCAT) is ready.
speedrun-setup-summary =
    Provisioned the { $notetype } notetype, the { $deck } deck, and a
    { $topicCount }-topic MCAT blueprint.
speedrun-setup-demo-summary =
    Loaded synthetic demo data: { $questionCount } practice questions and
    { $cardCount } linked flashcards.
speedrun-setup-demo-skipped = Demo data was already present, so it was left unchanged.
speedrun-setup-no-collection = Please open a collection first.

## Question-first study loop.

speedrun-study-title = Speedrun — Question-first study
speedrun-study-no-questions =
    No served questions found. Run “Set up Speedrun (MCAT)” first.
speedrun-study-progress = Question { $currentCount } of { $totalCount }
speedrun-study-topic = Topic: { $topic }
speedrun-study-submit = Submit answer
speedrun-study-correct = ✓ Correct
speedrun-study-incorrect = ✗ Incorrect
speedrun-study-explanation = Explanation
speedrun-study-why-missed = Why did you miss it?
speedrun-study-next = Next question
speedrun-study-finished = You’ve reached the end of the served questions.
speedrun-study-activated = Activated { $count } linked card(s).
speedrun-study-none-activated = No cards activated — this reason isn’t a memory gap.
speedrun-study-tally =
    Answered { $answeredCount } · Correct { $correctCount } · Cards activated { $activatedCount }
speedrun-study-run-sweep = Run coverage sweep
speedrun-study-sweep-done = Coverage sweep activated { $count } card(s).

## Miss reasons.

speedrun-miss-knowledge-gap = Knowledge gap
speedrun-miss-missing-context = Missing context
speedrun-miss-misunderstanding = Misunderstanding
speedrun-miss-careless = Careless
speedrun-miss-activates = activates linked cards
speedrun-miss-no-activation = no activation

## Memory dashboard (three-score layout).

speedrun-dashboard-title = Speedrun — Memory dashboard
speedrun-dashboard-subtitle =
    Honest, evidence-backed scores. Memory is live (M1); Performance and
    Readiness arrive in M3.
speedrun-dashboard-memory = Memory
speedrun-dashboard-performance = Performance
speedrun-dashboard-readiness = Readiness
speedrun-dashboard-coming-m3 = Coming in M3 — not yet available
speedrun-dashboard-abstaining = Abstaining — insufficient data
speedrun-dashboard-abstaining-hint =
    A score is shown once there are enough graded cards across enough topics.
speedrun-dashboard-overall = Overall mastery
speedrun-dashboard-range = 80% range
speedrun-dashboard-coverage = Topic coverage
speedrun-dashboard-graded = Graded cards
speedrun-dashboard-per-topic = Per-topic mastery
speedrun-dashboard-topic-unknown = No data yet
speedrun-dashboard-topic-cards = { $count } card(s)
speedrun-dashboard-empty =
    Nothing to show yet. Set up Speedrun and study some questions first.
speedrun-dashboard-refresh = Refresh
