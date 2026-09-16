from drills.run import Drill

DRILL = Drill(
    id='10',
    title='The secret leaks through the clock',
    area='netsec',
    difficulty='hard',
    minutes=30,
    file='app/nodes/store.py',
    good='    return hmac.compare_digest(row["secret_hash"], _hash(secret))',
    bad='    return row["secret_hash"] == _hash(secret)',
    symptom='There is no visible symptom. Every test of node authentication passes.\nCorrect secrets are accepted, wrong ones are rejected, nothing crashes,\nnothing is slow.\n\nThe only thing that changed is HOW LONG the server takes to say no.\n\nOne test fails, and it is not a behaviour test -- it reads the source.\nThat should tell you what kind of bug this is.',
    tests=['tests/test_security_invariants.py::test_node_secrets_are_compared_in_constant_time'],
    hints=['Read the failing assertion message. It names the construct that must be present and says why.', "How does Python's == compare two strings? It walks byte by byte and returns False at the first mismatch. How long does that take for a wrong first byte versus a wrong last byte?", 'Now imagine an attacker who can measure your response time to microseconds and try millions of guesses.'],
    explanation='`==` on strings short-circuits: it returns False the instant two bytes\ndiffer. So comparing against a 64-character hash takes measurably longer\nwhen the first 30 characters are right than when the first one is wrong.\n\nAn attacker guesses one character at a time, keeping whichever guess was\nfractionally slower. That turns an impossible search -- 16^64 -- into\nabout 64 x 16 guesses. Minutes, not heat-death-of-the-universe.\n\n`hmac.compare_digest` always compares every byte regardless of where the\nmismatch is, so the timing carries no information.\n\nTwo things worth noticing about this drill:\n\n1. NO BEHAVIOUR TEST CAN CATCH THIS. Both versions return identical\n   answers for identical inputs. The bug is in a dimension unit tests do\n   not observe. That is why tests/test_security_invariants.py reads the\n   source instead -- a real and widely used technique.\n\n2. This code hashes first, which reduces the practical severity. But\n   depending on a second mitigation to cover a known-unsafe primitive is\n   how vulnerabilities get built: someone later removes the hashing for a\n   good reason and never learns the comparison was load-bearing.\n\nSame rule applies to your passcode check in app/core/security.py -- go and\nlook at it, it uses compare_digest for exactly this reason.',
    concept='Compare secrets in constant time, always. Some bugs live in timing, memory or power -- dimensions your tests cannot see.',
)
