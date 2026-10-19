"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.daily_signals import turtle_breakout_mask

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 阳线且真涨：今日 close > 今日 open，且今日 close > 昨日 close

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """通过 baostock 查询候选股票的流通市值（不复权收盘价 × 流通股本）。

        流通股本 = 成交量 / (换手率% / 100)
        流通市值 = 流通股本 × 不复权收盘价
        """
        from datetime import date

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        market_caps: dict[str, float] = {}

        bs.login()
        try:
            for symbol in symbols:
                bs_code = self.engine._to_baostock_code(symbol)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "close,volume,turn",
                    start_date=today_str,
                    end_date=today_str,
                    frequency="d",
                    adjustflag="3",  # 不复权，真实价格
                )
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        close = float(row[0])
                        volume = float(row[1])
                        turn = float(row[2])
                        if turn > 0:
                            circulating_shares = volume / (turn / 100)
                            market_caps[symbol] = circulating_shares * close
                    except (ValueError, ZeroDivisionError):
                        continue
        finally:
            bs.logout()

        return market_caps

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        candidates: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                if turtle_breakout_mask(df).iloc[-1]:
                    candidates.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 按流通市值从大到小排序
        if candidates:
            market_caps = self._get_market_caps(candidates)
            candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates
