from drills.run import Drill

DRILL = Drill(
    id='05',
    title='One join token, many nodes',
    area='netsec',
    difficulty='medium',
    minutes=25,
    file='app/nodes/store.py',
    good='        if row is None or row["used_at"] is not None:\n            return False',
    bad='        if row is None:\n            return False',
    symptom='You mint one enrolment token and give it to a laptop. It joins fine.\n\nThen you notice /api/v1/nodes lists FOUR nodes -- because you pasted the\nsame token into your terminal history a few times while testing, and every\nattempt enrolled successfully.\n\nA token that was meant to be used once is being accepted forever.',
    tests=['tests/test_nodes.py::test_enrolment_token_is_single_use', 'tests/test_nodes.py::test_reused_enrolment_token_is_refused'],
    hints=['Enrolment goes through redeem_enrolment_token(). Read it and list every reason it might return False.', 'The table has a used_at column, and the function does write to it. So the marking works -- what about the reading?', "Ask what a credential's lifecycle is: issued, used, spent. Which of those three states does this code actually distinguish?"],
    explanation='The token was still being MARKED used -- `used_at` was written correctly.\nIt just was not being CHECKED on the way in.\n\nThat asymmetry is what makes this class of bug survive review: the write\nside looks complete, so the feature appears implemented. Nobody notices\nthe read side never asks.\n\nWhy single-use matters here: an enrolment token grants the right to join\nyour fleet and receive a long-lived node secret. If it leaks -- shell\nhistory, a screenshot, a chat message -- anyone who finds it can enrol\ntheir own machine into your cluster.\n\nDefence in depth in this codebase:\n  - single use (this check)\n  - 30-minute expiry (the check just below it)\n  - hashed at rest, so the database does not hold usable tokens\n\nAny one of those failing still leaves two others standing. That is the\npoint of layering.',
    concept='For any credential, write down its lifecycle and check that EVERY transition is enforced on read, not just recorded on write.',
)
