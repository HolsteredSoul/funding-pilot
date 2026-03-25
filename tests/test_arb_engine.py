"""Unit tests for ArbEngine — fully mocked, no network, no .env needed.

Run:  pip install pytest pytest-asyncio
      pytest tests/ -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from src.arb_engine import ArbEngine
from src.models import FundingPayment, HedgePosition


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    """Builds a real-ish Settings mock without touching .env."""
    s = MagicMock()
    s.min_current_funding = 0.0003
    s.min_profitable_cycles = 3
    s.expected_holding_periods = 8
    s.max_concurrent_pairs = 5
    s.hard_stop_loss_pct = -0.02
    s.funding_negative_hours = 24
    s.basis_divergence_sigma = 3.0
    s.funding_floor = 0.0
    s.min_hold_periods = 21
    s.position_size_usd = 1000.0
    s.min_position_usd = 500.0
    s.max_position_usd = 2000.0
    s.max_portfolio_pct = 0.02
    s.circuit_breaker_pct = 0.05
    # round_trip_maker_fee is a @property on the real Settings
    type(s).round_trip_maker_fee = PropertyMock(return_value=0.0024)
    return s


@pytest.fixture
def client():
    c = MagicMock()
    c.fetch_ticker = AsyncMock(return_value={
        "quoteVolume": 50_000_000,
        "openInterest": 20_000_000,
        "mark": 45000.0,
        "index": 45000.0,
        "last": 45000.0,
        "info": {},
    })
    c.fetch_all_funding_rates = AsyncMock(return_value={})
    c.fetch_funding_rate_history = AsyncMock(return_value=[])
    return c


@pytest.fixture
def pos_mgr():
    p = MagicMock()
    p.positions = {}
    p.open_count = 0
    p.has_hedge = MagicMock(return_value=False)
    p.portfolio = MagicMock()
    p.portfolio.total_equity_usd = 50000.0
    p.portfolio.peak_equity_usd = 50000.0
    p.drawdown_pct = MagicMock(return_value=0.0)
    return p


@pytest.fixture
def engine(settings, client, pos_mgr):
    return ArbEngine(settings, client, pos_mgr)


def _make_position(**overrides) -> HedgePosition:
    """Helper to build a HedgePosition with sensible defaults."""
    defaults = dict(
        pair_id="BTCUSDT",
        perp_symbol="BTC/USDT:USDT",
        spot_symbol="BTC/USDT",
        entry_time=datetime.now(timezone.utc) - timedelta(days=10),
        spot_entry_price=45000.0,
        perp_entry_price=45000.0,
        notional_usd=1000.0,
        spot_qty=0.0222,
        perp_qty=0.0222,
    )
    defaults.update(overrides)
    return HedgePosition(**defaults)


# ── Profitability math ────────────────────────────────────────────────


class TestNetProfitability:
    """compute_net_profitability is a sync method — no async needed."""

    def test_profitable_entry(self, engine):
        # 0.04% avg x 21 periods = 0.84% gross - 0.24% fees = 0.60% net
        net = engine.compute_net_profitability(0.0004, 21)
        assert net == pytest.approx(0.0060, abs=1e-6)

    def test_breakeven_entry(self, engine):
        # 0.03% avg x 8 periods = 0.24% gross - 0.24% fees = 0% net
        net = engine.compute_net_profitability(0.0003, 8)
        assert net == pytest.approx(0.0, abs=1e-6)

    def test_unprofitable_entry(self, engine):
        # 0.025% avg x 8 periods = 0.20% gross - 0.24% fees = -0.04%
        net = engine.compute_net_profitability(0.00025, 8)
        assert net < 0

    def test_high_funding_alt(self, engine):
        # 0.10% avg x 21 periods = 2.10% gross - 0.24% fees = 1.86%
        net = engine.compute_net_profitability(0.001, 21)
        assert net == pytest.approx(0.0186, abs=1e-6)


# ── Liquidity filter ─────────────────────────────────────────────────


class TestLiquidityFilter:

    @pytest.mark.asyncio
    async def test_liquid_pair_passes(self, engine, client):
        client.fetch_ticker.return_value = {
            "quoteVolume": 50_000_000,
            "openInterest": 20_000_000,
            "info": {},
        }
        assert await engine._is_liquid_enough("BTC/USDT:USDT") is True

    @pytest.mark.asyncio
    async def test_low_volume_rejected(self, engine, client):
        client.fetch_ticker.return_value = {
            "quoteVolume": 5_000_000,  # below $10M
            "openInterest": 20_000_000,
            "info": {},
        }
        assert await engine._is_liquid_enough("JUNK/USDT:USDT") is False

    @pytest.mark.asyncio
    async def test_low_oi_rejected(self, engine, client):
        client.fetch_ticker.return_value = {
            "quoteVolume": 50_000_000,
            "openInterest": 2_000_000,  # below $5M
            "info": {},
        }
        assert await engine._is_liquid_enough("THIN/USDT:USDT") is False

    @pytest.mark.asyncio
    async def test_oi_from_info_dict(self, engine, client):
        """Some exchanges put OI in the nested info dict."""
        client.fetch_ticker.return_value = {
            "quoteVolume": 50_000_000,
            "openInterest": 0,
            "info": {"openInterest": 15_000_000},
        }
        assert await engine._is_liquid_enough("ALT/USDT:USDT") is True

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self, engine, client):
        """On error, safer to skip the pair than crash the scanner."""
        client.fetch_ticker.side_effect = Exception("timeout")
        assert await engine._is_liquid_enough("ERR/USDT:USDT") is False


# ── Exit conditions ──────────────────────────────────────────────────


class TestExitConditions:

    @pytest.mark.asyncio
    async def test_hard_stop_loss_fires_immediately(self, engine):
        """Stop-loss triggers regardless of hold period."""
        pos = _make_position(
            cumulative_pnl_usd=-25.0,  # -2.5% of $1000 notional
            notional_usd=1000.0,
        )
        reason = await engine._check_exit_conditions(pos)
        assert reason is not None
        assert "hard_stop_loss" in reason

    @pytest.mark.asyncio
    async def test_no_stop_loss_within_threshold(self, engine):
        """PnL within threshold should not trigger stop-loss."""
        pos = _make_position(
            cumulative_pnl_usd=-15.0,  # -1.5%, within -2% threshold
            notional_usd=1000.0,
        )
        reason = await engine._check_exit_conditions(pos)
        # Should not be stop-loss (might be None or another reason)
        assert reason is None or "hard_stop_loss" not in reason

    @pytest.mark.asyncio
    async def test_negative_funding_24h_streak(self, engine):
        """Exit after 24h of continuous negative funding."""
        pos = _make_position(
            negative_funding_since=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        reason = await engine._check_exit_conditions(pos)
        assert reason is not None
        assert "funding_negative" in reason

    @pytest.mark.asyncio
    async def test_negative_streak_under_24h_holds(self, engine):
        """Should NOT exit if negative streak is under 24h."""
        pos = _make_position(
            negative_funding_since=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        reason = await engine._check_exit_conditions(pos)
        assert reason is None or "funding_negative" not in reason

    @pytest.mark.asyncio
    async def test_funding_decay_gated_by_min_hold(self, engine):
        """Funding decay exit should NOT fire before min_hold_periods."""
        # 10 funding periods (below 21 min_hold) with zero rates
        pos = _make_position(
            funding_history=[
                FundingPayment(
                    timestamp=datetime.now(timezone.utc) - timedelta(hours=i * 8),
                    rate=-0.0001,
                    amount_usd=-0.1,
                )
                for i in range(10)
            ],
        )
        reason = await engine._check_exit_conditions(pos)
        # Should NOT be funding_decay (not enough periods yet)
        assert reason is None or "funding_decay" not in reason

    @pytest.mark.asyncio
    async def test_funding_decay_fires_after_min_hold(self, engine):
        """Funding decay fires after 21+ periods with below-floor rates."""
        pos = _make_position(
            funding_history=[
                FundingPayment(
                    timestamp=datetime.now(timezone.utc) - timedelta(hours=i * 8),
                    rate=-0.0001,
                    amount_usd=-0.1,
                )
                for i in range(25)
            ],
        )
        reason = await engine._check_exit_conditions(pos)
        assert reason is not None
        assert "funding_decay" in reason

    @pytest.mark.asyncio
    async def test_basis_divergence_fires_on_extreme(self, engine, client):
        """3-sigma basis divergence triggers exit."""
        # Pre-seed stable basis history
        engine._basis_history["BTC/USDT:USDT"] = [0.0001] * 30

        # Now return a wildly divergent mark/index
        client.fetch_ticker.return_value = {
            "mark": 46000.0,
            "index": 45000.0,  # ~2.2% basis vs history of 0.01%
            "last": 46000.0,
        }
        pos = _make_position()
        reason = await engine._check_exit_conditions(pos)
        assert reason is not None
        assert "basis_divergence" in reason

    @pytest.mark.asyncio
    async def test_basis_divergence_skipped_with_no_history(self, engine, client):
        """With <10 data points, basis check returns std=0 (no trigger)."""
        engine._basis_history = {}
        client.fetch_ticker.return_value = {
            "mark": 45100.0,
            "index": 45000.0,
            "last": 45100.0,
        }
        pos = _make_position()
        reason = await engine._check_exit_conditions(pos)
        # Should not be basis_divergence with insufficient history
        assert reason is None or "basis_divergence" not in reason


# ── Position sizing ──────────────────────────────────────────────────


class TestPositionSizing:

    def test_default_size(self, engine, pos_mgr):
        pos_mgr.portfolio.total_equity_usd = 100_000.0
        size = engine.compute_position_size()
        assert size == 1000.0  # default position_size_usd

    def test_capped_by_portfolio_pct(self, engine, pos_mgr, settings):
        pos_mgr.portfolio.total_equity_usd = 10_000.0
        # 2% of $10k = $200, but min is $500
        size = engine.compute_position_size()
        assert size == 500.0  # clamped to min_position_usd

    def test_capped_by_max_position(self, engine, pos_mgr, settings):
        settings.position_size_usd = 5000.0  # above max_position_usd
        pos_mgr.portfolio.total_equity_usd = 1_000_000.0
        # 2% of $1M = $20k, position_size = $5k, but max = $2000
        size = engine.compute_position_size()
        assert size == 2000.0


# ── Circuit breaker ──────────────────────────────────────────────────


class TestCircuitBreaker:

    @pytest.mark.asyncio
    async def test_triggers_at_threshold(self, engine, pos_mgr):
        pos_mgr.drawdown_pct.return_value = 0.06  # 6% > 5% threshold
        assert await engine.should_circuit_break() is True

    @pytest.mark.asyncio
    async def test_safe_below_threshold(self, engine, pos_mgr):
        pos_mgr.drawdown_pct.return_value = 0.03  # 3% < 5%
        assert await engine.should_circuit_break() is False


# ── Symbol normalisation ─────────────────────────────────────────────


class TestHelpers:

    def test_normalise_perp_symbol(self):
        assert ArbEngine._normalise_pair_id("BTC/USDT:USDT") == "BTCUSDT"

    def test_normalise_spot_symbol(self):
        assert ArbEngine._normalise_pair_id("ETH/USDT") == "ETHUSDT"
