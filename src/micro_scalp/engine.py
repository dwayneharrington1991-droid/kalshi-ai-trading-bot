"""Chronological paper-trading engine for KXBTC15M micro-scalps.

Decisions run on a fixed clock (default every second). A decision at time T
sees only events with ``at < T`` (events stamped exactly T are applied after
the decision). Orders then follow ``execution.PaperExchange`` timing.

Decision vocabulary (simulation only): BUY UP, BUY DOWN, SELL, WAIT. Every
decision carries a one-line plain-English explanation.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, field
import math

from .execution import ExecutionConfig, PaperExchange
from .features import FEATURE_NAMES_AI, FEATURE_NAMES_MARKET, MarketState, features
from .predictor import OnlineRidge
from .risk import RiskConfig, RiskManager, taker_fee
from .safety import PAPER_TRADING_ONLY, PaperOrderGateway


@dataclass(frozen=True)
class StrategyConfig:
    horizon_seconds: int = 30
    step_seconds: float = 1.0
    use_ai: bool = False
    min_expected_net_edge: float = 0.01  # $/contract after fees & slippage
    min_abs_predicted_move: float = 0.0  # used by the cost-blind DIAGNOSTIC variant
    max_spread: float = 0.03
    max_quote_age_seconds: float = 5.0
    ai_max_age_seconds: float = 5.0
    min_price: float = 0.10
    max_price: float = 0.90
    target_net_per_contract: float = 0.03
    stop_net_per_contract: float = 0.05
    max_hold_seconds: float = 90.0
    min_hold_seconds: float = 5.0
    cooldown_seconds: float = 15.0
    no_entry_last_seconds: float = 120.0
    exit_before_close_seconds: float = 30.0
    require_oos_skill: bool = True
    min_oos_r2: float = 0.0
    min_oos_samples: int = 1000  # scored forecasts before the skill gate can open
    min_train_samples: int = 1500
    ridge: float = 1.0


@dataclass
class Position:
    ticker: str
    side: str
    qty: float = 0.0
    cost: float = 0.0  # includes entry fees
    entry_fees: float = 0.0
    entry_notional: float = 0.0
    exit_proceeds: float = 0.0  # net of exit fees
    exit_fees: float = 0.0
    exit_notional: float = 0.0
    exited_qty: float = 0.0
    opened_at: float = 0.0
    entry_info: dict = field(default_factory=dict)
    exit_reason: str | None = None

    def cost_per_contract(self):
        return self.cost / (self.qty + self.exited_qty) if (self.qty + self.exited_qty) else 0.0


class Engine:
    def __init__(self, strategy=None, risk=None, execution=None, name="market_only"):
        assert PAPER_TRADING_ONLY
        self.s = strategy or StrategyConfig()
        self.rc = risk or RiskConfig()
        self.ec = execution or ExecutionConfig(liquidity_fraction=self.rc.liquidity_fraction)
        self.name = name
        self.risk = RiskManager(self.rc)
        self.exchange = PaperExchange(self.ec)
        self.gateway = PaperOrderGateway()
        n = len(FEATURE_NAMES_MARKET) + (len(FEATURE_NAMES_AI) if self.s.use_ai else 0)
        self.model = OnlineRidge(n, ridge=self.s.ridge, min_train=self.s.min_train_samples)
        self.markets: dict[str, MarketState] = {}
        self._recent_markets = deque(maxlen=4)
        self.cash = self.rc.starting_balance
        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []
        self.samples = deque()
        self.cooldown_until = -math.inf
        self.next_tick = None
        self.now = None
        self.peak_equity = self.cash
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0
        self.equity_curve = []
        self.decision_counts = Counter()
        self.wait_reasons = Counter()
        self.actions = []  # every non-WAIT decision
        self.recent = deque(maxlen=200)
        self.last_decision = None
        self.ai_samples = []  # (ticker, t, live_up, mid) for AI evaluation
        self.outcomes = {}

    # ------------------------------------------------------------------ run
    def run(self, events):
        for e in events:
            self.process(e)
        if self.next_tick is not None:
            self.tick(self.next_tick)
        return self

    def process(self, e):
        if self.next_tick is None:
            self.next_tick = math.floor(e["at"])
        while self.next_tick <= e["at"]:
            if not self._idle():
                self.tick(self.next_tick)
                self.next_tick += self.s.step_seconds
            else:
                self.next_tick = max(self.next_tick + self.s.step_seconds, math.ceil(e["at"]))
                if self.next_tick > e["at"]:
                    break
        self.apply(e)

    def _idle(self):
        if self.positions or self.exchange.pending:
            return False
        return self._active(self.next_tick) is None

    def apply(self, e):
        k = e["kind"]
        self.now = e["at"]
        if k == "market":
            if e["ticker"] not in self.markets:
                self.markets[e["ticker"]] = MarketState(e["ticker"], e["opens"], e["closes"], e.get("strike"))
                self._recent_markets.append(self.markets[e["ticker"]])
            return
        m = self.markets.get(e.get("ticker"))
        if k == "settlement":
            self.outcomes[e["ticker"]] = e["outcome"]
            if m:
                m.outcome = e["outcome"]
            self._settle(e)
            return
        if m is None:
            return
        if k == "quote":
            m.on_quote(e)
            for o in self.exchange.on_quote(e, m.has_depth_source):
                self._on_order_done(o)
        elif k == "trade":
            m.on_trade(e)
            for o in self.exchange.on_trade(e, m.has_depth_source):
                self._on_order_done(o)
        elif k == "ai":
            m.on_ai(e)

    # ----------------------------------------------------------------- tick
    def _active(self, t):
        # Markets are announced chronologically, so only recent ones can be live.
        act = [m for m in self._recent_markets if m.opens <= t < m.closes]
        return act[0] if len(act) == 1 else None

    def equity(self, t):
        value = self.cash
        for p in self.positions.values():
            m = self.markets.get(p.ticker)
            q = m.quotes() if m else None
            bid = q[p.side]["bid"] if q else 0.0
            value += p.qty * bid - taker_fee(bid, p.qty, self.ec.fee_multiplier) if p.qty else 0.0
        return value

    def tick(self, t):
        for o in self.exchange.expire(t):
            self._on_order_done(o)
        for ticker in {o.ticker for o in self.exchange.pending if t >= self.markets[o.ticker].closes}:
            for o in self.exchange.cancel_ticker(ticker):
                self._on_order_done(o)
        m = self._active(t)
        self._resolve_labels(t)
        x, info, pred = None, None, None
        if m is not None:
            x, info = features(m, t, self.s.max_quote_age_seconds, self.s.ai_max_age_seconds, self.s.use_ai)
            if x is not None:
                pred = self.model.predict(x)
                self.samples.append((t, x, info["mid"], pred, m.ticker))
                if m.ai_live_up is not None and t - m.ai_live_at <= self.s.ai_max_age_seconds and int(t) % 30 == 0:
                    self.ai_samples.append((m.ticker, t, m.ai_live_up, info["mid"]))
        eq = self.equity(t)
        self.risk.update(t, eq)
        self.peak_equity = max(self.peak_equity, eq)
        dd = self.peak_equity - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        self.max_drawdown_pct = max(self.max_drawdown_pct, dd / self.peak_equity if self.peak_equity else 0)
        if int(t) % 60 == 0:
            self.equity_curve.append((t, round(eq, 4)))
        decision = self._decide(t, m, x, info if x is not None else None, pred, eq, info if x is None else None)
        self._record(t, m, decision)

    def _resolve_labels(self, t):
        h = self.s.horizon_seconds
        while self.samples and self.samples[0][0] + h <= t:
            t0, x, mid0, pred, ticker = self.samples.popleft()
            m = self.markets.get(ticker)
            if m is None or t >= m.closes or m.quote_age(t) > self.s.max_quote_age_seconds:
                continue  # label not observable: dropped, never guessed
            y = m.mid() - mid0
            self.model.score(pred, y)
            self.model.add(x, y)

    # ------------------------------------------------------------- decision
    def _decide(self, t, m, x, info, pred, equity, why_not):
        if m is None:
            return {"action": "WAIT", "why": "No active Bitcoin 15-minute contract right now."}
        if any(o.ticker == m.ticker for o in self.exchange.pending):
            return {"action": "WAIT", "why": "A simulated order is still working; waiting for its fill or cancel."}
        pos = self.positions.get(m.ticker)
        q = m.quotes()
        fresh = q is not None and m.quote_age(t) <= self.s.max_quote_age_seconds
        if pos:
            return self._exit_decision(t, m, pos, q, fresh, pred)
        if t < self.cooldown_until:
            return {"action": "WAIT", "why": "Short cooldown after the last exit to avoid over-trading."}
        if m.closes - t < self.s.no_entry_last_seconds:
            return {"action": "WAIT", "why": "Too close to settlement for a new short scalp."}
        ok, reason = self.risk.can_enter(t)
        if not ok:
            return {"action": "WAIT", "why": "Risk limit: " + reason + "."}
        if x is None:
            return {"action": "WAIT", "why": "Not enough clean data: " + str(why_not) + "."}
        if not self.model.ready():
            return {"action": "WAIT", "why": f"Model still learning from past data ({self.model.n}/{self.s.min_train_samples} labelled samples)."}
        skill = self.model.skill()
        if self.s.require_oos_skill and (
            skill["r2_vs_zero"] is None or skill["n"] < self.s.min_oos_samples or skill["r2_vs_zero"] <= self.s.min_oos_r2
        ):
            r2 = "n/a" if skill["r2_vs_zero"] is None else f"{skill['r2_vs_zero']:.3f}"
            return {"action": "WAIT", "why": f"The price model has not beaten a no-change forecast out of sample yet (R² {r2}, {skill['n']} scored forecasts, need ≥{self.s.min_oos_samples}), so no edge is assumed."}
        best = None
        for side, sign in (("up", 1.0), ("down", -1.0)):
            bid, ask = q[side]["bid"], q[side]["ask"]
            spread = ask - bid
            entry = ask + self.ec.limit_tolerance / 2
            exp_exit = (bid + ask) / 2 + sign * pred - spread / 2 - self.ec.limit_tolerance / 2
            exp_net = exp_exit - entry - taker_fee(entry, 100) / 100 - taker_fee(max(0.01, min(0.99, exp_exit)), 100) / 100
            cand = {"side": side, "bid": bid, "ask": ask, "spread": spread, "exp_net": exp_net, "pred": sign * pred}
            if best is None or exp_net > best["exp_net"]:
                best = cand
        cents = best["exp_net"] * 100
        if abs(best["pred"]) < self.s.min_abs_predicted_move:
            return {"action": "WAIT", "why": f"Predicted move {best['pred']*100:+.1f}¢ is too small to act on."}
        if best["exp_net"] < self.s.min_expected_net_edge:
            return {"action": "WAIT", "why": f"Best idea ({best['side'].upper()}) expects {cents:+.1f}¢/contract after fees and spread; needs ≥ {self.s.min_expected_net_edge*100:.1f}¢.", "expected_net": best["exp_net"]}
        if best["spread"] > self.s.max_spread + 1e-9:
            return {"action": "WAIT", "why": f"Spread {best['spread']*100:.0f}¢ is wider than the {self.s.max_spread*100:.0f}¢ limit."}
        if not self.s.min_price <= best["ask"] <= self.s.max_price:
            return {"action": "WAIT", "why": f"Price {best['ask']:.2f} is outside the {self.s.min_price:.2f}-{self.s.max_price:.2f} band (fees/tail risk)."}
        exposure = sum(p.qty * p.cost_per_contract() for p in self.positions.values())
        limit = min(0.99, best["ask"] + self.ec.limit_tolerance)
        displayed = m.depth_qty(best["side"], "buy", limit, t, self.ec.max_book_age_seconds) if m.has_depth_source else None
        qty, size_reason = self.risk.size(equity, limit, displayed, exposure, self.ec.fee_multiplier)
        if qty < 1:
            return {"action": "WAIT", "why": "Position size is zero: " + size_reason + "."}
        why = (
            f"Model expects {best['side'].upper()} to gain {best['pred']*100:+.1f}¢ in {self.s.horizon_seconds}s; "
            f"after spread, slippage and fees that is {cents:+.1f}¢/contract. Buying {qty} ({size_reason})."
        )
        return {"action": "BUY " + best["side"].upper(), "side": best["side"], "qty": qty, "quote": best["ask"], "why": why,
                "expected_net": best["exp_net"], "entry_info": {"time_left": m.closes - t, "price": best["ask"], "spread": best["spread"],
                "realized_vol_60s": info["realized_vol_60s"], "volume_60s": info["volume_60s"], "hour_utc": int(t // 3600 % 24)}}

    def _exit_decision(self, t, m, pos, q, fresh, pred):
        held = t - pos.opened_at
        if not fresh:
            return {"action": "WAIT", "why": "Holding, but prices are stale: no exit can be priced honestly; will settle if it never refreshes."}
        bid = q[pos.side]["bid"]
        unit = bid - taker_fee(bid, pos.qty, self.ec.fee_multiplier) / pos.qty - pos.cost_per_contract()
        reason = None
        if self.risk.must_flatten():
            reason = "risk limit reached; closing the position"
        elif m.closes - t <= self.s.exit_before_close_seconds:
            reason = "contract is about to close"
        elif unit >= self.s.target_net_per_contract:
            reason = f"profit target hit ({unit*100:+.1f}¢/contract net)"
        elif unit <= -self.s.stop_net_per_contract:
            reason = f"stop-loss hit ({unit*100:+.1f}¢/contract net)"
        elif held >= self.s.max_hold_seconds:
            reason = f"time limit of {self.s.max_hold_seconds:.0f}s reached"
        elif pred is not None and held >= self.s.min_hold_seconds:
            side_pred = pred if pos.side == "up" else -pred
            if side_pred < -0.01:
                reason = f"model now expects {pos.side.upper()} to fall {side_pred*100:.1f}¢"
        if reason is None:
            return {"action": "WAIT", "why": f"Holding {pos.side.upper()}: {unit*100:+.1f}¢/contract if sold now; target {self.s.target_net_per_contract*100:.0f}¢, stop -{self.s.stop_net_per_contract*100:.0f}¢."}
        return {"action": "SELL", "side": pos.side, "qty": int(pos.qty), "quote": bid, "why": "Sell: " + reason + "."}

    def _record(self, t, m, d):
        self.decision_counts[d["action"]] += 1
        view = {"t": t, "ticker": m.ticker if m else None, **{k: v for k, v in d.items() if k != "entry_info"}}
        self.last_decision = view
        self.recent.append(view)
        if d["action"] == "WAIT":
            self.wait_reasons[d["why"].split(":")[0].split("(")[0].strip()[:70]] += 1
            return
        self.actions.append(view)
        side, action = d["side"], ("sell" if d["action"] == "SELL" else "buy")
        order = self.exchange.make_order(m.ticker, side, action, d["qty"], d["quote"], t, d["why"])
        self.gateway.submit_paper(order)
        self.exchange.submit(order)
        if action == "buy":
            order.entry_info = d.get("entry_info", {})
        else:
            pos = self.positions[m.ticker]
            pos.exit_reason = d["why"]

    # ----------------------------------------------------------- accounting
    def _on_order_done(self, o):
        if o.filled <= 0:
            return  # UNFILLED: no position, no trade, no P&L
        notional = sum(f["qty"] * f["price"] for f in o.fills)
        fees = o.fees()
        if o.action == "buy":
            p = self.positions.get(o.ticker)
            if p is None:
                p = self.positions[o.ticker] = Position(o.ticker, o.side, opened_at=o.fills[0]["t"], entry_info=getattr(o, "entry_info", {}))
            p.qty += o.filled
            p.cost += notional + fees
            p.entry_fees += fees
            p.entry_notional += notional
            self.cash -= notional + fees
        else:
            p = self.positions[o.ticker]
            p.qty -= o.filled
            p.exited_qty += o.filled
            p.exit_proceeds += notional - fees
            p.exit_fees += fees
            p.exit_notional += notional
            self.cash += notional - fees
            if p.qty <= 1e-9:
                self._close(p, o.fills[-1]["t"], p.exit_reason or "SELL")

    def _settle(self, e):
        for o in self.exchange.cancel_ticker(e["ticker"]):
            self._on_order_done(o)
        p = self.positions.get(e["ticker"])
        if p is None or p.qty <= 0:
            return
        payout = 1.0 if p.side == e["outcome"] else 0.0
        self.cash += p.qty * payout
        p.exit_proceeds += p.qty * payout
        p.exit_notional += p.qty * payout
        p.exited_qty += p.qty
        p.qty = 0.0
        self._close(p, e["at"], f"Held to official settlement ({e['outcome'].upper()} won); payout ${payout:.0f}/contract")

    def _close(self, p, t, reason):
        total = p.exited_qty
        net = p.exit_proceeds - p.cost
        fees = p.entry_fees + p.exit_fees
        trade = {
            "ticker": p.ticker,
            "side": p.side,
            "contracts": total,
            "entry_time": p.opened_at,
            "exit_time": t,
            "hold_seconds": t - p.opened_at,
            "entry_price": p.entry_notional / total,
            "exit_price": p.exit_notional / total,
            "fees": round(fees, 4),
            "gross_pnl": round(net + fees, 4),
            "net_pnl": round(net, 4),
            "exit_reason": reason,
            "settled": reason.startswith("Held to official settlement"),
            **{"entry_" + k: v for k, v in p.entry_info.items()},
        }
        self.trades.append(trade)
        del self.positions[p.ticker]
        self.risk.on_trade_closed(t, net)
        self.cooldown_until = t + self.s.cooldown_seconds

    # ----------------------------------------------------------------- view
    def view(self):
        t = self.next_tick or 0
        eq = self.equity(t)
        pos_view = []
        for p in self.positions.values():
            m = self.markets.get(p.ticker)
            q = m.quotes() if m else None
            bid = q[p.side]["bid"] if q else None
            unreal = (p.qty * bid - taker_fee(bid, p.qty) - p.qty * p.cost_per_contract()) if bid is not None else None
            pos_view.append({"ticker": p.ticker, "side": p.side.upper(), "contracts": p.qty, "entry_price": p.entry_notional / max(1e-9, p.qty + p.exited_qty),
                             "current_exit_price": bid, "unrealized_pnl": unreal})
        realized = sum(tr["net_pnl"] for tr in self.trades)
        return {
            "paper_trading_only": True,
            "strategy": self.name,
            "time": t,
            "scalp": self.last_decision["action"] if self.last_decision else "WAIT",
            "explanation": self.last_decision["why"] if self.last_decision else "Starting up.",
            "balance_cash": round(self.cash, 2),
            "equity": round(eq, 2),
            "starting_balance": self.rc.starting_balance,
            "positions": pos_view,
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(sum(pv["unrealized_pnl"] or 0 for pv in pos_view), 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "risk_state": {"killed": self.risk.killed, "kill_reason": self.risk.kill_reason, "daily_halt": self.risk.day_halted,
                           "consecutive_losses": self.risk.consecutive_losses},
            "model": {"labelled_samples": self.model.n, "ready": self.model.ready(), "out_of_sample": self.model.skill()},
            "recent_decisions": list(self.recent)[-25:],
            "recent_trades": self.trades[-25:],
            "strategy_config": asdict(self.s),
        }
