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


@pytest.mark.parametrize("maker,name", [(fixtures.make_encrypted_pdf, "p.pdf"),
                                        (fixtures.make_encrypted_excel, "p.xlsx")])
def test_password_protected_statements(tmp_path, maker, name):
    from services.statement_parser import PasswordRequired
    path = maker(str(tmp_path / name))
    with pytest.raises(PasswordRequired) as exc:
        parse_statement(path)
    assert not exc.value.wrong_password
    with pytest.raises(PasswordRequired) as exc:
        parse_statement(path, password="nope")
    assert exc.value.wrong_password and "Incorrect password" in str(exc.value)
    assert _summary(parse_statement(path, password="secret")) == EXPECTED


@pytest.mark.parametrize("kind", ["xlsx", "html", "xml2003", "tsv"])
def test_xls_files_that_are_really_something_else(tmp_path, kind):
    """Banks name many formats .xls; the reader goes by content. The summary block is ignored."""
    rows = parse_statement(fixtures.make_disguised_xls(str(tmp_path / ("s_%s.xls" % kind)), kind))
    assert [(r["txn_date"], r["narration"], r["ref_no"], r["debit"], r["credit"], r["balance"]) for r in rows] == [
        ("2026-04-07", "SMS CHARGES", "", 2.12, 0.0, 1191.12),
        ("2026-07-01", "INTEREST ON SB A/C", "", 0.0, 7.0, 1198.12),
    ]


def test_encrypted_xlsx_named_xls(tmp_path):
    path = fixtures.make_encrypted_excel(str(tmp_path / "locked.xls"))
    assert _summary(parse_statement(path, password="secret")) == EXPECTED


def test_wrapped_narration_is_joined_but_summary_is_not():
    from services.statement_parser import rows_from_table
    table = [["Date", "Narration", "Withdrawal", "Deposit"],
             ["01/04/2026", "NEFT TO ACME", "100", ""],
             ["", "TRADERS PVT LTD", "", ""],          # wrapped line: joined
             ["", "", "", ""],
             ["", "Debit Count", "", ""]]               # after a blank row: not joined
    rows = rows_from_table(table)
    assert [r["narration"] for r in rows] == ["NEFT TO ACME TRADERS PVT LTD"]


def test_long_pdf_reports_page_progress_and_is_cached(tmp_path):
    from services import statement_parser as sp
    path = fixtures.make_long_pdf(str(tmp_path / "long.pdf"), pages=6, rows_per_page=20)
    seen = []
    info, rows = sp.inspect_statement(path, progress=lambda stage, done=None, total=None: seen.append((stage, done, total)))
    assert len(rows) == 120
    pages = [(d, t) for st, d, t in seen if st == "Reading tables"]
    assert pages[0] == (0, 6) and pages[-1] == (6, 6) and len(pages) == 7

    # second read comes from the cache: no progress callbacks, same result
    seen.clear()
    info2, rows2 = sp.inspect_statement(path, progress=lambda *a, **k: seen.append(a))
    assert rows2 == rows and seen == []
    # a changed file is read again
    fixtures.make_long_pdf(path, pages=2, rows_per_page=5)
    assert len(sp.inspect_statement(path)[1]) == 10


def test_parallel_pdf_reading_matches_sequential(tmp_path, monkeypatch):
    from services import statement_parser as sp
    path = fixtures.make_long_pdf(str(tmp_path / "par.pdf"), pages=5, rows_per_page=12)
    sequential = sp._read_table_uncached(path, None, sp._no_progress)
    monkeypatch.setattr(sp, "PARALLEL_MIN_PAGES", 2)
    monkeypatch.setattr(sp, "PAGES_PER_CHUNK", 1)
    monkeypatch.setattr(sp.os, "cpu_count", lambda: 3)
    seen = []
    parallel = sp._read_table_uncached(path, None, lambda st, d=None, t=None: seen.append((st, d, t)))
    assert parallel == sequential
    assert seen[-1][1] == seen[-1][2]  # progress reaches the last page


def test_parallel_failure_falls_back_to_sequential(tmp_path, monkeypatch):
    from services import statement_parser as sp
    path = fixtures.make_long_pdf(str(tmp_path / "fb.pdf"), pages=3, rows_per_page=5)
    monkeypatch.setattr(sp, "PARALLEL_MIN_PAGES", 2)

    def broken(*a, **k):
        raise OSError("no worker processes here")
    monkeypatch.setattr(sp, "_read_pdf_tables_parallel", broken)
    assert len(sp.rows_from_table(sp._read_table_uncached(path, None, sp._no_progress))) == 15
