"""Sequoia-X: daily scanning, history backfill and local-only observations."""

import argparse
import socket
import sys

from dotenv import load_dotenv

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.research.daily_report import (
    resolve_as_of,
    scan_daily_observations,
    write_daily_report,
)
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

load_dotenv()
socket.setdefaulttimeout(10.0)


def build_strategies(engine: DataEngine, settings: Settings) -> list[BaseStrategy]:
    """Preserve existing scanners; the observation report annotates shared evidence."""
    return [
        MaVolumeStrategy(engine=engine, settings=settings),
        TurtleTradeStrategy(engine=engine, settings=settings),
        HighTightFlagStrategy(engine=engine, settings=settings),
        LimitUpShakeoutStrategy(engine=engine, settings=settings),
        UptrendLimitDownStrategy(engine=engine, settings=settings),
        RpsBreakoutStrategy(engine=engine, settings=settings),
        PrivatePlacementStrategy(engine=engine, settings=settings),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill", action="store_true", help="回填后复权历史日线")
    mode.add_argument(
        "--observe-only",
        action="store_true",
        help="仅从本地日线生成观察报告，不更新行情、不发送消息",
    )
    parser.add_argument("--as-of", help="仅供 --observe-only：截止已收盘日期 YYYY-MM-DD")
    parser.add_argument(
        "--output", default="reports/daily-observations.json", help="本地观察报告 JSON 路径"
    )
    args = parser.parse_args()
    if args.as_of and not args.observe_only:
        parser.error("--as-of requires --observe-only; historical runs must not notify")

    try:
        # Local-only and backfill modes do not need or instantiate a notifier.
        settings = (
            Settings(feishu_webhook_url="")
            if args.observe_only or args.backfill
            else get_settings()
        )
        logger = get_logger(__name__)
        cutoff = resolve_as_of(args.as_of)
        engine = DataEngine(settings)

        if args.backfill:
            engine.backfill(engine.get_all_symbols())
            return
        if args.observe_only:
            report = scan_daily_observations(engine, settings.daily_signals, cutoff)
            write_daily_report(report, args.output)
            logger.info(
                "观察报告已保存：%s；评估 %d / 本地 %d 只，排除 %d 只",
                args.output,
                report["evaluated_symbols"],
                report["local_symbols"],
                len(report["excluded"]),
            )
            return

        count = engine.sync_today_bulk()
        logger.info("快照同步完成，写入 %d 条日线", count)
        report = scan_daily_observations(engine, settings.daily_signals, cutoff)
        write_daily_report(report, args.output)
        logger.info("本地日线观察报告已保存：%s", args.output)

        notifier = FeishuNotifier(settings)
        for strategy in build_strategies(engine, settings):
            selected = strategy.run()
            if selected:
                notifier.send(
                    symbols=selected,
                    strategy_name=type(strategy).__name__,
                    webhook_key=strategy.webhook_key,
                )
        logger.info("Sequoia-X V2 运行完成")
    except Exception:
        get_logger(__name__).exception("主流程发生未捕获异常，程序终止")
        sys.exit(1)


if __name__ == "__main__":
    main()
