#!/usr/bin/env python3
"""Servidor local do Backlog (só usa a biblioteca padrão do Python 3.8+).

Uso:  python servidor.py [porta]      (padrão: 8765)
Abre http://localhost:8765 . Os dados ficam em ./dados-locais (db.json e assets/).
O servidor só aceita conexões deste computador (127.0.0.1).
"""
import base64, hashlib, json, mimetypes, os, re, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

AQUI = os.path.dirname(os.path.abspath(__file__))
DADOS = os.path.join(AQUI, 'dados-locais')
ASSETS = os.path.join(DADOS, 'assets')
DB = os.path.join(DADOS, 'db.json')
os.makedirs(ASSETS, exist_ok=True)
LOCK = threading.Lock()
ABAS = {}                 # id da aba -> última vez que avisou que está aberta
ESTADO = {'ja_teve_aba': False, 'vazio_desde': None}
ESPERA_FECHAR = 6         # segundos sem nenhuma aba (dá tempo de recarregar a página)
ABA_SUMIU = 150           # segundos sem sinal: aba travada ou navegador fechado à força
EXT = {'image/webp': '.webp', 'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/avif': '.avif'}

def carregar():
    try:
        with open(DB, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def gravar(db):
    tmp = DB + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, DB)

def mesclar(a, b):
    for k, v in b.items():
        if isinstance(v, dict):
            if not isinstance(a.get(k), dict):
                a[k] = {}
            mesclar(a[k], v)
        else:
            a[k] = v

# ---------------------------------------------------------------- atualização automática
# O app é publicado em https://github.com/nickplo111/Backlog-local . Ao abrir, o servidor confere o
# arquivo versao.json de lá e baixa só os arquivos que mudaram. Seus dados (dados-locais, dados.json,
# capas) e os atalhos (.bat) nunca são tocados. Sem internet, ele segue com a versão que você já tem.
URL_ATUALIZACAO = os.environ.get('BACKLOG_UPDATE_URL', 'https://raw.githubusercontent.com/nickplo111/Backlog-local/main').rstrip('/')
ATUALIZAVEIS = {'backlog.html', 'claude-local.js', 'servidor.py', 'icone.png', 'icone.ico', 'LEIA-ME.txt', 'mudancas.json'}

def _baixar(nome, tempo=15):
    req = urllib.request.Request(f'{URL_ATUALIZACAO}/{nome}', headers={'User-Agent': 'Backlog-local', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=tempo) as r:
        return r.read()

def atualizar():
    """Devolve a lista de arquivos atualizados (vazia se já estava em dia ou sem internet)."""
    try:
        info = json.loads(_baixar('versao.json', 6).decode('utf-8'))
        arquivos = info['arquivos']
    except Exception:
        return []
    feitos = []
    for nome, sha in arquivos.items():
        if nome not in ATUALIZAVEIS or not re.fullmatch(r'[0-9a-f]{64}', str(sha)):
            continue
        destino = os.path.join(AQUI, nome)
        try:
            atual = hashlib.sha256(open(destino, 'rb').read()).hexdigest() if os.path.isfile(destino) else ''
        except OSError:
            atual = ''
        if atual == sha:
            continue
        try:
            novo = _baixar(nome)
            if hashlib.sha256(novo).hexdigest() != sha:
                continue          # download corrompido: mantém o arquivo atual
            tmp = destino + '.novo'
            with open(tmp, 'wb') as f:
                f.write(novo)
            os.replace(tmp, destino)
            feitos.append(nome)
        except Exception:
            try:
                os.remove(destino + '.novo')
            except OSError:
                pass
    if feitos:
        try:
            with open(os.path.join(DADOS, 'versao.txt'), 'w', encoding='utf-8') as f:
                f.write(str(info.get('versao', '')))
        except OSError:
            pass
    return feitos

# ---------------------------------------------------------------- fichas (diretor, desenvolvedora, nota IMDb) e capas
# Fontes, nesta ordem, até uma achar: Steam (games: desenvolvedora + publicadora e capa), Wikipedia (inglês),
# Wikipedia (português), Wikidata e, para livros, Open Library. Se uma barrar (limite de acesso, fora do ar),
# passa para a próxima. Nota IMDb: vem sozinha se houver chave OMDb em dados-locais/omdb_chave.txt
# (grátis em omdbapi.com). Sem a chave, o campo fica em branco.
CAMPO = {'Filme': ('director', 'film'), 'Filme/Curta': ('director', 'film'), 'Documentário': ('director', 'documentary'),
         'Série': ('creator', 'television series'), 'Anime': ('director', 'anime series'), 'Livro': ('author', 'novel book'),
         'Game': ('developer', 'video game')}
UA = {'User-Agent': 'Backlog-local/1.0 (catalogo pessoal)'}
WD_PROP = {'Filme': 'P57', 'Filme/Curta': 'P57', 'Documentário': 'P57', 'Anime': 'P57', 'Série': 'P170', 'Livro': 'P50', 'Game': 'P178'}

def _pedido(url, tempo=12, binario=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=tempo) as r:
        dados = r.read()
    return dados if binario else dados.decode('utf-8')

def _json(url, tempo=12):
    return json.loads(_pedido(url, tempo))

def _chave(nome):
    return re.sub(r'[^a-z0-9]', '', nome.lower())

def _parecido(nome, titulo):
    a, b = _chave(nome), _chave(titulo)
    return bool(a) and (a == b or a in b)

def _ano(texto):
    m = re.search(r'\b(19\d{2}|20\d{2})\b', texto or '')
    return m.group(1) if m else ''

def _limpa(v):
    v = re.sub(r'<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->', '', v, flags=re.S)
    anterior = None
    while anterior != v:
        anterior = v
        v = re.sub(r'\{\{(?:plainlist|flatlist|ubl|hlist|unbulleted list|collapsible list)\s*\|', '\n', v, flags=re.I)
        v = re.sub(r'\{\{[^{}]*\}\}', '', v)
    v = v.replace('}}', '')
    v = re.sub(r'\[\[(?:[^\]|]*\|)?([^\]]*)\]\]', r'\1', v)
    v = re.sub(r'<br\s*/?>', '\n', v, flags=re.I)
    v = re.sub(r"'{2,}", '', v)
    partes = []
    for linha in re.split(r'\n|\*', v):
        linha = linha.strip(' +\t')
        if linha and not re.match(r'^(plainlist|flatlist|ubl|hlist)\b', linha, flags=re.I):
            partes.append(linha)
    vistos, out = set(), []
    for x in partes:
        if x not in vistos:
            vistos.add(x); out.append(x)
    return ' + '.join(out)[:200]

def _campo_bruto(texto, nome):
    m = re.search(r'^\s*\|\s*' + nome + r'\s*=(.*?)(?=^\s*\||\Z|^\}\})', texto, flags=re.S | re.M)
    return m.group(1) if m else ''

def _campo(texto, nome):
    return _limpa(_campo_bruto(texto, nome))

def chave_omdb():
    try:
        with open(os.path.join(DADOS, 'omdb_chave.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''

def nota_imdb(imdb_id):
    chave = chave_omdb()
    if not chave or not imdb_id:
        return ''
    d = _json(f'https://www.omdbapi.com/?i={imdb_id}&apikey={chave}')
    nota = str(d.get('imdbRating') or '')
    return nota if re.fullmatch(r'\d{1,2}(\.\d)?', nota) else ''

# ---- fontes de ficha
def fonte_steam(nome, ano):
    busca = _json('https://store.steampowered.com/api/storesearch/?' + urllib.parse.urlencode({'term': nome, 'l': 'english', 'cc': 'us'}))
    for it in busca.get('items', [])[:5]:
        if not _parecido(nome, it.get('name', '')):
            continue
        det = _json(f"https://store.steampowered.com/api/appdetails?appids={it['id']}&cc=br&l=portuguese")
        dados = (det.get(str(it['id'])) or {}).get('data') or {}
        devs = [x for x in dados.get('developers', []) if x]
        pubs = [x for x in dados.get('publishers', []) if x and x not in devs]
        autor = ' - '.join(x for x in [' + '.join(devs), ' + '.join(pubs)] if x)
        if autor:
            return {'titulo': it.get('name', nome), 'autor': autor, 'ano': _ano((dados.get('release_date') or {}).get('date', '')),
                    'imdbId': '', 'fonte': 'Steam', 'appid': it['id']}
    return None

def fonte_wikipedia(nome, tipo, lang):
    campo, palavra = CAMPO[tipo]
    q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': f'{nome} {palavra}', 'srlimit': 3, 'format': 'json'})
    busca = _json(f'https://{lang}.wikipedia.org/w/api.php?{q}')
    for h in busca.get('query', {}).get('search', []):
        titulo = h['title']
        if not _parecido(nome, titulo):
            continue
        q = urllib.parse.urlencode({'action': 'parse', 'page': titulo, 'prop': 'wikitext', 'redirects': 1, 'format': 'json'})
        texto = (_json(f'https://{lang}.wikipedia.org/w/api.php?{q}').get('parse', {}).get('wikitext') or {}).get('*', '')
        if not texto:
            continue
        autor = _campo(texto, campo)
        if tipo == 'Game':
            pub = _campo(texto, 'publisher')
            autor = ' - '.join(x for x in [autor, pub] if x)
        if not autor:
            continue
        imdb = re.search(r'IMDb[^\n]{0,60}?(tt\d{7,8})|imdb\.com/title/(tt\d{7,8})', texto)
        ano = _ano(_campo_bruto(texto, 'released') + ' ' + _campo_bruto(texto, 'first_aired') + ' ' + _campo_bruto(texto, 'publication_date'))
        return {'titulo': titulo, 'autor': autor, 'ano': ano, 'imdbId': (imdb.group(1) or imdb.group(2)) if imdb else '',
                'fonte': 'Wikipedia (' + ('inglês' if lang == 'en' else 'português') + ')'}
    return None

def fonte_wikidata(nome, tipo, ano):
    prop = WD_PROP.get(tipo)
    if not prop:
        return None
    busca = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbsearchentities', 'search': nome, 'language': 'en', 'type': 'item', 'limit': 5, 'format': 'json'}))
    for cand in busca.get('search', []):
        if not _parecido(nome, cand.get('label', '')):
            continue
        ent = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': cand['id'], 'props': 'claims', 'format': 'json'}))
        claims = ent.get('entities', {}).get(cand['id'], {}).get('claims', {})
        ids_pessoas = [c['mainsnak']['datavalue']['value']['id'] for c in claims.get(prop, []) if c.get('mainsnak', {}).get('datavalue')]
        pubs = []
        if tipo == 'Game':
            pubs = [c['mainsnak']['datavalue']['value']['id'] for c in claims.get('P123', []) if c.get('mainsnak', {}).get('datavalue')]
        ids = list(dict.fromkeys(ids_pessoas[:3] + pubs[:2]))
        if not ids:
            continue
        rot = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': '|'.join(ids), 'props': 'labels', 'languages': 'en', 'format': 'json'}))
        nomes = {k: v.get('labels', {}).get('en', {}).get('value', '') for k, v in rot.get('entities', {}).items()}
        dev = [nomes[i] for i in ids_pessoas[:3] if nomes.get(i)]
        pub = [nomes[i] for i in pubs[:2] if nomes.get(i)]
        autor = ' + '.join(dict.fromkeys(dev))
        if tipo == 'Game' and pub:
            autor = ' - '.join(x for x in [autor, ' + '.join(dict.fromkeys(pub))] if x)
        if not autor:
            continue
        imdb = ''
        p345 = claims.get('P345', [])
        if p345 and p345[0].get('mainsnak', {}).get('datavalue'):
            imdb = p345[0]['mainsnak']['datavalue']['value']
        anoc = ''
        for c in claims.get('P577', []):
            v = c.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('time', '')
            anoc = _ano(v)
            if anoc:
                break
        return {'titulo': cand.get('label', nome), 'autor': autor, 'ano': anoc, 'imdbId': imdb, 'fonte': 'Wikidata'}
    return None

def fonte_openlibrary(nome, ano):
    q = urllib.parse.urlencode({'title': nome, 'limit': 5})
    for d in _json(f'https://openlibrary.org/search.json?{q}').get('docs', []):
        if _parecido(nome, d.get('title', '')) and d.get('author_name'):
            return {'titulo': d.get('title', nome), 'autor': ' + '.join(d['author_name'][:3]), 'ano': str(d.get('first_publish_year') or ''),
                    'imdbId': '', 'fonte': 'Open Library', 'cover_i': d.get('cover_i')}
    return None

def ficha_de(item):
    tipo = item.get('tipo') or ''
    nome = (item.get('nome') or item.get('tituloEN') or '').strip()
    ano = str(item.get('ano') or '')
    if not nome or tipo not in CAMPO:
        return {'encontrado': False, 'confianca': 'baixa'}
    fontes = []
    if tipo == 'Game':
        fontes.append(lambda: fonte_steam(nome, ano))
    fontes.append(lambda: fonte_wikipedia(nome, tipo, 'en'))
    fontes.append(lambda: fonte_wikipedia(nome, tipo, 'pt'))
    fontes.append(lambda: fonte_wikidata(nome, tipo, ano))
    if tipo == 'Livro':
        fontes.append(lambda: fonte_openlibrary(nome, ano))
    for f in fontes:
        try:
            r = f()
        except Exception:
            continue           # esta fonte barrou ou caiu: tenta a próxima
        if r and r.get('autor'):
            try:
                nota = '' if tipo == 'Game' else nota_imdb(r.get('imdbId'))
            except Exception:
                nota = ''
            return {'encontrado': True, 'confianca': 'media', 'ident': r['titulo'], 'ptbr': '', 'en': r['titulo'],
                    'ano': r.get('ano') or ano, 'autor': r['autor'], 'imdb': nota, 'imdbId': r.get('imdbId') or '',
                    'fonte': r['fonte']}
    return {'encontrado': False, 'confianca': 'baixa'}

def responder_pedido(prompt):
    m = re.search(r'^Item:\s*(\{.*\})\s*$', prompt, flags=re.M)
    if not m:
        return {'encontrado': False, 'confianca': 'baixa'}
    try:
        item = json.loads(m.group(1))
    except ValueError:
        return {'encontrado': False, 'confianca': 'baixa'}
    return ficha_de(item)

# ---- capas
MAGIA = [(b'\xff\xd8\xff', '.jpg'), (b'\x89PNG', '.png'), (b'GIF8', '.gif')]

def _salvar_imagem(dados):
    if len(dados) < 1000 or len(dados) > 15 * 1024 * 1024:
        return None
    ext = None
    for assinatura, e in MAGIA:
        if dados.startswith(assinatura):
            ext = e
    if ext is None and dados[:4] == b'RIFF' and dados[8:12] == b'WEBP':
        ext = '.webp'
    if ext is None:
        return None
    ident = hashlib.sha256(dados).hexdigest()[:32]
    with open(os.path.join(ASSETS, ident + ext), 'wb') as f:
        f.write(dados)
    return ident

def capa_steam(nome):
    busca = _json('https://store.steampowered.com/api/storesearch/?' + urllib.parse.urlencode({'term': nome, 'l': 'english', 'cc': 'us'}))
    for it in busca.get('items', [])[:5]:
        if _parecido(nome, it.get('name', '')):
            return [f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{it['id']}/library_600x900_2x.jpg",
                    f"https://cdn.akamai.steamstatic.com/steam/apps/{it['id']}/library_600x900.jpg"]
    return []

def capa_wikipedia(nome, lang):
    q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': nome, 'srlimit': 3, 'format': 'json'})
    for h in _json(f'https://{lang}.wikipedia.org/w/api.php?{q}').get('query', {}).get('search', []):
        if not _parecido(nome, h['title']):
            continue
        q = urllib.parse.urlencode({'action': 'query', 'prop': 'pageimages', 'piprop': 'thumbnail', 'pithumbsize': 600, 'titles': h['title'], 'format': 'json'})
        for p in _json(f'https://{lang}.wikipedia.org/w/api.php?{q}').get('query', {}).get('pages', {}).values():
            if p.get('thumbnail', {}).get('source'):
                return [p['thumbnail']['source']]
    return []

def capa_openlibrary(nome):
    q = urllib.parse.urlencode({'title': nome, 'limit': 5})
    for d in _json(f'https://openlibrary.org/search.json?{q}').get('docs', []):
        if d.get('cover_i') and _parecido(nome, d.get('title', '')):
            return [f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg"]
    return []

def capa_de(item):
    tipo = item.get('tipo') or ''
    nome = (item.get('nome') or item.get('tituloEN') or '').strip()
    if not nome:
        return None
    fontes = []
    if tipo == 'Game':
        fontes.append(lambda: capa_steam(nome))
    fontes.append(lambda: capa_wikipedia(nome, 'en'))
    fontes.append(lambda: capa_wikipedia(nome, 'pt'))
    if tipo == 'Livro':
        fontes.append(lambda: capa_openlibrary(nome))
    for f in fontes:
        try:
            urls = f()
        except Exception:
            continue
        for u in urls:
            try:
                ident = _salvar_imagem(_pedido(u, 20, True))
            except Exception:
                ident = None
            if ident:
                return ident
    return None


# ---------------------------------------------------------------- mudanças de dados (itens novos/corrigidos)
# O arquivo mudancas.json do repositório lista itens para adicionar ou corrigir. Cada um tem uma "rev";
# o servidor local aplica só as revs novas e guarda o que já aplicou em dados-locais/mudancas_aplicadas.json.
def bucket_do_item(ident):
    h = 0x811c9dc5
    for ch in ident:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return 'b%02d' % (int(format(h, '08x')[:6], 16) % 16)

def aplicar_mudancas():
    caminho = os.path.join(AQUI, 'mudancas.json')
    if not os.path.isfile(caminho):
        return 0
    try:
        with open(caminho, encoding='utf-8') as f:
            lista = json.load(f).get('aplicar', [])
    except (ValueError, OSError):
        return 0
    marca = os.path.join(DADOS, 'mudancas_aplicadas.json')
    try:
        with open(marca, encoding='utf-8') as f:
            feito = json.load(f)
    except (FileNotFoundError, ValueError):
        feito = {}
    n = 0
    with LOCK:
        db = carregar()
        for mud in lista:
            ident, rev, item = str(mud.get('id', '')), int(mud.get('rev', 0)), mud.get('item') or {}
            if not ident or not isinstance(item, dict) or rev <= int(feito.get(ident, 0)):
                continue
            doc = db.setdefault('edits/' + bucket_do_item(ident), {'itens': {}})
            doc.setdefault('itens', {})[ident] = item
            feito[ident] = rev
            n += 1
        if n:
            gravar(db)
    with open(marca, 'w', encoding='utf-8') as f:
        json.dump(feito, f, ensure_ascii=False)
    return n

PAGINA = '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>:root{color-scheme:light}body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}</style><link rel="icon" type="image/png" href="/icone.png"><link rel="apple-touch-icon" href="/icone.png"><script src="/claude-local.js"></script></head><body>@@PAGINA@@</body></html>'''

class H(BaseHTTPRequestHandler):
    server_version = 'Backlog'

    def log_message(self, *a):
        pass

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _arquivo(self, caminho):
        if not os.path.isfile(caminho):
            return self._send(404, {'erro': 'não encontrado'})
        ctype = mimetypes.guess_type(caminho)[0] or 'application/octet-stream'
        with open(caminho, 'rb') as f:
            self._send(200, f.read(), ctype, {'Cache-Control': 'public, max-age=3600'} if caminho.endswith('.webp') else None)

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        if p in ('/', '/index.html'):
            with open(os.path.join(AQUI, 'backlog.html'), encoding='utf-8') as f:
                return self._send(200, PAGINA.replace('@@PAGINA@@', f.read()), 'text/html; charset=utf-8')
        if p == '/_api/db':
            with LOCK:
                return self._send(200, carregar())
        if p == '/_api/drive':
            return self._drive(parse_qs(u.query))
        if p.startswith('/_blob/'):
            nome = os.path.basename(p[7:])
            for ext in ('', '.webp', '.png', '.jpg', '.gif', '.avif'):
                c = os.path.join(ASSETS, nome + ext)
                if os.path.isfile(c):
                    return self._arquivo(c)
            return self._send(404, {'erro': 'capa não encontrada'})
        if p == '/favicon.ico':
            p = '/icone.ico'
        if p in ('/claude-local.js', '/dados.json', '/icone.png', '/icone.ico') or p.startswith('/capas/'):
            c = os.path.normpath(os.path.join(AQUI, p.lstrip('/')))
            if c.startswith(AQUI) and os.path.isfile(c):
                return self._arquivo(c)
        self._send(404, {'erro': 'não encontrado'})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        corpo = self.rfile.read(n)
        p = urlparse(self.path).path
        if p in ('/_api/ping', '/_api/bye'):
            ident = (parse_qs(urlparse(self.path).query).get('id') or [''])[0][:40]
            if ident:
                if p == '/_api/ping':
                    ABAS[ident] = time.time(); ESTADO['ja_teve_aba'] = True; ESTADO['vazio_desde'] = None
                else:
                    ABAS.pop(ident, None)
            return self._send(200, {'ok': True})
        if p == '/_api/db':
            try:
                r = json.loads(corpo.decode('utf-8'))
                op, caminho, dados = r['op'], r['path'], r.get('data')
                if not isinstance(caminho, str) or '..' in caminho:
                    raise ValueError('caminho')
            except Exception:
                return self._send(400, {'erro': 'pedido inválido'})
            with LOCK:
                db = carregar()
                if op == 'set':
                    db[caminho] = dados
                elif op == 'update':
                    if not isinstance(db.get(caminho), dict):
                        db[caminho] = {}
                    mesclar(db[caminho], dados or {})
                elif op == 'delete':
                    db.pop(caminho, None)
                else:
                    return self._send(400, {'erro': 'operação inválida'})
                gravar(db)
            return self._send(200, {'ok': True})
        if p == '/_api/capa':
            try:
                item = json.loads(corpo.decode('utf-8'))
                ident = capa_de(item)
            except Exception:
                ident = None
            return self._send(200, {'id': ident} if ident else {'erro': 'sem capa encontrada'})
        if p == '/_api/sample':
            try:
                saida = responder_pedido(corpo.decode('utf-8'))
            except Exception:
                saida = {'encontrado': False, 'confianca': 'baixa'}   # sem internet ou Wikipedia fora do ar
            return self._send(200, saida)
        if p == '/_api/asset':
            if not corpo or len(corpo) > 25 * 1024 * 1024:
                return self._send(400, {'erro': 'imagem vazia ou grande demais'})
            ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            ext = EXT.get(ctype, '.bin')
            ident = hashlib.sha256(corpo).hexdigest()[:32]
            with open(os.path.join(ASSETS, ident + ext), 'wb') as f:
                f.write(corpo)
            return self._send(200, {'id': ident})
        self._send(404, {'erro': 'não encontrado'})

    def _drive(self, q):
        """Baixa a planilha pelo link de exportação do Google. Funciona quando ela está
        compartilhada como "Qualquer pessoa com o link"; planilha privada não baixa daqui."""
        ident = (q.get('id') or [''])[0]
        fmt = 'xlsx' if (q.get('fmt') or ['csv'])[0] == 'xlsx' else 'csv'
        if not ident or not all(c.isalnum() or c in '-_' for c in ident):
            return self._send(400, {'message': 'identificador de planilha inválido'})
        url = f'https://docs.google.com/spreadsheets/d/{ident}/export?format={fmt}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Backlog-local'})
            with urllib.request.urlopen(req, timeout=60) as r:
                dados, tipo = r.read(), r.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            return self._send(502, {'message': f'o Google respondeu {e.code}. Compartilhe a planilha como "Qualquer pessoa com o link pode ver" e tente de novo'})
        except Exception as e:
            return self._send(502, {'message': f'sem acesso ao Google agora ({e.__class__.__name__}). Confira a internet'})
        if 'text/html' in tipo.lower():
            return self._send(502, {'message': 'o Google pediu login: compartilhe a planilha como "Qualquer pessoa com o link pode ver" e tente de novo'})
        mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if fmt == 'xlsx' else 'text/csv'
        self._send(200, {'mimeType': mime, 'content': base64.b64encode(dados).decode('ascii')})

def main():
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if os.environ.get('BACKLOG_SEM_ATUALIZAR') != '1':
        print('Procurando atualizações...')
        feitos = atualizar()
        if feitos:
            print('Atualizado: ' + ', '.join(feitos))
            if 'servidor.py' in feitos:       # o próprio servidor mudou: reinicia já com o código novo
                os.environ['BACKLOG_SEM_ATUALIZAR'] = '1'
                sys.exit(subprocess.call([sys.executable, os.path.join(AQUI, 'servidor.py')] + sys.argv[1:]))
        else:
            print('Já está na versão mais recente (ou sem internet).')
    n = aplicar_mudancas()
    if n:
        print(f'{n} item(ns) novo(s) ou corrigido(s) aplicado(s) do repositório.')
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', porta), H)
    except OSError:
        print(f'A porta {porta} já está em uso. Se o Backlog já está aberto, use http://localhost:{porta}')
        webbrowser.open(f'http://localhost:{porta}')
        return
    url = f'http://localhost:{porta}'
    print(f'Backlog rodando em {url}\nDeixe esta janela aberta enquanto usa. Para parar: Ctrl+C ou feche a janela.')
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def vigia():
        # encerra o servidor quando a última aba/janela do Backlog for fechada
        while True:
            time.sleep(1)
            agora = time.time()
            for k in [k for k, t in ABAS.items() if agora - t > ABA_SUMIU]:
                ABAS.pop(k, None)
            if ESTADO['ja_teve_aba'] and not ABAS:
                if ESTADO['vazio_desde'] is None:
                    ESTADO['vazio_desde'] = agora
                elif agora - ESTADO['vazio_desde'] >= ESPERA_FECHAR:
                    print('Backlog fechado. Encerrando o servidor.')
                    srv.shutdown()
                    return
    threading.Thread(target=vigia, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
