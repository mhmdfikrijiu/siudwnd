const $ = (s, r=document) => r.querySelector(s);

let SLUG = "";
let EPS = [];
let activeJob = null;
let pollTimer = null;

function log(msg){
  const el = $("#log");
  el.textContent += msg + "\n";
  el.scrollTop = el.scrollHeight;
}
function toast(msg){
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._id);
  toast._id = setTimeout(()=> t.classList.remove("show"), 2600);
}
function setResolving(on){
  const b = $("#btnResolve");
  b.disabled = on;
  b.classList.toggle("loading", on);
}
function setActionsBusy(on){
  for(const b of document.querySelectorAll("#actions button, #listCard button")) b.disabled = on;
}

function showProgress(title){
  const c = $("#progressCard");
  c.classList.remove("hidden","done","error");
  $("#progressLabel").textContent = title;
  $("#progressBar").style.width = "6%";
  $("#progressPct").textContent = "6%";
  $("#progressMsg").textContent = "Antri…";
  $("#progressErr").classList.add("hidden");
  $("#progressErr").textContent = "";
}
function updateProgress(pct, msg){
  $("#progressBar").style.width = pct + "%";
  $("#progressPct").textContent = pct + "%";
  if(msg) $("#progressMsg").textContent = msg;
}
function finishProgress(ok, msg, err){
  const c = $("#progressCard");
  c.classList.toggle("done", ok);
  c.classList.toggle("error", !ok);
  updateProgress(ok ? 100 : 100, msg);
  if(err){
    const e = $("#progressErr");
    e.textContent = err;
    e.classList.remove("hidden");
  }
  setActionsBusy(false);
  activeJob = null;
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
}

async function pollJob(jobId){
  const r = await fetch(`/api/progress/${jobId}`);
  const j = await r.json();
  if(!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  const pct = Math.max(0, Math.min(100, j.progress ?? 0));
  updateProgress(pct, j.message || `${j.current||0}/${j.total||0}`);
  if(j.status === "done"){
    finishProgress(true, "Siap — download otomatis…");
    log(`[done] ${j.message}`);
    window.location.href = `/api/download/${jobId}`;
    toast("Download dimulai");
  } else if(j.status === "error"){
    finishProgress(false, j.message || "Gagal", j.error || j.traceback || "");
    log(`[err] ${j.error || j.message}`);
    toast(j.error || "Gagal");
  }
}

async function startJob(path, nums, label){
  if(!nums.length) return toast("Pilih episode dulu");
  if(!SLUG) return toast("Cek episode dulu");
  showProgress(label === "full" ? "FULL — gabung jadi 1 MP4" : "ZIP — bungkus episode");
  setActionsBusy(true);
  log(`[${label}] ${nums.length} episode -> ${path}`);
  try{
    const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({slug: SLUG, episodes: nums})});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    activeJob = j.job_id;
    log(`[job] ${activeJob}`);
    pollTimer = setInterval(()=> pollJob(activeJob).catch(e=>{
      finishProgress(false, String(e.message||e), String(e.message||e));
    }), 900);
    pollJob(activeJob).catch(e=> finishProgress(false, String(e.message||e), ""));
  }catch(e){
    finishProgress(false, String(e.message||e), String(e.message||e));
    log(`[err] ${e.message||e}`);
  }
}

function renderMeta(title, total, slug){
  $("#meta").classList.remove("error");
  $("#meta").textContent = `${title} — ${total} episode | slug: ${slug}`;
}
function updSel(){
  const n = document.querySelectorAll(".ck:checked").length;
  $("#selInfo").textContent = n ? `${n} terpilih · ~${(n*9).toFixed(0)} MB` : "Pilih episode untuk FULL/ZIP";
}
function render(){
  const tb = $("#tbody");
  tb.innerHTML = "";
  for(const ep of EPS){
    const tr = document.createElement("tr");
    tr.dataset.n = String(ep.n);
    tr.innerHTML = `
      <td><input type="checkbox" class="ck" data-n="${ep.n}" checked></td>
      <td>Ep${String(ep.n).padStart(2,"0")}</td>
      <td>${ep.title}</td>
      <td><a href="/api/mp4/${SLUG}/${ep.n}" target="_blank" rel="noopener"><button class="btn ghost small">MP4</button></a></td>
    `;
    tb.appendChild(tr);
  }
  $("#dramaCard").classList.remove("hidden");
  $("#listCard").classList.remove("hidden");
  updSel();
}
function applyFilter(){
  const q = ($("#filter").value || "").trim().toLowerCase();
  let shown = 0;
  for(const tr of document.querySelectorAll("#tbody tr")){
    const n = tr.dataset.n || "";
    const title = (tr.children[2]?.textContent || "").toLowerCase();
    const ok = !q || n.includes(q) || title.includes(q);
    tr.style.display = ok ? "" : "none";
    if(ok) shown++;
  }
  $("#empty").classList.toggle("hidden", shown !== 0);
}

$("#btnResolve").addEventListener("click", async ()=>{
  const url = ($("#url").value || "").trim();
  if(!url) return toast("Paste link drama dulu");
  setResolving(true);
  $("#meta").textContent = "Loading…";
  log(`[resolve] ${url}`);
  try{
    const r = await fetch("/api/resolve", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({url})});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    SLUG = j.slug;
    EPS = j.episodes || [];
    $("#title").textContent = j.title || SLUG;
    $("#total").textContent = `${j.total} episode`;
    $("#slug").textContent = SLUG;
    $("#estimate").textContent = `· ~${(j.total*9).toFixed(0)} MB FULL`;
    const poster = $("#poster");
    if(j.poster){ poster.src = j.poster; poster.alt = j.title || SLUG; } else { poster.removeAttribute("src"); }
    renderMeta(j.title || SLUG, j.total, SLUG);
    render(); applyFilter();
    log(`[ok] ${j.total} episode`);
    toast(`${j.total} episode siap`);
  }catch(e){
    $("#meta").textContent = String(e.message || e);
    $("#meta").classList.add("error");
    log(`[err] ${e.message || e}`);
    toast(String(e.message || e));
  }finally{ setResolving(false); }
});
$("#url").addEventListener("keydown", e=>{ if(e.key==="Enter") $("#btnResolve").click(); });
$("#master").addEventListener("change", e=>{ for(const c of document.querySelectorAll(".ck")) c.checked = e.target.checked; updSel(); });
$("#tbody").addEventListener("change", updSel);
$("#btnCheck").addEventListener("click", ()=>{ for(const c of document.querySelectorAll(".ck")) c.checked = true; updSel(); });
$("#btnUncheck").addEventListener("click", ()=>{ for(const c of document.querySelectorAll(".ck")) c.checked = false; updSel(); });
$("#filter").addEventListener("input", applyFilter);
$("#btnClearLog").addEventListener("click", ()=> $("#log").textContent = "");
$("#btnZip").addEventListener("click", ()=> startJob("/api/zip", [...document.querySelectorAll(".ck:checked")].map(c=> +c.dataset.n), "zip"));
$("#btnFull").addEventListener("click", ()=> startJob("/api/full", [...document.querySelectorAll(".ck:checked")].map(c=> +c.dataset.n), "full"));
$("#btnFullAll").addEventListener("click", ()=> startJob("/api/full", EPS.map(e=> e.n), "full"));
