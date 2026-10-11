"""Purpose and action rules shared by model dispatch and source authorization."""


def Outcome(action, purpose):
    if not isinstance(action, str):
        return "deny"
    if purpose not in {"main", "review", "recall", "management"}:
        return "deny"
    if action in {"list", "search"}:
        return "allow"
    if action == "add" and purpose != "recall":
        return "allow"
    if action in {"replace", "remove"} and purpose in {"main", "management"}:
        return "approval"
    return "deny"
