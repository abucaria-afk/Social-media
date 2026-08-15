async function api(path, opts={}){
  const res = await fetch('/api'+path, Object.assign({headers:{'content-type':'application/json'}}, opts));
  return res.json();
}

async function loadPosts(){
  const posts = await api('/posts');
  const cont = document.getElementById('posts');
  cont.innerHTML = '';
  if (!posts.length) cont.innerHTML = '<div class="muted">No posts yet</div>';
  for (const p of posts){
    const el = document.createElement('div');
    el.className = 'post';
    el.innerHTML = `<strong>${escapeHtml(p.title)}</strong> <div class="muted">${p.createdAt}</div>
      <div style="margin-top:6px">${escapeHtml(p.content)}</div>
      <div class="muted">${p.published ? 'Published: '+p.publishedAt : (p.scheduledFor ? 'Scheduled: '+p.scheduledFor : 'Draft')}</div>
      <div class="actions" style="margin-top:8px">
        ${p.published ? '' : `<button data-id="${p.id}" class="publish">Publish</button>
        <button data-id="${p.id}" class="schedule">Schedule (in 1m)</button>`}
      </div>`;
    cont.appendChild(el);
  }
  document.querySelectorAll('.publish').forEach(btn=>btn.onclick = async e=>{
    const id = e.target.dataset.id;
    await api('/publish',{method:'POST',body:JSON.stringify({id})});
    loadPosts();
  });
  document.querySelectorAll('.schedule').forEach(btn=>btn.onclick = async e=>{
    const id = e.target.dataset.id;
    // schedule 1 minute from now for demo
    const at = new Date(Date.now()+60*1000).toISOString();
    await api('/schedule',{method:'POST',body:JSON.stringify({id,at})});
    loadPosts();
  });
}

function escapeHtml(s){
  if (!s) return '';
  return s.replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

document.getElementById('create').onclick = async ()=>{
  const title = document.getElementById('title').value.trim();
  const content = document.getElementById('content').value.trim();
  const msg = document.getElementById('msg');
  if (!title || !content){ msg.textContent = 'title and content required'; return; }
  msg.textContent = 'Creating...';
  await api('/posts',{method:'POST',body:JSON.stringify({title,content})});
  document.getElementById('title').value='';
  document.getElementById('content').value='';
  msg.textContent = 'Created';
  loadPosts();
  setTimeout(()=>msg.textContent='',2000);
}

document.getElementById('refresh').onclick = loadPosts;

loadPosts();

// register service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
}
