"""Starting self-edit policy. Candidates may improve this module itself."""

import json


def build_prompt(evidence, goal):
    return (
        "Improve this Python system using the failure evidence. "
        "First analyze the supplied failure evidence and previous edits. "
        "Explain each change with a failure ID, cause, expected effect and validation. "
        "Fix general causes, never hardcode task answers. "
        "Edit the allowed source files directly.\n"
        + goal + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False)
    )
