from datetime import datetime, timezone

from radar.dates import parse_schedule

REF = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_exact_utc():
    s = parse_schedule("Free mint Oct 15 at 3pm UTC", REF)
    assert s.precision == "exact" and s.launch_at == utc(2026, 10, 15, 15, 0)


def test_24h_clock_and_day_first():
    s = parse_schedule("free mint 15th October 15:00 utc", REF)
    assert s.launch_at == utc(2026, 10, 15, 15, 0)


def test_eastern_time_across_dst_boundary():
    # US DST ends Sunday Nov 1, 2026: Oct 30 is EDT (UTC-4), Nov 2 is EST (UTC-5)
    before = parse_schedule("mint Oct 30 3pm ET", REF)
    after = parse_schedule("mint Nov 2 3pm ET", REF)
    assert before.launch_at == utc(2026, 10, 30, 19, 0)
    assert after.launch_at == utc(2026, 11, 2, 20, 0)


def test_est_in_summer_is_read_as_local_eastern_with_assumption():
    s = parse_schedule("mint Oct 30, 3 PM EST", REF)
    assert s.launch_at == utc(2026, 10, 30, 19, 0)
    assert any("EDT" in a for a in s.assumptions)


def test_manila_relative_day():
    # 'tomorrow' resolved in Manila: REF is Oct 13 08:00 PHT, so tomorrow = Oct 14
    s = parse_schedule("mint tomorrow 8pm PHT", REF)
    assert s.launch_at == utc(2026, 10, 14, 12, 0)


def test_utc_offset_zone():
    s = parse_schedule("free mint Oct 20 18:00 UTC+8", REF)
    assert s.launch_at == utc(2026, 10, 20, 10, 0)


def test_relative_in_hours():
    s = parse_schedule("free mint in 2 hours", REF)
    assert s.precision == "exact" and s.launch_at == utc(2026, 10, 13, 2, 0)


def test_date_without_time_is_day_precision():
    s = parse_schedule("free mint Oct 20", REF)
    assert s.precision == "day" and s.launch_at is None and s.launch_date == "2026-10-20"


def test_time_without_zone_never_guessed():
    s = parse_schedule("free mint 10/20 3pm", REF)
    assert s.precision == "day" and s.launch_at is None
    assert any("without a time zone" in a for a in s.assumptions)


def test_year_rollover():
    ref = datetime(2026, 12, 30, tzinfo=timezone.utc)
    s = parse_schedule("free mint Jan 2 noon UTC", ref)
    assert s.launch_at == utc(2027, 1, 2, 12, 0)


def test_tba_live_ended_signals():
    assert parse_schedule("free mint date TBA", REF).tba
    assert parse_schedule("mint is live now!", REF).live_signal
    assert parse_schedule("we minted out, thank you", REF).ended_signal


def test_one_of_one_is_not_a_date():
    s = parse_schedule("1/1 free mint art", REF)
    assert s.precision == "unknown"


def test_weekday_with_time():
    s = parse_schedule("Mint Friday 4pm UTC", REF)  # REF is Tuesday
    assert s.launch_at == utc(2026, 10, 16, 16, 0)
