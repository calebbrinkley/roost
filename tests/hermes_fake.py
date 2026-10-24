"""A stand-in for roost.hermes.Hermes at its interface, for route tests.

Built from the real class, so a route calling a method Hermes doesn't have fails the test.
Each method answers with an empty, well-formed result until a test sets its own.
"""

from unittest.mock import create_autospec

from roost.hermes import Hermes


def fake_hermes() -> Hermes:
    hermes = create_autospec(Hermes, instance=True)
    for name, value in {"sessions": {"sessions": []}, "messages": {"messages": []}, "history": [],
                        "older_messages": {"messages": [], "has_older": False},
                        "jobs": {"jobs": []}, "job": {}, "runs": {"runs": []}, "create_job": {}, "update_job": {},
                        "job_action": {}, "delete_job": {}, "rename_session": {}, "delete_session": {},
                        "profile": {"name": "default"}, "soul": "", "status": {}, "transcribe": "", "speak": "",
                        "approvals": []}.items():
        getattr(hermes, name).return_value = value
    return hermes
