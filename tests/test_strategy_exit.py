import pytest
from htxbot.models import ExitLadderConfig, SellLadderParams
from htxbot.strategy_exit import ExitStrategy


class MockBot(ExitStrategy):
    pass


def test_parse_exit_ladder_config_direct():
    bot = MockBot()
    config = ExitLadderConfig(
        symbol="BTCUSDT",
        total_contracts=10.0,
        avg_entry_price=50000.0,
        rebuild=True,
    )
    result = bot._parse_exit_ladder_config(config)
    assert result == config
    assert result is config


def test_parse_exit_ladder_config_sell_ladder_params():
    bot = MockBot()
    params = SellLadderParams(
        symbol="ETHUSDT",
        total_contracts=5.0,
        avg_entry_price=3000.0,
        rebuild=False,
    )
    result = bot._parse_exit_ladder_config(params)
    assert isinstance(result, ExitLadderConfig)
    assert result.symbol == "ETHUSDT"
    assert result.total_contracts == 5.0
    assert result.avg_entry_price == 3000.0
    assert result.rebuild is False


def test_parse_exit_ladder_config_args_kwargs():
    bot = MockBot()
    # String symbol, positional args
    result1 = bot._parse_exit_ladder_config("SOLUSDT", 20.0, 100.0, True)
    assert isinstance(result1, ExitLadderConfig)
    assert result1.symbol == "SOLUSDT"
    assert result1.total_contracts == 20.0
    assert result1.avg_entry_price == 100.0
    assert result1.rebuild is True

    # Keyword args
    result2 = bot._parse_exit_ladder_config(
        symbol="ADAUSDT",
        total_contracts=1000.0,
        avg_entry_price=0.5,
        rebuild=False,
        mode="urgent_time_exit",
    )
    assert isinstance(result2, ExitLadderConfig)
    assert result2.symbol == "ADAUSDT"
    assert result2.total_contracts == 1000.0
    assert result2.avg_entry_price == 0.5
    assert result2.rebuild is False
    assert result2.mode == "urgent_time_exit"


def test_parse_exit_ladder_config_errors():
    bot = MockBot()

    with pytest.raises(TypeError, match="missing required argument 'total_contracts'"):
        bot._parse_exit_ladder_config(symbol="XRPUSDT")

    with pytest.raises(TypeError, match="received too many positional arguments"):
        bot._parse_exit_ladder_config("XRPUSDT", 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)

    with pytest.raises(TypeError, match="unexpected keyword argument 'invalid_arg'"):
        bot._parse_exit_ladder_config(
            symbol="XRPUSDT",
            total_contracts=1,
            avg_entry_price=1,
            rebuild=True,
            invalid_arg=1,
        )

    with pytest.raises(
        TypeError, match="requires ExitLadderConfig or SellLadderParams"
    ):
        bot._parse_exit_ladder_config(123)
