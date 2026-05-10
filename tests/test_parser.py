"""Regression tests for the wrapper parsers. Each test loads a saved tmux
capture (with ANSI codes) from tests/fixtures/ and asserts what the parser
extracts. Add new fixtures with:

    tmux capture-pane -t cdda -p -e > tests/fixtures/<name>.txt

Run: python3 tests/test_parser.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wrapper  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures')


def load(name):
    with open(os.path.join(FIXTURES, name), 'r', encoding='utf-8') as handle:
        return handle.read()


class ChargenTraitsTests(unittest.TestCase):
    def test_traits_with_selections_detects_mode(self):
        raw = load('chargen_traits_with_selections.txt')
        plain = wrapper.strip_ansi(raw)
        self.assertEqual(wrapper.detect_mode(plain), 'chargen_traits')

    def test_traits_with_selections_partitions_panes(self):
        # Fixture state: filter was narrowed to "Near"; only Near-Sighted is
        # visible in the negative pane and it's bold-red (selected) with
        # cursor-on (blue background). Asthmatic and Clumsy were also toggled
        # on at game-state level but are scrolled off-screen behind the
        # filter, so they cannot appear in this capture's color stream.
        # The Summary line shows Lifestyle:average / Knowledge:powerful etc.
        # in bold colors — none of those rating words must leak into the
        # selected-trait list.
        raw = load('chargen_traits_with_selections.txt')
        panes = wrapper.selected_trait_panes(raw)
        self.assertIn('Near-Sighted', panes['negative'])
        for word in ('powerful', 'strong', 'average', 'weak'):
            self.assertNotIn(word, [t.lower() for t in panes['positive']])
            self.assertNotIn(word, [t.lower() for t in panes['negative']])


if __name__ == '__main__':
    unittest.main()
