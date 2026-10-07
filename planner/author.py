"""Who built this, as shown in the page's "About" drawer.

Edit this file to change what the drawer says. Anything left empty is simply
not shown. Each link's kind picks its icon: github, linkedin, website, blog,
x or email.
"""

BIO_MAX_LENGTH = 120

AUTHOR = {
    'name': 'M. Hammad',
    'role': '',
    'location': '',
    'bio': 'I bring ideas to life, MVPs to production and confusion to clarity.',
    'links': [
        {'kind': 'github', 'label': 'GitHub', 'url': 'https://github.com/Blankscreen-exe'},
        {'kind': 'linkedin', 'label': 'LinkedIn', 'url': 'https://www.linkedin.com/in/hammadai/'},
        {'kind': 'website', 'label': 'Website', 'url': 'https://hammad-ai.vercel.app'},
        {'kind': 'blog', 'label': 'Blog', 'url': 'https://hammadai.hashnode.dev/'},
        {'kind': 'x', 'label': 'X', 'url': 'https://x.com/blankscreenexe'},
    ],
    'project': {
        'label': 'Source code for this project',
        'url': 'https://github.com/Blankscreen-exe/spotter.ai-coding-assessment',
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
