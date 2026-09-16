from drills.run import Drill

DRILL = Drill(
    id='08',
    title='Dangerous tools stop asking permission',
    area='netsec',
    difficulty='medium',
    minutes=20,
    file='app/tools/registry.py',
    good='    if tool.risk is Risk.DANGEROUS:\n        return PermissionVerdict(True, needs_approval=True, reason="dangerous tool requires approval")',
    bad='    if tool.risk is Risk.SENSITIVE:\n        return PermissionVerdict(True, needs_approval=True, reason="dangerous tool requires approval")',
    symptom='An agent calls shell.exec. No approval card appears in the PWA. The\ncommand just runs on the node.\n\nMeanwhile memory.write -- which should be routine -- now blocks and waits\nfor you to approve it every single time.\n\nThe approval system is working. It is guarding the wrong things.',
    tests=['tests/test_agents.py::test_dangerous_tool_always_needs_approval', 'tests/test_nodes.py::test_dangerous_node_tools_require_approval'],
    hints=['Both symptoms point the same way: the risk levels have been swapped somewhere, not broken.', 'check_permission() in app/tools/registry.py decides this. There are three risk levels; the function only mentions one by name.', 'Read that one line and ask which level it names, and which level SHOULD trigger a human decision.'],
    explanation='One enum member swapped for another. The mechanism worked perfectly and\nprotected entirely the wrong set of operations.\n\nThe three levels and what they mean:\n\n    SAFE       read-only, no side effects        runs\n    SENSITIVE  real but reversible effects       runs if granted, always audited\n    DANGEROUS  irreversible or high-impact       ALWAYS parks for a human\n\n    memory.write   SENSITIVE   you can overwrite it again\n    memory.forget  DANGEROUS   the data is gone\n    fs.write       SENSITIVE   reversible\n    fs.delete      DANGEROUS   not reversible\n    shell.exec     DANGEROUS   arbitrary code on your hardware\n\nNote the second symptom is the more dangerous one operationally. When\nroutine actions start demanding approval, users learn to click Approve\nwithout reading. Then the one that mattered gets waved through too.\nAlarm fatigue is a security failure, not a UX complaint.',
    concept='Classify by reversibility, and keep the gate narrow. A gate that fires on everything trains people to ignore it.',
)
