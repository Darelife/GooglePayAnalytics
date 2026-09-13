from gpay_analytics import analyze, parse_transactions


def test_parses_google_pay_rows_and_split_amounts():
    text = """
    01 Aug,
    2026
    Paid to Jio Prepaid
    ₹900.9
    0
    02 Aug,
    2026
    Paid to Refund from Amazon
    ₹200
    """
    rows = parse_transactions(text, {"Bills": ["jio"], "Shopping": ["amazon"]}, ["refund"])
    assert rows[0].amount == 900.90
    assert rows[0].category == "Bills"
    assert rows[1].amount == -200


def test_analysis_has_category_month_and_uncategorized_totals():
    rows = parse_transactions("01 Aug,\n2026\nPaid to Cafe\n₹100", {"Food": ["cafe"]})
    rows += parse_transactions("02 Aug,\n2026\nPaid to Mystery\n₹50", {"Food": ["cafe"]})
    result = analyze(rows)
    assert result["total_expenses"] == 150
    assert result["category_totals"] == {"Food": 100, "Uncategorized": 50}
    assert result["monthly_totals"] == {"2026-08": 150}


def test_analysis_keeps_all_expenses_sorted_and_calculates_statistics():
    rows = [
        parse_transactions("01 Aug,\n2026\nPaid to One\n₹100", {})[0],
        parse_transactions("02 Aug,\n2026\nPaid to Two\n₹300", {})[0],
        parse_transactions("03 Aug,\n2026\nPaid to Three\n₹50", {})[0],
    ]
    result = analyze(rows)
    assert [item["amount"] for item in result["largest_expenses"]] == [300, 100, 50]
    assert result["statistics"]["median_expense"] == 100
    assert result["statistics"]["active_days"] == 3