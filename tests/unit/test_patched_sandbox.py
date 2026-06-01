"""Unit tests for :mod:`src.adapters.patched_sandbox`.

Focused on the seam that fixes the NT 1.227.0 SandboxExecutionClient
partial-fill bug: the subclass MUST call ``set_fill_model`` with a
``BestPriceFillModel`` on its inherited ``self.exchange``. The full
end-to-end behavioural proof lives in
``tests/integration/test_sandbox_partial_fill.py``; here we just pin
the wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.backtest.models import BestPriceFillModel
from nautilus_trader.live.factories import LiveExecClientFactory

from src.adapters.patched_sandbox import (
    PatchedSandboxExecutionClient,
    PatchedSandboxLiveExecClientFactory,
)


class TestClassHierarchy:
    """``PatchedSandboxExecutionClient`` and its factory must be
    drop-in compatible with NT's originals — otherwise the registration
    in ``scripts/run_sandbox.py`` will break in subtle ways.
    """

    def test_client_subclasses_sandbox_execution_client(self) -> None:
        assert issubclass(PatchedSandboxExecutionClient, SandboxExecutionClient)

    def test_factory_subclasses_live_exec_client_factory(self) -> None:
        assert issubclass(
            PatchedSandboxLiveExecClientFactory, LiveExecClientFactory,
        )

    def test_factory_create_is_static(self) -> None:
        # NT's LiveExecClientFactory.create is a staticmethod; our
        # override must be too, otherwise `factory.create(...)` fails
        # because compose-time wiring calls it without an instance.
        assert isinstance(
            PatchedSandboxLiveExecClientFactory.__dict__["create"],
            staticmethod,
        )

    def test_factory_name_matches_nt_string_check(self) -> None:
        # NT's live/node_builder.py:246 (in 1.227.0) hardcodes a name
        # string match: `if factory.__name__ == "SandboxLiveExecClientFactory"`
        # decides whether to pass the `portfolio` kwarg to `create()`.
        # If our __name__ doesn't match, `portfolio` is never passed and
        # `PatchedSandboxLiveExecClientFactory.create(...)` raises
        # `TypeError: missing 1 required positional argument: 'portfolio'`
        # at TradingNode build time. We discovered this the hard way on
        # 2026-06-01 when the first deploy crash-looped on it.
        #
        # Pinning the override here so a future "let's clean up this hack"
        # commit will surface the regression in CI before deploy.
        assert PatchedSandboxLiveExecClientFactory.__name__ == "SandboxLiveExecClientFactory", (
            "PatchedSandboxLiveExecClientFactory.__name__ MUST match NT's "
            "hardcoded string check in nautilus_trader.live.node_builder so "
            "the `portfolio` kwarg is passed to create(). See class docstring."
        )


class TestFillModelSwap:
    """The whole point of this module: after ``__init__`` runs, the
    inherited ``self.exchange.fill_model`` must be a
    ``BestPriceFillModel``.
    """

    def test_init_calls_set_fill_model_with_best_price_model(self) -> None:
        # Patch the parent __init__ so we don't have to construct an
        # event loop / msgbus / cache / portfolio. Substitute a fake
        # ``self.exchange`` that records calls to ``set_fill_model``.
        captured: dict[str, Any] = {}

        def fake_parent_init(self: SandboxExecutionClient, *_: Any, **__: Any) -> None:
            fake_exchange = MagicMock()
            self.exchange = fake_exchange
            captured["exchange"] = fake_exchange

        with patch.object(SandboxExecutionClient, "__init__", fake_parent_init):
            PatchedSandboxExecutionClient()

        fake_exchange = captured["exchange"]
        fake_exchange.set_fill_model.assert_called_once()
        called_with = fake_exchange.set_fill_model.call_args.args[0]
        assert isinstance(called_with, BestPriceFillModel), (
            f"PatchedSandboxExecutionClient must install BestPriceFillModel "
            f"on its exchange; got {type(called_with).__name__}. This is the "
            f"NT 1.227.0 partial-fill workaround — see "
            f"docs/SANDBOX_PARTIAL_FILL_AUDIT.md."
        )


class TestFactoryDelegation:
    """``PatchedSandboxLiveExecClientFactory.create`` must return a
    ``PatchedSandboxExecutionClient`` (not NT's
    ``SandboxExecutionClient``). The factory's job is exactly this
    type-swap.
    """

    def test_create_returns_patched_client(self) -> None:
        # Same parent-init patching trick — we just want to confirm the
        # factory instantiates the right subclass.
        constructed: list[type] = []

        def fake_parent_init(self: SandboxExecutionClient, *_: Any, **__: Any) -> None:
            self.exchange = MagicMock()
            constructed.append(type(self))

        with patch.object(SandboxExecutionClient, "__init__", fake_parent_init):
            client = PatchedSandboxLiveExecClientFactory.create(
                loop=MagicMock(),
                name="SANDBOX",
                config=MagicMock(),
                portfolio=MagicMock(),
                msgbus=MagicMock(),
                cache=MagicMock(),
                clock=MagicMock(),
            )

        assert isinstance(client, PatchedSandboxExecutionClient)
        # And specifically NOT vanilla SandboxExecutionClient (issubclass
        # of course, but the runtime type matters for the patched
        # __init__ to have actually run).
        assert type(client) is PatchedSandboxExecutionClient
        assert constructed == [PatchedSandboxExecutionClient]


@pytest.mark.parametrize("attr_name", ["__init__"])
def test_patched_init_calls_super(attr_name: str) -> None:
    """If someone removes the ``super().__init__(...)`` call, the
    inherited exchange/account setup never runs and ``self.exchange``
    raises AttributeError on the next line. The unit test for
    ``set_fill_model`` above already exercises this — but make the
    invariant explicit with a separate assertion so a regression
    produces a sharp test name in CI output.
    """
    # Inspect the __init__ source to confirm a super() call exists.
    import inspect

    src = inspect.getsource(getattr(PatchedSandboxExecutionClient, attr_name))
    assert "super().__init__" in src, (
        "PatchedSandboxExecutionClient.__init__ must call "
        "super().__init__(...) before set_fill_model — otherwise the "
        "inherited SandboxExecutionClient setup (exchange, account, "
        "client registration) never runs."
    )
