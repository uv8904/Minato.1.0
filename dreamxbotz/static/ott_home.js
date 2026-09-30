/* ==========================================================================
   MinatoVerse OTT · storefront logic (Option A)
   --------------------------------------------------------------------------
   Shared by /home and /search.  Everything is progressive enhancement: the
   server already rendered the hero, the rails and the results, so this file
   only *adds* the trailer, My List, Continue Watching, live refresh, the
   detail sheet and the search box behaviour.

   It reads the same localStorage keys the watch page's Netflix Pack writes
   (`dx:cw:list`, `dx:mylist`, `dx:resume:<uid>`), so a movie you started in
   the player shows up here and vice versa.
   ========================================================================== */
(function () {
    "use strict";

    /* ------------------------------ boot --------------------------------- */
    function readJSON(id, fallback) {
        try {
            const el = document.getElementById(id);
            return el ? JSON.parse(el.textContent || "null") || fallback : fallback;
        } catch (e) { return fallback; }
    }

    const CONFIG = readJSON("ott-config", {});
    const BOOT = readJSON("ott-boot", {});
    const KEYS = (CONFIG && CONFIG.keys) || { continue: "dx:cw:list", mylist: "dx:mylist", resume: "dx:resume:" };
    const LIMITS = (CONFIG && CONFIG.limits) || {};
    const PATHS = {
        home: CONFIG.home_api || "/api/ott/home",
        search: CONFIG.search_api || "/api/ott/search",
        suggest: CONFIG.suggest_api || "/api/ott/suggest",
        movie: CONFIG.movie_api || "/api/ott/movie",
        searchPage: CONFIG.search_path || "/search",
        homePage: CONFIG.home_path || "/home"
    };
    const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    /* ------------------------------ store -------------------------------- */
    const store = {
        get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
        set(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
        json(key, fallback) {
            try { const raw = window.localStorage.getItem(key); return raw ? JSON.parse(raw) : fallback; }
            catch (e) { return fallback; }
        },
        setJSON(key, value) { try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ignore */ } }
    };

    function safeText(value, limit) {
        let text = String(value === undefined || value === null ? "" : value)
            .replace(/[\u0000-\u001f\u007f]/g, "").trim();
        if (limit && text.length > limit) { text = text.slice(0, limit - 1) + "…"; }
        return text;
    }

    function toast(message) {
        const el = document.getElementById("otToast");
        if (!el) { return; }
        el.textContent = safeText(message, 120);
        el.classList.add("on");
        window.clearTimeout(el._timer);
        el._timer = window.setTimeout(() => el.classList.remove("on"), 2400);
    }

    function beacon(event, extra) {
        try {
            const title = safeText((document.querySelector(".ot-slide.is-active") || {}).dataset ?
                document.querySelector(".ot-slide.is-active").dataset.title : document.title, 120);
            const payload = JSON.stringify(Object.assign({ title: title, event: event, uid: "" }, extra || {}));
            if (navigator.sendBeacon) {
                navigator.sendBeacon("/api/analytics/track", new Blob([payload], { type: "application/json" }));
            } else {
                fetch("/api/analytics/track", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: payload, keepalive: true
                }).catch(() => {});
            }
        } catch (e) { /* analytics is decoration */ }
    }

    /* --------------------------- my list state --------------------------- */
    const MyList = {
        all() { const list = store.json(KEYS.mylist, []); return Array.isArray(list) ? list : []; },
        save(list) { store.setJSON(KEYS.mylist, list.slice(0, 200)); },
        has(id) { return this.all().some((item) => item && item.id === id); },
        add(card) {
            const list = this.all().filter((item) => item && item.id !== card.id);
            list.unshift(entry(card));
            this.save(list);
        },
        remove(id) { this.save(this.all().filter((item) => item && item.id !== id)); },
        toggle(card) {
            if (this.has(card.id)) { this.remove(card.id); return false; }
            this.add(card); return true;
        }
    };

    function entry(card) {
        return {
            id: card.id, title: safeText(card.title, 120), year: card.year || null,
            poster: card.poster || "", backdrop: card.backdrop || "", quality: card.quality || "",
            rating: card.rating || null, genres: card.genres || [], link: card.link || card.deeplink || "",
            search: card.search || "", added: Date.now()
        };
    }

    const ContinueWatching = {
        all() { const list = store.json(KEYS.continue, []); return Array.isArray(list) ? list : []; },
        fresh() {
            const now = Date.now();
            return this.all()
                .filter((item) => item && (now - (item.updated || 0)) < 60 * 864e5)
                .sort((a, b) => (b.updated || 0) - (a.updated || 0));
        },
        progress(id) {
            const row = this.all().find((item) => item && item.uid === id);
            if (!row) { return 0; }
            const pct = Number(row.pct);
            if (isFinite(pct) && pct > 0) { return Math.max(1, Math.min(99, Math.round(pct))); }
            const seconds = Number(row.progress), duration = Number(row.duration);
            if (isFinite(seconds) && isFinite(duration) && duration > 0) {
                return Math.max(1, Math.min(99, Math.round((seconds / duration) * 100)));
            }
            return 0;
        },
        remove(uid) {
            const next = this.all().filter((item) => item && item.uid !== uid);
            this.save(next);
        },
        save(list) { store.setJSON(KEYS.continue, list.slice(0, 40)); }
    };

    /* ------------------------------ cards -------------------------------- */
    function fromDOM(node) {
        const d = node.dataset || {};
        return {
            id: d.id, title: d.title, year: d.year ? parseInt(d.year, 10) : null,
            quality: d.quality || "", bucket: d.bucket || "", trailer: d.trailer || "",
            rating: d.rating ? parseFloat(d.rating) : null, genres: (d.genres || "").split(",").map((s) => s.trim()).filter(Boolean),
            link: d.link || d.deeplink || "", poster: d.poster || "", backdrop: d.backdrop || "",
            search: d.search || "", added: d.added || "", series: d.series === "1",
            release: d.release || "", waiting: d.waiting || 0
        };
    }

    function cardFromJSON(card) {
        return {
            id: card.id, title: card.title, year: card.year || null, quality: card.quality || "",
            bucket: card.bucket || "", trailer: card.trailer || "", rating: card.rating || null,
            genres: card.genres || [], link: card.link || card.deeplink || card.search_url || "",
            poster: card.poster || "",
            backdrop: card.backdrop || "", search: card.search_url || "", added: card.added || "",
            series: !!card.is_series, overview: card.overview || "", runtime: card.runtime || 0,
            release: card.release_date || "", waiting: card.waiting || 0, upcoming: !!card.upcoming
        };
    }

    function cardMarkup(card, options) {
        const opts = options || {};
        const id = safeText(card.id, 64), title = safeText(card.title, 120);
        const poster = card.poster ? card.poster + (card.poster.indexOf("?") === -1 ? "?w=320" : "&w=320") : "";
        const srcset = poster ? poster.replace("w=320", "w=320") + " 1x, " + poster.replace("w=320", "w=480") + " 2x" : "";
        const badge = card.release
            ? '<span class="ot-badge ot-badge-soon">' + safeText(card.release, 32) + "</span>"
            : (card.quality ? '<span class="ot-badge">' + safeText(String(card.quality).toUpperCase(), 12) + "</span>" : "");
        const sub = card.release
            ? "Coming · " + safeText(card.release, 24)
            : [card.year || "", card.quality ? String(card.quality).toUpperCase() : "", card.rating ? "★ " + card.rating : ""]
                .filter(Boolean).join(" · ");
        return '' +
            '<article class="ot-card' + (opts.rank ? " ot-card-rank" : "") + (card.release ? " ot-card-upcoming" : "") + '"' +
            ' data-id="' + id + '" data-title="' + title + '" data-year="' + (card.year || "") + '"' +
            ' data-quality="' + safeText(card.quality, 12) + '" data-bucket="' + safeText(card.bucket, 8) + '"' +
            ' data-trailer="' + safeText(card.trailer, 24) + '" data-rating="' + (card.rating || "") + '"' +
            ' data-genres="' + safeText((card.genres || []).join(", "), 160) + '"' +
            ' data-link="' + safeText(card.link, 300) + '" data-poster="' + safeText(card.poster, 300) + '"' +
            ' data-backdrop="' + safeText(card.backdrop, 300) + '" data-search="' + safeText(card.search, 300) + '"' +
            ' data-added="' + safeText(card.added, 40) + '" data-series="' + (card.series ? 1 : 0) + '"' +
            (card.release ? ' data-release="' + safeText(card.release, 32) + '"' : "") +
            (card.waiting ? ' data-waiting="' + parseInt(card.waiting, 10) + '"' : "") + '>' +
            (opts.rank ? '<span class="ot-rank" aria-hidden="true">' + opts.rank + "</span>" : "") +
            '<a class="ot-card-art" href="' + safeText(card.link || "#", 300) + '" aria-label="' + title +
            (card.year ? " (" + card.year + ")" : "") + '">' +
            (poster
                ? '<img src="' + poster + '" srcset="' + srcset + '" alt="' + title + ' poster" loading="lazy" decoding="async" width="320" height="480">'
                : '<span class="ot-card-ph" aria-hidden="true"><i>MV</i></span>') +
            '<span class="ot-card-shade"></span>' + badge +
            '<button class="ot-fav" type="button" data-role="mylist" aria-pressed="false" aria-label="Add ' + title + ' to My List" title="My List">' +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20s-7-4.3-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.7-7 9-7 9Z"></path></svg>' +
            "</button>" +
            '<span class="ot-card-hover"><span class="ot-card-play" aria-hidden="true">' +
            '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5.2v13.6a1 1 0 0 0 1.5.9l11-6.8a1 1 0 0 0 0-1.8l-11-6.8A1 1 0 0 0 8 5.2Z"></path></svg></span>' +
            '<span class="ot-card-actions"><button class="ot-mini" type="button" data-role="info">ⓘ Details</button>' +
            (card.trailer ? '<button class="ot-mini" type="button" data-role="trailer">▶ Trailer</button>' : "") +
            "</span></span>" +
            '<span class="ot-progress" hidden><i style="width:0%"></i></span>' +
            "</a>" +
            '<div class="ot-card-body"><h3 class="ot-card-title">' + title + "</h3>" +
            '<p class="ot-card-sub">' + safeText(sub, 60) + "</p></div></article>";
    }

    function hydrateCard(node) {
        const card = fromDOM(node);
        if (!card.id) { return; }
        const fav = node.querySelector(".ot-fav");
        if (fav) {
            const has = MyList.has(card.id);
            fav.setAttribute("aria-pressed", has ? "true" : "false");
        }
        const pct = ContinueWatching.progress(card.id);
        const bar = node.querySelector(".ot-progress");
        if (bar && pct > 0) {
            bar.hidden = false;
            const fill = bar.querySelector("i");
            if (fill) { fill.style.width = pct + "%"; }
        }
        const upcoming = !!card.release;
        if (!upcoming) {
            const img = node.querySelector("img");
            if (img) {
                img.addEventListener("error", () => {
                    const art = node.querySelector(".ot-card-art");
                    if (art && !art.querySelector(".ot-card-ph")) {
                        const ph = document.createElement("span");
                        ph.className = "ot-card-ph";
                        ph.setAttribute("aria-hidden", "true");
                        ph.innerHTML = "<i>MV</i>";
                        art.insertBefore(ph, art.firstChild);
                        img.remove();
                    }
                }, { once: true });
            }
        }
    }

    /* ------------------------------ rails -------------------------------- */
    function railMarkup(rail) {
        const items = (rail.cards || []).map((card, index) =>
            cardMarkup(cardFromJSON(card), { rank: rail.kind === "trending" ? index + 1 : 0 })).join("");
        return '<section class="ot-rail" data-rail="' + safeText(rail.id, 64) + '" data-kind="' + safeText(rail.kind || "rail", 24) + '">' +
            '<header class="ot-rail-head"><div class="ot-rail-title-wrap">' +
            '<h2 class="ot-rail-title">' + (rail.icon ? '<span aria-hidden="true">' + safeText(rail.icon, 8) + "</span>" : "") +
            safeText(rail.title, 80) + "</h2>" +
            (rail.subtitle ? '<p class="ot-rail-sub">' + safeText(rail.subtitle, 140) + "</p>" : "") +
            "</div><div class=\"ot-rail-tools\">" +
            '<a class="ot-text-btn" href="' + PATHS.searchPage + (rail.genre ? "?genre=" + encodeURIComponent(rail.genre) : "") + '">See all</a>' +
            '<button class="ot-scroll-btn" type="button" data-dir="-1" aria-label="Scroll left"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M15 5l-7 7 7 7"></path></svg></button>' +
            '<button class="ot-scroll-btn" type="button" data-dir="1" aria-label="Scroll right"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M9 5l7 7-7 7"></path></svg></button>' +
            "</div></header>" +
            '<div class="ot-row" role="list" tabindex="0" aria-label="' + safeText(rail.title, 80) + '">' + items + "</div></section>";
    }

    function mountRails(rails, options) {
        const list = document.getElementById("otRailList");
        if (!list || !Array.isArray(rails)) { return; }
        const opts = options || {};
        if (opts.injectBefore) {
            const anchor = document.querySelector('[data-rail="' + opts.injectBefore + '"]');
            const html = rails.map(railMarkup).join("");
            if (anchor) { anchor.insertAdjacentHTML("beforebegin", html); }
            else { list.insertAdjacentHTML("beforeend", html); }
        } else {
            list.innerHTML = rails.map(railMarkup).join("");
        }
        hydrate(list);
    }

    function hydrate(root) {
        const scope = root || document;
        scope.querySelectorAll(".ot-card").forEach(hydrateCard);
    }

    function injectClientRails() {
        const list = document.getElementById("otRailList");
        if (!list) { return; }
        const existing = list.querySelector('[data-rail="mylist"]');
        if (existing) { existing.remove(); }

        const myList = MyList.all();
        if (myList.length) {
            const cards = myList.slice(0, parseInt(LIMITS.rail || 18, 10)).map((item) => ({
                id: item.id, title: item.title, year: item.year, poster: item.poster, backdrop: item.backdrop,
                quality: item.quality, rating: item.rating, genres: item.genres || [], link: item.link, search: item.search
            }));
            list.insertAdjacentHTML("afterbegin", railMarkup({
                id: "mylist", title: "My List", icon: "❤️", kind: "mylist",
                subtitle: myList.length + " saved title" + (myList.length === 1 ? "" : "s") + " · stored in this browser",
                cards: cards
            }));
        }

        const watching = ContinueWatching.fresh();
        const container = document.getElementById("otRailList");
        if (watching.length && container && !container.querySelector('[data-rail="continue"]')) {
            const cards = watching.slice(0, 12).map((item) => ({
                id: item.uid, title: safeText(item.title || item.name || "Continue watching", 120),
                poster: "", backdrop: item.poster || "", link: item.url || "", quality: "", genres: [],
                search: PATHS.searchPage + "?q=" + encodeURIComponent(item.title || item.name || "")
            }));
            container.insertAdjacentHTML("afterbegin", railMarkup({
                id: "continue", title: "Continue watching", icon: "▶", kind: "continue",
                subtitle: "Pick up right where you left off — resume is stored on this device",
                cards: cards
            }));
            const rail = container.querySelector('[data-rail="continue"]');
            if (rail) {
                rail.querySelectorAll(".ot-card").forEach((node) => {
                    const pct = ContinueWatching.progress(node.dataset.id);
                    const bar = node.querySelector(".ot-progress");
                    if (bar && pct > 0) { bar.hidden = false; const fill = bar.querySelector("i"); if (fill) { fill.style.width = pct + "%"; } }
                });
            }
        }
        hydrate(container);
    }

    function scrollRail(rail, direction) {
        const row = rail && rail.querySelector(".ot-row");
        if (!row) { return; }
        const amount = Math.max(240, row.clientWidth * 0.85) * direction;
        row.scrollBy({ left: amount, behavior: REDUCED ? "auto" : "smooth" });
    }

    function moveFocus(node, delta) {
        const rail = node.closest(".ot-rail");
        if (!rail) { return; }
        const cards = Array.prototype.slice.call(rail.querySelectorAll(".ot-card"));
        const index = cards.indexOf(node);
        const next = cards[index + delta];
        if (next) {
            next.querySelector(".ot-card-art").focus();
            next.scrollIntoView({ inline: "center", block: "nearest", behavior: REDUCED ? "auto" : "smooth" });
        }
    }

    /* ------------------------------- hero -------------------------------- */
    const Hero = {
        slides: [], index: 0, timer: null, trailerOn: false, hoverTimer: null,
        init() {
            const root = document.getElementById("otHero");
            if (!root || root.dataset.empty === "1") { return; }
            this.root = root;
            this.slides = Array.prototype.slice.call(root.querySelectorAll(".ot-slide"));
            if (!this.slides.length) { return; }
            this.dots = Array.prototype.slice.call(root.querySelectorAll(".ot-dot"));
            this.layer = document.getElementById("otTrailerLayer");

            const prev = document.getElementById("otHeroPrev");
            const next = document.getElementById("otHeroNext");
            if (prev) { prev.addEventListener("click", () => this.go(this.index - 1, true)); }
            if (next) { next.addEventListener("click", () => this.go(this.index + 1, true)); }
            this.dots.forEach((dot, i) => dot.addEventListener("click", () => this.go(i, true)));

            root.addEventListener("keydown", (event) => {
                if (event.key === "ArrowRight") { this.go(this.index + 1, true); }
                if (event.key === "ArrowLeft") { this.go(this.index - 1, true); }
            });

            document.addEventListener("visibilitychange", () => {
                if (document.hidden) { this.stopTimer(); this.stopTrailer(); }
                else { this.startTimer(); }
            });
            if (window.IntersectionObserver) {
                new IntersectionObserver((entries) => {
                    entries.forEach((entry) => {
                        if (entry.isIntersecting) { this.startTimer(); this.maybeAutoplay(); }
                        else { this.stopTimer(); this.stopTrailer(); }
                    });
                }, { threshold: 0.25 }).observe(root);
            } else {
                this.startTimer();
            }
            if (window.matchMedia && window.matchMedia("(hover: hover)").matches) {
                root.addEventListener("mouseenter", () => { this.hoverTimer = window.setTimeout(() => this.playTrailer(), 900); });
                root.addEventListener("mouseleave", () => { window.clearTimeout(this.hoverTimer); this.stopTrailer(); });
            }
            this.wire(root);
            if (!REDUCED) { this.startTimer(); }
            this.slides.forEach((slide) => { if (slide.dataset.active) { this.index = parseInt(slide.dataset.index, 10) || 0; } });
        },
        wire(root) {
            root.addEventListener("click", (event) => {
                const button = event.target.closest("[data-role]");
                if (!button || !root.contains(button)) { return; }
                const slide = button.closest(".ot-slide");
                const card = slide ? fromDOM(slide) : null;
                if (!card) { return; }
                const role = button.dataset.role;
                if (role === "mylist") {
                    event.preventDefault();
                    const added = MyList.toggle(card);
                    button.setAttribute("aria-pressed", added ? "true" : "false");
                    const label = button.querySelector("span");
                    if (label) { label.textContent = added ? "In My List" : "My List"; }
                    toast(added ? "Added to My List" : "Removed from My List");
                    injectClientRails();
                } else if (role === "info") {
                    event.preventDefault();
                    Sheet.open(card);
                } else if (role === "trailer-toggle") {
                    event.preventDefault();
                    if (this.trailerOn) { this.stopTrailer(); } else { this.playTrailer(true); }
                } else if (role === "play") {
                    beacon("ott_play", { title: card.title });
                }
            });
        },
        maybeAutoplay() {
            if (REDUCED) { return; }
            const slide = this.slides[this.index];
            if (slide && slide.dataset.trailer) { this.playTrailer(); }
        },
        go(index, manual) {
            if (!this.slides.length) { return; }
            const count = this.slides.length;
            this.index = ((index % count) + count) % count;
            this.slides.forEach((slide, i) => {
                const active = i === this.index;
                slide.classList.toggle("is-active", active);
                slide.setAttribute("data-active", active ? "1" : "0");
                if (active) {
                    slide.style.animation = "none";
                    void slide.offsetWidth;
                    slide.style.animation = "";
                }
            });
            (this.dots || []).forEach((dot, i) => {
                dot.classList.toggle("is-active", i === this.index);
                dot.setAttribute("aria-selected", i === this.index ? "true" : "false");
            });
            this.stopTrailer();
            if (manual) { this.startTimer(); }
        },
        startTimer() {
            if (REDUCED) { return; }
            this.stopTimer();
            this.timer = window.setInterval(() => this.go(this.index + 1, false), 10000);
        },
        stopTimer() { if (this.timer) { window.clearInterval(this.timer); this.timer = null; } },
        playTrailer(userAction) {
            const slide = this.slides[this.index];
            if (!slide || !this.layer) { return; }
            const key = safeText(slide.dataset.trailer, 24);
            if (!key || !/^[A-Za-z0-9_-]{6,20}$/.test(key)) { return; }
            if (this.trailerOn && this.layer.dataset.key === key) { return; }
            this.layer.dataset.key = key;
            this.layer.innerHTML = "";
            const frame = document.createElement("iframe");
            frame.src = "https://www.youtube-nocookie.com/embed/" + key +
                "?autoplay=1&mute=1&controls=0&loop=1&playlist=" + key +
                "&playsinline=1&modestbranding=1&rel=0&iv_load_policy=3&disablekb=1";
            frame.title = safeText(slide.dataset.title + " trailer", 120);
            frame.allow = "autoplay; encrypted-media; picture-in-picture";
            frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
            frame.setAttribute("tabindex", "-1");
            frame.addEventListener("load", () => {
                this.layer.hidden = false;
                this.layer.classList.add("on");
                const toggle = slide.querySelector('[data-role="trailer-toggle"]');
                if (toggle) { toggle.setAttribute("aria-pressed", "true"); }
            });
            this.layer.appendChild(frame);
            const close = document.createElement("button");
            close.type = "button";
            close.className = "ot-round ot-trailer-close";
            close.setAttribute("aria-label", "Close trailer");
            close.textContent = "✕";
            close.addEventListener("click", () => this.stopTrailer());
            this.layer.appendChild(close);
            this.trailerOn = true;
            if (userAction) { beacon("ott_trailer", { title: safeText(slide.dataset.title, 120) }); }
        },
        stopTrailer() {
            if (!this.layer || !this.trailerOn) { return; }
            this.layer.innerHTML = "";
            this.layer.hidden = true;
            this.layer.classList.remove("on");
            delete this.layer.dataset.key;
            this.trailerOn = false;
            this.slides.forEach((slide) => {
                const toggle = slide.querySelector('[data-role="trailer-toggle"]');
                if (toggle) { toggle.setAttribute("aria-pressed", "false"); }
            });
        }
    };

    /* --------------------------- detail sheet ---------------------------- */
    const Sheet = {
        card: null,
        init() {
            this.root = document.getElementById("otSheet");
            if (!this.root) { return; }
            this.title = document.getElementById("otSheetTitle");
            this.facts = document.getElementById("otSheetFacts");
            this.overview = document.getElementById("otSheetOverview");
            this.art = document.getElementById("otSheetArt");
            this.play = document.getElementById("otSheetPlay");
            this.listBtn = document.getElementById("otSheetList");
            this.trailerBtn = document.getElementById("otSheetTrailer");
            this.searchLink = document.getElementById("otSheetSearch");

            this.root.addEventListener("click", (event) => {
                if (event.target.closest('[data-role="close"]')) { this.close(); }
            });
            document.addEventListener("keydown", (event) => {
                if (event.key === "Escape" && !this.root.hidden) { this.close(); }
            });
            if (this.listBtn) {
                this.listBtn.addEventListener("click", () => {
                    if (!this.card) { return; }
                    const added = MyList.toggle(this.card);
                    this.listBtn.setAttribute("aria-pressed", added ? "true" : "false");
                    this.listBtn.textContent = added ? "In My List ✓" : "Add to My List";
                    toast(added ? "Added to My List" : "Removed from My List");
                    injectClientRails();
                });
            }
            if (this.trailerBtn) {
                this.trailerBtn.addEventListener("click", () => {
                    if (!this.card || !this.card.trailer) { return; }
                    this.close();
                    window.open("https://www.youtube.com/watch?v=" + encodeURIComponent(this.card.trailer), "_blank", "noopener");
                });
            }
        },
        open(card) {
            if (!this.root) { return; }
            this.card = card;
            this.render(card);
            this.root.hidden = false;
            document.body.style.overflow = "hidden";
            // Enrich with the server copy (overview, rating, trailer) when we have an id.
            if (card.id) {
                fetch(PATHS.movie + "/" + encodeURIComponent(card.id))
                    .then((response) => (response.ok ? response.json() : null))
                    .then((payload) => {
                        if (payload && payload.ok && payload.movie && this.card && this.card.id === payload.movie.id) {
                            this.card = Object.assign({}, this.card, cardFromJSON(payload.movie));
                            this.render(this.card);
                        }
                    })
                    .catch(() => {});
            }
        },
        render(card) {
            if (!this.title) { return; }
            this.title.textContent = safeText(card.title, 120);
            const facts = [];
            if (card.year) { facts.push(card.year); }
            if (card.quality) { facts.push(String(card.quality).toUpperCase()); }
            if (card.rating) { facts.push("★ " + card.rating); }
            if (card.runtime) { facts.push(card.runtime + " min"); }
            (card.genres || []).slice(0, 4).forEach((genre) => facts.push(genre));
            if (card.series) { facts.push("Series"); }
            if (card.added) { facts.push("added " + card.added); }
            this.facts.innerHTML = facts.map((fact) => "<span>" + safeText(fact, 40) + "</span>").join("");
            this.overview.textContent = card.overview || "Open the movie in the bot to stream or download it — every quality is listed there.";
            if (this.art) {
                if (card.poster) { this.art.src = card.poster + (card.poster.indexOf("?") === -1 ? "?w=480" : "&w=480"); this.art.alt = safeText(card.title + " poster", 120); }
                else { this.art.removeAttribute("src"); this.art.alt = ""; }
            }
            if (this.play) { this.play.href = card.link || (CONFIG.open_url || "#"); }
            if (this.listBtn) {
                const has = MyList.has(card.id);
                this.listBtn.setAttribute("aria-pressed", has ? "true" : "false");
                this.listBtn.textContent = has ? "In My List ✓" : "Add to My List";
            }
            if (this.trailerBtn) { this.trailerBtn.hidden = !card.trailer; }
            if (this.searchLink) {
                this.searchLink.href = card.search || (PATHS.searchPage + "?q=" + encodeURIComponent(card.title || ""));
            }
        },
        close() {
            if (!this.root) { return; }
            this.root.hidden = true;
            document.body.style.overflow = "";
            this.card = null;
        }
    };

    /* --------------------------- search box ------------------------------ */
    const Suggest = {
        init() {
            const input = document.getElementById("otSearchInput");
            const panel = document.getElementById("otSuggest");
            if (!input || !panel) { return; }
            this.input = input; this.panel = panel;
            this.items = []; this.active = -1;
            let timer = null;

            input.addEventListener("input", () => {
                window.clearTimeout(timer);
                const value = input.value.trim();
                const clear = document.getElementById("otSearchClear");
                if (clear) { clear.hidden = !value; }
                if (value.length < 2) { this.close(); return; }
                timer = window.setTimeout(() => this.fetch(value), 160);
            });
            input.addEventListener("keydown", (event) => this.keydown(event));
            input.addEventListener("focus", () => {
                if (this.input.value.trim().length >= 2) { this.fetch(this.input.value.trim()); }
            });
            document.addEventListener("click", (event) => {
                if (!panel.contains(event.target) && event.target !== input) { this.close(); }
            });
            document.addEventListener("keydown", (event) => {
                if (event.key === "/" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName) && !document.activeElement.isContentEditable) {
                    event.preventDefault();
                    input.focus();
                }
            });
            this.panel.addEventListener("click", (event) => {
                const item = event.target.closest("[data-id]");
                if (!item) { return; }
                const node = document.querySelector('.ot-card[data-id="' + item.dataset.id + '"]');
                if (node) {
                    this.close();
                    node.querySelector(".ot-card-art").focus();
                    node.scrollIntoView({ block: "center", behavior: REDUCED ? "auto" : "smooth" });
                } else {
                    window.location.href = PATHS.searchPage + "?q=" + encodeURIComponent(item.dataset.title || "");
                }
            });
        },
        async fetch(query) {
            try {
                const response = await window.fetch(PATHS.suggest + "?q=" + encodeURIComponent(query) +
                    "&limit=" + (LIMITS.suggest || 8));
                if (!response.ok) { return; }
                const payload = await response.json();
                this.render((payload && payload.items) || []);
            } catch (e) { /* suggestions are optional */ }
        },
        render(items) {
            this.items = items;
            this.active = -1;
            if (!items.length) {
                this.panel.innerHTML = '<p class="ot-suggest-empty">No title matched — press Enter to search anyway.</p>';
                this.panel.hidden = false;
                return;
            }
            this.panel.innerHTML = items.map((item, index) => {
                const poster = item.poster ? item.poster + (item.poster.indexOf("?") === -1 ? "?w=200" : "&w=200") : "";
                return '<button type="button" class="ot-suggest-item" role="option" data-id="' + safeText(item.id, 64) +
                    '" data-title="' + safeText(item.title, 120) + '" data-index="' + index + '">' +
                    (poster ? '<img class="ot-suggest-thumb" src="' + poster + '" alt="" loading="lazy" decoding="async">' : "") +
                    '<span class="ot-suggest-text"><strong>' + safeText(item.title, 120) + "</strong>" +
                    "<small>" + (item.year ? safeText(item.year, 8) + " · " : "") + "open in the bot</small></span></button>";
            }).join("");
            this.panel.hidden = false;
            this.input.setAttribute("aria-expanded", "true");
        },
        keydown(event) {
            const options = Array.prototype.slice.call(this.panel.querySelectorAll(".ot-suggest-item"));
            if (!options.length || this.panel.hidden) { return; }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                this.active = event.key === "ArrowDown"
                    ? (this.active + 1) % options.length
                    : (this.active - 1 + options.length) % options.length;
                options.forEach((option, index) => option.setAttribute("aria-selected", index === this.active ? "true" : "false"));
            } else if (event.key === "Enter" && this.active >= 0) {
                event.preventDefault();
                options[this.active].click();
            } else if (event.key === "Escape") {
                this.close();
            }
        },
        close() {
            this.panel.hidden = true;
            this.input.setAttribute("aria-expanded", "false");
            this.active = -1;
        }
    };

    /* ------------------------- global interactions ----------------------- */
    function wireDocument() {
        document.addEventListener("click", (event) => {
            const roleNode = event.target.closest("[data-role]");
            if (roleNode) {
                const card = roleNode.closest(".ot-card");
                if (card && roleNode.dataset.role === "mylist") {
                    event.preventDefault();
                    const data = fromDOM(card);
                    const added = MyList.toggle(data);
                    roleNode.setAttribute("aria-pressed", added ? "true" : "false");
                    toast(added ? "Added to My List · " + safeText(data.title, 40) : "Removed from My List");
                    injectClientRails();
                    return;
                }
                if (card && roleNode.dataset.role === "info") {
                    event.preventDefault();
                    Sheet.open(fromDOM(card));
                    return;
                }
                if (card && roleNode.dataset.role === "trailer") {
                    event.preventDefault();
                    const data = fromDOM(card);
                    if (data.trailer) { window.open("https://www.youtube.com/watch?v=" + encodeURIComponent(data.trailer), "_blank", "noopener"); }
                    return;
                }
            }
            if (event.target.closest('[data-role="scroll-mylist"]')) {
                const rail = document.querySelector('[data-rail="mylist"]');
                if (rail) { rail.scrollIntoView({ block: "start", behavior: REDUCED ? "auto" : "smooth" }); }
                else { toast("Your list is empty — tap the ♡ on any poster"); }
                return;
            }
            const scrollBtn = event.target.closest(".ot-scroll-btn");
            if (scrollBtn) {
                scrollRail(scrollBtn.closest(".ot-rail"), parseInt(scrollBtn.dataset.dir, 10) || 1);
            }
        });

        document.addEventListener("keydown", (event) => {
            const art = event.target.closest && event.target.closest(".ot-card-art");
            if (!art) { return; }
            const card = art.closest(".ot-card");
            if (event.key === "ArrowRight") { event.preventDefault(); moveFocus(card, 1); }
            if (event.key === "ArrowLeft") { event.preventDefault(); moveFocus(card, -1); }
            if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); Sheet.open(fromDOM(card)); }
        });

        const nav = document.getElementById("otNav");
        if (nav) {
            const onScroll = () => nav.setAttribute("data-scrolled", window.scrollY > 24 ? "1" : "0");
            window.addEventListener("scroll", onScroll, { passive: true });
            onScroll();
        }

        const genreBtn = document.getElementById("otGenreBtn");
        const genreMenu = document.getElementById("otGenreMenu");
        if (genreBtn && genreMenu) {
            genreBtn.addEventListener("click", () => {
                const open = genreMenu.hidden;
                genreMenu.hidden = !open;
                genreBtn.setAttribute("aria-expanded", open ? "true" : "false");
            });
            document.addEventListener("click", (event) => {
                if (!genreMenu.hidden && !genreMenu.contains(event.target) && !genreBtn.contains(event.target)) {
                    genreMenu.hidden = true;
                    genreBtn.setAttribute("aria-expanded", "false");
                }
            });
        }

        const refresh = document.getElementById("otRefreshBtn");
        if (refresh) {
            refresh.addEventListener("click", () => { refresh.disabled = true; refresh.textContent = "Refreshing…"; loadHome(true); });
        }
    }

    /* --------------------------- live refresh ---------------------------- */
    let refreshing = false;

    async function loadHome(force) {
        if (refreshing) { return; }
        refreshing = true;
        try {
            const response = await window.fetch(PATHS.home + "?light=1", { headers: { Accept: "application/json" } });
            if (!response.ok) { throw new Error("HTTP " + response.status); }
            const payload = await response.json();
            if (!payload || !payload.ok) { throw new Error("payload"); }
            const scroller = document.getElementById("otRails");
            const scrolled = window.scrollY;
            mountRails(payload.rails || []);
            injectClientRails();
            const status = document.getElementById("otRailsStatusText");
            if (status) {
                status.textContent = (payload.counts && payload.counts.movies ? payload.counts.movies : 0) + " titles · " +
                    ((payload.rails || []).length) + " rails · updated " + (payload.updated_at || "just now");
            }
            if (force) { toast("Catalogue refreshed"); }
            if (scrolled) { window.scrollTo({ top: scrolled, behavior: "auto" }); }
            if (scroller) { scroller.dataset.loaded = "1"; }
        } catch (e) {
            if (force) { toast("Could not refresh — try again"); }
        } finally {
            refreshing = false;
            const refresh = document.getElementById("otRefreshBtn");
            if (refresh) { refresh.disabled = false; refresh.textContent = "Refresh"; }
        }
    }

    function startPolling() {
        const seconds = parseInt(CONFIG.poll, 10);
        if (!seconds || seconds < 15 || REDUCED) { return; }
        window.setInterval(() => {
            if (document.hidden) { return; }
            loadHome(false);
        }, seconds * 1000);
    }

    /* ------------------------------- init -------------------------------- */
    function mountBootRails() {
        // The server rendered only the first rails into the HTML (fast first
        // paint); the bootstrap carries the whole list, so mount the rest
        // without another round trip.
        const rails = (BOOT && BOOT.rails) || [];
        if (!rails.length || !document.getElementById("otRailList")) { return; }
        const rendered = document.querySelectorAll("#otRailList .ot-rail").length;
        if (rails.length > rendered) { mountRails(rails); }
    }

    function init() {
        wireDocument();
        Suggest.init();
        Sheet.init();
        Hero.init();
        mountBootRails();
        injectClientRails();
        if (document.body.classList.contains("ot-body-search")) { return; }
        startPolling();
        if (document.documentElement.dataset.view === "mylist") {
            window.setTimeout(() => {
                const rail = document.querySelector('[data-rail="mylist"]');
                if (rail) { rail.scrollIntoView({ block: "start", behavior: "smooth" }); }
                else { toast("My List is empty — tap ♡ on a poster to save it"); }
            }, 260);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    /* Public helpers reused by ott_search.js */
    window.MinatoOTT = {
        CONFIG: CONFIG, BOOT: BOOT, PATHS: PATHS,
        MyList, ContinueWatching, cardMarkup, cardFromJSON, fromDOM, hydrate, toast, beacon,
        openSheet: (card) => Sheet.open(card), injectClientRails, railMarkup
    };
}());
