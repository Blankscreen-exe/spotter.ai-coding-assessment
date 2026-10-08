"""What the page's "About" drawer says: a note to the reviewer, then links.

Edit this file to change the drawer. Anything left empty is simply not shown.
Each link's kind picks its icon: github, linkedin, website, blog, x or email.
"""

AUTHOR = {
    # A note to whoever is reviewing this, shown at the top of the drawer.
    # Every claim in it should stay true of the page as it is.
    'note': {
        'greeting': '👋 Hey there! Yes, you!',
        # One string per paragraph. Wrap a word in `backticks` to highlight it.
        'paragraphs': [
            'A few things worth knowing while you look around.',
            'You probably have a pile of these to get through, so I built this one with your time in mind.',
            'The example trips are one click. Change a number with its arrows, or scroll over it, and the '
            'route re-plans. The API call tab shows the exact request behind whatever is on screen, so '
            'Postman is optional. There is a collection in the repo if you prefer it.',
            'I focused on small things like that: the points where you would otherwise switch tabs and '
            'lose your place.',
            'If something still gets in your way, I would LOVE to hear about it. The reasoning behind '
            'each choice is in the `decision log` below.',
        ],
        'signoff': "I'm Hammad by the way.",
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
    'decisions': {
        'label': 'Decision log: why it is built this way',
        'url': 'https://github.com/Blankscreen-exe/spotter.ai-coding-assessment/blob/main/docs/DECISIONS.md',
    },
}

LINK_KINDS = {'github', 'linkedin', 'website', 'blog', 'x', 'email'}


def author_for_page():
    """AUTHOR, with each link given the short text shown under its label (the address without the https)."""

    def short(url):
        return url.removeprefix('mailto:').removeprefix('https://').removeprefix('www.').rstrip('/')

    return {**AUTHOR, 'links': [{**link, 'text': short(link['url'])} for link in AUTHOR['links'] if link['url']]}
