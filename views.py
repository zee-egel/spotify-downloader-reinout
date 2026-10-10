"""HTML templates; callers retain the existing explicit escaping of user data."""
import re
from functools import lru_cache
from pathlib import Path
from string import Template

TEMPLATES = Path(__file__).with_name('templates')


@lru_cache(maxsize=None)
def template(name):
    return Template((TEMPLATES / 'server' / (name + '.html')).read_text().strip())


def render_html(template_name, **values):
    markup = template(template_name).substitute(values)
    # Boolean attributes are true by presence, even when their value says "false".
    return re.sub(r' selected="(True|False)"', lambda match: ' selected' if match[1] == 'True' else '', markup)


@lru_cache(maxsize=1)
def client_templates_json():
    import json
    fragments = {path.stem: path.read_text().strip() for path in sorted((TEMPLATES / 'client').glob('*.html'))}
    return json.dumps(fragments, ensure_ascii=False).replace('<', '\\u003c')
