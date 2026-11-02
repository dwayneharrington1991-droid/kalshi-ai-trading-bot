import pytest

from src.orders.state_machine import InvalidOrderTransition, OrderState, OrderStateMachine


@pytest.mark.parametrize("current,target", [
    ("locally_created", "submitted"), ("submitted", "accepted"),
    ("submitted", "rejected"), ("submitted", "verification_failed"),
    ("accepted", "resting"), ("accepted", "fully_filled"),
    ("resting", "partially_filled"), ("partially_filled", "fully_filled"),
    ("partially_filled", "canceled"), ("verification_failed", "resting"),
])
def test_valid_transitions(current, target):
    OrderStateMachine.validate(current, target)


@pytest.mark.parametrize("current,target", [
    ("locally_created", "fully_filled"), ("submitted", "fully_filled"),
    ("fully_filled", "resting"), ("canceled", "submitted"),
    ("rejected", "accepted"),
])
def test_invalid_transitions(current, target):
    with pytest.raises(InvalidOrderTransition):
        OrderStateMachine.validate(current, target)


def test_state_groups_are_safe():
    assert OrderState.FULLY_FILLED in OrderStateMachine.TERMINAL
    assert OrderState.VERIFICATION_FAILED in OrderStateMachine.UNCERTAIN
    assert OrderState.VERIFICATION_FAILED in OrderStateMachine.ACTIVE
    assert OrderState.ACCEPTED not in OrderStateMachine.TERMINAL
