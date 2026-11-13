"""The wizard's single page. Kept in a Python string so the program ships as one piece."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo recovery</title>
<style>
:root{--bg:#f4f1ea;--card:#fffdf8;--ink:#1f2421;--soft:#5c645f;--line:#ddd6c8;--go:#1f6f4a;--go-ink:#fff;--warn:#8a4b00;--warn-bg:#fdf1dc;--bad:#9b1c1c;--bar:#e6dfd0}
@media(prefers-color-scheme:dark){:root{--bg:#171a18;--card:#202522;--ink:#eceee9;--soft:#a3aba5;--line:#353c37;--go:#3fa272;--go-ink:#0c1a12;--warn:#f0b662;--warn-bg:#33291a;--bad:#ff8f8f;--bar:#2c332e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.55 system-ui,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:42rem;margin:0 auto;padding:2rem 1rem 4rem}
header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:1.2rem}
header b{font-size:1.15rem;letter-spacing:.01em}header span{color:var(--soft);font-size:.85rem}
ul.did{list-style:none;padding:0;margin:.9rem 0 0;color:var(--soft);font-size:.92rem}ul.did li{margin:.15rem 0}
ul.did li::before{content:"\2713";color:var(--go);margin-right:.5rem}
section{background:var(--card);border:1px solid var(--line);border-radius:.9rem;padding:1.6rem 1.5rem}
h1{font-size:1.55rem;line-height:1.25;margin:0 0 .6rem}p{margin:.5rem 0}.soft{color:var(--soft)}
.actions{display:flex;flex-wrap:wrap;gap:.7rem;margin-top:1.3rem}
button{font:inherit;border-radius:.6rem;padding:.65rem 1.15rem;border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer}
button.go{background:var(--go);border-color:var(--go);color:var(--go-ink);font-weight:600}
button:focus-visible,input:focus-visible{outline:3px solid var(--go);outline-offset:2px}
button[disabled]{opacity:.5;cursor:default}
.note{background:var(--warn-bg);color:var(--warn);border-radius:.6rem;padding:.7rem .9rem;margin:1rem 0}
.err{color:var(--bad);margin-top:.8rem}
ul.plain{padding-left:1.2rem;margin:.6rem 0}ul.plain li{margin:.25rem 0}
input[type=text]{font:inherit;width:100%;padding:.55rem .7rem;border-radius:.5rem;border:1px solid var(--line);background:transparent;color:var(--ink)}
.row{display:flex;gap:.5rem;align-items:center;margin-top:.4rem}.row input{flex:1}
progress{width:100%;height:1rem;margin:.8rem 0 .3rem;accent-color:var(--go)}
table{border-collapse:collapse;width:100%;margin-top:.8rem}td{padding:.4rem .2rem;border-bottom:1px solid var(--line);vertical-align:top}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(16rem,1fr));gap:.8rem;margin-top:1rem}
.tile{padding:0;border:2px solid var(--line);border-radius:.6rem;overflow:hidden;background:none;position:relative;line-height:0}
.tile img{width:100%;display:block}.tile:hover{border-color:var(--go)}
.tile i{position:absolute;left:0;right:0;border-top:2px dashed #fff;mix-blend-mode:difference;pointer-events:none}
img.prev{max-width:10rem;border-radius:.4rem;border:1px solid var(--line);display:block;margin:.5rem 0}
td.n{font-variant-numeric:tabular-nums;font-weight:700;width:4.5rem;font-size:1.2rem}
.why{font-size:.9rem;line-height:1.4;margin-top:.1rem}.lead{font-size:1.15rem;margin:.2rem 0 .4rem}
details{margin:.6rem 0}summary{cursor:pointer}
label.pick{display:flex;gap:.6rem;align-items:center;padding:.5rem .2rem}
</style></head><body><main>
<header><b>Photo recovery</b><span id="ver"></span></header>
<section id="view" aria-live="polite"></section>
</main>
<script>
const T="__TOKEN__", V=document.getElementById("view");
let S=null, timer=null, folderEdit=null;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const size=b=>b>=1e9?(b/1e9).toFixed(b>=1e10?0:1)+" GB":Math.round(b/1e6)+" MB";
async function api(a,body){
  const r=await fetch("/api/"+a,{method:body===undefined&&a==="state"?"GET":"POST",headers:{"X-Token":T,"Content-Type":"application/json"},body:a==="state"?undefined:JSON.stringify(body||{})});
  const j=await r.json(); if(a!=="pick"||j.step){S=j; render();} return j;
}
function err(){return S.message?`<p class="err" role="alert">${esc(S.message)}</p>`:"";}
const views={
 start(){return `<h1>Get your photos back</h1>
  <p>This copies your memory card onto this computer, then recovers deleted photos from the copy. Your card is only ever read, never changed.</p>
  <div class="note"><b>Before you start:</b> stop using the card. Don't take photos on it, don't save anything to it, and don't let anything format it.</div>
  <div class="actions"><button class="go" onclick="go('unplug')">Recover from a card</button>
  <button onclick="go('image')">I already have a card image file</button></div>`;},
 unplug(){return `<h1>First, take the card out</h1>
  <p>Unplug the memory card (or its reader) you want to recover, so the program can tell which drive it is when you plug it back in.</p>
  <p class="soft">If the card has a small lock switch on its side, slide it to <b>Lock</b>. That stops the computer writing anything to it.</p>
  <div class="actions"><button class="go" onclick="api('begin_card')">The card is unplugged</button><button onclick="go('start')">Back</button></div>`;},
 plug(){return `<h1>Now plug the card in</h1>
  <p>Plug it in and wait a few seconds.</p>
  <div class="note">If the computer offers to <b>format</b> the card or to <b>scan and fix</b> it, choose <b>Cancel</b>.</div>
  ${err()}<div class="actions"><button class="go" onclick="api('find_card')">It's plugged in</button><button onclick="api('restart')">Start again</button></div>`;},
 confirm(){
  const many=S.found.length>1;
  const list=many?S.found.map(d=>`<label class="pick"><input type="radio" name="d" ${S.drive&&S.drive.id===d.id?"checked":""} onchange="api('choose',{id:'${esc(d.id)}'})"> ${esc(d.label)}</label>`).join(""):"";
  const sp=S.space, f=folderEdit??S.folder;
  return `<h1>${many?"Which one is your card?":"Is this your card?"}</h1>
  ${many?`<p>More than one new drive appeared.</p>${list}`:`<p><b>${esc(S.drive?S.drive.label:"")}</b></p>`}
  <p style="margin-top:1rem">A copy of the card and your recovered photos will be saved in a new folder${S.beside?" next to this program":""}. You do not need to do anything.</p>
  <details${sp&&!sp.ok?" open":""}><summary class="soft">Save somewhere else</summary>
  <div class="row"><input type="text" id="folder" value="${esc(f)}" aria-label="Folder to save into" oninput="folderEdit=this.value"><button onclick="pick('folder')">Choose…</button></div></details>
  ${sp?(sp.ok?`<p class="soft">The copy needs ${size(sp.need)}; ${size(sp.free)} is free.${sp.tight?" That is enough for the copy. If a great deal is recovered it could still run short, and the program will say so if it does.":""}</p>`
   :`<p class="note"><b>There is not enough room.</b> The copy needs ${size(sp.need)} and only ${size(sp.free)} is free. Open "Save somewhere else" and choose another drive, such as an external one.</p>`):""}
  ${S.windows?`<p class="soft">Windows will ask for permission to read the card. Choose Yes.</p>`:""}
  ${err()}<div class="actions"><button class="go" ${S.drive?"":"disabled"} onclick="api('start_clone',{folder:document.getElementById('folder').value})">Yes, copy this card</button>
  <button onclick="api('restart')">No, start again</button></div>`;},
 cloning(){const c=S.clone||{},p=c.total?c.done/c.total:0,left=c.done&&c.seconds?Math.round((c.total-c.done)/(c.done/c.seconds)/60):null;
  return `<h1>Copying the card</h1><p>Leave the card plugged in until this finishes.</p>
  <progress max="1" value="${p}"></progress>
  <p class="soft">${size(c.done||0)} of ${size(c.total||0)}${left!==null&&left>0?` · about ${left} min left`:""}${c.bad_bytes?` · ${size(c.bad_bytes)} could not be read and was skipped`:""}</p>`;},
 recovering(){return `<h1>Recovering your photos</h1>
  <p><b>You can unplug the card now.</b> Everything from here uses the copy.</p>
  <p class="soft">The card is worked through in several steps, one after another. The bar shows the step under way.</p>
  ${(S.log||[]).length>1?`<ul class="did">${S.log.slice(0,-1).map(m=>`<li>${esc(m)}</li>`).join("")}</ul>`:""}
  <p style="margin-top:1rem"><b>Now:</b> ${esc((S.log||[]).slice(-1)[0]||"Starting")}</p>
  ${S.progress&&S.progress.total?`<progress max="${S.progress.total}" value="${S.progress.done}"></progress>`:`<progress></progress>`}
  <p class="soft">${S.progress&&S.progress.total?`${S.progress.done} of ${S.progress.total} in this step`:""}</p>`;},
 done(){const c=(S.summary||{}).counts||{};
  const uk=(S.summary||{}).unverified_kinds;
  const got=(c["Verified"]||0)+(c["Complete, no preview"]||0)+(c["Recovered, unverified"]||0);
  const rows=[["Verified","Recovered and double-checked","Each one matches the small preview picture the camera stored inside it, so the tool knows the picture is right."],
   ["Complete, no preview","Recovered whole","The whole file is there from start to end and opens. It has no built-in preview to compare against, so the tool could not double-check the picture itself. These are almost always fine."],
   ["Recovered, unverified","Recovered, but the tool cannot check them",`${uk?`${[[uk.other,"other files (documents, music, app data and the like)"],[uk.videos,"videos"],[uk.photos,"photos of a kind with nothing to check against"]].filter(x=>x[0]).map(x=>x[0].toLocaleString()+" "+x[1]).join(", ")}. `:""}They are all there from start to end, but nothing inside this kind of file lets the tool confirm it is undamaged. Open the ones you care about to make sure.`],
   ["Partial","Partly recovered","The top part of the picture, or part of a video, saved in its own folder. The rest could not be found, most likely because newer files were saved over it. What has been overwritten is gone for good: no tool or lab can bring it back."],
   ["Needs review","Waiting for you to decide","Close calls where the tool would rather ask than guess."],
   ["Not recoverable","Could not be recovered","Newer files have been saved over them. What has been overwritten is gone for good: no other tool or paid lab can bring it back, so there is no need to keep trying."]]
   .filter(r=>c[r[0]]).map(r=>`<tr><td class="n">${c[r[0]].toLocaleString()}</td><td><b>${r[1]}</b><div class="soft why">${r[2]}</div></td></tr>`).join("");
  return `<h1>Finished</h1>${((S.summary||{}).findings||[]).map(f=>`<div class="note">${esc(f.text)}</div>`).join("")}
  ${rows?`<p class="lead">${got.toLocaleString()} file${got===1?" was":"s were"} recovered.</p>`:""}<table>${rows||"<tr><td>No deleted files were found on this card.</td></tr>"}</table>
  <p class="soft" style="margin-top:1rem">Saved in ${esc(S.folder)}. The copy of your card (card.img) is there too; keep it until you are happy with the results.</p>
  ${S.to_review?`<div class="note"><b>${S.to_review} photo${S.to_review>1?"s":""} need your eye.</b> The program found more than one piece of the card that could come next and would rather ask than guess. <button style="margin-left:.5rem" onclick="review()">Decide now</button></div>`:""}
  <div class="actions"><button class="go" onclick="api('open',{what:'folder'})">Open the folder</button><button onclick="api('open',{what:'report'})">See the full report</button><button onclick="api('restart')">Recover another card</button></div>`;},
 image(){return `<h1>Choose the card image</h1><p>A raw copy of a card, usually a .img or .dd file.</p>
  <div class="row"><input type="text" id="img" aria-label="Path of the image file" placeholder="for example C:\\Users\\you\\Desktop\\card.img" value="${esc(imgPath)}" oninput="imgPath=this.value"><button onclick="pick('file')">Choose…</button></div>
  ${S&&S.message?err():""}<div class="actions"><button class="go" onclick="api('use_image',{path:imgPath})">Recover from this file</button><button onclick="go('start')">Back</button></div>`;},
 review(){const r=R;
  return `<h1>Which one looks right?</h1>
  <p><b>${esc(r.name)}</b> is ${Math.round(r.percent)}% rebuilt. Each picture below shows the photo around the dashed line, continued with a different piece of the card. Pick the one where the picture carries on properly below the line.</p>
  ${r.preview?`<p class="soft">This is how the whole photo should look:</p><img class="prev" src="${r.preview}" alt="Small preview of the whole photo">`:""}
  <div class="tiles">${r.candidates.map((c,i)=>`<button class="tile" onclick="pickPiece(${r.index},${c.cluster})" aria-label="Choice ${i+1}"><img src="${c.image}" alt="Choice ${i+1}"><i style="top:${(c.join*100).toFixed(1)}%"></i></button>`).join("")}</div>
  <div class="actions"><button onclick="pickPiece(${r.index},null)">None of these look right</button><button onclick="go(null)">Stop for now</button></div>
  <p class="soft">${r.left} left to decide.</p>`;},
 error(){return `<h1>That didn't work</h1><p>${esc(S.message)}</p><p class="soft">Your card has not been changed.</p>
  <div class="actions">${S.can_resume?`<button class="go" onclick="api('resume_clone')">Carry on with the copy</button><button onclick="api('restart')">Start again from the beginning</button>`:`<button class="go" onclick="api('restart')">Start again</button>`}</div>`;}
};
let local=null, R=null, imgPath="";
async function review(){const j=await api("review_next"); R=j.review; local=R?"review":null; render();}
async function pickPiece(index,cluster){V.style.opacity=.5; const j=await api("review_pick",{index,cluster}); V.style.opacity=1; R=j.review; local=R?"review":null; render();}
function go(v){local=v; if(S)S.message=""; render();}
async function pick(kind){const j=await api("pick",{kind}); if(!j.path)return;
  if(kind==="folder"){folderEdit=j.path; await api("set_folder",{folder:j.path});} else {imgPath=j.path; if(S)S.message=""; render();}}
function render(){
  if(!S)return; document.getElementById("ver").textContent="version "+S.version;
  let v=S.step; if((v==="start"||(v==="done"&&local==="review"))&&local)v=local; else if(v!=="start")local=null;
  const keep=document.activeElement&&document.activeElement.id, val=keep&&document.activeElement.value;
  const html=views[v]();
  if(V.dataset.v!==v||!["confirm","image"].includes(v)||V.dataset.h!==html){V.innerHTML=html;V.dataset.v=v;V.dataset.h=html;
    if(keep){const el=document.getElementById(keep); if(el){el.value=val;el.focus();}}}
  clearTimeout(timer); if(["cloning","recovering"].includes(v))timer=setTimeout(()=>api("state"),1000);
}
api("state");
</script></body></html>
"""
