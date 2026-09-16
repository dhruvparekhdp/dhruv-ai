from drills.run import Drill

DRILL = Drill(
    id='01',
    title='The chart draws backwards',
    area='python',
    difficulty='easy',
    minutes=20,
    file='app/core/telemetry.py',
    good='    return datetime.now(timezone.utc).isoformat(timespec="microseconds")',
    bad='    return datetime.now(timezone.utc).isoformat(timespec="seconds")',
    symptom="Your mood trend strip shows the OLDEST check-in on the right and the\nnewest on the left -- backwards. But only sometimes. If you check in\nonce in the morning and once at night, it looks fine. If you tap three\nmoods quickly to test it, the order is scrambled.\n\nThe API says entries come back newest-first. They don't.",
    tests=['tests/test_telemetry.py::test_mood_history_is_newest_first_within_the_same_second'],
    hints=['The failing test writes five check-ins in a tight loop. What do all five share that two check-ins hours apart would not?', 'Look at the SQL in mood_history(): `ORDER BY created_at DESC, checkin_id DESC`. If created_at ties for every row, what is actually deciding the order?', 'checkin_id is a uuid4 -- random. Now go and look at how created_at is generated. How much precision does it carry?'],
    explanation='This is a real bug that shipped in this project and had to be found the hard way.\n\n`isoformat(timespec="seconds")` produces "2026-08-26T14:03:07+00:00" -- one\nsecond of resolution. Five check-ins written in the same second get five\nIDENTICAL timestamps. The ORDER BY then falls through to the tiebreaker,\n`checkin_id DESC`, which is a random UUID. The order becomes arbitrary.\n\nWorse: the original test asserted the right thing and PASSED, because with\nthree rows a random order has a decent chance of looking correct. It was\npassing by luck.\n\n`timespec="microseconds"` gives six decimal places, so ties essentially\nstop happening. Fixed width matters too -- SQLite sorts these as TEXT, so\n"...07+00:00" and "...07.123456+00:00" would sort wrongly if the format\nvaried between rows.',
    concept='When you sort by a value, ask what happens when it ties. A tiebreaker that is random makes the sort random.',
)
