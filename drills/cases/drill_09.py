from drills.run import Drill

DRILL = Drill(
    id='09',
    title='Internal plumbing leaks to the node',
    area='backend',
    difficulty='medium',
    minutes=20,
    file='app/agents/runner.py',
    good='                arguments={k: v for k, v in arguments.items() if not k.startswith("_")},',
    bad='                arguments=arguments,',
    symptom="The operator agent asks a node to list a directory. The node refuses:\n\n    fs.list() got an unexpected keyword argument '_mission_id'\n\nThe node's own tools never declared that parameter. Something upstream is\nattaching it.",
    tests=['tests/test_nodes.py::test_agent_tool_call_is_routed_to_a_node'],
    hints=['Search the runner for _mission_id. Where does it get added to the arguments, and why?', "It is injected deliberately -- so the model cannot claim to be operating in a different mission's memory scope. It just must not travel further than this process.", "Underscore-prefixed keys are a convention here for 'internal, not part of the tool's public schema'. What should happen to them at the process boundary?"],
    explanation='`_mission_id` and `_agent_id` are injected by the runner out-of-band, so\nthat a tool always knows its true scope even if the model tries to claim a\ndifferent one. That is a security property worth keeping.\n\nBut they are internal to the orchestrator. The node has never heard of\nthem, and its tool signatures do not accept them -- so passing them\nthrough is an immediate TypeError.\n\nThe fix filters underscore-prefixed keys at the boundary:\n\n    {k: v for k, v in call.arguments.items() if not k.startswith("_")}\n\nThis is a dict comprehension, Python\'s equivalent of:\n\n    // JS\n    Object.fromEntries(\n      Object.entries(args).filter(([k]) => !k.startsWith("_"))\n    )\n\nThe general principle: every process boundary needs an explicit decision\nabout what crosses it. Internal context that is useful in-process is\nnoise -- or a leak -- once it goes over a wire.',
    concept='Filter internal fields at process boundaries. What is useful context locally is noise or a leak remotely.',
)
