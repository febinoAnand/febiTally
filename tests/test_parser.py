import pytest

from services.statement_parser import ParseError, parse_amount, parse_date, parse_statement
from tests import fixtures

EXPECTED = [
    ("2026-04-01", 8500.0, 0.0),
    ("2026-04-03", 0.0, 12000.0),
    ("2026-04-05", 25000.0, 0.0),
    ("2026-04-07", 2000.0, 0.0),
]


def _summary(rows):
    return [(r["txn_date"], r["debit"], r["credit"]) for r in rows]


def test_parse_helpers():
    assert parse_date("05/04/2026") == "2026-04-05"
    assert parse_date("05-Apr-2026") == "2026-04-05"
    assert parse_date("2026-04-05 10:30:00") == "2026-04-05"
    assert parse_date("Opening balance") is None
    assert parse_amount("1,23,456.50 Dr") == 123456.5
    assert parse_amount("") == 0.0
    assert parse_amount("₹ 500") == 500.0


def test_excel_with_letterhead(tmp_path):
    rows = parse_statement(fixtures.make_excel(str(tmp_path / "s.xlsx")))
    assert _summary(rows) == EXPECTED
    assert rows[0]["narration"] == "NEFT-AWS INDIA-AMAZON WEB SERVICES"
    assert rows[2]["ref_no"] == "000781"
    assert rows[0]["balance"] == 91500.0


def test_csv_single_amount_column(tmp_path):
    rows = parse_statement(fixtures.make_csv_single_amount(str(tmp_path / "s.csv")))
    assert _summary(rows) == [("2026-04-01", 0.0, 50000.0), ("2026-04-02", 1234.5, 0.0)]


def test_pdf_table(tmp_path):
    rows = parse_statement(fixtures.make_pdf_table(str(tmp_path / "s.pdf")))
    assert _summary(rows) == EXPECTED


def test_pdf_text_fallback(tmp_path):
    rows = parse_statement(fixtures.make_pdf_text(str(tmp_path / "t.pdf")))
    # first line has no previous balance: treated as debit; later lines use balance movement
    assert _summary(rows) == [("2026-04-01", 8500.0, 0.0), ("2026-04-03", 0.0, 12000.0),
                              ("2026-04-05", 25000.0, 0.0)]
    assert rows[1]["narration"] == "UPI ACME TRADERS"


def test_unknown_columns_need_mapping(tmp_path):
    path = tmp_path / "odd.csv"
    path.write_text("When,What,How much\n01/04/2026,Something,100\n")
    with pytest.raises(ParseError) as exc:
        parse_statement(str(path))
    assert exc.value.headers == ["When", "What", "How much"]
    rows = parse_statement(str(path), mapping={"txn_date": 0, "narration": 1, "debit": 2})
    assert _summary(rows) == [("2026-04-01", 100.0, 0.0)]
