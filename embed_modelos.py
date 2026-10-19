"""Ferramenta de desenvolvimento: copia modelos/*.html para dentro do index.html.

Os modelos viajam dentro do index.html para o site nunca depender de ficheiros extra no deploy.
Depois de editares um modelo em modelos/<id>.html, corre:  python embed_modelos.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(ROOT, 'index.html')
html = open(path, encoding='utf-8').read()
start = html.index('const MODELOS = [')
end = html.index('];\n', start)
models = json.loads(html[start + len('const MODELOS = '):end + 1])
for m in models:
    m['html'] = open(os.path.join(ROOT, 'modelos', m['id'] + '.html'), encoding='utf-8').read()
blob = json.dumps(models, ensure_ascii=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
open(path, 'w', encoding='utf-8').write(html[:start] + 'const MODELOS = ' + blob + html[end + 1:])
print('Modelos embutidos:', ', '.join(m['id'] for m in models))
