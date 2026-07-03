### Strings for the Speedrun (MCAT) fork: question-gated card activation,
### coverage sweep, and the Memory model. These are currently in development.

## Undo entries (referenced by Op::describe in the Rust engine).

# Undo entry shown after a missed question activates its linked cards.
speedrun-activate-for-miss = Activate Cards for Miss
# Undo entry shown after a coverage sweep re-activates cards across topics.
speedrun-coverage-sweep = Coverage Sweep
# Undo entry shown after seeding deterministic synthetic practice responses
# (a clearly-labelled dev/test helper; never real progress).
speedrun-seed-synthetic-responses = Seed Synthetic Responses

## Menu (Tools).

speedrun-menu = Speedrun (MCAT)
speedrun-setup-action = Set up Speedrun (MCAT)…
speedrun-study-action = Study (question-first)…
speedrun-dashboard-action = Memory dashboard…
speedrun-import-bank-action = Import question bank…
# Checkable master switch for AI features (grounded rephrasal).
speedrun-ai-enable-action = Enable AI rewording
speedrun-ai-generate-cards-action = Generate AI flashcard variants…
speedrun-ai-cleanup-cards-action = Delete AI flashcard variants…

## Setup.

speedrun-setup-complete = Speedrun (MCAT) is ready.
speedrun-setup-summary =
    Provisioned the { $notetype } notetype, the { $deck } deck, and a
    { $topicCount }-topic MCAT blueprint.
speedrun-setup-demo-removed =
    Removed { $count } leftover synthetic demo note(s) — only imported questions
    are used now.
speedrun-setup-no-collection = Please open a collection first.

## Question bank import (real, legally reusable MCAT-relevant questions).

speedrun-import-bank-running = Importing question bank…
speedrun-import-bank-complete =
    Imported { $importedCount } new question(s); { $skippedCount } were already present.
speedrun-import-bank-synced =
    Stored as native notes — they will sync to your other devices automatically.
speedrun-import-bank-breakdown = Sources: { $sources }. Topics: { $topics }.
speedrun-import-bank-empty =
    No question bank file was found to import.
# Result of importing the linked first-principles memory cards alongside the bank.
speedrun-import-first-principles =
    Added { $importedCount } first-principles memory card(s) ({ $skippedCount }
    already present). They stay suspended and unlock when you miss a related
    question.

## Question-bank gate (practice requires the imported bank).

# Shown when the student tries to study/start a session before importing.
speedrun-bank-required-title = Import the question bank first
speedrun-bank-required-body =
    Speedrun practice uses the real imported MCAT question bank. Import it once
    now? It is stored as native notes and syncs to your other devices.

## Question-first study loop.

speedrun-study-title = Speedrun — Question-first study
speedrun-study-no-questions =
    No served questions found. Run “Import question bank” first.
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
speedrun-study-already-active =
    No new cards — this topic’s linked cards are already active.
speedrun-study-no-linked-cards =
    No memory cards are linked to this topic yet — add flashcards with a
    matching topic:: tag to unlock gated review.
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

speedrun-dashboard-title = Speedrun — Score dashboard
speedrun-dashboard-subtitle =
    Honest, evidence-backed scores. Every number shows a range and abstains when
    the data is too thin.
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

## Performance meter (2PL-IRT: P(correct on a new question)).

speedrun-dashboard-performance-headline = Chance of a correct answer
speedrun-dashboard-performance-overall = P(correct) on a new question
speedrun-dashboard-performance-ability = Ability (θ)
speedrun-dashboard-performance-per-topic = Per-topic P(correct)
speedrun-dashboard-performance-responses = { $count } response(s)
speedrun-dashboard-performance-abstaining-hint =
    A score is shown once you've answered enough questions across enough topics.

## Readiness meter (projected MCAT score).

speedrun-dashboard-readiness-headline = Projected MCAT score
speedrun-dashboard-readiness-interval = 80% range
speedrun-dashboard-readiness-raw = Raw correct
speedrun-dashboard-readiness-confidence = Confidence
speedrun-dashboard-readiness-reasons = What's driving this
speedrun-dashboard-readiness-abstaining-hint =
    A projection is shown once coverage and answer counts are high enough and the
    range is tight enough to be meaningful.

## Synthetic seed data (dev/test) — always labelled, never silent.

speedrun-synthetic-badge = Synthetic data
speedrun-synthetic-hint =
    This score includes synthetic seed responses (a dev/test helper), not real
    study progress.
# Destructive action: wipe the learner's progress but keep imported content.
speedrun-reset-profile-action = Reset profile
speedrun-reset-profile-confirm =
    Reset your Speedrun progress? This re-suspends every activated memory card
    and clears their review history, so Memory starts from scratch. Your imported
    question bank and memory cards are kept. This cannot be undone.
speedrun-reset-profile-done =
    Profile reset — { $resuspendedCount } card(s) re-suspended, { $forgottenCount }
    card(s) cleared. Imported questions and cards were kept.
# Shown when opening the dashboard while FSRS scheduling is disabled.
speedrun-dashboard-fsrs-off =
    FSRS is turned off, so the Memory model has no data to score. Enable FSRS in
    Deck Options, then review activated memory cards — Memory and coverage build
    from FSRS retention, not from answering questions.

## Tier 2 — MCAT home screen (the default landing).

speedrun-home-title = MCAT Anki-Plus
speedrun-home-tagline =
    Question-first MCAT practice that decides what to study next.
speedrun-home-start = Start session
speedrun-home-start-hint = Practice → memory cards → recap
speedrun-home-open-dashboard = Full Memory dashboard
speedrun-home-open-decks = Decks (standard Anki)
# Top-toolbar link back to the MCAT home.
speedrun-home-link = Home
speedrun-home-link-tip = MCAT Anki-Plus home

## Tier 2 — curriculum (topic → concept navigation).

speedrun-home-curriculum-title = Your curriculum
speedrun-home-curriculum-hint =
    Pick a topic or concept to focus on, or press Start for a smart mix that
    targets your weak spots.
speedrun-home-curriculum-empty =
    Import the question bank to build your curriculum.
speedrun-curriculum-mastery = Mastery
speedrun-curriculum-mastery-unknown = No data yet
speedrun-curriculum-questions = { $count } questions
speedrun-curriculum-lessons = { $count } lessons
speedrun-curriculum-accuracy = { $percent }% accurate
speedrun-curriculum-not-started = Not started
speedrun-curriculum-study = Study
speedrun-curriculum-study-topic = Study topic
speedrun-curriculum-lessons-active = { $activated }/{ $total } lessons active

## Tier 2 — guided session (fixed Practice → Flashcards → Recap sequence).

speedrun-session-practice-title = Practice questions
speedrun-session-recap-title = Recap — transfer check
speedrun-session-continue = Continue
speedrun-session-stop = Stop session
speedrun-session-stopped = Speedrun session stopped.
speedrun-session-paused =
    Progress saved. Press Start to pick up where you left off.
speedrun-session-nothing =
    Nothing to study yet — run Tools → Speedrun (MCAT) → Import question bank first.
speedrun-session-complete = Session complete.
speedrun-session-complete-detail =
    You answered { $answeredCount } question(s) ({ $correctCount } correct) and
    reviewed { $reviewedCount } memory card(s).
speedrun-session-complete-topics = Topics studied: { $topics }.

## AI rewording (grounded flashcard / question variants; opt-in, off by default).

speedrun-ai-enabled-on =
    AI rewording enabled. Grounded, source-checked variants only; the app still
    scores with AI off.
speedrun-ai-enabled-off = AI rewording disabled.
speedrun-ai-disabled-body =
    Turn on Tools → Speedrun (MCAT) → Enable AI rewording first.
speedrun-ai-unavailable-body =
    To use AI features, set the OPENAI_API_KEY environment variable and install
    the “openai” package (pip install openai), then restart Anki. Runs offline
    with AI off.
speedrun-ai-no-sources =
    No first-principles memory cards found to reword. Import the question bank
    first.
speedrun-ai-generate-running = Generating AI flashcard variants…
speedrun-ai-generate-complete =
    Wrote { $writtenCount } reworded flashcard variant(s) from { $consideredCount }
    source card(s); { $blockedCount } candidate(s) blocked by the quality gate.
    Variants are suspended, sync like their source, and are excluded from the
    Memory score.
speedrun-ai-generate-skipped-note =
    { $skippedCount } already existed and were skipped (re-running is idempotent).
speedrun-ai-generate-error-note =
    { $errorCount } source card(s) could not be reworded — the AI provider
    rejected the request. First error: { $firstError }
speedrun-ai-cleanup-none = No AI-generated flashcard variants to delete.
speedrun-ai-cleanup-confirm =
    Delete { $count } AI-generated flashcard variant(s)? Your source cards and the
    question bank are not affected. This can be undone.
speedrun-ai-cleanup-complete = Deleted { $count } AI-generated flashcard variant(s).
