from __future__ import annotations

from pipeline.fp_homepage_scraper import parse_homepage_links


# Minimal reproduction of antiwar.com's region-grouped link section structure.
SAMPLE_HTML = """\
<html><body>
<table>
<tr><td class="hotspot">Iran</td></tr>
<tr><td bgcolor="#F3F5F6">
  <table width="240" border="0" cellspacing="0" cellpadding="2" bgcolor="#F3F5F6">
    <tr>
      <td width="10"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://aljazeera.com/iran-war">Iran War Escalates</a></font></td>
    </tr>
    <tr>
      <td width="10"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://middleeasteye.net/iran-drones">Pentagon Struggles With Drones</a></font></td>
    </tr>
  </table>
</td></tr>
<tr><td height="5"></td></tr>
<tr><td class="hotspot">Europe</td></tr>
<tr><td bgcolor="#F3F5F6">
  <table width="240" border="0" cellspacing="0" cellpadding="2" bgcolor="#F3F5F6">
    <tr>
      <td width="10"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://euronews.com/finland-nukes">Finland Lifts Nuke Ban</a></font></td>
    </tr>
  </table>
</td></tr>
</table>
</body></html>
"""


def test_parse_homepage_links_extracts_regions() -> None:
    """Both 'Iran' and 'Europe' region headers are found."""
    links = parse_homepage_links(SAMPLE_HTML)
    regions = {link.region for link in links}
    assert "Iran" in regions
    assert "Europe" in regions


def test_parse_homepage_links_extracts_urls_and_headlines() -> None:
    """Iran region has 2 links with correct URLs and headlines."""
    links = parse_homepage_links(SAMPLE_HTML)
    iran_links = [link for link in links if link.region == "Iran"]
    assert len(iran_links) == 2
    assert iran_links[0].url == "https://aljazeera.com/iran-war"
    assert iran_links[0].headline == "Iran War Escalates"
    assert iran_links[1].url == "https://middleeasteye.net/iran-drones"
    assert iran_links[1].headline == "Pentagon Struggles With Drones"


def test_parse_homepage_links_total_count() -> None:
    """Total link count across all regions is 3."""
    links = parse_homepage_links(SAMPLE_HTML)
    assert len(links) == 3


def test_parse_homepage_links_empty_html() -> None:
    """Returns empty list when no hotspot elements are present."""
    html = "<html><body><p>No hotspots here.</p></body></html>"
    links = parse_homepage_links(html)
    assert links == []


def test_parse_homepage_links_normalizes_whitespace() -> None:
    """Headlines with internal newlines/whitespace are collapsed to single spaces."""
    html = """\
<html><body>
<table>
<tr><td class="hotspot">Iran</td></tr>
<tr><td>
  <table>
    <tr>
      <td><a href="https://example.com/story">Democrats
                    Say White House Offers No Clarity on
                    Iran War Goals After 11 Days</a></td>
    </tr>
  </table>
</td></tr>
</table>
</body></html>
"""
    links = parse_homepage_links(html)
    assert len(links) == 1
    assert links[0].headline == (
        "Democrats Say White House Offers No Clarity on Iran War Goals After 11 Days"
    )
