"""The digest's figure rows and its week-against block (owner, 9/30/26):
uneven gaps beside "$10,060", "$124,765" broken onto two lines, a 4.40 rating
in black, and the comparison as one run-on paragraph."""
import re

import emails


def _cells(html):
    return re.findall(r'<td width="(\d+)%"[^>]*>(.*?)</td>', html)


def test_a_figure_never_wraps_and_a_long_row_steps_its_size_down():
    html = emails.report_stats([("41.7%", "labor ratio"), ("$124,765", "labor cost"), ("$299,490", "in sales")])
    assert html.count("white-space:nowrap") == 3
    assert "font-size:22px" in html and "font-size:25px" not in html
    short = emails.report_stats([("9", "reviews"), ("7", "positive")])
    assert "font-size:25px" in short


def test_columns_follow_their_figures_so_the_gaps_match():
    html = emails.report_stats([("$10,060", "sales per day"), ("44.6%", "labor %"), ("4.40&#9733;", "average rating")])
    widths = [int(w) for w, _ in _cells(html)]
    assert sum(widths) == 100 and widths[0] > widths[1] and widths[2] > widths[1]
    # a short second row keeps the first row's columns
    grid = emails.report_stats([("4.3", "avg rating"), ("9", "reviews"), ("7", "positive"), ("2", "negative"), ("1", "urgent")])
    rows = grid.split("<tr>")[1:]
    first = re.findall(r'width="(\d+)%"', rows[0])
    second = re.findall(r'width="(\d+)%"', rows[2])
    assert first == second


def test_the_rating_is_coloured_by_where_it_stands():
    html = emails.review_kpi_stats({"metrics": [{"label": "Average rating", "value": 4.4, "unit": "★",
                                                 "verdict": None}]})
    assert emails.BRAND["good"] in html and "4.40&#9733;" in html
    low = emails.review_kpi_stats({"metrics": [{"label": "Average rating", "value": 2.8, "unit": "★",
                                                "verdict": "improved"}]})
    assert emails.BRAND["bad"] in low


def test_each_metric_is_its_own_block_under_an_orange_heading():
    review = {"metrics": [{"label": "Sales per day"}, {"label": "Labor %"}]}
    body = ["Sales per day: $10,060, worse than last week's $11,230.", "Labor %: 44.6%, in line with 44.1%."]
    parts = emails.metric_parts(review, body)
    assert parts == [("Sales per day", "$10,060, worse than last week's $11,230."),
                     ("Labor %", "44.6%, in line with 44.1%.")]
    html = emails.report_metric_lines(parts)
    assert html.count(f'color:{emails.BRAND["ember"]}') == 2 and "margin:0 0 18px" in html
    src = open(emails.__file__, encoding="utf-8").read()
    assert "report_paragraph(_list(body))" not in src
