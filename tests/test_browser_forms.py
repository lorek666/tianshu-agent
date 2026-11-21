"""Static forms come from bounded public HTML, not browser or model state."""

import pytest

from tianshu.browser_navigation import browser_target, page_snapshot
from tianshu.net import Fetched

PAGE = "network://https/example.com/contact"
DEST = "network://https/example.com/send"


def snapshot(html: str):
    return page_snapshot(browser_target(PAGE), Fetched(
        PAGE, 200, "text/html", html.encode(), False, (), 1,
    ))


def test_navigation_exposes_only_the_explicit_submission_fields():
    page = snapshot('''<form method="post" action="/send">
      <label>Name <input type="text" name="name"></label>
      <input name="message"><button type="submit">Send</button>
    </form>''')
    assert page.get("forms") == [{"url": DEST, "fields": ["name", "message"]}]


@pytest.mark.parametrize("html", [
    '<form method="post" action="https://other.example/send"><input name="a"></form>',
    '<form method="post" action="/send?q=private"><input name="a"></form>',
    '<form method="post" action="/send"><input type="hidden" name="a" value="s"></form>',
    '<form method="post" action="/send"><input type="password" name="a"></form>',
    '<form method="post" action="/send"><input name="a" value="default"></form>',
    '<form method="post" action="/send"><input name="a"><input name="a"></form>',
    '<form method="post" action="/send"><input name="a"><select name="b"></select></form>',
    '<form method="post" action="/send"><input name="a">'
    '<button formaction="/other"></button></form>',
    '<form method="post" action="/send"><input name="a"></form><base href="https://other.example/">',
    '<template><form method="post" action="/send"><input name="a"></form></template>',
    '<form method="post" action="/send"><input name="a"></form><input form="outside" name="b">',
    '<form method="post" action="/send"><input name="a">',
])
def test_unsupported_forms_are_not_proposed(html):
    assert snapshot(html).get("forms") == []
