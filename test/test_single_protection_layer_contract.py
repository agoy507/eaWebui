from pathlib import Path

ROOT = Path(__file__).parents[1] / "ea"
REGULAR = (ROOT / "RSI_Martingale_Web_Control.mq5").read_text(encoding="utf-8")
TRAILING = (ROOT / "RSI_Martingale_Web_Control_Trailing.mq5").read_text(encoding="utf-8")


def test_regular_keeps_sl_tp_only_on_first_layer_and_recalculates_values():
    assert "UpdateFirstLayerProtection" in REGULAR
    assert "time_msc < first_time_msc" in REGULAR
    assert "desired_sl = ticket == protected_ticket ? target_sl : 0.0" in REGULAR
    assert "desired_tp = ticket == protected_ticket ? target_tp : 0.0" in REGULAR
    assert "latest_price = GetLatestOpenPrice(type)" in REGULAR
    assert "DEAL_REASON_TP" in REGULAR and "DEAL_REASON_SL" in REGULAR
    assert "ProcessCloseAllAfterProtectedExit" in REGULAR
    assert "CLOSE_ALL_RETRY_SECONDS" in REGULAR


def test_trailing_keeps_tp_on_layer_one_and_sl_on_layer_two():
    assert "ApplySecondLayerStop" in TRAILING
    assert "second_ticket" in TRAILING
    assert "desired_sl = ticket == second_ticket ? stop : 0.0" in TRAILING
    assert "Layer 2 keeps SL even when later martingale layers open" in TRAILING
    assert "DEAL_REASON_TP" in TRAILING and "DEAL_REASON_SL" in TRAILING
    assert "ProcessCloseAllAfterProtectedExit" in TRAILING


if __name__ == "__main__":
    test_regular_keeps_sl_tp_only_on_first_layer_and_recalculates_values()
    test_trailing_keeps_tp_on_layer_one_and_sl_on_layer_two()
    print("single protection layer contract: PASS")
