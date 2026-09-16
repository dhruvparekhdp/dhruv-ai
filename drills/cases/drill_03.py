from drills.run import Drill

DRILL = Drill(
    id='03',
    title='Node commands hang forever',
    area='async',
    difficulty='medium',
    minutes=25,
    file='app/nodes/gateway.py',
    good='            await socket.send_text(json.dumps({\n                "type": "dispatch",',
    bad='            socket.send_text(json.dumps({\n                "type": "dispatch",',
    symptom="Ask the operator agent to check disk space on a node. The node is\nconnected -- /api/v1/nodes shows it online. But the call sits there and\neventually fails with:\n\n    node bench-01 did not answer 'system.stats' within 60s\n\nCheck the node's own log: it never received anything. The orchestrator\nthinks it sent a command. The node disagrees.\n\nPython may also print: RuntimeWarning: coroutine was never awaited",
    tests=['tests/test_nodes.py::test_dispatch_round_trip'],
    hints=['The node never received the message. So the failure is on the sending side -- look at NodeGateway.dispatch().', 'In Python, calling an async function does NOT run it. It builds a coroutine object and hands it to you, inert. Something has to drive it.', 'Compare the line that sends the dispatch with the other awaits around it. What is missing?'],
    explanation="Calling an `async def` function returns a coroutine object -- it does not\nexecute anything. `await` is what hands it to the event loop to actually run.\n\n    socket.send_text(payload)          # builds a coroutine, drops it. Nothing sent.\n    await socket.send_text(payload)    # actually sends\n\nThis is the single most common async Python mistake, and it usually fails\nSILENTLY -- you get a RuntimeWarning buried in the logs, and a feature that\nsimply does nothing.\n\nJavaScript is more forgiving here: calling an async function starts it\nimmediately and returns a Promise, so forgetting `await` still runs the\ncode -- you just do not wait for it. In Python nothing runs at all. That\ndifference bites JS developers constantly.\n\n    // JS: runs, you just don't wait\n    sendText(payload);\n\n    # Python: does not run\n    send_text(payload)",
    concept='In Python a coroutine does nothing until awaited. In JS it starts immediately. Forgetting await fails louder in Python and more silently in JS.',
)
