"""Turns your web index.html into the Tauri version.
Usage: put your page next to this file as original.html, then run:  python patch.py
Output: dist/index.html"""
import re, sys, pathlib

src = pathlib.Path("original.html")
if not src.exists():
    sys.exit("Put your page next to patch.py and name it original.html")
s = src.read_text(encoding="utf-8").replace("\r\n", "\n")

NEW_PLAY = r"""let prevIdx=-1;
async function play(i){
  const t=T[i];if(!t)return;
  go(i);
  if(t.demo){nudge();return}
  graph();now=i;bar();session(t);
  if(!t.u){                                   // read the song from disk through Rust
    const buf=await invoke('read_range',{path:t.path,start:0,len:t.size});
    if(now!==i)return;                        // user already skipped to another song
    t.u=URL.createObjectURL(new Blob([buf],{type:t.mime}));
  }
  const old=prevIdx>=0&&prevIdx!==i?T[prevIdx]:null;
  A.src=t.u;A.play().catch(()=>{});prevIdx=i;
  if(old&&old.u){URL.revokeObjectURL(old.u);old.u=null}   // free memory of the previous song
}
"""

NEW_LOAD = r"""/* ---------- native folder loading + remembered folder (Tauri / Rust) ---------- */
const TAURI=window.__TAURI__,invoke=TAURI.core.invoke;
const MIME={mp3:'audio/mpeg',m4a:'audio/mp4',flac:'audio/flac',ogg:'audio/ogg',opus:'audio/ogg',wav:'audio/wav',aac:'audio/aac',wma:'audio/x-ms-wma'};
const chunk=(path,start,len)=>invoke('read_range',{path,start,len});
// tiny File-like object so the built-in tag reader works unchanged
const mkFile=it=>({name:it.rel.split('/').pop(),size:it.size,type:'',slice:(a,b)=>({arrayBuffer:()=>chunk(it.path,a,b-a)})});
async function loadFolder(path){
  $('#status').textContent='Scanning\u2026';
  let res;try{res=await invoke('scan',{path})}catch(e){$('#status').textContent='Folder not found';return}
  if(!res.audio.length){$('#status').textContent='No songs found';return}
  covers={};
  res.covers.forEach(c=>{
    c.type='image/'+c.path.split('.').pop().toLowerCase().replace('jpg','jpeg');
    c.bytes=()=>chunk(c.path,0,c.size);
    covers[c.rel.split('/').slice(0,-1).join('/')]=c;
  });
  A.pause();if(T[prevIdx]&&T[prevIdx].u)URL.revokeObjectURL(T[prevIdx].u);
  prevIdx=-1;now=-1;sel=0;bgCss='';
  res.audio.sort((a,b)=>a.rel.localeCompare(b.rel,undefined,{numeric:true}));
  T=res.audio.map(it=>{
    const p=it.rel.split('/'),n=p.length;
    const t={file:mkFile(it),path:it.path,size:it.size,mime:MIME[it.path.split('.').pop().toLowerCase()]||'audio/mpeg',
      title:clean(p[n-1]),artist:n>2?p[n-3]:'',album:n>1?p[n-2]:'',dir:p.slice(0,-1).join('/')};
    t.hue=hash(t.album+t.title);return t;
  });
  build();go(0);bar();readTags();
}
$('#open').onclick=async()=>{
  const p=await TAURI.dialog.open({directory:true,title:'Choose your music folder'});
  if(p)loadFolder(p);
};
"""

def sub(pattern, repl, text, label):
    out, n = re.subn(pattern, lambda m: repl, text, count=1, flags=re.S)
    if n != 1:
        sys.exit(f"Patch failed at: {label}. Is original.html the latest version?")
    return out

s = sub(r"function play\(i\)\{.*?\n\}\n", NEW_PLAY, s, "play()")
s = sub(r"\$\('#open'\)\.onclick=.*?readTags\(\);\n\};", NEW_LOAD, s, "folder loading")
s = sub(r"thumb\(f,f\.type\)", "f.bytes().then(b=>thumb(b,f.type))", s, "folder cover.jpg")
s = sub(r"build\(\);go\(0\);bar\(\);\n</script>",
        "build();go(0);bar();\ninvoke('last_folder').then(p=>p||invoke('void_music')).then(async p=>{if(!p)return;await loadFolder(p);adoptRoot(p)}).catch(e=>logErr('startup',e));   // reopen the last folder, or C:\\VOID\\Music the first time\n</script>",
        s, "startup")

# ---------------------------------------------------------------------
#  spotDL (native): replaces the old spotDL code AND the old spotDL window
# ---------------------------------------------------------------------
SPOTDL_JS = r"""/* ---------- spotDL (native, runs through Rust) ---------- */
const spotdlBtn=$('#spotdlBtn');
const sd=id=>document.getElementById(id);
let spotdlOpen=false,spotdlBusy=false,sdToolsP=null,sdFolder=null,spdTotal=0,spdDone=0;
function sdStatus(msg,type){const el=sd('spotdlStatus');if(!el)return;el.textContent=msg;el.className='spotdl-status '+(type||'')}
function sdBar(p,busy){const el=sd('spotdlProgress'),w=sd('spotdlProgressWrap');if(w)w.style.display='block';if(el){el.style.width=p+'%';el.classList.toggle('busy',!!busy)}}
function sdLog(line){const el=sd('spotdlLog');if(!el)return;el.style.display='block';el.textContent+=line+'\n';const l=el.textContent.split('\n');if(l.length>200)el.textContent=l.slice(-150).join('\n');el.scrollTop=el.scrollHeight}
function sdFolderLabel(){const t=sd('spotdlFolderText');if(t){t.textContent='Save to: '+(sdFolder||'(not chosen yet)');t.title=sdFolder||''}}
async function sdPickFolder(){
  const f=await TAURI.dialog.open({directory:true,title:'Choose your music folder'});
  if(f){sdFolder=f;sdFolderLabel()}
  return f;
}
// events coming from Rust (progress + live spotDL output)
try{
  TAURI.event.listen('spotdl',ev=>{
    const {kind,msg,pct}=ev.payload;
    if(kind==='progress'){sdBar(pct,false);sdStatus(msg,'')}
    else if(kind==='status'){sdStatus(msg,'');if(pct==null)sdBar(100,true);else sdBar(pct,false)}
    else if(kind==='log'){
      sdLog(msg);if(/error|fail|exception|traceback/i.test(msg))logErr('add-song output',msg);
      const m=msg.match(/Found (\d+) songs?/i);if(m)spdTotal=+m[1];
      if(/^Downloaded\b/i.test(msg)){spdDone++;if(spdTotal)sdBar(Math.min(99,spdDone/spdTotal*100),false);sdStatus('Downloaded '+spdDone+(spdTotal?' of '+spdTotal:''),'')}
    }
  }).catch(e=>console.warn('spotDL events unavailable',e));
}catch(e){console.warn('spotDL events unavailable',e)}
// get spotdl + deno + ffmpeg (does nothing if they are already there)
function sdEnsure(){return sdToolsP||(sdToolsP=invoke('spotdl_setup').catch(e=>{sdToolsP=null;throw e}))}
async function spotdlToggle(f){
  spotdlOpen=f===undefined?!spotdlOpen:f;
  const m=sd('spotdlModal');if(!m)return;
  m.classList.toggle('open',spotdlOpen);m.setAttribute('aria-hidden',!spotdlOpen);
  spotdlBtn.setAttribute('aria-pressed',spotdlOpen);
  if(!spotdlOpen)return;
  sd('spotdlInput').focus();
  if(spotdlBusy)return;
  try{
    if(!sdFolder)sdFolder=ROOT||await invoke('last_folder');
    sdFolderLabel();
    sdStatus('Checking downloader\u2026','');
    await sdEnsure();
    if(!spotdlBusy){sdStatus('Ready \u2014 paste a link or type a song name','success');const w=sd('spotdlProgressWrap');if(w)w.style.display='none'}
  }catch(e){sdStatus('Setup failed: '+(e&&e.message||e),'error')}
}
async function spotdlRun(){
  if(spotdlBusy)return;
  sdStatus('Starting\u2026','');                       // instant feedback so you know the click worked
  const url=sd('spotdlInput').value.trim();
  if(!url){sdStatus('Paste a link or type a song name','error');return}
  const isUrl=/^https?:\/\//.test(url);if(!isUrl&&/^https?:/i.test(url)){sdStatus('That link looks incomplete','error');return}
  spotdlBusy=true;sd('spotdlRun').disabled=true;sd('spotdlLog').textContent='';spdTotal=0;spdDone=0;
  sd('spotdlQuote').classList.add('show');
  try{
    sdLog(isUrl?'[1/4] Link OK':'[1/4] Searching for "'+url+'"');
    if(!sdFolder)sdFolder=ROOT||await invoke('last_folder');
    if(!sdFolder){sdLog('Choose the folder where your music lives\u2026');await sdPickFolder()}
    if(!sdFolder)throw new Error('No music folder chosen');
    sdFolderLabel();
    sdLog('[2/4] Saving to '+sdFolder);
    const st=await invoke('spotdl_tools_status');
    const ok=b=>b?'found':'MISSING (will download)';
    sdLog('[3/4] Tools folder: '+st.dir);
    sdLog('      spotDL: '+ok(st.spotdl)+' | deno: '+ok(st.deno)+' | ffmpeg: '+ok(st.ffmpeg));
    await sdEnsure();
    const visible=sd('spotdlTerm').checked;
    sdLog('[4/4] Starting spotDL'+(visible?' - a terminal window opens now':''));
    const added=await invoke('spotdl_download',{url,folder:sdFolder,visible});
    sdBar(100,false);sdStatus('Done \u2014 adding to your library\u2026','success');
    await sdAdd(sdFolder,added);
    sdStatus(added.length?('Added '+added.length+' song'+(added.length>1?'s':'')+' to your library'):'Finished (nothing new was added)','success');
  }catch(e){sdBar(0,false);const msg=String(e&&e.message||e);sdStatus(msg,'error');sdLog('ERROR: '+msg);logErr('add-song',msg)}
  finally{spotdlBusy=false;sd('spotdlRun').disabled=false;sd('spotdlQuote').classList.remove('show')}
}
// add new songs to the open library without stopping what is playing
async function sdAdd(folder,items){
  if(!items.length)return;
  try{
    if(!ALL.length){await loadFolder(folder);return}
    tagAdd(addItems(items));
  }catch(e){await loadFolder(folder)}
}
spotdlBtn.onclick=e=>{e.stopPropagation();spotdlToggle()};
// one listener for everything inside the window (capture phase, so nothing else can swallow the click)
document.addEventListener('click',e=>{
  const t=e.target&&e.target.closest?e.target.closest('#spotdlRun,#spotdlClose,#spotdlBackdrop,#spotdlFolderBtn'):null;
  if(!t)return;
  e.stopPropagation();
  if(t.id==='spotdlRun')spotdlRun().catch(err=>sdStatus(String(err&&err.message||err),'error'));
  else if(t.id==='spotdlFolderBtn')sdPickFolder().catch(err=>sdStatus(String(err&&err.message||err),'error'));
  else spotdlToggle(false);
},true);
// the spotDL window sits further down the page than this script, so wire it up once the page has loaded
function sdInit(){
  sd('spotdlInput').addEventListener('keydown',e=>{if(e.key==='Enter')spotdlRun().catch(err=>sdStatus(String(err&&err.message||err),'error'))});
  try{if(localStorage.getItem('void.spotdl.term')==='0')sd('spotdlTerm').checked=false}catch(e){}
  sd('spotdlTerm').addEventListener('change',e=>{try{localStorage.setItem('void.spotdl.term',e.target.checked?'1':'0')}catch(x){}});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',sdInit);else sdInit();
addEventListener('keydown',e=>{if(e.key==='Escape'&&spotdlOpen)spotdlToggle(false)});

"""

LIB_CSS = """
.vpop{position:fixed;z-index:6000;min-width:250px;max-width:min(340px,92vw);max-height:62vh;overflow:auto;border-radius:20px;padding:8px;opacity:0;pointer-events:none;transform:translateY(-8px) scale(.98);transition:opacity .25s var(--ease),transform .25s var(--ease)}
.vpop.open{opacity:1;pointer-events:auto;transform:none}
.vhead{padding:10px 12px 6px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink2)}
.vnote{padding:8px 12px 12px;font-size:13px;line-height:1.45;color:var(--ink2)}
.vrow{display:flex;align-items:center;gap:12px;width:100%;padding:10px 12px;border-radius:12px;text-align:left;font-size:14px;color:var(--ink)}
.vrow span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vrow:hover{background:rgba(255,255,255,.13)}
.vrow.on{background:rgba(255,255,255,.17);color:#22d3ee}
.vrow small{flex:none;color:var(--ink2);font-size:12px}
.vinput{width:calc(100% - 8px);margin:4px;padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.16);background:rgba(0,0,0,.3);color:var(--ink);font:inherit;outline:0}
.vinput:focus{border-color:rgba(34,211,238,.6)}
.vtoast{position:fixed;left:50%;bottom:112px;transform:translate(-50%,10px);padding:10px 20px;border-radius:999px;font-size:13px;max-width:86vw;text-align:center;opacity:0;pointer-events:none;transition:opacity .35s var(--ease),transform .35s var(--ease);z-index:7000}
.vtoast.show{opacity:1;transform:translate(-50%,0)}
#heart[aria-pressed=true]{color:#ff5a7a}
#heart[aria-pressed=true] svg{fill:currentColor}
body.lite *,body.lite *::before,body.lite *::after{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}
body.lite .glass{background:linear-gradient(135deg,rgba(255,255,255,.2),rgba(255,255,255,.09))!important}
body.lite .card.sel{background:linear-gradient(135deg,rgba(255,255,255,.3),rgba(255,255,255,.14))!important}
body.lite::before{display:none!important}
body.lite .bg i{filter:blur(24px) saturate(1.6) brightness(.85)!important}
body.lite .vpop,body.lite .vtoast,body.lite .spotdl-content{background:rgba(26,16,44,.92)!important}
body.bgmode *,body.bgmode *::before,body.bgmode *::after{animation-play-state:paused!important;transition-duration:0s!important}
#perfBtn[aria-pressed=true]{color:#22d3ee}
#plusBtn[aria-pressed=true],#libBtn[aria-pressed=true],#queueBtn[aria-pressed=true],#sleepBtn[aria-pressed=true]{color:#22d3ee}
"""

DOCK_HTML = """
    <button class="fab glass" id="plusBtn" data-tip="Import / new playlist" aria-label="Import songs or new playlist"><svg class="ic" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
    <button class="fab glass" id="libBtn" data-tip="Library & playlists" aria-label="Library and playlists"><svg class="ic" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></button>
    <button class="fab glass" id="queueBtn" data-tip="Queue" aria-label="Queue" aria-pressed="false"><svg class="ic" viewBox="0 0 24 24"><path d="M4 6h12M4 12h12M4 18h8"/><path d="m18 14 4 3-4 3z"/></svg></button>
    <button class="fab glass" id="perfBtn" data-tip="Smooth mode" aria-label="Smooth mode" aria-pressed="false"><svg class="ic" viewBox="0 0 24 24"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg></button>
    <button class="fab glass" id="sleepBtn" data-tip="Sleep timer" aria-label="Sleep timer" aria-pressed="false"><svg class="ic" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
"""
HEART_HTML = """
  <button class="ctl tg" id="heart" title="Add to Liked Songs" aria-label="Like song" aria-pressed="false"><svg class="ic" viewBox="0 0 24 24"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/></svg></button>"""

SPOTDL_CSS = """
.spotdl-x{position:absolute;top:12px;right:14px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:var(--ink);font-size:22px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.spotdl-x:hover{background:rgba(255,255,255,.2)}
.spotdl-folder{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;font-size:12px;color:var(--ink2);text-align:left}
.spotdl-folder span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spotdl-folder button{flex:none;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.08);color:var(--ink);border-radius:10px;padding:5px 12px;font:inherit;cursor:pointer}
.spotdl-folder button:hover{background:rgba(255,255,255,.18)}
.spotdl-opt{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:14px;font-size:12px;color:var(--ink2);cursor:pointer}
.spotdl-log{display:none;margin:12px 0 0;max-height:150px;overflow:auto;text-align:left;font:11px/1.45 ui-monospace,Consolas,monospace;color:var(--ink2);background:rgba(0,0,0,.35);border-radius:12px;padding:10px;white-space:pre-wrap;word-break:break-word}
.spotdl-quote{max-height:0;opacity:0;overflow:hidden;margin-top:0;text-align:center;font-size:13px;font-style:italic;line-height:1.5;letter-spacing:.01em;background:linear-gradient(90deg,var(--ink2),#22d3ee,var(--ink2));background-size:200% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;transition:max-height .5s ease,opacity .5s ease,margin .5s ease}
.spotdl-quote.show{max-height:70px;opacity:1;margin-top:14px;animation:sdlShine 4s linear infinite}
@keyframes sdlShine{to{background-position:-200% 0}}
.spotdl-progress.busy{animation:sdlPulse 1.2s ease-in-out infinite}
@keyframes sdlPulse{50%{opacity:.35}}
"""

SPOTDL_MODAL = """<!-- spotDL modal -->
<div class="spotdl-modal glass" id="spotdlModal" role="dialog" aria-modal="true" aria-labelledby="spotdlTitle" aria-hidden="true">
  <div class="spotdl-backdrop" id="spotdlBackdrop"></div>
  <div class="spotdl-content">
    <button type="button" class="spotdl-x" id="spotdlClose" aria-label="Close">&times;</button>
    <h2 id="spotdlTitle">Add song</h2>
    <input type="text" id="spotdlInput" placeholder="Paste a link or search a song..." aria-label="Music link" spellcheck="false" autocomplete="off">
    <div class="spotdl-folder"><span id="spotdlFolderText">Save to: (not chosen yet)</span><button type="button" id="spotdlFolderBtn">Change</button></div>
    <button type="button" class="spotdl-run glass" id="spotdlRun">Add song</button>
    <div class="spotdl-progress-wrap" id="spotdlProgressWrap" style="height:4px;background:rgba(255,255,255,.1);border-radius:2px;margin:12px 0 8px;overflow:hidden;display:none">
      <div class="spotdl-progress" id="spotdlProgress" style="height:100%;width:0%;background:linear-gradient(90deg,#22d3ee,#22d3ee);border-radius:2px;transition:width .3s ease"></div>
    </div>
    <div class="spotdl-status" id="spotdlStatus" aria-live="polite"></div>
    <div class="spotdl-quote" id="spotdlQuote">Be patient \u2014 your music is downloading.<br>Good things take a moment.</div>
    <label class="spotdl-opt"><input type="checkbox" id="spotdlTerm" checked> Show terminal window</label>
    <pre class="spotdl-log" id="spotdlLog"></pre>
  </div>
</div>
"""

LIB_JS = r"""/* ---------- library: all songs, playlists, liked songs, queue, sleep timer ---------- */
const LIKED='Liked Songs',SEP='\\';
let ALL=[],ROOT='',ROOTN='',VIEW='all',DIRS=[],QUEUE=[],playTrack=null,vpopOwner=null,vpopAnchor=null;
const pl=t=>{const p=t.dir.split('/');return p.length===2?p[1]:''};            // playlist = first folder inside the music folder
const inView=t=>VIEW==='all'?pl(t)!==LIKED:pl(t)===VIEW;
const bn=p=>p.split(/[\\/]/).pop().toLowerCase();
const cnt=v=>ALL.filter(t=>v==='all'?pl(t)!==LIKED:pl(t)===v).length;
function lists(){const s=new Set(DIRS),deep=new Set();ALL.forEach(t=>{const p=t.dir.split('/');if(p.length>2)deep.add(p[1]);else if(p.length===2)s.add(p[1])});deep.forEach(d=>s.delete(d));s.delete(LIKED);return[...s].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}))}  // a playlist = a folder that holds songs directly (artist/album folders are not playlists)
function mkTrack(it){
  const p=it.rel.split('/'),n=p.length;
  const t={file:mkFile(it),path:it.path,size:it.size,mime:MIME[it.path.split('.').pop().toLowerCase()]||'audio/mpeg',
    title:clean(p[n-1]),artist:n>2?p[n-3]:'',album:n>1?p[n-2]:'',dir:p.slice(0,-1).join('/')};
  t.hue=hash(t.album+t.title);return t;
}
// called by the folder loader right after a music folder was scanned
function libInit(path){
  ALL=T.slice();ROOT=path;ROOTN=path.split(/[\\\\/]/).filter(Boolean).pop()||'Music';VIEW='all';playTrack=null;
  T=ALL.filter(inView);
  invoke('list_dirs',{path}).then(d=>{
    DIRS=d;
    const saved=mem.get('libraryView','all');
    if(saved==='all'||saved===LIKED||d.includes(saved)) setView(saved);
    else { VIEW='all'; mem.set('libraryView','all'); libRefresh(); }
  }).catch(()=>{});
}
/* small glass toast */
const vtoast=document.createElement('div');vtoast.className='vtoast glass';document.body.append(vtoast);
let vtoastT=0;
function toast(msg){vtoast.textContent=msg;vtoast.classList.add('show');clearTimeout(vtoastT);vtoastT=setTimeout(()=>vtoast.classList.remove('show'),2600)}
/* tags + cover for tracks that were added later */
async function tagOne(t){
  try{
    const r=await readMeta(t.file);
    if(r.title)t.title=r.title;if(r.artist)t.artist=r.artist;if(r.album)t.album=r.album;
    if(r.picture)t.cb=await thumb(r.picture.data,r.picture.format);
    t._tg=1;
    const i=T.indexOf(t);
    if(i>=0&&cards[i]){cards[i].querySelector('b').textContent=t.title;cards[i].querySelector('span').textContent=t.artist;cards[i]._a=0}
  }catch(e){t._tg=1}
}
async function tagAdd(list){
  const q=list.filter(t=>!t._tg);
  const w=async()=>{while(q.length)await tagOne(q.shift())};
  await Promise.all([w(),w(),w()]);
  if(cards.length&&T.length){layout();bg();bar()}
}
function addItems(items){
  const have=new Set(ALL.map(t=>t.path)),added=[];
  for(const it of items){if(have.has(it.path))continue;have.add(it.path);const t=mkTrack(it);ALL.push(t);added.push(t)}
  const vis=added.filter(inView);
  if(vis.length){const first=!T.length;T.push(...vis);build();if(first)go(0);else layout();viewStatus()}
  libRefresh();return added;
}
function removeTrack(t){
  ALL=ALL.filter(x=>x!==t);QUEUE=QUEUE.filter(x=>x!==t);
  const i=T.indexOf(t);
  if(i>=0){
    T.splice(i,1);if(now===i)now=-1;else if(now>i)now--;
    build();if(T.length){go(Math.min(sel,T.length-1))}
    viewStatus();
  }
  if(playTrack===t)playTrack=null;
}
function viewStatus(){$('#status').textContent=(VIEW==='all'?'All songs':VIEW)+' \u00b7 '+T.length+' song'+(T.length===1?'':'s')+(T.length?'':' \u2014 use + to add some')}
function setView(v){
  VIEW=v;mem.set('libraryView',v);run++;                // persist selected playlist between launches
  T=ALL.filter(inView);now=playTrack?T.indexOf(playTrack):-1;
  build();
  if(T.length){go(Math.max(0,now));bar();tagAdd(T.filter(t=>!t._tg))}
  viewStatus();
  libRefresh();
}
/* remember which song is loaded (for the heart) */
const _play=play;
play=function(i){playTrack=T[i]||playTrack;const r=_play(i);heartSync();return r};
/* queue: Next (and auto-advance) takes songs from the queue first */
function qSet(){const b=$('#queueBtn');if(b){b.dataset.tip='Queue'+(QUEUE.length?' ('+QUEUE.length+')':'');b.setAttribute('aria-pressed',QUEUE.length?'true':'false')}}
function qAdd(t,next){if(!t||t.demo)return;QUEUE=QUEUE.filter(x=>x!==t);next?QUEUE.unshift(t):QUEUE.push(t);qSet();libRefresh();toast(next?'Playing next: '+t.title:'Added to queue: '+t.title)}
function qPlay(t){
  let i=T.indexOf(t);
  if(i<0){setView(pl(t)===LIKED?LIKED:'all');i=T.indexOf(t)}
  if(i>=0)play(i);
}
function qNext(){const t=QUEUE.shift();qSet();libRefresh();if(t)qPlay(t)}
/* heart = copy into the "Liked Songs" folder (created the first time) */
const heartEl=$('#heart');
const likedCopy=t=>ALL.find(x=>pl(x)===LIKED&&bn(x.path)===bn(t.path));
function heartSync(){
  const t=playTrack,on=!!t&&!t.demo&&(pl(t)===LIKED||!!likedCopy(t));
  heartEl.setAttribute('aria-pressed',on?'true':'false');heartEl.title=on?'Remove from Liked Songs':'Add to Liked Songs';
}
async function toggleLike(t){
  if(!t||t.demo){toast('Play a song from your library first');return}
  if(!ROOT){toast('Choose your music folder first');return}
  try{
    const lc=pl(t)===LIKED?t:likedCopy(t);
    if(lc){await invoke('delete_liked',{path:lc.path});removeTrack(lc);toast('Removed from Liked Songs')}
    else{
      const items=await invoke('copy_into',{paths:[t.path],dest:ROOT+SEP+LIKED,root:ROOT});
      const [c]=addItems(items);
      if(c){c.title=t.title;c.artist=t.artist;c.album=t.album;c.cb=t.cb;c.cu=t.cu;c._tg=1}
      if(!DIRS.includes(LIKED))DIRS.push(LIKED);
      toast('Added to Liked Songs');
    }
  }catch(e){logErr('library',e);toast(String(e&&e.message||e))}
  heartSync();libRefresh();
}
heartEl.onclick=()=>toggleLike(playTrack);
/* glass popover used by every dock menu */
const vpop=document.createElement('div');vpop.className='vpop glass';document.body.append(vpop);
const el=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
function row(label,fn,o={}){const b=el('button','vrow'+(o.on?' on':''));b.append(el('span','',label));if(o.small!=null)b.append(el('small','',String(o.small)));b.onclick=e=>{e.stopPropagation();fn()};return b}
function popShow(owner,anchor,nodes){
  vpopOwner=owner;vpopAnchor=anchor;vpop.replaceChildren(...nodes);vpop.classList.add('open');
  const w=Math.min(vpop.offsetWidth||260,innerWidth-16);
  if(anchor.btn){const r=anchor.btn.getBoundingClientRect();vpop.style.top=(r.bottom+12)+'px';vpop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+'px'}
  else{vpop.style.top=Math.max(8,Math.min(anchor.y,innerHeight-vpop.offsetHeight-8))+'px';vpop.style.left=Math.max(8,Math.min(anchor.x,innerWidth-w-8))+'px'}
}
function popHide(){vpop.classList.remove('open');vpopOwner=null}
function libRefresh(){if(vpopOwner==='lib')libMenu(vpopAnchor);else if(vpopOwner==='queue')queueMenu(vpopAnchor);qSet()}
function libMenu(a){
  const n=[el('div','vhead','Music folder'),row(ROOTN||'Choose your music folder',()=>{popHide();$('#open').click()},{small:'Change'}),el('div','vhead','Library')];
  n.push(row('All songs',()=>{setView('all');popHide()},{on:VIEW==='all',small:cnt('all')}));
  n.push(row('\u2665 Liked Songs',()=>{setView(LIKED);popHide()},{on:VIEW===LIKED,small:cnt(LIKED)}));
  const L=lists();if(L.length)n.push(el('div','vhead','Playlists'));
  L.forEach(name=>n.push(row(name,()=>{setView(name);popHide()},{on:VIEW===name,small:cnt(name)})));
  popShow('lib',a,n);
}
function queueMenu(a){
  const n=[el('div','vhead','Up next')];
  if(!QUEUE.length)n.push(el('div','vnote','Queue is empty. Right-click a song and choose \u201cAdd to queue\u201d.'));
  QUEUE.forEach(t=>n.push(row(t.title+(t.artist?' \u2014 '+t.artist:''),()=>{QUEUE=QUEUE.filter(x=>x!==t);qSet();queueMenu(a)},{small:'remove'})));
  if(QUEUE.length)n.push(row('Clear queue',()=>{QUEUE=[];qSet();queueMenu(a)}));
  popShow('queue',a,n);
}
/* sleep timer */
let slT=0,slTick=0,slEnd=0,slSong=false;
function sleepClear(){clearTimeout(slT);clearInterval(slTick);slT=0;slTick=0;slEnd=0;slSong=false;const b=$('#sleepBtn');b.setAttribute('aria-pressed','false');b.dataset.tip='Sleep timer'}
function sleepTip(){const b=$('#sleepBtn');b.dataset.tip=slSong?'Sleep: end of this song':'Sleep timer \u00b7 '+Math.max(1,Math.round((slEnd-Date.now())/60000))+' min left'}
function sleepFire(){
  const v0=A.volume,st=Date.now();
  const iv=setInterval(()=>{const k=(Date.now()-st)/4000;if(k>=1){clearInterval(iv);A.pause();A.volume=v0;sleepClear();toast('Sleep timer: good night')}else A.volume=v0*(1-k)},100);
}
function sleepSet(min){
  sleepClear();$('#sleepBtn').setAttribute('aria-pressed','true');
  if(min==='song'){slSong=true}
  else{slEnd=Date.now()+min*60000;slT=setTimeout(sleepFire,min*60000);slTick=setInterval(sleepTip,20000)}
  sleepTip();toast(min==='song'?'Music will stop after this song':'Music will stop in '+min+' minutes');
}
A.addEventListener('timeupdate',()=>{if(slSong&&A.duration&&A.duration-A.currentTime<.35){A.pause();sleepClear();toast('Sleep timer: good night')}});
function sleepMenu(a){
  const act=!!(slT||slSong);
  const n=[el('div','vhead','Sleep timer')];
  [15,30,45,60].forEach(m=>n.push(row(m+' minutes',()=>{sleepSet(m);popHide()})));
  n.push(row('End of this song',()=>{sleepSet('song');popHide()}));
  if(act)n.push(row('Turn off',()=>{sleepClear();popHide();toast('Sleep timer off')}));
  popShow('sleep',a,n);
}
/* + menu: import songs, new playlist, folder as playlist */
async function doImport(){
  popHide();
  if(!ROOT){toast('Choose your music folder first');return}
  const files=await TAURI.dialog.open({multiple:true,title:'Import songs',filters:[{name:'Audio',extensions:['mp3','m4a','flac','ogg','opus','wav','aac','wma']}]});
  if(!files||!files.length)return;
  const list=Array.isArray(files)?files:[files];
  const dest=(VIEW!=='all'&&VIEW!==LIKED)?ROOT+SEP+VIEW:ROOT;
  toast('Importing '+list.length+' song'+(list.length>1?'s':'')+'\u2026');
  try{const items=await invoke('copy_into',{paths:list,dest,root:ROOT});tagAdd(addItems(items));toast('Imported '+items.length+(dest!==ROOT?' into '+VIEW:''))}
  catch(e){logErr('library',e);toast(String(e&&e.message||e))}
}
async function newPlaylist(name){
  name=name.replace(/[\\/:*?"<>|]/g,'').trim();
  if(!name){toast('Give the playlist a name');return}
  try{await invoke('make_dir',{path:ROOT+SEP+name});if(!DIRS.includes(name))DIRS.push(name);popHide();setView(name);toast('Playlist \u201c'+name+'\u201d created \u2014 use + to add songs')}
  catch(e){logErr('library',e);toast(String(e&&e.message||e))}
}
function newPlaylistForm(a){
  if(!ROOT){popHide();toast('Choose your music folder first');return}
  const inp=el('input','vinput');inp.placeholder='Playlist name';inp.spellcheck=false;
  inp.onkeydown=e=>{e.stopPropagation();if(e.key==='Enter')newPlaylist(inp.value)};
  popShow('plus',a,[el('div','vhead','New playlist'),inp,row('Create',()=>newPlaylist(inp.value))]);
  setTimeout(()=>inp.focus(),30);
}
async function addFolder(){
  popHide();
  if(!ROOT){toast('Choose your music folder first');return}
  const src=await TAURI.dialog.open({directory:true,title:'Choose a folder to use as a playlist'});
  if(!src)return;
  const name=src.split(/[\\/]/).filter(Boolean).pop();
  toast('Adding \u201c'+name+'\u201d\u2026');
  try{
    const items=await invoke('import_folder',{src,root:ROOT});
    DIRS=await invoke('list_dirs',{path:ROOT});
    const added=addItems(items);setView(name);tagAdd(added);
    toast('Playlist \u201c'+name+'\u201d ready');
  }catch(e){logErr('library',e);toast(String(e&&e.message||e))}
}
function plusMenu(a){popShow('plus',a,[row('Import songs\u2026',doImport),row('New playlist\u2026',()=>newPlaylistForm(a)),row('Add folder as playlist\u2026',addFolder)])}
/* right-click a song */
function ctxMenu(x,y,t){
  const a={x,y};
  const lc=pl(t)===LIKED||likedCopy(t);
  popShow('ctx',a,[
    row('Play next',()=>{qAdd(t,true);popHide()}),
    row('Add to queue',()=>{qAdd(t,false);popHide()}),
    row('Add to playlist\u2026',()=>{
      const L=lists();
      popShow('ctx',a,[el('div','vhead','Add to playlist')].concat(L.length?L.map(name=>row(name,async()=>{
        popHide();
        try{const items=await invoke('copy_into',{paths:[t.path],dest:ROOT+SEP+name,root:ROOT});tagAdd(addItems(items));toast('Added to '+name)}catch(e){logErr('library',e);toast(String(e&&e.message||e))}
      })):[el('div','vnote','No playlists yet. Use + to create one.')]));
    }),
    row(lc?'Remove from Liked Songs':'Add to Liked Songs',()=>{popHide();toggleLike(t)})
  ]);
}
stage.addEventListener('contextmenu',e=>{
  const c=e.target.closest&&e.target.closest('.card');if(!c)return;
  const i=cards.indexOf(c);if(i<0||!T[i]||T[i].demo)return;
  e.preventDefault();ctxMenu(e.clientX,e.clientY,T[i]);
});
/* dock buttons */
const dockMenus={plusBtn:plusMenu,libBtn:libMenu,queueBtn:queueMenu,sleepBtn:sleepMenu};
Object.keys(dockMenus).forEach(id=>{
  const b=$('#'+id);
  b.onclick=e=>{e.stopPropagation();if(vpopOwner&&vpopAnchor&&vpopAnchor.btn===b){popHide();return}dockMenus[id]({btn:b})};
});
document.addEventListener('pointerdown',e=>{if(vpopOwner&&!vpop.contains(e.target)&&!(e.target.closest&&e.target.closest('#plusBtn,#libBtn,#queueBtn,#sleepBtn')))popHide()},true);
addEventListener('keydown',e=>{if(e.key==='Escape'&&vpopOwner)popHide()});
/* background mode: while the window is minimized or hidden, stop all drawing and keep only the sound */
let BG=false,bgMin=false,bgHid=false;
const _raf=window.requestAnimationFrame.bind(window),_caf=window.cancelAnimationFrame.bind(window);
const rafHeld=new Map();let rafFake=1e9;
window.requestAnimationFrame=cb=>{if(!BG)return _raf(cb);const id=++rafFake;rafHeld.set(id,cb);return id};
window.cancelAnimationFrame=id=>{if(rafHeld.has(id))rafHeld.delete(id);else _caf(id)};
const _tu=A.ontimeupdate;
A.ontimeupdate=function(e){if(BG)return;return _tu&&_tu.call(this,e)};
function setBg(){
  const on=bgMin||bgHid;if(on===BG)return;
  BG=on;document.body.classList.toggle('bgmode',on);
  if(on)return;
  /* back on screen: restart every animation loop and redraw everything right away */
  const held=[...rafHeld.values()];rafHeld.clear();held.forEach(cb=>_raf(cb));
  try{_tu&&_tu()}catch(e){}
  try{if(cards.length&&T.length){layout();bg();bar()}}catch(e){}
}
try{TAURI.event.listen('void-minimized',e=>{bgMin=!!e.payload;setBg()}).catch(()=>{})}catch(e){}
document.addEventListener('visibilitychange',()=>{bgHid=document.hidden;setBg()});
/* errors and notable events are written to C:\VOID\Logs\void.log by the app */
function logErr(ctx,e){try{invoke('log_event',{level:'ERROR',msg:ctx+': '+String((e&&(e.stack||e.message))||e)})}catch(x){}}
addEventListener('error',e=>logErr('window',(e.message||'error')+' (line '+e.lineno+')'));
addEventListener('unhandledrejection',e=>logErr('promise',e.reason));
/* the first time there may be no songs yet: still make C:\VOID\Music the working folder */
function adoptRoot(p){
  if(!ROOT){ROOT=p;ROOTN=p.split(/[\\/]/).filter(Boolean).pop()||'Music';VIEW='all';invoke('list_dirs',{path:p}).then(d=>{DIRS=d;libRefresh()}).catch(()=>{})}
  if(!ALL.length){const st=$('#status');if(st)st.textContent='Your VOID library is empty \u2014 use + or Add song to add music'}
}
/* smooth mode: lighter effects. Turns on by itself when the screen is slow; the lightning button switches it by hand */
const perfBtn=$('#perfBtn');
function setLite(on,save){
  document.body.classList.toggle('lite',on);perfBtn.setAttribute('aria-pressed',on?'true':'false');
  perfBtn.dataset.tip=on?'Smooth mode: on (tap for full glass)':'Smooth mode: off (tap for faster)';
  if(save){try{mem.set('perf',on?'lite':'fancy')}catch(e){}}
}
perfBtn.onclick=e=>{e.stopPropagation();const on=!document.body.classList.contains('lite');setLite(on,true);toast(on?'Smooth mode on \u2014 lighter effects':'Full glass effects on')};
{
  let pref='auto';try{pref=mem.get('perf','auto')}catch(e){}
  if(pref==='lite')setLite(true,false);else setLite(false,false);
  if(pref==='auto')setTimeout(()=>{
    if(document.hidden)return;
    let n=0;const t0=performance.now();
    const f=t=>{n++;if(t-t0<2500&&!document.hidden)requestAnimationFrame(f);else if(!document.hidden){
      const fps=n/((t-t0)/1000);
      if(fps<40&&!document.body.classList.contains('lite')){setLite(true,false);toast('Smooth mode turned on automatically \u2014 tap \u26a1 to switch back')}
    }};
    requestAnimationFrame(f);
  },9000);
}
/* if loading fails, say why instead of hanging on "Scanning..." */
function loadFail(msg){const st=$('#status');if(st&&/^Scanning|reading covers/.test(st.textContent)){st.textContent='Error: '+msg;try{ldrHide(true)}catch(e){}}}
addEventListener('unhandledrejection',e=>loadFail(String(e.reason&&e.reason.message||e.reason)));
addEventListener('error',e=>loadFail(String(e.message||e)));
qSet();heartSync();

"""

print("spotDL markers in original.html:",
      {k: (k in s) for k in ["spotDL: Standalone", "spotDL: VOID downloader", 'id="spotdlModal"', 'id="spotdlBtn"', "build();go(0);bar();"]})
if 'id="spotdlBtn"' not in s:
    sys.exit("This original.html has no spotDL button (id=spotdlBtn). Please use the newest VOID file.")

# 1) the old spotDL code (either known version) -> new code
OLD_JS = r"/\* -+ spotDL: (?:Standalone|VOID downloader).*?(?=build\(\);go\(0\);bar\(\);\n(?:invoke\(|</script>))"
if re.search(OLD_JS, s, flags=re.S):
    s = sub(OLD_JS, SPOTDL_JS + LIB_JS, s, "old spotDL code")
else:
    if re.search(r"const spotdlBtn|let spotdlOpen|function spotdl", s):
        sys.exit("Found old spotDL code in a form I don't recognise. Send original.html to Claude.")
    print("NOTE: old spotDL code not found - adding the new code at the end of the script instead.")
    s = sub(r"(?=build\(\);go\(0\);bar\(\);\n(?:invoke\(|</script>))", SPOTDL_JS + LIB_JS, s, "end of script")

# 2) the old spotDL window (everything from its comment to </body>) -> new window with a close button
if re.search(r"<!-- spotDL modal -->.*?(?=\s*</body>)", s, flags=re.S):
    s = sub(r"<!-- spotDL modal -->.*?(?=\s*</body>)", SPOTDL_MODAL, s, "spotDL window")
elif 'id="spotdlModal"' in s:
    s = sub(r'<div class="spotdl-modal.*?(?=\s*</body>)', SPOTDL_MODAL, s, "spotDL window")
else:
    s = sub(r"</body>", SPOTDL_MODAL + "</body>", s, "add spotDL window")

# 3) styles
s = sub(r"</style>", SPOTDL_CSS + "</style>", s, "spotDL css")

# 4) library hooks: folder loader, queue-aware Next, dock buttons, heart, styles
s = sub(r"  build\(\);go\(0\);bar\(\);readTags\(\);\n\}\n\$\('#open'\)\.onclick=async", "  try{libInit(path)}catch(e){console.error('libInit',e)}\n  build();go(0);bar();readTags();\n}\n$('#open').onclick=async", s, "library hook")
s = sub(r"const step=\(n,auto\)=>\{\n", "const step=(n,auto)=>{\n  if(n>0&&QUEUE.length){qNext();return}\n", s, "queue hook")
k = s.index('id="spotdlBtn"'); k = s.index("</button>", k) + len("</button>")
s = s[:k] + DOCK_HTML + s[k:]
k = s.index('id="rep"'); k = s.index("</button>", k) + len("</button>")
s = s[:k] + HEART_HTML + s[k:]
s = sub(r"</style>", LIB_CSS + "</style>", s, "library css")

# 5) the loading screen only closed on "N songs" - a playlist with exactly 1 song says "1 song", so it never closed
OLD_OBS = "else if(/\\d+ songs/.test(s)){"
assert OLD_OBS in s, "loader status check not found"
s = s.replace(OLD_OBS, "else if(/\\d+ songs?/.test(s)){", 1)

# 6) remove the "VOID." intro splash and the Google Fonts download (privacy: no outside requests)
s = sub(r"@import url\('https://fonts\.googleapis\.com[^)]*\)\s*;", "", s, "google fonts import")
s = sub(r'<style id="voidIntroCss">.*?</style>', "", s, "intro css")
s = sub(r'<div id="voidIntro".*?(?=<div class="bg">)', "", s, "intro markup")
s = sub(r'<script id="voidIntroJs">.*?</script>', "", s, "intro script")
s = sub(r'data-tip="Download from Spotify" aria-label="Download from Spotify"', 'data-tip="Add song" aria-label="Add song"', s, "dock button name")

out = pathlib.Path("dist/index.html")
out.parent.mkdir(exist_ok=True)
out.write_text(s, encoding="utf-8")
print("OK -> dist/index.html")
