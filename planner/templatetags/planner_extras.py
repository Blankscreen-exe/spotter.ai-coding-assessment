import re

from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(needs_autoescape=True)
def code_spans(text, autoescape=True):
    """Show `backticked` words as highlighted code. Everything else stays escaped text."""
    escaped = conditional_escape(text) if autoescape else text
    return mark_safe(re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped))
