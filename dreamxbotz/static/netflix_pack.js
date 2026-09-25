/* ==========================================================================
   Netflix Pack · Continue Watching + My List + Trailer Hover + Player V2
   Requires: req.html's FILE payload and existing player elements
   ========================================================================== */
(function () {
    "use strict";

    var FILE = {};
    try {
        var payload = document.getElementById("dx-file");
        FILE = JSON.parse(payload ? payload.textContent : "{}");
    } catch (e) { FILE = {}; }

    var CW_KEY = "dx:cw:list";
    var MYLIST_KEY = "dx:mylist";
    var STORAGE_PREFIX = "dx:resume:";

    var store = {
        get: function (k) { try { return localStorage.getItem(k); } catch(e){ return null; } },
        set: function (k,v) { try { localStorage.setItem(k,v); } catch(e){} },
        del: function (k) { try { localStorage.removeItem(k); } catch(e){} },
        jsonGet: function (k, fb) { try { var r = localStorage.getItem(k); return r ? JSON.parse(r) : fb; } catch(e){ return fb; } },
        jsonSet: function (k,v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} }
    };

    function safeText(s, n){ s = String(s||"").replace(/[\u0000-\u001f\u007f]/g,"").trim(); if(n&&s.length>n) s=s.slice(0,n-1)+"…"; return s; }

    function toast(msg){
        var t = document.getElementById("toast");
        if(!t) return;
        t.textContent = msg; t.classList.add("on");
        clearTimeout(t._tm); t._tm = setTimeout(function(){ t.classList.remove("on"); }, 2400);
    }

    /* ---------- Trailer IDs for known titles ---------- */
    var TRAILERS = {
        "Jawan": "MwoUr5wPz9o",
        "Pushpa 2 The Rule": "g3JUbgOHgdw",
        "Pathaan": "vqu4z34wENw",
        "Jailer": "x3DlegJyjOs",
        "Leo": "Po3jStAgaK0",
        "RRR": "NgBoMJy386M",
        "3 Idiots": "K0eDl6F_m3k",
        "Sholay": "Xjil_d0yT0o",
        "Interstellar": "zSWdZVtXT7E",
        "Inception": "YoHD9XEInc0",
        "Oppenheimer": "uYPbbksJxIg",
        "Dune Part Two": "Way9Dexny3w",
        "Kalki 2898 AD": "yfuYLcToCls",
        "Stree 2": "KVnhe3B6F7g"
    };

    function trailerIdFor(title){
        if(!title) return null;
        if(TRAILERS[title]) return TRAILERS[title];
        var low = title.toLowerCase();
        for(var k in TRAILERS){ if(low.indexOf(k.toLowerCase())!==-1 || k.toLowerCase().indexOf(low)!==-1) return TRAILERS[k]; }
        return null;
    }

    /* ---------- Continue Watching ---------- */
    function cwGet(){ return store.jsonGet(CW_KEY, []); }
    function cwSet(list){ store.jsonSet(CW_KEY, list.slice(0,12)); }
    function cwSaveCurrent(current, duration){
        if(!FILE.uid || !isFinite(duration) || duration<30) return;
        if(current < 8 || current > duration - 5) return;
        var list = cwGet();
        var idx = -1;
        for(var i=0;i<list.length;i++) if(list[i].uid===FILE.uid) idx=i;
        var item = {
            uid: FILE.uid,
            name: FILE.name || "Unknown",
            size: FILE.size || "",
            url: FILE.url || window.location.href,
            title: (FILE.name||"").split(".")[0].slice(0,60) || "Current Video",
            progress: current,
            duration: duration,
            pct: Math.round((current/duration)*100),
            updated: Date.now()
        };
        if(idx!==-1) list.splice(idx,1);
        list.unshift(item);
        // also save legacy resume key
        store.set(STORAGE_PREFIX + FILE.uid, String(current));
        cwSet(list);
        renderCW();
    }
    function cwRemove(uid){
        var list = cwGet().filter(function(x){ return x.uid!==uid; });
        cwSet(list);
        store.del(STORAGE_PREFIX + uid);
        renderCW();
        toast("Removed from Continue Watching");
    }

    function renderCW(){
        var root = document.getElementById("nfxContinue");
        var grid = document.getElementById("nfxContinueGrid");
        var count = document.getElementById("nfxContinueCount");
        if(!root||!grid) return;
        var list = cwGet();
        // filter stale (>30 days)
        var now = Date.now();
        list = list.filter(function(x){ return (now - (x.updated||0)) < 30*864e5; });
        // sort by updated desc
        list.sort(function(a,b){ return b.updated - a.updated; });
        if(list.length===0){
            grid.classList.add("is-empty");
            grid.innerHTML = '<div class="nfx-empty"><div class="nfx-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg></div><p>Abhi kuch nahi dekha</p><small>Video play karte hi yahan progress dikhega · 8 sec ke baad auto-save</small></div>';
            if(count) count.textContent = "0 videos";
            root.hidden = false;
            return;
        }
        grid.classList.remove("is-empty");
        grid.innerHTML = "";
        if(count) count.textContent = list.length + " videos";
        list.forEach(function(item){
            var card = document.createElement("button");
            card.className = "nfx-cw-card";
            card.type = "button";
            card.title = item.name;
            var art = document.createElement("div");
            art.className = "nfx-cw-art";
            // gradient fallback with initial letter
            art.style.background = "linear-gradient(135deg, #1b2036, #07080c)";
            art.textContent = "";
            var play = document.createElement("div");
            play.className = "nfx-cw-play";
            play.innerHTML = '<span><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.2v13.6a1 1 0 0 0 1.5.9l11-6.8a1 1 0 0 0 0-1.8l-11-6.8A1 1 0 0 0 8 5.2Z"/></svg></span>';
            var prog = document.createElement("div");
            prog.className = "nfx-cw-progress";
            var fill = document.createElement("i");
            fill.style.width = (item.pct||0) + "%";
            prog.appendChild(fill);
            art.appendChild(play);
            art.appendChild(prog);
            var body = document.createElement("div");
            body.className = "nfx-cw-body";
            var title = document.createElement("div");
            title.className = "nfx-cw-title";
            title.textContent = item.title || item.name.slice(0,50);
            var meta = document.createElement("div");
            meta.className = "nfx-cw-meta";
            meta.textContent = item.pct + "% watched · " + fmt(item.progress) + " / " + fmt(item.duration);
            body.appendChild(title); body.appendChild(meta);
            var rm = document.createElement("button");
            rm.className = "nfx-cw-remove";
            rm.type = "button";
            rm.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>';
            rm.title = "Remove";
            rm.addEventListener("click", function(ev){ ev.stopPropagation(); cwRemove(item.uid); });
            card.appendChild(art); card.appendChild(body); card.appendChild(rm);
            card.addEventListener("click", function(){
                // if same video, seek else open url with time
                if(item.uid===FILE.uid){
                    var v = document.getElementById("video");
                    if(v && isFinite(item.progress)) { v.currentTime = item.progress; v.play(); window.scrollTo({top:0, behavior:"smooth"}); }
                } else {
                    // open the stored url (for demo, just toast)
                    window.location.href = item.url;
                }
            });
            grid.appendChild(card);
        });
        root.hidden = false;
    }

    function fmt(s){ if(!isFinite(s)||s<0) s=0; var h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=Math.floor(s%60); var mm=h&&m<10?"0"+m:String(m); var ss=sec<10?"0"+sec:String(sec); return h>0? h+":"+mm+":"+ss : mm+":"+ss; }

    /* ---------- My List ---------- */
    function mlGet(){ return store.jsonGet(MYLIST_KEY, []); }
    function mlSet(list){ store.jsonSet(MYLIST_KEY, list); }
    function mlHas(label){ var l=mlGet(); for(var i=0;i<l.length;i++) if(l[i].label===label) return true; return false; }
    function mlToggle(item){
        var list = mlGet();
        var idx=-1; for(var i=0;i<list.length;i++) if(list[i].label===item.label) idx=i;
        if(idx!==-1){ list.splice(idx,1); mlSet(list); toast("Removed from My List ♥"); }
        else { list.unshift(item); if(list.length>24) list=list.slice(0,24); mlSet(list); toast("Added to My List ♥"); }
        renderML();
        syncHearts();
    }
    function mlRemove(label){
        var list = mlGet().filter(function(x){ return x.label!==label; });
        mlSet(list); renderML(); syncHearts(); toast("Removed from My List");
    }
    function renderML(){
        var root=document.getElementById("nfxMyList");
        var grid=document.getElementById("nfxMyListGrid");
        var count=document.getElementById("nfxMyListCount");
        if(!root||!grid) return;
        var list=mlGet();
        if(list.length===0){
            grid.classList.add("is-empty");
            grid.innerHTML='<div class="nfx-empty"><div class="nfx-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.84 4.6a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.07a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.79 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg></div><p>My List khali hai</p><small>♥ dabao kisi bhi poster pe — yahan save ho jayega</small></div>';
            if(count) count.textContent="0 titles";
            root.hidden=false;
            return;
        }
        grid.classList.remove("is-empty");
        grid.innerHTML="";
        if(count) count.textContent=list.length+" titles";
        list.forEach(function(item){
            var card=document.createElement("a");
            card.className="nfx-ml-card";
            card.href=item.link||"#";
            card.title="Search "+item.label;
            if(item.link && item.link.indexOf("https://t.me/")===0){ card.target="_blank"; card.rel="noopener"; }
            var art=document.createElement("div");
            art.className="nfx-ml-art";
            art.style.cssText = item.hue ? "--h1:"+item.hue+";--h2:"+((item.hue+40)%360) : "";
            if(item.poster){
                var img=document.createElement("img");
                img.src=item.poster; img.alt=item.label; img.loading="lazy";
                img.onload=function(){ img.classList.add("on"); };
                art.appendChild(img);
            }
            var body=document.createElement("div");
            body.className="nfx-ml-info";
            var t=document.createElement("div"); t.className="nfx-ml-title"; t.textContent=item.label;
            var s=document.createElement("div"); s.className="nfx-ml-sub"; s.textContent=item.sub||item.tag||"";
            body.appendChild(t); body.appendChild(s);
            var rm=document.createElement("button");
            rm.className="nfx-ml-heart";
            rm.type="button"; rm.innerHTML='<svg viewBox="0 0 24 24"><path d="M20.84 4.6a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.07a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.79 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
            rm.title="Remove";
            rm.addEventListener("click", function(ev){ ev.preventDefault(); ev.stopPropagation(); mlRemove(item.label); });
            card.appendChild(art); card.appendChild(body); card.appendChild(rm);
            grid.appendChild(card);
        });
        root.hidden=false;
    }

    function syncHearts(){
        var list=mlGet();
        var set={}; for(var i=0;i<list.length;i++) set[list[i].label]=true;
        document.querySelectorAll(".nfx-heart").forEach(function(btn){
            var label=btn.getAttribute("data-label");
            if(set[label]) btn.classList.add("is-active");
            else btn.classList.remove("is-active");
        });
    }

    function attachHeart(card, item){
        if(card.querySelector(".nfx-heart")) return;
        var btn=document.createElement("button");
        btn.className="nfx-heart" + (mlHas(item.label)?" is-active":"");
        btn.type="button";
        btn.setAttribute("data-label", item.label);
        btn.title=mlHas(item.label)?"Remove from My List":"Add to My List";
        btn.innerHTML='<svg viewBox="0 0 24 24"><path d="M20.84 4.6a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.07a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.79 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
        btn.addEventListener("click", function(ev){
            ev.preventDefault(); ev.stopPropagation();
            mlToggle(item);
            btn.classList.toggle("is-active");
        });
        card.appendChild(btn);
        card.style.position="relative";
    }

    /* ---------- Trailer Hover ---------- */
    var trailerTimer=null, activeTrailer=null;
    function createTrailer(card, title){
        var tid=trailerIdFor(title);
        if(!tid) return null;
        var wrap=document.createElement("div");
        wrap.className="nfx-trailer";
        var iframe=document.createElement("iframe");
        iframe.allow="autoplay; encrypted-media";
        iframe.loading="lazy";
        // muted autoplay, loop, no controls
        iframe.src="https://www.youtube-nocookie.com/embed/"+tid+"?autoplay=1&mute=1&controls=0&rel=0&showinfo=0&modestbranding=1&loop=1&playlist="+tid;
        var label=document.createElement("span");
        label.className="nfx-trailer-label";
        label.textContent="▶ Trailer · "+title;
        var close=document.createElement("button");
        close.className="nfx-trailer-close";
        close.type="button";
        close.innerHTML="✕";
        close.addEventListener("click", function(ev){ ev.stopPropagation(); hideTrailer(card); });
        wrap.appendChild(iframe); wrap.appendChild(label); wrap.appendChild(close);
        card.appendChild(wrap);
        return wrap;
    }
    function showTrailer(card, title){
        if(window.matchMedia("(hover: none)").matches) return;
        if(card.querySelector(".nfx-trailer.on")) return;
        var t=card.querySelector(".nfx-trailer");
        if(!t) t=createTrailer(card, title);
        if(!t) return;
        activeTrailer=t;
        requestAnimationFrame(function(){ t.classList.add("on"); });
    }
    function hideTrailer(card){
        var t=card.querySelector(".nfx-trailer");
        if(t) t.classList.remove("on");
        if(activeTrailer===t) activeTrailer=null;
        // remove iframe src to stop audio after animation
        setTimeout(function(){
            if(t && !t.classList.contains("on")){
                var ifr=t.querySelector("iframe");
                if(ifr) ifr.src="";
                if(t.parentNode) t.parentNode.removeChild(t);
            }
        }, 400);
    }
    function enableTrailerOnCard(card, title){
        if(!title || !trailerIdFor(title)) return;
        var enter=function(){
            clearTimeout(trailerTimer);
            trailerTimer=setTimeout(function(){ showTrailer(card, title); }, 650);
        };
        var leave=function(){ clearTimeout(trailerTimer); hideTrailer(card); };
        card.addEventListener("mouseenter", enter);
        card.addEventListener("mouseleave", leave);
        card.addEventListener("focusin", enter);
        card.addEventListener("focusout", leave);
    }

    /* ---------- Enhanced Player ---------- */
    function enhancePlayer(){
        var player=document.getElementById("player");
        var video=document.getElementById("video");
        if(!player||!video) return;
        var ctrlRow=player.querySelector(".ctrl-row");
        var rightGroup = ctrlRow ? ctrlRow.querySelectorAll(".ctrl-group")[1] : null;
        if(!rightGroup) return;

        // Theatre button
        if(!document.getElementById("nfxTheatre")){
            var theatre=document.createElement("button");
            theatre.id="nfxTheatre";
            theatre.className="ctrl-btn nfx-theatre-btn";
            theatre.type="button";
            theatre.title="Theatre mode (T)";
            theatre.setAttribute("aria-label","Theatre mode");
            theatre.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="10" rx="2"/><path d="M7 7v10M17 7v10"/></svg>';
            theatre.addEventListener("click", function(){
                player.classList.toggle("is-theatre");
                theatre.classList.toggle("is-active");
                document.body.classList.toggle("nfx-theatre-open", player.classList.contains("is-theatre"));
                toast(player.classList.contains("is-theatre") ? "Theatre mode on" : "Theatre mode off");
            });
            rightGroup.insertBefore(theatre, rightGroup.firstChild);
        }
        // Screenshot button
        if(!document.getElementById("nfxShot")){
            var shot=document.createElement("button");
            shot.id="nfxShot";
            shot.className="ctrl-btn";
            shot.type="button";
            shot.title="Screenshot (S)";
            shot.setAttribute("aria-label","Screenshot");
            shot.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>';
            shot.addEventListener("click", function(){
                try{
                    var c=document.createElement("canvas");
                    c.width=video.videoWidth||1280; c.height=video.videoHeight||720;
                    var ctx=c.getContext("2d");
                    ctx.drawImage(video, 0, 0, c.width, c.height);
                    var a=document.createElement("a");
                    var name=(FILE.name||"MinatoVerse").replace(/[^a-z0-9]/gi,"_").slice(0,40);
                    a.download=name+"_"+Math.floor(video.currentTime)+"s.png";
                    a.href=c.toDataURL("image/png");
                    a.click();
                    var flash=document.getElementById("nfxFlashShot");
                    if(flash){ flash.classList.add("on"); setTimeout(function(){ flash.classList.remove("on"); }, 180); }
                    toast("Screenshot saved ✓");
                } catch(e){ toast("Screenshot failed: "+e.message); }
            });
            rightGroup.insertBefore(shot, rightGroup.firstChild);
        }
        // Help button
        if(!document.getElementById("nfxHelp")){
            var help=document.createElement("button");
            help.id="nfxHelp";
            help.className="ctrl-btn";
            help.type="button";
            help.title="Shortcuts (?)";
            help.setAttribute("aria-label","Keyboard shortcuts");
            help.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.82 0c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>';
            help.addEventListener("click", function(){ openShortcuts(); });
            rightGroup.insertBefore(help, rightGroup.firstChild);
        }

        // Skip intro button (floating)
        if(!document.getElementById("nfxSkip")){
            var skip=document.createElement("button");
            skip.id="nfxSkip";
            skip.className="nfx-skip-intro";
            skip.type="button";
            skip.textContent="Skip intro → 1:25";
            skip.addEventListener("click", function(){
                video.currentTime=Math.min(video.currentTime+85, video.duration-2);
                skip.classList.remove("on");
                toast("Intro skipped");
            });
            player.appendChild(skip);
            // show only first 90s and if not near end
            video.addEventListener("timeupdate", function(){
                if(video.currentTime>4 && video.currentTime<90 && video.duration>120){
                    skip.classList.add("on");
                } else skip.classList.remove("on");
            });
        }
        // Flash overlay for screenshot
        if(!document.getElementById("nfxFlashShot")){
            var f=document.createElement("div");
            f.id="nfxFlashShot"; f.className="nfx-flash-shot";
            player.appendChild(f);
        }

        // Shortcuts modal
        if(!document.getElementById("nfxModal")){
            var modal=document.createElement("div");
            modal.id="nfxModal"; modal.className="nfx-modal";
            modal.innerHTML='<div class="nfx-modal-box"><div class="nfx-modal-head"><h3>⌨️ Keyboard Shortcuts</h3><button class="nfx-modal-close" type="button">✕</button></div><div class="nfx-modal-body">'+
                '<div class="nfx-kbd-row"><span>Play / Pause</span><b class="nfx-kbd"><kbd>K</kbd> <kbd>Space</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Back 10s / Forward 10s</span><b class="nfx-kbd"><kbd>J</kbd> <kbd>L</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Back 5s / Forward 5s</span><b class="nfx-kbd"><kbd>←</kbd> <kbd>→</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Volume</span><b class="nfx-kbd"><kbd>↑</kbd> <kbd>↓</kbd> <kbd>M</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Theatre mode</span><b class="nfx-kbd"><kbd>T</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Fullscreen</span><b class="nfx-kbd"><kbd>F</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>PIP</span><b class="nfx-kbd"><kbd>P</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Screenshot</span><b class="nfx-kbd"><kbd>S</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Help</span><b class="nfx-kbd"><kbd>?</kbd> <kbd>H</kbd></b></div>'+
                '<div class="nfx-kbd-row"><span>Skip intro</span><b class="nfx-kbd"><kbd>I</kbd></b></div>'+
                '</div></div>';
            document.body.appendChild(modal);
            modal.querySelector(".nfx-modal-close").addEventListener("click", closeShortcuts);
            modal.addEventListener("click", function(e){ if(e.target===modal) closeShortcuts(); });
        }
        function openShortcuts(){ document.getElementById("nfxModal").classList.add("on"); }
        function closeShortcuts(){ document.getElementById("nfxModal").classList.remove("on"); }
        window._nfxOpenHelp=openShortcuts;

        // extra key handlers for new shortcuts
        document.addEventListener("keydown", function(e){
            var tag=(e.target.tagName||"").toLowerCase();
            if(tag==="input"||tag==="textarea"||e.metaKey||e.ctrlKey||e.altKey) return;
            if(e.key==="?"||e.key==="h"||e.key==="H"){ e.preventDefault(); var m=document.getElementById("nfxModal"); if(m.classList.contains("on")) m.classList.remove("on"); else m.classList.add("on"); }
            if(e.key==="t"||e.key==="T"){ e.preventDefault(); var b=document.getElementById("nfxTheatre"); if(b) b.click(); }
            if(e.key==="s"||e.key==="S"){ if(!e.shiftKey){ e.preventDefault(); var s=document.getElementById("nfxShot"); if(s) s.click(); } }
            if(e.key==="i"||e.key==="I"){ e.preventDefault(); var sk=document.getElementById("nfxSkip"); if(sk) sk.click(); }
            if(e.key==="Escape"){ var m2=document.getElementById("nfxModal"); if(m2) m2.classList.remove("on"); }
        });
    }

    /* ---------- Hook into existing wall/trending + newly_uploaded ---------- */
    function enhanceExistingCards(){
        // Wall cards (created synchronously) - patch after a short delay to catch them
        function patchWall(){
            var wall=document.getElementById("wall");
            if(!wall) return;
            var cards=wall.querySelectorAll(".poster");
            cards.forEach(function(card){
                if(card._nfxDone) return; card._nfxDone=true;
                var titleEl=card.querySelector(".p-title");
                var title=titleEl ? titleEl.textContent.trim() : "";
                if(!title) return;
                var tagEl=card.querySelector(".p-sub");
                var tag=tagEl ? tagEl.textContent.trim() : "";
                var link=card.getAttribute("href")||"";
                var style=card.getAttribute("style")||"";
                var hueMatch=style.match(/--h1:\s*(\d+)/);
                var hue=hueMatch?parseInt(hueMatch[1],10):null;
                var item={ label:title, year:"", tag:tag, sub:tag, hue:hue, link:link, poster:"" };
                // find poster img src if already loaded
                var img=card.querySelector(".poster-img");
                if(img && img.src && img.src.indexOf("itunes")!==-1) item.poster=img.src;
                attachHeart(card, item);
                enableTrailerOnCard(card, title);
            });
        }
        function patchTrending(){
            var trending=document.getElementById("trending");
            if(!trending) return;
            var cards=trending.querySelectorAll(".trend");
            cards.forEach(function(card){
                if(card._nfxDone) return; card._nfxDone=true;
                var titleEl=card.querySelector(".trend-body strong");
                var title=titleEl?titleEl.textContent.trim():"";
                if(!title) return;
                var subEl=card.querySelector(".trend-body small");
                var sub=subEl?subEl.textContent.trim():"";
                var link=card.getAttribute("href")||"";
                var style=card.getAttribute("style")||"";
                var hueMatch=style.match(/--h1:\s*(\d+)/);
                var hue=hueMatch?parseInt(hueMatch[1],10):null;
                var item={ label:title, tag:sub, sub:sub, hue:hue, link:link };
                attachHeart(card, item);
                enableTrailerOnCard(card, title);
            });
        }
        // Try now and also after delays (posters load async)
        setTimeout(function(){ patchWall(); patchTrending(); }, 800);
        setTimeout(function(){ patchWall(); patchTrending(); }, 2500);
        setTimeout(function(){ patchWall(); patchTrending(); }, 5000);
        // Observe wall for new nodes (if user navigates)
        try{
            var observer=new MutationObserver(function(){ patchWall(); patchTrending(); });
            var wall=document.getElementById("wall"); if(wall) observer.observe(wall,{childList:true});
            var tr=document.getElementById("trending"); if(tr) observer.observe(tr,{childList:true});
        } catch(e){}
    }

    function hookNewlyUploaded(){
        // Newly uploaded cards are created dynamically by newly_uploaded.js
        var grid=document.getElementById("nuGrid");
        if(!grid) return;
        try{
            var obs=new MutationObserver(function(mutations){
                mutations.forEach(function(m){
                    m.addedNodes.forEach(function(node){
                        if(node.nodeType!==1) return;
                        if(node.classList.contains("nu-card")){
                            enhanceNuCard(node);
                        }
                        if(node.querySelectorAll){
                            node.querySelectorAll(".nu-card").forEach(enhanceNuCard);
                        }
                    });
                });
            });
            obs.observe(grid,{childList:true,subtree:true});
            // also patch existing after a bit
            setTimeout(function(){ grid.querySelectorAll(".nu-card").forEach(enhanceNuCard); }, 1500);
            setTimeout(function(){ grid.querySelectorAll(".nu-card").forEach(enhanceNuCard); }, 4000);
        } catch(e){}
    }
    function enhanceNuCard(card){
        if(card._nfxDone) return; card._nfxDone=true;
        var titleEl=card.querySelector(".nu-card-title");
        var title=titleEl?titleEl.textContent.trim():"";
        if(!title) return;
        var subEl=card.querySelector(".nu-card-sub");
        var sub=subEl?subEl.textContent.trim():"";
        var link=card.getAttribute("href")||"";
        var badge=card.querySelector(".nu-badge");
        var quality=badge?badge.textContent.trim():"";
        var item={ label:title, tag:quality+" · "+sub, sub:sub, hue:null, link:link };
        var img=card.querySelector(".nu-art img");
        if(img && img.src) item.poster=img.src;
        attachHeart(card, item);
        enableTrailerOnCard(card, title);
    }

    function hookComingSoon(){
        var grid=document.getElementById("csGrid");
        if(!grid) return;
        try{
            var obs=new MutationObserver(function(mutations){
                mutations.forEach(function(m){
                    m.addedNodes.forEach(function(node){
                        if(node.nodeType!==1) return;
                        if(node.classList.contains("cs-card") || node.classList.contains("coming-soon-card")){
                            enhanceCsCard(node);
                        }
                        if(node.querySelectorAll){
                            node.querySelectorAll(".cs-card, .coming-soon-card").forEach(enhanceCsCard);
                        }
                    });
                });
            });
            obs.observe(grid,{childList:true,subtree:true});
        } catch(e){}
    }
    function enhanceCsCard(card){
        if(card._nfxDone) return; card._nfxDone=true;
        var t=card.querySelector(".cs-title, .cs-card-title");
        var title=t?t.textContent.trim():"";
        if(title) enableTrailerOnCard(card, title);
    }

    /* ---------- Inject rails HTML if missing ---------- */
    function injectRails(){
        if(document.getElementById("nfxContinue") && document.getElementById("nfxMyList")) return;
        var trending=document.getElementById("trendingSection");
        var anchor=trending || document.getElementById("wallSection") || document.querySelector(".nu-section") || document.querySelector("main.wrap");
        if(!anchor) return;
        var html = ''
            + '<section class="nfx-rail" id="nfxContinue" hidden>'
            + '<div class="nfx-rail-head"><div><h2 class="nfx-rail-title">Continue <span>Watching</span></h2><p class="nfx-rail-sub">Jahan chhoda tha, wahi se start karo</p></div><span class="nfx-rail-count" id="nfxContinueCount">0 videos</span></div>'
            + '<div class="nfx-rail-grid" id="nfxContinueGrid"></div></section>'
            + '<section class="nfx-rail" id="nfxMyList" hidden>'
            + '<div class="nfx-rail-head"><div><h2 class="nfx-rail-title">My <span>List</span> ♥</h2><p class="nfx-rail-sub">Tumhare favourite movies — ek click me bot me search</p></div><span class="nfx-rail-count" id="nfxMyListCount">0 titles</span></div>'
            + '<div class="nfx-mylist-grid" id="nfxMyListGrid"></div></section>';
        // Insert before trendingSection if exists, else append to main
        if(trending && trending.parentNode){
            trending.insertAdjacentHTML("beforebegin", html);
        } else {
            var main=document.querySelector("main.wrap");
            if(main) main.insertAdjacentHTML("beforeend", html);
        }
    }

    /* ---------- Init ---------- */
    function init(){
        injectRails();
        renderCW();
        renderML();
        enhancePlayer();
        enhanceExistingCards();
        hookNewlyUploaded();
        hookComingSoon();

        // Hook video progress for Continue Watching
        var video=document.getElementById("video");
        if(video){
            var saveTimer=null;
            video.addEventListener("timeupdate", function(){
                clearTimeout(saveTimer);
                saveTimer=setTimeout(function(){ cwSaveCurrent(video.currentTime, video.duration); }, 900);
            });
            video.addEventListener("pause", function(){ cwSaveCurrent(video.currentTime, video.duration); });
            // also periodic
            setInterval(function(){ if(!video.paused && isFinite(video.duration)) cwSaveCurrent(video.currentTime, video.duration); }, 5000);
            // on loadedmetadata, try resume toast already handled by original - we just ensure CW rendered
            video.addEventListener("loadedmetadata", function(){ setTimeout(renderCW, 400); });
        }

        // expose for debug
        window.NfxPack={ cwGet:cwGet, mlGet:mlGet, renderCW:renderCW, renderML:renderML, clearCW:function(){cwSet([]);renderCW();}, clearML:function(){mlSet([]);renderML(); syncHearts();} };
    }

    if(document.readyState==="loading") document.addEventListener("DOMContentLoaded", init);
    else init();

    // Re-run after window.load (iTunes posters arrive late)
    window.addEventListener("load", function(){ setTimeout(function(){ enhanceExistingCards(); renderCW(); renderML(); }, 1200); });
})();
