#!/usr/bin/env python3
"""Servidor local do Backlog (só usa a biblioteca padrão do Python 3.8+).

Uso:  python servidor.py [porta]      (padrão: 8765)
Abre http://localhost:8765 . Os dados ficam em ./dados-locais (db.json e assets/).
O servidor só aceita conexões deste computador (127.0.0.1).
"""
import base64, hashlib, json, mimetypes, os, re, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
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
ATUALIZAVEIS = {'backlog.html', 'claude-local.js', 'servidor.py', 'icone.png', 'icone.ico', 'LEIA-ME.txt'}

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
