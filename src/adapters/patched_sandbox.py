"""SandboxExecutionClient with ``BestPriceFillModel`` — workaround for
NT 1.227.0's partial-fill race.

**Background.** ``SandboxExecutionClient`` builds an L1 book from live
bars decomposed into 4 synthetic ticks per bar (each tick holds
``bar.volume / 4`` units). When a MARKET order requests more than that
per-tick size, the matching engine's "slip-fill the residual" safety
net (``backtest/engine.pyx:7299-7336``) silently no-ops because it
gates on ``order.is_open_c()`` — and in sandbox mode
``LiveExecutionEngine.process()`` enqueues ``OrderFilled`` events
asynchronously, so the order's FSM is still ``SUBMITTED`` when the
gate is checked. The MARKET order ends up as a zombie in
``orders_open`` with ``leaves_qty > 0``, no completion event ever
arriving, no future ``iterate()`` call revisiting it.

Full audit: :doc:`/SANDBOX_PARTIAL_FILL_AUDIT` (`docs/SANDBOX_PARTIAL_FILL_AUDIT.md`).

**The workaround.** ``BestPriceFillModel`` (built into NT at
``backtest/models/fill.pyx:170``) overrides
``get_orderbook_for_fill_simulation`` to return a synthetic L1 book
with 1_000_000 units at the best bid and ask. Any reasonable order
quantity fills in one event, sidestepping the buggy slip-fill path
entirely. Slippage is still modeled via the base ``FillModel``'s
``prob_slippage`` parameter — chain via ``super().is_slipped()`` from
a further subclass if you need it.

**When to remove this patch.** Re-verify after every NT version bump
by running ``pytest tests/integration/test_sandbox_partial_fill.py``.
If ``test_default_fillmodel_leaves_order_partially_filled_zombie``
starts FAILING (meaning the default model now fills MARKET orders
cleanly), upstream NT has fixed the race and this patch can be
deleted. See SANDBOX_PARTIAL_FILL_AUDIT.md §7 for the full recipe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.backtest.models import BestPriceFillModel
from nautilus_trader.live.factories import LiveExecClientFactory

if TYPE_CHECKING:
    import asyncio

    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.portfolio import PortfolioFacade


class PatchedSandboxExecutionClient(SandboxExecutionClient):
    """``SandboxExecutionClient`` with ``BestPriceFillModel`` installed.

    Drop-in replacement for ``SandboxExecutionClient``. Identical
    behavior except market orders fill in one event against an
    abundant synthetic book, working around the NT 1.227.0
    partial-fill race documented in the module docstring.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The sandbox-built ``SimulatedExchange`` exists at this point.
        # ``set_fill_model`` propagates the new model to every
        # ``OrderMatchingEngine`` the exchange has constructed (and
        # to any built lazily later).
        self.exchange.set_fill_model(BestPriceFillModel())


class PatchedSandboxLiveExecClientFactory(LiveExecClientFactory):
    """Factory that returns :class:`PatchedSandboxExecutionClient`.

    Register via ``node.add_exec_client_factory("SANDBOX", ...)`` in
    place of NT's ``SandboxLiveExecClientFactory``.

    .. warning::

        NT's ``live/node_builder.py`` (line 246 in 1.227.0) special-
        cases the factory by name string match — ``factory.__name__ ==
        "SandboxLiveExecClientFactory"`` decides whether the
        ``portfolio`` kwarg gets passed to ``create()``. Subclassing
        with a different name skips the special-case, the kwarg is
        missing, and ``create()`` raises ``TypeError`` at startup.

        We override ``__name__`` at class-definition time (see below)
        so the runtime check matches and ``portfolio`` arrives. If
        upstream NT replaces the string check with proper subclass
        introspection, this hack can be deleted.
    """

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SandboxExecutionClientConfig,
        portfolio: PortfolioFacade,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PatchedSandboxExecutionClient:
        return PatchedSandboxExecutionClient(
            loop=loop,
            clock=clock,
            portfolio=portfolio,
            msgbus=msgbus,
            cache=cache,
            config=config,
        )


# Match the name string NT's node_builder hardcodes (see class docstring).
# Do NOT remove without verifying NT no longer string-matches the factory name.
PatchedSandboxLiveExecClientFactory.__name__ = "SandboxLiveExecClientFactory"
PatchedSandboxLiveExecClientFactory.__qualname__ = "SandboxLiveExecClientFactory"
