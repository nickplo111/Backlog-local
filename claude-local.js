// Ponte local: no claude.ai o Backlog recebe "window.claude" (banco, usuário, arquivos, Google Drive).
// Aqui o servidor local (servidor.py) faz o mesmo papel, guardando tudo na pasta "dados-locais".
(() => {
  const store = new Map(), subs = [];
  const ready = fetch('/_api/db').then(r => r.json()).then(o => { Object.entries(o).forEach(([k, v]) => store.set(k, v)); }).catch(() => {});
  let chain = Promise.resolve();
  const persist = (op, path, data) => {
    chain = chain.then(() => fetch('/_api/db', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ op, path, data }) }).then(r => { if (!r.ok) throw Object.assign(new Error('save'), { code: 'unavailable' }); }));
    return chain.catch(e => { chain = Promise.resolve(); throw e; });
  };
  const clone = v => JSON.parse(JSON.stringify(v));
  const merge = (a, b) => { for (const k of Object.keys(b)) { if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k])) { if (!a[k] || typeof a[k] !== 'object') a[k] = {}; merge(a[k], b[k]); } else a[k] = b[k]; } };
  const snapOf = p => { const d = store.get(p); return { id: p.split('/').pop(), exists: d !== undefined, data: () => d, metadata: { fromCache: false, hasPendingWrites: false } }; };
  const notify = () => subs.forEach(s => s());
  const db = {
    doc(path) {
      return {
        get: async () => { await ready; return snapOf(path); },
        set: async d => { await ready; store.set(path, clone(d)); notify(); await persist('set', path, d); },
        update: async d => {
          await ready;
          if (!store.has(path)) { const e = new Error('documento inexistente'); e.code = 'invalid_argument'; throw e; }
          merge(store.get(path), clone(d)); notify(); await persist('update', path, d);
        },
        delete: async () => { await ready; store.delete(path); notify(); await persist('delete', path); },
        onSnapshot: next => { const f = () => next(snapOf(path)); subs.push(f); ready.then(f); return () => { const i = subs.indexOf(f); if (i >= 0) subs.splice(i, 1); }; },
      };
    },
    collection(c) {
      return {
        onSnapshot: next => {
          const f = () => { const docs = [...store.keys()].filter(k => k.startsWith(c + '/') && k.split('/').length === c.split('/').length + 1).map(snapOf); next({ docs, size: docs.length, empty: !docs.length, metadata: { fromCache: false } }); };
          subs.push(f); ready.then(f);
          return () => { const i = subs.indexOf(f); if (i >= 0) subs.splice(i, 1); };
        },
      };
    },
  };
  const user = { can: () => true, isOwner: () => true, canEdit: () => true, id: async () => 'local' };
  // "Auto-completar" e preenchimento de diretor: aqui a busca é feita na Wikipedia pelo servidor local
  const sample = { json: async prompt => { const r = await fetch('/_api/sample', { method: 'POST', headers: { 'content-type': 'text/plain; charset=utf-8' }, body: prompt }); return r.json(); } };
  const assets = {
    upload: async blob => {
      const r = await fetch('/_api/asset', { method: 'POST', headers: { 'content-type': blob.type || 'application/octet-stream' }, body: blob });
      if (!r.ok) throw Object.assign(new Error('upload'), { code: 'unavailable' });
      const j = await r.json();
      return { id: j.id, url: '/_blob/' + j.id };
    },
  };
  const downloads = {
    save: async ({ filename, data }) => {
      const blob = data instanceof Blob ? data : new Blob([data], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = filename || 'arquivo';
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      return { status: 'saved' };
    },
  };
  const mcp = {
    callTool: async (server, tool, input) => {
      if (tool === 'download_file_content') {
        const fmt = /spreadsheetml/.test(input.exportMimeType || '') ? 'xlsx' : 'csv';
        const r = await fetch('/_api/drive?id=' + encodeURIComponent(input.fileId) + '&fmt=' + fmt);
        const j = await r.json().catch(() => ({}));
        if (!r.ok || !j.content) throw Object.assign(new Error(j.message || 'o Google não deixou baixar a planilha'), { code: 'tool_error' });
        return { content: [], payload: { id: input.fileId, mimeType: j.mimeType, title: 'Planilha', content: j.content } };
      }
      throw Object.assign(new Error('Criar planilhas no Google Drive só funciona no claude.ai. Aqui use o menu ⋮ › Exportar CSV.'), { code: 'tool_error' });
    },
  };
  window.BACKLOG_LOCAL = true;
  window.claude = { use: async n => ({ db, user, assets, downloads, mcp, sample }[n] || null) };
  // no computador local, "nuvem" é a pasta dados-locais
  const trocaTexto = () => { const t = document.getElementById('statusTxt'); if (t && /Salvo na nuvem/.test(t.textContent)) t.textContent = t.textContent.replace('Salvo na nuvem', 'Salvo neste computador'); };
  document.addEventListener('DOMContentLoaded', () => { new MutationObserver(trocaTexto).observe(document.body, { subtree: true, childList: true, characterData: true }); trocaTexto(); });
  // avisa o servidor que esta aba está aberta; quando a última fechar, o servidor se encerra sozinho
  const aba = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const ping = () => fetch('/_api/ping?id=' + aba, { method: 'POST', keepalive: true }).catch(() => {});
  ping(); setInterval(ping, 5000);
  addEventListener('pageshow', ping);
  addEventListener('pagehide', () => { try { navigator.sendBeacon('/_api/bye?id=' + aba); } catch (e) { /* o servidor percebe pela falta de sinal */ } });
  // aviso de progresso da tarefa que completa o catálogo (capas, autores de livros, notas do IMDb)
  let avisoVisto = 0;
  const aviso = () => {
    fetch('/_api/tarefa').then(r => r.json()).then(t => {
      let el = document.getElementById('tarefaAviso');
      const achou = (t.capas || 0) + (t.autores || 0) + (t.notas || 0) + (t.titulos || 0);
      if (!t.ativo && !(t.terminou_em && t.terminou_em !== avisoVisto && achou)) { if (el && !t.terminou_em) el.remove(); return; }
      if (!el) {
        el = document.createElement('div'); el.id = 'tarefaAviso';
        el.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:300;max-width:340px;padding:12px 14px;border-radius:12px;background:#1a1a1e;color:#f0efec;border:1px solid rgba(255,255,255,.13);box-shadow:0 12px 30px rgba(0,0,0,.45);font:13px/1.4 Inter,system-ui,sans-serif';
        document.body.append(el);
      }
      const resumo = `${t.capas || 0} capas · ${t.autores || 0} autores · ${t.notas || 0} notas IMDb · ${t.titulos || 0} títulos em português`;
      if (t.ativo) el.innerHTML = `<b>Completando o catálogo…</b><br>${t.feitos} de ${t.total} itens verificados<br><span style="color:#8a8984">${resumo}</span>`;
      else {
        el.innerHTML = `<b>Catálogo completado</b><br>${resumo}<br><button type="button" style="margin-top:8px;padding:6px 12px;border-radius:8px;border:0;background:#f43f5e;color:#fff;font-weight:700;cursor:pointer">Recarregar para ver</button> <button type="button" style="margin-top:8px;padding:6px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.2);background:transparent;color:#c3c2b7;cursor:pointer">Fechar</button>`;
        const [rec, fec] = el.querySelectorAll('button');
        rec.onclick = () => location.reload();
        fec.onclick = () => { avisoVisto = t.terminou_em; el.remove(); };
      }
    }).catch(() => {});
  };
  setInterval(aviso, 6000); setTimeout(aviso, 2500);
})();
