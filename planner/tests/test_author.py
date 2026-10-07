from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from planner import author
from planner.author import AUTHOR, BIO_MAX_LENGTH, LINK_KINDS, author_for_page


class AuthorDetailsTests(SimpleTestCase):
    """Guards on planner/author.py, which is edited by hand."""

    def test_bio_is_short(self):
        self.assertLessEqual(len(AUTHOR['bio']), BIO_MAX_LENGTH)

    def test_links_are_real_web_addresses_with_known_icons(self):
        for link in AUTHOR['links'] + [AUTHOR['project']]:
            self.assertRegex(link['url'], r'^(https://|mailto:)\S+$', link)
            self.assertTrue(link['label'])
        for link in AUTHOR['links']:
            self.assertIn(link['kind'], LINK_KINDS)

    def test_initials_and_short_link_text(self):
        details = {'name': 'ada lovelace-king', 'role': 'Engineer', 'location': '', 'bio': '', 'project': {},
                   'links': [{'kind': 'linkedin', 'label': 'LinkedIn', 'url': 'https://www.linkedin.com/in/ada/'},
                             {'kind': 'email', 'label': 'Email', 'url': 'mailto:ada@example.com'},
                             {'kind': 'x', 'label': 'X', 'url': ''}]}
        with mock.patch.object(author, 'AUTHOR', details):
            page = author_for_page()
        self.assertEqual(page['initials'], 'AL')
        self.assertEqual(page['details'], 'Engineer')
        self.assertEqual([link['text'] for link in page['links']], ['linkedin.com/in/ada', 'ada@example.com'])  # the empty one is dropped


class AboutDrawerTests(TestCase):
    def page(self):
        response = self.client.get(reverse('route-map'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_drawer_shows_the_author(self):
        response = self.page()
        self.assertContains(response, 'id="showAbout" aria-label="About the author" title="About the author"')
        self.assertContains(response, AUTHOR['name'])
        self.assertContains(response, AUTHOR['bio'])
        for link in AUTHOR['links'] + [AUTHOR['project']]:
            self.assertContains(response, f'href="{link["url"]}" target="_blank" rel="noopener noreferrer"')

    def test_empty_details_are_left_out_and_text_is_escaped(self):
        details = {'name': 'A <b>', 'role': '', 'location': '', 'bio': '', 'links': [], 'project': {'label': '', 'url': ''}}
        with mock.patch.object(author, 'AUTHOR', details):
            response = self.page()
        self.assertContains(response, 'A &lt;b&gt;')
        self.assertNotContains(response, 'class="bio"')
        self.assertNotContains(response, 'This project')
