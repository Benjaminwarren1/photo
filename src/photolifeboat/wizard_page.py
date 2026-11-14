"""The wizard's single page. Kept in a Python string so the program ships as one piece."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo Lifeboat</title>
<style>
/* Laid out like a Windows setup wizard (title, content, Back / Next in the bottom right) and styled after
   Windows 11, so it looks like the rest of the computer. Follows the system's light or dark mode. */
:root{--bg:#f3f3f3;--layer:#fbfbfb;--card:#ffffff;--ink:#1b1b1b;--soft:#5f5f5f;--line:#e5e5e5;--stroke:rgba(0,0,0,.0578);--stroke2:rgba(0,0,0,.1622);
--accent:#005fb8;--accent2:#196ebf;--accent-ink:#fff;--warn-bg:#fff4ce;--warn:#5c4300;--warn-icon:#9d5d00;--bad:#c42b1c;--ok:#0f7b0f;--track:#e0e0e0;--hover:#f6f6f6}
@media(prefers-color-scheme:dark){:root{--bg:#202020;--layer:#272727;--card:#2d2d2d;--ink:#ffffff;--soft:#c8c8c8;--line:#3b3b3b;--stroke:rgba(255,255,255,.0698);--stroke2:rgba(255,255,255,.093);
--accent:#60cdff;--accent2:#5abce9;--accent-ink:#000;--warn-bg:#433519;--warn:#f5e7c2;--warn-icon:#fce100;--bad:#ff99a4;--ok:#6ccb5f;--track:#4a4a4a;--hover:#323232}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;display:flex;flex-direction:column}
header.top{display:flex;align-items:center;gap:.6rem;padding:.7rem 1.5rem;background:var(--layer);border-bottom:1px solid var(--line);font-size:.93rem}
header.top svg{flex:none}header.top b{font-weight:600}header.top span{margin-left:auto;color:var(--soft);font-size:.8rem}
main{flex:1;overflow:auto;padding:1.6rem 2rem 1.5rem}
section{max-width:44rem}
h1{font:600 1.45rem/1.3 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;margin:0 0 .7rem}
p{margin:.5rem 0}.soft{color:var(--soft)}
footer.bar{display:flex;flex-direction:row-reverse;justify-content:flex-start;gap:.5rem;padding:.9rem 1.5rem;background:var(--layer);border-top:1px solid var(--line)}
footer.bar:empty{display:none}
button{font:inherit;font-size:.95rem;min-height:2rem;min-width:6rem;padding:.3rem 1rem;border-radius:4px;border:1px solid var(--stroke);border-bottom-color:var(--stroke2);background:var(--card);color:var(--ink);cursor:default}
button:hover{background:var(--hover)}
a{color:var(--accent)}
button.go{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}button.go:hover{background:var(--accent2)}
button.link{border:0;background:none;color:var(--accent);min-width:0;padding:0 .2rem;text-decoration:underline}
button:focus-visible,input:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
button[disabled]{opacity:.45}
.note{position:relative;background:var(--warn-bg);color:var(--warn);border:1px solid var(--stroke);border-radius:6px;padding:.65rem .9rem .65rem 2.5rem;margin:1rem 0}
.note::before{content:"!";position:absolute;left:.9rem;top:.85rem;width:1.1rem;height:1.1rem;border-radius:50%;background:var(--warn-icon);color:var(--warn-bg);font:700 .75rem/1.1rem "Segoe UI",sans-serif;text-align:center}
.card{background:var(--card);border:1px solid var(--stroke);border-radius:6px;padding:.9rem 1.1rem;margin:1rem 0}
.card h2{font-size:1rem;font-weight:600;margin:0 0 .3rem}
.inline{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.7rem}
.err{color:var(--bad);margin-top:.8rem}
ul.did{list-style:none;padding:0;margin:.9rem 0 0;color:var(--soft);font-size:.92rem}ul.did li{margin:.15rem 0}
ul.did li::before{content:"\2713";color:var(--ok);margin-right:.5rem}
ul.plain{padding-left:1.2rem;margin:.6rem 0}ul.plain li{margin:.25rem 0}
input[type=text]{font:inherit;width:100%;min-height:2rem;padding:.3rem .6rem;border-radius:4px;border:1px solid var(--stroke);border-bottom:1px solid var(--soft);background:var(--card);color:var(--ink)}
.row{display:flex;gap:.5rem;align-items:center;margin-top:.4rem}.row input{flex:1}
progress{-webkit-appearance:none;appearance:none;width:100%;height:4px;margin:1rem 0 .4rem;border:0;border-radius:2px;background:var(--track);accent-color:var(--accent)}
progress::-webkit-progress-bar{background:var(--track);border-radius:2px}progress::-webkit-progress-value{background:var(--accent);border-radius:2px}
progress:not([value]){background:linear-gradient(90deg,var(--track) 0 35%,var(--accent) 35% 65%,var(--track) 65%) 0 0/300% 100%;animation:ind 1.6s linear infinite}
progress:not([value])::-webkit-progress-bar{background:transparent}
@keyframes ind{from{background-position:100% 0}to{background-position:-50% 0}}
table{border-collapse:collapse;width:100%;margin-top:.8rem}td{padding:.5rem .3rem;border-bottom:1px solid var(--line);vertical-align:top}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr));gap:.8rem;margin-top:1rem}
.tile{padding:0;min-width:0;border:2px solid var(--line);border-radius:6px;overflow:hidden;background:none;position:relative;line-height:0}
.tile img{width:100%;display:block}.tile:hover{border-color:var(--accent);background:none}
.tile i{position:absolute;left:0;right:0;border-top:2px dashed #fff;mix-blend-mode:difference;pointer-events:none}
img.prev{max-width:10rem;border-radius:4px;border:1px solid var(--line);display:block;margin:.5rem 0}
td.n{font-variant-numeric:tabular-nums;font-weight:600;width:4.5rem;font-size:1.15rem}
.why{font-size:.9rem;line-height:1.4;margin-top:.1rem}.lead{font-size:1.1rem;margin:.2rem 0 .4rem}
details{margin:.6rem 0}summary{cursor:default}
label.pick{display:flex;gap:.6rem;align-items:center;padding:.5rem .2rem}
label.opt{display:flex;gap:.8rem;align-items:center;background:var(--card);border:1px solid var(--stroke);border-radius:6px;padding:.75rem 1rem;margin:.45rem 0}
label.opt:hover{background:var(--hover)}label.opt input{accent-color:var(--accent);width:1.1rem;height:1.1rem;margin:0}
label.opt b{display:block;font-weight:600}label.opt small{color:var(--soft);font-size:.86rem}
input[type=radio]{accent-color:var(--accent)}
</style></head><body>
<header class="top"><svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="#f26522" stroke-width="6"/><circle cx="12" cy="12" r="8" fill="none" stroke="#fff" stroke-width="6" stroke-dasharray="6.28 6.28" stroke-dashoffset="3.14" transform="rotate(45 12 12)"/><circle cx="12" cy="12" r="11.2" fill="none" stroke="#7a2e08" stroke-width="1.2"/><circle cx="12" cy="12" r="4.8" fill="none" stroke="#7a2e08" stroke-width="1.2"/></svg><b>Photo Lifeboat</b><span id="ver"></span></header>
<main><section id="view" aria-live="polite"></section></main>
<footer class="bar" id="bar"></footer>
<script>
const T="__TOKEN__", V=document.getElementById("view");
let S=null, timer=null;
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
  <div class="note"><b>Before you start:</b> stop using the card. Don't let anything format it, don't take photos on it, and don't save anything to it.</div>
  <p class="soft">Already formatted the card? It's still worth trying. Photos can often be rescued from a formatted card.</p>
  <div class="actions"><button class="go" onclick="go('where')">Recover from a card</button>
  <button onclick="go('image')">I already have a card image file</button></div>`;},
 where(){const back=S.step==="confirm";
  const opt=(k,t,d)=>`<label class="opt"><input type="radio" name="place" ${S.place===k?"checked":""} onclick="${k==="other"?"pickPlace()":`api('set_place',{place:'${k}'})`}"><span><b>${t}</b><small>${d}</small></span></label>`;
  const name=String(S.folder).split(/[\\/]/).pop();
  return `<h1>Where should your rescued photos go?</h1>
  <p>A new folder called <b>${esc(name)}</b> will be made there. The copy of your card is saved in it too.</p>
  ${opt("pictures","Pictures","Your Pictures folder, where photos usually go")}
  ${opt("desktop","Desktop","Easy to find straight away")}
  ${opt("other","Somewhere else…",S.place==="other"?esc(String(S.folder).replace(/[\\/][^\\/]*$/,"")):"Choose a folder, for example on an external drive")}
  ${err()}<div class="actions"><button class="go" onclick="${back?"go(null)":"go('unplug')"}">Next</button><button onclick="${back?"go(null)":"go('start')"}">Back</button></div>`;},
 unplug(){return `<h1>First, take the card out</h1>
  <p>Unplug the memory card (or its reader) you want to recover, so the program can tell which drive it is when you plug it back in.</p>
  <p class="soft">If the card has a small lock switch on its side, slide it to <b>Lock</b>. That stops the computer writing anything to it.</p>
  <div class="actions"><button class="go" onclick="api('begin_card')">The card is unplugged</button><button onclick="go('where')">Back</button></div>`;},
 plug(){return `<h1>Now plug the card in</h1>
  <p>Plug it in and wait a few seconds.</p>
  <div class="note">If the computer offers to <b>format</b> the card or to <b>scan and fix</b> it, choose <b>Cancel</b>.</div>
  ${err()}<div class="actions"><button class="go" onclick="api('find_card')">It's plugged in</button><button onclick="api('restart')">Start again</button></div>`;},
 confirm(){
  const many=S.found.length>1;
  const list=many?S.found.map(d=>`<label class="pick"><input type="radio" name="d" ${S.drive&&S.drive.id===d.id?"checked":""} onchange="api('choose',{id:'${esc(d.id)}'})"> ${esc(d.label)}</label>`).join(""):"";
  const sp=S.space, where={pictures:"in your Pictures folder",desktop:"on your Desktop"}[S.place];
  return `<h1>${many?"Which one is your card?":"Is this your card?"}</h1>
  ${many?`<p>More than one new drive appeared.</p>${list}`:`<p><b>${esc(S.drive?S.drive.label:"")}</b></p>`}
  <p style="margin-top:1rem">A copy of the card and your rescued photos will be saved in a new folder ${where||"in <b>"+esc(String(S.folder).replace(/[\\/][^\\/]*$/,""))+"</b>"}. <button class="link" onclick="go('where')">Change</button></p>
  ${sp?(sp.ok?`<p class="soft">The copy needs ${size(sp.need)}; ${size(sp.free)} is free.${sp.tight?" That is enough for the copy. If a great deal is recovered it could still run short, and the program will say so if it does.":""}</p>`
   :`<p class="note"><b>There is not enough room.</b> The copy needs ${size(sp.need)} and only ${size(sp.free)} is free. Press <b>Change</b> and choose another drive, such as an external one.</p>`):""}
  ${S.windows?`<p class="soft">Windows will ask for permission to read the card. Choose Yes.</p>`:""}
  ${err()}<div class="actions"><button class="go" ${S.drive?"":"disabled"} onclick="api('start_clone')">Yes, copy this card</button>
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
 done(){const c=Object.assign({},(S.summary||{}).counts||{});
  // "Needs review" covers two kinds: photos with pieces to choose between (the ones "Decide now" shows), and
  // files that failed the final check, where there is nothing to choose. They are counted apart so the
  // numbers on this screen agree with each other.
  const ask=Math.min(S.to_review||0,c["Needs review"]||0); c["_look"]=(c["Needs review"]||0)-ask; c["Needs review"]=ask;
  const uk=(S.summary||{}).unverified_kinds;
  const got=(c["Verified"]||0)+(c["Complete, no preview"]||0)+(c["Recovered, unverified"]||0);
  const rows=[["Verified","Recovered and double-checked","Each one matches the small preview picture the camera stored inside it, so the tool knows the picture is right."],
   ["Complete, no preview","Recovered whole","The whole file is there from start to end and opens. It has no built-in preview to compare against, so the tool could not double-check the picture itself. These are almost always fine."],
   ["Recovered, unverified","Recovered, but the tool cannot check them",`${uk?`${[[uk.other,"other files (documents, music, app data and the like)"],[uk.videos,"videos"],[uk.photos,"photos of a kind with nothing to check against"]].filter(x=>x[0]).map(x=>x[0].toLocaleString()+" "+x[1]).join(", ")}. `:""}They are all there from start to end, but nothing inside this kind of file lets the tool confirm it is undamaged. Open the ones you care about to make sure.`],
   ["Partial","Partly recovered","The top part of the picture, or part of a video, saved in its own folder. The rest could not be found, most likely because newer files were saved over it. What has been overwritten is gone for good: no tool or lab can bring it back."],
   ["Needs review","Waiting for you to decide","Close calls where the tool would rather ask than guess."],
   ["_look","Need a closer look","The tool could not confirm these are right, and there is no choice to make. They are saved in the Partial folder: open them to check. The report says what was wrong with each."],
   ["Not recoverable","Could not be recovered","Newer files have been saved over them. What has been overwritten is gone for good: no other tool or paid lab can bring it back, so there is no need to keep trying."]]
   .filter(r=>c[r[0]]).map(r=>`<tr><td class="n">${c[r[0]].toLocaleString()}</td><td><b>${r[1]}</b><div class="soft why">${r[2]}</div></td></tr>`).join("");
  return `<h1>Finished</h1>${((S.summary||{}).findings||[]).map(f=>`<div class="note">${esc(f.text)}</div>`).join("")}
  ${rows?`<p class="lead">${got.toLocaleString()} file${got===1?" was":"s were"} recovered.</p>`:""}<table>${rows||"<tr><td>No deleted files were found on this card.</td></tr>"}</table>
  <p class="soft" style="margin-top:1rem">${{pictures:"Saved in your Pictures folder, in ",desktop:"Saved on your Desktop, in "}[S.place]?`${{pictures:"Saved in your Pictures folder, in ",desktop:"Saved on your Desktop, in "}[S.place]}the folder <b>${esc(String(S.folder).split(/[\\/]/).pop())}</b>.`:`Saved in ${esc(S.folder)}.`}</p>
  ${S.to_review?`<div class="note"><b>${S.to_review} photo${S.to_review>1?"s":""} need your eye.</b> The program found more than one piece of the card that could come next and would rather ask than guess. <button style="margin-left:.5rem" onclick="review()">Decide now</button></div>`:""}
  ${copyCard()}
  <div class="actions"><button class="go" onclick="api('open',{what:'folder'})">Open the folder</button><button onclick="api('open',{what:'report'})">See the full report</button><button onclick="api('restart')">Recover another card</button></div>
  <p class="soft" style="margin-top:1.5rem">Photo Lifeboat is free and always will be. If it helped, you can <a href="#" onclick="api('open',{what:'donate'});return false">support it on Ko-fi</a>.</p>`;},
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
let local=null, R=null, imgPath="", askDelete=false;
function copyCard(){const c=S.copy||{};
  if(!c.offered)return "";
  const keep=`<p class="soft">Keep the memory card itself as it is for now, and don't save anything new to it. If something you wanted is missing, another recovery program may still find it, but only while the card stays untouched. That's why this program copies the card first and only ever works from the copy.</p>`;
  if(c.deleted)return `<div class="card"><h2>The copy of your card has been deleted</h2>${keep}</div>`;
  if(!c.size)return "";
  return `<div class="card"><h2>The copy of your card</h2>
  <p>Your photos were rescued from a copy of the card, saved in the same folder as <b>card.img</b> (${size(c.size)}). Once you're happy with your photos, you can delete it to free up the space.</p>${keep}
  ${S.to_review?`<p class="soft">Decide the photos above first: that needs the copy.</p>`:
   askDelete?`<p><b>Delete the copy for good?</b> Your rescued photos stay where they are.</p><div class="inline"><button class="go" onclick="askDelete=false;api('delete_copy')">Yes, delete the copy</button><button onclick="askDelete=false;render()">No, keep it</button></div>`
   :`<div class="inline"><button onclick="askDelete=true;render()">Delete the copy…</button></div>`}</div>`;}
async function pickPlace(){const j=await api("pick",{kind:"folder"}); if(j.path)await api("set_place",{place:"other",folder:j.path}); else render();}
async function review(){const j=await api("review_next"); R=j.review; local=R?"review":null; render();}
async function pickPiece(index,cluster){V.style.opacity=.5; const j=await api("review_pick",{index,cluster}); V.style.opacity=1; R=j.review; local=R?"review":null; render();}
function go(v){local=v; if(S)S.message=""; render();}
async function pick(kind){const j=await api("pick",{kind}); if(!j.path)return;
  if(kind==="folder"){await api("set_folder",{folder:j.path});} else {imgPath=j.path; if(S)S.message=""; render();}}
function render(){
  if(!S)return; document.getElementById("ver").textContent="version "+S.version;
  let v=S.step; if(local&&(v==="start"||(v==="confirm"&&local==="where")||(v==="done"&&local==="review")))v=local; else if(v!=="start")local=null;
  const keep=document.activeElement&&document.activeElement.id, val=keep&&document.activeElement.value;
  const html=views[v]();
  if(V.dataset.v!==v||!["confirm","image"].includes(v)||V.dataset.h!==html){V.innerHTML=html;V.dataset.v=v;V.dataset.h=html;
    const acts=V.querySelector(".actions"), bar=document.getElementById("bar"); bar.replaceChildren(...(acts?acts.childNodes:[])); if(acts)acts.remove();
    if(keep){const el=document.getElementById(keep); if(el){el.value=val;el.focus();}}}
  clearTimeout(timer); if(["cloning","recovering"].includes(v))timer=setTimeout(()=>api("state"),1000);
}
api("state");
</script></body></html>
"""
