"""Who built this, as shown in the page's "About" drawer.

Edit this file to change what the drawer says. Anything left empty is simply
not shown. Each link's kind picks its icon: github, linkedin, website, blog,
x or email.
"""

BIO_MAX_LENGTH = 120

AUTHOR = {
    'name': 'M. Hammad Hassan',
    'role': '',
    'location': '',
    'bio': 'I bring ideas to life, MVPs to production and confusion to clarity.',
    # A note to whoever is reviewing this, shown at the top of the drawer.
    # Every claim in it should stay true of the page as it is.
    'note': {
        'greeting': 'Hey there. Yes, you.',
        'paragraphs': [
            'You probably have a pile of these to get through, so I built this one to be reviewed '
            'without leaving the page.',
            'The example trips are one click. Let go of a slider and the route re-plans. The API call '
            'tab shows the exact request behind whatever is on screen, so Postman is optional.',
            'I focused on small things like that: the error that tells you what to fix, the API key box '
            'that sits under the provider it belongs to, the link to where you get one.',
            'If something still gets in your way, that is exactly what I would want to hear. The '
            'reasoning behind each choice is in the decision log below.',
        ],
        'signoff': 'Hammad',
    },
    'links': [
        {'kind': 'github', 'label': 'GitHub', 'url': 'https://github.com/Blankscreen-exe'},
        {'kind': 'linkedin', 'label': 'LinkedIn', 'url': 'https://www.linkedin.com/in/hammadai/'},
        {'kind': 'website', 'label': 'Website', 'url': 'https://hammad-ai.vercel.app'},
    ],
    'project': {
        'label': 'Source code for this project',
        'url': 'https://github.com/Blankscreen-exe/spotter.ai-coding-assessment',
    },
    # Works once the work is merged to main and pushed.
    'decisions': {
        'label': 'Decision log: why it is built this way',
        'url': 'https://github.com/Blankscreen-exe/spotter.ai-coding-assessment/blob/main/docs/DECISIONS.md',
    },
}

LINK_KINDS = {'github', 'linkedin', 'website', 'blog', 'x', 'email'}


def author_for_page():
    """AUTHOR with the two things the template cannot work out itself: initials and short link text."""
    def short(url):
        return url.removeprefix('mailto:').removeprefix('https://').removeprefix('www.').rstrip('/')

    initials = ''.join(word[0] for word in AUTHOR['name'].replace('.', ' ').split())[:2].upper()
    return {
        **AUTHOR,
        'initials': initials,
        'details': ' · '.join(part for part in (AUTHOR['role'], AUTHOR['location']) if part),
        'links': [{**link, 'text': short(link['url'])} for link in AUTHOR['links'] if link['url']],
    }
