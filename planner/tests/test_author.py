from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.html import escape

from planner import author
from planner.author import AUTHOR, LINK_KINDS, author_for_page
from planner.templatetags.planner_extras import code_spans


class AuthorDetailsTests(SimpleTestCase):
    """Guards on planner/author.py, which is edited by hand."""

    def test_links_are_real_web_addresses_with_known_icons(self):
        for link in AUTHOR['links'] + [AUTHOR['project'], AUTHOR['decisions']]:
            self.assertRegex(link['url'], r'^(https://|mailto:)\S+$', link)
            self.assertTrue(link['label'])
        for link in AUTHOR['links']:
            self.assertIn(link['kind'], LINK_KINDS)

    def test_note_to_the_reviewer_is_a_greeting_and_a_few_short_paragraphs(self):
        note = AUTHOR['note']
        self.assertTrue(note['greeting'])
        self.assertTrue(all(isinstance(paragraph, str) and paragraph.strip() for paragraph in note['paragraphs']))
        # It sits in a narrow drawer: long enough to say something, short enough to be read.
        self.assertLessEqual(len(note['paragraphs']), 8)
        self.assertLessEqual(sum(len(paragraph.split()) for paragraph in note['paragraphs']), 220)
        # Two strings on adjacent lines with no comma between them silently become one paragraph
        # with no space at the join. A full stop followed directly by a capital gives that away.
        for paragraph in note['paragraphs']:
            self.assertNotRegex(paragraph, r'[a-z][.!?][A-Z]', paragraph)

    def test_backticks_highlight_a_word_and_nothing_else_is_treated_as_html(self):
        self.assertEqual(code_spans('see the `decision log` below'), 'see the <code>decision log</code> below')
        self.assertEqual(code_spans("<b>it's</b> `<i>`"), '&lt;b&gt;it&#x27;s&lt;/b&gt; <code>&lt;i&gt;</code>')

    def test_links_get_short_text_and_empty_ones_are_dropped(self):
        details = {
            'note': {},
            'project': {},
            'decisions': {},
            'links': [
                {'kind': 'linkedin', 'label': 'LinkedIn', 'url': 'https://www.linkedin.com/in/ada/'},
                {'kind': 'email', 'label': 'Email', 'url': 'mailto:ada@example.com'},
                {'kind': 'x', 'label': 'X', 'url': ''},
            ],
        }
        with mock.patch.object(author, 'AUTHOR', details):
            page = author_for_page()
        self.assertEqual([link['text'] for link in page['links']], ['linkedin.com/in/ada', 'ada@example.com'])


class AboutDrawerTests(TestCase):
    def page(self):
        response = self.client.get(reverse('route-map'))
        self.assertEqual(response.status_code, 200)
        return response

    def drawer(self):
        content = self.page().content.decode()
        return content[content.index('id="about"') : content.index('id="planner-config"')]

    def test_drawer_is_the_note_then_the_links(self):
        drawer = self.drawer()
        # escape(): an apostrophe or an ampersand in the note reaches the page as an HTML entity.
        self.assertIn(escape(AUTHOR['note']['greeting']), drawer)
        self.assertIn(escape(AUTHOR['note']['signoff']), drawer)
        for paragraph in AUTHOR['note']['paragraphs']:
            self.assertIn(code_spans(paragraph), drawer)
        for link in AUTHOR['links'] + [AUTHOR['project'], AUTHOR['decisions']]:
            self.assertIn(f'href="{link["url"]}" target="_blank" rel="noopener noreferrer"', drawer)
        self.assertLess(drawer.index('class="letter"'), drawer.index('class="links"'))

    def test_drawer_has_no_title_but_still_has_a_name_for_screen_readers(self):
        response = self.page()
        self.assertContains(response, 'id="showAbout" aria-label="About the author" title="About the author"')
        self.assertContains(response, 'id="about" role="dialog" aria-modal="true" aria-label="About the author"')
        self.assertNotIn('<h2', self.drawer())

    def test_empty_details_are_left_out_and_text_is_escaped(self):
        details = {
            'note': {'greeting': 'Hi <b>', 'paragraphs': [], 'signoff': ''},
            'links': [],
            'project': {'label': '', 'url': ''},
            'decisions': {'label': '', 'url': ''},
        }
        with mock.patch.object(author, 'AUTHOR', details):
            drawer = self.drawer()
        self.assertIn('Hi &lt;b&gt;', drawer)
        self.assertNotIn('class="signoff"', drawer)
        self.assertNotIn('This project', drawer)
