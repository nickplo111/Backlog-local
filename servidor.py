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

def _pedido_nav(url, tempo=12):
    req = urllib.request.Request(url, headers=NAVEGADOR)
    with urllib.request.urlopen(req, timeout=tempo) as r:
        return r.read().decode('utf-8', 'replace')

def log_busca(msg):
    try:
        arq = os.path.join(DADOS, 'buscas.log')
        if os.path.isfile(arq) and os.path.getsize(arq) > 400_000:
            os.replace(arq, arq + '.antigo')
        with open(arq, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except OSError:
        pass

def imdb_sugestao(nome, ano=''):
    """Caixa de busca do IMDb (JSON público): devolve (tt, url do pôster, título) ou None."""
    q = urllib.parse.quote(nome.lower().strip()[:60])
    d = json.loads(_pedido_nav(f'https://v3.sg.media-imdb.com/suggestion/x/{q}.json'))
    for it in d.get('d', []):
        tt, titulo = str(it.get('id', '')), it.get('l', '')
        if not tt.startswith('tt') or not _parecido(nome, titulo):
            continue
        if ano and str(ano).isdigit() and it.get('y') and abs(int(it['y']) - int(ano)) > 1:
            continue
        return tt, (it.get('i') or {}).get('imageUrl', ''), titulo
    return None

def fonte_imdb_busca(nome, tipo, ano=''):
    """Procura o título no IMDb e devolve o id tt... (ou '' se não achar)."""
    if tipo in ('Livro', 'Game', 'Vídeo'):
        return ''
    try:
        r = imdb_sugestao(nome, ano)
        if r:
            return r[0]
    except Exception as e:
        log_busca(f'IMDb (sugestão) falhou para {nome!r}: {e}')
    ttype = {'Série': 'tv', 'Anime': 'tv'}.get(tipo, 'ft')
    html = _pedido_nav('https://www.imdb.com/find/?' + urllib.parse.urlencode({'q': nome, 's': 'tt', 'ttype': ttype}))
    m = re.search(r'/title/(tt\d{7,8})/', html)
    return m.group(1) if m else ''

def capa_imdb(tt):
    html = _pedido_nav(f'https://www.imdb.com/title/{tt}/')
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    return [m.group(1)] if m else []

def _json(url, tempo=12):
    return json.loads(_pedido(url, tempo))

def _chave(nome):
    return re.sub(r'[^a-z0-9]', '', nome.lower())

def _parecido(nome, titulo):
    # igual, ou um contido no outro com tamanho parecido ("O Filho" não casa com "O filho de mil homens")
    a, b = _chave(nome), _chave(titulo)
    if not a or not b:
        return False
    if a == b:
        return True
    curto, longo = sorted((a, b), key=len)
    return curto in longo and len(curto) / len(longo) >= 0.6

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

# Nota IMDb: lê a página pública do título no IMDb (o id tt... vem da Wikipedia ou da Wikidata).
# O IMDb mostra a nota numa parte da página em formato JSON-LD ("aggregateRating"). Sem cadastro, sem chave.
NAVEGADOR = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
             'Accept-Language': 'en-US,en;q=0.9'}

# Nota IMDb: arquivo oficial do IMDb (title.ratings.tsv.gz, gratuito para uso pessoal), baixado de novo
# a cada 3 dias para dados-locais/imdb. A página do título no IMDb barra programas (código 202), então
# não é usada.
IMDB_DIR = os.path.join(DADOS, 'imdb')
_NOTAS = {}

def arquivo_notas_imdb(max_dias=3):
    os.makedirs(IMDB_DIR, exist_ok=True)
    arq = os.path.join(IMDB_DIR, 'title.ratings.tsv.gz')
    if not os.path.isfile(arq) or time.time() - os.path.getmtime(arq) > max_dias * 86400:
        try:
            dados = _pedido('https://datasets.imdbws.com/title.ratings.tsv.gz', 180, True)
            if dados[:2] == b'\x1f\x8b':
                with open(arq + '.novo', 'wb') as f:
                    f.write(dados)
                os.replace(arq + '.novo', arq)
                _NOTAS.clear()
        except Exception as e:
            log_busca(f'notas do IMDb: não consegui baixar o arquivo oficial ({e})')
    return arq if os.path.isfile(arq) else None

def notas_imdb_lote(ids):
    import gzip
    ids = {i for i in ids if i and re.fullmatch(r'tt\d{7,9}', i)}
    falta = ids - set(_NOTAS)
    if falta:
        arq = arquivo_notas_imdb()
        if arq:
            with gzip.open(arq, 'rt', encoding='utf-8') as f:
                next(f, None)
                for linha in f:
                    tt = linha[:linha.find('\t')]
                    if tt in falta:
                        _NOTAS[tt] = linha.split('\t')[1]
                        falta.discard(tt)
                        if not falta:
                            break
            for tt in falta:
                _NOTAS[tt] = ''
    return {i: _NOTAS.get(i, '') for i in ids}

def nota_imdb(imdb_id):
    return notas_imdb_lote([imdb_id]).get(imdb_id, '') if imdb_id else ''

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

TIPOS_TITULO = ('Filme', 'Filme/Curta', 'Série', 'Documentário', 'Anime', 'Livro')

# ---- fontes novas: IMDb (busca), Wikidata pelo código do IMDb, TVmaze (séries), Google Livros
def imdb_poster(url, largura=600):
    # pede ao IMDb uma versão menor do pôster (o original pode ter vários MB)
    return re.sub(r'\._V1_.*?\.jpg$', f'._V1_SX{largura}.jpg', url) if '._V1_' in url else url

def fonte_wikidata_por_imdb(tt, tipo):
    if not tt:
        return None
    q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': f'haswbstatement:P345={tt}', 'srlimit': 1, 'format': 'json'})
    res = _json(f'https://www.wikidata.org/w/api.php?{q}').get('query', {}).get('search', [])
    if not res:
        return None
    qid = res[0]['title']
    ent = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': qid, 'props': 'claims|labels', 'languages': 'pt-br|pt|en', 'format': 'json'}))
    e = ent.get('entities', {}).get(qid, {})
    claims, lab = e.get('claims', {}), e.get('labels', {})
    def ids_de(p):
        return [c['mainsnak']['datavalue']['value']['id'] for c in claims.get(p, []) if c.get('mainsnak', {}).get('datavalue')]
    pessoas = ids_de(WD_PROP.get(_tipo_base(tipo), 'P57'))
    if _tipo_base(tipo) == 'Série' and not pessoas:
        pessoas = ids_de('P58')            # sem "criador": usa o roteirista
    autor = ''
    if pessoas:
        rot = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': '|'.join(pessoas[:3]), 'props': 'labels', 'languages': 'en|pt', 'format': 'json'}))
        nomes = []
        for i in pessoas[:3]:
            l = rot.get('entities', {}).get(i, {}).get('labels', {})
            v = (l.get('en') or l.get('pt') or {}).get('value')
            if v:
                nomes.append(v)
        autor = ' + '.join(dict.fromkeys(nomes))
    ano = ''
    for c in claims.get('P577', []):
        ano = _ano(c.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('time', ''))
        if ano:
            break
    pt = (lab.get('pt-br') or lab.get('pt') or {}).get('value', '')
    en = (lab.get('en') or {}).get('value', '')
    return {'titulo': en, 'autor': autor, 'ano': ano, 'imdbId': tt, 'ptbr': _limpa_titulo(pt), 'fonte': 'Wikidata (pelo IMDb)'}

def fonte_tvmaze(nome):
    d = _json('https://api.tvmaze.com/singlesearch/shows?' + urllib.parse.urlencode({'q': nome}))
    if not d or not _parecido(nome, d.get('name', '')):
        return None
    return {'titulo': d.get('name', ''), 'ano': (d.get('premiered') or '')[:4], 'imdbId': (d.get('externals') or {}).get('imdb') or '',
            'capa': (d.get('image') or {}).get('original') or ''}

def fonte_google_livros(nome):
    q = urllib.parse.urlencode({'q': f'intitle:"{nome}"', 'maxResults': 5, 'printType': 'books'})
    for v in _json(f'https://www.googleapis.com/books/v1/volumes?{q}').get('items', []):
        info = v.get('volumeInfo', {})
        if _parecido(nome, info.get('title', '')) and info.get('authors'):
            capa = (info.get('imageLinks') or {}).get('thumbnail', '').replace('http://', 'https://')
            return {'titulo': info.get('title', nome), 'autor': ' + '.join(info['authors'][:3]), 'ano': _ano(info.get('publishedDate', '')),
                    'imdbId': '', 'fonte': 'Google Livros', 'capa': capa}
    return None

def _limpa_titulo(t):
    # tira o "(filme)", "(série de televisão)", "(livro)", "(2019)"... que a Wikipedia põe no fim
    t = re.sub(r'\s*\((?:[^)]*\b(?:filme|série|serie|livro|romance|conto|jogo|anime|televis|telenovela|minissérie|documentário)\b[^)]*|\d{4})\)\s*$', '', t or '', flags=re.I)
    return t.strip()

def titulo_pt(nome, tipo):
    """Título em português: artigo em inglês -> versão em português; senão busca na Wikipedia em
    português; senão o rótulo em português da Wikidata. Devolve '' se não achar."""
    if not nome or _tipo_base(tipo) not in TIPOS_TITULO:
        return ''
    campo, palavra = CAMPO.get(_tipo_base(tipo), ('', ''))
    try:
        q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': f'{nome} {palavra}', 'srlimit': 3, 'format': 'json'})
        for h in _json(f'https://en.wikipedia.org/w/api.php?{q}').get('query', {}).get('search', []):
            if not _parecido(nome, h['title']):
                continue
            q = urllib.parse.urlencode({'action': 'query', 'prop': 'langlinks', 'lllang': 'pt', 'titles': h['title'], 'redirects': 1, 'format': 'json'})
            for pg in _json(f'https://en.wikipedia.org/w/api.php?{q}').get('query', {}).get('pages', {}).values():
                for ll in pg.get('langlinks', []):
                    if ll.get('*'):
                        return _limpa_titulo(ll['*'])
            break
    except Exception:
        pass
    try:
        q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srsearch': nome, 'srlimit': 3, 'format': 'json'})
        for h in _json(f'https://pt.wikipedia.org/w/api.php?{q}').get('query', {}).get('search', []):
            if _parecido(nome, h['title']):
                return _limpa_titulo(h['title'])
    except Exception:
        pass
    try:
        busca = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbsearchentities', 'search': nome, 'language': 'en', 'type': 'item', 'limit': 3, 'format': 'json'}))
        for cand in busca.get('search', []):
            if not _parecido(nome, cand.get('label', '')):
                continue
            ent = _json('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': cand['id'], 'props': 'labels', 'languages': 'pt-br|pt', 'format': 'json'}))
            lab = ent.get('entities', {}).get(cand['id'], {}).get('labels', {})
            v = (lab.get('pt-br') or lab.get('pt') or {}).get('value', '')
            if v:
                return _limpa_titulo(v)
    except Exception:
        pass
    return ''

def _tentar(f, rotulo, nome):
    try:
        return f()
    except Exception as e:
        log_busca(f'{rotulo} falhou para {nome!r}: {e}')
        return None

def ficha_de(item):
    """Junta, de várias fontes, diretor/autor, ano, código e nota do IMDb e título em português."""
    tipo_orig = item.get('tipo') or ''
    tipo = _tipo_base(tipo_orig)
    nome = (item.get('nome') or item.get('tituloEN') or '').strip()
    nome = re.sub(r'\s*\((season|temporada|parte)[^)]*\)\s*$', '', nome, flags=re.I)
    ano = str(item.get('ano') or '')
    ano = ano if ano.isdigit() else ''
    if not nome or tipo not in CAMPO:
        return {'encontrado': False, 'confianca': 'baixa'}
    out = {'autor': '', 'ano': '', 'imdbId': item.get('imdbId') or '', 'ptbr': '', 'en': '', 'fonte': []}
    def junta(r):
        if not r:
            return
        for k in ('autor', 'ano', 'imdbId', 'ptbr'):
            if r.get(k) and not out[k]:
                out[k] = r[k]
        if r.get('titulo') and not out['en']:
            out['en'] = r['titulo']
        if r.get('fonte'):
            out['fonte'].append(r['fonte'])
    if tipo in VIDEO:
        if not out['imdbId']:
            s = _tentar(lambda: imdb_sugestao(nome, ano), 'IMDb (busca)', nome)
            if s:
                out['imdbId'], out['en'] = s[0], s[2]
                out['fonte'].append('IMDb')
        if tipo == 'Série' or tipo == 'Anime':
            junta(_tentar(lambda: fonte_tvmaze(nome), 'TVmaze', nome))
        junta(_tentar(lambda: fonte_wikidata_por_imdb(out['imdbId'], tipo), 'Wikidata (pelo IMDb)', nome))
    fontes = []
    if tipo == 'Game':
        fontes.append(('Steam', lambda: fonte_steam(nome, ano)))
    if tipo == 'Livro':
        fontes.append(('Google Livros', lambda: fonte_google_livros(nome)))
        fontes.append(('Open Library', lambda: fonte_openlibrary(nome, ano)))
    fontes += [('Wikipedia (inglês)', lambda: fonte_wikipedia(nome, tipo, 'en')), ('Wikipedia (português)', lambda: fonte_wikipedia(nome, tipo, 'pt')),
               ('Wikidata', lambda: fonte_wikidata(nome, tipo, ano))]
    for rotulo, f in fontes:
        if out['autor'] and (out['imdbId'] or tipo not in VIDEO):
            break
        junta(_tentar(f, rotulo, nome))
    if tipo in VIDEO and not out['imdbId']:
        out['imdbId'] = _tentar(lambda: fonte_imdb_busca(nome, tipo, ano), 'IMDb (procura)', nome) or ''
    nota = ''
    if tipo in VIDEO and out['imdbId']:
        nota = _tentar(lambda: nota_imdb(out['imdbId']), 'nota do IMDb', nome) or ''
    if not out['ptbr'] and tipo in TIPOS_TITULO:
        out['ptbr'] = _tentar(lambda: titulo_pt(out['en'] or nome, tipo), 'título em português', nome) or ''
    if out['ptbr'] and _chave(out['ptbr']) in (_chave(out['en'] or ''), _chave(nome)):
        out['ptbr'] = ''        # igual ao título em inglês: não é um título em português de verdade
    achou = bool(out['autor'] or out['imdbId'] or nota)
    if not achou:
        return {'encontrado': False, 'confianca': 'baixa'}
    return {'encontrado': True, 'confianca': 'media', 'ident': out['en'] or nome, 'ptbr': out['ptbr'], 'en': out['en'],
            'ano': out['ano'] or ano, 'autor': out['autor'], 'imdb': nota, 'imdbId': out['imdbId'],
            'fonte': ', '.join(dict.fromkeys(out['fonte']))}

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

def capa_de(item, tt=None):
    tipo = _tipo_base(item.get('tipo'))
    nome = (item.get('nome') or item.get('tituloEN') or '').strip()
    nome = re.sub(r'\s*\((season|temporada|parte)[^)]*\)\s*$', '', nome, flags=re.I)
    if not nome:
        return None
    ano = str(item.get('ano') or '')
    fontes = []
    if tipo in VIDEO:
        fontes.append(('IMDb', lambda: [imdb_poster(x) for x in [(imdb_sugestao(nome, ano if ano.isdigit() else '') or (None, '', ''))[1]] if x]))
        if tipo in ('Série', 'Anime'):
            fontes.append(('TVmaze', lambda: [x for x in [(fonte_tvmaze(nome) or {}).get('capa')] if x]))
    if tipo == 'Game':
        fontes.append(('Steam', lambda: capa_steam(nome)))
    if tipo == 'Livro':
        fontes.append(('Open Library', lambda: capa_openlibrary(nome)))
        fontes.append(('Google Livros', lambda: [x for x in [(fonte_google_livros(nome) or {}).get('capa')] if x]))
    fontes.append(('Wikipedia (inglês)', lambda: capa_wikipedia(nome, 'en')))
    fontes.append(('Wikipedia (português)', lambda: capa_wikipedia(nome, 'pt')))
    for rotulo, f in fontes:
        urls = _tentar(f, f'capa ({rotulo})', nome) or []
        for u in urls:
            try:
                ident = _salvar_imagem(_pedido(u, 25, True))
            except Exception as e:
                log_busca(f'capa ({rotulo}) de {nome!r}: download falhou ({e})')
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

def completar_item(item):
    """Preenche o que faltar (diretor, ano, id e nota do IMDb, capa) usando as fontes deste computador."""
    busca = dict(item)
    if item.get('buscar_como'):
        busca['nome'] = item['buscar_como']
    tipo = item.get('tipo') or ''
    if tipo in CAMPO and not item.get('autor'):
        try:
            r = ficha_de(busca)
        except Exception:
            r = {}
        if r.get('encontrado'):
            item['autor'] = r.get('autor', '')
            item['ano'] = item.get('ano') or r.get('ano', '')
            item['imdb'] = item.get('imdb') or r.get('imdb', '')
            item['imdbId'] = item.get('imdbId') or r.get('imdbId', '')
    if _vazio(item.get('ptbr')) and _tipo_base(tipo) in TIPOS_TITULO:
        try:
            pt = titulo_pt(item.get('en') or busca['nome'], tipo)
            if pt and _chave(pt) != _chave(item.get('en') or busca['nome']):
                item['ptbr'] = pt
        except Exception:
            pass
    if tipo != 'Game' and not item.get('imdbId'):
        try:
            item['imdbId'] = fonte_imdb_busca(busca['nome'], tipo)
        except Exception:
            pass
    if tipo != 'Game' and item.get('imdbId') and not item.get('imdb'):
        try:
            item['imdb'] = nota_imdb(item['imdbId'])
        except Exception:
            pass
    if not item.get('capaId'):
        try:
            ident = capa_de(busca, item.get('imdbId') or None)
        except Exception:
            ident = None
        if ident:
            item['capaId'] = ident
    return item

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
            url_capa = item.pop('capa_url', '')
            if url_capa and not item.get('capaId'):
                try:
                    cid = _salvar_imagem(_pedido(url_capa, 25, True))
                    if cid:
                        item['capaId'] = cid
                except Exception as e:
                    log_busca(f'capa enviada para {ident}: download falhou ({e})')
            if item.pop('auto', False):
                item = completar_item(dict(item))
            item.pop('buscar_como', None)
            doc = db.setdefault('edits/' + bucket_do_item(ident), {'itens': {}})
            antigo = doc.setdefault('itens', {}).get(ident) or {}
            # campos vazios na mudança não apagam o que este computador já preencheu (diretor, capa, nota...)
            doc['itens'][ident] = {**antigo, **{k: v for k, v in item.items() if v not in ('', None) or k not in antigo}}
            feito[ident] = rev
            n += 1
        if n:
            gravar(db)
    with open(marca, 'w', encoding='utf-8') as f:
        json.dump(feito, f, ensure_ascii=False)
    return n

# ---------------------------------------------------------------- tarefas de completar o catálogo
# O mudancas.json pode pedir tarefas ("tarefas": [{"id": ..., "tipos": [...]}]). Cada uma roda uma vez, em
# segundo plano, depois que o servidor abre: capas que faltam, autor de livros, notas do IMDb. O progresso
# fica em dados-locais/tarefas.json; se o Backlog fechar no meio, continua de onde parou na próxima vez.
PROGRESSO = {'ativo': False, 'feitos': 0, 'total': 0, 'capas': 0, 'autores': 0, 'notas': 0, 'terminou_em': 0}
VIDEO = ('Filme', 'Filme/Curta', 'Série', 'Documentário', 'Anime')

def _vazio(v):
    return (not v) or str(v).strip().lower() in ('sem info', 'n/a', '-', '?')

def _tipo_base(tipo):
    tipo = tipo or ''
    return 'Livro' if tipo.startswith('Livro') else tipo

def itens_atuais(db):
    base = {}
    docs = [k for k in db if k.startswith('base/c')]
    if docs:
        for k in sorted(docs):
            for it in (db[k] or {}).get('itens', []):
                base[it['id']] = dict(it)
    else:
        try:
            with open(os.path.join(AQUI, 'dados.json'), encoding='utf-8') as f:
                for it in json.load(f).get('itens', []):
                    base[it['id']] = dict(it)
        except (OSError, ValueError, KeyError):
            pass
    for k, v in db.items():
        if k.startswith('edits/'):
            for i, e in ((v or {}).get('itens') or {}).items():
                base[i] = {**base.get(i, {}), **e, 'id': i}
    return [x for x in base.values() if not x.get('excluido')]

def _gravar_campos(ident, campos):
    with LOCK:
        db = carregar()
        doc = db.setdefault('edits/' + bucket_do_item(ident), {'itens': {}})
        atual = doc.setdefault('itens', {}).get(ident) or {}
        doc['itens'][ident] = {**atual, **campos, 'atualizadoEm': int(time.time() * 1000)}
        gravar(db)

def _completar_um(it, tipos):
    tipo = _tipo_base(it.get('tipo'))
    nome = (it.get('en') or it.get('nome') or it.get('ptbr') or '').strip()
    nome = re.sub(r'\s*\((season|temporada|parte)[^)]*\)\s*$', '', nome, flags=re.I)
    if not nome:
        return {}
    busca = {'tipo': tipo, 'nome': nome, 'ano': '' if _vazio(it.get('ano')) else it.get('ano')}
    campos = {}
    if 'autores_livros' in tipos and tipo == 'Livro' and _vazio(it.get('autor')):
        try:
            r = ficha_de(busca)
        except Exception:
            r = {}
        if r.get('encontrado') and r.get('autor'):
            campos['autor'] = r['autor']
            if _vazio(it.get('ano')) and r.get('ano'):
                campos['ano'] = r['ano']
    if 'titulos_pt' in tipos and tipo in TIPOS_TITULO and _vazio(it.get('ptbr')):
        try:
            pt = titulo_pt(nome, tipo)
        except Exception:
            pt = ''
        if pt and _chave(pt) != _chave(nome):
            campos['ptbr'] = pt
    imdb_id = it.get('imdbId') or ''
    if 'notas_imdb' in tipos and tipo in VIDEO and _vazio(it.get('imdb')):
        try:
            if not imdb_id:
                imdb_id = fonte_imdb_busca(nome, tipo)
            nota = nota_imdb(imdb_id) if imdb_id else ''
        except Exception:
            nota = ''
        if imdb_id and imdb_id != it.get('imdbId'):
            campos['imdbId'] = imdb_id
        if nota:
            campos['imdb'] = nota
    if 'capas' in tipos and not it.get('capaId') and not it.get('sp'):
        try:
            ident = capa_de(busca, imdb_id or None)
        except Exception:
            ident = None
        if ident:
            campos['capaId'] = ident
    return campos

def rodar_tarefas():
    try:
        with open(os.path.join(AQUI, 'mudancas.json'), encoding='utf-8') as f:
            tarefas = json.load(f).get('tarefas', [])
    except (OSError, ValueError):
        return
    arq = os.path.join(DADOS, 'tarefas.json')
    try:
        with open(arq, encoding='utf-8') as f:
            estado = json.load(f)
    except (FileNotFoundError, ValueError):
        estado = {}
    for t in tarefas:
        tid, tipos = str(t.get('id', '')), set(t.get('tipos') or [])
        st = estado.setdefault(tid, {'feito': [], 'fim': False})
        repetir = bool(t.get('repetir'))
        tentativas = st.setdefault('tentativas', {})
        if not tid or (st.get('fim') and not repetir):
            continue
        with LOCK:
            itens = itens_atuais(carregar())
        alvo = []
        for it in itens:
            tipo = _tipo_base(it.get('tipo'))
            if (not repetir and it['id'] in st['feito']) or (repetir and time.time() - tentativas.get(it['id'], 0) < 86400):
                continue
            if (('capas' in tipos and not it.get('capaId') and not it.get('sp'))
                    or ('autores_livros' in tipos and tipo == 'Livro' and _vazio(it.get('autor')))
                    or ('notas_imdb' in tipos and tipo in VIDEO and _vazio(it.get('imdb')))
                    or ('titulos_pt' in tipos and tipo in TIPOS_TITULO and _vazio(it.get('ptbr')))):
                alvo.append(it)
        PROGRESSO.update({'ativo': True, 'feitos': 0, 'total': len(alvo)})
        for it in alvo:
            campos = _completar_um(it, tipos)
            if campos:
                _gravar_campos(it['id'], campos)
                PROGRESSO['capas'] += 'capaId' in campos
                PROGRESSO['autores'] += 'autor' in campos
                PROGRESSO['notas'] += 'imdb' in campos
                PROGRESSO['titulos'] = PROGRESSO.get('titulos', 0) + ('ptbr' in campos)
            if repetir:
                tentativas[it['id']] = int(time.time())
            else:
                st['feito'].append(it['id'])
            PROGRESSO['feitos'] += 1
            with open(arq, 'w', encoding='utf-8') as f:
                json.dump(estado, f, ensure_ascii=False)
            time.sleep(1)          # uma consulta por segundo, para não ser barrado pelos sites
        st['fim'] = not repetir
        with open(arq, 'w', encoding='utf-8') as f:
            json.dump(estado, f, ensure_ascii=False)
    if PROGRESSO['ativo']:
        PROGRESSO.update({'ativo': False, 'terminou_em': int(time.time())})

def pagina_diagnostico():
    """Testa cada fonte deste computador e mostra o resultado (para descobrir o que está barrando)."""
    testes = [
        ('Wikipedia (inglês)', lambda: str(bool(_json('https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json').get('query')))),
        ('Wikipedia (português)', lambda: str(bool(_json('https://pt.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json').get('query')))),
        ('Wikidata', lambda: str(bool(_json('https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q42&props=labels&languages=en&format=json').get('entities')))),
        ('Steam', lambda: str(len(_json('https://store.steampowered.com/api/storesearch/?term=portal&l=english&cc=us').get('items', []))) + ' resultados'),
        ('Open Library', lambda: str(_json('https://openlibrary.org/search.json?title=dune&limit=1').get('numFound', 0)) + ' resultados'),
        ('IMDb (busca rápida)', lambda: str(imdb_sugestao('The Shawshank Redemption'))),
        ('IMDb (página e nota)', lambda: 'nota ' + (nota_imdb('tt0111161') or '(não achou a nota na página)')),
    ]
    linhas = []
    for nome, f in testes:
        t0 = time.time()
        try:
            res, ok = f(), True
        except Exception as e:
            res, ok = f'{type(e).__name__}: {e}', False
        linhas.append(f'<tr><td>{nome}</td><td style="color:{"#22c55e" if ok else "#f43f5e"}">{"OK" if ok else "ERRO"}</td><td>{res}</td><td>{time.time() - t0:.1f}s</td></tr>')
    try:
        with open(os.path.join(DADOS, 'buscas.log'), encoding='utf-8') as f:
            log = f.read()[-6000:]
    except OSError:
        log = '(vazio)'
    import html as _h
    return ('<!doctype html><meta charset="utf-8"><title>Diagnóstico do Backlog</title>'
            '<body style="font:14px system-ui;background:#0b0b0d;color:#f0efec;padding:24px;max-width:1100px">'
            '<h2>Diagnóstico das fontes</h2><p>Tire um print desta página e envie.</p>'
            '<table cellpadding="8" style="border-collapse:collapse;background:#131316">'
            '<tr><th align=left>Fonte</th><th>Status</th><th align=left>Resultado</th><th>Tempo</th></tr>'
            + ''.join(linhas) + '</table><h3>Últimas buscas</h3><pre style="white-space:pre-wrap;background:#131316;padding:12px">'
            + _h.escape(log) + '</pre></body>')

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
        if p == '/diagnostico':
            return self._send(200, pagina_diagnostico(), 'text/html; charset=utf-8')
        if p == '/_api/tarefa':
            return self._send(200, PROGRESSO)
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
                if op == 'set' and caminho.startswith('edits/') and isinstance(db.get(caminho), dict) and isinstance(dados, dict):
                    mesclar(db[caminho], dados)
                elif op == 'set':
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
    threading.Thread(target=rodar_tarefas, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
