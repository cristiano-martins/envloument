"""Ferramenta de desenvolvimento: gera as miniaturas dos modelos a partir de modelos/*.html.

Escreve static/img/modelo-<id>.jpg e miniaturas.py (as mesmas imagens em base64, servidas pela app
mesmo que a pasta static/ do deploy esteja desatualizada). Requer:  pip install playwright && playwright install chromium
Uso:  python gerar_miniaturas.py
"""
import base64
import glob
import io
import os

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
dados = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1280, 'height': 800})
    for caminho in sorted(glob.glob(os.path.join(ROOT, 'modelos', '*.html'))):
        mid = os.path.basename(caminho)[:-5]
        page.goto('file://' + caminho)
        page.wait_for_timeout(900)
        im = Image.open(io.BytesIO(page.screenshot())).convert('RGB').resize((640, 400), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=72, optimize=True, progressive=True)
        open(os.path.join(ROOT, 'static', 'img', 'modelo-%s.jpg' % mid), 'wb').write(buf.getvalue())
        dados[mid] = base64.b64encode(buf.getvalue()).decode()
        print(mid, len(buf.getvalue()) // 1024, 'KB')
    browser.close()
with open(os.path.join(ROOT, 'miniaturas.py'), 'w', encoding='utf-8') as f:
    f.write('"""Gerado por gerar_miniaturas.py (não editar à mão): miniaturas JPEG dos modelos em base64."""\nMINIATURAS = {\n')
    for mid, b64 in dados.items():
        f.write('    %r: %r,\n' % (mid, b64))
    f.write('}\n')
print('miniaturas.py gravado com', len(dados), 'modelos')
