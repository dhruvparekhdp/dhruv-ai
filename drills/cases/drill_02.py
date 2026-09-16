from drills.run import Drill

DRILL = Drill(
    id='02',
    title='Dependent tasks never run',
    area='python',
    difficulty='easy',
    minutes=20,
    file='app/missions/scheduler.py',
    good='            if all(d is not None and d.status is TaskStatus.SUCCESS for d in dependencies):',
    bad='            if all(d is not None and d.status is "SUCCESS" for d in dependencies):',
    symptom='You give Jarvis a goal. The planner produces two tasks: research the\nthing, then summarise it. The first task succeeds. The second never\nstarts -- the mission finishes with one task done and one still PENDING.\n\nNo error. No exception. The scheduler simply never considers the second\ntask ready.',
    tests=['tests/test_missions.py::test_mission_runs_dependent_tasks_in_order'],
    hints=['A task becomes runnable when all its dependencies have succeeded. Find the function that decides that -- _ready_tasks().', "The comparison there uses `is`. In Python, `is` asks 'are these the same object in memory?', not 'are these equal?'", "Try it in a REPL:  x = 'SUCC' + 'ESS'  then  x is 'SUCCESS'  and  x == 'SUCCESS'. Two different answers."],
    explanation='`is` compares IDENTITY -- the same object in memory. `==` compares VALUE.\n\n    status is "SUCCESS"    # is this the exact same string object?\n    status == "SUCCESS"    # do these have the same value?\n\nCPython interns some short strings, so `is` sometimes appears to work,\nwhich is what makes this bug so nasty -- it can pass in a REPL and fail in\nproduction with a string built at runtime.\n\nThe correct code compares against the enum member:\n\n    d.status is TaskStatus.SUCCESS\n\n`is` is right HERE because enum members are singletons -- there is exactly\none TaskStatus.SUCCESS object. That is the one case where identity is the\ncorrect question.\n\nJavaScript comparison: `is` is roughly `===` on object references, while\n`==` is closer to a deep value comparison. JS has no direct equivalent of\nPython\'s enum-singleton identity check.',
    concept='Use `is` only for singletons: None, True, False, and enum members. Everything else uses ==.',
)
