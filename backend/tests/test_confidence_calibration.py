import app.services.confidence_calibration as cc
from app.services.confidence_calibration import (
    Calibrator,
    PlattCalibrator,
    WeakLabel,
    _parse_overridden,
    build_calibration,
    expected_calibration_error,
    fit_platt,
    reliability_bins,
)


def test_parse_overridden_handles_dict_list_garbage():
    assert _parse_overridden('{"table_role": "x", "name": "y"}') == {"table_role", "name"}
    assert _parse_overridden('["table_role", "display_name"]') == {"table_role", "display_name"}
    assert _parse_overridden("not json") == set()
    assert _parse_overridden(None) == set()


def test_identity_calibrator_clamps():
    c = Calibrator()
    assert c.cold_start is True
    assert c.calibrate(0.7) == 0.7
    assert c.calibrate(1.5) == 1.0
    assert c.calibrate(-0.2) == 0.0


def test_platt_calibrator_monotonic():
    cal = PlattCalibrator(w=4.0, b=-2.0)
    vals = [cal.calibrate(x / 10) for x in range(11)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))  # 单调不减
    assert 0.0 <= vals[0] <= vals[-1] <= 1.0


def test_fit_platt_separates_two_classes():
    # 低置信全错、高置信全对 → 拟合后校准应把二者拉开。
    labels = (
        [WeakLabel(0.2, False) for _ in range(40)]
        + [WeakLabel(0.9, True) for _ in range(40)]
    )
    cal = fit_platt(labels)
    assert not cal.cold_start
    assert cal.calibrate(0.9) > cal.calibrate(0.2)
    assert cal.calibrate(0.9) > 0.5 > cal.calibrate(0.2)


def test_ece_zero_for_perfectly_calibrated():
    # 每个置信档的真实采纳率恰等于置信度 → 未校准 ECE 近 0。
    labels = []
    for conf, correct_n, total in [(0.1, 1, 10), (0.5, 5, 10), (0.9, 9, 10)]:
        labels += [WeakLabel(conf, True) for _ in range(correct_n)]
        labels += [WeakLabel(conf, False) for _ in range(total - correct_n)]
    assert expected_calibration_error(labels) < 0.02


def test_ece_high_for_overconfident():
    # 全部预测 0.9 但只有一半正确 → ECE 约 0.4。
    labels = [WeakLabel(0.9, i % 2 == 0) for i in range(100)]
    assert expected_calibration_error(labels) > 0.3


def test_reliability_bins_counts_and_empirical():
    labels = [WeakLabel(0.85, True) for _ in range(8)] + [
        WeakLabel(0.85, False) for _ in range(2)
    ]
    bins = reliability_bins(labels, None, n_bins=10)
    assert len(bins) == 1
    assert bins[0].count == 10
    assert bins[0].empirical == 0.8


def test_build_calibration_cold_start_when_insufficient(monkeypatch):
    monkeypatch.setattr(
        cc, "collect_role_weak_labels", lambda db: [WeakLabel(0.8, True) for _ in range(5)]
    )
    report = build_calibration(db=None)
    assert report.cold_start is True
    assert isinstance(report.calibrator, Calibrator)
    assert not isinstance(report.calibrator, PlattCalibrator)
    assert "冷启动" in report.note


def test_build_calibration_cold_start_when_single_class(monkeypatch):
    # 够量但全是正类（如当前库全被判 wrong 的退化情形的反面）→ 不拟合。
    monkeypatch.setattr(
        cc, "collect_role_weak_labels", lambda db: [WeakLabel(0.7, True) for _ in range(80)]
    )
    report = build_calibration(db=None)
    assert report.cold_start is True


def test_build_calibration_fits_when_two_classes_and_enough(monkeypatch):
    labels = (
        [WeakLabel(0.9, True) for _ in range(40)]
        + [WeakLabel(0.3, False) for _ in range(40)]
    )
    monkeypatch.setattr(cc, "collect_role_weak_labels", lambda db: labels)
    report = build_calibration(db=None)
    assert report.cold_start is False
    assert isinstance(report.calibrator, PlattCalibrator)
    assert report.n == 80
    assert "ECE" in report.note
