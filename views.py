"""HTML templates; callers retain the existing explicit escaping of user data."""
from functools import lru_cache
from pathlib import Path
from string import Template

TEMPLATES = Path(__file__).with_name('templates')


@lru_cache(maxsize=None)
def template(name):
    return Template((TEMPLATES / 'server' / (name + '.html')).read_text().strip())


def render_html(template_name, **values):
    return template(template_name).substitute(values)


@lru_cache(maxsize=1)
def client_templates_json():
    import json
    fragments = {path.stem: path.read_text().strip() for path in sorted((TEMPLATES / 'client').glob('*.html'))}
    return json.dumps(fragments, ensure_ascii=False).replace('<', '\\u003c')
