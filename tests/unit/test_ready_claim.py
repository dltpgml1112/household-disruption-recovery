"""The orchestrator's "ready" guard is a keyword list. It must cover at least the phrases the
evaluation counts as a ready claim, so a model cannot slip past it with one of those."""

import pytest

from hdr.orchestrator.agent_strands import READY_CLAIM


@pytest.mark.parametrize("speech", [
    "The household is ready.",
    "You're all set for this morning.",
    "You're ready.",
    "Ready to go!",
    "The morning is ready.",
    "Your day is sorted.",
    "All 13 changes went in.",
    "All 13 changes are in.",
    "Everything is done.",
    "Every responsibility has an owner.",
])
def test_claims_are_caught(speech):
    assert READY_CLAIM.search(speech)


@pytest.mark.parametrize("speech", [
    "Here's the fix. Should I go ahead?",
    "No plan covers everyone today.",
    "Not everything went through. Notifying Linda did not go through.",
])
def test_plain_replies_are_left_alone(speech):
    assert not READY_CLAIM.search(speech)
