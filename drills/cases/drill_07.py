from drills.run import Drill

DRILL = Drill(
    id='07',
    title='Untrusted code runs on the wrong machine',
    area='os-security',
    difficulty='medium',
    minutes=25,
    file='app/nodes/gateway.py',
    good='            if untrusted and not node.untrusted_ok:\n                continue',
    bad='            if untrusted and not node.untrusted_ok:\n                pass',
    symptom='You have two nodes: your main laptop (holds Postgres and all your\nmemory) and a spare designated as the code sandbox.\n\nYou revoke the sandbox to test something. Then you ask the coding agent to\nrun a generated script -- expecting it to be refused, because no node is\ncleared for untrusted code any more.\n\nIt runs anyway. On the main laptop.',
    tests=['tests/test_nodes.py::test_untrusted_work_refuses_a_node_without_consent'],
    hints=['Placement happens in NodeGateway.select(). Read the loop that filters candidate nodes.', 'The untrusted check is present and the condition is correct. Look at what the body of the if actually does.', 'In a filter loop, what is the difference between `continue` and `pass`? Say each one out loud as an English sentence.'],
    explanation='`pass` means "do nothing" -- execution falls through to the next line,\nwhich appends the node as a candidate. `continue` means "skip to the next\niteration" -- the node is never appended.\n\n    if untrusted and not node.untrusted_ok:\n        pass          # noted, and then added anyway\n        continue      # actually excluded\n\nA one-word change that silently inverts a security boundary while leaving\ncode that reads correctly at a glance. This is why the test matters more\nthan the review.\n\nThe design principle underneath: when the safe answer is unavailable,\nREFUSE rather than substitute. Falling back to a trusted machine is worse\nthan failing, because the user asked for isolation and got the appearance\nof it. Everywhere in this codebase, a missing capability produces an\nhonest refusal:\n\n    no node cleared for untrusted code   -> refused\n    no node connected at all             -> refused\n    no API key                           -> says it is on the fallback engine',
    concept='When the safe option is unavailable, refuse. A silent fallback to the unsafe option is worse than an error.',
)
