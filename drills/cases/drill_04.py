from drills.run import Drill

DRILL = Drill(
    id='04',
    title='A runaway agent burns the daily quota',
    area='backend',
    difficulty='medium',
    minutes=25,
    file='app/agents/runner.py',
    good='                if result.tool_calls >= budget.max_tool_calls:',
    bad='                if result.tool_calls >= budget.max_tool_calls * 100:',
    symptom='An agent gets stuck in a loop calling memory.read over and over. Its\nbudget says max_tool_calls: 10, but the logs show dozens of calls before\nanything stops it.\n\nIn production this is what empties a 14,400-request-per-day free tier in\nabout a minute. The safety net is not catching.',
    tests=['tests/test_agents.py::test_tool_call_budget_is_enforced'],
    hints=['The test sets max_tool_calls=2 and scripts six tool calls, then asserts only two ran. Find where the runner counts tool calls against the budget.', 'The comparison is there and looks structurally right. Read the right-hand side of it very carefully.', 'Budgets exist because a model that misbehaves is not an exception -- it is Tuesday. A guard that can be silently widened is not a guard.'],
    explanation='The check was still present and still executed -- it had just been made\nunreachable in practice by inflating the limit 100x.\n\nThis is worth internalising because it is how safety code dies in real\ncodebases. Nobody deletes the guard; someone widens it "temporarily" to\nunblock something, and it never comes back. The code still LOOKS safe in\nreview.\n\nTwo defences:\n  1. Tests that assert the limit binds -- exactly what caught this.\n  2. Keep the limit and the comparison adjacent, so a change to either is\n     visible in the diff.\n\nIn this codebase MAX_ITERATIONS in the runner is a second, independent\nceiling for the same reason: two guards from different angles.',
    concept='Safety limits need tests that prove they actually bind. A guard nobody verifies is decoration.',
)
