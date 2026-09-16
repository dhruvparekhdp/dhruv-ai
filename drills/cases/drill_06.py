from drills.run import Drill

DRILL = Drill(
    id='06',
    title='The sandbox is not a sandbox',
    area='os-security',
    difficulty='hard',
    minutes=35,
    file='nodes/pc/agent.py',
    good='        path = Path(raw).expanduser().resolve()\n        if not any(path == root or root in path.parents for root in self.roots):\n            allowed = ", ".join(str(r) for r in self.roots)\n            raise ToolError(f"path outside the allowed roots ({allowed}): {path}")\n        return path',
    bad='        path = Path(raw).expanduser()\n        if not any(str(path).startswith(str(root)) for root in self.roots):\n            allowed = ", ".join(str(r) for r in self.roots)\n            raise ToolError(f"path outside the allowed roots ({allowed}): {path}")\n        return path.resolve()',
    symptom="The node agent is started with --root ~/jarvis-workspace, so it should\nonly ever touch files in there. Direct attempts are correctly refused:\n\n    fs.read /etc/passwd            -> refused, good\n\nBut this succeeds and returns the real file:\n\n    fs.read ~/jarvis-workspace/../../etc/passwd\n\nThe jail has a hole. Anything the node's user can read is reachable.",
    tests=['tests/test_nodes.py::test_node_path_jail_blocks_traversal', 'tests/test_security_invariants.py::test_node_agent_resolves_paths_before_checking_them'],
    hints=['Both paths point at the same file. One is refused, one is not. So the check is not looking at the file -- it is looking at the text.', "Print what the string actually is before the check runs. '/home/dp/jarvis-workspace/../../etc/passwd' -- does that start with '/home/dp/jarvis-workspace'?", 'There are two operations here: resolve (collapse .. and symlinks) and check (is it inside a root). Only the ORDER is wrong.'],
    explanation='The bug is ordering, not logic. The broken version tests the raw string,\nthen resolves afterwards:\n\n    check("/home/dp/jarvis-workspace/../../etc/passwd")  -> starts with the root, allowed\n    resolve()                                            -> /etc/passwd, too late\n\nThe correct version resolves FIRST, so `..` is collapsed before the\nquestion is asked:\n\n    resolve()  -> /etc/passwd\n    check()    -> not inside any root, refused\n\nTwo further details in the correct version:\n\n`str.startswith` is the wrong test even after resolving. "/home/dp/jarvis-workspace-evil"\nstarts with "/home/dp/jarvis-workspace" but is a different directory. Comparing\nPath objects with `root in path.parents` compares path COMPONENTS, so it cannot\nbe fooled that way.\n\n`.resolve()` also follows symlinks -- so a symlink inside the workspace pointing\nat /etc cannot be used to escape either.\n\nThis is CWE-22, path traversal, and it is one of the most common serious\nweb vulnerabilities in existence. The rule generalises: normalise untrusted\ninput into its canonical form BEFORE you validate it, never after.',
    concept='Canonicalise first, then validate. Validating a string and acting on a different resolved value is the shape of most traversal bugs.',
)
