"""Validated lifecycle transitions for locally tracked orders."""

from enum import Enum
from typing import Union


class OrderState(str, Enum):
    LOCALLY_CREATED = "locally_created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    VERIFICATION_FAILED = "verification_failed"


class InvalidOrderTransition(ValueError):
    """Raised when an order lifecycle transition violates the state machine."""


class OrderStateMachine:
    TERMINAL = {
        OrderState.FULLY_FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
    UNCERTAIN = {OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.VERIFICATION_FAILED}
    ACTIVE = {
        OrderState.LOCALLY_CREATED,
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.RESTING,
        OrderState.PARTIALLY_FILLED,
        OrderState.VERIFICATION_FAILED,
    }
    _TRANSITIONS = {
        OrderState.LOCALLY_CREATED: {OrderState.SUBMITTED},
        OrderState.SUBMITTED: {
            OrderState.ACCEPTED, OrderState.REJECTED, OrderState.VERIFICATION_FAILED,
        },
        OrderState.ACCEPTED: {
            OrderState.RESTING, OrderState.PARTIALLY_FILLED, OrderState.FULLY_FILLED,
            OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED,
            OrderState.VERIFICATION_FAILED,
        },
        OrderState.RESTING: {
            OrderState.PARTIALLY_FILLED, OrderState.FULLY_FILLED,
            OrderState.CANCELED, OrderState.EXPIRED, OrderState.VERIFICATION_FAILED,
        },
        OrderState.PARTIALLY_FILLED: {
            OrderState.PARTIALLY_FILLED, OrderState.FULLY_FILLED,
            OrderState.CANCELED, OrderState.EXPIRED, OrderState.VERIFICATION_FAILED,
        },
        OrderState.VERIFICATION_FAILED: {
            OrderState.ACCEPTED, OrderState.RESTING, OrderState.PARTIALLY_FILLED,
            OrderState.FULLY_FILLED, OrderState.CANCELED, OrderState.REJECTED,
            OrderState.EXPIRED,
        },
    }

    @classmethod
    def normalize(cls, state: Union[OrderState, str]) -> OrderState:
        try:
            return state if isinstance(state, OrderState) else OrderState(state)
        except ValueError as exc:
            raise InvalidOrderTransition(f"Unknown order state: {state}") from exc

    @classmethod
    def validate(cls, current: Union[OrderState, str], target: Union[OrderState, str]) -> None:
        current_state = cls.normalize(current)
        target_state = cls.normalize(target)
        if target_state not in cls._TRANSITIONS.get(current_state, set()):
            raise InvalidOrderTransition(
                f"Invalid order transition: {current_state.value} -> {target_state.value}"
            )
