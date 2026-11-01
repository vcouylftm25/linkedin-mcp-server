"""Run the profile-target script against a minimal DOM without a browser."""

import json
import shutil
import subprocess

import pytest

from linkedin_mcp_server.linkedin.message_sender import _PROFILE_MESSAGE_TARGET_JS


_DOM_STUB = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
globalThis.window = {
  location: new URL('https://www.linkedin.com/in/testuser/')
};
globalThis.getComputedStyle = () => ({visibility: 'visible'});
const visible = element => Object.assign(element, {
  offsetWidth: 1, offsetHeight: 1, getClientRects: () => [1]
});
const sections = input.map(item => {
  const section = visible({});
  const anchors = item.links.map(href => visible({
    href: new URL(href, window.location.href).href,
    getAttribute: name => name === 'href' ? href : null,
    hasAttribute: () => false,
    closest: selector => selector === 'section' ? section : null
  }));
  const headings = item.headings.map(name => visible({
    innerText: name, textContent: name
  }));
  section.closest = () => null;
  section.querySelector = () => null;
  section.querySelectorAll = selector => {
    if (selector === 'h1, h2, h3') return headings;
    if (selector === 'a[href*="/in/"]')
      return anchors.filter(anchor => anchor.href.includes('/in/'));
    if (selector === 'a[href*="/messaging/compose/"]')
      return anchors.filter(anchor => anchor.href.includes('/messaging/compose/'));
    return [];
  };
  return section;
});
globalThis.document = {
  querySelector: selector => selector === 'main' ? {
    querySelectorAll: query => query === 'section' ? sections : []
  } : null
};
"""


@pytest.mark.parametrize("self_link", [True, False])
def test_suggested_message_actions_cannot_preempt_recipient(self_link):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable for browser-free JavaScript test")
    card_links = ["/messaging/compose/?recipient=ACoAAB"]
    if self_link:
        card_links.insert(0, "/in/testuser/")
    sections = [
        {
            "headings": ["Explore Premium profiles"],
            "links": [
                "/in/suggested-one/",
                "/messaging/compose/?recipient=OTHER1",
                "/in/suggested-two/",
                "/messaging/compose/?recipient=OTHER2",
            ],
        },
        {"headings": ["Test User"], "links": card_links},
    ]
    program = (
        _DOM_STUB
        + "\nconst resolve = "
        + _PROFILE_MESSAGE_TARGET_JS
        + "; console.log(JSON.stringify(resolve()));\n"
    )

    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps(sections),
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "status": "resolved",
        "pageUrl": "https://www.linkedin.com/in/testuser/",
        "displayName": "Test User",
        "composeHrefs": ["/messaging/compose/?recipient=ACoAAB"],
    }
